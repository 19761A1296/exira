from __future__ import annotations

from typing import Any, Callable, Dict, List

from trade_decision.trade_router import route_trade_task


# ============================================================
# CONFIG
# ============================================================

MAX_THINKING_GENERATED_QUESTIONS = 5

VALID_RESULT_STATUSES = {
    "FINAL",
    "CONTINUE",
}


# ============================================================
# SMALL HELPERS
# ============================================================

def _task_query(
    node: Dict[str, Any],
) -> str:
    """
    Read a query from any of the formats currently used
    across EXIRA.
    """

    return str(
        node.get("resolved_query")
        or node.get("query")
        or node.get("question")
        or node.get("text")
        or ""
    ).strip()


def _as_list(
    value: Any,
) -> List[Any]:

    if isinstance(value, list):
        return value

    return []


def _dedupe(
    values: List[str],
) -> List[str]:

    return list(
        dict.fromkeys(
            value
            for value in values
            if value
        )
    )


# ============================================================
# NORMALIZE EXECUTOR RESULT
# ============================================================

def normalize_executor_result(
    raw_result: Any,
    *,
    executor: str,
    task_id: str,
) -> Dict[str, Any]:
    """
    Convert TEXT2SQL / THINKING output into one common
    Mini Connector result format.

    Supported input forms:

        "plain text"

    or:

        {
            "text": "...",
            "ok": True,
            "pending": False
        }

    or Thinking:

        {
            "status": "FINAL",
            "answer": "..."
        }

    or:

        {
            "status": "CONTINUE",
            "generated_questions": [...]
        }

    Existing EXIRA Text2SQL handler output therefore remains
    compatible.
    """

    # --------------------------------------------------------
    # Plain string
    # --------------------------------------------------------

    if isinstance(
        raw_result,
        str,
    ):

        return {
            "task_id": task_id,
            "executor": executor,
            "status": "FINAL",
            "text": raw_result.strip(),
            "ok": True,
            "pending": False,
            "needs_answer": False,
            "data": None,
            "generated_questions": [],
            "error": "",
            "raw": raw_result,
        }

    # --------------------------------------------------------
    # Nothing returned
    # --------------------------------------------------------

    if raw_result is None:

        return {
            "task_id": task_id,
            "executor": executor,
            "status": "FINAL",
            "text": "",
            "ok": False,
            "pending": False,
            "needs_answer": False,
            "data": None,
            "generated_questions": [],
            "error":
                f"{executor} returned no result.",
            "raw": None,
        }

    # --------------------------------------------------------
    # Must otherwise be a dictionary
    # --------------------------------------------------------

    if not isinstance(
        raw_result,
        dict,
    ):

        return {
            "task_id": task_id,
            "executor": executor,
            "status": "FINAL",
            "text": str(raw_result),
            "ok": False,
            "pending": False,
            "needs_answer": False,
            "data": None,
            "generated_questions": [],
            "error":
                f"{executor} returned unsupported "
                f"type {type(raw_result).__name__}.",
            "raw": raw_result,
        }

    generated_questions = (
        raw_result.get(
            "generated_questions"
        )
        or raw_result.get(
            "questions"
        )
        or []
    )

    if not isinstance(
        generated_questions,
        list,
    ):

        generated_questions = []

    # Thinking may call its final answer "answer".
    text = str(
        raw_result.get("text")
        or raw_result.get("answer")
        or raw_result.get("final_answer")
        or ""
    ).strip()

    status = str(
        raw_result.get(
            "status",
            "",
        )
    ).upper().strip()

    # Existing Text2SQL does not return status.
    # Treat normal handler output as FINAL.
    if not status:

        if generated_questions:
            status = "CONTINUE"

        else:
            status = "FINAL"

    if status not in VALID_RESULT_STATUSES:

        return {
            "task_id": task_id,
            "executor": executor,
            "status": "FINAL",
            "text": text,
            "ok": False,
            "pending": bool(
                raw_result.get(
                    "pending",
                    False,
                )
            ),
            "needs_answer": bool(
                raw_result.get(
                    "needs_answer",
                    False,
                )
            ),
            "data": raw_result.get(
                "data"
            ),
            "generated_questions": [],
            "error":
                f"{executor} returned invalid "
                f"status {status!r}.",
            "raw": raw_result,
        }

    ok_value = raw_result.get(
        "ok"
    )

    if ok_value is None:
        ok_value = not bool(
            raw_result.get(
                "error"
            )
        )

    return {
        "task_id":
            task_id,

        "executor":
            executor,

        "status":
            status,

        "text":
            text,

        "ok":
            bool(ok_value),

        "pending":
            bool(
                raw_result.get(
                    "pending",
                    False,
                )
            ),

        "needs_answer":
            bool(
                raw_result.get(
                    "needs_answer",
                    False,
                )
            ),

        "data":
            raw_result.get(
                "data"
            ),

        "generated_questions":
            generated_questions,

        "error":
            str(
                raw_result.get(
                    "error",
                    "",
                )
                or ""
            ).strip(),

        "raw":
            raw_result,
    }


