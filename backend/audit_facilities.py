"""Independently reconcile a facility snapshot and trace its recommendation data."""

from collections import Counter
import csv
import json
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit

import pyarrow as pa
import pyarrow.dataset as ds


_CITABLE = {"description", "procedure", "capability", "equipment", "specialties"}
_BLOCKERS = {"identity_review", "location_review", "other_named_site",
             "specialty_name_mismatch", "missing_record_source_url",
             "unclear_negation_scope", "conflicting_service_statements",
             "education_provider_review", "branch_attribution_unclear", "manual_review", "review_source_changed"}
_UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I)


def _table(directory, name):
    directory = Path(directory)
    flat = directory / (name + ".parquet")
    return ds.dataset(flat if flat.exists() else directory / name, format="parquet")


def _rows(table, columns=None):
    for batch in table.to_batches(columns=columns, batch_size=256):
        batch.validate(full=True)
        yield from batch.to_pylist()


def _check(ok, message):
    if not ok:
        raise ValueError(message)


def _payload(row, schema):
    # Compare all original cells together. IEEE bytes preserve signed zero and
    # distinguish NaN from a null. Tuple keys keep true nulls distinct from text.
    return tuple(struct.pack(">d", row[f.name]) if row[f.name] is not None and pa.types.is_floating(f.type)
                 else row[f.name] for f in schema)


def _decoded(raw):
    if isinstance(raw, str) and raw.lstrip().startswith(("[", '"', "{")):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def _urls(raw):
    decoded = _decoded(raw.strip() if isinstance(raw, str) else raw)
    values = decoded if isinstance(decoded, list) else [decoded]
    result = []
    for value in values:
        if not isinstance(value, str) or any(c.isspace() for c in value):
            continue
        try:
            parts = urlsplit(value)
            if parts.scheme in {"https", "http"} and parts.hostname and "." in parts.hostname and not parts.username:
                result.append(value)
        except ValueError:
            continue
    return list(dict.fromkeys(result))


def _quarantine_reasons(row):
    reasons = set()
    if not _UUID.fullmatch(row["unique_id"] or ""):
        reasons.add("invalid_record_id")
    if (row["organization_type"] or "").strip().lower() != "facility":
        reasons.add("invalid_record_type")
    if (row["name"] or "").strip().lower() in {"", "null", "nan", "none", "n/a", "[]"}:
        reasons.add("missing_facility_name")
    return reasons


def _check_citations(row, source):
    key = (row["record_key"], row["service"])
    citations = row["citations"]
    _check(citations, f"No citation for {key}")
    for citation in citations:
        field, index, quote = (citation[k] for k in ("field", "item_index", "quote"))
        _check(field in _CITABLE and isinstance(quote, str) and quote.strip(), f"Invalid citation for {key}")
        cell = _decoded(source[field])
        if index is not None:
            _check(isinstance(cell, list) and 0 <= index < len(cell), f"Invalid item index in {field} for {key}")
            cell = cell[index]
        _check(isinstance(cell, str) and quote in cell, f"Citation absent from {field}[{index}] for {key}")
    return len(citations)


def _status(kinds):
    limitations = kinds & {"negated", "referral", "planned"}
    if kinds & {"other_site", "unclear_negation", "branch_unclear"}:
        return "review"
    if len(limitations) > 1 or (limitations and "direct" in kinds):
        return "mixed"
    if limitations:
        return "referral_only" if "referral" in limitations else next(iter(limitations))
    return "documented" if "direct" in kinds else "suggestive"


