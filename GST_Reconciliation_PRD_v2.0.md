# Product Requirements Document
## GST Reconciliation Agent

**Version:** 2.0
**Date:** July 2026
**Status:** Draft

---

## 1. Overview

### Problem Statement
Indian CAs and finance teams manually reconcile GST returns every quarter — comparing GSTR-2A from the portal against the purchase register in Tally or Zoho. A business with 200–300 invoices/month takes 6–10 hours per filing period. Errors cost ITC claims and attract 18% per annum interest on wrong GSTR-3B filings.

### Solution
An agentic AI **system** — not a single app — that ingests invoices from Tally/Zoho, fetches GST portal data, runs parallel reconciliation agents across independently deployable services, classifies mismatches by cause and severity, and produces a ready-to-act report — reducing an 8-hour job to under 5 minutes, with live progress visible to the CA the whole time.

### Target Users
- Chartered Accountants managing 20–200 client filings/month
- Finance teams at MSMEs filing their own GST returns
- Tax compliance SaaS platforms embedding reconciliation as a feature (via API)

### v2.0 Change Summary
This revision moves the product from a single-service app to a small distributed system: independent services communicating over a message broker, real-time job updates over WebSockets, containerized deployment to GCP, and observability built in from day one. This is a deliberate architecture decision, not scope creep — see Section 10.

---

## 2. Goals

| Goal | Metric |
|------|--------|
| Reduce reconciliation time | < 5 minutes per filing period |
| Mismatch detection accuracy | ≥ 98% recall on amount mismatches |
| ITC risk coverage | Flag all GSTR-2A vs purchase register gaps |
| CA adoption | Free trial → paid conversion in < 1 session |
| Live visibility | Job progress reflected in UI within 500ms of state change (WebSocket push, not poll) |
| System resilience | No single service outage takes down ingestion, orchestration, or reporting simultaneously |

---

## 3. Non-Goals

- Filing returns directly to the GST portal (read + analysis only)
- Replacing a CA for complex tax advisory decisions
- Handling customs / import GST (IGST on imports) in v1
- Running our own hosted LLM — we orchestrate hosted providers (Gemini, Groq) rather than serving models ourselves

---

## 4. User Stories

**As a CA,** I want to upload my client's Tally export and get a mismatch report in minutes, so I don't spend hours on manual Excel comparison.

**As a CA,** I want to watch reconciliation progress update live on screen, so I know the system is working and roughly how long is left.

**As a CA,** I want the agent to draft supplier follow-up emails automatically, so I can approve and send them without writing from scratch.

**As a finance manager,** I want a dashboard showing all pending ITC mismatches by severity, so I can prioritise what to fix before the filing deadline.

**As a CA firm owner,** I want to run reconciliation for multiple clients in parallel without one client's large job slowing another's down, so my team can handle more filings without hiring.

**As a CA firm owner,** I want the system to keep working even if the email-sending component is temporarily down, so a single component failure doesn't block report generation.

---

## 5. Functional Requirements

### 5.1 Data Ingestion
- Accept Tally XML / CSV ledger exports via file upload
- Accept Zoho Books CSV exports
- Fetch GSTR-1, GSTR-2A, GSTR-3B via GST portal API (sandbox + production)
- Accept bank statement PDF or CSV
- Owned by the **Ingestion Service** (see Tech Stack §2)

### 5.2 Normalisation Agent
- Standardise GSTIN format (remove dashes, uppercase)
- Deduplicate invoices across sources using composite key: `GSTIN + invoice_no + date + amount`
- Handle amount variations ≤ ₹1 as rounding — do not flag as mismatch
- Map tax heads: IGST, CGST, SGST, CESS
- Fuzzy-match invoice line-item *descriptions* across sources (free-text supplier descriptions rarely match exactly) using vector similarity search — this is the system's one genuine retrieval/embedding use case (see Tech Stack §5)

