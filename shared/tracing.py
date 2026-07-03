"""
shared/tracing.py
OpenTelemetry bootstrap for distributed tracing.

In Phase 1 this is a stub — it sets up the tracer but doesn't export anywhere.
In Phase 6 (Observability), ENABLE_TRACING=true wires it to Grafana Cloud.

Usage in any service:
    from shared.tracing import get_tracer

    tracer = get_tracer("ingestion-service")

    with tracer.start_as_current_span("parse_tally_xml", attributes={"job_id": job_id}):
        # ... your code ...
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)


def _build_provider(service_name: str, enable_export: bool) -> TracerProvider:
    """Build a TracerProvider with the given service name."""
    resource = Resource.create({
        "service.name": service_name,
        "service.version": "1.0.0",
        "deployment.environment": os.getenv("APP_ENV", "development"),
    })
    provider = TracerProvider(resource=resource)

    if enable_export:
        # Phase 6: wire to Grafana Cloud OTLP endpoint
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            endpoint = os.getenv("GRAFANA_OTLP_ENDPOINT", "")
            instance_id = os.getenv("GRAFANA_INSTANCE_ID", "")
            api_key = os.getenv("GRAFANA_API_KEY", "")

            if endpoint and instance_id and api_key:
                import base64
                token = base64.b64encode(f"{instance_id}:{api_key}".encode()).decode()
                exporter = OTLPSpanExporter(
                    endpoint=endpoint,
                    headers={"Authorization": f"Basic {token}"},
                )
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info("OpenTelemetry: exporting traces to Grafana Cloud")
            else:
                logger.warning("OpenTelemetry: ENABLE_TRACING=true but Grafana credentials missing. Falling back to console.")
                provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        except ImportError:
            logger.warning("opentelemetry-exporter-otlp not installed. Using console exporter.")
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    else:
        # Dev: print spans to console (useful for debugging without Grafana)
        if os.getenv("APP_ENV", "development") == "development":
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    return provider


# Module-level initialisation
_initialized = False


def setup_tracing(service_name: str) -> None:
    """
    Call once at service startup:
        setup_tracing("ingestion-service")
    """
    global _initialized
    if _initialized:
        return

    enable_export = os.getenv("ENABLE_TRACING", "false").lower() == "true"
    provider = _build_provider(service_name, enable_export)
    trace.set_tracer_provider(provider)
    _initialized = True
    logger.info("OpenTelemetry tracing initialised for service: %s", service_name)


def get_tracer(service_name: str) -> trace.Tracer:
    """Get a tracer for the given service name."""
    return trace.get_tracer(service_name)
