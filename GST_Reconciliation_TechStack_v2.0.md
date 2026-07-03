# Tech Stack Document
## GST Reconciliation Agent

**Version:** 2.0
**Date:** July 2026

---

## 0. What Changed from v1

v1 was a well-designed monolith: one FastAPI app, one LangGraph process, AWS Lambda, polling for status. v2 splits this into a small system of independently deployable services communicating over a message broker, adds real-time updates, moves to GCP, and adds observability and CI/CD from the start. Every keyword below is load-bearing — it solves a specific problem in this system, not decoration.

| Layer | v1 | v2 |
|-------|----|----|
| Cloud | AWS | **GCP** |
| Compute | AWS Lambda | **Cloud Run (Docker containers), one per service** |
| Job queue / events | Redis (`rq`) | **RabbitMQ (CloudAMQP)** — real pub/sub, not just a queue |
| Status updates | HTTP polling | **WebSockets** (gateway service) |
| Architecture | Single FastAPI monolith | **4 microservices**: Ingestion, Orchestration, Notification, Report |
| Storage | AWS S3 | **Google Cloud Storage** |
| Retrieval | None | **pgvector** for fuzzy invoice-description matching |
| Observability | None | **OpenTelemetry + Grafana Cloud** |
| CI/CD | None | **GitHub Actions** → build, test, push Docker image, deploy to Cloud Run |
| Local dev | `docker-compose` (DB + Redis only) | **`docker-compose` for the full system** (all 4 services + RabbitMQ + Postgres) |

---

## 1. Stack at a Glance

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Agent orchestration | LangGraph | Multi-agent graph execution (inside Orchestration Service) |
| AI — parsing & normalisation | Google Gemini 1.5 Flash (free tier) | Document parsing, data extraction |
| AI — reasoning & classification | Groq API — Llama 3.3 70B (free tier) | Mismatch classification, email drafting |
| Services | FastAPI (Python), 4 independent services | Ingestion, Orchestration, Notification, Report |
| Message broker / events | RabbitMQ (CloudAMQP free tier) | Async job events, inter-service pub/sub |
| Real-time updates | WebSockets (dedicated Gateway service) | Live job progress to CA dashboard |
| Database | PostgreSQL + pgvector extension | Invoice store, audit trail, tenant data, embedding search |
| File storage | Google Cloud Storage | Uploaded CSVs, PDFs, generated reports |
| Compute / deployment | Docker + GCP Cloud Run | Containerized, independently scalable services |
| CI/CD | GitHub Actions | Build, test, containerize, deploy on every merge |
| Observability | OpenTelemetry + Grafana Cloud (free tier) | Distributed tracing, logs, metrics across services |
| Frontend | Next.js | CA dashboard, report viewer, live progress UI |
| Email | SMTP / SendGrid | Supplier follow-up emails |
| Integrations | GST portal API, Tally XML, Zoho Books API | Data ingestion |

---

## 2. Services

The system is split along the natural failure/scaling boundaries: parsing untrusted uploads, running expensive multi-agent reasoning, sending external emails, and generating reports are different workloads with different resource needs and different failure modes — they shouldn't share a process.

### 2.1 Ingestion Service
- Owns: `/upload`, GST portal fetch, Tally XML / CSV / Zoho parsing
- Publishes `invoice.ingested` events to RabbitMQ once files are parsed and stored
- Scales independently — file parsing is CPU-bound and bursty, shouldn't compete with LLM-bound orchestration work

### 2.2 Orchestration Service
- Runs the LangGraph agent graph (unchanged core logic from v1: normalise → parallel match/validate/check → classify → resolve)
- Consumes `invoice.ingested` events, publishes `job.progress.*` events at each node transition and `mismatch.found` on completion
- All LLM calls (Gemini, Groq) go through the rate-limiter/circuit-breaker described in §6

### 2.3 Notification Service
- Consumes `mismatch.found` events for follow-up-required mismatches
- Drafts supplier emails, holds them for CA approval (human-in-the-loop, unchanged from v1), sends via SendGrid on approval
- Isolated deliberately: if SendGrid is down or rate-limited, it does not block report generation

### 2.4 Report Service
- Consumes `mismatch.found` events, generates PDF/Excel, writes audit log
- Uploads final reports to GCS, publishes `report.ready`

