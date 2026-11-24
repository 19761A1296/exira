# summarizer/connector.py
"""Walks a tagged flow, runs each sub-question, and combines the answers.

    analyzer ──► question_flow_builder ──► classifier.tag_flow ──► connector

    run = FlowRun(flow, memory, handlers)
    out = run.start()

    while out["status"] == "needs_hs_pick":
        # the caller shows the engine's domain options and sends the user's pick
        # to the engine itself, then:
        out = run.resume()

    while out["status"] == "needs_answer":
        # the caller shows the engine's clarifying question as a short list,
        # takes the user's reply, then:
        out = run.supply_answer(text_the_engine_returned)

    out["status"] == "done"     -> out["answer"] is the combined reply
    out["status"] == "stopped"  -> out["answer"] explains why it stopped

FlowRun is resumable on purpose. The engine can interrupt a node twice over -
once to ask which HS industry a product belongs to, once to ask what an
ambiguous question actually means - and in Streamlit those pauses have to
survive a rerun, so the object is kept in session state rather than the call
stack.

The two pauses differ in who resolves them:

    needs_hs_pick   the CALLER sends the pick to the engine and calls resume(),
                    which retries the same node from the top.
    needs_answer    the CALLER sends the clarification answer to the engine,
                    gets the real reply back, and hands that text to
                    supply_answer(), which records it and moves on.

HANDLERS — injected by the caller, so this module imports no engine and no web.

    personal(question) -> str

    trade(question, node) -> {"text": str,          what to show and pass on
                              "ok": bool,           True only if data came back
                              "pending": bool,      True if the engine wants an HS pick
                              "needs_answer": bool} True if the engine asked a question

    For backward compatibility, a legacy trade(question) handler is also accepted.

    web(question)      -> str

WHAT EACH ROUTE DOES

    PERSONAL  personal()
    TRADE     trade() only. A failed/private trade-data query is NEVER silently
              substituted with a public-web answer.
    WEB       web() alone in a multi-node flow, because the grounding comes from
              the TRADE nodes it depends on; in a single-node flow trade() runs
              first and web() is appended, which is the existing behaviour

WHEN A NODE COMES BACK EMPTY

Both its source and its fallback produced nothing. If anything depends on that
node, the flow stops there and says so — a dependent question cannot be answered
from a blank. If nothing depends on it, the gap is recorded and the rest runs.

.env
----
    OPENROUTER_API_KEY=sk-or-...
    MODEL_CONNECTOR
"""

import inspect
import json
import os
import re
import unicodedata

import requests
from dotenv import load_dotenv

from summarizer.question_flow_builder import dependants_of

load_dotenv()

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("MODEL_CONNECTOR")
REWRITE_MAX_TOKENS = int(os.getenv("CONNECTOR_REWRITE_MAX_TOKENS", "1000"))
COMBINE_MAX_TOKENS = int(os.getenv("CONNECTOR_COMBINE_MAX_TOKENS", "3000"))
ANSWER_CHARS = int(os.getenv("CONNECTOR_ANSWER_CHARS", "3000"))
CONNECTOR_RETRY_MAX_TOKENS = int(os.getenv("CONNECTOR_RETRY_MAX_TOKENS", "12000"))
CONNECTOR_DEPENDENCY_HARD_LIMIT = int(os.getenv("CONNECTOR_DEPENDENCY_HARD_LIMIT", "50000"))
PRINT_CONNECTOR = (os.getenv("PRINT_CONNECTOR", "1") or "").strip() not in {"0", "false", "False", ""}


PROMPT_REWRITE = """You are rewriting one sub-question so it can be asked on its own.

The sub-question refers to the answer of an earlier sub-question. That answer is
now known. Put the real value into the question and hand back one self-contained
line.

RULES
- Replace every reference to an earlier question with the actual value from its
  answer: "the product identified in q1" becomes the real product, "those buyers"
  becomes the real buyer names.
- Take only what the question needs. If the earlier answer lists ten buyers and
  the question needs the top three, name the top three.
- Copy names, HS codes, ports, countries and figures exactly as the earlier
  answer wrote them. Never round, rename or abbreviate them.
- Change nothing else. Keep the deliverable, the time window, the geography and
  the register exactly as they are.
- If the earlier answer does not contain what the question needs, say so by
  leaving the reference as plain words rather than inventing a value.
- Never add analysis, never answer the question, never add a preamble.
- Preserve ownership and perspective. If the input refers to "my company",
  "our company", "me" or "us" in a company context, keep that relationship
  intact. Do not turn the user's own company into an unrelated third party.
- If an explicit company name is already present, copy it exactly.

EARLIER ANSWERS
{answers}

SUB-QUESTION TO REWRITE
{question}

Return ONLY this JSON:
{{"question": "the rewritten self-contained question"}}
"""


