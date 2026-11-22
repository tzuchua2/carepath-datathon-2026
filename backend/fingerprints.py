"""Order-independent Spark fingerprints for comparing local and Databricks tables."""

from pyspark.sql import functions as F


SOURCE_TABLES = ("facilities", "india_post_pincode_directory", "nfhs_5_district_health_indicators")
DERIVED_TABLES = ("postal_reference", "district_indicators", "facility_quarantine", "clean_facilities",
                  "service_evidence", "external_corroboration", "recommendation_candidates", "build_summary")


def table_fingerprint(frame):
    # Transcendental functions in different engines can differ at machine precision.
    # Compare the two calculated distance fields to six decimals in kilometres (1 mm).
    fields = [
        (F.round(F.col(name), 6).cast("decimal(20,6)").alias(name)
         if name in {"coordinate_span_km", "pin_distance_km"} else F.col(name))
        for name in sorted(frame.columns)
    ]
    payload = F.to_json(F.struct(*fields), options={"ignoreNullFields": "false", "timeZone": "UTC"})
    rows = frame.select(F.sha2(payload, 256).alias("digest"))
    # The prefix bounds each sorted list. Repeated input rows contribute repeated digests.
    buckets = rows.groupBy(F.substring("digest", 1, 2).alias("prefix")).agg(
        F.sha2(F.concat_ws("", F.sort_array(F.collect_list("digest"))), 256).alias("bucket_hash")
    )
    return buckets.select(F.concat("prefix", "bucket_hash").alias("entry")).agg(
        F.sha2(F.concat_ws("", F.sort_array(F.collect_list("entry"))), 256).alias("digest")
    ).first()["digest"]


def snapshot_fingerprints(source, derived):
    return {**{name: table_fingerprint(source(name)) for name in SOURCE_TABLES},
            **{name: table_fingerprint(derived(name)) for name in DERIVED_TABLES}}