### 2.5 WebSocket Gateway
- Subscribes to `job.progress.*` events on RabbitMQ
- Maintains one WebSocket connection per active job per CA session
- Pushes progress events to the frontend in real time; frontend falls back to REST polling if the socket drops

```python
# gateway/main.py — simplified
from fastapi import FastAPI, WebSocket
import aio_pika

app = FastAPI()

@app.websocket("/ws/jobs/{job_id}")
async def job_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    queue = await channel.declare_queue(f"progress.{job_id}", auto_delete=True)
    await queue.bind("job_events", routing_key=f"job.progress.{job_id}.*")

    async with queue.iterator() as messages:
        async for message in messages:
            async with message.process():
                await websocket.send_json(json.loads(message.body))
```

---

## 3. Agent Layer (Orchestration Service internals)

### LangGraph
- Models the reconciliation pipeline as a directed state graph — unchanged from v1
- Three parallel nodes: `gstr2a_matcher`, `gstr1_validator`, `tax_liability_checker`
- Single synthesis node: `mismatch_classifier`
- Human-in-the-loop checkpoint before any supplier email is sent
- State persisted to PostgreSQL between steps (resumable if the service restarts)
- Each node transition publishes a `job.progress.*` event so the WebSocket Gateway can push live updates

```python
from langgraph.graph import StateGraph, END

graph = StateGraph(ReconciliationState)
graph.add_node("normalise", normalise_agent)
graph.add_node("gstr2a_match", gstr2a_matcher)
graph.add_node("gstr1_validate", gstr1_validator)
graph.add_node("tax_check", tax_liability_checker)
graph.add_node("classify", mismatch_classifier)
graph.add_node("resolve", resolution_router)

graph.set_entry_point("normalise")
graph.add_edge("normalise", "gstr2a_match")
graph.add_edge("normalise", "gstr1_validate")
graph.add_edge("normalise", "tax_check")
graph.add_edge(["gstr2a_match", "gstr1_validate", "tax_check"], "classify")
graph.add_edge("classify", "resolve")
graph.add_edge("resolve", END)
```

### Google Gemini 1.5 Flash (free tier)
- Used in: `normalise_agent` node — document parsing, Tally XML extraction, GSTIN standardisation
- Free limit: 15 requests/min, 1M tokens/day
- SDK: `google-generativeai` Python package
- Structured output: `response_mime_type="application/json"`
- **v2 change:** calls go through the rate-limiter queue (§6), not called directly

```python
import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])
model = genai.GenerativeModel("gemini-1.5-flash")
```

### Groq API — Llama 3.3 70B (free tier)
- Used in: `mismatch_classifier` and `resolve` nodes — cause reasoning, severity classification, supplier email drafting
- Free limit: 14,400 requests/day, 500K tokens/min
- SDK: OpenAI-compatible client (`groq` Python package)
- Structured JSON output enforced via `response_format={"type": "json_object"}`
- **v2 change:** calls go through the rate-limiter queue (§6), not called directly

```python
from groq import Groq

client = Groq(api_key=os.environ["GROQ_API_KEY"])
response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    response_format={"type": "json_object"},
    messages=[{"role": "user", "content": prompt}]
)
```

> Both APIs remain free for this project's scale. The rate-limiting and circuit-breaker layer in §6 is what makes that sustainable under real usage, rather than something to work around with a manual delay.

---

## 4. Message Broker — RabbitMQ (CloudAMQP)

Replaces Redis-as-queue from v1. RabbitMQ is used here as a real event bus, not just a task queue — multiple services (Notification, Report, WebSocket Gateway) independently subscribe to the same `mismatch.found` and `job.progress.*` events without the Orchestration Service knowing or caring who's listening. That decoupling is the actual point of a message broker, and it's what Redis-as-a-list-based-queue doesn't give you.

- Provider: **CloudAMQP free tier** ("Little Lemur" plan — 1M messages/month, sufficient for 300 jobs/month with room to spare)
- Exchange: `job_events` (topic exchange)
- Routing keys: `invoice.ingested`, `job.progress.{job_id}.{node}`, `mismatch.found`, `report.ready`
- Client: `aio_pika` (async, plays well with FastAPI)
- Redis is kept, but scoped down to what it's actually good at: caching GSTR-2A responses per GSTIN per quarter (TTL: 7 days) and storing short-lived GST portal OTP session tokens

