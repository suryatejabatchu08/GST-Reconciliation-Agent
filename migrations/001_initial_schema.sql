-- ============================================================
-- Migration 001: Initial Schema
-- GST Reconciliation Agent
-- Run via: python scripts/init_db.py
-- ============================================================

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── clients ───────────────────────────────────────────────
-- Represents a CA's client / tenant (MSME filing GST)
CREATE TABLE IF NOT EXISTS clients (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    gstin           VARCHAR(15) NOT NULL,
    firm_name       VARCHAR(255) NOT NULL,
    ca_user_id      VARCHAR(255) NOT NULL,
    email           VARCHAR(255),
    phone           VARCHAR(20),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ,

    CONSTRAINT clients_gstin_unique UNIQUE (gstin),
    CONSTRAINT clients_gstin_length CHECK (length(gstin) = 15)
);

CREATE INDEX IF NOT EXISTS idx_clients_ca_user_id ON clients (ca_user_id);
CREATE INDEX IF NOT EXISTS idx_clients_gstin ON clients (gstin);

-- ── jobs ──────────────────────────────────────────────────
-- Reconciliation job per client per filing period
CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id       UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    ca_user_id      VARCHAR(255) NOT NULL,
    filing_period   VARCHAR(7) NOT NULL,        -- "YYYY-MM"
    status          VARCHAR(30) NOT NULL DEFAULT 'pending',
    progress_pct    INTEGER NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    current_node    VARCHAR(50),
    total_invoices  INTEGER DEFAULT 0,
    total_mismatches INTEGER DEFAULT 0,
    report_url      VARCHAR(500),
    error_message   TEXT,
    trace_id        VARCHAR(64),                -- OpenTelemetry trace ID
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_client_id ON jobs (client_id);
CREATE INDEX IF NOT EXISTS idx_jobs_ca_user_id ON jobs (ca_user_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_trace_id ON jobs (trace_id);
CREATE INDEX IF NOT EXISTS idx_jobs_filing_period ON jobs (client_id, filing_period);

-- ── invoices ──────────────────────────────────────────────
-- Normalised invoice rows from all sources (Tally, Zoho, GSTR-1/2A/3B)
CREATE TABLE IF NOT EXISTS invoices (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    job_id              UUID REFERENCES jobs(id) ON DELETE SET NULL,
    source              VARCHAR(20) NOT NULL,       -- 'tally'|'zoho'|'gstr1'|'gstr2a'|'gstr3b'|'bank'
    gstin               VARCHAR(15) NOT NULL,
    supplier_name       VARCHAR(255),
    invoice_no          VARCHAR(100) NOT NULL,
    invoice_date        DATE NOT NULL,
    filing_period       VARCHAR(7) NOT NULL,         -- "YYYY-MM"
    taxable_amount      NUMERIC(15, 2) DEFAULT 0,
    igst                NUMERIC(15, 2) DEFAULT 0,
    cgst                NUMERIC(15, 2) DEFAULT 0,
    sgst                NUMERIC(15, 2) DEFAULT 0,
    cess                NUMERIC(15, 2) DEFAULT 0,
    total_amount        NUMERIC(15, 2) DEFAULT 0,
    description         TEXT,
    raw_data            JSONB,                       -- Original parsed row (for audit)
    -- description_embedding added in migration 002 (requires pgvector extension)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT invoices_source_check CHECK (
        source IN ('tally', 'zoho', 'gstr1', 'gstr2a', 'gstr3b', 'bank')
    )
);

-- Composite index for deduplication: GSTIN + invoice_no + date + amount
CREATE INDEX IF NOT EXISTS idx_invoices_dedup
    ON invoices (client_id, filing_period, gstin, invoice_no, invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_client_period
    ON invoices (client_id, filing_period);
CREATE INDEX IF NOT EXISTS idx_invoices_job
    ON invoices (job_id);
CREATE INDEX IF NOT EXISTS idx_invoices_source
    ON invoices (client_id, source);

-- ── mismatches ────────────────────────────────────────────
-- Detected discrepancies between book entries and portal data
CREATE TABLE IF NOT EXISTS mismatches (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    client_id           UUID NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    job_id              UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    invoice_id_books    UUID REFERENCES invoices(id) ON DELETE SET NULL,
    invoice_id_portal   UUID REFERENCES invoices(id) ON DELETE SET NULL,
    mismatch_type       VARCHAR(20) NOT NULL,       -- 'amount'|'missing'|'tax_head'|'duplicate'|'gstin'
    severity            VARCHAR(20) NOT NULL,        -- 'auto'|'followup'|'escalate'
    cause_reasoning     TEXT,                        -- Groq/Llama plain-English explanation
    itc_risk_amount     NUMERIC(15, 2) DEFAULT 0,    -- ITC at risk (₹)
    resolved            BOOLEAN NOT NULL DEFAULT FALSE,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT mismatches_type_check CHECK (
        mismatch_type IN ('amount', 'missing', 'tax_head', 'duplicate', 'gstin')
    ),
    CONSTRAINT mismatches_severity_check CHECK (
        severity IN ('auto', 'followup', 'escalate')
    )
);

CREATE INDEX IF NOT EXISTS idx_mismatches_client ON mismatches (client_id);
CREATE INDEX IF NOT EXISTS idx_mismatches_job ON mismatches (job_id);
CREATE INDEX IF NOT EXISTS idx_mismatches_severity ON mismatches (severity);
CREATE INDEX IF NOT EXISTS idx_mismatches_resolved ON mismatches (resolved);

-- ── actions ───────────────────────────────────────────────
-- Actions generated for each mismatch (email draft, journal entry, escalation)
CREATE TABLE IF NOT EXISTS actions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mismatch_id     UUID NOT NULL REFERENCES mismatches(id) ON DELETE CASCADE,
    action_type     VARCHAR(30) NOT NULL,            -- 'journal_entry'|'supplier_email'|'escalation'
    content         JSONB NOT NULL,                  -- Email body / journal XML / escalation note
    approved_by     VARCHAR(255),                    -- CA user who approved
    approved_at     TIMESTAMPTZ,
    sent_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT actions_type_check CHECK (
        action_type IN ('journal_entry', 'supplier_email', 'escalation')
    )
);

CREATE INDEX IF NOT EXISTS idx_actions_mismatch ON actions (mismatch_id);
CREATE INDEX IF NOT EXISTS idx_actions_type ON actions (action_type);
CREATE INDEX IF NOT EXISTS idx_actions_approved ON actions (approved_by);

-- ============================================================
-- Row-Level Security (enabled in Phase 7; defined here for completeness)
-- ============================================================
-- ALTER TABLE clients ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE invoices ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE mismatches ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE actions ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE clients IS 'CA client firms — one row per GST-registered entity';
COMMENT ON TABLE jobs IS 'Reconciliation jobs — one per client per filing period';
COMMENT ON TABLE invoices IS 'Normalised invoices from Tally, Zoho, GSTR-1/2A/3B';
COMMENT ON TABLE mismatches IS 'Detected discrepancies classified by type and severity';
COMMENT ON TABLE actions IS 'Generated actions: email drafts, journal entries, escalations';
