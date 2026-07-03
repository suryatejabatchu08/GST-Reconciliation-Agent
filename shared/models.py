"""
shared/models.py
Pydantic models and SQLAlchemy ORM models shared across all services.
"""

from __future__ import annotations

import uuid
from datetime import datetime, date
from decimal import Decimal
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field, validator
from sqlalchemy import (
    Column, String, Numeric, Date, DateTime, Boolean,
    ForeignKey, Text, Integer, func
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


# ──────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────

class InvoiceSource(str, Enum):
    TALLY = "tally"
    ZOHO = "zoho"
    GSTR1 = "gstr1"
    GSTR2A = "gstr2a"
    GSTR3B = "gstr3b"
    BANK = "bank"


class MismatchType(str, Enum):
    AMOUNT = "amount"           # Amount differs between sources
    MISSING = "missing"         # Invoice exists in one source only
    TAX_HEAD = "tax_head"       # IGST/CGST/SGST breakdown wrong
    DUPLICATE = "duplicate"     # Same invoice appears twice
    GSTIN = "gstin"             # GSTIN mismatch


class MismatchSeverity(str, Enum):
    AUTO = "auto"               # System can auto-fix (journal entry)
    FOLLOWUP = "followup"       # Supplier follow-up email needed
    ESCALATE = "escalate"       # CA escalation required (ITC risk ≥ ₹5000)


class ActionType(str, Enum):
    JOURNAL_ENTRY = "journal_entry"
    SUPPLIER_EMAIL = "supplier_email"
    ESCALATION = "escalation"


class JobStatus(str, Enum):
    PENDING = "pending"
    INGESTING = "ingesting"
    NORMALISING = "normalising"
    MATCHING = "matching"
    CLASSIFYING = "classifying"
    GENERATING_REPORT = "generating_report"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


# ──────────────────────────────────────────────────────────
# SQLAlchemy ORM Base
# ──────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class ClientORM(Base):
    """CA's client / tenant."""
    __tablename__ = "clients"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    gstin = Column(String(15), nullable=False, unique=True, index=True)
    firm_name = Column(String(255), nullable=False)
    ca_user_id = Column(String(255), nullable=False, index=True)
    email = Column(String(255))
    phone = Column(String(20))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    invoices = relationship("InvoiceORM", back_populates="client")
    jobs = relationship("JobORM", back_populates="client")


class InvoiceORM(Base):
    """Normalised invoice row from any source."""
    __tablename__ = "invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True, index=True)
    source = Column(String(20), nullable=False)             # InvoiceSource enum value
    gstin = Column(String(15), nullable=False, index=True)
    supplier_name = Column(String(255))
    invoice_no = Column(String(100), nullable=False)
    invoice_date = Column(Date, nullable=False)
    filing_period = Column(String(7), nullable=False)       # "YYYY-MM" e.g. "2024-03"
    taxable_amount = Column(Numeric(15, 2), default=0)
    igst = Column(Numeric(15, 2), default=0)
    cgst = Column(Numeric(15, 2), default=0)
    sgst = Column(Numeric(15, 2), default=0)
    cess = Column(Numeric(15, 2), default=0)
    total_amount = Column(Numeric(15, 2), default=0)
    description = Column(Text)
    raw_data = Column(JSONB)                                # Original parsed row
    # description_embedding is added via migration (pgvector type)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    client = relationship("ClientORM", back_populates="invoices")


class MismatchORM(Base):
    """Detected mismatch between two invoice sources."""
    __tablename__ = "mismatches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False, index=True)
    invoice_id_books = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True)
    invoice_id_portal = Column(UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=True)
    mismatch_type = Column(String(20), nullable=False)      # MismatchType enum value
    severity = Column(String(20), nullable=False)           # MismatchSeverity enum value
    cause_reasoning = Column(Text)                          # Groq/Llama explanation
    itc_risk_amount = Column(Numeric(15, 2), default=0)     # ITC at risk (₹)
    resolved = Column(Boolean, default=False)
    resolved_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    actions = relationship("ActionORM", back_populates="mismatch")


