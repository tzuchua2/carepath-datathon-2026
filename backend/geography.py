"""Clean the two challenge reference tables with Spark SQL expressions.

The postal result has one row per PIN. The survey result has one row per
state_key, district_key and indicator. Filter to one indicator before joining
a district measure to facility rows.
"""

from pyspark.sql import Column, DataFrame, functions as F


_STATES = (
    "andaman and nicobar islands", "andhra pradesh", "arunachal pradesh", "assam",
    "bihar", "chandigarh", "chhattisgarh", "dadra and nagar haveli and daman and diu",
    "delhi", "goa", "gujarat", "haryana", "himachal pradesh", "jammu and kashmir",
    "jharkhand", "karnataka", "kerala", "ladakh", "lakshadweep", "madhya pradesh",
    "maharashtra", "manipur", "meghalaya", "mizoram", "nagaland", "odisha",
    "puducherry", "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana",
    "tripura", "uttar pradesh", "uttarakhand", "west bengal",
)
_STATE_ALIASES = {
    "a p": "andhra pradesh",
    "chattisgarh": "chhattisgarh",
    "j and k": "jammu and kashmir",
    "m p": "madhya pradesh",
    "maharastra": "maharashtra",
    "nct delhi": "delhi",
    "nct of delhi": "delhi",
    "new delhi": "delhi",
    "orissa": "odisha",
    "pondicherry": "puducherry",
    "tamilnadu": "tamil nadu",
    "the dadra and nagar haveli and daman and diu": "dadra and nagar haveli and daman and diu",
    "u p": "uttar pradesh",
    "up": "uttar pradesh",
}


def _column(value: Column | str) -> Column:
    return F.col(value) if isinstance(value, str) else value


def place_key(column: Column | str) -> Column:
    """Return a lowercase place name with normalized punctuation, or SQL null."""
    text = F.lower(F.trim(_column(column).cast("string")))
    words = F.regexp_replace(text, "&", " and ")
    key = F.trim(F.regexp_replace(words, r"[^\p{L}\p{N}]+", " "))
    return F.when(~key.isin("", "na", "n a", "null", "none", "nan", "unknown"), key)


def state_key(column: Column | str) -> Column:
    """Resolve recognized Indian state/UT labels. Unrecognized labels stay null."""
    labels = {**{name: name for name in _STATES}, **_STATE_ALIASES}
    entries = [F.lit(item) for pair in labels.items() for item in pair]
    return F.element_at(F.create_map(*entries), place_key(column))


def pincode_key(column: Column | str) -> Column:
    """Return a six-digit Indian PIN beginning with 1–9, or SQL null."""
    key = F.trim(_column(column).cast("string"))
    return F.when(key.rlike(r"^[1-9][0-9]{5}$"), key)


def clean_postal_directory(raw: DataFrame, max_span_km: float = 50.0) -> DataFrame:
    """Return one row per valid PIN with location candidates and quality flags.

    Coordinates are office-based approximations. A broad India bounding box
    excludes obvious coordinate errors. max_span_km is a configurable safeguard
    on the approximate bounding-box diagonal of eligible office coordinates.
    Incomplete or conflicting geography disables the map point. Coordinates
    repeated across three or more states and three or more PINs cannot safely
    identify one site and are excluded before calculating the spread and medians.
    """
    if not 0 < max_span_km < float("inf"):
        raise ValueError("max_span_km must be positive and finite")

    offices = raw.select(
        pincode_key("pincode").alias("pincode"),
        state_key("statename").alias("state_key"),
        place_key("district").alias("district_key"),
        F.expr("try_cast(latitude AS DOUBLE)").alias("lat"),
        F.expr("try_cast(longitude AS DOUBLE)").alias("lon"),
    ).where(F.col("pincode").isNotNull())
    has_place = F.col("state_key").isNotNull() & F.col("district_key").isNotNull()
    has_point = has_place & F.col("lat").between(6.0, 38.0) & F.col("lon").between(67.0, 99.0)
    shared = (offices.where(has_point).groupBy("lat", "lon")
              .agg(F.countDistinct("state_key").alias("states"), F.countDistinct("pincode").alias("pins"))
              .where((F.col("states") >= 3) & (F.col("pins") >= 3))
              .select("lat", "lon", F.lit(True).alias("shared_coordinate")))
    offices = offices.join(shared, ["lat", "lon"], "left")
    shared_point = has_point & F.coalesce("shared_coordinate", F.lit(False))
    usable_point = has_point & ~shared_point
    offices = offices.select(
        "*",
        F.when(has_place, F.struct("state_key", "district_key")).alias("district_pair"),
        F.when(shared_point, 1).alias("shared_point"),
        F.when(usable_point, F.col("lat")).alias("valid_lat"),
        F.when(usable_point, F.col("lon")).alias("valid_lon"),
    )
    pins = offices.groupBy("pincode").agg(
        F.count("*").alias("office_row_count"),
        F.count("district_pair").alias("located_office_rows"),
        F.sort_array(F.collect_set("state_key")).alias("candidate_state_keys"),
        F.sort_array(F.collect_set("district_pair")).alias("candidate_districts"),
        F.count("shared_point").alias("excluded_shared_coordinate_rows"),
        F.count("valid_lat").alias("valid_coordinate_rows"),
        F.percentile_approx("valid_lat", 0.5).alias("median_lat"),
        F.percentile_approx("valid_lon", 0.5).alias("median_lon"),
        F.min("valid_lat").alias("min_lat"),
        F.max("valid_lat").alias("max_lat"),
        F.min("valid_lon").alias("min_lon"),
        F.max("valid_lon").alias("max_lon"),
    )
    n_states = F.size("candidate_state_keys")
    n_districts = F.size("candidate_districts")
    one_district = (n_states == 1) & (n_districts == 1)
    resolved = one_district & (F.col("located_office_rows") == F.col("office_row_count"))
    only_pair = F.get("candidate_districts", 0)
    # Equirectangular distance across box corners, suitable for a spread check.
    lat_delta = F.col("max_lat") - F.col("min_lat")
    lon_delta = F.col("max_lon") - F.col("min_lon")
    mid_lat_cos = F.cos(F.radians((F.col("min_lat") + F.col("max_lat")) / 2))
    span = 111.2 * F.sqrt(lat_delta ** 2 + (lon_delta * mid_lat_cos) ** 2)
    pins = pins.withColumn("coordinate_span_km", span)
    point_ok = resolved & (F.col("coordinate_span_km") <= max_span_km)
    return pins.select(
        "pincode", "office_row_count", "located_office_rows", "valid_coordinate_rows", "excluded_shared_coordinate_rows",
        "candidate_state_keys", "candidate_districts",
        F.when(n_states == 1, F.get("candidate_state_keys", 0)).alias("state_key"),
        F.when(resolved, only_pair["district_key"]).alias("district_key"),
        F.when((n_states > 1) | (n_districts > 1), "ambiguous")
        .when(n_districts == 0, "unknown")
        .when(F.col("located_office_rows") < F.col("office_row_count"), "partial")
        .otherwise("unique").alias("pin_geography_status"),
        F.when(point_ok, F.col("median_lat")).alias("pin_latitude"),
        F.when(point_ok, F.col("median_lon")).alias("pin_longitude"),
        "coordinate_span_km",
        F.when(~resolved, "unresolved_geography")
        .when((F.col("valid_coordinate_rows") == 0) & (F.col("excluded_shared_coordinate_rows") > 0),
              "shared_across_states")
        .when(F.col("valid_coordinate_rows") == 0, "missing")
        .when(F.col("coordinate_span_km") > max_span_km, "dispersed")
        .otherwise("approximate").alias("pin_coordinate_status"),
    )


