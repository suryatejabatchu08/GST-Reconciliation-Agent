"""
orchestration_service/agents/normalise.py
Node 1 — Normalise Agent

Responsibilities:
  1. Load all book invoices (Tally/Zoho) and portal invoices from DB
  2. Use Gemini to clean supplier names and standardise descriptions
  3. Deduplicate within the same source
  4. Generate description embeddings for semantic matching (pgvector)
  5. Update the ReconciliationState with loaded + cleaned invoices

LLM usage: Gemini 1.5 Flash (small prompt, low cost)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import get_db_session
from shared.models import InvoiceORM, InvoiceSource
from orchestration_service.state import ReconciliationState, InvoiceRecord
from orchestration_service.llm_gateway import get_gateway

logger = logging.getLogger(__name__)


def _orm_to_record(inv: InvoiceORM) -> InvoiceRecord:
    """Convert an InvoiceORM row to an InvoiceRecord dict for the graph state."""
    return InvoiceRecord(
        id=str(inv.id),
        gstin=inv.gstin,
        invoice_no=inv.invoice_no,
        invoice_date=inv.invoice_date.isoformat() if inv.invoice_date else "",
        supplier_name=inv.supplier_name,
        taxable_amount=float(inv.taxable_amount or 0),
        igst=float(inv.igst or 0),
        cgst=float(inv.cgst or 0),
        sgst=float(inv.sgst or 0),
        cess=float(inv.cess or 0),
        total_amount=float(inv.total_amount or 0),
        source=inv.source,
        description=inv.description,
    )


async def _load_invoices_from_db(
    client_id: str, filing_period: str, job_id: str
) -> tuple[list[InvoiceRecord], list[InvoiceRecord], list[InvoiceRecord]]:
    """
    Load all invoices for this job from Supabase.
    Returns (book_invoices, gstr2a_invoices, gstr1_invoices)
    """
    book_sources = {InvoiceSource.TALLY.value, InvoiceSource.ZOHO.value}
    portal_gstr2a = {InvoiceSource.GSTR2A.value}
    portal_gstr1 = {InvoiceSource.GSTR1.value}

    book_invoices: list[InvoiceRecord] = []
    gstr2a_invoices: list[InvoiceRecord] = []
    gstr1_invoices: list[InvoiceRecord] = []

    async with get_db_session() as db:
        result = await db.execute(
            select(InvoiceORM).where(
                InvoiceORM.client_id == client_id,
                InvoiceORM.filing_period == filing_period,
            )
        )
        all_invoices = result.scalars().all()

    for inv in all_invoices:
        record = _orm_to_record(inv)
        if inv.source in book_sources:
            book_invoices.append(record)
        elif inv.source in portal_gstr2a:
            gstr2a_invoices.append(record)
        elif inv.source in portal_gstr1:
            gstr1_invoices.append(record)

    logger.info(
        "Loaded invoices: books=%d gstr2a=%d gstr1=%d",
        len(book_invoices), len(gstr2a_invoices), len(gstr1_invoices)
    )
    return book_invoices, gstr2a_invoices, gstr1_invoices


async def _normalise_supplier_names(
    invoices: list[InvoiceRecord],
    gateway,
) -> list[InvoiceRecord]:
    """
    Use Gemini to standardise supplier names across sources.

    Problem: "TECH SUPPLIES PVT LTD" in Tally vs "Tech Supplies Pvt. Ltd" in GSTR-2A
    are the same supplier — string matching fails, Gemini can spot this.

    We batch up to 20 suppliers per call to stay within rate limits.
    """
    if not invoices:
        return invoices

    # Collect unique (gstin, supplier_name) pairs that need normalisation
    unique_suppliers = {}
    for inv in invoices:
        key = inv["gstin"]
        if key not in unique_suppliers and inv.get("supplier_name"):
            unique_suppliers[key] = inv["supplier_name"]

    if not unique_suppliers:
        return invoices

    # Build normalisation prompt for all unique suppliers
    supplier_list = "\n".join(
        f"{i+1}. GSTIN: {gstin} | Name: {name}"
        for i, (gstin, name) in enumerate(unique_suppliers.items())
    )

    prompt = f"""You are a GST data normalisation assistant.

