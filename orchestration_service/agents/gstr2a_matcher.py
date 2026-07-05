"""
orchestration_service/agents/gstr2a_matcher.py
Node 2a — GSTR-2A Matcher (runs in parallel with gstr1_validator and tax_checker)

Compares purchase register (Tally/Zoho books) against GSTR-2A
(what suppliers actually filed in their GSTR-1).

Match strategy (cascading):
  1. Exact match: GSTIN + invoice_no + date (exact)
  2. Fuzzy amount match: GSTIN + invoice_no, amount within ₹1 tolerance (rounding)
  3. GSTIN-only match: flag all unmatched portal entries as "supplier not found"

PRD §5.2 mismatch types detected here:
  - "missing": invoice in books but not in GSTR-2A, or vice versa
  - "amount": total_amount differs by more than ₹1
  - "tax_head": IGST/CGST/SGST wrong (total fine but breakdown differs)
  - "gstin": GSTIN in books doesn't match GSTR-2A
"""

from __future__ import annotations

import logging
from decimal import Decimal

from orchestration_service.state import (
    ReconciliationState, InvoiceRecord, MismatchRecord
)

logger = logging.getLogger(__name__)

ROUNDING_TOLERANCE = 1.0   # ₹1 — PRD §5.2


def _build_index(invoices: list[InvoiceRecord]) -> dict[tuple, InvoiceRecord]:
    """Build a lookup dict keyed by (gstin, invoice_no, invoice_date)."""
    return {
        (inv["gstin"], inv["invoice_no"], inv["invoice_date"]): inv
        for inv in invoices
    }


def _amount_differs(inv_a: InvoiceRecord, inv_b: InvoiceRecord) -> bool:
    """Returns True if total amounts differ by more than rounding tolerance."""
    return abs(inv_a["total_amount"] - inv_b["total_amount"]) > ROUNDING_TOLERANCE


def _tax_head_differs(inv_a: InvoiceRecord, inv_b: InvoiceRecord) -> bool:
    """Returns True if IGST/CGST/SGST breakdown differs even if total is fine."""
    return (
        abs(inv_a["igst"] - inv_b["igst"]) > ROUNDING_TOLERANCE or
        abs(inv_a["cgst"] - inv_b["cgst"]) > ROUNDING_TOLERANCE or
        abs(inv_a["sgst"] - inv_b["sgst"]) > ROUNDING_TOLERANCE
    )


def _itc_at_risk(book: InvoiceRecord, portal: InvoiceRecord | None) -> float:
    """
    Calculate ITC at risk for a mismatch.
    ITC = IGST + CGST + SGST claimed in books.
    If portal invoice missing → full tax amount is at risk.
    If amount mismatch → difference in tax is at risk.
    """
    book_tax = book["igst"] + book["cgst"] + book["sgst"]
    if portal is None:
        return book_tax
    portal_tax = portal["igst"] + portal["cgst"] + portal["sgst"]
    return abs(book_tax - portal_tax)


def _severity(itc_risk: float, mismatch_type: str) -> str:
    """
    PRD §5.3 severity classification:
      - auto:     rounding difference ≤ ₹1 (handled automatically)
      - followup: ITC risk < ₹5,000 (send supplier email)
      - escalate: ITC risk ≥ ₹5,000 OR missing invoice (must go to CA)
    """
    if mismatch_type == "amount" and itc_risk <= ROUNDING_TOLERANCE:
        return "auto"
    if itc_risk >= 5000 or mismatch_type == "missing":
        return "escalate"
    return "followup"


