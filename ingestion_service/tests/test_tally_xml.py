"""
ingestion_service/tests/test_tally_xml.py
Unit tests for the Tally XML parser.
"""

import pytest
from pathlib import Path
from decimal import Decimal

from ingestion_service.parsers.tally_xml import TallyXMLParser

SAMPLE_DIR = Path(__file__).parent / "sample_data"


def load_sample(filename: str) -> bytes:
    return (SAMPLE_DIR / filename).read_bytes()


class TestTallyXMLParser:

    def test_parses_correct_number_of_invoices(self):
        """Should parse 3 purchase vouchers, skip 1 receipt voucher."""
        content = load_sample("sample_tally.xml")
        parser = TallyXMLParser(content, "sample_tally.xml")
        invoices = parser.parse()
        assert len(invoices) == 3, f"Expected 3, got {len(invoices)}"

    def test_intrastate_invoice_has_cgst_sgst(self):
        """PUR-001 is intrastate — should have CGST + SGST, no IGST."""
        content = load_sample("sample_tally.xml")
        invoices = TallyXMLParser(content, "sample_tally.xml").parse()
        pur_001 = next((i for i in invoices if i.invoice_no == "PUR-001"), None)

        assert pur_001 is not None, "PUR-001 not found"
        assert pur_001.cgst > Decimal("0"), "Expected CGST > 0 for intrastate invoice"
        assert pur_001.sgst > Decimal("0"), "Expected SGST > 0 for intrastate invoice"
        assert pur_001.igst == Decimal("0"), "Expected IGST = 0 for intrastate invoice"

    def test_interstate_invoice_has_igst(self):
        """PUR-002 is interstate — should have IGST, no CGST/SGST."""
        content = load_sample("sample_tally.xml")
        invoices = TallyXMLParser(content, "sample_tally.xml").parse()
        pur_002 = next((i for i in invoices if i.invoice_no == "PUR-002"), None)

        assert pur_002 is not None, "PUR-002 not found"
        assert pur_002.igst > Decimal("0"), "Expected IGST > 0 for interstate invoice"
        assert pur_002.cgst == Decimal("0"), "Expected CGST = 0 for interstate invoice"

    def test_gstin_normalised_to_uppercase(self):
        """GSTIN should be normalised: uppercase, no dashes."""
        content = load_sample("sample_tally.xml")
        invoices = TallyXMLParser(content, "sample_tally.xml").parse()
        for inv in invoices:
            assert inv.gstin == inv.gstin.upper(), f"GSTIN not uppercase: {inv.gstin}"
            assert "-" not in inv.gstin, f"GSTIN contains dash: {inv.gstin}"
            assert len(inv.gstin) == 15, f"GSTIN wrong length: {inv.gstin}"

    def test_date_parsed_correctly(self):
        """Tally XML dates are in YYYYMMDD format."""
        from datetime import date
        content = load_sample("sample_tally.xml")
        invoices = TallyXMLParser(content, "sample_tally.xml").parse()
        pur_001 = next(i for i in invoices if i.invoice_no == "PUR-001")
        assert pur_001.invoice_date == date(2024, 3, 15)

    def test_parse_summary_stats(self):
        """parse_summary should report correct counts."""
        content = load_sample("sample_tally.xml")
        parser = TallyXMLParser(content, "sample_tally.xml")
        invoices = parser.parse()
        summary = parser.parse_summary
        assert summary["parsed_ok"] == len(invoices)

    def test_invalid_xml_raises_value_error(self):
        """Non-XML content should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid XML"):
            TallyXMLParser(b"this is not xml at all", "bad.xml").parse()

    def test_empty_file_raises(self):
        """Empty bytes should raise ValueError."""
        with pytest.raises(Exception):
            TallyXMLParser(b"", "empty.xml").parse()
