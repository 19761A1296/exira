# text2sql_thinking/cli.py
"""
Try text2sql_thinking from the terminal.

    python -m thinking_layer.text2sql_thinking.cli                      # interactive, type questions
    python -m thinking_layer.text2sql_thinking.cli -q "your question"   # one question, then exit
    python -m thinking_layer.text2sql_thinking.cli -q "..." --rows 20   # show more rows
    python -m thinking_layer.text2sql_thinking.cli -q "..." --plan      # also print the SQL plan
    python -m thinking_layer.text2sql_thinking.cli -q "..." --json out.json   # save the full result

Run from the project root (exira_prod) so the thinking_layer.* imports resolve and the .env there is picked up.
Set DEBUG_TRADE_THINKING=0 to hide the step-by-step debug prints.
"""

import argparse
import json

from thinking_layer.text2sql_thinking.text2sql_thinking_tool import run

LINE = "─" * 72


def _section(title: str, body: str) -> None:
    print(f"\n{LINE}\n{title}\n{LINE}\n{body.strip() if body else '(none)'}")


def _table(rows: list, cols: list, limit: int) -> str:
    if not rows:
        return "(no rows)"
    shown = rows[:limit]
    widths = {c: min(40, max(len(str(c)), *(len(str(r.get(c, ""))) for r in shown))) for c in cols}
    fmt = lambda vals: " | ".join(str(v)[:widths[c]].ljust(widths[c]) for c, v in zip(cols, vals))
    out = [fmt(cols), "-+-".join("-" * widths[c] for c in cols)]
    out += [fmt([r.get(c, "") for c in cols]) for r in shown]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} more row(s)")
    return "\n".join(out)


def show(question: str, rows_limit: int, show_plan: bool, json_path: str | None) -> None:
    out = run(question)
    parts = out.get("prompt_a_parts") or {}

    _section("QUESTION", question)
    if out.get("normalized_query") and out["normalized_query"] != question:
        _section("NORMALIZED", out["normalized_query"])
    _section("STAGE", f"{out['stage']}   ({out['seconds']}s)" + (f"\nERROR: {out['error']}" if out.get("error") else ""))
    _section("INTERPRETATION", parts.get("interpretation_summary", ""))
    if show_plan:
        _section("SQL PLAN", parts.get("sql_plan", ""))
    for i, a in enumerate(out.get("attempts") or [], start=1):
        _section(f"FAILED ATTEMPT {i}", f"{a['sql']}\n\nERROR: {a['error']}")
    _section("SQL" + (" (after fixes)" if out.get("attempts") else ""), out.get("sql", ""))
    _section(f"ROWS ({len(out.get('rows') or [])})", _table(out.get("rows") or [], out.get("cols") or [], rows_limit))
    _section("NARRATIVE (Prompt C)", out.get("narrative", ""))
    _section("VISUALIZATION", json.dumps(out.get("visualization"), indent=2, ensure_ascii=False))

    if json_path:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nSaved full result to {json_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Engine-free text2sql tester")
    ap.add_argument("-q", "--question", help="run one question and exit")
    ap.add_argument("--rows", type=int, default=10, help="rows to print (default 10)")
    ap.add_argument("--plan", action="store_true", help="also print the SQL Implementation Plan")
    ap.add_argument("--json", help="save the full result dict to this file")
    args = ap.parse_args()

    if args.question:
        show(args.question, args.rows, args.plan, args.json)
        return

    print("text2sql_thinking — type a question, or 'exit' to quit.")
    while True:
        try:
            q = input("\nQuestion: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if q.lower() in {"exit", "quit", "q"}:
            break
        if q:
            show(q, args.rows, args.plan, args.json)


if __name__ == "__main__":
    main()
