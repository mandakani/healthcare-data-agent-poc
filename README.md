# Healthcare Data Agent

A proof-of-concept consent-aware healthcare data governance platform. Six DPDP compliance scenarios run end-to-end — scope enforcement, PHI masking, consent checks, audit logging, RAG retrieval, and erasure propagation — on a medallion-layered DuckDB database with real FHIR structures and medical codes. An LLM agent with tool-calling runs through the same governance controls and cannot escape them.

Every pattern here maps directly to a production stack (Snowflake/Databricks, SMART-on-FHIR, vector stores) — the PoC is designed to be a working reference, not a toy.

---

## What it demonstrates

| # | Scenario | Governance control | Maps to |
|---|---|---|---|
| 1 | Clinical agent reads full patient record | Purpose-bound access; privileged role sees real PHI | Privacy & Compliance Architecture |
| 2 | Analytics agent reads same patient | Tag-driven PHI masking from classification catalog | Data Classification & Retention |
| 3 | Agent claims wrong purpose → denied | Scope enforcement (SMART-on-FHIR pattern) | Integration Architecture Standards |
| 4 | No consent → no data returned | DPDP runtime consent check against append-only ledger | Privacy & Compliance Architecture |
| 5 | AI retrieval with source citations | Pre-filtered RAG — unauthorized chunks never enter candidate set | AI-safe data foundation |
| 6 | Consent withdrawal → erasure propagates | Ledger append + lineage-driven deletion from derived stores | Audit Logging & Data Lineage |
| 7 | LLM agent subject to same controls | Governance holds through a real tool-calling model | AI-safe data foundation |

---

## Architecture

```
FHIR-shaped JSON sources
(Patient / Encounter / Observation / Condition / Consent)
        |
        v
  BRONZE  raw JSON, immutable, audit-preserving            (pipeline.py)
        |
        v
  SILVER  normalized relational tables, terminology as codes,
          classification_catalog drives masking policy,
          consent_ledger: append-only, SCD2-style          (pipeline.py)
        |
        +------------------------------+
        v                              v
  GOLD analytics view          retrieval_index
  (no direct identifiers,      (RAG chunks with
   minimum-necessary design)    patient + purpose metadata)
        |                              |
        v                              v
  +--------------------------------------------------+
  |           GOVERNED ACCESS LAYER                  |  (access.py / retrieval.py)
  |                                                  |
  |  Every request goes through:                     |
  |  1. Scope check    (is this purpose allowed?)    |
  |  2. Consent check  (has patient opted in?)       |
  |  3. Tag masking    (PHI_DIRECT → ***MASKED***)   |
  |  4. Audit log      (who, what, outcome, when)    |
  +--------------------------------------------------+
        ^                              ^
        | declared purpose             | identity baked into tool closures
  Hardcoded callers             LLM agent (agents.py / litellm)
  (demo.py scenarios)           --agent clinical-summary-agent
```

---

## Quick start