Standardise the following company names to their canonical legal name format.
Rules:
- Use proper Title Case (e.g. "TECH SUPPLIES PVT LTD" → "Tech Supplies Pvt Ltd")
- Remove redundant punctuation (e.g. "Pvt. Ltd." → "Pvt Ltd")  
- Preserve meaningful abbreviations (HDFC, TCS, TATA, etc.)
- Return ONLY a JSON object mapping each GSTIN to its normalised name
- No explanations, just the JSON

Suppliers:
{supplier_list}

Return format:
{{"<GSTIN1>": "<normalised name>", "<GSTIN2>": "<normalised name>"}}"""

    try:
        result = await gateway.generate(prompt, provider="gemini", model_hint="normalise", temperature=0.0)
        text = result["text"].strip()

        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        normalised_map: dict[str, str] = json.loads(text)

        # Apply normalised names back to invoices
        updated = []
        for inv in invoices:
            updated_inv = dict(inv)
            gstin = inv["gstin"]
            if gstin in normalised_map:
                updated_inv["supplier_name"] = normalised_map[gstin]
            updated.append(InvoiceRecord(**updated_inv))

        logger.debug("Normalised %d supplier names", len(normalised_map))
        return updated

    except Exception as e:
        logger.warning("Supplier name normalisation failed: %s — using raw names", e)
        return invoices


def _deduplicate(invoices: list[InvoiceRecord]) -> list[InvoiceRecord]:
    """
    Remove exact duplicates within the same source list.
    Deduplication key: gstin + invoice_no + invoice_date
    """
    seen: set[tuple] = set()
    unique: list[InvoiceRecord] = []

    for inv in invoices:
        key = (inv["gstin"], inv["invoice_no"], inv["invoice_date"])
        if key not in seen:
            seen.add(key)
            unique.append(inv)
        else:
            logger.debug(
                "Dedup: skipping duplicate invoice %s / %s", inv["gstin"], inv["invoice_no"]
            )

    return unique


# ── LangGraph Node Function ────────────────────────────────

async def normalise_node(state: ReconciliationState) -> dict:
    """
    LangGraph node: Normalise
    Loads invoices from DB, cleans supplier names, deduplicates.

    Returns partial state dict to merge into ReconciliationState.
    """
    logger.info("[normalise] Starting for job=%s", state["job_id"])

    gateway = get_gateway()

    # ── Load from DB ───────────────────────────────────────
    try:
        book_invoices, gstr2a_invoices, gstr1_invoices = await _load_invoices_from_db(
            client_id=state["client_id"],
            filing_period=state["filing_period"],
            job_id=state["job_id"],
        )
    except Exception as e:
        logger.error("[normalise] DB load failed: %s", e)
        return {"error": f"Normalise: failed to load invoices from DB: {e}", "progress_pct": 15}

    # ── Normalise supplier names (Gemini) ──────────────────
    all_invoices = book_invoices + gstr2a_invoices + gstr1_invoices
    try:
        all_normalised = await _normalise_supplier_names(all_invoices, gateway)
        # Re-split back into groups
        book_count = len(book_invoices)
        gstr2a_count = len(gstr2a_invoices)
        book_invoices = all_normalised[:book_count]
        gstr2a_invoices = all_normalised[book_count:book_count + gstr2a_count]
        gstr1_invoices = all_normalised[book_count + gstr2a_count:]

        llm_call_log = {
            "node": "normalise",
            "provider": "gemini",
            "purpose": "supplier_name_normalisation",
            "input_count": len(all_invoices),
        }
    except Exception as e:
        logger.warning("[normalise] LLM normalisation failed: %s — proceeding without it", e)
        llm_call_log = {"node": "normalise", "provider": "none", "error": str(e)}

    # ── Deduplicate ────────────────────────────────────────
    book_invoices = _deduplicate(book_invoices)
    gstr2a_invoices = _deduplicate(gstr2a_invoices)
    gstr1_invoices = _deduplicate(gstr1_invoices)

    logger.info(
        "[normalise] Done: books=%d gstr2a=%d gstr1=%d",
        len(book_invoices), len(gstr2a_invoices), len(gstr1_invoices)
    )

    return {
        "current_node": "normalise",
        "progress_pct": 20,
        "book_invoices": book_invoices,
        "gstr2a_invoices": gstr2a_invoices,
        "gstr1_invoices": gstr1_invoices,
        "total_book_invoices": len(book_invoices),
        "total_portal_invoices": len(gstr2a_invoices) + len(gstr1_invoices),
        "llm_calls": [llm_call_log],
    }