PROMPT_COMBINE = """You are answering a user on a trade-intelligence platform.

Their message may contain one question or several related questions. Each part
has already been answered separately by the appropriate system. Your job is to
combine those answers into ONE polished, highly readable final response.

The final response should feel like a strong professional assistant response,
not like several raw outputs pasted together.

RULES

ACCURACY
- Use only what the answers below contain.
- Add no figures, names, HS codes, companies, ports, countries, percentages,
  dates, claims or recommendations of your own.
- When you choose to surface a supplied name, company, HS code, port, country or
  numerical value, preserve it exactly as the underlying answer wrote it.
- You MAY omit technical diagnostic values that the user did not ask to see, as
  required by the USER-FACING ANALYTICS rules below. Omitting an unrequested
  diagnostic is not a factual change. Data-coverage values are the exception:
  when supplied, preserve their numbers and present them using the DATA COVERAGE
  rules below rather than hiding them.
- Do not change the meaning of any individual answer.
- Do not resolve contradictions by inventing new facts.

ORDER
- Follow the user's original order.
- Answer what they asked first, first.
- When a later answer depends on an earlier answer, explicitly name the entity
  it depends on so the relationship is clear to the user.

STRUCTURE AND READABILITY
- If the user's request contains multiple meaningful topics or deliverables,
  divide the response into clearly separated sections.
- Give each major topic a short, natural Markdown heading using ##.
- Headings should describe the user's actual question in human-readable language.
- Do NOT use generic headings such as "Question 1", "Part 2", "TRADE", "WEB",
  "PERSONAL", "q1", "q2", or internal system terminology.
- Do not create a heading for every tiny sentence. Group closely related facts
  under the same heading.
- A short introductory sentence is allowed before the first section when useful.
- Use short paragraphs beneath each heading.
- Use bullet points only when they genuinely improve readability, such as:
    * several markets
    * several adjacent product categories
    * several routes
    * several risks or actions
- Avoid giant walls of text.
- Avoid excessive headings for simple one-part questions.

HEADINGS SHOULD BE CONTEXTUAL

For example, if the user asks:
"Which product or HS code do I deal in the most, show its trend from the USA in
2025, explain how tariffs affect it, and suggest expansion opportunities"

A good structure would be similar to:

## Your Most-Dealt Product

...

## 2025 USA Trade Trend

...

## How Tariffs Affect This Product

...

## Expansion Opportunities

...

The exact headings should adapt naturally to the user's actual question and the
answers available.

DEPENDENT ANSWERS
- Where one answer builds on another, make the dependency obvious.
  Example:
  "Based on your most-dealt product, HS 84795000..."
- Do not make the reader work out what "this", "it", or "that product" refers to
  when the actual product or HS code is already known.

STYLE
- Professional, clear and conversational.
- Prioritize readability over dense prose.
- Be concise where the underlying answer is simple and detailed where the
  underlying answer requires explanation.
- Use Markdown headings, bullets and bold emphasis sparingly when they improve
  scanning.
- Do not repeat the same fact in multiple sections.
- Do not mention routing, agents, sub-questions, q1/q2, prompts, connectors,
  databases or internal processing. Do not expose raw internal source labels.
- You MAY refer naturally to the information you worked with, for example
  "From the trade records I have...", "I found...", or "From the latest market
  information I found..." when that makes the answer clearer and more human.

YOUR COMPANY / PERSPECTIVE (CRITICAL)
- USER COMPANY CONTEXT below is authoritative only for identifying the user's own
  company and choosing the correct speaking perspective. It is NOT an additional
  source of trade facts, figures, recommendations or market claims.
- If the user's question says "my", "our", "me" or "us" in a company/business
  context, treat that as the user's own company.
- If the question names the same company identified in USER COMPANY CONTEXT, treat
  that company as the user's company even when the question uses its exact legal
  name instead of "my company".
- For the user's own company, speak directly to the user: prefer "your company",
  "your buyers", "your exports", "your suppliers", "your markets", "you sold",
  and similar second-person wording.
- Do NOT describe the user's own company as an unrelated third party. For example,
  if WALMART INC is the user's company, prefer "your top buyers are..." over
  "Walmart Inc's top buyers are...".
- When useful for clarity, the first mention may say "your company, WALMART INC"
  or "WALMART INC (your company)"; after that, prefer natural second-person wording.
- Companies that are NOT the user's company must remain in third person. Never
  convert another company into "you" or "your".
- If USER COMPANY CONTEXT is empty or does not identify ownership confidently, do
  not guess. Preserve the perspective supported by the user's wording and answers.

USER-FACING ANALYTICS (CRITICAL)
- The final answer is for a business user, not a model developer or statistician.
  Lead with the business conclusion and decision-relevant result.
- TECHNICAL DETAILS EXPLICITLY REQUESTED: {technical_details_requested}
- When the flag above is NO, do NOT expose analytical machinery or diagnostic
  metrics merely because an upstream answer contains them. This rule OVERRIDES the
  general instruction to preserve every numerical value: technical/internal values
  may and should be omitted from the user-facing response when they were not asked for.
  This includes formulas,
  equations, regression slope/intercept, R2/R-squared/R², HHI/Herfindahl-Hirschman
  Index values, p-values, model-fit statistics, similarity scores, confidence
  thresholds, engine thresholds, calculation cut-offs, or internal formula names.
- Instead, keep the plain-language business conclusion already supported by THE
  ANSWERS: for example, "demand is increasing", "the trend is stable", "buyer
  concentration is high", or "this market is more diversified" when the upstream
  answer already states that conclusion.
- Do NOT invent a qualitative conclusion from a diagnostic number if THE ANSWERS
  do not already support that conclusion. Omit the diagnostic rather than creating
  a new interpretation.
- When TECHNICAL DETAILS EXPLICITLY REQUESTED is YES, show only the technical detail
  the user actually asked for. Do not dump every available formula or diagnostic.
- Never expose internal analytical-engine names, implementation rules, thresholds,
  SQL formulas or system methodology unless the user explicitly asks how the
  calculation/method works.

DATA COVERAGE PRESENTATION (CRITICAL)
- Data coverage is USER-FACING trust context, not an internal diagnostic to hide.
- If THE ANSWERS contain records_before_engine, records_after_engine,
  excluded_records and/or coverage_pct, ALWAYS surface the available coverage
  information once in concise business language, even when the user did not
  explicitly ask for coverage.
- NEVER expose those raw internal field names. Translate them into a short note
  such as: "Data coverage: This analysis used 82,000 of 100,000 matching trade
  records available for this scope (82.0%). The remaining 18,000 records were
  excluded by the analysis requirements, so interpret the result within that
  coverage."
- If coverage is 100%, say naturally that the analysis used all matching records
  available for the selected scope. Do not make it sound like the platform has
  complete knowledge of all real-world trade; it means 100% of the matching data
  available to this analysis.
- Adapt "this scope" when THE USER'S QUESTION clearly supports a more useful
  phrase such as "this country", "this trade corridor", "this product scope" or
  "this period". Do not invent a scope that is not supported.
- Preserve the supplied coverage percentage and record counts exactly. Do not
  recalculate or estimate them.
- Coverage should normally appear as one short final note or a small **Data
  coverage** line, not as a technical table or raw engine metadata dump.

WEB SOURCES / URL PRIVACY (CRITICAL)
- Web-grounded facts may be used in the answer, but the final UI response MUST NOT
  display URLs, hyperlinks, domains, citation lists, source lists, reference lists,
  footnote links, or raw source labels from THE ANSWERS.
- Do not add a Sources, References, Citations or Links section.
- If an upstream answer contains a URL or Markdown hyperlink, retain only the
  supported factual statement needed to answer the user; omit the link/source
  presentation itself.
- Never expose internal web-provider names or retrieval metadata merely because an
  upstream answer included them.

INTERNAL DATA STRUCTURE / SCHEMA CONFIDENTIALITY (CRITICAL)
- INTERNAL SCHEMA REQUEST DETECTED: {schema_request_detected}
- Never reveal the platform's internal database schema, table names, number of
  columns/fields, column names, raw storage identifiers, internal metadata fields,
  SQL identifiers, or storage structure.
- If the user asks what columns/fields exist, how many columns/fields exist, asks
  for the schema/table structure, or asks for an internal field inventory, do not
  reproduce any such inventory from THE ANSWERS.
- For a schema-only request, respond briefly that you can explain the trade-analysis
  capabilities available to the user, but cannot expose the internal data schema or
  field inventory. You MAY describe capabilities at a business level (for example
  products, counterparties, markets, ports, values, quantities and time-based trade
  analysis) without presenting them as database columns.
- For a mixed request, answer the allowed business-analysis parts and omit the
  schema/field-inventory portion, with at most one brief sentence saying the
  internal schema cannot be exposed.

HUMAN VOICE (CRITICAL)
- Write as if you are speaking directly to the user as a capable trade analyst,
  not as a machine reporting a system output.
- Use first-person phrasing naturally when appropriate: "I found...", "What
  stands out to me is...", "From the trade records I have...", "I'd focus on...".
- Address the user directly with "you" and "your" where the underlying answers
  support that relationship.
- Do NOT overuse "I" in every sentence. The tone should feel natural, calm and
  professional rather than scripted.
- Avoid robotic phrases such as "the system identified", "the database shows",
  "the provided data indicates", "the user has", or "according to the generated
  result" when a natural human phrasing can say the same thing.
- Do not pretend to have personal experiences, feelings, private knowledge or
  information beyond THE ANSWERS. First-person language refers only to the
  analysis and information supplied in this prompt.

PRESENTATION QUALITY (CRITICAL)
- Lead with the answer or the most decision-relevant finding. Do not make the
  user read a long setup before learning what matters.
- For complex answers, use a clear progression such as: key finding -> evidence
  -> implications/risks -> actions -> useful next checks. Adapt this structure
  to the actual question; do not force sections that are not needed.
- When several comparable entities and their values are already present in THE
  ANSWERS, a compact Markdown table is preferred when it improves scanning.
  Reformatting supplied facts into a table is allowed; recalculating or inventing
  values is not.
- Use bullets for distinct risks, findings or actions. Keep each bullet focused
  on one idea.
- Recommendations should be concrete and action-oriented when recommendations
  are present in THE ANSWERS. Do not create recommendations that upstream
  answers did not provide.
- Put caveats and limitations near the finding they qualify, or in a short
  final limitations/next-checks section when there are several of them.
- Do not dump long raw prose from one upstream answer when it can be organized
  more clearly without changing its meaning.

MARKDOWN AND TYPOGRAPHY HYGIENE (CRITICAL)
- Use standard Markdown only.
- Use only normal Unicode prose. NEVER use mathematical/fancy Unicode alphabets
  for ordinary words or sentences.
- Use ## for major section headings when sections are needed. Do not mix several
  decorative heading styles.
- Use **bold** only for short labels, entity names or key values that genuinely
  deserve emphasis. Never bold or italicize whole paragraphs.
- Do not use italic emphasis for ordinary prose. Do not use decorative ***text***.
- Use hyphen bullets (-) rather than asterisks as bullet markers.
- Never expose unmatched or stray Markdown markers such as *, **, ***, _, ` or #.
- Ensure normal spaces exist between words and after sentence-ending punctuation.
  If an upstream answer contains an obvious formatting defect such as words
  accidentally concatenated together, repair ONLY the spacing/punctuation needed
  for readability; do not alter entity names, HS codes, figures, dates or meaning.
- Put a blank line before and after headings, tables and lists so the frontend
  renders them cleanly.
- Do not place literal Markdown markers around values in a way that leaves the
  markers visible to the user.

FINAL SELF-CHECK (MANDATORY BEFORE RETURNING)
Before returning the response, silently verify all of the following:
1. Every factual claim is supported by THE ANSWERS.
2. No number, name, date, HS code or classification was changed.
3. The response reads like one person speaking to the user, not pasted modules.
4. No broken spacing or concatenated prose remains where it is obviously a
   formatting defect.
5. Markdown markers are balanced and standard.
6. Typography is consistent: normal text plus restrained standard Markdown only.
7. The most important answer is easy to find within the first few lines.
8. No unrequested R2/R-squared, HHI, regression coefficients, formulas, thresholds
   or other technical diagnostics are exposed.
9. If the question is about the user's own company, the answer uses second-person
   language (you/your) rather than treating that company as a third party.
10. If coverage metrics were supplied, they are shown once in business-friendly
    language with no raw coverage field names.
11. No URLs, hyperlinks, source/reference lists or citation links are visible.
12. No internal schema, table name, column count or column/field inventory is exposed.

FAILURES
- If one part could not be answered, mention that briefly in the appropriate
  section and continue with the remaining answered parts.
- Do not apologise at length.

THE USER'S QUESTION
{question}

USER COMPANY CONTEXT
{company_context}

THE ANSWERS
{answers}

Write the combined final answer now."""