```python
# orchestration_service/publisher.py
import aio_pika, json

async def publish_progress(job_id: str, node: str, status: str):
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    exchange = await channel.declare_exchange("job_events", aio_pika.ExchangeType.TOPIC)
    await exchange.publish(
        aio_pika.Message(body=json.dumps({"job_id": job_id, "node": node, "status": status}).encode()),
        routing_key=f"job.progress.{job_id}.{node}",
    )
```

---

## 5. Database — PostgreSQL + pgvector

### Key tables (unchanged from v1)

```sql
-- Tenant / CA firm
clients (id, gstin, firm_name, ca_user_id, created_at)

-- Normalised invoice rows
invoices (
  id, client_id, source,         -- 'tally' | 'gstr1' | 'gstr2a' | 'gstr3b'
  gstin, invoice_no, invoice_date,
  taxable_amount, igst, cgst, sgst, cess,
  filing_period, created_at,
  description_embedding vector(768)   -- NEW in v2, see below
)

-- Detected mismatches
mismatches (
  id, client_id, job_id,
  invoice_id_books, invoice_id_portal,
  mismatch_type,                 -- 'amount' | 'missing' | 'tax_head'
  severity,                      -- 'auto' | 'followup' | 'escalate'
  cause_reasoning,               -- Llama 3.3's plain-English explanation
  resolved, created_at
)

-- Generated actions
actions (
  id, mismatch_id, action_type,  -- 'journal_entry' | 'supplier_email' | 'escalation'
  content,                       -- JSON payload or email body
  approved_by, sent_at
)
```

### pgvector — why it's here and what it's actually for
This is deliberately the system's *only* embedding/retrieval use case, and it exists to solve a real problem: invoice line-item descriptions from Tally vs GSTR-2A rarely match exactly as strings (`"Office Chairs - Ergonomic x10"` vs `"OFFICE CHAIR ERGO 10 UNITS"`). Exact-match and composite-key dedup (GSTIN + invoice_no + date + amount) catches most cases, but description similarity is the tie-breaker when the composite key is ambiguous (e.g. two invoices to the same supplier on the same date).

- Extension: `CREATE EXTENSION vector;` on the same Postgres instance — no separate vector DB service needed at this scale
- Embeddings generated once at normalisation time via Gemini's embedding endpoint, stored alongside the invoice row
- Queried with `ORDER BY description_embedding <=> $1 LIMIT 5` inside the GSTR-2A Matcher node when composite-key matching returns more than one candidate

- Row-level security: every query filtered by `client_id` — no cross-tenant data leakage
- Indexes: `(client_id, filing_period, gstin)` for lookups, `ivfflat` index on `description_embedding` for vector search
- Hosting: **Neon or Supabase free tier** (both support pgvector out of the box), or Cloud SQL if you want to stay fully inside GCP

---

## 6. LLM Provider Resilience Layer

This is the piece that makes "free-tier LLM APIs" and "production-grade system" compatible, and it lives in the Orchestration Service.

