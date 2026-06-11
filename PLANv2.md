# Plan: Humm PoC — SQLite → DuckDB + litellm agent + Gradio app

## Context

The Humm PoC is a consent-aware healthcare data governance platform demonstrating six DPDP
compliance scenarios. This plan covers three sequential workstreams:
1. Migrate storage from SQLite → DuckDB
2. Add a litellm-powered LLM agent demo with `--agent` CLI flag
3. Add a Gradio UI so stakeholders can explore governance controls interactively

---

## Dependencies

```bash
uv add duckdb litellm python-dotenv gradio
```

`pyproject.toml` requires `requires-python = ">=3.13"` (Python 3.14 breaks `docstring-parser`
used by older LLM libs). API key in `.env` (gitignored):
```
GROQ_API_KEY="your_free_groq_key"
```

---

## Phase 0 — Verify existing scripts (SQLite baseline) ✅

```bash
uv run python src/generate_data.py
uv run python src/pipeline.py
uv run python src/demo.py   # all 6 scenarios must pass before migrating
```

---

## Phase 1 — Create `src/db.py` ✅

Shared DuckDB connection factory with `_Row`/`_Result`/`_Conn` wrapper providing SQLite-compatible
dict-key access (`row["col"]`) and integer-index access (`fetchone()[0]`) throughout, without
touching every callsite. Exports `get_conn()`, `DB_PATH`, `ROOT`.

---

## Phase 2 — Migrate `src/pipeline.py` ✅

- Import: `from db import get_conn, DB_PATH, ROOT`
- Gold view: `strftime('%Y','now')` → `year(current_date)`, `substr(birth_date,1,4)` → `birth_date[1:4]`
- GROUP BY: added `p.gender, p.birth_date, c.display` (DuckDB enforces strict GROUP BY)

---

## Phase 3 — Migrate `src/access.py` ✅