# ───────────────────────── llm helper ─────────────────────────


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _clean_final_answer(text: str) -> str:
    """Normalize presentation artifacts without changing answer content.

    This is intentionally conservative. It does not rewrite facts, names, numbers,
    dates, HS codes or analytical conclusions; the LLM combine pass owns prose and
    structure. This helper only normalizes typography/whitespace and removes
    obviously unbalanced emphasis markers that can break the Streamlit rendering.
    """
    text = (text or "").strip()
    if not text:
        return ""

    # Compatibility-normalize decorative mathematical Unicode letters into normal
    # text while preserving ordinary Unicode names and punctuation.
    text = unicodedata.normalize("NFKC", text)

    # Remove invisible formatting characters that can make words render oddly.
    text = (
        text.replace("\u00a0", " ")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    # Strip trailing whitespace and keep paragraph spacing predictable.
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    # The final-answer style deliberately avoids italics. If a model still leaves
    # an unmatched bold/italic marker, remove that marker family rather than let
    # raw asterisks leak into the UI. Balanced standard bold remains untouched.
    if text.count("**") % 2:
        text = text.replace("**", "")
    single_star_view = text.replace("**", "")
    if single_star_view.count("*") % 2:
        text = text.replace("*", "")

    return text.strip()


_TECHNICAL_DETAIL_RE = re.compile(
    r"(?i)(?:\br\s*(?:\^?2|²)\b|r[- ]?squared|\bhhi\b|herfindahl|"
    r"regression\s+(?:slope|intercept)|\bslope\b|\bintercept\b|\bp[- ]?value\b|"
    r"formula|methodology|how\s+(?:is|was|do|does).*calculat|statistical\s+(?:fit|method|evidence)|"
    r"confidence\s+(?:score|threshold)|similarity\s+(?:score|threshold))"
)


def _technical_detail_requested(question: str) -> bool:
    """Return True only when the user explicitly asks for technical/statistical detail."""
    return bool(_TECHNICAL_DETAIL_RE.search((question or "").strip()))


def _remove_unrequested_technical_details(text: str, question: str) -> str:
    """Hide unrequested analytical mechanics while preserving user-facing coverage.

    Coverage is intentionally NOT removed here. The separate coverage formatter turns
    raw coverage fields into a business-facing trust note. This helper only suppresses
    statistical/implementation diagnostics such as R-squared, HHI values and regression
    coefficients when the user did not explicitly request them.
    """
    text = (text or "").strip()
    if not text:
        return ""

    technical_requested = _technical_detail_requested((question or "").strip())
    if technical_requested:
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    # Drop standalone sentences whose purpose is to report regression mechanics.
    # Sentence splitting only occurs after punctuation followed by whitespace, so
    # decimal points inside values such as 0.03 are not treated as boundaries.
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text):
        sentences = re.split(r"(?<=[.!?])\s+", paragraph.strip()) if paragraph.strip() else []
        kept = []
        for sentence in sentences:
            low = sentence.strip().lower()
            if low.startswith((
                "linear regression",
                "regression analysis",
                "the regression",
            )):
                continue
            kept.append(sentence.strip())
        if kept:
            paragraphs.append(" ".join(kept))
    text = "\n\n".join(paragraphs)

    # Remove diagnostic clauses while keeping the surrounding business statement.
    r2_token = r"(?:R\s*(?:\^?2|²)|R[- ]?squared)"
    hhi_token = r"(?:HHI|Herfindahl(?:-Hirschman)?(?:\s+Index)?)"
    numberish = r"[-+]?\d[\d,]*(?:\.\d+)?%?"

    text = re.sub(
        rf"(?i)\s*,?\s*(?:with|at|having|showing|based\s+on)?\s*"
        rf"(?:an?\s+)?{r2_token}\s*(?:of|=|:)?\s*{numberish}",
        "",
        text,
    )
    text = re.sub(
        rf"(?i)\s*,?\s*(?:with|at|having|showing|based\s+on)?\s*"
        rf"(?:an?\s+)?{hhi_token}\s*(?:of|=|:)?\s*{numberish}",
        "",
        text,
    )
    text = re.sub(
        rf"(?i)\s*,?\s*(?:monthly\s+)?(?:regression\s+)?"
        rf"(?:slope|intercept)\s*(?:of|=|:)?\s*(?:USD\s*)?{numberish}(?:\s*/\s*month)?",
        "",
        text,
    )

    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r",\s*([.!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_COVERAGE_FIELD_RE = re.compile(
    r"(?i)\b(records_before_engine|records_after_engine|excluded_records|coverage_pct)"
    r"\s*=\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.\d+)?%?)"
)


