"""
scripts/validate_env.py
Pre-flight environment validator.
Checks that all required .env keys are present before services start.

Run this before starting any service:
    python scripts/validate_env.py

Exit code 0 = all required keys present
Exit code 1 = missing required keys (printed to stderr)
"""

import os
import sys
from pathlib import Path

# Load .env manually (without pydantic) so this script has no dependencies
def load_dotenv(path: str = ".env") -> dict:
    env = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    except FileNotFoundError:
        print(f"ERROR: .env file not found at '{path}'", file=sys.stderr)
        sys.exit(1)
    return env


REQUIRED = {
    "DATABASE_URL":              "Supabase PostgreSQL connection URL",
    "DATABASE_URL_SYNC":         "Supabase PostgreSQL sync URL",
    "SUPABASE_URL":              "Supabase project URL",
    "SUPABASE_ANON_KEY":         "Supabase anon public key",
    "SUPABASE_SERVICE_ROLE_KEY": "Supabase service role key",
    "RABBITMQ_URL":              "CloudAMQP RabbitMQ URL (amqps://...)",
}

OPTIONAL_WITH_WARNINGS = {
    "GEMINI_API_KEY":      "Gemini AI (supplier normalisation) — AI features will use stub fallback",
    "GROQ_API_KEY":        "Groq AI (mismatch classification) — AI features will use stub fallback",
    "SUPABASE_JWT_SECRET": "Gateway JWT validation — running in dev bypass mode (no auth)",
    "SMTP_USERNAME":       "Gmail SMTP — emails will be logged only (dry-run mode)",
    "SMTP_PASSWORD":       "Gmail App Password — emails will be logged only (dry-run mode)",
}


def main():
    print("=" * 60)
    print("  GST Reconciliation Agent — Environment Validator")
    print("=" * 60)

    env = load_dotenv()
    errors = []
    warnings = []

    # Check required keys
    print("\n[REQUIRED] Required keys:")
    for key, description in REQUIRED.items():
        value = env.get(key, "")
        if not value or value.startswith("YOUR_") or value == "change-me-to-a-random-64-char-string":
            errors.append(f"  [MISSING] {key}: {description}")
            print(f"  [MISSING] {key:<35} MISSING")
        else:
            masked = value[:8] + "..." if len(value) > 8 else value
            print(f"  [OK]      {key:<35} {masked}")

    # Check optional keys with warnings
    print("\n[OPTIONAL] Optional keys (warnings if missing):")
    for key, warning in OPTIONAL_WITH_WARNINGS.items():
        value = env.get(key, "")
        if not value or value.startswith("YOUR_"):
            note = warning.split(" -- ")[1] if " -- " in warning else "degraded"
            warnings.append(f"  [WARN] {key}: {warning}")
            print(f"  [WARN]    {key:<35} not set -- {note}")
        else:
            masked = value[:8] + "..." if len(value) > 8 else value
            print(f"  [OK]      {key:<35} {masked}")

    print("\n" + "=" * 60)

    if errors:
        print(f"\n[FAIL] {len(errors)} required key(s) missing:\n")
        for e in errors:
            print(e)
        print("\nFix the above in your .env file before starting services.")
        sys.exit(1)

    if warnings:
        print(f"\n[WARN] {len(warnings)} optional key(s) not set -- some features degraded.")
        print("   Services will start but with reduced functionality.")

    print("\n[PASS] All required keys present. Ready to start.\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