- Import: `from db import get_conn, DB_PATH`
- `propagate_erasure`: COUNT-then-DELETE (DuckDB's `.rowcount` returns -1 for DELETE)
- `get_conn` re-exported so `demo.py`'s `from access import get_conn` still works

---

## Phase 4 — Migrate `src/retrieval.py` ✅

- Import: `from db import get_conn, DB_PATH`
- `AUTOINCREMENT` → DuckDB sequence (`DROP SEQUENCE IF EXISTS` + `CREATE SEQUENCE` + `DEFAULT nextval()`)
- Chunk text: `round(o['value'], 2)` prevents float32→float64 precision artifacts

---

## Phase 5 — Create `src/agents.py` (litellm agent) ✅

LLM agent with `--agent` CLI flag. Agent identity + purpose baked into tool closures — the
model cannot override them. Uses a manual tool loop (not litellm's built-in) so provider-specific
fields like `reasoning_content` are never echoed back into message history.

**Why litellm (not aisuite):** aisuite's Groq provider echoes `reasoning_content` back into
subsequent turn messages, which Groq then rejects. litellm is actively maintained and avoids this.

Default model: `groq/llama-3.3-70b-versatile`

```bash
uv run python src/agents.py --agent clinical-summary-agent
uv run python src/agents.py --agent analytics-agent --question "What is the patient's name?"
uv run python src/agents.py --help
```

Verified governance: clinical agent sees real PHI; analytics agent sees `***MASKED***`.

---

## Phase 6 — Verify full DuckDB pipeline ✅

```bash
uv run python src/generate_data.py && uv run python src/pipeline.py && uv run python src/demo.py
```

All 6 scenarios pass on DuckDB.

---

## Phase 7 — Create `src/app.py` (Gradio UI)

### Step 7a — Add dependency
```bash
uv add gradio
```

### Step 7b — Layout (`gr.Blocks`, two tabs)

```
gr.Blocks
└── gr.Tabs
    ├── Tab: "Ask Agent"
    │   ├── Row: agent_dd (Dropdown), patient_dd (Dropdown), model_tb (Textbox)
    │   ├── question_tb (Textbox, 2 lines)
    │   ├── run_btn (Button)
    │   ├── Row:
    │   │   ├── llm_out  (Textbox,  label="LLM Response")
    │   │   └── gov_out  (gr.JSON,  label="Raw Governance Response")
    │   └── Accordion: "One-click Scenarios"
    │       └── 3 rows × 2 buttons → s1_btn … s6_btn
    └── Tab: "Audit Trail"
        ├── refresh_btn (Button)
        └── audit_tbl   (gr.Dataframe, interactive=False, wrap=True)
```

Use `gr.Blocks`, not `gr.Interface` or `gr.ChatInterface` — two output panels and scenario
buttons that update multiple components simultaneously require `gr.Blocks`.

### Step 7c — Callbacks

```python
def list_patients() -> list[str]:
    # SELECT patient_id FROM silver_patient ORDER BY 1

def ask_agent(agent, patient_id, question, model) -> tuple[str, dict]:
    # purpose = next(iter(AGENT_SCOPES[agent]["purposes"]))
    # gov = get_patient_record(agent, purpose, patient_id)
    # llm = run(agent, patient_id, question, model)   # from agents.py
    # return llm, gov

def refresh_audit() -> list[list]:
    # show_audit_trail(limit=20) → rows for gr.Dataframe
    # cols: ts, agent, purpose, patient_id, action, outcome, detail
```

### Step 7d — Scenario buttons

Each returns `(llm_str, gov_dict, audit_rows)` → updates `[llm_out, gov_out, audit_tbl]`.

| Button | Agent | Calls |
|---|---|---|
| S1 Clinical reads full record | `clinical-summary-agent` | `ask_agent()` |
| S2 Analytics sees masked PHI | `analytics-agent` | `ask_agent()` |
| S3 Scope violation → DENIED | `analytics-agent` claims TREATMENT | `get_patient_record()` direct |
| S4 No consent → DENIED | `research-pipeline` unconsented patient | `get_patient_record()` direct |
| S5 RAG retrieval with citations | `ai-chat-assistant` | `ask_agent()` |
| S6 Consent withdrawal + erasure | `withdraw_consent()` then `retrieve()` | direct calls |

S3, S4, S6 bypass the LLM — the governance layer response is the interesting result. Add a
warning label above S6 (withdraws consent on the live DB for the session).

### Step 7e — Wiring

```python
app.load(lambda: gr.Dropdown(choices=list_patients()), outputs=[patient_dd])
run_btn.click(ask_agent, inputs=[agent_dd, patient_dd, question_tb, model_tb],
              outputs=[llm_out, gov_out])
# scenario buttons all share same 3 outputs
refresh_btn.click(refresh_audit, outputs=[audit_tbl])
```

### Step 7f — sys.path / imports

`app.py` lives in `src/` — same pattern as `demo.py` and `agents.py`:
```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
```
Import `DEFAULT_MODEL` from `agents.py` — single source of truth.

---

## Run

```bash
uv run python src/generate_data.py   # once, to seed data
uv run python src/pipeline.py        # once, to build platform.duckdb
uv run python src/app.py             # → http://localhost:7860
```

---

## Verification

1. App loads; patient dropdown populated from DuckDB
2. `clinical-summary-agent` + "What is the patient's name?" → real name in LLM response; `gov_out` shows full PHI
3. `analytics-agent` + same question → `***MASKED***` in `gov_out`; LLM cannot fabricate what it never received
4. S3 button → `gov_out` shows `{"status": "DENIED", "reason": "...not scoped..."}`
5. S6 button → withdrawal confirmed; Audit Trail tab shows all events

---

## Files changed summary

| File | Status | Change |
|---|---|---|
| `pyproject.toml` | ✅ done | `duckdb`, `litellm`, `python-dotenv`; `requires-python = ">=3.13"` |
| `.python-version` | ✅ done | `3.13` |
| `.env` | ✅ done | `GROQ_API_KEY` (gitignored) |
| `src/db.py` | ✅ done | New — DuckDB wrapper |
| `src/pipeline.py` | ✅ done | DuckDB import, gold view SQL, strict GROUP BY |
| `src/access.py` | ✅ done | DuckDB import, COUNT-then-DELETE |
| `src/retrieval.py` | ✅ done | DuckDB import, sequence, float rounding |
| `src/agents.py` | ✅ done | New — litellm agent with `--agent` CLI flag |
| `src/demo.py` | ✅ done | No changes |
| `src/app.py` | **TODO** | New — Gradio UI |
