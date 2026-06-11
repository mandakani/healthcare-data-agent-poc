"""
Step 2 — Medallion pipeline with classification tagging.

Bronze : raw JSON landed as-is (audit-preserving).
Silver : FHIR-normalized relational tables + terminology kept as codes.
Gold   : analytics-ready patient summary.
Tags   : every column registered in a classification catalog
         (PHI_DIRECT / PHI_INDIRECT / CLINICAL / OPERATIONAL) —
         the same pattern as Snowflake object tags driving masking.

Storage is SQLite for portability; swap the connection for DuckDB or
Snowflake and the SQL carries over almost unchanged.

Run: python src/pipeline.py
"""

import json
from db import get_conn, DB_PATH, ROOT

# --- Classification catalog: policy as data ---------------------------------
# In production this lives in the governance catalog (Alation/Purview) and is
# applied as warehouse tags. Tier drives masking in access.py.
COLUMN_TAGS = [
    # (table, column, tag)
    ("silver_patient", "full_name",   "PHI_DIRECT"),
    ("silver_patient", "mrn",         "PHI_DIRECT"),
    ("silver_patient", "phone",       "PHI_DIRECT"),
    ("silver_patient", "birth_date",  "PHI_INDIRECT"),
    ("silver_patient", "gender",      "PHI_INDIRECT"),
    ("silver_observation", "value",   "CLINICAL"),
    ("silver_observation", "loinc_code", "CLINICAL"),
    ("silver_condition", "snomed_code", "CLINICAL"),
    ("silver_condition", "icd10_code",  "CLINICAL"),
    ("silver_encounter", "encounter_class", "OPERATIONAL"),
]



