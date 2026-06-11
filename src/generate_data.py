"""
Step 1 — Synthetic source data (Bronze input).

Simulates what arrives from hospital systems: FHIR-shaped JSON resources
(Patient, Encounter, Observation, Condition) plus consent grants.
Codes use real LOINC / SNOMED CT / ICD-10 identifiers so the
terminology-mapping step downstream is authentic.

No real patient data anywhere. Run: python src/generate_data.py
"""

import json
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Real terminology codes (the interoperability layer) -------------------
LOINC_OBSERVATIONS = [
    {"code": "4548-4",  "display": "Hemoglobin A1c",          "unit": "%",      "range": (5.0, 9.5)},
    {"code": "8480-6",  "display": "Systolic blood pressure", "unit": "mm[Hg]", "range": (105, 165)},
    {"code": "2093-3",  "display": "Total cholesterol",       "unit": "mg/dL",  "range": (150, 280)},
    {"code": "29463-7", "display": "Body weight",             "unit": "kg",     "range": (52, 110)},
]

SNOMED_CONDITIONS = [
    {"snomed": "44054006",  "icd10": "E11.9", "display": "Type 2 diabetes mellitus"},
    {"snomed": "38341003",  "icd10": "I10",   "display": "Essential hypertension"},
    {"snomed": "13644009",  "icd10": "E78.5", "display": "Hypercholesterolemia"},
]

FIRST = ["Aarav", "Diya", "Kabir", "Meera", "Rohan", "Ananya", "Vikram", "Priya"]
LAST = ["Sharma", "Patel", "Reddy", "Iyer", "Khan", "Das", "Mehta", "Nair"]

PURPOSES = ["TREATMENT", "ANALYTICS", "AI_ASSISTANCE", "RESEARCH"]


def make_patient(i: int) -> dict:
    pid = f"PAT-{1000 + i}"
    return {
        "resourceType": "Patient",
        "id": pid,
        "identifier": [{"system": "https://hummcare.example/mrn", "value": f"MRN{700000 + i}"}],
        "name": [{"given": [random.choice(FIRST)], "family": random.choice(LAST)}],
        "telecom": [{"system": "phone", "value": f"+91-98{random.randint(10000000, 99999999)}"}],
        "gender": random.choice(["male", "female"]),
        "birthDate": (datetime(1955, 1, 1) + timedelta(days=random.randint(0, 18000))).strftime("%Y-%m-%d"),
    }


def make_encounter(pid: str) -> dict:
    start = datetime(2026, 1, 1) + timedelta(days=random.randint(0, 150))
    return {
        "resourceType": "Encounter",
        "id": f"ENC-{uuid.uuid4().hex[:8]}",
        "status": "finished",
        "class": {"code": random.choice(["AMB", "IMP"])},  # ambulatory / inpatient
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": start.isoformat(), "end": (start + timedelta(hours=2)).isoformat()},
    }


def make_observation(pid: str, enc_id: str) -> dict:
    loinc = random.choice(LOINC_OBSERVATIONS)
    lo, hi = loinc["range"]
    return {
        "resourceType": "Observation",
        "id": f"OBS-{uuid.uuid4().hex[:8]}",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": loinc["code"], "display": loinc["display"]}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{enc_id}"},
        "effectiveDateTime": (datetime(2026, 2, 1) + timedelta(days=random.randint(0, 120))).isoformat(),
        "valueQuantity": {"value": round(random.uniform(lo, hi), 1), "unit": loinc["unit"]},
    }


def make_condition(pid: str) -> dict:
    c = random.choice(SNOMED_CONDITIONS)
    return {
        "resourceType": "Condition",
        "id": f"CON-{uuid.uuid4().hex[:8]}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [
            {"system": "http://snomed.info/sct", "code": c["snomed"], "display": c["display"]},
            {"system": "http://hl7.org/fhir/sid/icd-10", "code": c["icd10"]},
        ]},
        "subject": {"reference": f"Patient/{pid}"},
    }


def make_consents(pid: str) -> list[dict]:
    """DPDP-style consent grants. TREATMENT always granted;
    secondary purposes granted probabilistically so the demo shows denials."""
    consents = []
    for purpose in PURPOSES:
        granted = purpose == "TREATMENT" or random.random() < 0.6
        if granted:
            consents.append({
                "resourceType": "Consent",
                "id": f"CNS-{uuid.uuid4().hex[:8]}",
                "patient": {"reference": f"Patient/{pid}"},
                "purpose": purpose,
                "status": "active",
                "dateTime": "2026-01-15T09:00:00",
                "noticeVersion": "v2.1-en",
            })
    return consents


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    bundle = {"patients": [], "encounters": [], "observations": [], "conditions": [], "consents": []}

    for i in range(8):
        p = make_patient(i)
        bundle["patients"].append(p)
        bundle["consents"].extend(make_consents(p["id"]))
        for _ in range(random.randint(1, 3)):
            e = make_encounter(p["id"])
            bundle["encounters"].append(e)
            for _ in range(random.randint(1, 4)):
                bundle["observations"].append(make_observation(p["id"], e["id"]))
        bundle["conditions"].append(make_condition(p["id"]))

    out = DATA_DIR / "source_bundle.json"
    out.write_text(json.dumps(bundle, indent=2))
    counts = {k: len(v) for k, v in bundle.items()}
    print(f"Wrote {out.name}: {counts}")


if __name__ == "__main__":
    main()
