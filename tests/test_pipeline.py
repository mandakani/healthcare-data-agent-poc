"""Gold-layer grain tests: one row per patient, counts that don't fan out."""

import duckdb
import pytest

from db import _Conn
from pipeline import build_bronze, build_gold, build_silver


def patient(pid):
    return {"resourceType": "Patient", "id": pid,
            "identifier": [{"value": f"MRN-{pid}"}],
            "name": [{"given": ["Test"], "family": pid}],
            "telecom": [{"value": "+91-0000000000"}],
            "gender": "female", "birthDate": "1980-01-01"}


def encounter(eid, pid):
    return {"resourceType": "Encounter", "id": eid,
            "subject": {"reference": f"Patient/{pid}"}, "class": {"code": "AMB"},
            "period": {"start": "2026-01-01T09:00:00", "end": "2026-01-01T10:00:00"}}


def observation(oid, pid, eid, loinc, value):
    return {"resourceType": "Observation", "id": oid,
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "code": {"coding": [{"code": loinc, "display": "x"}]},
            "valueQuantity": {"value": value, "unit": "u"},
            "effectiveDateTime": "2026-02-01T09:00:00"}


def condition(cid, pid, display):
    return {"resourceType": "Condition", "id": cid,
            "subject": {"reference": f"Patient/{pid}"},
            "code": {"coding": [{"system": "http://snomed.info/sct", "code": "1", "display": display}]},
            "clinicalStatus": {"coding": [{"code": "active"}]}}


@pytest.fixture
def gold():
    # P1: 3 encounters, 4 observations, 2 conditions. P2: nothing but the patient row.
    bundle = {
        "patients": [patient("P1"), patient("P2")],
        "encounters": [encounter(e, "P1") for e in ("E1", "E2", "E3")],
        "observations": [
            observation("O1", "P1", "E1", "4548-4", 6.0),
            observation("O2", "P1", "E1", "4548-4", 8.0),
            observation("O3", "P1", "E2", "8480-6", 120),
            observation("O4", "P1", "E3", "8480-6", 130),
        ],
        "conditions": [condition("C1", "P1", "Diabetes"), condition("C2", "P1", "Hypertension")],
        "consents": [],
    }
    conn = _Conn(duckdb.connect())
    build_bronze(conn, bundle)
    build_silver(conn)
    build_gold(conn)
    rows = conn.execute("SELECT * FROM gold_patient_summary ORDER BY patient_id").fetchall()
    conn.close()
    return rows


@pytest.fixture
def by_patient(gold):
    return {r["patient_id"]: r for r in gold}


def test_one_row_per_patient(gold):
    # The old view emitted one row per (patient, condition).
    assert [r["patient_id"] for r in gold] == ["P1", "P2"]


def test_counts_do_not_fan_out(by_patient):
    p1 = by_patient["P1"]
    assert p1["encounter_count"] == 3
    assert p1["observation_count"] == 4  # the old join counted each observation once per encounter: 12
    assert p1["avg_hba1c"] == 7.0
    assert p1["primary_condition"] == "Diabetes"


def test_patient_without_activity_gets_zero_counts(by_patient):
    p2 = by_patient["P2"]
    assert (p2["encounter_count"], p2["observation_count"]) == (0, 0)
    assert p2["avg_hba1c"] is None and p2["primary_condition"] is None