def clean_district_survey(raw: DataFrame) -> DataFrame:
    """Return tidy numeric indicators and the original NFHS text and flags.

    In the publisher's district factsheet notation, parentheses denote 25–49
    unweighted cases, and * suppresses percentages with fewer than 25 cases.
    https://www.data.gov.in/catalog/national-family-health-survey-5-nfhs-5-india-districts-factsheet-data-provisional
    Values of percent indicators remain on their original 0–100 scale.
    """
    measures = [c for c in raw.columns if c not in {"state_ut", "district_name"}]
    if not measures:
        raise ValueError("No district survey indicators found")
    # Retain the source spelling in the output, alongside stable join keys.
    survey = raw.select(
        state_key("state_ut").alias("state_key"), place_key("district_name").alias("district_key"),
        F.trim("state_ut").alias("state_ut"), F.trim("district_name").alias("district_name"),
        F.explode(F.array(*[
            F.struct(F.lit(c).alias("indicator"), raw[c].cast("string").alias("raw_value"))
            for c in measures
        ])).alias("entry"),
    ).select("state_key", "district_key", "state_ut", "district_name", "entry.*")
    survey = survey.groupBy("state_key", "district_key", "indicator").agg(
        F.min("state_ut").alias("state_ut"),
        F.min("district_name").alias("district_name"),
        F.count("*").alias("source_row_count"),
        F.sort_array(F.collect_set(F.struct("raw_value"))).alias("source_cells"),
    )
    conflict = F.size("source_cells") > 1
    survey = survey.withColumn("raw_value", F.when(~conflict, F.get("source_cells", 0)["raw_value"]))
    raw_text = F.trim("raw_value")
    number_pattern = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    small_sample = raw_text.rlike(r"^\(\s*" + number_pattern + r"\s*\)$")
    survey = survey.withColumn(
        "number_text",
        F.when(small_sample, F.regexp_extract(raw_text, r"^\(\s*(" + number_pattern + r")\s*\)$", 1))
        .when(raw_text.rlike("^" + number_pattern + "$"), raw_text),
    ).withColumn("parsed", F.expr("try_cast(number_text AS DOUBLE)"))
    pct = F.col("indicator").endswith("_pct")
    valid = (F.abs("parsed") < float("inf")) & (~pct | F.col("parsed").between(0.0, 100.0))
    status = (
        F.when(conflict, "conflicting_values")
        .when(raw_text.isNull() | F.lower(raw_text).isin("", "na", "n/a", "null"), "missing")
        .when(raw_text == "*", "suppressed")
        .when(valid & small_sample, "small_sample")
        .when(valid, "reported")
        .otherwise("invalid")
    )
    return survey.select(
        "state_key", "district_key", "state_ut", "district_name", "indicator",
        "raw_value", F.when(valid, F.col("parsed")).alias("value"), status.alias("value_status"),
        "source_row_count",
        F.when(conflict, F.transform("source_cells", lambda cell: cell["raw_value"])).alias("conflicting_raw_values"),
    )
