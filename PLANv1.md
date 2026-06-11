# Plan: Verify scripts, migrate SQLite → DuckDB, add litellm agent demo

## What was built

1. Verified all six governance scenarios passing on the original SQLite stack
2. Migrated storage from SQLite → DuckDB (db.py wrapper, pipeline/access/retrieval updated)
3. Added `src/agents.py` — a litellm-powered LLM agent demo with `--agent` CLI flag showing governance holds through a real LLM tool loop

---

## Dependencies (uv)

```bash
uv add duckdb litellm python-dotenv
```

`pyproject.toml` requires Python `>=3.13` (Python 3.14 breaks `docstring-parser` used by older LLM libs).

API key goes in `.env` (gitignored):
```
GROQ_API_KEY="your_free_groq_key"
```
Free key at console.groq.com — no credit card needed.

---

## SQLite → DuckDB migration

### New `src/db.py`
Shared DuckDB connection factory. Provides a SQLite-compatible row interface (`_Row`/`_Result`/`_Conn`) so dict-key access (`row["col"]`) and integer-index access (`fetchone()[0]`) work throughout without touching every callsite. All three source files import `get_conn` and `DB_PATH` from here instead of defining them locally.

### `src/pipeline.py`
- Import: `from db import get_conn, DB_PATH, ROOT`
- Gold view: `strftime('%Y','now')` → `year(current_date)`, `substr(birth_date,1,4)` → `birth_date[1:4]`
- GROUP BY: added `p.gender, p.birth_date, c.display` (DuckDB enforces strict GROUP BY, SQLite did not)

### `src/access.py`
- Import: `from db import get_conn, DB_PATH`
- `propagate_erasure`: replaced `.rowcount` on DELETE (returns -1 in DuckDB) with COUNT-then-DELETE pattern

### `src/retrieval.py`
- Import: `from db import get_conn, DB_PATH`
- `AUTOINCREMENT` → DuckDB sequence: `DROP SEQUENCE IF EXISTS chunk_seq` + `CREATE SEQUENCE chunk_seq START 1` + `DEFAULT nextval('chunk_seq')`
- Chunk text: `round(o['value'], 2)` to prevent float32→float64 precision artifacts

---

## `src/agents.py` — litellm agent demo

### Why litellm (not aisuite)
aisuite was evaluated first but its Groq provider has a bug: it echoes `reasoning_content` (returned by Groq models) back into subsequent turn messages, which Groq then rejects. litellm is the industry-standard multi-provider wrapper, actively maintained, and doesn't have this issue.

### Design
- `fn_to_schema()` converts Python functions to OpenAI-format tool schemas using `inspect`
- `make_tools()` returns closures with agent ID + purpose baked in — the LLM cannot override them
- Manual tool loop (not litellm's built-in): we build each assistant message as a plain dict, so `reasoning_content` and other provider-specific fields are never echoed back
- `--agent` CLI flag selects from `AGENT_SCOPES`; purpose is derived automatically
- Default model: `groq/llama-3.3-70b-versatile`

### CLI usage
```bash
uv run python src/agents.py --agent clinical-summary-agent
uv run python src/agents.py --agent analytics-agent --question "What is the patient's name and MRN?"
uv run python src/agents.py --agent research-pipeline
uv run python src/agents.py --agent ai-chat-assistant --model groq/llama-3.3-70b-versatile
uv run python src/agents.py --help
```

### Verified governance outcomes
| Agent | `sees_phi` | PHI query result |
|---|---|---|
| `clinical-summary-agent` | True | Returns real name, MRN, phone |
| `analytics-agent` | False | Returns `***MASKED***` — LLM cannot fabricate what it never received |
| `research-pipeline` | False | DENIED if no RESEARCH consent |
| `ai-chat-assistant` | False | DENIED after consent withdrawal |

---

## Full run sequence

```bash
uv run python src/generate_data.py          # → data/source_bundle.json
uv run python src/pipeline.py               # → data/platform.duckdb
uv run python src/demo.py                   # → 6 governance scenarios
uv run python src/agents.py --agent clinical-summary-agent
uv run python src/agents.py --agent analytics-agent
```

## Files changed

| File | Change |
|---|---|
| `pyproject.toml` | `duckdb`, `litellm`, `python-dotenv`; `requires-python = ">=3.13"` |
| `.python-version` | `3.13` |
| `.env` | `GROQ_API_KEY` (gitignored) |
| `src/db.py` | **New** — DuckDB wrapper + shared `get_conn` / `DB_PATH` |
| `src/pipeline.py` | DuckDB import, gold view SQL, strict GROUP BY |
| `src/access.py` | DuckDB import, COUNT-then-DELETE for rowcount |
| `src/retrieval.py` | DuckDB import, sequence for auto-increment, float rounding |
| `src/agents.py` | **New** — litellm agent with `--agent` CLI flag |
| `src/demo.py` | No changes |
