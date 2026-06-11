"""
Step 6 — LLM agent via litellm, subject to the governed access layer.

The key insight: even when an AI model is the data caller, consent and
masking are enforced identically — the LLM sees DENIED or ***MASKED***
just like any other agent.

Usage:
    uv run python src/agents.py
    uv run python src/agents.py --agent clinical-summary-agent
    uv run python src/agents.py --agent analytics-agent --question "What is the avg HbA1c?"
    uv run python src/agents.py --help

Requires: GROQ_API_KEY in environment (free at console.groq.com — no credit card needed)
"""
import argparse
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv()

import litellm
from access import get_patient_record, get_conn, AGENT_SCOPES
from retrieval import retrieve

litellm.suppress_debug_info = True

DEFAULT_MODEL = "groq/llama-3.3-70b-versatile"


def fn_to_schema(fn) -> dict:
    """Convert a Python function to an OpenAI-format tool schema."""
    sig = inspect.signature(fn)
    properties, required = {}, []
    for name, param in sig.parameters.items():
        ann = param.annotation
        prop_type = "number" if ann in (int, float) else "string"
        properties[name] = {"type": prop_type}
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": fn.__doc__ or "",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def make_tools(agent_id: str, purpose: str):
    """Return governed tool functions with agent identity baked in."""

    def fetch_patient_record(patient_id: str) -> str:
        """Fetch a patient's clinical record. May return DENIED or masked fields."""
        return json.dumps(get_patient_record(agent_id, purpose, patient_id))

    def search_patient_records(patient_id: str, query: str) -> str:
        """Search a patient's records for relevant clinical information."""
        return json.dumps(retrieve(agent_id, purpose, patient_id, query))

    return [fetch_patient_record, search_patient_records]


def pick_patient(purpose: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT patient_id FROM consent_ledger WHERE purpose=? AND status='active' LIMIT 1",
        (purpose,),
    ).fetchone()
    conn.close()
    if not row:
        sys.exit(f"No patient with active {purpose} consent found. Run pipeline.py first.")
    return row["patient_id"]


def run(agent_id: str, patient_id: str, question: str, model: str) -> str:
    scopes = AGENT_SCOPES[agent_id]
    purpose = next(iter(scopes["purposes"]))
    tools_list = make_tools(agent_id, purpose)
    tool_schemas = [fn_to_schema(fn) for fn in tools_list]
    tool_map = {fn.__name__: fn for fn in tools_list}

    system = (
        f"You are operating as '{agent_id}' with purpose '{purpose}'. "
        "Use the provided tools to answer questions about patient data. "
        "If access is denied or data is masked, report that honestly — never fabricate."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Patient {patient_id}: {question}"},
    ]

    # Manual tool loop: we build each assistant message as a plain dict,
    # so provider-specific fields like reasoning_content are never echoed back.
    for _ in range(4):
        response = litellm.completion(model=model, messages=messages, tools=tool_schemas)
        msg = response.choices[0].message
        tool_calls = msg.tool_calls

        if not tool_calls:
            return msg.content

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            fn = tool_map[tc.function.name]
            result = fn(**json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    return response.choices[0].message.content


def main():
    parser = argparse.ArgumentParser(description="Governed LLM agent demo")
    parser.add_argument(
        "--agent", choices=list(AGENT_SCOPES), default="ai-chat-assistant",
        help="Agent identity (determines scope and PHI visibility)",
    )
    parser.add_argument(
        "--patient", default=None,
        help="Patient ID (auto-picks a consented patient if omitted)",
    )
    parser.add_argument(
        "--question", default="Summarise this patient's latest clinical observations.",
        help="Question to ask",
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help="litellm model string, e.g. groq/qwen/qwen3-32b or openai/gpt-4o",
    )
    args = parser.parse_args()

    scopes = AGENT_SCOPES[args.agent]
    purpose = next(iter(scopes["purposes"]))
    patient_id = args.patient or pick_patient(purpose)

    print(f"Agent  : {args.agent}")
    print(f"Purpose: {purpose}  |  sees_phi: {scopes['sees_phi']}")
    print(f"Patient: {patient_id}  |  Model: {args.model}")
    print()
    print(run(args.agent, patient_id, args.question, args.model))


if __name__ == "__main__":
    main()
