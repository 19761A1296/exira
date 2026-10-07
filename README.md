# exira
Building an end-to-end trade chatbot

# flow diagram
                         you type something
                                 │
     ┌───────────────────┬───────┴───────┬────────────────────┐
S.pending_clarify?   S.selecting?   from a button?         typed
(query ambiguity)    (HS pick)      (standalone)             │
     │                   │                │          ┌───────▼─────────┐
 resume the         skip everything  skip everything │  1. ANALYZER    │
 paused node        send the pick    force TRADE     │  resolve_query  │
     │                   │                │          └───────┬─────────┘
     │                   │                │     blocked ─────┤
     │                   │                │     (all parts   ├──► message, STOP
     │                   │                │      off-topic)  │
     │                   │                │     dropped_note ┤──► show above answer
     │                   │                │     (some parts) │
     │                   │                │                  │
     │                   │                │     ANSWER_RECALL┤──╗
     │                   │                │                  │  ║ summarise_answers
     │                   │                │                  │  ║ scope: last | all
     │                   │                │                  │  ║ reads S.answers
     │                   │                │                  │  ║ no builder
     │                   │                │                  │  ║ no connector
     │                   │                │                  │  ║ no engine
     │                   │                │                  │  ║ no sonar
     │                   │                │                  │  ║ NOT captured
     │                   │                │                  │  ╚══► answer, STOP
     │                   │                │                  ▼
     │                   │                │          ┌─────────────────┐
     │                   │                │          │ 2. FLOW BUILDER │
     │                   │                │          │  build_flow     │
     │                   │                │          └───────┬─────────┘
     │                   │                │          q1, q2, q3 + depends_on
     │                   │                │                  │
     │                   │                │          ┌───────▼─────────┐
     │                   │                │          │ 3. TAG EACH NODE│
     │                   │                │          │  tag_flow       │
     │                   │                │          └───────┬─────────┘
     │                   │                │       each node P / T / W
     │                   │                │                  │
     │                   │                │          ┌───────▼─────────┐
     │                   │                │          │ 4. CONNECTOR    │
     │                   │                │          │  FlowRun.start()│
     │                   │                │          └───────┬─────────┘
     │                   │                │                  │
     │                   │                │  ╔═══════════════▼═══════════════╗
     │                   │                │  ║  for each level in order      ║
     │                   │                │  ║                               ║
     │                   │                │  ║  has dependencies?            ║
     │                   │                │  ║   └─► REWRITE: substitute     ║
     │                   │                │  ║       every parent's answer   ║
     │                   │                │  ║                               ║
     │                   │                │  ║  dispatch by tag:             ║
     │                   │                │  ║   PERSONAL → memory  (no      ║
     │                   │                │  ║               engine, ever)   ║
     │                   │                │  ║   TRADE    → engine           ║
     │                   │                │  ║   WEB      → sonar            ║
     │                   │                │  ║          (+ engine if the     ║
     │                   │                │  ║           flow is one node)   ║
     │                   │                │  ║                               ║
     │                   │                │  ║  empty + dependants? → STOP   ║
     │                   │                │  ║  empty + none?       → skip   ║
     │                   │                │  ║  engine wants HS?    → PAUSE ─╫─┐
     │                   │                │  ║  engine asks a       → PAUSE ─╫─┼┐
     │                   │                │  ║   question (once per node)    ║ ││
     │                   │                │  ╚═══════════════╤═══════════════╝ ││
     │                   │                │       all nodes done              ││
     │                   │                │          ┌───────▼─────────┐      ││
     │                   │                │          │  5. COMBINE     │      ││
     │                   │                │          │  one answer     │      ││
     │                   │                ▼          │ (skipped if one │      ││
     │                   │          ENGINE (direct)  │  node answered) │      ││
     │                   │          confirm · A/B/C  └───────┬─────────┘      ││
     │                   │          maybe_web                │                ││
     │                   │                │                  │                ││
     └───────────────────┴────────────────┴────────┬─────────┘                ││
                                                   ▼                          ││
                                          show the answer                      ││
                                                   │                          ││
                     ┌─────────────────────────────┤                          ││
                     ▼                             ▼                          ││
        ╔═══════════════════════╗   record_query · persona · sync_scope       ││
        ║ CAPTURE               ║                  │                          ││
        ║ S.answers.append(     ║   3 followups → suggestion buttons ──┐      ││
        ║   question, answer)   ║                                      │      ││
        ║ cap 20, newest kept   ║                                      │      ││
        ║                       ║                                      │      ││
        ║ 3 capture points:     ║                                      │      ││
        ║  finish_flow          ║                                      │      ││
        ║  run_confirm          ║                                      │      ││
        ║  from_list exit       ║                                      │      ││
        ║         ↓             ║                                      │      ││
        ║ SIDEBAR               ║                                      │      ││
        ║ Developer context     ║                                      │      ││
        ║ 💬 Captured responses ║                                      │      ││
        ║   question heading    ║                                      │      ││
        ║   300 chars + ...     ║                                      │      ││
        ╚═══════════════════════╝                                      │      ││
                                                                       │      ││
                  the HS pick resumes the paused node ────────────────────────┘│
                  the clarification answer resumes it ─────────────────────────┘
                                                (next turn can bypass) ─┘


Commands:
python -m src.run_pipeline 
streamlit run app.py 
