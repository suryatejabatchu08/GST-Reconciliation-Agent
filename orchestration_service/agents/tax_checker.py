"""
orchestration_service/agents/tax_checker.py
Node 2c — Tax Liability Checker (runs in parallel)

Cross-checks the aggregated IGST/CGST/SGST from book invoices
against the GSTR-3B summary return.

GSTR-3B is a monthly summary return — it doesn't have line-item invoices,
just aggregate numbers. If the sum of book invoices doesn't match GSTR-3B,
it's a potential compliance issue.

PRD §5.2: "Cross-check GSTR-3B tax liability"
"""

from __future__ import annotations

import logging
from typing import Optional

from orchestration_service.state import (
    ReconciliationState, MismatchRecord, TaxLiabilitySummary
)
from ingestion_service.gst_portal.client import GSTPortalClient

logger = logging.getLogger(__name__)
TOLERANCE = 10.0   # ₹10 tolerance for aggregate rounding differences


async def tax_checker_node(state: ReconciliationState) -> dict:
    """
    LangGraph node: Tax Liability Checker (parallel)
    Fetches GSTR-3B if not already in state, cross-checks totals.
    """
    logger.info("[tax_checker] Starting for job=%s", state["job_id"])

    # Fetch GSTR-3B from portal (mock or live)
    try:
        portal = GSTPortalClient()
        gstr3b_data = await portal.fetch_gstr3b(state["gstin"], state["filing_period"])
    except Exception as e:
        logger.warning("[tax_checker] Failed to fetch GSTR-3B: %s — skipping", e)
        return {
            "current_node": "tax_checker",
            "progress_pct": 55,
            "raw_mismatches": [],
        }

    # Parse GSTR-3B summary
    sup = gstr3b_data.get("sup_details", {})
    osup = sup.get("osup_det", {})
    itc = gstr3b_data.get("itc_elg", {})

    gstr3b_igst = float(osup.get("iamt", 0))
    gstr3b_cgst = float(osup.get("camt", 0))
    gstr3b_sgst = float(osup.get("samt", 0))

    # Sum book invoices (sales/outward)
    book_igst = sum(i["igst"] for i in state["book_invoices"])
    book_cgst = sum(i["cgst"] for i in state["book_invoices"])
    book_sgst = sum(i["sgst"] for i in state["book_invoices"])

    mismatches: list[MismatchRecord] = []

    # Check each tax head
    checks = [
        ("IGST", book_igst, gstr3b_igst),
        ("CGST", book_cgst, gstr3b_cgst),
        ("SGST", book_sgst, gstr3b_sgst),
    ]

    for tax_name, book_val, portal_val in checks:
        diff = abs(book_val - portal_val)
        if diff > TOLERANCE:
            mismatches.append(MismatchRecord(
                invoice_id_books=None,
                invoice_id_portal=None,
                mismatch_type="tax_head",
                severity="escalate" if diff >= 5000 else "followup",
                cause_reasoning=(
                    f"{tax_name} mismatch: books total ₹{book_val:.2f} vs "
                    f"GSTR-3B declared ₹{portal_val:.2f}. "
                    f"Difference of ₹{diff:.2f} requires reconciliation."
                ),
                itc_risk_amount=diff,
                recommended_action=(
                    f"Review {tax_name} entries and file amended GSTR-3B if required"
                ),
            ))

    # Store GSTR-3B summary in state
    tax_summary = TaxLiabilitySummary(
        outward_taxable=float(osup.get("txval", 0)),
        igst_payable=gstr3b_igst,
        cgst_payable=gstr3b_cgst,
        sgst_payable=gstr3b_sgst,
        itc_claimed_igst=0.0,
        itc_claimed_cgst=0.0,
        itc_claimed_sgst=0.0,
    )

    logger.info("[tax_checker] Done: %d tax-head mismatches", len(mismatches))

    return {
        "current_node": "tax_checker",
        "progress_pct": 55,
        "gstr3b_summary": tax_summary,
        "raw_mismatches": mismatches,
    }