def _beautify_coverage_metrics(text: str) -> str:
    """Translate raw engine coverage fields into one business-facing coverage note."""
    text = (text or "").strip()
    if not text:
        return ""

    values = {}
    for key, value in _COVERAGE_FIELD_RE.findall(text):
        values[key.lower()] = value
    if not values:
        return text

    already_friendly = bool(re.search(r"(?i)\bdata\s+coverage\s*:", text))

    # Remove only the raw metadata portion. If an upstream answer placed the Note on
    # the same line as useful prose, keep the prose before the metadata starts.
    kept_lines = []
    raw_start_re = re.compile(
        r"(?i)(?:\bnote\s*:\s*)?(?:records_before_engine|records_after_engine|"
        r"excluded_records|coverage_pct)\s*="
    )
    for line in text.splitlines():
        match = raw_start_re.search(line)
        if match:
            prefix = line[:match.start()].rstrip(" ,;:-")
            if prefix:
                kept_lines.append(prefix)
            continue
        kept_lines.append(line)
    text = "\n".join(kept_lines).strip()
    if already_friendly:
        return text

    before = values.get("records_before_engine")
    after = values.get("records_after_engine")
    excluded = values.get("excluded_records")
    pct = values.get("coverage_pct")

    pct_num = None
    if pct:
        try:
            pct_num = float(pct.rstrip("%"))
        except ValueError:
            pct_num = None

    if before and after and pct:
        if pct_num is not None and pct_num >= 99.999 and (excluded in {None, "0", "0.0"}):
            note = (
                f"**Data coverage:** This analysis used all {after} matching trade records "
                f"available for the selected trade scope ({pct})."
            )
        else:
            note = (
                f"**Data coverage:** This analysis used {after} of {before} matching trade "
                f"records available for the selected trade scope ({pct})."
            )
            if excluded is not None:
                note += (
                    f" {excluded} records were excluded by the analysis requirements, so "
                    "interpret the result within that coverage."
                )
    elif pct:
        note = (
            f"**Data coverage:** The analysis covered {pct} of the matching trade data "
            "available for the selected trade scope."
        )
    elif before and after:
        note = (
            f"**Data coverage:** This analysis used {after} of {before} matching trade "
            "records available for the selected trade scope."
        )
    else:
        note = ""

    if not note:
        return text
    return (text + "\n\n" + note).strip() if text else note


_SOURCE_SECTION_RE = re.compile(
    r"(?i)^#{0,6}\s*(?:sources?|references?|citations?|links?)\s*:?[ \t]*$"
)
_SOURCE_LINE_RE = re.compile(
    r"(?i)^\s*(?:[-*]\s*)?(?:source|sources|reference|references|citation|citations)\s*:"
)