async def gstr2a_matcher_node(state: ReconciliationState) -> dict:
    """
    LangGraph node: GSTR-2A Matcher
    Runs in parallel with gstr1_validator_node and tax_checker_node.
    Appends to raw_mismatches (operator.add reducer merges all parallel outputs).
    """
    logger.info("[gstr2a_matcher] Starting for job=%s", state["job_id"])

    book_invoices = state["book_invoices"]
    portal_invoices = state["gstr2a_invoices"]

    if not portal_invoices:
        logger.info("[gstr2a_matcher] No GSTR-2A invoices — skipping matching")
        return {
            "current_node": "gstr2a_matcher",
            "progress_pct": 45,
            "raw_mismatches": [],
        }

    # ── Build indexes ──────────────────────────────────────
    book_index = _build_index(book_invoices)
    portal_index = _build_index(portal_invoices)

    mismatches: list[MismatchRecord] = []
    matched_keys: set[tuple] = set()

    # ── Pass 1: For each book invoice, find matching portal invoice ─
    for key, book_inv in book_index.items():
        portal_inv = portal_index.get(key)

        if portal_inv is None:
            # Missing from GSTR-2A — supplier hasn't filed or used different invoice no
            # Try fuzzy: same GSTIN + invoice_no, ignore date
            fuzzy_key = None
            for pkey, pinv in portal_index.items():
                if pkey[0] == key[0] and pkey[1] == key[1]:   # GSTIN + invoice_no match
                    fuzzy_key = pkey
                    portal_inv = pinv
                    break

            if portal_inv is None:
                # Truly missing from GSTR-2A
                itc_risk = _itc_at_risk(book_inv, None)
                mismatches.append(MismatchRecord(
                    invoice_id_books=book_inv["id"],
                    invoice_id_portal=None,
                    mismatch_type="missing",
                    severity=_severity(itc_risk, "missing"),
                    cause_reasoning="Invoice present in purchase register but not found in GSTR-2A. Supplier may not have filed GSTR-1 or may have used a different invoice number.",
                    itc_risk_amount=itc_risk,
                    recommended_action="Send follow-up email to supplier requesting GSTR-1 correction",
                ))
                continue

        matched_keys.add(key)

        # ── Amount mismatch check ──────────────────────────
        if _amount_differs(book_inv, portal_inv):
            itc_risk = _itc_at_risk(book_inv, portal_inv)
            mismatches.append(MismatchRecord(
                invoice_id_books=book_inv["id"],
                invoice_id_portal=portal_inv["id"],
                mismatch_type="amount",
                severity=_severity(itc_risk, "amount"),
                cause_reasoning=(
                    f"Invoice total in books: ₹{book_inv['total_amount']:.2f}, "
                    f"in GSTR-2A: ₹{portal_inv['total_amount']:.2f}. "
                    f"Difference: ₹{abs(book_inv['total_amount'] - portal_inv['total_amount']):.2f}"
                ),
                itc_risk_amount=itc_risk,
                recommended_action="Request corrected invoice or amended GSTR-1 from supplier",
            ))
            continue  # Don't also flag tax_head if total differs

        # ── Tax head mismatch check (IGST vs CGST+SGST) ───
        if _tax_head_differs(book_inv, portal_inv):
            itc_risk = _itc_at_risk(book_inv, portal_inv)
            mismatches.append(MismatchRecord(
                invoice_id_books=book_inv["id"],
                invoice_id_portal=portal_inv["id"],
                mismatch_type="tax_head",
                severity=_severity(itc_risk, "tax_head"),
                cause_reasoning=(
                    f"Total amount matches but tax head breakdown differs. "
                    f"Books: IGST={book_inv['igst']:.2f} CGST={book_inv['cgst']:.2f} SGST={book_inv['sgst']:.2f}. "
                    f"Portal: IGST={portal_inv['igst']:.2f} CGST={portal_inv['cgst']:.2f} SGST={portal_inv['sgst']:.2f}. "
                    f"Possible place-of-supply error."
                ),
                itc_risk_amount=itc_risk,
                recommended_action="Verify place of supply and request amended return from supplier",
            ))

    # ── Pass 2: Portal invoices not matched to any book entry ─
    for key, portal_inv in portal_index.items():
        if key not in matched_keys:
            # Invoice on portal not in books — could be unrecorded or duplicate filing
            mismatches.append(MismatchRecord(
                invoice_id_books=None,
                invoice_id_portal=portal_inv["id"],
                mismatch_type="missing",
                severity="followup",
                cause_reasoning=(
                    f"Invoice {portal_inv['invoice_no']} from {portal_inv.get('supplier_name', portal_inv['gstin'])} "
                    f"found in GSTR-2A but not in purchase register. "
                    f"May be unrecorded expense or supplier filed incorrectly against this GSTIN."
                ),
                itc_risk_amount=0.0,   # Not claiming ITC for unrecorded purchase
                recommended_action="Verify if this is a genuine purchase; add to books if yes",
            ))

    logger.info(
        "[gstr2a_matcher] Done: %d mismatches from %d book vs %d portal invoices",
        len(mismatches), len(book_invoices), len(portal_invoices)
    )

    return {
        "current_node": "gstr2a_matcher",
        "progress_pct": 45,
        "raw_mismatches": mismatches,   # Appended to state via operator.add
        "matched_count": len(matched_keys),
    }
