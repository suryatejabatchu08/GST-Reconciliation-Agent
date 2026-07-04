"""
ingestion_service/tests/test_tally_csv.py
Unit tests for the Tally CSV parser.
"""

import pytest
from pathlib import Path
from decimal import Decimal

from ingestion_service.parsers.tally_csv import TallyCSVParser

SAMPLE_DIR = Path(__file__).parent / "sample_data"


class TestTallyCSVParser:

    def test_parses_correct_number_of_invoices(self):
        """Should parse 4 data rows, skip the total row."""
        content = (SAMPLE_DIR / "sample_tally.csv").read_bytes()
        invoices = TallyCSVParser(content, "sample_tally.csv").parse()
        assert len(invoices) == 4

    def test_indian_number_format_parsed(self):
        """Amounts like '50,000.00' and '1,15,000.00' should parse correctly."""
        content = (SAMPLE_DIR / "sample_tally.csv").read_bytes()
        invoices = TallyCSVParser(content, "sample_tally.csv").parse()
        pur_001 = next(i for i in invoices if i.invoice_no == "PUR-CSV-001")
        assert pur_001.taxable_amount == Decimal("50000.00")

    def test_total_row_skipped(self):
        """Row starting with 'Total' should be skipped."""
        content = (SAMPLE_DIR / "sample_tally.csv").read_bytes()
        invoices = TallyCSVParser(content, "sample_tally.csv").parse()
        invoice_nos = [i.invoice_no for i in invoices]
        assert "Total" not in invoice_nos

    def test_gstin_normalised(self):
        """GSTINs should be 15-char uppercase."""
        content = (SAMPLE_DIR / "sample_tally.csv").read_bytes()
        invoices = TallyCSVParser(content, "sample_tally.csv").parse()
        for inv in invoices:
            assert len(inv.gstin) == 15
            assert inv.gstin == inv.gstin.upper()

    def test_empty_file_raises(self):
        with pytest.raises(ValueError, match="empty"):
            TallyCSVParser(b"", "empty.csv").parse()

    def test_file_with_no_recognisable_headers_raises(self):
        bad_csv = b"Col1,Col2,Col3\nfoo,bar,baz\n"
        with pytest.raises(ValueError, match="Could not detect"):
            TallyCSVParser(bad_csv, "bad.csv").parse()


class TestZohoCSVParser:

    def test_parses_valid_invoices_skips_void(self):
        """Should parse 4 valid invoices, skip 1 void."""
        from ingestion_service.parsers.zoho_csv import ZohoCSVParser
        content = (SAMPLE_DIR / "sample_zoho.csv").read_bytes()
        invoices = ZohoCSVParser(content, "sample_zoho.csv").parse()
        assert len(invoices) == 4, f"Expected 4, got {len(invoices)}"

    def test_void_invoice_not_in_results(self):
        from ingestion_service.parsers.zoho_csv import ZohoCSVParser
        content = (SAMPLE_DIR / "sample_zoho.csv").read_bytes()
        invoices = ZohoCSVParser(content, "sample_zoho.csv").parse()
        invoice_nos = [i.invoice_no for i in invoices]
        assert "ZBILL-VOID" not in invoice_nos

    def test_amounts_parsed_correctly(self):
        from ingestion_service.parsers.zoho_csv import ZohoCSVParser
        content = (SAMPLE_DIR / "sample_zoho.csv").read_bytes()
        invoices = ZohoCSVParser(content, "sample_zoho.csv").parse()
        zbill_001 = next(i for i in invoices if i.invoice_no == "ZBILL-001")
        assert zbill_001.taxable_amount == Decimal("50000.00")
        assert zbill_001.cgst == Decimal("4500.00")
        assert zbill_001.sgst == Decimal("4500.00")