def _strip_web_sources_and_urls(text: str) -> str:
    """Remove source/link presentation while preserving the answer's factual prose."""
    text = (text or "").strip()
    if not text:
        return ""

    # Keep useful anchor text but remove its clickable destination.
    text = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)", r"\1", text, flags=re.I)
    text = re.sub(r"<https?://[^>]+>", "", text, flags=re.I)
    text = re.sub(r"(?i)\bhttps?://[^\s)>\]}]+", "", text)
    text = re.sub(r"(?i)\bwww\.[^\s)>\]}]+", "", text)
    text = re.sub(r"(?i)\s*\((?:source|sources|reference|references)\s*:[^)]*\)", "", text)

    out = []
    skipping_source_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if _SOURCE_SECTION_RE.match(stripped):
            skipping_source_section = True
            continue
        if skipping_source_section:
            if not stripped:
                continue
            if re.match(r"^#{1,6}\s+\S", stripped):
                skipping_source_section = False
            elif (
                stripped.startswith(("-", "*"))
                or _SOURCE_LINE_RE.match(stripped)
                or re.search(r"(?i)https?://|www\.", stripped)
            ):
                continue
            else:
                # A normal prose line means the source block has ended.
                skipping_source_section = False
        if _SOURCE_LINE_RE.match(stripped):
            continue
        if re.fullmatch(r"\s*(?:[-*]\s*)?(?:https?://\S+|www\.\S+)\s*", stripped, flags=re.I):
            continue
        out.append(line)

    text = "\n".join(out)
    # Defensive cleanup for source labels/bullets that may already have been collapsed
    # inline by an upstream formatter. Do not remove ordinary prose containing the
    # word "source"; only strip label-shaped trailing fragments.
    text = re.sub(
        r"(?im)^\s*(?:sources?|references?|citations?|links?)\s*:\s*(?:[-*]\s*)?$",
        "",
        text,
    )
    text = re.sub(
        r"(?i)\s+(?:sources?|references?|citations?|links?)\s*:\s*(?:[-*]\s*)?$",
        "",
        text,
    )
    text = re.sub(r"(?m)^\s*[-*]\s*$", "", text)
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_SCHEMA_REQUEST_RE = re.compile(
    r"(?is)(?:"
    r"\bhow\s+many\s+(?:database\s+|trade\s+data\s+)?(?:columns?|fields?)\b|"
    r"\b(?:what|which|list|show|display|give|tell\s+me)\b.{0,80}\b(?:columns?|fields?|schema)\b|"
    r"\b(?:database|table|data)\s+(?:schema|structure)\b|"
    r"\bcolumn\s+names?\b|\bfield\s+names?\b|\binternal\s+schema\b"
    r")"
)
_SCHEMA_BUSINESS_INTENT_RE = re.compile(
    r"(?i)\b(?:top|trend|growth|demand|competition|concentration|retention|churn|"
    r"buyers?|sellers?|suppliers?|markets?|ports?|price|seasonality|shipments?|"
    r"imports?|exports?|compare|share|dependency|reactivat|momentum|acceleration)\b"
)


def _schema_request_detected(question: str) -> bool:
    return bool(_SCHEMA_REQUEST_RE.search((question or "").strip()))


def _schema_only_request(question: str) -> bool:
    q = (question or "").strip()
    return _schema_request_detected(q) and not bool(_SCHEMA_BUSINESS_INTENT_RE.search(q))


def _schema_safe_response() -> str:
    return (
        "I can help you analyze the trade information available to you—such as products, "
        "counterparties, markets, ports, values, quantities, and time-based trade patterns—"
        "but I can’t expose the platform’s internal database schema, column/field inventory, "
        "table structure, or storage identifiers."
    )


_INTERNAL_FIELD_LABELS = {
    "imports_exports": "trade records",
    "shipping_date": "shipping date",
    "shipment_value": "shipment value",
    "product_description_norm": "product description",
    "product_description": "product description",
    "buyer_country": "buyer country",
    "buyer_state": "buyer state",
    "buyer_city": "buyer city",
    "buyer_address": "buyer address",
    "buyer_company": "buyer company",
    "port_of_destination": "destination port",
    "seller_country": "seller country",
    "seller_state": "seller state",
    "seller_city": "seller city",
    "seller_address": "seller address",
    "seller_company": "seller company",
    "port_of_origin": "origin port",
    "decl_no": "shipment reference",
    "hs_code": "HS code",
    "coverage_pct": "data coverage",
    "records_before_engine": "matching records before analysis",
    "records_after_engine": "records used in the analysis",
    "excluded_records": "excluded records",
}


def _humanize_internal_field_names(text: str) -> str:
    text = text or ""
    for field, label in sorted(_INTERNAL_FIELD_LABELS.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"(?i)\b{re.escape(field)}\b", label, text)
    return text


def _finalize_user_visible_answer(text: str, question: str) -> str:
    """Final deterministic UI guard shared by combine and fallback paths."""
    if _schema_only_request(question):
        return _schema_safe_response()

    # Strip source/link presentation first. Later prose-cleanup passes may join lines,
    # which can otherwise turn a clean source block into an inline ``Sources: -``
    # fragment that is harder to recognize safely. Grounding remains available to
    # upstream agents; only the final user-facing presentation is affected here.
    text = _strip_web_sources_and_urls(text)
    text = _beautify_coverage_metrics(text)
    text = _remove_unrequested_technical_details(text, question)
    text = _humanize_internal_field_names(text)
    return _clean_final_answer(text)


