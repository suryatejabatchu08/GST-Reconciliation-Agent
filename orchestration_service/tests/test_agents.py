"""
orchestration_service/tests/test_agents.py
Unit tests for matching and classification agents.
Uses pre-built InvoiceRecord fixtures — no DB or LLM calls.
"""

import pytest
from decimal import Decimal

from orchestration_service.state import InvoiceRecord, MismatchRecord, make_initial_state
from orchestration_service.agents.gstr2a_matcher import (
    gstr2a_matcher_node, _build_index, _amount_differs, _tax_head_differs, _severity
)
from orchestration_service.agents.gstr1_validator import gstr1_validator_node


# ── Fixtures ───────────────────────────────────────────────

def make_invoice(**kwargs) -> InvoiceRecord:
    """Create a test InvoiceRecord with sensible defaults."""
    defaults = {
        "id": "inv-001",
        "gstin": "29AABCT1332L1ZT",
        "invoice_no": "INV-001",
        "invoice_date": "2024-03-15",
        "supplier_name": "Tech Supplies Pvt Ltd",
        "taxable_amount": 50000.0,
        "igst": 0.0,
        "cgst": 4500.0,
        "sgst": 4500.0,
        "cess": 0.0,
        "total_amount": 59000.0,
        "source": "tally",
        "description": "Office chairs",
    }
    defaults.update(kwargs)
    return InvoiceRecord(**defaults)


def make_state(**kwargs) -> dict:
    """Create a minimal ReconciliationState-like dict for testing."""
    base = make_initial_state(
        job_id="job-001",
        client_id="client-001",
        gstin="29ABCDE1234F1Z5",
        filing_period="2024-03",
        ca_user_id="ca@test.com",
    )
    base.update(kwargs)
    return base


# ── GSTR-2A Matcher Tests ──────────────────────────────────

class TestGSTR2AMatcher:

    @pytest.mark.asyncio
    async def test_clean_match_produces_no_mismatch(self):
        """Identical book and portal invoice → no mismatch."""
        book = make_invoice(id="book-001", source="tally")
        portal = make_invoice(id="portal-001", source="gstr2a")

        state = make_state(book_invoices=[book], gstr2a_invoices=[portal])
        result = await gstr2a_matcher_node(state)
        assert result["raw_mismatches"] == []

    @pytest.mark.asyncio
    async def test_missing_portal_invoice_detected(self):
        """Book invoice with no match in GSTR-2A → missing mismatch."""
        book = make_invoice(id="book-001", invoice_no="INV-UNIQUE")
        portal = make_invoice(id="portal-001", invoice_no="INV-DIFFERENT")

        state = make_state(book_invoices=[book], gstr2a_invoices=[portal])
        result = await gstr2a_matcher_node(state)

        mismatches = result["raw_mismatches"]
        assert any(m["mismatch_type"] == "missing" for m in mismatches)

    @pytest.mark.asyncio
    async def test_amount_mismatch_detected(self):
        """₹100 difference in total_amount → amount mismatch."""
        book = make_invoice(id="book-001", total_amount=59000.0)
        portal = make_invoice(id="portal-001", total_amount=58900.0)  # ₹100 less

        state = make_state(book_invoices=[book], gstr2a_invoices=[portal])
        result = await gstr2a_matcher_node(state)

        mismatches = result["raw_mismatches"]
        assert any(m["mismatch_type"] == "amount" for m in mismatches)

    @pytest.mark.asyncio
    async def test_rounding_difference_not_flagged(self):
        """₹0.50 difference → within tolerance, no mismatch."""
        book = make_invoice(id="book-001", total_amount=59000.00)
        portal = make_invoice(id="portal-001", total_amount=59000.50)

        state = make_state(book_invoices=[book], gstr2a_invoices=[portal])
        result = await gstr2a_matcher_node(state)
        assert result["raw_mismatches"] == []

    @pytest.mark.asyncio
    async def test_tax_head_mismatch_detected(self):
        """Same total, different IGST vs CGST+SGST → tax_head mismatch."""
        book = make_invoice(id="b-001", igst=0.0, cgst=4500.0, sgst=4500.0, total_amount=59000.0)
        portal = make_invoice(id="p-001", igst=9000.0, cgst=0.0, sgst=0.0, total_amount=59000.0)

        state = make_state(book_invoices=[book], gstr2a_invoices=[portal])
        result = await gstr2a_matcher_node(state)

        mismatches = result["raw_mismatches"]
        assert any(m["mismatch_type"] == "tax_head" for m in mismatches)

    @pytest.mark.asyncio
    async def test_no_portal_invoices_returns_empty(self):
        """No GSTR-2A data at all → no mismatches, clean state."""
        state = make_state(book_invoices=[make_invoice()], gstr2a_invoices=[])
        result = await gstr2a_matcher_node(state)
        assert result["raw_mismatches"] == []

    def test_severity_escalates_high_itc(self):
        """ITC risk >= ₹5000 → escalate severity."""
        assert _severity(5000.0, "amount") == "escalate"
        assert _severity(10000.0, "missing") == "escalate"

    def test_severity_followup_low_itc(self):
        """ITC risk < ₹5000 and not missing → followup."""
        assert _severity(100.0, "amount") == "followup"
        assert _severity(4999.0, "tax_head") == "followup"

    def test_severity_auto_for_rounding(self):
        """Amount mismatch with risk <= ₹1 → auto."""
        assert _severity(0.5, "amount") == "auto"


class TestGSTR1Validator:

    @pytest.mark.asyncio
    async def test_clean_match_no_mismatch(self):
        """Book and GSTR-1 match exactly → no mismatch."""
        book = make_invoice(id="b-001", invoice_no="SALES-001", source="tally")
        gstr1 = make_invoice(id="g-001", invoice_no="SALES-001", source="gstr1")

        state = make_state(book_invoices=[book], gstr1_invoices=[gstr1])
        result = await gstr1_validator_node(state)
        assert result["raw_mismatches"] == []

    @pytest.mark.asyncio
    async def test_unfiled_sales_detected(self):
        """Invoice in books not matched in GSTR-1 → escalate mismatch."""
        book = make_invoice(id="b-001", invoice_no="SALES-UNFILED", source="tally")
        # Provide a different GSTR-1 invoice so validator runs but finds no match
        other_gstr1 = make_invoice(id="g-001", invoice_no="SALES-OTHER", source="gstr1")

        state = make_state(book_invoices=[book], gstr1_invoices=[other_gstr1])
        result = await gstr1_validator_node(state)

        mismatches = result["raw_mismatches"]
        assert len(mismatches) == 1
        assert mismatches[0]["mismatch_type"] == "missing"
        assert mismatches[0]["severity"] == "escalate"

    @pytest.mark.asyncio
    async def test_empty_gstr1_skipped_gracefully(self):
        """No GSTR-1 data → node returns empty mismatches without error."""
        state = make_state(book_invoices=[], gstr1_invoices=[])
        result = await gstr1_validator_node(state)
        assert result["raw_mismatches"] == []