### 5.3 Reconciliation Agents (parallel)
- **GSTR-2A Matcher:** compare every purchase register invoice against supplier-filed GSTR-2A
- **GSTR-1 Validator:** verify all sales invoices in books appear correctly in filed GSTR-1
- **Tax Liability Checker:** validate IGST/CGST/SGST breakdown matches GSTR-3B summary
- All three run as parallel LangGraph nodes inside the **Orchestration Service**

### 5.4 Mismatch Classification Agent
- For each mismatch, the system must determine:
  - Cause: supplier not filed / timing difference / data entry error / genuine ITC risk
  - Severity: auto-fixable / follow-up required / CA escalation
  - Recommended action: specific, one-line instruction
- All classifications must cite the source row IDs they were derived from (no unsourced claims)

### 5.5 Resolution Actions
- Generate Tally-compatible journal entry XML for auto-fixable mismatches
- Draft supplier follow-up email (professional, references invoice no. and GSTIN)
- Create escalation task on CA dashboard for ITC risk items
- Emails are queued to the **Notification Service** and only sent after explicit CA approval (human-in-the-loop checkpoint, unchanged from v1)

### 5.6 Outputs
- PDF reconciliation report (client-ready)
- Excel sheet with mismatches colour-coded by severity
- Audit trail log stored in database
- Email drafts ready for CA approval
- Owned by the **Report Service**

### 5.7 Multi-client Support
- CA can run reconciliation for multiple clients simultaneously
- Each client's data is strictly isolated (row-level security by tenant ID)
- Job status visible on dashboard **in real time** via WebSocket, not polling

### 5.8 Live Job Progress (new in v2.0)
- Frontend opens a WebSocket connection per active job
- Each service publishes progress events (`ingest.started`, `normalise.done`, `match.progress`, `classify.done`, `report.ready`, etc.) to the message broker
- A lightweight gateway subscribes to these events and pushes them to the relevant open WebSocket connections
- Falls back to a REST status endpoint if the WebSocket connection drops, so nothing is lost on flaky networks

### 5.9 Provider Resilience (new in v2.0)
- LLM calls (Gemini for parsing, Groq/Llama for classification) go through a queue-backed rate limiter, not called directly inline
- If a provider returns a rate-limit or 5xx error, the job re-queues with backoff rather than failing outright
- This is what makes running on free-tier LLM APIs viable at real usage volumes — see Section 10 and Risks

---

## 6. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Reconciliation job time | < 5 minutes for 500 invoices |
| API uptime | 99.5% |
| Job progress latency | < 500ms from event to UI update (WebSocket) |
| Data isolation | Per-tenant row-level security in PostgreSQL |
| Security | All files encrypted at rest (GCS), in transit (TLS) |
| Cost (free tier) | ≤ ₹0 until 300 jobs/month across all infra |
| Service independence | Ingestion, Orchestration, Notification, and Report services deploy and scale independently |
| Observability | Every job traceable end-to-end across services by a single trace ID |
| Deployability | Every service ships as a Docker image, deployed via CI/CD on every merge to main |

---

## 7. System Architecture — Service Flow

```
                        ┌─────────────────────────┐
   Tally / Zoho /       │   Ingestion Service      │
   GST Portal / Bank ──▶│   (upload, fetch, parse) │
                        └────────────┬─────────────┘
                                     │ publishes "invoice.ingested"
                                     ▼
                        ┌─────────────────────────┐
                        │       RabbitMQ           │
                        │  (event bus / job queue) │
                        └────────────┬─────────────┘
                                     │
                                     ▼
                        ┌─────────────────────────┐
                        │  Orchestration Service    │
                        │  (LangGraph agent graph)  │
                        │                            │
                        │  Normalise → [GSTR-2A      │
                        │  Matcher | GSTR-1          │
                        │  Validator | Tax Liability │
                        │  Checker] → Classifier      │
                        └──────┬─────────────┬───────┘
                               │             │
                 publishes     │             │  publishes
              "mismatch.found" │             │  "job.progress.*"
                               ▼             ▼
                 ┌─────────────────────┐   ┌──────────────────┐
                 │ Notification Service │   │  WebSocket        │
                 │ (email drafts, CA    │   │  Gateway           │
                 │  approval, SendGrid) │   │  (pushes live      │
                 └───────────┬──────────┘   │  progress to CA    │
                             │              │  dashboard)        │
                             │              └──────────────────┘
                             ▼
                 ┌─────────────────────┐
                 │   Report Service     │
                 │ (PDF/Excel/audit log)│
                 └─────────────────────┘
```