# ============================================================
# GENERATED QUESTION NORMALIZATION
# ============================================================

def normalize_generated_question(
    generated: Any,
    *,
    parent_task_id: str,
    generated_number: int,
) -> Dict[str, Any]:
    """
    Normalize one question generated by the Thinking Layer.

    IMPORTANT CONTRACT WITH THINKING TEAM:

    Every generated question must identify its destination:

        {
            "query": "...",
            "trade_executor": "TEXT2SQL"
        }

    or:

        {
            "query": "...",
            "execution_route": "TEXT2SQL"
        }

    We intentionally DO NOT guess the destination.
    """

    if isinstance(
        generated,
        str,
    ):

        raise ValueError(
            "Thinking generated a plain-string question. "
            "Generated questions must include an explicit "
            "trade_executor."
        )

    if not isinstance(
        generated,
        dict,
    ):

        raise ValueError(
            "Thinking generated an invalid question "
            f"type: {type(generated).__name__}"
        )

    query = _task_query(
        generated
    )

    if not query:

        raise ValueError(
            "Thinking generated a question with "
            "an empty query."
        )

    executor = str(
        generated.get(
            "trade_executor"
        )
        or generated.get(
            "execution_route"
        )
        or ""
    ).upper().strip()

    if executor not in {
        "TEXT2SQL",
        "THINKING",
    }:

        raise ValueError(
            "Thinking-generated question must "
            "explicitly specify trade_executor as "
            "TEXT2SQL or THINKING."
        )

    task_id = str(
        generated.get(
            "task_id"
        )
        or generated.get(
            "id"
        )
        or (
            f"{parent_task_id}."
            f"g{generated_number}"
        )
    ).strip()

    task = dict(
        generated
    )

    task.update(
        {
            "task_id":
                task_id,

            "id":
                task_id,

            "query":
                query,

            "text":
                query,

            "route":
                "TRADE",

            "trade_executor":
                executor,
        }
    )

    return task


# ============================================================
# FAILURE RESULT
# ============================================================

def _failure_result(
    *,
    task_id: str,
    executor: str,
    error: str,
) -> Dict[str, Any]:

    return {
        "task_id": task_id,
        "executor": executor,
        "status": "FINAL",
        "text": "",
        "ok": False,
        "pending": False,
        "needs_answer": False,
        "data": None,
        "generated_questions": [],
        "error": error,
        "raw": None,
    }


# ============================================================
# EXECUTE ONE ATOMIC TRADE TASK
# ============================================================

