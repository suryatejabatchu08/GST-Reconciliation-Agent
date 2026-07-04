"""
ingestion_service/parsers/zoho_csv.py
Parser for Zoho Books CSV exports.

Export from Zoho Books:
  Accountant > Reports > Vendor Invoice Report > Export as CSV
  OR
  Purchases > Bills > Export as CSV

Zoho CSV has fairly consistent headers compared to Tally.
Standard columns: Invoice Date, Invoice Number, Vendor Name, GSTIN/UIN of Vendor,
  Taxable Amount, IGST, CGST, SGST, Total
"""

from __future__ import annotations

import csv
import io
import logging

from ingestion_service.parsers.base import (
    BaseParser,
    ParsedInvoice,
    normalise_gstin,
    parse_decimal,
    parse_date,
)

logger = logging.getLogger(__name__)


# Zoho Books CSV column name aliases
ZOHO_COLUMN_ALIASES: dict[str, list[str]] = {
    "date": [
        "invoice date", "bill date", "date", "transaction date",
        "created date", "document date"
    ],
    "invoice_no": [
        "invoice number", "bill number", "invoice#", "bill#",
        "document number", "reference number", "invoice no"
    ],
    "gstin": [
        "gstin/uin of vendor", "gstin/uin", "vendor gstin", "gstin",
        "gst identification number", "gst no", "supplier gstin"
    ],
    "supplier_name": [
        "vendor name", "supplier name", "party name", "contact name",
        "vendor", "supplier", "party"
    ],
    "taxable_amount": [
        "taxable amount", "taxable value", "sub total", "subtotal",
        "amount before tax", "assessable value"
    ],
    "igst": ["igst", "integrated tax", "igst amount"],
    "cgst": ["cgst", "central tax", "cgst amount"],
    "sgst": ["sgst", "state tax", "sgst amount", "utgst", "state/ut tax"],
    "cess": ["cess", "cess amount", "additional tax"],
    "total_amount": [
        "total", "total amount", "invoice amount", "bill amount",
        "grand total", "net amount", "amount"
    ],
    "description": [
        "item description", "description", "narration", "particulars",
        "item name", "product name", "service description"
    ],
    "status": ["status", "payment status"],  # Zoho includes payment status
}


def _detect_zoho_columns(header_row: list[str]) -> dict[str, int | None]:
    """Map canonical field names to column indices using Zoho aliases."""
    normalised = [h.strip().lower() for h in header_row]
    mapping: dict[str, int | None] = {k: None for k in ZOHO_COLUMN_ALIASES}
    for canonical, aliases in ZOHO_COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalised:
                mapping[canonical] = normalised.index(alias)
                break
    return mapping


class ZohoCSVParser(BaseParser):
    """
    Parses Zoho Books CSV invoice exports.
    More predictable column structure than Tally exports.
    """

    def parse(self) -> list[ParsedInvoice]:
        """Parse Zoho Books CSV and return normalised invoices."""
        try:
            text = self.content.decode("utf-8-sig")   # Handle BOM
        except UnicodeDecodeError:
            text = self.content.decode("latin-1")

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)

        if not rows:
            raise ValueError(f"{self.filename} is empty")

        # Find header row
        header_row_idx, col_map = self._find_header(rows)
        if header_row_idx is None:
            raise ValueError(
                f"Could not detect Zoho Books headers in {self.filename}. "
                "Export Bills or Invoices from Zoho Books and ensure 'GSTIN/UIN' column is present."
            )

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

            # Skip blank rows
            if not any(cell.strip() for cell in row):
                self._row_count -= 1
                continue

            # Skip cancelled/void invoices (Zoho marks these in status column)
            status_idx = col_map.get("status")
            if status_idx is not None and status_idx < len(row):
                status = row[status_idx].strip().lower()
                if status in ("void", "cancelled", "draft"):
                    self._row_count -= 1
                    logger.debug("Skipping %s invoice at row %d", status, i)
                    continue

            try:
                invoice = self._parse_row(row, col_map, i)
                invoices.append(invoice)
                self._success_count += 1
            except Exception as e:
                self._log_row_error(i, str(e))
                logger.warning("ZohoCSV row %d skipped: %s", i, e)

        logger.info(
            "ZohoCSV parse complete: %d/%d rows parsed from %s",
            self._success_count, self._row_count, self.filename
        )
        return invoices

    def _find_header(self, rows: list[list[str]]) -> tuple[int | None, dict]:
        """Scan first 5 rows for Zoho column headers."""
        for idx, row in enumerate(rows[:5]):
            col_map = _detect_zoho_columns(row)
            if col_map["date"] is not None and col_map["invoice_no"] is not None:
                return idx, col_map
        return None, {}

    def _parse_row(self, row: list[str], col_map: dict, row_num: int) -> ParsedInvoice:
        """Parse a single Zoho Books CSV row."""

        def get(field: str, default: str = "") -> str:
            idx = col_map.get(field)
            if idx is None or idx >= len(row):
                return default
            return row[idx].strip()

        # ── Required fields ────────────────────────────────
        raw_date = get("date")
        invoice_date = parse_date(raw_date, "date")

        invoice_no = get("invoice_no")
        if not invoice_no:
            raise ValueError("Missing invoice number")

        raw_gstin = get("gstin")
        if not raw_gstin:
            raise ValueError(f"Missing GSTIN/UIN for invoice {invoice_no}")
        gstin = normalise_gstin(raw_gstin)

        # ── Optional fields ────────────────────────────────
        supplier_name = get("supplier_name") or None
        description = get("description") or None
        if description:
            description = description[:500]

        # ── Amounts ────────────────────────────────────────
        taxable_amount = parse_decimal(get("taxable_amount"), "taxable_amount")
        igst = parse_decimal(get("igst"), "igst")
        cgst = parse_decimal(get("cgst"), "cgst")
        sgst = parse_decimal(get("sgst"), "sgst")
        cess = parse_decimal(get("cess"), "cess")

        raw_total = get("total_amount")
        total_amount = (
            parse_decimal(raw_total, "total_amount")
            if raw_total
            else taxable_amount + igst + cgst + sgst + cess
        )

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
