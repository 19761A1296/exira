from __future__ import annotations

from typing import Any, Callable, Dict


VALID_EXECUTORS = {
    "TEXT2SQL",
    "THINKING",
}


def route_trade_task(
    task: Dict[str, Any],
    text2sql_handler: Callable,
    thinking_handler: Callable,
) -> Dict[str, Any]:
    """
    Route ONE atomic TRADE task.

    The task is already classified by the Decision Layer.

    This function does not decide anything.
    It only dispatches.
    """

    executor = str(
        task.get(
            "trade_executor",
            "",
        )
    ).upper().strip()

    if executor not in VALID_EXECUTORS:
        raise ValueError(
            f"Invalid trade executor: "
            f"{executor!r}"
        )

    if executor == "TEXT2SQL":

        if text2sql_handler is None:
            raise RuntimeError(
                "TEXT2SQL handler is not configured."
            )

        return text2sql_handler(
            task
        )

    if executor == "THINKING":

        if thinking_handler is None:
            raise RuntimeError(
                "THINKING handler is not configured."
            )

        return thinking_handler(
            task
        )

    raise RuntimeError(
        "Unreachable trade router state."
    )