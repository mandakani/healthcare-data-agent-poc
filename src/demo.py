"""
Step 5 — End-to-end demo. Run after generate_data.py and pipeline.py:

    python src/demo.py

Walks through six scenarios that map one-to-one to Humm Care's
deliverables list: governance, classification, privacy architecture,
audit/lineage, consent (DPDP), and AI-safe retrieval.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from access import get_patient_record, withdraw_consent, show_audit_trail, get_conn
from retrieval import build_index, retrieve

DIVIDER = "=" * 72


def banner(n, title):
    print(f"\n{DIVIDER}\nSCENARIO {n}: {title}\n{DIVIDER}")


def pick_patient_with_consent(purpose: str) -> str:
    conn = get_conn()
    row = conn.execute("""
        SELECT patient_id FROM consent_ledger
        WHERE purpose=? AND status='active' LIMIT 1""", (purpose,)).fetchone()
    conn.close()
    return row["patient_id"]


def pick_patient_without_consent(purpose: str) -> str | None:
    conn = get_conn()
    row = conn.execute("""
        SELECT p.patient_id FROM silver_patient p
        WHERE p.patient_id NOT IN (
            SELECT patient_id FROM consent_ledger WHERE purpose=? AND status='active')
        LIMIT 1""", (purpose,)).fetchone()
    conn.close()
    return row["patient_id"] if row else None


def main():
    build_index()
    treated = pick_patient_with_consent("TREATMENT")
    ai_ok = pick_patient_with_consent("AI_ASSISTANCE")
    no_research = pick_patient_without_consent("RESEARCH")

    # 1 — Clinical agent, valid purpose, privileged role: full record
    banner(1, "Clinical agent reads record under TREATMENT (PHI visible)")
    r = get_patient_record("clinical-summary-agent", "TREATMENT", treated)
    rec = r["record"]
    print(f"name={rec['full_name']}  mrn={rec['mrn']}  phone={rec['phone']}")
    print(f"conditions={[c['display'] for c in rec['conditions']]}")
    print(f"observations returned: {len(rec['observations'])}")

    # 2 — Analytics agent, same patient: PHI_DIRECT masked by tag policy
    banner(2, "Analytics agent reads same patient (tag-based masking)")
    r = get_patient_record("analytics-agent", "ANALYTICS", treated)
    if r["status"] == "OK":
        rec = r["record"]
        print(f"name={rec['full_name']}  mrn={rec['mrn']}  phone={rec['phone']}")
        print("-> identity masked, clinical values intact (minimum-necessary)")
    else:
        print(f"DENIED: {r['reason']}  (no ANALYTICS consent for this patient)")

    # 3 — Purpose scope enforcement: analytics agent cannot claim TREATMENT
    banner(3, "Analytics agent attempts TREATMENT purpose (scope violation)")
    r = get_patient_record("analytics-agent", "TREATMENT", treated)
    print(f"{r['status']}: {r['reason']}")

    # 4 — DPDP consent enforcement: no consent, no data
    banner(4, "Research pipeline blocked where RESEARCH consent absent")
    if no_research:
        r = get_patient_record("research-pipeline", "RESEARCH", no_research)
        print(f"{r['status']}: {r['reason']}")
    else:
        print("(all synthetic patients granted RESEARCH consent on this seed)")

    # 5 — Governed RAG retrieval with citations
    banner(5, "AI assistant retrieves grounded, cited chunks")
    r = retrieve("ai-chat-assistant", "AI_ASSISTANCE", ai_ok,
                 "What is the latest hemoglobin A1c blood result?")
    for c in r.get("chunks", []):
        print(f"  [{c['source']}] {c['text']}")
    if not r.get("chunks"):
        print(f"  {r.get('answer', r)}")

    # 6 — Consent withdrawal with erasure propagation to derived store
    banner(6, "Patient withdraws AI_ASSISTANCE consent (DPDP withdrawal)")
    w = withdraw_consent(ai_ok, "AI_ASSISTANCE")
    print(f"ledger: {w['status']}  |  {w['erasure']}")
    r = retrieve("ai-chat-assistant", "AI_ASSISTANCE", ai_ok, "hemoglobin A1c result")
    print(f"post-withdrawal retrieval -> {r['status']}: {r.get('reason','')}")

    # Audit trail proves all of the above
    banner("FINAL", "Audit trail (who / purpose / what / outcome)")
    for row in reversed(show_audit_trail(12)):
        print(f"{row['ts'][:19]}  {row['agent']:<24} {row['purpose']:<14} "
              f"{row['patient_id']:<10} {row['action']:<17} {row['outcome']:<8} {row['detail']}")

    print(f"\n{DIVIDER}\nAll six controls demonstrated. Gold layer sample:\n")
    conn = get_conn()
    for row in conn.execute("SELECT * FROM gold_patient_summary LIMIT 4"):
        print(dict(row))
    conn.close()


if __name__ == "__main__":
    main()