def build_bronze(conn, bundle: dict) -> None:
    """Land raw resources untouched — immutable evidence layer."""
    conn.execute("DROP TABLE IF EXISTS bronze_raw")
    conn.execute("""
        CREATE TABLE bronze_raw (
            resource_type TEXT, resource_id TEXT, raw_json TEXT,
            ingested_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
    for key, resources in bundle.items():
        for r in resources:
            conn.execute(
                "INSERT INTO bronze_raw (resource_type, resource_id, raw_json) VALUES (?,?,?)",
                (r["resourceType"], r["id"], json.dumps(r)),
            )


def build_silver(conn) -> None:
    """Normalize FHIR JSON into governed relational tables."""
    cur = conn.cursor()
    for t in ["silver_patient", "silver_encounter", "silver_observation",
              "silver_condition", "consent_ledger", "classification_catalog"]:
        cur.execute(f"DROP TABLE IF EXISTS {t}")

    cur.execute("""CREATE TABLE silver_patient (
        patient_id TEXT PRIMARY KEY, mrn TEXT, full_name TEXT, phone TEXT,
        gender TEXT, birth_date TEXT)""")
    cur.execute("""CREATE TABLE silver_encounter (
        encounter_id TEXT PRIMARY KEY, patient_id TEXT, encounter_class TEXT,
        start_time TEXT, end_time TEXT)""")
    cur.execute("""CREATE TABLE silver_observation (
        observation_id TEXT PRIMARY KEY, patient_id TEXT, encounter_id TEXT,
        loinc_code TEXT, loinc_display TEXT, value REAL, unit TEXT, effective_time TEXT)""")
    cur.execute("""CREATE TABLE silver_condition (
        condition_id TEXT PRIMARY KEY, patient_id TEXT,
        snomed_code TEXT, icd10_code TEXT, display TEXT, status TEXT)""")
    # Append-only consent ledger (DPDP): never UPDATE rows in place —
    # withdrawal inserts a superseding row. SCD2 thinking applied to consent.
    cur.execute("""CREATE TABLE consent_ledger (
        consent_id TEXT, patient_id TEXT, purpose TEXT, status TEXT,
        notice_version TEXT, effective_from TEXT,
        recorded_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    cur.execute("""CREATE TABLE classification_catalog (
        table_name TEXT, column_name TEXT, tag TEXT)""")

    rows = cur.execute("SELECT resource_type, raw_json FROM bronze_raw").fetchall()
    for row in rows:
        r = json.loads(row["raw_json"])
        rt = row["resource_type"]
        if rt == "Patient":
            name = r["name"][0]
            cur.execute("INSERT INTO silver_patient VALUES (?,?,?,?,?,?)", (
                r["id"], r["identifier"][0]["value"],
                f"{name['given'][0]} {name['family']}",
                r["telecom"][0]["value"], r["gender"], r["birthDate"]))
        elif rt == "Encounter":
            cur.execute("INSERT INTO silver_encounter VALUES (?,?,?,?,?)", (
                r["id"], r["subject"]["reference"].split("/")[1],
                r["class"]["code"], r["period"]["start"], r["period"]["end"]))
        elif rt == "Observation":
            coding = r["code"]["coding"][0]
            cur.execute("INSERT INTO silver_observation VALUES (?,?,?,?,?,?,?,?)", (
                r["id"], r["subject"]["reference"].split("/")[1],
                r["encounter"]["reference"].split("/")[1],
                coding["code"], coding["display"],
                round(r["valueQuantity"]["value"], 4), r["valueQuantity"]["unit"],
                r["effectiveDateTime"]))
        elif rt == "Condition":
            codings = {c.get("system", ""): c for c in r["code"]["coding"]}
            snomed = codings.get("http://snomed.info/sct", {})
            icd = codings.get("http://hl7.org/fhir/sid/icd-10", {})
            cur.execute("INSERT INTO silver_condition VALUES (?,?,?,?,?,?)", (
                r["id"], r["subject"]["reference"].split("/")[1],
                snomed.get("code"), icd.get("code"), snomed.get("display"),
                r["clinicalStatus"]["coding"][0]["code"]))
        elif rt == "Consent":
            cur.execute(
                "INSERT INTO consent_ledger (consent_id, patient_id, purpose, status, notice_version, effective_from) "
                "VALUES (?,?,?,?,?,?)", (
                    r["id"], r["patient"]["reference"].split("/")[1],
                    r["purpose"], r["status"], r["noticeVersion"], r["dateTime"]))

    cur.executemany("INSERT INTO classification_catalog VALUES (?,?,?)", COLUMN_TAGS)


def build_gold(conn) -> None:
    """Analytics-ready summary. Note: only PHI_INDIRECT/CLINICAL columns —
    direct identifiers stay out of the analytics path by design."""
    conn.execute("DROP VIEW IF EXISTS gold_patient_summary")
    conn.execute("""
        CREATE VIEW gold_patient_summary AS
        SELECT p.patient_id,
               p.gender,
               year(current_date) - CAST(p.birth_date[1:4] AS INT) AS age,
               c.display AS primary_condition,
               COUNT(DISTINCT e.encounter_id) AS encounter_count,
               COUNT(o.observation_id) AS observation_count,
               ROUND(AVG(CASE WHEN o.loinc_code='4548-4' THEN o.value END), 2) AS avg_hba1c
        FROM silver_patient p
        LEFT JOIN silver_condition c ON c.patient_id = p.patient_id
        LEFT JOIN silver_encounter e ON e.patient_id = p.patient_id
        LEFT JOIN silver_observation o ON o.patient_id = p.patient_id
        GROUP BY p.patient_id, p.gender, p.birth_date, c.display""")


def main() -> None:
    bundle = json.loads((ROOT / "data" / "source_bundle.json").read_text())
    conn = get_conn()
    build_bronze(conn, bundle)
    build_silver(conn)
    build_gold(conn)
    conn.commit()
    for t in ["bronze_raw", "silver_patient", "silver_observation", "consent_ledger"]:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{t}: {n} rows")
    conn.close()
    print(f"Database ready: {DB_PATH.name}")


if __name__ == "__main__":
    main()