def _company_context_from_memory(memory: dict, max_chars: int = 6000) -> str:
    """Return only profile-like memory that can identify the user's own company.

    This intentionally avoids dumping the whole conversation/session memory into the
    combine prompt. The returned context is used only for perspective (you/your vs
    third person), never as an additional source of trade facts.
    """
    if not isinstance(memory, dict) or not memory:
        return "(no company profile context available)"

    preferred_keys = (
        "company_profile",
        "user_company_profile",
        "user_company",
        "company_name",
        "company",
        "organization",
        "organisation",
        "business_profile",
        "user_product_info",
        "persona",
        "profile",
    )

    selected = []
    seen = set()

    def add_item(key, value):
        if value is None or value == "":
            return
        marker = str(key).lower()
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(value, (dict, list, tuple)):
            try:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                rendered = str(value)
        else:
            rendered = str(value)
        rendered = rendered.strip()
        if rendered:
            selected.append(f"{key}: {rendered}")

    for key in preferred_keys:
        if key in memory:
            add_item(key, memory.get(key))

    # Support differently named profile fields without exposing unrelated memory.
    profile_tokens = ("company", "organisation", "organization", "business", "profile")
    for key, value in memory.items():
        key_text = str(key).lower()
        if any(token in key_text for token in profile_tokens):
            add_item(key, value)

    if not selected:
        return "(no company profile context available)"

    text = "\n".join(selected)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def _chat(prompt: str, max_tokens: int, as_json: bool = False) -> str:
    """Call the connector model and never accept a token-truncated completion as complete."""
    token_budget = max(1, int(max_tokens))
    while True:
        body = {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": token_budget,
            "messages": [{"role": "user", "content": prompt}],
        }
        if as_json:
            body["response_format"] = {"type": "json_object"}

        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
            json=body,
            timeout=90,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(payload["error"].get("message", "OpenRouter error"))

        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Unexpected OpenRouter response: {str(payload)[:500]}") from exc

        finish_reason = str(choice.get("finish_reason") or "").lower()
        content = _strip_fences(choice.get("message", {}).get("content") or "")

        if finish_reason == "length":
            if token_budget < CONNECTOR_RETRY_MAX_TOKENS:
                next_budget = min(CONNECTOR_RETRY_MAX_TOKENS, max(token_budget * 2, token_budget + 1000))
                _log("model output hit token limit; retrying", f"{token_budget} -> {next_budget}")
                token_budget = next_budget
                continue
            raise RuntimeError(
                f"Connector model output was truncated at max_tokens={token_budget}; refusing to return a partial answer."
            )
        if not content:
            raise RuntimeError(f"Connector model returned empty content (finish_reason={finish_reason or 'unknown'}).")
        return content


def _rewrite_looks_malformed(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return True
    if s.count("(") != s.count(")") or s.count("[") != s.count("]"):
        return True
    if s.endswith("...") or s.endswith("…"):
        return True
    if re.search(r"(?i)(?:\.\.\.|…)\s*wait\b|\bwait\s*,\s*(?:actually|no|sorry)?", s):
        return True
    return False


def _single_hs_from_dependency_answers(dependency_answers: list[str]) -> str:
    """Return one unambiguous HS code carried by dependency answers.

    This is intentionally conservative: dependent TRADE rewrites may replace generic
    references such as "the product identified in q1" with a stable HS identifier,
    but only when the dependency evidence contains exactly one distinct HS code.
    """
    codes = []
    for answer in dependency_answers or []:
        for match in re.finditer(r"(?i)\bHS(?:\s+code)?\s*[:#-]?\s*(\d{4,18})\b", str(answer or "")):
            code = match.group(1)
            if code not in codes:
                codes.append(code)
    return codes[0] if len(codes) == 1 else ""


def _deterministic_dependency_trade_rewrite(question: str, dependency_answers: list[str]) -> str:
    """Resolve generic product back-references to a single dependency HS code.

    This avoids asking the rewrite LLM to invent or embellish a product description when
    the previous trade step already returned the authoritative HS scope.  It deliberately
    handles only generic dependency phrases; concrete user-specified product wording is
    left untouched.
    """
    hs_code = _single_hs_from_dependency_answers(dependency_answers)
    if not hs_code:
        return ""

    q = str(question or "")
    patterns = [
        r"(?i)\bthe\s+(?:leading|strongest|top|largest|biggest|highest(?:[- ]value)?)\s+(?:export\s+|import\s+)?product\s+(?:identified|found|selected)\s+in\s+q\d+\b",
        r"(?i)\bthe\s+product\s+(?:identified|found|selected)\s+in\s+q\d+\b",
        r"(?i)\bproduct\s+(?:identified|found|selected)\s+in\s+q\d+\b",
    ]
    rewritten = q
    changed = False
    for pattern in patterns:
        rewritten, n = re.subn(pattern, f"HS code {hs_code}", rewritten)
        changed = changed or bool(n)
    return re.sub(r"\s+", " ", rewritten).strip() if changed else ""


def _log(label: str, value="") -> None:
    if PRINT_CONNECTOR:
        print(f"--- CONNECTOR {label}" + (f" | {str(value)[:]}" if value else ""))


# ───────────────────────── handler compatibility ─────────────────────────


def _call_trade_handler(fn, question: str, node: dict):
    """
    Call the TRADE handler with the full node when the handler supports it.

    New contract:
        trade(question, node)

    Legacy contract:
        trade(question)

    This keeps the current UI/CLI working while the new Trade Mini Connector
    is wired in. Once both callers use the two-argument contract, this helper
    can remain harmlessly in place.
    """
    if fn is None:
        return {
            "text": "",
            "ok": False,
            "pending": False,
            "needs_answer": False,
        }

    try:
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())

        accepts_varargs = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL
            for p in params
        )

        positional = [
            p
            for p in params
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]

        if accepts_varargs or len(positional) >= 2:
            return fn(question, node)

    except (TypeError, ValueError):
        # If the callable cannot be introspected, preserve the legacy path.
        pass

    return fn(question)


# ───────────────────────── the run ─────────────────────────


