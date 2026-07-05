"""
orchestration_service/graph.py
LangGraph StateGraph — the reconciliation pipeline.

Graph topology:
  START
    │
    ▼
  normalise                    ← Node 1: Load + clean invoices (Gemini)
    │
    ├──── gstr2a_matcher ────┐  ← Node 2a: Purchase register vs GSTR-2A (parallel)
    ├──── gstr1_validator ───┤  ← Node 2b: Sales register vs GSTR-1 (parallel)
    └──── tax_checker ───────┘  ← Node 2c: Aggregate vs GSTR-3B (parallel)
                    │
                    ▼
               classifier       ← Node 3: Classify all mismatches (Groq)
                    │
                    ▼
                  END

Parallel nodes (2a, 2b, 2c) all run simultaneously using LangGraph's
fan-out routing. Their outputs are merged by the operator.add reducer on
raw_mismatches in ReconciliationState.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from langgraph.graph import StateGraph, START, END

from orchestration_service.state import ReconciliationState, make_initial_state
from orchestration_service.agents.normalise import normalise_node
from orchestration_service.agents.gstr2a_matcher import gstr2a_matcher_node
from orchestration_service.agents.gstr1_validator import gstr1_validator_node
from orchestration_service.agents.tax_checker import tax_checker_node
from orchestration_service.agents.classifier import classifier_node

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# Error gate: check if normalise failed before parallel nodes
# ──────────────────────────────────────────────────────────

def route_after_normalise(state: ReconciliationState) -> Literal["parallel", "end"]:
    """
    Routing function after normalise node.
    If normalise hit a fatal error, skip to END.
    Otherwise, fan out to all three parallel nodes.
    """
    if state.get("error"):
        logger.error("Normalise failed: %s — terminating graph", state["error"])
        return "end"
    return "parallel"


# ──────────────────────────────────────────────────────────
# Build the graph
# ──────────────────────────────────────────────────────────

def build_reconciliation_graph() -> StateGraph:
    """
    Construct and compile the LangGraph reconciliation pipeline.
    Returns a compiled graph ready for .ainvoke().
    """
    builder = StateGraph(ReconciliationState)

    # ── Add nodes ──────────────────────────────────────────
    builder.add_node("normalise", normalise_node)
    builder.add_node("gstr2a_matcher", gstr2a_matcher_node)
    builder.add_node("gstr1_validator", gstr1_validator_node)
    builder.add_node("tax_checker", tax_checker_node)
    builder.add_node("classifier", classifier_node)

    # ── Edges ──────────────────────────────────────────────
    # START → normalise
    builder.add_edge(START, "normalise")

    # normalise → (conditional) → parallel matching nodes
    builder.add_conditional_edges(
        "normalise",
        route_after_normalise,
        {
            "parallel": ["gstr2a_matcher", "gstr1_validator", "tax_checker"],
            "end": END,
        }
    )

    # All three parallel nodes → classifier (fan-in)
    builder.add_edge("gstr2a_matcher", "classifier")
    builder.add_edge("gstr1_validator", "classifier")
    builder.add_edge("tax_checker", "classifier")

    # classifier → END
    builder.add_edge("classifier", END)

    return builder.compile()


# ── Singleton compiled graph ───────────────────────────────
_graph = None


def get_graph():
    """Return the singleton compiled reconciliation graph."""
    global _graph
    if _graph is None:
        _graph = build_reconciliation_graph()
        logger.info("Reconciliation graph compiled successfully")
    return _graph


# ──────────────────────────────────────────────────────────
# Public API: run a reconciliation job
# ──────────────────────────────────────────────────────────

async def run_reconciliation(
    job_id: str,
    client_id: str,
    gstin: str,
    filing_period: str,
    ca_user_id: str,
    progress_callback=None,
) -> ReconciliationState:
    """
    Run the full reconciliation pipeline for a job.

    Args:
        job_id: UUID of the Job record
        client_id: UUID of the Client record
        gstin: Taxpayer GSTIN
        filing_period: "YYYY-MM" e.g. "2024-03"
        ca_user_id: CA user identifier (for notifications)
        progress_callback: Optional async callable(node_name, progress_pct)
                           Called after each node completes

    Returns:
        Final ReconciliationState with all results
    """
    initial_state = make_initial_state(
        job_id=job_id,
        client_id=client_id,
        gstin=gstin,
        filing_period=filing_period,
        ca_user_id=ca_user_id,
    )

    graph = get_graph()

    logger.info(
        "Starting reconciliation: job=%s gstin=%s period=%s",
        job_id, gstin, filing_period
    )

    # Stream graph execution — get intermediate states for progress updates
    final_state = initial_state
    async for event in graph.astream(initial_state):
        for node_name, node_output in event.items():
            if isinstance(node_output, dict):
                # Merge node output into our tracked state
                final_state = {**final_state, **node_output}

                progress = node_output.get("progress_pct", 0)
                logger.debug("Node %s complete — progress: %d%%", node_name, progress)

                if progress_callback:
                    try:
                        await progress_callback(node_name, progress)
                    except Exception as e:
                        logger.warning("Progress callback failed: %s", e)

    logger.info(
        "Reconciliation complete: job=%s mismatches=%d itc_risk=%.2f",
        job_id,
        final_state.get("total_mismatches", 0),
        final_state.get("total_itc_at_risk", 0.0),
    )

    return final_state
