"""
notification_service/tests/test_email_builder.py
Unit tests for email_builder — no network calls, no SMTP.
"""

import pytest
from notification_service.email_builder import (
    fmt_inr, build_supplier_followup, build_ca_escalation, Email
)


class TestFmtInr:
    def test_zero(self):
        assert fmt_inr(0) == "₹0.00"

    def test_small_amount(self):
        assert fmt_inr(500.0) == "₹500.00"

    def test_thousands(self):
        assert fmt_inr(59000.0) == "₹59,000.00"

    def test_lakhs(self):
        assert fmt_inr(150000.0) == "₹1,50,000.00"

    def test_crores(self):
        assert fmt_inr(1000000.0) == "₹10,00,000.00"

    def test_negative(self):
        result = fmt_inr(-9000.0)
        assert result.startswith("-₹")

    def test_decimal_preserved(self):
        assert fmt_inr(1234.56) == "₹1,234.56"


class TestSupplierFollowup:
    def _build(self, **overrides):
        defaults = dict(
            supplier_email="supplier@test.com",
            supplier_name="Tech Supplies Pvt Ltd",
            invoice_no="INV-001",
            invoice_date="2024-03-15",
            total_amount=59000.0,
            filing_period="2024-03",
            cause_reasoning="Invoice not found in GSTR-2A",
            recommended_action="Please file GSTR-1",
            itc_risk=9000.0,
            ca_email="ca@firm.com",
            ca_name="CA Sharma",
            firm_name="Sharma & Co",
        )
        defaults.update(overrides)
        return build_supplier_followup(**defaults)

    def test_returns_email_object(self):
        email = self._build()
        assert isinstance(email, Email)

    def test_to_address_set(self):
        email = self._build(supplier_email="vendor@abc.com")
        assert email.to == "vendor@abc.com"

    def test_subject_contains_invoice_no(self):
        email = self._build(invoice_no="PUR-XYZ-123")
        assert "PUR-XYZ-123" in email.subject

    def test_subject_contains_period(self):
        email = self._build(filing_period="2024-03")
        assert "2024-03" in email.subject

    def test_body_contains_supplier_name(self):
        email = self._build(supplier_name="Acme Corp")
        assert "Acme Corp" in email.body_text

    def test_body_contains_itc_amount(self):
        email = self._build(itc_risk=9000.0)
        assert "9,000.00" in email.body_text

    def test_html_is_non_empty(self):
        email = self._build()
        assert "<html>" in email.body_html
        assert len(email.body_html) > 100


class TestCAEscalation:
    def _escalations(self):
        return [
            {
                "invoice_no": "INV-001",
                "supplier_name": "Test Supplier",
                "gstin": "29AABCT1332L1ZT",
                "mismatch_type": "missing",
                "itc_risk": 45000.0,
                "cause_reasoning": "Invoice not in GSTR-2A",
                "recommended_action": "Contact supplier",
            }
        ]

    def _build(self, **overrides):
        defaults = dict(
            ca_email="ca@firm.com",
            ca_name="CA Sharma",
            client_name="Acme Pvt Ltd",
            client_gstin="29ABCDE1234F1Z5",
            filing_period="2024-03",
            total_invoices=100,
            clean_matches=90,
            auto_count=2,
            followup_count=5,
            escalations=self._escalations(),
            total_itc=45000.0,
        )
        defaults.update(overrides)
        return build_ca_escalation(**defaults)

    def test_returns_email_object(self):
        email = self._build()
        assert isinstance(email, Email)

    def test_subject_contains_escalation_count(self):
        email = self._build()
        assert "1" in email.subject   # 1 escalation

    def test_subject_contains_gstin(self):
        email = self._build(client_gstin="29ABCDE1234F1Z5")
        assert "29ABCDE1234F1Z5" in email.subject

    def test_body_contains_client_name(self):
        email = self._build(client_name="Acme Pvt Ltd")
        assert "Acme Pvt Ltd" in email.body_text

    def test_body_contains_total_itc(self):
        email = self._build(total_itc=45000.0)
        assert "45,000.00" in email.body_text

    def test_no_escalations_still_builds(self):
        """Edge case: escalation email with empty list still renders."""
        email = self._build(escalations=[], total_itc=0.0)
        assert isinstance(email, Email)