class FlowRun:
    """One pass over a tagged flow. Resumable at the node that paused."""

    def __init__(self, flow: dict, memory: dict, handlers: dict,
                 original_question: str = ""):
        self.flow = flow or {}
        self.memory = memory or {}
        self.handlers = handlers or {}
        self.original_question = (original_question or "").strip()
        self.questions = {q["id"]: q for q in self.flow.get("questions", [])}
        self.queue = [qid for level in self.flow.get("order", []) for qid in level]
        self.multi = len(self.questions) > 1
        self.answers = {}          # qid -> text shown to the user
        self.asked = {}            # qid -> the question actually sent
        self.failed = []           # qids that produced nothing
        self.clarified = set()     # qids that already paused for a clarification
        self.position = 0          # index into self.queue
        self.stopped_reason = ""

    # ---- public -------------------------------------------------

    def start(self) -> dict:
        return self._walk()

    def resume(self) -> dict:
        """Call after an HS domain pick. Retries the same node from the top."""
        return self._walk()

    def supply_answer(self, text: str) -> dict:
        """Record the paused node's answer and carry on.

        Call this after a clarification: the caller sent the user's reply to the
        engine, got the real answer back, and passes that text in here. An empty
        text means the node produced nothing, which follows the usual
        empty-node rules.
        """
        if self.position >= len(self.queue):
            return self._done()

        qid = self.queue[self.position]
        text = (text or "").strip()

        if text:
            self.answers[qid] = text
            _log(f"{qid} answered after a clarification")
        else:
            self.failed.append(qid)
            blocked = dependants_of(self.flow, qid)
            if blocked:
                self.stopped_reason = (
                    "Nothing came back for that part, and the rest of your "
                    "question depends on it, so I stopped there."
                )
                _log(f"{qid} empty after a clarification and {blocked} depend on it")
                return self._stopped()
            _log(f"{qid} empty after a clarification, nothing depends on it")

        self.position += 1
        return self._walk()

    # ---- the walk -----------------------------------------------

    def _walk(self) -> dict:
        while self.position < len(self.queue):
            qid = self.queue[self.position]
            node = self.questions[qid]

            question = self._prepare(node)
            self.asked[qid] = question

            outcome = self._run_node(node, question)

            if outcome["pending"]:
                _log(f"{qid} paused for an HS domain pick")
                return {"status": "needs_hs_pick", "qid": qid,
                        "question": question, "answer": "",
                        "answers": dict(self.answers)}

            if outcome.get("needs_answer"):
                # one clarification per node; a second one is treated as the answer
                self.clarified.add(qid)
                _log(f"{qid} paused for a clarification")
                return {"status": "needs_answer", "qid": qid,
                        "question": question, "answer": "",
                        "answers": dict(self.answers)}

            if outcome.get("hard_failure"):
                self.failed.append(qid)
                blocked = dependants_of(self.flow, qid)
                failure_text = (outcome.get("text") or "").strip()
                if blocked:
                    self.stopped_reason = failure_text or (
                        "The trade-data step could not be completed, and the rest of your "
                        "question depends on it, so I stopped there."
                    )
                    _log(f"{qid} trade failure and {blocked} depend on it — stopping")
                    return self._stopped()
                if failure_text:
                    self.answers[qid] = failure_text
                _log(f"{qid} trade failure, nothing depends on it — keeping the failure message")
            elif outcome["text"]:
                self.answers[qid] = outcome["text"]
            else:
                self.failed.append(qid)
                blocked = dependants_of(self.flow, qid)
                if blocked:
                    self.stopped_reason = (
                        f"Nothing came back for the first part, and the rest of "
                        f"your question depends on it, so I stopped there."
                    )
                    _log(f"{qid} empty and {blocked} depend on it — stopping")
                    return self._stopped()
                _log(f"{qid} empty, nothing depends on it — carrying on")

            self.position += 1

        return self._done()

    # ---- one node -----------------------------------------------

    def _prepare(self, node: dict) -> str:
        """Substitute earlier answers into a dependent question."""
        deps = [d for d in node.get("depends_on", []) if d in self.answers]
        if not deps:
            return node["text"]

        dependency_parts = []
        for d in deps:
            answer = str(self.answers[d] or "")
            if len(answer) > CONNECTOR_DEPENDENCY_HARD_LIMIT:
                raise RuntimeError(
                    f"Dependency answer for {d} exceeds the safe rewrite context limit; refusing to silently truncate it."
                )
            dependency_parts.append(
                f"{d} asked: {self.asked.get(d, '')}\n{d} answered: {answer}"
            )
        answers = "\n\n".join(dependency_parts)

        # For dependent TRADE nodes, a single HS code from earlier trade evidence is a
        # safer scope key than a generated descriptive label.  Resolve only generic
        # references deterministically; otherwise fall through to the normal rewrite LLM.
        if str(node.get("route") or "").upper() == "TRADE":
            deterministic = _deterministic_dependency_trade_rewrite(
                node.get("text", ""),
                [str(self.answers[d] or "") for d in deps],
            )
            if deterministic:
                _log(f"{node['id']} rewritten deterministically", deterministic)
                return deterministic

        prompt = PROMPT_REWRITE.format(answers=answers, question=node["text"])

        try:
            parsed = json.loads(_chat(prompt, REWRITE_MAX_TOKENS, as_json=True))
            rewritten = str(parsed.get("question") or "").strip()
            if _rewrite_looks_malformed(rewritten):
                repair_prompt = (
                    prompt
                    + "\n\nThe previous rewrite was incomplete or malformed. Rewrite again from scratch. "
                      "Do not self-correct with words like 'wait' or ellipses; keep every required product/HS pair intact; "
                      "return one complete self-contained question only in the required JSON.\n"
                    + f"PREVIOUS BAD REWRITE: {rewritten}"
                )
                parsed = json.loads(_chat(
                    repair_prompt,
                    max(REWRITE_MAX_TOKENS * 2, 2400),
                    as_json=True,
                ))
                rewritten = str(parsed.get("question") or "").strip()
            if rewritten and not _rewrite_looks_malformed(rewritten):
                _log(f"{node['id']} rewritten", rewritten)
                return rewritten
            raise RuntimeError("rewrite remained incomplete/malformed after retry")
        except Exception as exc:
            print(f"connector: rewrite of {node['id']} failed:",
                  type(exc).__name__, exc)

        return node["text"]

    def _run_node(self, node: dict, question: str) -> dict:
        """Dispatch one node. Returns {'text', 'pending', 'needs_answer'}."""
        route = node.get("route", "TRADE")
        _log(f"{node['id']} {route}", question)

        try:
            if route == "PERSONAL":
                fn = self.handlers.get("personal")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False, "needs_answer": False}

            if route == "WEB" and self.multi:
                # grounding comes from the TRADE nodes this one depends on
                fn = self.handlers.get("web")
                return {"text": (fn(question) if fn else "").strip(),
                        "pending": False, "needs_answer": False}

            # TRADE, and a single-node WEB flow: the engine runs first.
            #
            # The full node is now made available to the trade handler so the
            # new Trade Mini Connector can read node["trade_decision"].
            # Legacy one-argument handlers remain supported.
            fn = self.handlers.get("trade")
            result = _call_trade_handler(fn, question, node)
            if result.get("pending"):
                return {"text": "", "pending": True, "needs_answer": False}

            # the engine asked a question back. Pause once per node; if it asks
            # again after the user answered, take the question as the answer and
            # let the flow finish rather than looping.
            if result.get("needs_answer") and node["id"] not in self.clarified:
                return {"text": "", "pending": False, "needs_answer": True}

            text = (result.get("text") or "").strip()

            # WEB routes may intentionally combine the trade engine with public web.
            # TRADE routes must remain grounded in the trade database: a DB failure may
            # not silently switch evidence source and contaminate dependent questions.
            if route == "WEB":
                web = self.handlers.get("web")
                extra = (web(question) if web else "").strip()
                if extra:
                    text = f"{text}\n\n{extra}".strip() if text else extra

            # Any completed TRADE result with ok=False is a hard evidence failure.
            # Pending/clarification states were already handled above.  Do not treat a
            # user-facing error sentence as a successful dependency and continue into WEB
            # or another TRADE node.
            hard_failure = bool(route == "TRADE" and not result.get("ok"))

            return {"text": text, "pending": False, "needs_answer": False,
                    "hard_failure": hard_failure}

        except Exception as exc:
            print(f"connector: {node['id']} handler failed:",
                  type(exc).__name__, exc)
            return {"text": "", "pending": False, "needs_answer": False}

    # ---- finishing ----------------------------------------------

    def _combine(self) -> str:
        if not self.answers:
            return "Nothing came back for that. Try rephrasing it."

        # Always run the final presentation pass, even for a single answered
        # node. Previously single-node flows returned the raw engine text directly,
        # which allowed inconsistent tone/Markdown/spacing to reach the UI.
        blocks = []
        for qid in self.queue:
            if qid in self.answers:
                presented_answer = _beautify_coverage_metrics(self.answers[qid])
                blocks.append(f"QUESTION: {self.asked.get(qid, '')}\n"
                              f"ANSWER: {presented_answer}")
            elif qid in self.failed:
                blocks.append(f"QUESTION: {self.asked.get(qid, '')}\n"
                              f"ANSWER: (nothing came back)")

        original_question = self.original_question or "the user's question"
        prompt = PROMPT_COMBINE.format(
            question=original_question,
            company_context=_company_context_from_memory(self.memory),
            technical_details_requested=(
                "YES" if _technical_detail_requested(original_question) else "NO"
            ),
            schema_request_detected=(
                "YES" if _schema_request_detected(original_question) else "NO"
            ),
            answers="\n\n".join(blocks),
        )
        try:
            combined = _chat(prompt, COMBINE_MAX_TOKENS)
            if combined:
                return _finalize_user_visible_answer(combined, original_question)
        except Exception as exc:
            print("connector: combine failed:", type(exc).__name__, exc)

        # Fall back to the parts rather than losing the work. Even on fallback,
        # normalize only presentation artifacts; never rewrite the underlying facts.
        fallback = "\n\n".join(
            self.answers[qid]
            for qid in self.queue
            if qid in self.answers
        )
        return _finalize_user_visible_answer(fallback, original_question)

    def _done(self) -> dict:
        return {
            "status": "done",
            "answer": self._combine(),
            "answers": dict(self.answers),
            "asked": dict(self.asked),
            "failed": list(self.failed),
            "flow": self.flow,
        }

    def _stopped(self) -> dict:
        parts = [self.answers[qid] for qid in self.queue if qid in self.answers]
        answer = "\n\n".join(parts + [self.stopped_reason]) if parts \
            else self.stopped_reason
        return {
            "status": "stopped",
            "answer": _finalize_user_visible_answer(
                answer, self.original_question or "the user's question"
            ),
            "answers": dict(self.answers),
            "asked": dict(self.asked),
            "failed": list(self.failed),
            "flow": self.flow,
        }


