# Consent-Aware Healthcare Data Platform — Proof of Concept

A working demonstration of governed data architecture for an AI-powered
healthcare platform. Built with Python 3.13 + DuckDB; every pattern maps
directly to a production stack (Snowflake/Databricks, dbt, Airflow, vector DB).

## What it demonstrates

| Scenario | Control demonstrated | Maps to deliverable |
|---|---|---|
| 1. Clinical agent reads full record | Purpose-bound access, privileged role | Privacy & Compliance Architecture |
| 2. Analytics agent sees masked PHI | Tag-driven masking from classification catalog | Data Classification & Retention |
| 3. Agent claims wrong purpose → denied | Scope enforcement (SMART-on-FHIR pattern) | Integration Architecture Standards |
| 4. No consent → no data | DPDP runtime consent check | Privacy & Compliance Architecture |
| 5. AI retrieval with citations | Pre-filtered, grounded RAG retrieval | AI-safe data foundation |
| 6. Consent withdrawal → erasure propagates | Append-only consent ledger + lineage-driven erasure | Audit Logging & Data Lineage |
| 7. LLM agent subject to same governance | Governance holds through a real tool-calling agent | AI-safe data foundation |

Plus: medallion layering (bronze/silver/gold), FHIR-shaped canonical model,
real LOINC/SNOMED/ICD-10 codes, and a full audit trail of every access.

## Setup

```bash
uv sync                        # installs duckdb, litellm, python-dotenv
```

Add your Groq API key to `.env` (free at console.groq.com — no credit card needed):
```
GROQ_API_KEY="your_free_groq_key"
```

## Run it

```bash
uv run python src/generate_data.py   # synthetic FHIR bundle (8 patients, no real data)
uv run python src/pipeline.py        # bronze -> silver -> gold + classification catalog
uv run python src/demo.py            # six governance scenarios + audit trail

# LLM agent demo — governance holds through a real tool-calling model
uv run python src/agents.py --agent clinical-summary-agent   # PHI visible
uv run python src/agents.py --agent analytics-agent          # PHI masked
uv run python src/agents.py --agent research-pipeline        # DENIED (no consent)
uv run python src/agents.py --help                           # all flags
```

## Architecture

```
FHIR-shaped JSON sources (Patient / Encounter / Observation / Condition / Consent)
        |
        v
  BRONZE  raw JSON, immutable, audit-preserving        (pipeline.py)
        |
        v
  SILVER  normalized relational, terminology as codes,
          classification_catalog tags every column,
          consent_ledger (append-only, SCD2-style)
        |
        +--------------------------+
        v                          v
  GOLD analytics view        retrieval_index (RAG chunks
  (no direct identifiers)     with patient+purpose metadata)
        |                          |
        v                          v
   +---------------------------------------+
   |        GOVERNED ACCESS LAYER          |   (access.py / retrieval.py)
   |  scope check -> consent check ->      |
   |  tag masking -> audit log             |
   +---------------------------------------+
        ^                          ^
        |  declared purpose         |  tools with identity baked in
   Hardcoded callers           LLM agent (agents.py / litellm)
   (demo.py scenarios)         --agent clinical-summary-agent
                                --agent analytics-agent
```

## File map

| File | Role |
|---|---|
| `src/generate_data.py` | Step 1 — synthetic FHIR bundle |
| `src/pipeline.py` | Step 2 — medallion ETL (bronze/silver/gold) |
| `src/access.py` | Step 3 — governed access layer (scope, consent, masking, audit) |
| `src/retrieval.py` | Step 4 — consent-filtered RAG index + retrieval |
| `src/demo.py` | Step 5 — six governance scenarios |
| `src/agents.py` | Step 6 — litellm agent with `--agent` CLI flag |
| `src/db.py` | Shared DuckDB connection factory |
| `data/platform.duckdb` | Generated database (not committed) |
| `data/source_bundle.json` | Generated synthetic data (not committed) |

## Design decisions worth discussing

1. **Agents never touch SQL.** All access goes through one governed front
   door (`get_patient_record`, `retrieve`) that takes an agent identity and
   a declared purpose. Production analogue: FHIR APIs with SMART-on-FHIR
   OAuth scopes behind an API gateway.
2. **Classification is data, not code.** The `classification_catalog` table
   drives masking; change the tag, behaviour changes. Production analogue:
   Snowflake object tags + tag-based masking policies, synced to the
   governance catalog (Alation/Purview).
3. **Consent is an append-only ledger.** Withdrawal inserts a superseding
   row; history is never mutated, so consent state at any past moment is
   provable (DPDP audit requirement). SCD Type-2 thinking applied to consent.
4. **Erasure follows lineage.** Withdrawal propagates deletion to derived
   stores (here, the retrieval index; in production also embeddings, caches,
   extracts). TREATMENT data is retained under the medical-records legal
   basis — conflicts resolved via a legal-basis register, not ad hoc.
5. **Retrieval pre-filters before ranking.** Unauthorized chunks never enter
   the candidate set — RLS applied to RAG. Swap the keyword `score()` for an
   embedding lookup and the governance pattern is unchanged.
6. **Gold excludes direct identifiers by construction.** The analytics path
   physically cannot leak names/MRNs/phones — minimum-necessary by design.
7. **LLM agent identity is baked into tools, not declared by the model.**
   `agents.py` creates tool closures that capture agent ID + purpose at
   construction time. The LLM decides *when* to call tools but cannot change
   *who* is calling — governance is enforced regardless of what the model
   reasons or hallucinates.

## Production hardening path (what 3 months buys)

- DuckDB → Snowflake/Databricks; masking via native tag-based policies
- Keyword retrieval → embeddings + vector store with metadata filtering
- `AGENT_SCOPES` dict → OAuth2 / SMART on FHIR token scopes at the gateway
- Demo consent flow → integration with a DPDP-registered Consent Manager
- Audit table → immutable log sink (e.g., object lock storage) + alerting
- litellm `--model` flag → benchmark governance across providers (OpenAI, Anthropic, Groq)
- Add: de-identification service in front of any third-party LLM calls,
  HL7 v2 ingestion adapters, DPIA documentation, backup/retention schedules
