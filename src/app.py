"""
Gradio UI for the Humm Care governance PoC.

Run:
    uv run python src/app.py          # → http://localhost:7860

Two tabs:
  • Ask Agent  — pick an agent + patient, type a question, see the LLM
                 response alongside the raw governance layer response
                 (masking / denial visible as JSON).
  • Audit Trail — live table of every access event logged by the platform.

One-click scenario buttons run the six pre-built governance demos without
any typing required.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

import gradio as gr
from access import get_patient_record, withdraw_consent, show_audit_trail, AGENT_SCOPES
from agents import run as agent_run, DEFAULT_MODEL
from db import get_conn
from retrieval import retrieve

AGENTS = list(AGENT_SCOPES.keys())
AUDIT_COLS = ["ts", "agent", "purpose", "patient_id", "action", "outcome", "detail"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_patients() -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT patient_id FROM silver_patient ORDER BY 1").fetchall()
    conn.close()
    return [r["patient_id"] for r in rows]


def _pick_consented(purpose: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT patient_id FROM consent_ledger WHERE purpose=? AND status='active' LIMIT 1",
        (purpose,),
    ).fetchone()
    conn.close()
    return row["patient_id"] if row else None


def _pick_unconsented(purpose: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT p.patient_id FROM silver_patient p "
        "WHERE p.patient_id NOT IN ("
        "  SELECT patient_id FROM consent_ledger WHERE purpose=? AND status='active') LIMIT 1",
        (purpose,),
    ).fetchone()
    conn.close()
    return row["patient_id"] if row else None


def _audit_rows() -> list[list]:
    rows = show_audit_trail(limit=20)
    return [[r.get(c, "") for c in AUDIT_COLS] for r in reversed(rows)]


# ---------------------------------------------------------------------------
# Core callbacks
# ---------------------------------------------------------------------------

def ask_agent(agent: str, patient_id: str, question: str, model: str):
    if not patient_id:
        return "⚠️ Select a patient first.", {}, _audit_rows()
    purpose = next(iter(AGENT_SCOPES[agent]["purposes"]))
    gov = get_patient_record(agent, purpose, patient_id)
    try:
        llm = agent_run(agent, patient_id, question, model)
    except Exception as e:
        llm = f"LLM error: {e}"
    return llm, gov, _audit_rows()


def refresh_audit():
    return _audit_rows()


# ---------------------------------------------------------------------------
# Scenario callbacks
# ---------------------------------------------------------------------------

def scenario_1():
    pid = _pick_consented("TREATMENT")
    if not pid:
        return "No patient with TREATMENT consent found.", {}, _audit_rows()
    gov = get_patient_record("clinical-summary-agent", "TREATMENT", pid)
    try:
        llm = agent_run("clinical-summary-agent", pid,
                        "What is this patient's full name, MRN, and latest lab results?",
                        DEFAULT_MODEL)
    except Exception as e:
        llm = f"LLM error: {e}"
    return llm, gov, _audit_rows()


def scenario_2():
    pid = _pick_consented("ANALYTICS")
    if not pid:
        return "No patient with ANALYTICS consent found.", {}, _audit_rows()
    gov = get_patient_record("analytics-agent", "ANALYTICS", pid)
    try:
        llm = agent_run("analytics-agent", pid,
                        "What is this patient's full name and latest lab results?",
                        DEFAULT_MODEL)
    except Exception as e:
        llm = f"LLM error: {e}"
    return llm, gov, _audit_rows()


def scenario_3():
    pid = _pick_consented("TREATMENT")
    if not pid:
        return "No patient found.", {}, _audit_rows()
    gov = get_patient_record("analytics-agent", "TREATMENT", pid)
    return (
        f"Scope violation: analytics-agent claimed TREATMENT purpose.\n"
        f"Result → {gov['status']}: {gov.get('reason', '')}",
        gov,
        _audit_rows(),
    )


def scenario_4():
    pid = _pick_unconsented("RESEARCH")
    if not pid:
        return "All patients have RESEARCH consent on this seed.", {}, _audit_rows()
    gov = get_patient_record("research-pipeline", "RESEARCH", pid)
    return (
        f"No RESEARCH consent for {pid}.\n"
        f"Result → {gov['status']}: {gov.get('reason', '')}",
        gov,
        _audit_rows(),
    )


def scenario_5():
    pid = _pick_consented("AI_ASSISTANCE")
    if not pid:
        return "No patient with AI_ASSISTANCE consent found.", {}, _audit_rows()
    gov = retrieve("ai-chat-assistant", "AI_ASSISTANCE", pid, "hemoglobin A1c")
    try:
        llm = agent_run("ai-chat-assistant", pid,
                        "What is the latest hemoglobin A1c blood result?",
                        DEFAULT_MODEL)
    except Exception as e:
        llm = f"LLM error: {e}"
    return llm, gov, _audit_rows()


def scenario_6():
    pid = _pick_consented("AI_ASSISTANCE")
    if not pid:
        return "No patient with AI_ASSISTANCE consent found (may already be withdrawn).", {}, _audit_rows()
    w = withdraw_consent(pid, "AI_ASSISTANCE")
    post = retrieve("ai-chat-assistant", "AI_ASSISTANCE", pid, "hemoglobin A1c")
    summary = (
        f"Consent withdrawn for {pid} / AI_ASSISTANCE.\n"
        f"Erasure: {w['erasure']}\n"
        f"Post-withdrawal retrieval → {post['status']}: {post.get('reason', '')}"
    )
    return summary, {**w, "post_retrieval": post}, _audit_rows()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

SCENARIO_BTNS = [
    ("S1 — Clinical agent reads full record (PHI visible)", scenario_1),
    ("S2 — Analytics agent sees masked PHI", scenario_2),
    ("S3 — Scope violation → DENIED", scenario_3),
    ("S4 — No consent → DENIED", scenario_4),
    ("S5 — RAG retrieval with citations", scenario_5),
    ("S6 — Consent withdrawal + erasure ⚠️", scenario_6),
]

with gr.Blocks(title="Humm Care — Governance PoC") as app:
    gr.Markdown("# Humm Care — Consent-Aware Governance PoC")
    gr.Markdown(
        "Select an agent and patient, ask a question, and observe how the governance "
        "layer enforces consent, masking, and scope — even through a live LLM tool call."
    )

    with gr.Tabs():
        # ── Ask Agent tab ────────────────────────────────────────────────
        with gr.Tab("Ask Agent"):
            with gr.Row():
                agent_dd = gr.Dropdown(
                    choices=AGENTS, value=AGENTS[0], label="Agent identity"
                )
                patient_dd = gr.Dropdown(choices=[], label="Patient")
                model_tb = gr.Textbox(
                    value=DEFAULT_MODEL, label="Model (litellm string)", scale=2
                )
            question_tb = gr.Textbox(
                lines=2,
                placeholder="e.g. What is this patient's name and latest HbA1c?",
                label="Question",
            )
            run_btn = gr.Button("Run", variant="primary")

            with gr.Row():
                llm_out = gr.Textbox(label="LLM Response", lines=10, interactive=False)
                gov_out = gr.JSON(label="Raw Governance Response")

            audit_tbl_ask = gr.Dataframe(
                headers=AUDIT_COLS, label="Audit Trail", wrap=True, interactive=False
            )

            with gr.Accordion("One-click Scenarios", open=True):
                gr.Markdown(
                    "Each button runs a pre-built governance scenario. "
                    "**S6 withdraws consent on the live database** — re-run the pipeline to reset."
                )
                scenario_outputs = [llm_out, gov_out, audit_tbl_ask]
                for i in range(0, len(SCENARIO_BTNS), 2):
                    with gr.Row():
                        for label, fn in SCENARIO_BTNS[i : i + 2]:
                            btn = gr.Button(label)
                            btn.click(fn, outputs=scenario_outputs)

            run_btn.click(
                ask_agent,
                inputs=[agent_dd, patient_dd, question_tb, model_tb],
                outputs=scenario_outputs,
            )

        # ── Audit Trail tab ──────────────────────────────────────────────
        with gr.Tab("Audit Trail"):
            refresh_btn = gr.Button("Refresh")
            audit_tbl = gr.Dataframe(
                headers=AUDIT_COLS, label="Audit log (latest 20)", wrap=True,
                interactive=False,
            )
            refresh_btn.click(refresh_audit, outputs=[audit_tbl])

    # populate patients on load
    app.load(
        lambda: gr.Dropdown(choices=list_patients(), value=list_patients()[0]),
        outputs=[patient_dd],
    )
    # seed audit table on load
    app.load(refresh_audit, outputs=[audit_tbl])

if __name__ == "__main__":
    app.launch()
