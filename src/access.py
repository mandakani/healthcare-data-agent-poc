"""
Step 3 — The governed access layer: what agents call instead of SQL.

Implements four controls on every request:
  1. Scope check        — agent's role must permit the purpose (SMART-on-FHIR-style scopes)
  2. Consent check      — patient must have active consent for the declared purpose (DPDP)
  3. Tag-based masking  — PHI_DIRECT columns masked unless role is privileged
  4. Audit logging      — every access (allowed or denied) recorded with purpose

Also implements consent withdrawal with erasure propagation to derived stores.
"""

from datetime import datetime
from db import get_conn, DB_PATH

# Agent/service identities and what each may do. In production: OAuth scopes.
AGENT_SCOPES = {
    "clinical-summary-agent": {"purposes": {"TREATMENT"},               "sees_phi": True},
    "analytics-agent":        {"purposes": {"ANALYTICS"},               "sees_phi": False},
    "ai-chat-assistant":      {"purposes": {"AI_ASSISTANCE"},           "sees_phi": False},
    "research-pipeline":      {"purposes": {"RESEARCH"},                "sees_phi": False},
}

MASK = "***MASKED***"



def _ensure_audit_table(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
        ts TEXT, agent TEXT, purpose TEXT, patient_id TEXT,
        action TEXT, outcome TEXT, detail TEXT)""")


def _audit(conn, agent, purpose, patient_id, action, outcome, detail=""):
    _ensure_audit_table(conn)
    conn.execute("INSERT INTO audit_log VALUES (?,?,?,?,?,?,?)",
                 (datetime.utcnow().isoformat(), agent, purpose, patient_id,
                  action, outcome, detail))
    conn.commit()


def has_active_consent(conn, patient_id: str, purpose: str) -> bool:
    """Latest ledger entry for (patient, purpose) wins — append-only semantics."""
    row = conn.execute("""
        SELECT status FROM consent_ledger
        WHERE patient_id = ? AND purpose = ?
        ORDER BY recorded_at DESC, effective_from DESC LIMIT 1""",
        (patient_id, purpose)).fetchone()
    return bool(row and row["status"] == "active")


def _phi_direct_columns(conn) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM classification_catalog WHERE tag='PHI_DIRECT'").fetchall()
    return {r["column_name"] for r in rows}


def get_patient_record(agent: str, purpose: str, patient_id: str) -> dict:
    """The single front door for patient data. Returns record or denial."""
    conn = get_conn()
    try:
        scopes = AGENT_SCOPES.get(agent)
        if not scopes or purpose not in scopes["purposes"]:
            _audit(conn, agent, purpose, patient_id, "read_record", "DENIED", "out-of-scope purpose")
            return {"status": "DENIED", "reason": f"Agent '{agent}' not scoped for purpose {purpose}"}

        if not has_active_consent(conn, patient_id, purpose):
            _audit(conn, agent, purpose, patient_id, "read_record", "DENIED", "no active consent")
            return {"status": "DENIED", "reason": f"No active {purpose} consent for {patient_id}"}

        patient = conn.execute(
            "SELECT * FROM silver_patient WHERE patient_id=?", (patient_id,)).fetchone()
        if not patient:
            _audit(conn, agent, purpose, patient_id, "read_record", "NOT_FOUND")
            return {"status": "NOT_FOUND"}

        record = dict(patient)
        # Tag-driven masking: classification catalog decides, not the caller.
        if not scopes["sees_phi"]:
            for col in _phi_direct_columns(conn):
                if col in record:
                    record[col] = MASK

        obs = conn.execute("""
            SELECT loinc_code, loinc_display, value, unit, effective_time
            FROM silver_observation WHERE patient_id=? ORDER BY effective_time DESC""",
            (patient_id,)).fetchall()
        cond = conn.execute("""
            SELECT snomed_code, icd10_code, display, status
            FROM silver_condition WHERE patient_id=?""", (patient_id,)).fetchall()

        record["observations"] = [dict(o) for o in obs]
        record["conditions"] = [dict(c) for c in cond]
        _audit(conn, agent, purpose, patient_id, "read_record", "ALLOWED",
               f"{len(record['observations'])} observations returned")
        return {"status": "OK", "record": record}
    finally:
        conn.close()


def withdraw_consent(patient_id: str, purpose: str) -> dict:
    """DPDP withdrawal: append a 'withdrawn' row (never mutate history),
    then propagate erasure to derived stores for that purpose."""
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO consent_ledger
                (consent_id, patient_id, purpose, status, notice_version, effective_from)
            VALUES (?,?,?,?,?,?)""",
            (f"CNS-WD-{datetime.utcnow().strftime('%H%M%S%f')}", patient_id, purpose,
             "withdrawn", "v2.1-en", datetime.utcnow().isoformat()))
        conn.commit()

        erased = propagate_erasure(conn, patient_id, purpose)
        _audit(conn, "consent-manager", purpose, patient_id, "withdraw_consent",
               "RECORDED", f"erasure: {erased}")
        return {"status": "WITHDRAWN", "erasure": erased}
    finally:
        conn.close()


def propagate_erasure(conn, patient_id: str, purpose: str) -> str:
    """Lineage-driven erasure. In this PoC the derived store is the
    retrieval index (see retrieval.py); production would also cover
    warehouse extracts, caches, and vector embeddings. TREATMENT data
    is retained under medical record retention law (legal-basis register)."""
    if purpose == "TREATMENT":
        return "retained under medical-records legal basis"
    n = conn.execute(
        "SELECT COUNT(*) FROM retrieval_index WHERE patient_id=? AND purpose=?",
        (patient_id, purpose)).fetchone()[0]
    conn.execute(
        "DELETE FROM retrieval_index WHERE patient_id=? AND purpose=?",
        (patient_id, purpose))
    conn.commit()
    return f"{n} chunks removed from retrieval index for purpose {purpose}"


def show_audit_trail(limit: int = 15) -> list[dict]:
    conn = get_conn()
    _ensure_audit_table(conn)
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