def _execute_atomic_task(
    task: Dict[str, Any],
    *,
    text2sql_handler: Callable,
    thinking_handler: Callable,
    generation_budget: Dict[str, int],
) -> Dict[str, Any]:
    """
    Execute one atomic task.

    TEXT2SQL:
        runs once.

    THINKING:
        may return CONTINUE and generate additional tasks.

    The Mini Connector then:
        1. executes those generated tasks,
        2. gives their answers back to Thinking,
        3. calls Thinking again.

    Total generated questions are capped globally.
    """

    task_id = str(
        task.get(
            "task_id"
        )
        or task.get(
            "id"
        )
        or "trade-task"
    ).strip()

    executor = str(
        task.get(
            "trade_executor",
            "",
        )
    ).upper().strip()

    # --------------------------------------------------------
    # TEXT2SQL is one-shot
    # --------------------------------------------------------

    if executor == "TEXT2SQL":

        raw_result = route_trade_task(
            task=task,
            text2sql_handler=
                text2sql_handler,
            thinking_handler=
                thinking_handler,
        )

        result = (
            normalize_executor_result(
                raw_result,
                executor=executor,
                task_id=task_id,
            )
        )

        # TEXT2SQL should not create a Thinking-style loop.
        if (
            result["status"]
            == "CONTINUE"
        ):

            return _failure_result(
                task_id=task_id,
                executor=executor,
                error=(
                    "TEXT2SQL returned CONTINUE. "
                    "Only THINKING may request "
                    "generated follow-up tasks."
                ),
            )

        return result

    # --------------------------------------------------------
    # THINKING may loop
    # --------------------------------------------------------

    if executor != "THINKING":

        return _failure_result(
            task_id=task_id,
            executor=executor,
            error=(
                f"Unsupported executor "
                f"{executor!r}"
            ),
        )

    current_task = dict(
        task
    )

    thinking_iteration = 0

    while True:

        thinking_iteration += 1

        current_task[
            "thinking_iteration"
        ] = thinking_iteration

        current_task[
            "max_generated_questions"
        ] = generation_budget[
            "limit"
        ]

        current_task[
            "generated_questions_used"
        ] = generation_budget[
            "used"
        ]

        raw_result = route_trade_task(
            task=current_task,
            text2sql_handler=
                text2sql_handler,
            thinking_handler=
                thinking_handler,
        )

        result = (
            normalize_executor_result(
                raw_result,
                executor="THINKING",
                task_id=task_id,
            )
        )

        # ----------------------------------------------------
        # Existing user-interaction/pending behavior
        # ----------------------------------------------------

        if (
            result["pending"]
            or result["needs_answer"]
        ):

            return result

        # ----------------------------------------------------
        # Thinking finished
        # ----------------------------------------------------

        if (
            result["status"]
            == "FINAL"
        ):

            return result

        # ----------------------------------------------------
        # Thinking requested more evidence/work
        # ----------------------------------------------------

        generated_questions = (
            result[
                "generated_questions"
            ]
        )

        if not generated_questions:

            return _failure_result(
                task_id=task_id,
                executor="THINKING",
                error=(
                    "THINKING returned CONTINUE "
                    "without generated_questions."
                ),
            )

        generated_answers = []

        for generated in (
            generated_questions
        ):

            # ----------------------------------------------
            # Hard cap: maximum 5 generated questions
            # ----------------------------------------------

            if (
                generation_budget[
                    "used"
                ]
                >= generation_budget[
                    "limit"
                ]
            ):

                return _failure_result(
                    task_id=task_id,
                    executor="THINKING",
                    error=(
                        "Thinking generated-question "
                        f"limit of "
                        f"{generation_budget['limit']} "
                        "was reached."
                    ),
                )

            generation_budget[
                "used"
            ] += 1

            generated_number = (
                generation_budget[
                    "used"
                ]
            )

            try:

                generated_task = (
                    normalize_generated_question(
                        generated,
                        parent_task_id=
                            task_id,
                        generated_number=
                            generated_number,
                    )
                )

            except Exception as exc:

                return _failure_result(
                    task_id=task_id,
                    executor="THINKING",
                    error=str(exc),
                )

            # Pass the parent Thinking task context forward.
            generated_task[
                "generated_by"
            ] = task_id

            generated_task[
                "thinking_iteration"
            ] = thinking_iteration

            # Generated work may itself be TEXT2SQL
            # or THINKING.
            generated_result = (
                _execute_atomic_task(
                    generated_task,
                    text2sql_handler=
                        text2sql_handler,
                    thinking_handler=
                        thinking_handler,
                    generation_budget=
                        generation_budget,
                )
            )

            generated_answers.append(
                {
                    "task_id":
                        generated_task[
                            "task_id"
                        ],

                    "query":
                        generated_task[
                            "query"
                        ],

                    "trade_executor":
                        generated_task[
                            "trade_executor"
                        ],

                    "result":
                        generated_result,
                }
            )

            # If generated work pauses for some existing
            # EXIRA interaction, propagate the pause upward.
            if (
                generated_result.get(
                    "pending"
                )
                or generated_result.get(
                    "needs_answer"
                )
            ):

                generated_result[
                    "parent_thinking_task"
                ] = task_id

                return generated_result

            if not generated_result.get(
                "ok",
                False,
            ):

                return _failure_result(
                    task_id=task_id,
                    executor="THINKING",
                    error=(
                        "A Thinking-generated task "
                        "failed: "
                        + str(
                            generated_result.get(
                                "error"
                            )
                            or generated_task[
                                "task_id"
                            ]
                        )
                    ),
                )

        # ----------------------------------------------------
        # Give generated answers back to Thinking
        # ----------------------------------------------------

        current_task = dict(
            task
        )

        current_task[
            "continuation"
        ] = {
            "previous_thinking_response":
                result,

            "generated_answers":
                generated_answers,
        }