def audit_facilities(raw_dir, derived_dir):
    """Compare exact raw payloads/multiplicities, citations and candidate joins. Raise on any mismatch."""
    raw = _table(raw_dir, "facilities")
    remaining = Counter(_payload(row, raw.schema) for row in _rows(raw))
    raw_rows = remaining.total()
    keys = set()
    facilities = {}
    counts = {}
    for name in ("clean_facilities", "facility_quarantine"):
        table = _table(derived_dir, name)
        for field in raw.schema:
            _check(field.name in table.schema.names and table.schema.field(field.name).type == field.type,
                   f"Missing or changed raw column {field.name} in {name}")
        total = records = 0
        for row in _rows(table):
            key = row["record_key"]
            _check(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) and key not in keys,
                   f"Invalid or duplicate record key in {name}: {key}")
            keys.add(key)
            multiplicity = row["source_row_count"]
            _check(type(multiplicity) is int and multiplicity > 0, f"Invalid source count for {key}")
            found = remaining.pop(_payload(row, raw.schema), None)
            _check(found == multiplicity, f"Raw values, multiplicity or partition disagree for {key}")
            reasons = _quarantine_reasons(row)
            if name == "facility_quarantine":
                _check(reasons and set(row["quarantine_reasons"]) == reasons, f"Wrong quarantine reasons for {key}")
            else:
                _check(not reasons, f"Invalid facility in clean table: {key}")
                for flag in ("identity", "location"):
                    _check(row[flag + "_review"] is bool(row[flag + "_flags"]), f"Review indicator mismatch for {key}")
                facilities[key] = row
            total += multiplicity
            records += 1
        counts[name + "_source_rows"] = total
        counts[name + "_records"] = records
    _check(not remaining, f"{sum(remaining.values())} original facility rows could not be recovered")

    # Check the full evidence table, including claims deliberately held for review.
    evidence = _table(derived_dir, "service_evidence")
    review_fields = {"manual_review_reason", "reviewed_record_key"}
    has_reviews = review_fields.issubset(evidence.schema.names)
    _check(has_reviews or review_fields.isdisjoint(evidence.schema.names), "Partial manual review schema")
    holds = {}
    if has_reviews:
        path = Path(__file__).resolve().parents[1] / "data" / "quality" / "review_holds.csv"
        with path.open(encoding="utf-8", newline="") as source:
            for hold in csv.DictReader(source):
                key = (hold["unique_id"].lower(), hold["service"])
                _check(key not in holds and hold["manual_review_reason"].strip()
                       and re.fullmatch(r"[0-9a-f]{64}", hold["reviewed_record_key"]), f"Invalid review hold: {key}")
                holds[key] = hold
    held_rows = 0
    eligible = {}
    seen = set()
    citations = 0
    for row in _rows(evidence):
        pair = (row["record_key"], row["service"])
        source = facilities.get(pair[0])
        _check(source is not None and row["unique_id"] == source["unique_id"] and pair not in seen,
               f"Unmatched or duplicate evidence key {pair}")
        seen.add(pair)
        citations += _check_citations(row, source)
        _check(row["record_source_urls"] == _urls(source["source_urls"]), f"Record URLs differ for {pair}")
        _check(row["availability_status"] == "unverified", f"Unexpected availability assertion for {pair}")
        page = source["usable_page_date"]
        _check(row["page_update_date"] == (page.isoformat() if page else None), f"Page date mismatch for {pair}")
        flags = set(row["flags"])
        hold = holds.get((row["unique_id"].lower(), row["service"]))
        _check(("manual_review" in flags) == bool(hold), f"Manual review flag differs from reviewed cases for {pair}")
        stale = bool(hold and pair[0] != hold["reviewed_record_key"])
        _check(("review_source_changed" in flags) == stale, f"Stale review flag mismatch for {pair}")
        if has_reviews:
            for field in review_fields:
                expected = hold[field] if hold else None
                _check(row[field] == expected, f"Incorrect {field} for {pair}")
        held_rows += bool(hold)
        _check(not source["identity_review"] or "identity_review" in flags, f"Missing identity flag for {pair}")
        _check(not source["location_review"] or "location_review" in flags, f"Missing location flag for {pair}")
        kinds = {citation["kind"] for citation in row["citations"]}
        status = _status(kinds)
        _check(row["evidence_status"] == status, f"Evidence status contradicts cited kinds for {pair}")
        qualifies = bool(status == "documented" and not (flags & _BLOCKERS)
                         and "identity_conflict" not in kinds and row["record_source_urls"]
                         and source["city_key"] and source["state_key"])
        _check(row["eligible"] is qualifies, f"Eligibility contradicts evidence/location gates for {pair}")
        if qualifies:
            eligible[pair] = row

    candidate_count = len(eligible)
    candidate_facilities = len({key for key, service in eligible})
    candidate_table = _table(derived_dir, "recommendation_candidates")
    aliases = {"reported_phone": "officialPhone", "reported_website": "officialWebsite"}
    location_fields = {"name", "city_key", "state_key", "pincode", "address_line1", "address_city",
                       "address_stateOrRegion", "postal_district_key", "usable_latitude", "usable_longitude",
                       "pin_distance_km", "pin_geography_status", "quality_notes", "location_basis"}
    required = set(evidence.schema.names) | location_fields | set(aliases)
    _check(required.issubset(candidate_table.schema.names), "Candidate table is missing handoff fields")
    for candidate in _rows(candidate_table):
        pair = (candidate["record_key"], candidate["service"])
        claim = eligible.pop(pair, None)
        _check(claim is not None, f"Duplicate candidate or candidate missing eligible evidence: {pair}")
        source = facilities[pair[0]]
        for field, value in candidate.items():
            if field == "location_basis":
                expected = ("reported_point_compared_with_postal_area" if source["pin_distance_km"] is not None
                            else "reported_city_and_state")
            elif field in claim:
                expected = claim[field]
            else:
                expected = source[aliases.get(field, field)]
            _check(value == expected, f"Candidate field {field} disagrees for {pair}")
    _check(not eligible, f"{len(eligible)} eligible facility/service pairs missing from candidate table")

    return {
        **counts, "original_facility_rows": raw_rows, "original_facility_columns": len(raw.schema),
        "original_facility_cells_reconciled": len(raw.schema) * raw_rows,
        "service_evidence_rows": len(seen), "citations_matched": citations, "manual_hold_rows": held_rows,
        "candidate_pairs": candidate_count, "distinct_candidate_facilities": candidate_facilities,
    }
