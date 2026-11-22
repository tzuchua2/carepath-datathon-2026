"""Attach reviewed, version-specific source references to the original record and service."""

import csv
import json
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pyspark.sql import functions as F

REFERENCE_CSV = Path(__file__).resolve().parents[1] / "references" / "verified_service_sources.csv"
IDENTITY_COLUMNS = ["record_key", "unique_id", "name", "address_city", "address_stateOrRegion", "pincode"]
REFERENCE_COLUMNS = [
    "reference_id", *IDENTITY_COLUMNS, "service", "source_title", "source_url", "source_locator",
    "source_role", "source_period", "checked_on", "reported_facility", "claim", "branch_source_url",
    "branch_check", "external_location_conflict", "publisher_domains",
]
OUTPUT_SCHEMA = ", ".join(
    f"{column} {kind}" for column, kind in [
        *((column, "string") for column in REFERENCE_COLUMNS if column not in {
            "publisher_domains", "external_location_conflict", "checked_on"
        }),
        ("checked_on", "date"), ("external_location_conflict", "boolean"),
        ("identity_review", "boolean"), ("location_review", "boolean"),
        ("facility_text_status", "string"), ("local_eligible", "boolean"),
        ("source_overlap", "string"), ("comparison_status", "string"), ("availability_status", "string"),
    ]
)


def _url_parts(url):
    """Treat http/https, www and fragments alike, preserving query values and path case."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if parsed.scheme.lower() not in {"http", "https"} or not host:
            return None
        path = unquote(parsed.path).rstrip("/")
        return host, parsed.port, path, parsed.query
    except (TypeError, ValueError):
        return None


def _overlap(reference, listed_urls):
    try:
        urls = json.loads(listed_urls or "[]")
    except (TypeError, ValueError):
        urls = []
    parsed = [p for u in (urls if isinstance(urls, list) else []) if isinstance(u, str)
              and (p := _url_parts(u)) is not None]
    source = _url_parts(reference["source_url"])
    if not parsed:
        return "no_urls_to_compare"
    if source in parsed:
        return "same_url_listed"
    if any(p[0] == source[0] for p in parsed):
        return "same_host_listed"
    domains = reference["publisher_domains"].split("|")
    if any(host == domain or host.endswith("." + domain)
           for host, *_ in parsed for domain in domains):
        return "related_publisher_domain_listed"
    return "no_overlap_detected_in_listed_urls"


def build_external_service_matches(facilities, service_evidence, reference_path=REFERENCE_CSV):
    """Build the small manually reviewed citation crosswalk without altering local eligibility.

    Unmatched fingerprints, IDs or branch fields suppress the reference on new snapshots.
    Re-review the external source before adding the replacement record to the CSV.
    """
    with Path(reference_path).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != REFERENCE_COLUMNS:
            raise ValueError("Unexpected external service reference columns")
        references = list(reader)
    if len({r["reference_id"] for r in references}) != len(references):
        raise ValueError("Duplicate external service reference ID")
    spark = facilities.sparkSession
    # A small, driver-maintained set of source observations. Select only their challenge keys.
    keys = {r["record_key"] for r in references}
    if not keys:
        return spark.createDataFrame([], OUTPUT_SCHEMA)
    local_facilities = facilities.where(F.col("record_key").isin(sorted(keys))).select(
        *IDENTITY_COLUMNS, "source_urls", "identity_review", "location_review"
    ).collect()
    by_key = {r.record_key: r for r in local_facilities}
    if len(by_key) != len(local_facilities):
        raise ValueError("Duplicate challenge record key in external crosswalk")
    pairs = service_evidence.where(F.col("record_key").isin(sorted(keys))).select(
        "record_key", "service", "evidence_status", "eligible"
    ).collect()
    by_pair = {(r.record_key, r.service): r for r in pairs}
    if len(by_pair) != len(pairs):
        raise ValueError("Duplicate local facility/service pair in external crosswalk")

    output = []
    for ref in references:
        if _url_parts(ref["source_url"]) is None or _url_parts(ref["branch_source_url"]) is None:
            raise ValueError(f"Missing or invalid reference URL in {ref['reference_id']}")
        if ref["external_location_conflict"] not in {"true", "false"}:
            raise ValueError(f"Invalid external location flag in {ref['reference_id']}")
        local = by_key.get(ref["record_key"])
        if local is None or any(local[c] != ref[c] for c in IDENTITY_COLUMNS):
            continue
        evidence = by_pair.get((ref["record_key"], ref["service"]))
        status = evidence.evidence_status if evidence is not None else None
        eligible = evidence.eligible if evidence is not None else None
        overlap = _overlap(ref, local.source_urls)
        location_conflict = ref["external_location_conflict"] == "true"
        if location_conflict or local.identity_review or local.location_review or (status == "documented" and not eligible):
            comparison = "record_requires_review"
        elif status is None:
            comparison = "external_only_lead"
        elif status == "suggestive":
            comparison = "external_with_indirect_local_text"
        elif status != "documented":
            comparison = "compare_qualified_or_conflicting_text"
        elif overlap == "same_url_listed":
            comparison = "documented_with_same_url_listed"
        elif ref["source_role"].startswith("provider_"):
            comparison = "documented_with_provider_reference"
        else:
            comparison = "documented_with_external_reference"

        output.append({
            **{k: v for k, v in ref.items() if k != "publisher_domains"},
            "checked_on": date.fromisoformat(ref["checked_on"]),
            "external_location_conflict": location_conflict,
            "identity_review": local.identity_review, "location_review": local.location_review,
            "facility_text_status": status, "local_eligible": eligible, "source_overlap": overlap,
            "comparison_status": comparison, "availability_status": "unverified",
        })
    return spark.createDataFrame(output, OUTPUT_SCHEMA)
