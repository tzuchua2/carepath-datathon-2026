"""Compare a dated Delhi government haemodialysis list with facility records."""

import csv
from pathlib import Path

from pyspark.sql import Window, functions as F

from backend.geography import place_key
from backend.service_evidence import build_service_evidence


REFERENCE_CSV = Path(__file__).resolve().parents[1] / "references" / "delhi_dialysis_programmes.csv"
REPORT_URL = "https://dgehs.delhi.gov.in/sites/default/files/inline-files/ar25.pdf#page=87"
REPORT_TITLE = "Delhi Directorate General of Health Services, Annual Report 2024–25"


def build_corroboration(facilities, service_evidence=None, reference_path=REFERENCE_CSV):
    """Return candidate matches, keyed by record_key and service, with an external citation.

    Input is the output of build_facilities. Pass the existing build_service_evidence
    result when available. Otherwise the extractor runs on matched records only.
    Missing matches carry no information about a facility's dialysis services.
    """
    with Path(reference_path).open(encoding="utf-8", newline="") as source:
        values = list(csv.DictReader(source))
    reference = facilities.sparkSession.createDataFrame(
        values,
        "reported_name STRING, directory_name STRING, reported_district STRING, "
        "official_pin STRING, programme STRING, report_row STRING, "
        "reported_start_month STRING, address_source_url STRING",
    )
    # Only names printed by one of the two official sources are eligible.
    reference = reference.withColumn(
        "name_key",
        F.explode(F.array_distinct(F.array(place_key("reported_name"), place_key("directory_name")))),
    ).withColumnRenamed("official_pin", "pincode")
    candidates = (
        facilities.where(F.col("state_key") == "delhi")
        .select("record_key", "unique_id", "name_key", "pincode", "identity_review",
                "location_review", "source_urls")
        .join(F.broadcast(reference), ["name_key", "pincode"])
    )
    if service_evidence is None:
        target = facilities.join(candidates.select("record_key"), "record_key", "left_semi")
        service_evidence = build_service_evidence(target)
    local = service_evidence.where(F.col("service") == "dialysis").select(
        "record_key", F.col("evidence_status").alias("facility_text_status"), "eligible"
    )
    result = candidates.join(local, "record_key", "left")
    overlapping_rows = F.count("*").over(Window.partitionBy("programme", "report_row")) > 1
    held_local_claim = (F.col("facility_text_status") == "documented") & ~F.col("eligible")
    needs_review = (overlapping_rows | F.col("identity_review") | F.col("location_review")
                    | F.coalesce(held_local_claim, F.lit(False)))

    # Distinguish a reused document from an overlapping government publisher.
    # URL lists on the challenge record have no sentence-to-source mapping.
    urls = F.lower(F.coalesce(F.col("source_urls").cast("string"), F.lit("")))
    government_url = r"https?://(?:[a-z0-9-]+\.)*(?:delhi\.gov\.in|delhigovt\.nic\.in)(?=[/:?#\s\"\],]|$)"
    same_report = government_url + r"[^\s\"\],]*?/ar25\.pdf(?=[?#\s\"\],]|$)"
    overlap = (
        F.when(urls.rlike(same_report), "same_report_listed")
        .when(urls.rlike(government_url), "delhi_government_source_listed")
        .when(~urls.rlike(r"https?://"), "no_source_urls_to_compare")
        .otherwise("no_overlap_detected_in_listed_urls")
    )
    relation = (
        F.when(needs_review, "record_requires_review")
        .when(F.col("facility_text_status").isNull(), "external_only_lead")
        .when((F.col("facility_text_status") == "documented") & (overlap == "same_report_listed"),
              "record_already_cites_report")
        .when(F.col("facility_text_status") == "documented", "documented_in_record_and_report")
        .when(F.col("facility_text_status") == "suggestive", "external_listing_with_indirect_local_text")
        .otherwise("compare_conflicting_or_qualified_text")
    )
    return result.select(
        "record_key", "unique_id", F.lit("dialysis").alias("service"),
        "reported_name", "reported_district", F.col("pincode").alias("official_pin"),
        "programme", F.col("report_row").cast("int").alias("report_row"),
        F.nullif(F.col("reported_start_month"), F.lit("")).alias("reported_start_month"),
        F.lit(REPORT_TITLE).alias("source_title"), F.lit(REPORT_URL).alias("source_url"),
        F.lit("Section 4.19, printed page 82").alias("source_location"), "address_source_url",
        F.when(overlapping_rows, "multiple_records_match_name_and_pin")
        .otherwise("exact_official_name_delhi_and_pin").alias("identity_match"),
        "facility_text_status", relation.alias("comparison_status"),
        overlap.alias("source_overlap"),
    )
