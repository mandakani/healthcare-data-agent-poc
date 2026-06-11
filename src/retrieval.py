"""
Step 4 — Consent-filtered retrieval (RAG-lite).

Demonstrates the retrieval half of a RAG system with governance built in:
  - Clinical records are "chunked" into retrievable text snippets
  - Each chunk carries patient_id + purpose metadata (the governance hook)
  - Retrieval PRE-FILTERS by patient scope and consent before ranking —
    an unauthorized chunk never enters the candidate set

Keyword scoring stands in for vector similarity so the PoC runs offline;
swap `score()` for an embedding lookup (pgvector / Databricks Vector
Search) and the governance pattern is unchanged — that's the point.
"""

from access import has_active_consent, _audit, AGENT_SCOPES
from db import get_conn, DB_PATH



def build_index() -> None:
    """Chunk silver-layer clinical data into the retrieval index.
    Production analogue: embedding pipeline refreshed by orchestration
    whenever source records change."""
    conn = get_conn()
    conn.execute("DROP TABLE IF EXISTS retrieval_index")
    conn.execute("DROP SEQUENCE IF EXISTS chunk_seq")
    conn.execute("CREATE SEQUENCE chunk_seq START 1")
    conn.execute("""CREATE TABLE retrieval_index (
        chunk_id INTEGER DEFAULT nextval('chunk_seq'),
        patient_id TEXT, purpose TEXT, chunk_text TEXT, source_ref TEXT)""")

    obs = conn.execute("""
        SELECT o.patient_id, o.observation_id, o.loinc_display, o.value, o.unit,
               o.effective_time
        FROM silver_observation o""").fetchall()
    for o in obs:
        text = (f"{o['loinc_display']} recorded as {round(o['value'], 2)} {o['unit']} "
                f"on {o['effective_time'][:10]}")
        # Index a chunk per purpose that might consume it; consent gates at query time
        for purpose in ("TREATMENT", "AI_ASSISTANCE", "ANALYTICS"):
            conn.execute(
                "INSERT INTO retrieval_index (patient_id, purpose, chunk_text, source_ref) "
                "VALUES (?,?,?,?)",
                (o["patient_id"], purpose, text, f"Observation/{o['observation_id']}"))

    cond = conn.execute("""
        SELECT patient_id, condition_id, display, snomed_code, icd10_code
        FROM silver_condition""").fetchall()
    for c in cond:
        text = (f"Active condition: {c['display']} "
                f"(SNOMED {c['snomed_code']}, ICD-10 {c['icd10_code']})")
        for purpose in ("TREATMENT", "AI_ASSISTANCE", "ANALYTICS"):
            conn.execute(
                "INSERT INTO retrieval_index (patient_id, purpose, chunk_text, source_ref) "
                "VALUES (?,?,?,?)",
                (c["patient_id"], purpose, text, f"Condition/{c['condition_id']}"))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM retrieval_index").fetchone()[0]
    conn.close()
    print(f"retrieval_index: {n} chunks")


def score(query: str, text: str) -> int:
    """Offline stand-in for cosine similarity."""
    q_terms = {t.lower().strip("?.,") for t in query.split()}
    return sum(1 for t in q_terms if t and t in text.lower())


def retrieve(agent: str, purpose: str, patient_id: str, query: str, k: int = 3) -> dict:
    """Governed retrieval: scope -> consent -> pre-filter -> rank -> cite."""
    conn = get_conn()
    try:
        scopes = AGENT_SCOPES.get(agent)
        if not scopes or purpose not in scopes["purposes"]:
            _audit(conn, agent, purpose, patient_id, "retrieve", "DENIED", "out-of-scope")
            return {"status": "DENIED", "reason": "agent not scoped for purpose"}
        if not has_active_consent(conn, patient_id, purpose):
            _audit(conn, agent, purpose, patient_id, "retrieve", "DENIED", "no consent")
            return {"status": "DENIED", "reason": f"no active {purpose} consent"}

        # PRE-filter: only this patient's chunks for this purpose are candidates.
        rows = conn.execute(
            "SELECT chunk_text, source_ref FROM retrieval_index "
            "WHERE patient_id=? AND purpose=?", (patient_id, purpose)).fetchall()

        ranked = sorted(rows, key=lambda r: score(query, r["chunk_text"]), reverse=True)
        hits = [{"text": r["chunk_text"], "source": r["source_ref"]}
                for r in ranked[:k] if score(query, r["chunk_text"]) > 0]

        _audit(conn, agent, purpose, patient_id, "retrieve", "ALLOWED",
               f"query='{query}' hits={len(hits)}")
        if not hits:
            # Grounding rule: no retrieval -> explicit not-found, never a guess.
            return {"status": "OK", "answer": "NOT_FOUND_IN_RECORDS", "chunks": []}
        return {"status": "OK", "chunks": hits}
    finally:
        conn.close()
