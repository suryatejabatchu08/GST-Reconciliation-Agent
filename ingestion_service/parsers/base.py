"""
ingestion_service/parsers/base.py
Abstract base class that all parsers implement.
Enforces a consistent interface across Tally XML, Tally CSV, and Zoho CSV.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional


# ──────────────────────────────────────────────────────────
# ParsedInvoice — intermediate normalised invoice row
# (populated by parsers before being written to DB)
# ──────────────────────────────────────────────────────────

@dataclass
class ParsedInvoice:
    """
    Normalised invoice data extracted from any file format.
    All parsers return a list of these.
    """
    gstin: str                          # Supplier / counterparty GSTIN
    invoice_no: str                     # Invoice number
    invoice_date: date                  # Invoice date
    supplier_name: Optional[str] = None
    taxable_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    igst: Decimal = field(default_factory=lambda: Decimal("0"))
    cgst: Decimal = field(default_factory=lambda: Decimal("0"))
    sgst: Decimal = field(default_factory=lambda: Decimal("0"))
    cess: Decimal = field(default_factory=lambda: Decimal("0"))
    total_amount: Decimal = field(default_factory=lambda: Decimal("0"))
    description: Optional[str] = None
    raw_data: dict = field(default_factory=dict)   # Original row for audit trail

    def compute_total(self) -> None:
        """Recompute total_amount from components if not explicitly set."""
        if self.total_amount == Decimal("0"):
            self.total_amount = (
                self.taxable_amount + self.igst + self.cgst + self.sgst + self.cess
            )

    def is_rounding_difference(self, other_amount: Decimal, threshold: Decimal = Decimal("1")) -> bool:
        """
        PRD §5.2: amount variations ≤ ₹1 are treated as rounding, not mismatches.
        """
        return abs(self.total_amount - other_amount) <= threshold


# ──────────────────────────────────────────────────────────
# Normalisation helpers (shared across all parsers)
# ──────────────────────────────────────────────────────────

def normalise_gstin(raw: str) -> str:
    """
    Standardise GSTIN: uppercase, remove dashes and spaces, validate length.
    PRD §5.2: 'Standardise GSTIN format (remove dashes, uppercase)'

    Valid GSTIN format: 15 alphanumeric characters
    e.g. "29ABCDE1234F1Z5" or "29-ABCDE-1234-F1Z5" → "29ABCDE1234F1Z5"
    """
    if not raw:
        raise ValueError("GSTIN cannot be empty")
    cleaned = raw.upper().replace("-", "").replace(" ", "").strip()
    if len(cleaned) != 15:
        raise ValueError(
            f"Invalid GSTIN {raw!r}: expected 15 characters after cleaning, got {len(cleaned)}"
        )
    return cleaned


def parse_decimal(raw: str | float | int | None, field_name: str = "amount") -> Decimal:
    """
    Safely parse a string/number into Decimal. Returns Decimal("0") for empty/None.
    Handles Indian number formatting (e.g. "1,23,456.78").
    """
    if raw is None or raw == "":
        return Decimal("0")
    try:
        # Remove commas (Indian number format: 1,23,456.78)
        cleaned = str(raw).replace(",", "").strip()
        if not cleaned or cleaned == "-":
            return Decimal("0")
        return Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Cannot parse {field_name}={raw!r} as a decimal number")


def parse_date(raw: str | None, field_name: str = "date") -> date:
    """
    Parse date from common formats seen in Tally and Zoho exports:
    - DD-MM-YYYY  (Tally default)
    - DD/MM/YYYY
    - YYYY-MM-DD  (ISO)
    - YYYYMMDD    (Tally XML compact)
    - DD-Mon-YYYY (e.g. 15-Mar-2024)
    """
    if not raw:
        raise ValueError(f"{field_name} cannot be empty")

    raw = str(raw).strip()

    formats = [
        "%d-%m-%Y",     # 15-03-2024
        "%d/%m/%Y",     # 15/03/2024
        "%Y-%m-%d",     # 2024-03-15
        "%Y%m%d",       # 20240315 (Tally XML)
        "%d-%b-%Y",     # 15-Mar-2024
        "%d/%b/%Y",     # 15/Mar/2024
        "%B %d, %Y",    # March 15, 2024
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue

    raise ValueError(
        f"Cannot parse {field_name}={raw!r}. "
        f"Expected one of: DD-MM-YYYY, DD/MM/YYYY, YYYY-MM-DD, YYYYMMDD, DD-Mon-YYYY"
    )


def derive_filing_period(invoice_date: date) -> str:
    """
    Derive the GST filing period (YYYY-MM) from the invoice date.
    GST periods are monthly: April 2024 filing period = "2024-04"
    """
    return invoice_date.strftime("%Y-%m")


# ──────────────────────────────────────────────────────────
# Abstract Parser Base Class
# ──────────────────────────────────────────────────────────

class BaseParser(ABC):
    """
    Abstract base class for all file parsers.
    Every parser takes raw file bytes + filename, and returns a list of ParsedInvoice.
    """

    def __init__(self, content: bytes, filename: str):
        self.content = content
        self.filename = filename
        self._errors: list[str] = []      # Non-fatal parse warnings
        self._row_count: int = 0          # Total rows attempted
        self._success_count: int = 0      # Rows successfully parsed

    @abstractmethod
    def parse(self) -> list[ParsedInvoice]:
        """
        Parse the file content and return a list of normalised invoices.
        Should be fault-tolerant: log errors for bad rows but continue parsing.
        """
        ...

    @property
    def errors(self) -> list[str]:
        """Non-fatal parse errors — rows that were skipped with a reason."""
        return self._errors

    @property
    def parse_summary(self) -> dict:
        """Summary stats for logging and API response."""
        return {
            "filename": self.filename,
            "total_rows": self._row_count,
            "parsed_ok": self._success_count,
            "skipped": self._row_count - self._success_count,
            "errors": self._errors[:10],  # cap at 10 for API response
        }

    def _log_row_error(self, row_num: int, reason: str) -> None:
        """Record a skipped-row error without crashing the whole parse."""
        self._errors.append(f"Row {row_num}: {reason}")