# ───────────────────────── run directly ─────────────────────────
#
#   python -m summarizer.connector
#
# Stub handlers, so the real classifier, builder, rewrite and combine all run
# without touching Snowflake or sonar. Watch the rewritten questions, and the
# third query exercises the clarification pause.

if __name__ == "__main__":
    from summarizer.question_flow_builder import build_flow
    from summarizer.classifier import tag_flow

    MEMORY = {
        "user_product_info": (
            "ABC EXPORTS, Tirupur. Exports cotton men's t-shirts (HS 610910) and "
            "pullovers (HS 611020) to the USA, Spain and Hong Kong. Imports "
            "polyester fabric (HS 540752) from Ningbo, China. Ports: Chennai Sea, "
            "Nhava Sheva."
        ),
        "old_session_summary": "",
        "current_session_queries": [],
        "persona": "",
    }

    def stub_personal(q):
        print(f"      [personal] <- {q}")
        return f"(from the profile: {q})"

    def stub_trade(q, node=None):
        print(f"      [trade]    <- {q}")
        return {"text": "The user's largest import is polyester fabric, HS 540752, "
                        "from Ningbo, China.",
                "ok": True, "pending": False, "needs_answer": False}

    def stub_web(q):
        print(f"      [web]      <- {q}")
        return f"(from the web: {q})"

    HANDLERS = {"personal": stub_personal, "trade": stub_trade, "web": stub_web}

    QUERIES = [
        "Identify the product the user imports most and state which it is. Then "
        "recommend adjacent product categories for that product, and transhipment "
        "routes and destination markets worth entering.",
        "Who are the top buyers of HS 610910 in the last 24 months?",
    ]

    for query in QUERIES:
        print(f"\n{'='*70}\n{query[:90]}\n{'='*70}")
        flow = tag_flow(build_flow(query, MEMORY), MEMORY)
        run = FlowRun(flow, MEMORY, HANDLERS, original_question=query)
        out = run.start()
        print(f"\n  status: {out['status']}")
        print(f"\n  ANSWER:\n{out['answer']}\n")

    # ---- the clarification pause, with a trade stub that asks once ----
    print(f"\n{'='*70}\nCLARIFICATION PAUSE\n{'='*70}")

    state = {"asked": False}

    def stub_trade_asks(q, node=None):
        print(f"      [trade]    <- {q}")
        if not state["asked"]:
            state["asked"] = True
            print("      [trade]    -> asks a clarifying question")
            return {"text": "", "ok": False, "pending": False, "needs_answer": True}
        return {"text": "Germany, Netherlands and Canada show the strongest demand.",
                "ok": True, "pending": False, "needs_answer": False}

    flow = tag_flow(build_flow("Show me the best markets for cotton", MEMORY), MEMORY)
    run = FlowRun(flow, MEMORY,
                  {**HANDLERS, "trade": stub_trade_asks},
                  original_question="Show me the best markets for cotton")

    out = run.start()
    print(f"\n  status: {out['status']}  (expected needs_answer)")

    if out["status"] == "needs_answer":
        # the caller would show options, take the reply, send it to the engine,
        # and pass the engine's real answer back in
        out = run.supply_answer(
            "Germany, Netherlands and Canada show the strongest demand."
        )

    print(f"  status: {out['status']}  (expected done)")
    print(f"\n  ANSWER:\n{out['answer']}\n")