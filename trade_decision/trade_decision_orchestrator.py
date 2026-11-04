from __future__ import annotations

import os
from typing import Any, Dict

import requests

from trade_decision.trade_execution import (
    decide_trade_execution,
)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

DECISION_MODEL = (
    os.getenv("OPENROUTER_MODEL_TRADE_DECIDER")
    or os.getenv("OPENROUTER_MODEL_CLASSIFIER")
)


def decision_llm_callable(messages):

    if not DECISION_MODEL:
        raise RuntimeError(
            "Missing OPENROUTER_MODEL_TRADE_DECIDER "
            "or OPENROUTER_MODEL_CLASSIFIER."
        )

    api_key = os.getenv("OPENROUTER_API_KEY")

    if not api_key:
        raise RuntimeError(
            "Missing OPENROUTER_API_KEY."
        )

    response = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": DECISION_MODEL,
            "messages": messages,
            "temperature": 0,
            "top_p": 1,
            "seed": 7,
            "response_format": {
                "type": "json_object"
            },
        },
        timeout=90,
    )

    response.raise_for_status()

    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(
            str(payload["error"])
        )

    return (
        payload["choices"][0]
        ["message"]
        .get("content", "")
    )


def attach_trade_decisions(
    flow: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Run the Decision Layer on TRADE nodes only.

    Does NOT execute Text2SQL or Thinking.

    Does NOT expand sq1/sq2 into the outer FlowRun.

    It only attaches trade_decision to each TRADE parent.
    """

    questions = flow.get(
        "questions",
        [],
    )

    trade_nodes = [
        q
        for q in questions
        if str(
            q.get(
                "route",
                "",
            )
        ).upper() == "TRADE"
    ]

    if not trade_nodes:
        flow["trade_decisions"] = {
            "decisions": []
        }
        return flow

    result = decide_trade_execution(
        trade_nodes=trade_nodes,
        llm_json_callable=decision_llm_callable,
    )

    decision_map = {
        decision["parent_id"]: decision
        for decision
        in result.get(
            "decisions",
            [],
        )
    }

    for node in questions:

        if str(
            node.get(
                "route",
                "",
            )
        ).upper() != "TRADE":
            continue

        node_id = str(
            node.get(
                "id",
                "",
            )
        ).strip()

        decision = decision_map.get(
            node_id
        )

        if decision is None:
            raise RuntimeError(
                f"No trade decision returned "
                f"for {node_id!r}"
            )

        node["trade_decision"] = (
            decision
        )

        # Useful shortcut for DIRECT requests only.
        if (
            decision.get(
                "decision_type"
            )
            == "DIRECT"
        ):
            node["trade_executor"] = (
                decision.get(
                    "execution_route"
                )
            )

        else:
            node["trade_executor"] = None

    flow["trade_decisions"] = (
        result
    )

    return flow