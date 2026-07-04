"""
ingestion_service/gst_portal/client.py
GST Portal API client.

Supports two modes (controlled by MOCK_GST_PORTAL in .env):
  - MOCK mode (default, MOCK_GST_PORTAL=true):
      Returns realistic fake GSTR-2A / GSTR-1 / GSTR-3B data.
      Use this during development without real GST portal credentials.

  - LIVE mode (MOCK_GST_PORTAL=false):
      Calls the actual GST sandbox or production API.
      Requires GST_PORTAL_CLIENT_ID and GST_PORTAL_CLIENT_SECRET in .env.

GST Portal API docs: https://developer.gst.gov.in/apiportal/taxpayer/otpAuth
Rate limit: 100 requests/hour per GSTIN (handled by the rate-limiter in Phase 3)
Auth: OTP-based session token (TTL: 6 hours)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
import uuid

import httpx

from shared.config import get_settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Mock Data Generator
# ──────────────────────────────────────────────────────────

def _generate_mock_gstr2a(gstin: str, filing_period: str, count: int = 10) -> list[dict]:
    """
    Generate realistic mock GSTR-2A data for a given GSTIN and filing period.
    Used when MOCK_GST_PORTAL=true.

    The data mimics what the GST portal returns: each entry represents a
    supplier's invoice as they filed it in their GSTR-1.
    """
    year, month = filing_period.split("-")
    inv_date = date(int(year), int(month), 15)

    mock_suppliers = [
        ("29AABCT1332L1ZT", "Tech Supplies Pvt Ltd"),
        ("27AAKCS9175D1Z5", "Maharashtra Office Solutions"),
        ("07AAACH7409R1ZZ", "Delhi Stationary House"),
        ("19AABCU9603R1ZM", "West Bengal Traders"),
        ("33AADCB2230M1Z3", "Tamil Nadu Distributors"),
    ]

    invoices = []
    for i in range(count):
        supplier_gstin, supplier_name = mock_suppliers[i % len(mock_suppliers)]
        taxable = Decimal(str(round(5000 + (i * 1234.56), 2)))
        igst_applicable = (i % 3 == 0)   # Every 3rd invoice is interstate (IGST)

        if igst_applicable:
            igst = taxable * Decimal("0.18")
            cgst = Decimal("0")
            sgst = Decimal("0")
        else:
            igst = Decimal("0")
            cgst = taxable * Decimal("0.09")
            sgst = taxable * Decimal("0.09")

        invoices.append({
            "inum": f"INV-{filing_period}-{i+1:04d}",   # Invoice number as filed by supplier
            "idt": inv_date.strftime("%d-%m-%Y"),
            "val": float(taxable + igst + cgst + sgst),
            "txval": float(taxable),
            "iamt": float(igst),
            "camt": float(cgst),
            "samt": float(sgst),
            "csamt": 0.0,
            "ctin": supplier_gstin,
            "cname": supplier_name,
            "pos": gstin[:2],             # Place of supply (state code from GSTIN)
            "rchrg": "N",                 # Reverse charge: No
            "inv_typ": "R",               # Regular invoice
        })

    return invoices


def _generate_mock_gstr1(gstin: str, filing_period: str, count: int = 8) -> dict:
    """Generate mock GSTR-1 summary data (outward supplies)."""
    year, month = filing_period.split("-")

    return {
        "gstin": gstin,
        "fp": filing_period.replace("-", ""),
        "b2b": [
            {
                "ctin": f"27AAKCS{i}175D1Z5",
                "inv": [
                    {
                        "inum": f"SALES-{filing_period}-{i+1:03d}",
                        "idt": f"1{i}-{month}-{year}",
                        "val": round(10000 + i * 2500, 2),
                        "pos": "27",
                        "rchrg": "N",
                        "inv_typ": "R",
                        "itms": [
                            {
                                "num": 1,
                                "itm_det": {
                                    "txval": round(8500 + i * 2000, 2),
                                    "rt": 18,
                                    "iamt": 0,
                                    "camt": round((8500 + i * 2000) * 0.09, 2),
                                    "samt": round((8500 + i * 2000) * 0.09, 2),
                                    "csamt": 0,
                                }
                            }
                        ]
                    }
                ]
            }
            for i in range(count)
        ]
    }


def _generate_mock_gstr3b(gstin: str, filing_period: str) -> dict:
    """Generate mock GSTR-3B summary data (tax liability summary)."""
    return {
        "gstin": gstin,
        "ret_period": filing_period.replace("-", ""),
        "sup_details": {
            "osup_det": {
                "txval": 850000.00,
                "iamt": 0,
                "camt": 76500.00,
                "samt": 76500.00,
                "csamt": 0,
            },
            "osup_zero": {"txval": 0, "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "osup_nil_exmp": {"txval": 5000, "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "isup_rev": {"txval": 0, "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            "osup_nongst": {"txval": 0},
        },
        "itc_elg": {
            "itc_avl": [
                {"ty": "IMPG", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
                {"ty": "IMPS", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
                {"ty": "ISRC", "iamt": 0, "camt": 25000.00, "samt": 25000.00, "csamt": 0},
                {"ty": "ISD", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
                {"ty": "OTH", "iamt": 12000.00, "camt": 18000.00, "samt": 18000.00, "csamt": 0},
            ]
        },
    }


# ──────────────────────────────────────────────────────────
# GST Portal Client
# ──────────────────────────────────────────────────────────

class GSTPortalClient:
    """
    Client for fetching GSTR-1, GSTR-2A, GSTR-3B data from the GST portal.

    In MOCK mode: returns pre-generated realistic data (no network calls).
    In LIVE mode: calls the GST sandbox/production API.

    Usage:
        client = GSTPortalClient()
        gstr2a_invoices = await client.fetch_gstr2a(gstin="29ABCDE1234F1Z5", period="2024-03")
    """

    def __init__(self):
        self.settings = get_settings()
        self.mock_mode = self.settings.mock_gst_portal
        self.base_url = self.settings.gst_portal_base_url
        self._session_token: Optional[str] = None

        if self.mock_mode:
            logger.info("GSTPortalClient: running in MOCK mode (MOCK_GST_PORTAL=true)")
        else:
            logger.info("GSTPortalClient: running in LIVE mode against %s", self.base_url)

    async def fetch_gstr2a(self, gstin: str, period: str, count: int = 10) -> list[dict]:
        """
        Fetch GSTR-2A (inward supplies as filed by suppliers).

        Args:
            gstin: The taxpayer's GSTIN
            period: Filing period in "YYYY-MM" format
            count: Number of records to return (mock mode only)

        Returns:
            List of invoice dicts from the GST portal
        """
        if self.mock_mode:
            logger.debug("MOCK: fetch_gstr2a for GSTIN=%s period=%s", gstin, period)
            return _generate_mock_gstr2a(gstin, period, count)

        # LIVE: call GST portal API
        token = await self._get_session_token(gstin)
        period_compact = period.replace("-", "")   # "2024-03" → "202403"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/returns/gstr2a",
                headers={
                    "Authorization": f"Bearer {token}",
                    "gstin": gstin,
                    "ret_period": period_compact,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("b2b", [])

    async def fetch_gstr1(self, gstin: str, period: str) -> dict:
        """
        Fetch GSTR-1 (outward supplies filed by the taxpayer).

        Args:
            gstin: The taxpayer's GSTIN
            period: Filing period in "YYYY-MM" format

        Returns:
            GSTR-1 dict with b2b, b2c, etc. sections
        """
        if self.mock_mode:
            logger.debug("MOCK: fetch_gstr1 for GSTIN=%s period=%s", gstin, period)
            return _generate_mock_gstr1(gstin, period)

        token = await self._get_session_token(gstin)
        period_compact = period.replace("-", "")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/returns/gstr1",
                headers={
                    "Authorization": f"Bearer {token}",
                    "gstin": gstin,
                    "ret_period": period_compact,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def fetch_gstr3b(self, gstin: str, period: str) -> dict:
        """
        Fetch GSTR-3B (monthly summary return with tax liability).

        Args:
            gstin: The taxpayer's GSTIN
            period: Filing period in "YYYY-MM" format

        Returns:
            GSTR-3B summary dict
        """
        if self.mock_mode:
            logger.debug("MOCK: fetch_gstr3b for GSTIN=%s period=%s", gstin, period)
            return _generate_mock_gstr3b(gstin, period)

        token = await self._get_session_token(gstin)
        period_compact = period.replace("-", "")

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{self.base_url}/returns/gstr3b",
                headers={
                    "Authorization": f"Bearer {token}",
                    "gstin": gstin,
                    "ret_period": period_compact,
                },
            )
            resp.raise_for_status()
            return resp.json()

    async def _get_session_token(self, gstin: str) -> str:
        """
        Get or refresh the OTP-based session token for the given GSTIN.
        In production, this token is cached in Redis (TTL: 6 hours).
        For now, raises NotImplementedError in LIVE mode without Redis.
        """
        if self._session_token:
            return self._session_token

        # TODO Phase 3: integrate Redis cache for token storage
        # For now, require the token to be passed in env or raise
        token = os.getenv("GST_PORTAL_SESSION_TOKEN", "")
        if not token:
            raise RuntimeError(
                "GST portal session token not found. "
                "In LIVE mode, set GST_PORTAL_SESSION_TOKEN in .env "
                "or implement the OTP flow in Phase 3."
            )
        self._session_token = token
        return token
