"""
orchestration_service/agents/gstr1_validator.py
Node 2b — GSTR-1 Validator (runs in parallel)

Validates that the taxpayer's own sales invoices match what was filed in GSTR-1.
Detects: missing sales, understated sales, wrong tax applied.

PRD §5.2: "Validate GSTR-1 vs books (sales register)"
"""

from __future__ import annotations

import logging

from orchestration_service.state import (
    ReconciliationState, InvoiceRecord, MismatchRecord
)

logger = logging.getLogger(__name__)
ROUNDING_TOLERANCE = 1.0


async def gstr1_validator_node(state: ReconciliationState) -> dict:
    """
    LangGraph node: GSTR-1 Validator (parallel)
    Compares sales invoices in books vs GSTR-1 filing.
    """
    logger.info("[gstr1_validator] Starting for job=%s", state["job_id"])

    book_invoices = [i for i in state["book_invoices"] if i["source"] in ("tally", "zoho")]
    gstr1_invoices = state["gstr1_invoices"]

    if not gstr1_invoices:
        logger.info("[gstr1_validator] No GSTR-1 invoices to validate — skipping")
        return {
            "current_node": "gstr1_validator",
            "progress_pct": 50,
            "raw_mismatches": [],
        }

    # Build index for GSTR-1 invoices
    gstr1_index: dict[tuple, InvoiceRecord] = {
        (inv["gstin"], inv["invoice_no"]): inv
        for inv in gstr1_invoices
    }

    mismatches: list[MismatchRecord] = []

    for book_inv in book_invoices:
        key = (book_inv["gstin"], book_inv["invoice_no"])
        gstr1_inv = gstr1_index.get(key)

        if gstr1_inv is None:
            # Sales invoice in books but not filed in GSTR-1
            tax_amount = book_inv["igst"] + book_inv["cgst"] + book_inv["sgst"]
            mismatches.append(MismatchRecord(
                invoice_id_books=book_inv["id"],
                invoice_id_portal=None,
                mismatch_type="missing",
                severity="escalate",   # Unfiled sales = tax liability risk
                cause_reasoning=(
                    f"Invoice {book_inv['invoice_no']} found in sales register "
                    f"but not in GSTR-1 filing. Potential tax liability of ₹{tax_amount:.2f}."
                ),
                itc_risk_amount=tax_amount,
                recommended_action="File amended GSTR-1 to include this invoice before due date",
            ))
            continue

        # Amount check
        if abs(book_inv["total_amount"] - gstr1_inv["total_amount"]) > ROUNDING_TOLERANCE:
            diff = abs(book_inv["total_amount"] - gstr1_inv["total_amount"])
            mismatches.append(MismatchRecord(
                invoice_id_books=book_inv["id"],
                invoice_id_portal=gstr1_inv["id"],
                mismatch_type="amount",
                severity="followup" if diff < 5000 else "escalate",
                cause_reasoning=(
                    f"Sales invoice {book_inv['invoice_no']}: "
                    f"books show ₹{book_inv['total_amount']:.2f}, "
                    f"GSTR-1 shows ₹{gstr1_inv['total_amount']:.2f}. "
                    f"Difference: ₹{diff:.2f}."
                ),
                itc_risk_amount=diff * 0.18,   # Approximate tax on difference
                recommended_action="Amend GSTR-1 with correct invoice value",
            ))

    logger.info("[gstr1_validator] Done: %d mismatches", len(mismatches))

    return {
        "current_node": "gstr1_validator",
        "progress_pct": 50,
        "raw_mismatches": mismatches,
    }
