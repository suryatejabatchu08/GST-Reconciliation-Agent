"""
orchestration_service/state.py
LangGraph shared state — passed between every node in the reconciliation graph.

All agents read from and write to ReconciliationState.
LangGraph merges returned dicts into the state automatically.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from typing_extensions import TypedDict, Annotated
import operator


# ──────────────────────────────────────────────────────────
# Sub-types used inside the state
# ──────────────────────────────────────────────────────────

class InvoiceRecord(TypedDict):
    """Normalised invoice row (used within the graph)."""
    id: str                     # DB UUID
    gstin: str
    invoice_no: str
    invoice_date: str           # ISO format "YYYY-MM-DD"
    supplier_name: Optional[str]
    taxable_amount: float
    igst: float
    cgst: float
    sgst: float
    cess: float
    total_amount: float
    source: str                 # "tally", "zoho", "gstr2a", "gstr1", "gstr3b"
    description: Optional[str]


class MismatchRecord(TypedDict):
    """A detected discrepancy between book data and portal data."""
    invoice_id_books: Optional[str]    # UUID of book invoice
    invoice_id_portal: Optional[str]   # UUID of portal invoice
    mismatch_type: str                 # "amount" | "missing" | "tax_head" | "duplicate" | "gstin"
    severity: str                      # "auto" | "followup" | "escalate"
    cause_reasoning: str               # LLM-generated explanation
    itc_risk_amount: float             # ITC at risk in INR
    recommended_action: str            # What the CA should do


class TaxLiabilitySummary(TypedDict):
    """GSTR-3B summary data for tax liability cross-check."""
    outward_taxable: float
    igst_payable: float
    cgst_payable: float
    sgst_payable: float
    itc_claimed_igst: float
    itc_claimed_cgst: float
    itc_claimed_sgst: float


# ──────────────────────────────────────────────────────────
# Main Graph State
# ──────────────────────────────────────────────────────────

class ReconciliationState(TypedDict):
    """
    Shared state object passed through the LangGraph reconciliation pipeline.

    LangGraph passes this dict between nodes. Each node returns a partial dict
    with only the keys it updated — LangGraph merges them automatically.

    Keys annotated with Annotated[list, operator.add] are "append-only":
    each node can add items without overwriting what previous nodes wrote.
    """

    # ── Job context ─────────────────────────────────────────
    job_id: str                         # UUID of the reconciliation job
    client_id: str                      # UUID of the client
    gstin: str                          # Taxpayer GSTIN
    filing_period: str                  # "YYYY-MM"
    ca_user_id: str                     # CA user identifier (for notifications)

    # ── Progress tracking ────────────────────────────────────
    current_node: str                   # Name of the node currently executing
    progress_pct: int                   # 0–100 (updated after each node)
    error: Optional[str]                # Set if any node hits a fatal error

    # ── Invoice data (populated by normalise node) ───────────
    book_invoices: list[InvoiceRecord]       # From Tally/Zoho (normalised)
    gstr2a_invoices: list[InvoiceRecord]     # From GST portal GSTR-2A
    gstr1_invoices: list[InvoiceRecord]      # From GST portal GSTR-1
    gstr3b_summary: Optional[TaxLiabilitySummary]

    # ── Mismatches (appended by matcher/validator nodes) ─────
    # operator.add means each parallel node's list is merged, not overwritten
    raw_mismatches: Annotated[list[MismatchRecord], operator.add]

    # ── Classification results ────────────────────────────────
    classified_mismatches: list[MismatchRecord]   # After Groq classification
    auto_fixable: list[MismatchRecord]
    needs_followup: list[MismatchRecord]
    needs_escalation: list[MismatchRecord]

    # ── Statistics ────────────────────────────────────────────
    total_book_invoices: int
    total_portal_invoices: int
    total_mismatches: int
    total_itc_at_risk: float            # Sum of itc_risk_amount across all mismatches
    matched_count: int                  # Invoices matched cleanly

    # ── LLM usage tracking (for cost monitoring) ─────────────
    llm_calls: Annotated[list[dict], operator.add]   # Each LLM call logged here


def make_initial_state(
    job_id: str,
    client_id: str,
    gstin: str,
    filing_period: str,
    ca_user_id: str,
) -> ReconciliationState:
    """Create a fresh state for a new reconciliation job."""
    return ReconciliationState(
        job_id=job_id,
        client_id=client_id,
        gstin=gstin,
        filing_period=filing_period,
        ca_user_id=ca_user_id,
        current_node="start",
        progress_pct=0,
        error=None,
        book_invoices=[],
        gstr2a_invoices=[],
        gstr1_invoices=[],
        gstr3b_summary=None,
        raw_mismatches=[],
        classified_mismatches=[],
        auto_fixable=[],
        needs_followup=[],
        needs_escalation=[],
        total_book_invoices=0,
        total_portal_invoices=0,
        total_mismatches=0,
        total_itc_at_risk=0.0,
        matched_count=0,
        llm_calls=[],
    )
