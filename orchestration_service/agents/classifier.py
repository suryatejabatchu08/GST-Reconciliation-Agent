"""
orchestration_service/agents/classifier.py
Node 3 — Mismatch Classifier (Groq / Llama 3.3 70B)

Takes all raw_mismatches collected from the parallel matching nodes,
and uses Groq (Llama 3.3 70B) to:
  1. Re-classify each mismatch cause with deeper reasoning
  2. Confirm/override severity (auto / followup / escalate)
  3. Generate a recommended action specific to the mismatch

PRD §6.2: "All classifications must cite source row IDs"
PRD §9 risk: "ITC claim wrongly marked auto-fixable:
               Minimum ₹5,000 ITC mismatches always escalate to CA"

Why Groq for classification and Gemini for normalisation?
  - Normalisation (Gemini): needs small, structured output (JSON mapping)
  - Classification (Groq/Llama 3.3 70B): needs deeper GST domain reasoning
    and nuanced language for CA-facing descriptions.
    Groq has higher free RPD limits suitable for per-mismatch classification.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.db import get_db_session
from shared.models import MismatchORM
from orchestration_service.state import (
    ReconciliationState, MismatchRecord
)
from orchestration_service.llm_gateway import get_gateway

logger = logging.getLogger(__name__)

# Safety guard: always escalate if ITC risk >= this threshold
ITC_ESCALATION_THRESHOLD = 5000.0


CLASSIFICATION_PROMPT = """You are an expert Indian GST reconciliation assistant helping a Chartered Accountant.

Analyse the following GST invoice mismatch and provide:
1. A clear, specific cause explanation (1-2 sentences)
2. Severity classification: "auto" (rounding ≤₹1), "followup" (send supplier email), or "escalate" (CA must review)
3. A specific recommended action for the CA

Mismatch details:
{mismatch_json}

ITC at risk: ₹{itc_risk:.2f}

CRITICAL RULES:
- If ITC risk ≥ ₹5,000, severity MUST be "escalate" regardless of mismatch type
- "auto" severity only for rounding differences ≤ ₹1
- Always cite the specific invoice numbers and GSTINs in your reasoning
- Recommended action must be specific and actionable