- **Rate-limit-aware consumer:** LLM requests are never made directly from a request handler. They're enqueued, and a dedicated consumer pulls from the queue at a rate that respects each provider's free-tier ceiling (Gemini: 15 req/min; Groq: 14,400 req/day, 500K tokens/min).
- **Backoff on 429/5xx:** on a rate-limit or server error, the message is re-queued with exponential backoff (via RabbitMQ's dead-letter-exchange + TTL pattern) instead of failing the job.
- **Circuit breaker per provider:** if a provider's error rate crosses a threshold in a rolling window, new requests to it pause for a cooldown period; in-flight classification jobs wait rather than erroring, and progress events report `"status": "retrying"` so the CA sees an honest state instead of a stalled progress bar.
- **Provider-agnostic interface:** both Gemini and Groq are called through the same thin wrapper, so adding a paid tier or a third provider later is a config change, not a rewrite.

```python
# orchestration_service/llm_gateway.py — simplified shape
class LLMGateway:
    def __init__(self, provider_configs: dict, breaker: CircuitBreaker):
        self.providers = provider_configs
        self.breaker = breaker

    async def call(self, provider: str, prompt: str, **kwargs):
        if not self.breaker.allow(provider):
            raise ProviderCooldown(provider)
        try:
            result = await self.providers[provider].generate(prompt, **kwargs)
            self.breaker.record_success(provider)
            return result
        except RateLimitError:
            self.breaker.record_failure(provider)
            raise  # re-queued by the caller with backoff
```

---

## 7. Compute & Deployment — Docker + GCP Cloud Run

- Each service (Ingestion, Orchestration, Notification, Report, WebSocket Gateway) ships as its own Docker image
- Deployed to **Cloud Run** — scales to zero when idle (keeps this free), scales out per-service under load (Orchestration can scale independently of, say, Notification)
- Runtime: Python 3.12 base image, multi-stage Docker build to keep images small
- Cloud Run free tier: 2M requests/month + 360,000 GB-seconds — comfortably covers ~300 jobs/month across all five services

```dockerfile
# orchestration_service/Dockerfile
FROM python:3.12-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

```yaml
# docker-compose.yml — local dev, full system
version: "3.9"
services:
  postgres:
    image: ankane/pgvector
    environment:
      POSTGRES_PASSWORD: dev
    ports: ["5432:5432"]

  rabbitmq:
    image: rabbitmq:3-management
    ports: ["5672:5672", "15672:15672"]

  ingestion:
    build: ./ingestion_service
    env_file: .env
    depends_on: [postgres, rabbitmq]

  orchestration:
    build: ./orchestration_service
    env_file: .env
    depends_on: [postgres, rabbitmq]

  notification:
    build: ./notification_service
    env_file: .env
    depends_on: [rabbitmq]

  report:
    build: ./report_service
    env_file: .env
    depends_on: [postgres, rabbitmq]

  gateway:
    build: ./gateway
    ports: ["8080:8080"]
    depends_on: [rabbitmq]
```

---

## 8. CI/CD — GitHub Actions

Every push to `main` runs tests, builds a Docker image per changed service, pushes to Artifact Registry, and deploys to Cloud Run. Each service has its own workflow so a change to Notification doesn't redeploy Orchestration.

```yaml
# .github/workflows/deploy-orchestration.yml
name: Deploy Orchestration Service
on:
  push:
    branches: [main]
    paths: ["orchestration_service/**"]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: |
          cd orchestration_service
          pip install -r requirements.txt -r requirements-test.txt
          pytest
      - id: auth
        uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}
      - uses: google-github-actions/setup-gcloud@v2
      - name: Build and push image
        run: |
          gcloud auth configure-docker
          docker build -t gcr.io/$PROJECT_ID/orchestration:${{ github.sha }} orchestration_service/
          docker push gcr.io/$PROJECT_ID/orchestration:${{ github.sha }}
      - name: Deploy to Cloud Run
        run: |
          gcloud run deploy orchestration \
            --image gcr.io/$PROJECT_ID/orchestration:${{ github.sha }} \
            --region asia-south1 --platform managed
```

- Free: GitHub Actions is free for public repos (2,000 min/month even on private repos)
- GCP Artifact Registry free tier: 0.5 GB storage — enough for a handful of service images with old-version cleanup

---

## 9. Observability — OpenTelemetry + Grafana Cloud

The single hardest thing about a multi-agent, multi-service pipeline is debugging *why* a specific job was slow or wrong. Observability is what makes that answerable instead of guessable.

- Each service is instrumented with OpenTelemetry, propagating a single `trace_id` per job across every service boundary (HTTP headers for REST calls, message headers for RabbitMQ events)
- Traces exported to **Grafana Cloud free tier** (includes Tempo for traces, Loki for logs, Prometheus for metrics — 10k series, 50GB logs/traces free)
- What you get: a single trace view showing "job X spent 40s in Gemini parsing, 2s in matching, 90s waiting on the Groq rate limiter" — directly diagnostic for the exact free-tier-LLM constraint this system is designed around

```python
# shared/tracing.py
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=GRAFANA_OTLP_ENDPOINT)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("orchestration-service")

