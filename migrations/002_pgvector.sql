-- ============================================================
-- Migration 002: pgvector Extension + Embedding Column
-- GST Reconciliation Agent
-- ============================================================
-- Adds vector similarity search for fuzzy invoice description matching.
-- Use case: "Office Chairs - Ergonomic x10" ≈ "OFFICE CHAIR ERGO 10 UNITS"
-- ============================================================

-- Enable pgvector extension (supported on Supabase out of the box)
CREATE EXTENSION IF NOT EXISTS vector;

-- Add embedding column to invoices table
-- 768 dimensions = Gemini text-embedding-004 output size
ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS description_embedding vector(768);

-- IVFFlat index for approximate nearest-neighbour search
-- lists=100 is appropriate for up to ~1M rows; adjust as data grows
-- Requires at least 100 * lists rows before it's useful (use exact scan below that)
CREATE INDEX IF NOT EXISTS idx_invoices_embedding
    ON invoices
    USING ivfflat (description_embedding vector_cosine_ops)
    WITH (lists = 100);

-- ── Helper function: find similar invoice descriptions ─────
-- Used by the GSTR-2A Matcher node to disambiguate composite-key collisions
CREATE OR REPLACE FUNCTION find_similar_invoices(
    p_client_id     UUID,
    p_filing_period VARCHAR(7),
    p_embedding     vector(768),
    p_limit         INTEGER DEFAULT 5,
    p_threshold     FLOAT DEFAULT 0.8       -- cosine similarity threshold
)
RETURNS TABLE (
    id              UUID,
    invoice_no      VARCHAR,
    supplier_name   VARCHAR,
    source          VARCHAR,
    similarity      FLOAT
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        i.id,
        i.invoice_no,
        i.supplier_name,
        i.source,
        1 - (i.description_embedding <=> p_embedding) AS similarity
    FROM invoices i
    WHERE
        i.client_id = p_client_id
        AND i.filing_period = p_filing_period
        AND i.description_embedding IS NOT NULL
        AND 1 - (i.description_embedding <=> p_embedding) >= p_threshold
    ORDER BY i.description_embedding <=> p_embedding
    LIMIT p_limit;
$$;

COMMENT ON COLUMN invoices.description_embedding IS
    'Gemini text-embedding-004 vector (768-dim) of invoice description. Used for fuzzy description matching in GSTR-2A Matcher node.';

COMMENT ON FUNCTION find_similar_invoices IS
    'Returns invoices with cosine similarity >= threshold to the given embedding. Called when composite-key (GSTIN+invoice_no+date+amount) matching is ambiguous.';
