# exira
Building an end-to-end trade chatbot

## Working flow 
                         you type something
                                 │
        ┌────────────────────────┼────────────────────────┐
   S.selecting?            from a button?              typed
   (HS pick)               (already standalone)           │
        │                        │                ┌───────▼─────────┐
        │                  skip everything        │  1. ANALYZER    │
        │                  force TRADE            │  resolve_query  │
        │                        │                └───────┬─────────┘
        │                        │           blocked ─────┤
        │                        │           (all parts   ├──► message, STOP
        │                        │            off-topic)  │
        │                        │           dropped_note ┤──► print above answer
        │                        │           (some parts) │
        │                        │                        ▼
        │                        │                ┌─────────────────┐
        │                        │                │ 2. CLASSIFIER   │   ← the gate
        │                        │                │  classify_query │
        │                        │                └───────┬─────────┘
        │                        │                        │
        │                        │        PERSONAL and not looks_multi?
        │                        │                        │
        │                        │             yes ───────┴─────── no
        │                        │              │                   │
        │                        │   ┌──────────▼────────┐  ┌───────▼─────────┐
        │                        │   │ answer_from_memory│  │ 3. FLOW BUILDER │
        │                        │   │  no builder       │  │  build_flow     │
        │                        │   │  no connector     │  └───────┬─────────┘
        │                        │   │  no engine        │  q1, q2, q3 + depends_on
        │                        │   └──────────┬────────┘          │
        │                        │              │          ┌────────▼─────────┐
        │                        │              │          │ 4. TAG EACH NODE │
        │                        │              │          │  tag_flow        │
        │                        │              │          └────────┬─────────┘
        │                        │              │       each node P / T / W
        │                        │              │                   │
        │                        │              │          ┌────────▼─────────┐
        │                        │              │          │ 5. CONNECTOR     │
        │                        │              │          │  FlowRun.start() │
        │                        │              │          └────────┬─────────┘
        │                        │              │                   │
        │                        │              │   ╔═══════════════▼═══════════════╗
        │                        │              │   ║  for each level in order      ║
        │                        │              │   ║                               ║
        │                        │              │   ║  has dependencies?            ║
        │                        │              │   ║   └─► REWRITE: substitute      ║
        │                        │              │   ║       every parent's answer   ║
        │                        │              │   ║                               ║
        │                        │              │   ║  dispatch by tag:             ║
        │                        │              │   ║   PERSONAL → memory  (no      ║
        │                        │              │   ║               engine, ever)   ║
        │                        │              │   ║   TRADE    → engine           ║
        │                        │              │   ║   WEB      → sonar            ║
        │                        │              │   ║          (+ engine if the     ║
        │                        │              │   ║           flow is one node)   ║
        │                        │              │   ║                               ║
        │                        │              │   ║  empty + dependants? → STOP   ║
        │                        │              │   ║  empty + none?       → skip   ║
        │                        │              │   ║  engine wants HS?    → PAUSE ─╫─┐
        │                        │              │   ╚═══════════════╤═══════════════╝ │
        │                        │              │        all nodes done              │
        │                        │              │          ┌────────▼─────────┐      │
        │                        │              │          │  6. COMBINE      │      │
        │                        │              │          │  one answer      │      │
        │                        │              │          │  (skipped if one │      │
        │                        │              │          │   node answered) │      │
        │                        │              │          └────────┬─────────┘      │
        │                        ▼              │                   │                │
        │                  ENGINE (direct)      │                   │                │
        │                  confirm · A/B/C      │                   │                │
        │                  maybe_web fallback   │                   │                │
        │                        │              │                   │                │
        │                        └──────────────┴─────────┬─────────┘                │
        │                                                 ▼                          │
        │                                        show the answer                      │
        │                                                 │                          │
        │              record_query(typed + resolved) · persona · sync_scope          │
        │                                                 │                          │
        │                         3 followups → suggestion buttons ──┐               │
        │                                                            │               │
        └────────────────────────────────────────────────────────────┼───────────────┘
         the pick resumes the paused node, then the walk continues    │
                                                                      │
                                           (next turn can bypass) ────┘

python -m src.run_pipeline 
streamlit run app.py 