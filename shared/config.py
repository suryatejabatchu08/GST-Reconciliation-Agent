"""
shared/config.py
Centralised settings loader using pydantic-settings.
All services import from this module.
"""

import os
from functools import lru_cache
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All configuration values loaded from environment variables / .env file.
    Values with defaults are optional; values without defaults are required.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me"

    # ── Database ───────────────────────────────────────────
    database_url: str
    database_url_sync: str

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""  # Settings → API → JWT Secret

    # ── RabbitMQ ───────────────────────────────────────────
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    # ── LLM Providers ─────────────────────────────────────
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # ── GCP ────────────────────────────────────────────────
    gcp_project_id: str = ""
    gcp_region: str = "asia-south1"
    gcs_bucket_name: str = "gst-reconciliation-reports"
    google_application_credentials: str = ""

    # ── GST Portal ─────────────────────────────────────────
    gst_portal_base_url: str = "https://sandbox.gst.gov.in"
    gst_portal_client_id: str = ""
    gst_portal_client_secret: str = ""

    # ── Email ──────────────────────────────────────────────
    sendgrid_api_key: str = ""
    email_from: str = "no-reply@example.com"
    email_from_name: str = "GST Reconciliation Agent"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""

    # ── Observability ──────────────────────────────────────
    grafana_otlp_endpoint: str = ""
    grafana_instance_id: str = ""
    grafana_api_key: str = ""

    # ── Service Ports ──────────────────────────────────────
    ingestion_port: int = 8001
    orchestration_port: int = 8002
    notification_port: int = 8003
    report_port: int = 8004
    gateway_port: int = 8080

    # ── Rate Limits ────────────────────────────────────────
    gemini_rpm_limit: int = 15          # 15 req/min (Gemini free tier)
    groq_rpm_limit: int = 30            # 30 req/min (Groq free tier)
    groq_rpd_limit: int = 14400         # 14,400 req/day (Groq free tier)
    gst_portal_rph_limit: int = 100     # 100 req/hour per GSTIN

    # ── Feature Flags ──────────────────────────────────────
    enable_gcs_upload: bool = False
    enable_sendgrid: bool = False
    enable_tracing: bool = False
    mock_gst_portal: bool = True        # Use mock GST portal responses in dev

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache()
def get_settings() -> Settings:
    """
    Return cached Settings instance.
    Use this in all services:
        from shared.config import get_settings
        settings = get_settings()
    """
    return Settings()
