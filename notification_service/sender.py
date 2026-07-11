"""
notification_service/sender.py
Email sender supporting two backends:

1. SMTP (default, free) — works with Gmail App Passwords
2. SendGrid (optional) — set ENABLE_SENDGRID=true in .env

Gmail SMTP setup:
  1. Enable 2FA on your Google account
  2. Go to myaccount.google.com → Security → App passwords
  3. Create an app password for "Mail"
  4. Put it in .env: SMTP_PASSWORD=xxxx xxxx xxxx xxxx

SendGrid setup:
  1. Create free account at sendgrid.com (100 emails/day free)
  2. Create an API key at app.sendgrid.com/settings/api_keys
  3. Put it in .env: SENDGRID_API_KEY=SG.xxx ENABLE_SENDGRID=true
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from shared.config import get_settings
from notification_service.email_builder import Email

logger = logging.getLogger(__name__)
settings = get_settings()


async def send_email(email: Email) -> bool:
    """
    Send an email using the configured backend.
    Returns True on success, False on failure (non-raising).
    """
    if settings.enable_sendgrid and settings.sendgrid_api_key:
        return await _send_via_sendgrid(email)
    elif settings.smtp_username and settings.smtp_password:
        return await _send_via_smtp(email)
    else:
        # No sender configured — log and return True (dev mode)
        logger.info(
            "[EMAIL DRY-RUN] To: %s | Subject: %s",
            email.to, email.subject
        )
        logger.debug("[EMAIL BODY]\n%s", email.body_text)
        return True


async def _send_via_smtp(email: Email) -> bool:
    """Send via SMTP (Gmail or any SMTP server)."""
    try:
        # Build MIME message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = email.subject
        msg["From"] = f"{settings.email_from_name} <{settings.email_from}>"
        msg["To"] = email.to

        msg.attach(MIMEText(email.body_text, "plain", "utf-8"))
        msg.attach(MIMEText(email.body_html, "html", "utf-8"))

        # Send in executor to avoid blocking event loop
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _smtp_send(msg, email.to)
        )
        logger.info("Email sent via SMTP to %s: %s", email.to, email.subject)
        return True

    except Exception as e:
        logger.error("SMTP send failed to %s: %s", email.to, e)
        return False


def _smtp_send(msg: MIMEMultipart, to: str) -> None:
    """Synchronous SMTP send (runs in executor)."""
    context = ssl.create_default_context()
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(settings.smtp_username, settings.smtp_password)
        server.sendmail(settings.email_from, to, msg.as_string())


async def _send_via_sendgrid(email: Email) -> bool:
    """Send via SendGrid HTTP API."""
    try:
        import httpx
        payload = {
            "personalizations": [{"to": [{"email": email.to}]}],
            "from": {"email": settings.email_from, "name": settings.email_from_name},
            "subject": email.subject,
            "content": [
                {"type": "text/plain", "value": email.body_text},
                {"type": "text/html", "value": email.body_html},
            ],
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.sendgrid_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
        if resp.status_code in (200, 202):
            logger.info("Email sent via SendGrid to %s: %s", email.to, email.subject)
            return True
        else:
            logger.error("SendGrid error %d: %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("SendGrid send failed: %s", e)
        return False
