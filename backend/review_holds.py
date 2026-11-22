"""Apply versioned human review decisions before generating recommendation candidates."""

import csv
from pathlib import Path
import re
from uuid import UUID

from pyspark.sql import functions as F

from backend.service_evidence import SERVICES


REVIEW_HOLDS_CSV = Path(__file__).resolve().parents[1] / "data" / "quality" / "review_holds.csv"
_FIELDS = ("unique_id", "service", "reviewed_record_key", "source_fields", "manual_review_reason")


def load_review_holds(path):
    with Path(path).open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(_FIELDS):
            raise ValueError(f"Expected review_holds.csv fields {', '.join(_FIELDS)}")
        rows = list(reader)
    seen = set()
    for row in rows:
        if None in row or any(not row[field] or not row[field].strip() for field in _FIELDS):
            raise ValueError("Every manual hold needs a complete ID, service, reviewed key, source fields and reason")
        facility_id = row["unique_id"]
        if str(UUID(facility_id)) != facility_id or row["service"] not in SERVICES:
            raise ValueError(f"Invalid manual hold ID or service: {facility_id}, {row['service']}")
        if not re.fullmatch("[0-9a-f]{64}", row["reviewed_record_key"]):
            raise ValueError(f"Invalid reviewed record hash for {facility_id}")
        key = (facility_id, row["service"])
        if key in seen:
            raise ValueError(f"Duplicate manual hold for {key}")
        seen.add(key)
    return rows


def apply_review_holds(evidence, path=REVIEW_HOLDS_CSV):
    """Keep the extracted evidence visible while holding the scoped facility and service.

    A changed source payload retains its hold until the reviewer revisits it.
    Resolving a hold means updating this CSV in a reviewed Git change and rebuilding.
    """
    rows = load_review_holds(path)
    holds = evidence.sparkSession.createDataFrame(rows, ", ".join(f"{field} STRING" for field in _FIELDS))
    holds = holds.select(F.col("unique_id").alias("review_unique_id"), "service",
                         "reviewed_record_key", "manual_review_reason")
    result = evidence.withColumn("review_unique_id", F.lower("unique_id")).join(
        F.broadcast(holds), ["review_unique_id", "service"], "left"
    ).drop("review_unique_id")
    held = F.col("reviewed_record_key").isNotNull()
    changed = held & ~F.col("record_key").eqNullSafe(F.col("reviewed_record_key"))
    hold_flags = F.when(changed, F.array(F.lit("manual_review"), F.lit("review_source_changed"))).otherwise(
        F.array(F.lit("manual_review"))
    )
    return result.withColumns({
        "eligible": F.when(held, F.lit(False)).otherwise(F.col("eligible")),
        "flags": F.when(held, F.array_union(F.coalesce("flags", F.array().cast("array<string>")), hold_flags))
        .otherwise(F.col("flags")),
    })
