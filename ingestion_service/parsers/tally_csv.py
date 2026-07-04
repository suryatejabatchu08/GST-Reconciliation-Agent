"""
ingestion_service/parsers/tally_csv.py
Fallback CSV parser for Tally manual exports.

When CAs export from Tally via:
  Gateway of Tally → Export → Excel/CSV

The column headers vary slightly by Tally version, but common patterns are:
  Date, Voucher No, Party Name, GSTIN/UIN, Voucher Type,
  Taxable Amount, IGST, CGST, SGST/UTGST, Total Amount
"""

from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal

from ingestion_service.parsers.base import (
    BaseParser,
    ParsedInvoice,
    normalise_gstin,
    parse_decimal,
    parse_date,
)

logger = logging.getLogger(__name__)


# Column name aliases — maps many possible header names to our canonical names
# This handles different Tally versions and manual renames
COLUMN_ALIASES: dict[str, list[str]] = {
    "date":            ["date", "voucher date", "invoice date", "txn date", "transaction date"],
    "invoice_no":      ["voucher no", "voucher number", "invoice no", "invoice number", "vch no", "ref no", "reference"],
    "gstin":           ["gstin/uin", "gstin", "gst no", "gst number", "party gstin", "supplier gstin"],
    "supplier_name":   ["party name", "party's name", "supplier name", "ledger name", "name"],
    "taxable_amount":  ["taxable value", "taxable amount", "assessable value", "basic amount", "value"],
    "igst":            ["igst", "integrated gst", "igst amount"],
    "cgst":            ["cgst", "central gst", "cgst amount"],
    "sgst":            ["sgst", "sgst/utgst", "state gst", "sgst amount", "utgst", "utgst amount"],
    "cess":            ["cess", "cess amount"],
    "total_amount":    ["total", "total amount", "gross amount", "invoice amount", "amount"],
    "description":     ["narration", "description", "particulars", "item name", "goods/service"],
}


def _detect_column(header_row: list[str]) -> dict[str, int | None]:
    """
    Map canonical field names to column indices by fuzzy header matching.
    Returns a dict like {"date": 0, "invoice_no": 1, "gstin": 3, ...}
    """
    normalised_headers = [h.strip().lower() for h in header_row]
    mapping: dict[str, int | None] = {key: None for key in COLUMN_ALIASES}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised_headers:
                mapping[canonical] = normalised_headers.index(alias)
                break

    return mapping


def _get_cell(row: list[str], col_idx: int | None, default: str = "") -> str:
    """Safely get a cell value by column index."""
    if col_idx is None or col_idx >= len(row):
        return default
    return row[col_idx].strip()


class TallyCSVParser(BaseParser):
    """
    Parses Tally CSV/Excel exports (CSV format).
    Uses fuzzy header matching to handle different Tally versions.
    """

    def parse(self) -> list[ParsedInvoice]:
        """Parse CSV content and return normalised invoices."""
        try:
            text = self.content.decode("utf-8-sig")   # Strip BOM if present
        except UnicodeDecodeError:
            text = self.content.decode("latin-1")      # Fallback for Windows exports

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)

        if not rows:
            raise ValueError(f"{self.filename} is empty")

        # Find the header row (first row with recognisable column names)
        header_row_idx, col_map = self._find_header(rows)
        if header_row_idx is None:
            raise ValueError(
                f"Could not detect column headers in {self.filename}. "
                "Expected columns like: Date, Voucher No, GSTIN/UIN, IGST, CGST, SGST"
            )

        # Required columns check
        required = ["date", "invoice_no", "gstin"]
        missing = [f for f in required if col_map[f] is None]
        if missing:
            raise ValueError(
                f"Missing required columns in {self.filename}: {missing}. "
                f"Detected headers: {rows[header_row_idx]}"
            )

        invoices: list[ParsedInvoice] = []
        data_rows = rows[header_row_idx + 1:]

        for i, row in enumerate(data_rows, start=header_row_idx + 2):
            self._row_count += 1

            # Skip blank rows and subtotal/total rows
            if not any(cell.strip() for cell in row):
                self._row_count -= 1
                continue
            first_cell = _get_cell(row, 0).lower()
            if first_cell in ("total", "grand total", "subtotal", ""):
                self._row_count -= 1
                continue

            try:
                invoice = self._parse_row(row, col_map, i)
                invoices.append(invoice)
                self._success_count += 1
            except Exception as e:
                self._log_row_error(i, str(e))
                logger.warning("TallyCSV row %d skipped: %s", i, e)

        logger.info(
            "TallyCSV parse complete: %d/%d rows parsed from %s",
            self._success_count, self._row_count, self.filename
        )
        return invoices

    def _find_header(self, rows: list[list[str]]) -> tuple[int | None, dict]:
        """
        Scan the first 10 rows to find the header row.
        Returns (row_index, column_mapping) or (None, {}) if not found.
        """
        for idx, row in enumerate(rows[:10]):
            col_map = _detect_column(row)
            # Consider it a header if we can identify at least date + invoice_no + gstin
            if all(col_map[f] is not None for f in ["date", "invoice_no"]):
                return idx, col_map
        return None, {}

    def _parse_row(self, row: list[str], col_map: dict[str, int | None], row_num: int) -> ParsedInvoice:
        """Parse a single data row into a ParsedInvoice."""
        # ── Date ───────────────────────────────────────────
        raw_date = _get_cell(row, col_map["date"])
        invoice_date = parse_date(raw_date, "date")

        # ── Invoice number ─────────────────────────────────
        invoice_no = _get_cell(row, col_map["invoice_no"])
        if not invoice_no:
            raise ValueError("Missing invoice number")

        # ── GSTIN ──────────────────────────────────────────
        raw_gstin = _get_cell(row, col_map["gstin"])
        if not raw_gstin:
            raise ValueError(f"Missing GSTIN for invoice {invoice_no}")
        gstin = normalise_gstin(raw_gstin)

        # ── Optional fields ────────────────────────────────
        supplier_name = _get_cell(row, col_map["supplier_name"]) or None
        description = _get_cell(row, col_map["description"]) or None
        if description:
            description = description[:500]

        # ── Amounts ────────────────────────────────────────
        taxable_amount = parse_decimal(_get_cell(row, col_map["taxable_amount"]), "taxable_amount")
        igst = parse_decimal(_get_cell(row, col_map["igst"]), "igst")
        cgst = parse_decimal(_get_cell(row, col_map["cgst"]), "cgst")
        sgst = parse_decimal(_get_cell(row, col_map["sgst"]), "sgst")
        cess = parse_decimal(_get_cell(row, col_map["cess"]), "cess")

        raw_total = _get_cell(row, col_map["total_amount"])
        if raw_total:
            total_amount = parse_decimal(raw_total, "total_amount")
        else:
            total_amount = taxable_amount + igst + cgst + sgst + cess

        return ParsedInvoice(
            gstin=gstin,
            invoice_no=invoice_no,
            invoice_date=invoice_date,
            supplier_name=supplier_name,
            taxable_amount=taxable_amount,
            igst=igst,
            cgst=cgst,
            sgst=sgst,
            cess=cess,
            total_amount=total_amount,
            description=description,
            raw_data={"row_num": row_num, "raw_date": raw_date, "raw_gstin": raw_gstin},
        )