with tracer.start_as_current_span("mismatch_classifier", attributes={"job_id": job_id}):
    result = await llm_gateway.call("groq", prompt)
```

---

## 10. Frontend — Next.js

- CA dashboard: client list, job history, mismatch review table
- **Live progress bar** driven by the WebSocket Gateway connection, falling back to REST polling (`/jobs/{job_id}/status`) if the socket drops
- Report viewer: inline PDF preview + Excel download
- Email approval flow: CA reviews drafted email → approve → sent
- Deployed on Vercel (free tier, hobby plan)

---

## 11. Integrations

### GST Portal API
- Sandbox: `https://sandbox.gst.gov.in/`
- Auth: OTP-based session token (stored in Redis, TTL: 6 hours)
- Endpoints used: `GET /returns/gstr2a`, `GET /returns/gstr1`, `GET /returns/gstr3b`
- Rate limit: 100 requests/hour per GSTIN — handled by the same rate-limiter pattern as the LLM gateway (§6)

### Tally XML Parser
- Tally exports `TallyXML` format — parsed with `lxml`
- Versioned parser: auto-detects Tally 9 vs TallyPrime schema
- Fallback: generic CSV parser for manual exports

### Zoho Books API
- OAuth 2.0 authentication
- Endpoints: `/invoices`, `/bills`, `/chartofaccounts`
- Webhook support for real-time invoice sync (v2 feature)

### Email — SendGrid
- Supplier follow-up emails sent via SendGrid API (Notification Service only)
- Template: Groq/Llama-drafted body, CA's firm name/logo in header
- Delivery tracking: webhook updates `actions.sent_at` on delivery

---

## 12. Local Development Setup

```bash
# Clone and setup
git clone https://github.com/your-org/gst-reconciliation-agent
cd gst-reconciliation-agent

# Environment variables (shared across services)
cp .env.example .env
# Fill in: GEMINI_API_KEY, GROQ_API_KEY, DATABASE_URL, RABBITMQ_URL, GCP credentials

# Start the full system: Postgres+pgvector, RabbitMQ, all 5 services
docker-compose up --build

# Services now running:
#   Ingestion      → localhost:8001
#   Orchestration  → localhost:8002
#   Notification   → localhost:8003
#   Report         → localhost:8004
#   WS Gateway     → localhost:8080
#   RabbitMQ mgmt  → localhost:15672
```

---

## 13. Free Tier Cost Breakdown

| Service | Free Tier Limit | Expected Usage (300 jobs/month) |
|---------|----------------|----------------------------------|
| GCP Cloud Run (×5 services) | 2M requests + 360K GB-sec/month | ~15K requests, ~40K GB-sec |
| Google Cloud Storage | 5 GB free | ~200 MB |
| Postgres (Neon/Supabase, pgvector included) | 0.5–3 GB free tier | ~2 GB |
| CloudAMQP RabbitMQ (Little Lemur) | 1M messages/month | ~30K messages |
| Grafana Cloud | 10K metric series, 50GB logs/traces | Covered |
| GitHub Actions | 2,000 min/month (private) | ~200 min |
| Vercel (Next.js) | Unlimited hobby deploys | Covered |
| Gemini 1.5 Flash (Google) | 1M tokens/day free | Covered under free tier, rate-limited via queue |
| Groq API (Llama 3.3 70B) | 14,400 req/day free | Covered under free tier, rate-limited via queue |
| **Total infra cost** | | **~₹0/month up to 300 jobs** |

---

## 14. Security Checklist

- [ ] All API keys (GEMINI_API_KEY, GROQ_API_KEY, GCP, CloudAMQP) in GCP Secret Manager (never in `.env` in production)
- [ ] PostgreSQL row-level security enforced on all tables
- [ ] GCS bucket: public access blocked, signed URLs only (15 min expiry)
- [ ] GST portal OTP tokens stored in Redis with TTL, never in DB
- [ ] Rate limiting on every public-facing endpoint: 10 req/min per CA user (via `slowapi`)
- [ ] All file uploads virus-scanned before processing (ClamAV as a sidecar/init container)
- [ ] Service-to-service traffic authenticated (Cloud Run IAM invoker roles, not open endpoints)
- [ ] GDPR/DPDP compliant: client data deletion endpoint available, cascades across all services