**Prerequisites:** Python 3.13, [`uv`](https://github.com/astral-sh/uv)

```bash
pip install uv          # if you don't have it
uv sync                 # installs duckdb, litellm, python-dotenv, gradio
```

Add a free Groq API key to `.env` (sign up at [console.groq.com](https://console.groq.com) — no credit card):

```
GROQ_API_KEY="your_free_groq_key"
```

### Run the governance demo

```bash
uv run python src/generate_data.py   # Step 1: generate 8 synthetic FHIR patients
uv run python src/pipeline.py        # Step 2: bronze → silver → gold + classification catalog
uv run python src/demo.py            # Step 3: six governance scenarios + audit trail
```

### Run the LLM agent demo

Governance holds through a real tool-calling model. The agent cannot override scope or consent.

```bash
uv run python src/agents.py --agent clinical-summary-agent   # PHI visible (TREATMENT scope)
uv run python src/agents.py --agent analytics-agent          # PHI masked (ANALYTICS scope)
uv run python src/agents.py --agent ai-chat-assistant        # PHI masked (AI_ASSISTANCE scope)
uv run python src/agents.py --agent research-pipeline        # DENIED — no consent
uv run python src/agents.py --help                           # all options
```

**Optional flags:**

```
--patient PAT-1000                     pick a specific patient (auto-selected if omitted)
--question "What medications..."       custom query (defaults to clinical summary)
--model groq/llama-3.3-70b-versatile  switch LLM provider via litellm
```

---

## Key design patterns

Each pattern below has a direct production analogue.

**1. Agents never touch SQL.**
All data access routes through two governed functions — `get_patient_record(agent, purpose, patient_id)` and `retrieve(agent, purpose, patient_id, query)`. The agent declares its identity and purpose; the access layer decides what (if anything) to return.
**Production analogue:** FHIR R4 APIs with SMART-on-FHIR OAuth 2.0 scopes behind an API gateway. The scope is issued at token-minting time; the model cannot re-scope itself at inference time.

**2. Classification is data, not code.**
The `classification_catalog` table maps `(table_name, column_name) → tag`. Tags drive masking at read time — change a tag row, behavior changes immediately. No code deployment required.
**Production analogue:** Snowflake object tags and tag-based masking policies, or Databricks Unity Catalog column tags, synced from a governance catalog (Alation, Purview, Collibra).

**3. Consent is an append-only ledger.**
Consent grants and withdrawals are inserted as new rows — `UPDATE` is never used. This means consent state at any past moment is provable by replaying the ledger, satisfying DPDP audit requirements.
**Production analogue:** SCD Type-2 applied to consent state. In production: an immutable event log (Kafka topic or object-lock S3) feeding a Consent Manager registered under DPDP.

**4. Erasure follows lineage.**
When consent is withdrawn, deletion propagates to all derived stores — here, the RAG `retrieval_index`. In production, this extends to vector embeddings, analytical extracts, and query caches. TREATMENT data is retained under the medical-records legal basis, resolved by a legal-basis register, not ad hoc.
**Production analogue:** A data lineage graph (OpenLineage/Atlas) driving automated erasure workflows triggered on consent-withdrawal events.

**5. Retrieval pre-filters before ranking.**
The RAG query filters to authorized chunks *before* scoring. Unauthorized content never enters the candidate set — row-level security applied to retrieval, not bolted on after ranking.
**Production analogue:** Swap the keyword `score()` for an ANN embedding lookup (pgvector, Databricks Vector Search, Pinecone). The governance pattern — filter first, rank second — is unchanged.

**6. Gold excludes direct identifiers by construction.**
The Gold analytics view is defined without `name`, `mrn`, or `phone`. The analytics path physically cannot leak direct identifiers regardless of query; minimum-necessary is enforced at schema design time.
**Production analogue:** Snowflake views or dbt models exposing only non-identifying columns, gated by role-based access controls that exclude direct-identifier columns from the analytics role.

**7. LLM identity is baked into tools, not declared by the model.**
`agents.py` builds tool closures that capture `agent_id` and `purpose` at construction time. The model decides *when* to call a tool but cannot change *who* is calling. Governance is enforced regardless of what the model reasons, summarizes, or hallucinates.
**Production analogue:** Tool servers (MCP servers, OpenAI function hosts) that resolve caller identity from a verified token, not from anything in the LLM's message context.

---

## File map

| File | Role |
|---|---|
| `src/generate_data.py` | Step 1 — synthetic FHIR bundle (8 patients, LOINC/SNOMED/ICD-10 codes) |
| `src/pipeline.py` | Step 2 — medallion ETL: bronze → silver → gold + classification catalog |
| `src/access.py` | Step 3 — governed access layer: scope, consent, masking, audit log |
| `src/retrieval.py` | Step 4 — RAG chunking + consent-filtered retrieval |
| `src/demo.py` | Step 5 — six governance scenarios + audit trail output |
| `src/agents.py` | Step 6 — litellm agent with `--agent` CLI flag |
| `src/db.py` | Shared DuckDB connection factory |
| `src/app.py` | Gradio web UI (work in progress) |
| `data/platform.duckdb` | Generated database (not committed; created by `pipeline.py`) |
| `data/source_bundle.json` | Synthetic FHIR bundle (not committed; created by `generate_data.py`) |

---

## Tech stack

| Component | Choice | Why |
|---|---|---|
| Database | DuckDB 1.5+ | Zero-config, SQLite-compatible syntax, runs offline; patterns carry unchanged to Snowflake/Databricks |
| LLM layer | litellm 1.88+ | Multi-provider wrapper; swap `groq/...` for `openai/...` or `anthropic/...` without touching agent code |
| Default model | `groq/llama-3.3-70b-versatile` | Free API key, fast inference, real tool-calling support |
| Package manager | uv | Single `uv sync` installs all dependencies at pinned versions |
| Language | Python 3.13 | Required; Python 3.14 breaks `docstring-parser` used by older LLM libs |

---

## Production hardening path

This PoC is intentionally minimal. A three-month hardening sprint would add:

- **Storage:** DuckDB → Snowflake or Databricks; masking via native tag-based policies instead of application-level masking
- **Retrieval:** keyword scoring → vector embeddings + ANN index (pgvector, Databricks Vector Search) with metadata pre-filtering
- **Auth:** `AGENT_SCOPES` dict → OAuth2 / SMART-on-FHIR token scopes issued at the API gateway
- **Consent:** demo ledger → integration with a DPDP-registered Consent Manager
- **Audit:** in-process table → immutable log sink (S3 object lock, Splunk) with alerting on denied access patterns
- **LLM governance:** add a de-identification service in front of any third-party model calls; benchmark governance across providers using the `--model` flag
- **Ingestion:** add HL7 v2 adapters alongside the FHIR JSON path
- **Compliance docs:** DPIA documentation, backup/retention schedules, legal-basis register

---

## Data note

All patient data is completely synthetic — generated programmatically with no connection to real individuals. Medical codes (LOINC, SNOMED CT, ICD-10) are real terminology codes used with synthetic values for clinical realism. No PHI is present anywhere in this repository.
