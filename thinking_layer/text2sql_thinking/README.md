# text2sql_thinking

Engine-free text2sql used by the **thinking layer** for trade questions that no
existing analytical engine covers (e.g. buyer concentration, HHI, dependency share).

## Key points

- **Same text2sql, no analytical engines.** Trend, Seasonality, Demand, Competition,
  Market Share, Market Saturation and Margin engines are removed, along with the
  Data Coverage engine. All other Prompt A core rules are kept (direction,
  geography, product/HS, Timeline, growth, ranking, Unit-Safe Price, Onboarding,
  Absence, New Buyers/Suppliers, SQL generation rules).

- **Formulas are computed in SQL.** If a question names or describes a metric
  (concentration, HHI, top-N share, ratio, rate), Prompt A computes it directly
  instead of forcing it into the nearest engine.

- **Fully standalone.** Own prompts and helpers; imports nothing from `text2sql/`.
  Changes in either folder do not affect the other. Uses the same `.env` keys.

- **Stateless and non-interactive.** No sessions, Trade Agent, HS picker,
  clarification, confirmation or follow-up suggestions. The question must arrive
  complete (product / HS code, geography, period written into it).

- **One entry point:** `run(question) -> dict`, with the same result keys as
  text2sql's pipeline (`stage`, `narrative`, `visualization`, `sql`, `rows`,
  `cols`, ...) plus `normalized_query`, `attempts` and `seconds`. Never raises.

- **Thinking layer handles the rest.** Step cap, choosing web vs text2sql,
  rewriting vague terms into explicit formulas, reviewing answers and the
  temporary session all live in the layer above, not here.
  
- **Skeleton for the thinking layer.** `PROMPT_A_SKELETON_BIBLE_THINKING` is the
  engine-free capability map. `run()` does not use it; it tells the thinking
  layer what this text2sql can answer.

## Flow

```
              question  (complete, from the thinking layer)
                 │
                 ▼
        ┌──────────────────┐
        │    Normalize     │   typos, whitespace, usa/uk/uae
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐
        │ Prompt A         │   interpretation + plan + SQL
        │ (no engines,     │   (formulas computed in SQL)
        │  no coverage)    │
        └────────┬─────────┘
                 ▼
        ┌──────────────────┐   error    ┌──────────────────┐
        │      VeloDB       │ ─────────► │ Prompt B         │
        │  (Apache Doris)  │ ◄───────── │ (fix VeloDB SQL) │
        └────────┬─────────┘  retry     └──────────────────┘
                 │ success        (max 3 attempts in total)
                 ▼
        ┌──────────────────┐
        │ Prompt C         │   narrative + visualization spec
        │ (no coverage)    │
        └────────┬─────────┘
                 ▼
            result dict  ──►  back to the thinking layer
```

## Files

| File | What it holds |
|---|---|
| `text2sql_thinking_tool.py` | `run()`, normalize, Gemini call, VeloDB/PyMySQL runner, parsers |
| `text2sql_thinking_prompts/text2sql_thinking_prompts_all.py` | Skeleton, Normalize, Prompt A, B, C (thinking versions) |
| `cli.py` | Terminal tester |
| `__init__.py` | Exports `run` and `PROMPT_A_SKELETON_BIBLE_THINKING` |


## VeloDB migration

This Thinking Text2SQL is migrated independently from the normal Text2SQL path. Prompt A and Prompt B are VeloDB-native, and the runner executes through PyMySQL against `VELODB_*` environment variables. The prompt contract targets the validated VeloDB Cloud 26.0.5.1 behavior used by the main Text2SQL migration: `MONTHS_ADD`, `DATEDIFF(end,start)`, `DATE_TRUNC(date, unit)`, aggregate `PERCENTILE_CONT(value,p)`, `GROUP_CONCAT`, `SUM(CASE...)` instead of `COUNT_IF`, and LIKE/NGRAM candidate-resolution rules instead of Snowflake Jaro-Winkler. Thinking mode still has no predefined analytical-engine layer and no automatic Data Coverage CTEs.

## Usage

```python
from thinking_layer.text2sql_thinking import run

out = run("Share of the top 5 buyers in total trade value for HS 0901 "
          "imports into Germany in the last 12 months")
print(out["stage"], out["narrative"])
```

Test from the project root:

```
python -m thinking_layer.text2sql_thinking.cli                          # interactive
python -m thinking_layer.text2sql_thinking.cli -q "your question" --plan
python -m thinking_layer.text2sql_thinking.cli -q "..." --rows 20 --json out.json
```

Set `DEBUG_TRADE_THINKING=0` to hide step-by-step debug prints.

## Result stages

| `stage` | Meaning |
|---|---|
| `success` | SQL ran; `rows` may still be empty |
| `prompt_a_no_sql` | Prompt A returned no SQL |
| `sql_execution_failure` | All fix attempts failed; see `error` and `attempts` |
| `pipeline_exception` | Unexpected error (e.g. API key missing); see `error` |

## Tip

Vague terms like "buyer concentration" are interpreted by the model and can vary
between runs. Explicit questions give consistent answers:
"HHI of buyer value shares, HS 0901, imports into Germany, last 12 months".