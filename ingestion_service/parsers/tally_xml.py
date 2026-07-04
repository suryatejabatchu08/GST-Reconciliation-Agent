"""
ingestion_service/parsers/tally_xml.py
Parser for Tally XML exports (TallyXML format).

Supports:
  - Tally 9 / Tally ERP 9  (schema: TALLYMESSAGE)
  - TallyPrime 2.x / 3.x   (schema: ENVELOPE > BODY > IMPORTDATA)

Tally exports are generated via:
  Gateway of Tally → Display → Daybook or Ledger → Export → XML

The XML structure contains VOUCHER elements with LEDGERENTRIES for each line item.
We extract purchase vouchers (type="Purchase") and sales vouchers (type="Sales").
"""

from __future__ import annotations

import logging
from decimal import Decimal

from lxml import etree

from ingestion_service.parsers.base import (
    BaseParser,
    ParsedInvoice,
    normalise_gstin,
    parse_decimal,
    parse_date,
    derive_filing_period,
)

logger = logging.getLogger(__name__)

# Tally XML voucher types we care about
PURCHASE_TYPES = {"Purchase", "Purchase Order", "Debit Note"}
SALES_TYPES = {"Sales", "Sales Order", "Credit Note"}


class TallyXMLParser(BaseParser):
    """
    Parses Tally XML export files.
    Handles both Tally 9 and TallyPrime schemas by auto-detecting the root element.
    """

    def parse(self) -> list[ParsedInvoice]:
        """Parse Tally XML and return normalised invoices."""
        try:
            root = etree.fromstring(self.content)
        except etree.XMLSyntaxError as e:
            raise ValueError(f"Invalid XML in {self.filename}: {e}")

        # Auto-detect schema version
        tag = root.tag.upper()
        if "ENVELOPE" in tag:
            vouchers = self._find_vouchers_envelope(root)
        else:
            # Tally 9: root is TALLYMESSAGE or similar
            vouchers = root.findall(".//VOUCHER")

        invoices: list[ParsedInvoice] = []

        for i, voucher in enumerate(vouchers, start=1):
            self._row_count += 1
            try:
                invoice = self._parse_voucher(voucher, i)
                if invoice:
                    invoices.append(invoice)
                    self._success_count += 1
            except Exception as e:
                self._log_row_error(i, str(e))
                logger.warning("Tally XML row %d skipped: %s", i, e)

        logger.info(
            "TallyXML parse complete: %d/%d vouchers parsed from %s",
            self._success_count, self._row_count, self.filename
        )
        return invoices

    def _find_vouchers_envelope(self, root: etree._Element) -> list[etree._Element]:
        """TallyPrime uses ENVELOPE > BODY > IMPORTDATA > REQUESTDATA > TALLYMESSAGE > VOUCHER"""
        vouchers = root.findall(".//VOUCHER")
        return vouchers

    def _parse_voucher(self, voucher: etree._Element, row_num: int) -> ParsedInvoice | None:
        """Extract fields from a single VOUCHER element."""
        vtype = voucher.get("VCHTYPE", voucher.findtext("VOUCHERTYPENAME") or "")

        # Only process purchase and sales vouchers
        if vtype not in PURCHASE_TYPES and vtype not in SALES_TYPES:
            return None

        # ── Invoice number ─────────────────────────────────
        invoice_no = (
            voucher.findtext("VOUCHERNUMBER")
            or voucher.findtext("REFERENCE")
            or voucher.get("VCHNO")
            or ""
        ).strip()

        if not invoice_no:
            raise ValueError("Missing invoice number (VOUCHERNUMBER)")

        # ── Date ───────────────────────────────────────────
        raw_date = (
            voucher.findtext("DATE")
            or voucher.findtext("VOUCHERDATE")
            or ""
        ).strip()
        invoice_date = parse_date(raw_date, "DATE")

        # ── GSTIN ──────────────────────────────────────────
        # GSTIN is stored in the party ledger's GSTIN field
        # Look in LEDGERENTRIES > GSTDETAILS or in GSTREGISTRATIONDETAILS
        gstin_raw = (
            voucher.findtext(".//GSTIN")
            or voucher.findtext(".//GSTREGISTRATIONNUMBER")
            or voucher.findtext(".//PARTYGSTIN")
            or ""
        ).strip()

        if not gstin_raw:
            raise ValueError(f"Missing GSTIN in voucher {invoice_no}")

        try:
            gstin = normalise_gstin(gstin_raw)
        except ValueError as e:
            raise ValueError(f"Invalid GSTIN in voucher {invoice_no}: {e}")

        # ── Supplier name ──────────────────────────────────
        supplier_name = (
            voucher.findtext("PARTYLEDGERNAME")
            or voucher.findtext(".//LEDGERNAME")
            or ""
        ).strip() or None

        # ── Tax amounts ────────────────────────────────────
        # Tax details live in ALLINVENTORYENTRIES or GSTDETAILS sub-elements
        igst = Decimal("0")
        cgst = Decimal("0")
        sgst = Decimal("0")
        cess = Decimal("0")
        taxable_amount = Decimal("0")

        for entry in voucher.findall(".//LEDGERENTRIES.LIST"):
            ledger_name = (entry.findtext("LEDGERNAME") or "").upper()
            amount_raw = entry.findtext("AMOUNT") or "0"
            amount = abs(parse_decimal(amount_raw, "AMOUNT"))

            if "IGST" in ledger_name:
                igst += amount
            elif "CGST" in ledger_name:
                cgst += amount
            elif "SGST" in ledger_name or "UTGST" in ledger_name:
                sgst += amount
            elif "CESS" in ledger_name:
                cess += amount

        # Taxable amount from inventory entries
        for inv_entry in voucher.findall(".//ALLINVENTORYENTRIES.LIST"):
            rate_str = inv_entry.findtext("RATE") or "0"
            qty_str = inv_entry.findtext("ACTUALQTY") or "1"
            # Simpler: use AMOUNT directly
            amt_raw = inv_entry.findtext("AMOUNT") or "0"
            taxable_amount += abs(parse_decimal(amt_raw, "inventory AMOUNT"))

        # Fallback: use voucher-level amount if line items missing
        if taxable_amount == Decimal("0"):
            voucher_amount = voucher.findtext("AMOUNT") or "0"
            taxable_amount = abs(parse_decimal(voucher_amount, "AMOUNT"))

        # ── Description (from narration or first inventory item) ──
        description = (
            voucher.findtext("NARRATION")
            or voucher.findtext(".//NAME")
            or None
        )
        if description:
            description = description.strip()[:500]  # cap length

        total = taxable_amount + igst + cgst + sgst + cess

        invoice = ParsedInvoice(
            gstin=gstin,
            invoice_no=invoice_no,
            invoice_date=invoice_date,
            supplier_name=supplier_name,
            taxable_amount=taxable_amount,
            igst=igst,
            cgst=cgst,
            sgst=sgst,
            cess=cess,
            total_amount=total,
            description=description,
            raw_data={
                "voucher_type": vtype,
                "raw_date": raw_date,
                "raw_gstin": gstin_raw,
            }
        )
        return invoice