# ============================================================
# BUILD ONE ATOMIC TASK FROM A PARENT
# ============================================================

def _direct_task(
    parent_node: Dict[str, Any],
    decision: Dict[str, Any],
) -> Dict[str, Any]:

    parent_id = str(
        parent_node.get(
            "id",
            "trade",
        )
    ).strip()

    query = _task_query(
        parent_node
    )

    return {
        "task_id":
            parent_id,

        "id":
            parent_id,

        "parent_id":
            parent_id,

        "query":
            query,

        "text":
            query,

        "route":
            "TRADE",

        "trade_executor":
            decision[
                "execution_route"
            ],

        "depends_on":
            list(
                parent_node.get(
                    "depends_on"
                )
                or []
            ),

        "dependency_answers":
            {},

        "matched_capabilities":
            decision.get(
                "matched_capabilities",
                [],
            ),

        "matched_engines":
            decision.get(
                "matched_engines",
                [],
            ),

        "unsupported_concepts":
            decision.get(
                "unsupported_concepts",
                [],
            ),

        "trade_decision":
            decision,

        "parent_node":
            parent_node,
    }


# ============================================================
# BUILD ONE SPLIT SUBQUERY TASK
# ============================================================

def _split_task(
    parent_node: Dict[str, Any],
    decision: Dict[str, Any],
    subquery: Dict[str, Any],
    completed_results: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:

    parent_id = str(
        parent_node.get(
            "id",
            "trade",
        )
    ).strip()

    local_id = str(
        subquery[
            "subquery_id"
        ]
    ).strip()

    global_id = (
        f"{parent_id}.{local_id}"
    )

    dependencies = list(
        subquery.get(
            "depends_on"
        )
        or []
    )

    dependency_answers = {}

    for dependency in dependencies:

        if (
            dependency
            not in completed_results
        ):

            raise RuntimeError(
                f"{global_id} depends on "
                f"{dependency!r}, but that "
                "subquery has not completed."
            )

        dependency_answers[
            dependency
        ] = completed_results[
            dependency
        ]

    return {
        "task_id":
            global_id,

        "id":
            global_id,

        "parent_id":
            parent_id,

        "subquery_id":
            local_id,

        "query":
            subquery[
                "query"
            ],

        "text":
            subquery[
                "query"
            ],

        "route":
            "TRADE",

        "trade_executor":
            subquery[
                "execution_route"
            ],

        "depends_on":
            dependencies,

        "dependency_answers":
            dependency_answers,

        "matched_capabilities":
            subquery.get(
                "matched_capabilities",
                [],
            ),

        "matched_engines":
            subquery.get(
                "matched_engines",
                [],
            ),

        "unsupported_concepts":
            subquery.get(
                "unsupported_concepts",
                [],
            ),

        "trade_decision_reason":
            subquery.get(
                "reason",
                "",
            ),

        "trade_decision_confidence":
            subquery.get(
                "confidence",
                0.0,
            ),

        "parent_composition":
            decision.get(
                "composition",
                {},
            ),

        "parent_node":
            parent_node,
    }


# ============================================================
# TERMINAL SUBQUERY IDS
# ============================================================

def _terminal_subquery_ids(
    subqueries: List[Dict[str, Any]],
) -> List[str]:
    """
    A terminal subquery is a subquery whose result is not used
    by another subquery.
    """

    referenced = {
        dependency
        for sq in subqueries
        for dependency
        in (
            sq.get(
                "depends_on"
            )
            or []
        )
    }

    return [
        str(
            sq.get(
                "subquery_id",
                "",
            )
        )
        for sq in subqueries
        if str(
            sq.get(
                "subquery_id",
                "",
            )
        )
        not in referenced
    ]


# ============================================================
# COMPOSE SPLIT RESULT
# ============================================================

def _compose_split_text(
    decision: Dict[str, Any],
    completed_results: Dict[str, Dict[str, Any]],
) -> str:
    """
    The Mini Connector is not an analytical synthesis engine.

    If there is exactly one terminal child, use its answer as
    the parent answer.

    Otherwise preserve all child answers in execution order.

    A future final-synthesis layer may replace this behavior
    without changing the routing infrastructure.
    """

    subqueries = (
        decision.get(
            "subqueries"
        )
        or []
    )

    terminal_ids = (
        _terminal_subquery_ids(
            subqueries
        )
    )

    # Common sequential case:
    #
    # sq1 TEXT2SQL
    #   ↓
    # sq2 THINKING
    #
    # sq2 is already the final business answer.
    if len(terminal_ids) == 1:

        terminal_result = (
            completed_results.get(
                terminal_ids[0]
            )
            or {}
        )

        text = str(
            terminal_result.get(
                "text",
                "",
            )
            or ""
        ).strip()

        if text:
            return text

    # Multiple terminal branches.
    blocks = []

    for sq in subqueries:

        local_id = str(
            sq.get(
                "subquery_id",
                "",
            )
        )

        result = (
            completed_results.get(
                local_id
            )
            or {}
        )

        text = str(
            result.get(
                "text",
                "",
            )
            or ""
        ).strip()

        if not text:
            continue

        blocks.append(
            f"{local_id}:\n{text}"
        )

    return "\n\n".join(
        blocks
    ).strip()


# ============================================================
# PRIMARY MINI CONNECTOR
# ============================================================

def execute_trade_parent(
    parent_node: Dict[str, Any],
    *,
    text2sql_handler: Callable,
    thinking_handler: Callable,
    max_generated_questions: int =
        MAX_THINKING_GENERATED_QUESTIONS,
) -> Dict[str, Any]:
    """
    Execute ONE parent TRADE node.

    The parent has already passed through:

        Analyzer
        → Flow Builder
        → Classifier
        → Decision Layer

    This function owns runtime coordination only.

    DIRECT:
        parent → TEXT2SQL
        or
        parent → THINKING

    SPLIT:
        sq1 → ...
        sq2 → ...
        sq3 → ...

    Thinking-generated questions are also coordinated here.

    Maximum generated questions defaults to 5.
    """

    if not isinstance(
        parent_node,
        dict,
    ):

        raise TypeError(
            "parent_node must be a dictionary."
        )

    decision = parent_node.get(
        "trade_decision"
    )

    if not isinstance(
        decision,
        dict,
    ):

        raise ValueError(
            "TRADE parent node has no "
            "trade_decision attached."
        )

    parent_id = str(
        parent_node.get(
            "id",
            "trade",
        )
    ).strip()

    decision_type = str(
        decision.get(
            "decision_type",
            "",
        )
    ).upper().strip()

    generation_budget = {
        "used": 0,
        "limit": max(
            0,
            int(
                max_generated_questions
            ),
        ),
    }

    # ========================================================
    # DIRECT
    # ========================================================

    if decision_type == "DIRECT":

        task = _direct_task(
            parent_node,
            decision,
        )

        result = _execute_atomic_task(
            task,
            text2sql_handler=
                text2sql_handler,
            thinking_handler=
                thinking_handler,
            generation_budget=
                generation_budget,
        )

        return {
            "parent_id":
                parent_id,

            "decision_type":
                "DIRECT",

            "status":
                result.get(
                    "status",
                    "FINAL",
                ),

            "text":
                result.get(
                    "text",
                    "",
                ),

            "ok":
                bool(
                    result.get(
                        "ok",
                        False,
                    )
                ),

            "pending":
                bool(
                    result.get(
                        "pending",
                        False,
                    )
                ),

            "needs_answer":
                bool(
                    result.get(
                        "needs_answer",
                        False,
                    )
                ),

            "executor":
                task[
                    "trade_executor"
                ],

            "parts": {
                parent_id:
                    result
            },

            "generated_questions_used":
                generation_budget[
                    "used"
                ],

            "trade_decision":
                decision,

            "error":
                result.get(
                    "error",
                    "",
                ),
        }

    # ========================================================
    # SPLIT
    # ========================================================

    if decision_type != "SPLIT":

        raise ValueError(
            f"{parent_id}: invalid decision_type "
            f"{decision_type!r}"
        )

    subqueries = (
        decision.get(
            "subqueries"
        )
        or []
    )

    if not isinstance(
        subqueries,
        list,
    ) or not subqueries:

        raise ValueError(
            f"{parent_id}: SPLIT decision has "
            "no subqueries."
        )

    completed_results: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for subquery in subqueries:

        local_id = str(
            subquery.get(
                "subquery_id",
                "",
            )
        ).strip()

        if not local_id:

            raise ValueError(
                f"{parent_id}: split subquery "
                "has no subquery_id."
            )

        task = _split_task(
            parent_node=
                parent_node,
            decision=
                decision,
            subquery=
                subquery,
            completed_results=
                completed_results,
        )

        result = _execute_atomic_task(
            task,
            text2sql_handler=
                text2sql_handler,
            thinking_handler=
                thinking_handler,
            generation_budget=
                generation_budget,
        )

        completed_results[
            local_id
        ] = result

        # ----------------------------------------------------
        # Preserve existing EXIRA pause semantics
        # ----------------------------------------------------

        if (
            result.get(
                "pending"
            )
            or result.get(
                "needs_answer"
            )
        ):

            return {
                "parent_id":
                    parent_id,

                "decision_type":
                    "SPLIT",

                "status":
                    result.get(
                        "status",
                        "FINAL",
                    ),

                "text":
                    result.get(
                        "text",
                        "",
                    ),

                "ok":
                    False,

                "pending":
                    bool(
                        result.get(
                            "pending",
                            False,
                        )
                    ),

                "needs_answer":
                    bool(
                        result.get(
                            "needs_answer",
                            False,
                        )
                    ),

                "parts":
                    completed_results,

                "generated_questions_used":
                    generation_budget[
                        "used"
                    ],

                "trade_decision":
                    decision,

                "error":
                    result.get(
                        "error",
                        "",
                    ),
            }

        # ----------------------------------------------------
        # Child execution failure
        # ----------------------------------------------------
        # Keep going: later subqueries (especially THINKING) receive this
        # failed result as dependency evidence and can work around the gap.

        if not result.get(
            "ok",
            False,
        ):

            print(
                f"[MINI CONNECTOR] {parent_id}.{local_id} failed; "
                "continuing with the rest of the SPLIT"
            )
            continue

    # ========================================================
    # ALL SPLIT CHILDREN COMPLETED
    # ========================================================

    final_text = _compose_split_text(
        decision=
            decision,
        completed_results=
            completed_results,
    )

    return {
        "parent_id":
            parent_id,

        "decision_type":
            "SPLIT",

        "status":
            "FINAL",

        "text":
            final_text,

        "ok":
            all(
                (completed_results.get(tid) or {}).get("ok", False)
                for tid in _terminal_subquery_ids(subqueries)
            ),

        "pending":
            False,

        "needs_answer":
            False,

        "parts":
            completed_results,

        "composition":
            decision.get(
                "composition",
                {},
            ),

        "generated_questions_used":
            generation_budget[
                "used"
            ],

        "trade_decision":
            decision,

        "error":
            "",
    }