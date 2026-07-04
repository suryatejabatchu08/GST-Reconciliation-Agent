"""
ingestion_service/parsers/__init__.py
Parser package — exposes a single detect_and_parse() entry point.
"""

from ingestion_service.parsers.base import BaseParser, ParsedInvoice
from ingestion_service.parsers.tally_xml import TallyXMLParser
from ingestion_service.parsers.tally_csv import TallyCSVParser
from ingestion_service.parsers.zoho_csv import ZohoCSVParser

__all__ = [
    "BaseParser",
    "ParsedInvoice",
    "TallyXMLParser",
    "TallyCSVParser",
    "ZohoCSVParser",
    "get_parser",
]


def get_parser(source_type: str, content: bytes, filename: str) -> "BaseParser":
    """
    Factory: return the right parser based on source_type and file extension.

    Args:
        source_type: 'tally' | 'zoho' | 'gstr2a' | 'gstr1' | 'gstr3b'
        content: raw file bytes
        filename: original filename (used to detect .xml vs .csv)

    Returns:
        Instantiated parser ready to call .parse()
    """
    fname_lower = filename.lower()

    if source_type == "tally":
        if fname_lower.endswith(".xml"):
            return TallyXMLParser(content, filename)
        else:
            # Fallback: treat as CSV regardless of extension
            return TallyCSVParser(content, filename)

    if source_type == "zoho":
        return ZohoCSVParser(content, filename)

    raise ValueError(
        f"Unsupported source_type={source_type!r}. "
        "Valid options: 'tally', 'zoho'. "
        "GSTR-1/2A/3B are fetched via the GST portal client, not uploaded."
    )
