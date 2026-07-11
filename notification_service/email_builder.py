"""
notification_service/email_builder.py
Builds email content for two scenarios:

1. supplier_followup  — sent to a supplier asking them to correct/file their GST return
2. ca_escalation      — sent to the CA summarising high-risk mismatches that need review

Uses Jinja2 templates (inline, no separate template files needed for a microservice).
All amounts are formatted in Indian number format (₹1,23,456.78).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, BaseLoader

# ── Indian number formatter ────────────────────────────────

def fmt_inr(amount: float) -> str:
    """Format a float as Indian currency string: ₹1,23,456.78"""
    if amount == 0:
        return "₹0.00"
    # Indian numbering: last 3 digits, then groups of 2
    s = f"{amount:,.2f}"
    # Simple approach: use Python's locale-independent formatting
    integer_part, decimal_part = f"{abs(amount):.2f}".split(".")
    n = len(integer_part)
    if n <= 3:
        result = integer_part
    else:
        # First group: last 3 digits
        result = integer_part[-3:]
        remaining = integer_part[:-3]
        while remaining:
            result = remaining[-2:] + "," + result
            remaining = remaining[:-2]
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{result}.{decimal_part}"


# ── Jinja2 environment ────────────────────────────────────

_env = Environment(loader=BaseLoader(), autoescape=True)
_env.filters["inr"] = fmt_inr


# ── Email templates ────────────────────────────────────────

SUPPLIER_FOLLOWUP_SUBJECT = (
    "Action Required: GST Invoice Discrepancy — {{ invoice_no }} ({{ filing_period }})"
)

SUPPLIER_FOLLOWUP_BODY = """
Dear {{ supplier_name }},

We are writing regarding invoice **{{ invoice_no }}** dated **{{ invoice_date }}** 
for an amount of **{{ total_amount | inr }}**.

During our GST reconciliation for the period **{{ filing_period }}**, we identified 
the following discrepancy:

---
**Issue:** {{ cause_reasoning }}

**ITC Impact:** {{ itc_risk | inr }} of Input Tax Credit is at risk for our organisation.

**Required Action:** {{ recommended_action }}
---

We request you to review and rectify this in your GSTR-1 filing at the earliest. 
If you have already corrected this, please share the acknowledgement number.

For any queries, please contact us at {{ ca_email }}.

This is an automated notification from our GST Reconciliation System.

Regards,
{{ firm_name }}
{{ ca_name }}
"""

CA_ESCALATION_SUBJECT = (
    "⚠️ GST Escalation Alert — {{ total_escalations }} High-Risk Mismatches | "
    "{{ client_gstin }} | {{ filing_period }}"
)

CA_ESCALATION_BODY = """
Dear {{ ca_name }},

The automated GST reconciliation for **{{ client_name }}** (GSTIN: {{ client_gstin }}) 
for the period **{{ filing_period }}** has flagged **{{ total_escalations }} 
high-risk mismatches** requiring your immediate review.

---
**Summary**

| Metric | Value |
|--------|-------|
| Total Invoices Checked | {{ total_invoices }} |
| Clean Matches | {{ clean_matches }} |
| Auto-Resolved (Rounding) | {{ auto_count }} |
| Needs Supplier Follow-up | {{ followup_count }} |
| **Requires CA Review** | **{{ escalation_count }}** |
| **Total ITC at Risk** | **{{ total_itc | inr }}** |

---
**Escalated Mismatches**

{% for m in escalations %}
**{{ loop.index }}. Invoice {{ m.invoice_no or "N/A" }}**
- Supplier: {{ m.supplier_name or m.gstin or "Unknown" }}
- Type: {{ m.mismatch_type | upper }}
- ITC Risk: {{ m.itc_risk | inr }}
- Reason: {{ m.cause_reasoning }}
- Action: {{ m.recommended_action }}

{% endfor %}

---
Please log in to the reconciliation dashboard to review and resolve these items.

Report for this period will be shared separately.

Regards,
GST Reconciliation System
"""


# ── Email dataclass ────────────────────────────────────────

@dataclass
class Email:
    to: str
    subject: str
    body_text: str      # Plain text fallback
    body_html: str      # HTML version


# ── Builder functions ──────────────────────────────────────

def build_supplier_followup(
    supplier_email: str,
    supplier_name: str,
    invoice_no: str,
    invoice_date: str,
    total_amount: float,
    filing_period: str,
    cause_reasoning: str,
    recommended_action: str,
    itc_risk: float,
    ca_email: str,
    ca_name: str,
    firm_name: str,
) -> Email:
    """Build a supplier follow-up email for a single mismatch."""
    ctx = dict(
        supplier_name=supplier_name,
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        total_amount=total_amount,
        filing_period=filing_period,
        cause_reasoning=cause_reasoning,
        recommended_action=recommended_action,
        itc_risk=itc_risk,
        ca_email=ca_email,
        ca_name=ca_name,
        firm_name=firm_name,
    )
    subject = _env.from_string(SUPPLIER_FOLLOWUP_SUBJECT).render(**ctx)
    body = _env.from_string(SUPPLIER_FOLLOWUP_BODY).render(**ctx)

    # Simple markdown→HTML conversion (bold, headers)
    html = _md_to_html(body)

    return Email(to=supplier_email, subject=subject, body_text=body.strip(), body_html=html)


def build_ca_escalation(
    ca_email: str,
    ca_name: str,
    client_name: str,
    client_gstin: str,
    filing_period: str,
    total_invoices: int,
    clean_matches: int,
    auto_count: int,
    followup_count: int,
    escalations: list[dict],
    total_itc: float,
) -> Email:
    """Build a CA escalation alert summarising all high-risk mismatches."""
    ctx = dict(
        ca_name=ca_name,
        client_name=client_name,
        client_gstin=client_gstin,
        filing_period=filing_period,
        total_escalations=len(escalations),
        total_invoices=total_invoices,
        clean_matches=clean_matches,
        auto_count=auto_count,
        followup_count=followup_count,
        escalation_count=len(escalations),
        escalations=escalations,
        total_itc=total_itc,
    )
    subject = _env.from_string(CA_ESCALATION_SUBJECT).render(**ctx)
    body = _env.from_string(CA_ESCALATION_BODY).render(**ctx)
    html = _md_to_html(body)

    return Email(to=ca_email, subject=subject, body_text=body.strip(), body_html=html)


def _md_to_html(text: str) -> str:
    """
    Minimal markdown → HTML converter.
    Handles: **bold**, headers (---), line breaks.
    Good enough for transactional emails without a full markdown library.
    """
    import re
    lines = text.strip().split("\n")
    html_lines = []

    for line in lines:
        # Bold: **text** → <strong>text</strong>
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        # Table rows (pipe-separated)
        if line.startswith("|") and line.endswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            td = "".join(f"<td style='padding:4px 8px;border:1px solid #ddd'>{c}</td>" for c in cells)
            html_lines.append(f"<tr>{td}</tr>")
        elif set(line.strip()) <= set("|-"):  # separator row
            continue
        elif line.strip() == "---":
            html_lines.append("<hr>")
        elif line.strip() == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p style='margin:4px 0'>{line}</p>")

    body_content = "\n".join(html_lines)
    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;color:#333">
    <div style="background:#f5f5f5;padding:16px;border-radius:4px">
    {body_content}
    </div>
    </body></html>
    """