Every arrow that crosses a service boundary is either a RabbitMQ event or a REST call — never a direct in-process function call. This is what makes each service independently deployable and independently scalable (Section 10).

---

## 8. Milestones

| Phase | Scope | Timeline |
|-------|-------|----------|
| v0.1 — MVP | GSTR-2A vs purchase register matching, PDF report, single monolith | Week 1–3 |
| v0.2 — Full agents | All 3 parallel agents + mismatch classifier | Week 4–6 |
| v0.3 — Outputs | Email drafts, Tally entry export, dashboard | Week 7–9 |
| v0.4 — Service split | Split monolith into Ingestion / Orchestration / Notification / Report services, introduce RabbitMQ | Week 10–11 |
| v0.5 — Real-time | WebSocket gateway replaces polling; live job progress | Week 12 |
| v0.6 — Observability & CI/CD | OpenTelemetry tracing, Grafana dashboards, GitHub Actions pipeline, Docker images for every service | Week 13–14 |
| v1.0 — Multi-client, GCP deploy | Tenant isolation, bulk job queue, CA portal, full deployment to Cloud Run | Week 15–16 |

---

## 9. Risks

| Risk | Mitigation |
|------|------------|
| GST portal API rate limits | Cache GSTR-2A data; refresh once per session |
| Tally export format changes | Versioned parser with fallback to manual CSV |
| Claude hallucinating mismatch cause | All classifications must cite source row IDs |
| ITC claim wrongly marked auto-fixable | Minimum ₹5,000 ITC mismatches always escalate to CA |
| **Gemini/Groq free-tier rate limits under real load** | Queue-backed rate limiter (RabbitMQ) in front of every LLM call; automatic backoff and retry; jobs degrade gracefully to "queued, retrying" rather than failing. This is treated as a first-class design constraint, not a workaround — see Section 10. |
| Message broker becomes a single point of failure | CloudAMQP free tier includes automatic recovery; services are designed to reconnect and resume consuming on broker restart |
| WebSocket connection drops mid-job | REST status endpoint remains available as fallback; frontend polls it if the socket disconnects |

---

## 10. On Using Free-Tier LLM APIs in a "Production-Grade" System

This project intentionally continues to use **Gemini 1.5 Flash** (free tier) and **Groq / Llama 3.3 70B** (free tier) as the LLM providers — see the Tech Stack doc for reasoning. Free-tier APIs impose hard rate limits, which is a real constraint, not a hidden weakness. The system is designed around that constraint rather than in spite of it:

- LLM calls never happen synchronously in the request path — they're always queued through RabbitMQ, which naturally smooths bursts and enforces backpressure.
- A rate-limit-aware consumer paces requests to stay under each provider's free-tier ceiling (Gemini: 15 req/min; Groq: 14,400 req/day) and retries with exponential backoff on 429s.
- Provider calls are wrapped with a simple circuit breaker: if Groq is degraded, classification jobs pause and resume rather than failing the whole pipeline.
- This same design would let a future paid tier, or a second provider, be swapped in behind the same queue with no change to the orchestration logic — the rate limiter and circuit breaker are provider-agnostic.

In other words: the free-tier constraint is what forced the asynchronous, queue-backed architecture in the first place — which is also exactly the pattern a real production system would want regardless of provider cost.
