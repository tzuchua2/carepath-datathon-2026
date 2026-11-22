"""Reconcile the distributed Parquet tables with their original source files."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import re

import pyarrow.dataset as ds

from backend.audit_facilities import audit_facilities
from backend.audit_geography import audit_geography


def _table(directory, name):
    path = directory / (name + ".parquet")
    return ds.dataset(path if path.exists() else directory / name, format="parquet")


def _normalized_name(text):
    return " ".join(re.findall(r"[^\W_]+", text.lower().replace("&", " and ")))


def audit_snapshot(raw, derived):
    facility = audit_facilities(raw, derived)
    geography = audit_geography(raw, derived)
    clean = _table(derived, "clean_facilities").to_table(
        columns=["record_key", "name_key", "pincode", "state_key", "identity_review", "location_review"]
    ).to_pylist()
    gold = _table(derived, "recommendation_candidates").to_table(columns=["service"]).column(0).to_pylist()
    external = _table(derived, "external_corroboration").to_table().to_pylist()
    reference_path = Path(__file__).resolve().parents[1] / "references" / "delhi_dialysis_programmes.csv"
    with reference_path.open(newline="", encoding="utf-8") as source:
        sites = list(csv.DictReader(source))
    expected = {}
    for site in sites:
        names = {_normalized_name(site[field]) for field in ("reported_name", "directory_name")}
        for f in clean:
            if f["state_key"] == "delhi" and f["pincode"] == site["official_pin"] and f["name_key"] in names:
                expected[(f["record_key"], site["programme"], int(site["report_row"]))] = site
    for match in external:
        key = (match["record_key"], match["programme"], match["report_row"])
        site = expected.pop(key, None)
        if site is None or match["service"] != "dialysis" or any(
            match[field] != site[field] for field in ("reported_name", "official_pin", "reported_district", "address_source_url")
        ):
            raise ValueError(f"External programme listing or branch mismatch for {key}")
    if expected:
        raise ValueError(f"Missing {len(expected)} external programme matches")

    metrics = {
        "source_facility_rows": facility["original_facility_rows"],
        "source_postal_rows": _table(raw, "india_post_pincode_directory").count_rows(),
        "source_district_rows": _table(raw, "nfhs_5_district_health_indicators").count_rows(),
        "postal_keys": _table(derived, "postal_reference").count_rows(),
        "district_indicator_rows": _table(derived, "district_indicators").count_rows(),
        "clean_facility_records": len(clean), "clean_source_rows": facility["clean_facilities_source_rows"],
        "quarantined_source_rows": facility["facility_quarantine_source_rows"],
        "identity_review_records": sum(r["identity_review"] for r in clean),
        "location_review_records": sum(r["location_review"] for r in clean),
        "service_evidence_rows": facility["service_evidence_rows"],
        "external_reference_matches": len(external), "candidate_facility_service_pairs": len(gold),
        "distinct_candidate_facilities": facility["distinct_candidate_facilities"],
        **{"candidate_pairs_" + service: n for service, n in Counter(gold).items()},
    }
    summary = _table(derived, "build_summary").to_table().to_pylist()
    if len(summary) != len(metrics) or {r["metric"]: r["value"] for r in summary} != metrics:
        raise ValueError("Build summary disagrees with row-level audit")
    return {"facility": facility, "geography": geography, "metrics": metrics}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("data"))
    parser.add_argument("--derived", type=Path, default=Path("data/derived"))
    args = parser.parse_args()
    print(json.dumps(audit_snapshot(args.raw, args.derived), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