Return ONLY valid JSON in this exact format:
{{
  "cause_reasoning": "...",
  "severity": "auto|followup|escalate",
  "recommended_action": "..."
}}"""


async def _classify_single(
    mismatch: MismatchRecord,
    gateway,
    context: dict,
) -> MismatchRecord:
    """
    Use Groq to classify a single mismatch with deeper reasoning.
    Falls back to the rule-based classification if LLM fails.
    """
    # Safety: enforce ITC escalation rule without LLM
    if mismatch["itc_risk_amount"] >= ITC_ESCALATION_THRESHOLD:
        updated = dict(mismatch)
        updated["severity"] = "escalate"
        mismatch = MismatchRecord(**updated)

    # Build context-rich mismatch description for the LLM
    mismatch_context = {
        "mismatch_type": mismatch["mismatch_type"],
        "current_severity": mismatch["severity"],
        "rule_based_cause": mismatch["cause_reasoning"],
        "itc_risk_inr": mismatch["itc_risk_amount"],
        "gstin_context": context.get("gstin", ""),
        "filing_period": context.get("filing_period", ""),
    }

    prompt = CLASSIFICATION_PROMPT.format(
        mismatch_json=json.dumps(mismatch_context, indent=2),
        itc_risk=mismatch["itc_risk_amount"],
    )

    try:
        result = await gateway.generate(
            prompt=prompt,
            provider="groq",
            model_hint="classify",
            temperature=0.1,
        )
        text = result["text"].strip()

        # Extract JSON from response
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        classification = json.loads(text)

        # Validate required keys
        assert "cause_reasoning" in classification
        assert "severity" in classification
        assert classification["severity"] in ("auto", "followup", "escalate")

        # Safety override: never downgrade escalation on high ITC risk
        final_severity = classification["severity"]
        if mismatch["itc_risk_amount"] >= ITC_ESCALATION_THRESHOLD:
            final_severity = "escalate"

        return MismatchRecord(
            invoice_id_books=mismatch["invoice_id_books"],
            invoice_id_portal=mismatch["invoice_id_portal"],
            mismatch_type=mismatch["mismatch_type"],
            severity=final_severity,
            cause_reasoning=classification["cause_reasoning"],
            itc_risk_amount=mismatch["itc_risk_amount"],
            recommended_action=classification.get("recommended_action", mismatch["recommended_action"]),
        )

    except Exception as e:
        logger.warning("LLM classification failed for mismatch: %s — using rule-based", e)
        return mismatch  # Return the rule-based classification unchanged


async def _save_mismatches_to_db(
    mismatches: list[MismatchRecord],
    job_id: str,
    client_id: str,
) -> None:
    """Persist classified mismatches to the mismatches table."""
    import uuid
    async with get_db_session() as db:
        for m in mismatches:
            orm = MismatchORM(
                client_id=client_id,
                job_id=job_id,
                invoice_id_books=m["invoice_id_books"],
                invoice_id_portal=m["invoice_id_portal"],
                mismatch_type=m["mismatch_type"],
                severity=m["severity"],
                cause_reasoning=m["cause_reasoning"],
                itc_risk_amount=m["itc_risk_amount"],
                resolved=False,
            )
            db.add(orm)
        await db.commit()
    logger.info("Saved %d mismatches to DB", len(mismatches))


async def classifier_node(state: ReconciliationState) -> dict:
    """
    LangGraph node: Mismatch Classifier
    Runs after all parallel matching nodes complete.
    Classifies each mismatch using Groq, saves to DB, splits by severity.
    """
    logger.info(
        "[classifier] Starting for job=%s, %d raw mismatches",
        state["job_id"], len(state["raw_mismatches"])
    )

    raw_mismatches = state["raw_mismatches"]
    gateway = get_gateway()

    context = {
        "gstin": state["gstin"],
        "filing_period": state["filing_period"],
    }

    if not raw_mismatches:
        logger.info("[classifier] No mismatches to classify — clean reconciliation!")
        return {
            "current_node": "classifier",
            "progress_pct": 80,
            "classified_mismatches": [],
            "auto_fixable": [],
            "needs_followup": [],
            "needs_escalation": [],
            "total_mismatches": 0,
            "total_itc_at_risk": 0.0,
        }

    # Classify all mismatches (concurrently for speed, respects rate limiter internally)
    import asyncio
    classified = await asyncio.gather(*[
        _classify_single(m, gateway, context)
        for m in raw_mismatches
    ])

    # Split by severity
    auto_fixable = [m for m in classified if m["severity"] == "auto"]
    needs_followup = [m for m in classified if m["severity"] == "followup"]
    needs_escalation = [m for m in classified if m["severity"] == "escalate"]

    total_itc_risk = sum(m["itc_risk_amount"] for m in classified)

    # Log LLM usage
    llm_call_log = {
        "node": "classifier",
        "provider": "groq",
        "purpose": "mismatch_classification",
        "input_count": len(raw_mismatches),
        "total_itc_risk": total_itc_risk,
    }

    # Save to DB
    try:
        await _save_mismatches_to_db(
            list(classified),
            job_id=state["job_id"],
            client_id=state["client_id"],
        )
    except Exception as e:
        logger.error("[classifier] DB save failed: %s", e)
        # Don't fail the graph — mismatches are still in state even if DB write fails

    logger.info(
        "[classifier] Done: %d auto / %d followup / %d escalate | ITC risk: ₹%.2f",
        len(auto_fixable), len(needs_followup), len(needs_escalation), total_itc_risk
    )

    return {
        "current_node": "classifier",
        "progress_pct": 80,
        "classified_mismatches": list(classified),
        "auto_fixable": auto_fixable,
        "needs_followup": needs_followup,
        "needs_escalation": needs_escalation,
        "total_mismatches": len(classified),
        "total_itc_at_risk": total_itc_risk,
        "llm_calls": [llm_call_log],
    }
