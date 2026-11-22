"""Build a versioned set of tables and validate the handoff to recommendations."""

from pyspark.sql import functions as F

from backend.corroboration import build_corroboration
from backend.facilities import build_facilities, prepare_source
from backend.geography import clean_district_survey, clean_postal_directory
from backend.review_holds import apply_review_holds
from backend.service_evidence import build_service_evidence


def candidate_rows(facilities, evidence):
    locations = facilities.select(
        "record_key", "name", "city_key", "state_key", "pincode", "address_line1", "address_city",
        "address_stateOrRegion", "postal_district_key", "usable_latitude", "usable_longitude",
        "pin_distance_km", "pin_geography_status", "quality_notes",
        F.col("officialPhone").alias("reported_phone"), F.col("officialWebsite").alias("reported_website"),
    )
    return evidence.where(F.col("eligible")).join(locations, "record_key").withColumn(
        "location_basis",
        F.when(F.col("pin_distance_km").isNotNull(), "reported_point_compared_with_postal_area")
        .otherwise("reported_city_and_state"),
    )


def build_snapshot(spark, source, save, as_of):
    """save(name, data) must write into a fresh snapshot and return the stored table."""
    raw = source("facilities")
    offices = source("india_post_pincode_directory")
    nfhs = source("nfhs_5_district_health_indicators")
    postal = save("postal_reference", clean_postal_directory(offices))
    survey = save("district_indicators", clean_district_survey(nfhs))
    prepared = prepare_source(raw)
    quarantine = save("facility_quarantine", prepared.where(F.size("quarantine_reasons") > 0))
    facilities = save("clean_facilities", build_facilities(prepared, postal, as_of))
    evidence = save("service_evidence", apply_review_holds(build_service_evidence(facilities)))
    external = save("external_corroboration", build_corroboration(facilities, evidence))
    candidates = save("recommendation_candidates", candidate_rows(facilities, evidence))

    def source_count(frame):
        return frame.agg(F.sum("source_row_count")).first()[0] or 0

    metrics = {
        "source_facility_rows": raw.count(), "source_postal_rows": offices.count(),
        "source_district_rows": nfhs.count(), "postal_keys": postal.count(),
        "district_indicator_rows": survey.count(), "clean_facility_records": facilities.count(),
        "quarantined_source_rows": source_count(quarantine), "clean_source_rows": source_count(facilities),
        "identity_review_records": facilities.where("identity_review").count(),
        "location_review_records": facilities.where("location_review").count(),
        "service_evidence_rows": evidence.count(), "external_reference_matches": external.count(),
        "candidate_facility_service_pairs": candidates.count(),
        "distinct_candidate_facilities": candidates.select("record_key").distinct().count(),
    }
    if metrics["clean_source_rows"] + metrics["quarantined_source_rows"] != metrics["source_facility_rows"]:
        raise ValueError("Source facility rows lost or multiplied")
    if metrics["postal_keys"] != postal.select("pincode").distinct().count():
        raise ValueError("Postal lookup contains a non-unique key")
    if metrics["clean_facility_records"] != facilities.select("record_key").distinct().count():
        raise ValueError("Cleaned facility keys are not unique")
    if metrics["service_evidence_rows"] != evidence.select("record_key", "service").distinct().count():
        raise ValueError("Multiple evidence rows for one record and service")
    if metrics["candidate_facility_service_pairs"] != evidence.where("eligible").count():
        raise ValueError("The candidate join lost or multiplied eligible evidence")
    invalid = candidates.where(
        (F.col("evidence_status") != "documented") | (F.col("availability_status") != "unverified")
        | ~F.exists("citations", lambda c: (c.kind == "direct") & (F.length(c.quote) > 0))
        | (F.size("record_source_urls") == 0)
        | F.array_contains("flags", "location_review") | F.array_contains("flags", "identity_review")
        | F.array_contains("flags", "manual_review")
        | F.col("state_key").isNull() | F.col("city_key").isNull()
    )
    if invalid.limit(1).count():
        raise ValueError("A candidate failed its evidence or location requirements")
    # Consumers can distinguish a finished snapshot from a partially written run.
    for row in candidates.groupBy("service").count().collect():
        metrics["candidate_pairs_" + row.service] = row["count"]
    save("build_summary", spark.createDataFrame(
        [(metric, int(value), as_of) for metric, value in sorted(metrics.items())],
        "metric STRING, value LONG, as_of DATE",
    ))
    return metrics