class ActionORM(Base):
    """Generated action for a mismatch (email draft, journal entry, escalation)."""
    __tablename__ = "actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mismatch_id = Column(UUID(as_uuid=True), ForeignKey("mismatches.id"), nullable=False, index=True)
    action_type = Column(String(30), nullable=False)        # ActionType enum value
    content = Column(JSONB, nullable=False)                 # Email body / journal XML / escalation note
    approved_by = Column(String(255))                       # CA user who approved
    approved_at = Column(DateTime(timezone=True))
    sent_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    mismatch = relationship("MismatchORM", back_populates="actions")


class JobORM(Base):
    """Reconciliation job for a client filing period."""
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False, index=True)
    ca_user_id = Column(String(255), nullable=False, index=True)
    filing_period = Column(String(7), nullable=False)       # "YYYY-MM"
    status = Column(String(30), nullable=False, default=JobStatus.PENDING.value)
    progress_pct = Column(Integer, default=0)               # 0–100
    current_node = Column(String(50))                       # LangGraph node name
    total_invoices = Column(Integer, default=0)
    total_mismatches = Column(Integer, default=0)
    report_url = Column(String(500))                        # GCS signed URL
    error_message = Column(Text)
    trace_id = Column(String(64), index=True)               # OpenTelemetry trace ID
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    client = relationship("ClientORM", back_populates="jobs")


# ──────────────────────────────────────────────────────────
# Pydantic Schemas (API request/response models)
# ──────────────────────────────────────────────────────────

class InvoiceSchema(BaseModel):
    """Normalised invoice for API responses and inter-service events."""
    id: Optional[uuid.UUID] = None
    client_id: uuid.UUID
    job_id: Optional[uuid.UUID] = None
    source: InvoiceSource
    gstin: str
    supplier_name: Optional[str] = None
    invoice_no: str
    invoice_date: date
    filing_period: str                  # "YYYY-MM"
    taxable_amount: Decimal = Decimal("0")
    igst: Decimal = Decimal("0")
    cgst: Decimal = Decimal("0")
    sgst: Decimal = Decimal("0")
    cess: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")
    description: Optional[str] = None

    @validator("gstin")
    def validate_gstin(cls, v: str) -> str:
        """Normalise GSTIN: uppercase, remove dashes/spaces."""
        cleaned = v.upper().replace("-", "").replace(" ", "")
        if len(cleaned) != 15:
            raise ValueError(f"Invalid GSTIN length: {v!r}")
        return cleaned

    @validator("filing_period")
    def validate_filing_period(cls, v: str) -> str:
        """Validate YYYY-MM format."""
        parts = v.split("-")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError(f"filing_period must be YYYY-MM format, got: {v!r}")
        return v

    class Config:
        from_attributes = True


class MismatchSchema(BaseModel):
    id: Optional[uuid.UUID] = None
    client_id: uuid.UUID
    job_id: uuid.UUID
    invoice_id_books: Optional[uuid.UUID] = None
    invoice_id_portal: Optional[uuid.UUID] = None
    mismatch_type: MismatchType
    severity: MismatchSeverity
    cause_reasoning: Optional[str] = None
    itc_risk_amount: Decimal = Decimal("0")
    resolved: bool = False

    class Config:
        from_attributes = True


class JobSchema(BaseModel):
    id: Optional[uuid.UUID] = None
    client_id: uuid.UUID
    ca_user_id: str
    filing_period: str
    status: JobStatus
    progress_pct: int = 0
    current_node: Optional[str] = None
    total_invoices: int = 0
    total_mismatches: int = 0
    report_url: Optional[str] = None
    error_message: Optional[str] = None
    trace_id: Optional[str] = None

    class Config:
        from_attributes = True


class JobProgressEvent(BaseModel):
    """Published to RabbitMQ as job.progress.{job_id}.{node}"""
    job_id: str
    node: str
    status: JobStatus
    progress_pct: int
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class InvoiceIngestedEvent(BaseModel):
    """Published to RabbitMQ as invoice.ingested"""
    job_id: str
    client_id: str
    filing_period: str
    invoice_count: int
    source: InvoiceSource
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class MismatchFoundEvent(BaseModel):
    """Published to RabbitMQ as mismatch.found"""
    job_id: str
    client_id: str
    filing_period: str
    mismatch_count: int
    mismatches: List[MismatchSchema]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
