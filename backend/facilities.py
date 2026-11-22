"""Preserve source rows and identify records that need identity or location review."""

from pyspark.sql import Window, functions as F

from backend.geography import pincode_key, place_key, state_key


UUID = r"(?i)^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
PIN_DISTANCE_REVIEW_KM = 75.0

# Restrict the contradiction check to two recognizable, distinct city names.
# The city used for ordinary candidate lookup remains the normalized address.
_CITY_NAMES = (
    "new delhi", "delhi", "bengaluru", "bangalore", "mumbai", "bombay", "chennai", "madras",
    "kolkata", "calcutta", "ahmedabad", "pune", "hyderabad", "jaipur", "lucknow", "patna",
    "indore", "bhopal", "chandigarh", "nagpur", "surat", "kochi", "cochin", "bhubaneswar",
    "ludhiana", "coimbatore", "guwahati", "ranchi", "jodhpur", "visakhapatnam",
)
_CITIES = "(?:" + "|".join(_CITY_NAMES) + ")"
_CITY_ALIASES = {
    "new delhi": "delhi", "bangalore": "bengaluru", "bombay": "mumbai",
    "madras": "chennai", "calcutta": "kolkata", "cochin": "kochi",
}
# The first clause must name the same facility as the name column. A referral,
# address on a road named after a city, or generic specialty mention is insufficient.
_SITE_DECLARATION = (
    r"(?i)^\s*(.{2,150}?)(?:,\s*|\s+)(?:is\s+)?"
    r"(?:(?:an?\s+)?(?:(?:private|public|specialist|specialty|general|medical|dental|"
    r"multi[- ]?specialty|multi[- ]?speciality|tertiary|care|eye)\s+){0,4}"
    r"(?:hospital|clinic|centre|center|facility|practice|institute)\s+)?"
    r"(?:located|situated|based)\s+in\s+(?:the\s+(?:city|heart)\s+of\s+)?"
    rf"({_CITIES})(?=\s*[,.;!?]|\s*$)"
)
_MULTISITE = (
    r"(?i)\b(?:branch(?:es)?|chains?|networks?|referrals?|referred|refers|headquarters|"
    r"other sites?|several locations|multiple locations)\b"
    rf"|\b{_CITIES}\s*,\s*(?:and\s+)?{_CITIES}\b"
)
_EXPLICIT_PIN = r"(?i)\bpin(?:[\s-]*code)?\s*[:.\-]?\s*([1-9][0-9]{5})(?![\p{L}\p{N}])"
# A place-like address segment followed by a postal code, e.g. "Malad West 400064"
# or "Hyderabad-500082, Telangana". Retain the original line for review.
_PLACE_PIN = (r"(?i)(?:^|[,;])\s*([\p{L}\p{M}]+(?:[ '’-][\p{L}\p{M}]+){0,4})"
              r"[\s,–—-]+([1-9][0-9]{5})(?=\s*(?:$|[,;(]))")
_NON_PLACE = (r"(?i)\b(?:phone|telephone|mobile|tel|call|contact|fax|cell|helpline|landline|"
              r"number|no|plot|flat|sector|block|survey|house|street|lane|road|floor|ward|room|"
              r"box|shop|building|gate|near|opposite|registration|license|id|pin|code)\b")
_OTHER_SITE = (
    r"(?i)^\s*([\p{L}][\p{L}\p{N} '&’-]{1,95}?\s+(?:hospital|clinic|centre|center))"
    r"\s+is\s+an?\s+[^.!?\n]{1,220}\.\s+(?:located|situated|based)\s+in\s+"
    rf"({_CITIES})(?=\s*[,.;!?]|\s*$)"
)


def _comparison_city(key):
    mapping = F.create_map(*[F.lit(v) for pair in _CITY_ALIASES.items() for v in pair])
    return F.when(key.isin(*_CITY_NAMES), F.coalesce(F.element_at(mapping, key), key))


def add_address_evidence(facilities):
    """Extract auditable location evidence for comparisons with the reported address."""
    description = F.col("description")
    subject = place_key(F.regexp_extract(description, _SITE_DECLARATION, 1))
    city = place_key(F.regexp_extract(description, _SITE_DECLARATION, 2))
    # Limit multisite checks to the opening statement that produced the match.
    first_statement = F.regexp_extract(description, r"(?s)^(.{0,600}?)(?:[.!?](?:\s|$)|\n|$)", 1)
    statement = F.regexp_extract(description, _SITE_DECLARATION, 0)
    single_site = ((subject == F.col("name_key")) & (F.length(first_statement) >= F.length(statement))
                   & ~first_statement.rlike(_MULTISITE))
    other_statement = F.regexp_extract(description, _OTHER_SITE, 0)
    other_subject = place_key(F.regexp_extract(description, _OTHER_SITE, 1))
    other_city = _comparison_city(place_key(F.regexp_extract(description, _OTHER_SITE, 2)))
    other_site = ((other_subject != F.col("name_key")) & (other_city != _comparison_city(F.col("city_key")))
                  & ~F.substring(description, 1, 450).rlike(_MULTISITE))
    address = F.concat_ws(" ", "address_line1", "address_line2", "address_line3")
    return facilities.withColumns({
        "description_site_statement": F.when(single_site, statement),
        "description_site_city_key": F.when(single_site, _comparison_city(city)),
        "description_other_site_statement": F.when(other_site, other_statement),
        "address_line_pins": F.sort_array(F.array_distinct(F.regexp_extract_all(address, F.lit(_EXPLICIT_PIN), 1))),
    })


def _office_matched_address_pins(facilities, postal):
    """Collect place-formatted conflicting tokens confirmed in the supplied PIN directory."""
    lines = facilities.select("record_key", "pincode",
        F.explode(F.array("address_line1", "address_line2", "address_line3")).alias("line"))
    places = F.regexp_extract_all("line", F.lit(_PLACE_PIN), 1)
    codes = F.regexp_extract_all("line", F.lit(_PLACE_PIN), 2)
    mentions = lines.select("record_key", "pincode", F.explode(
        F.zip_with(places, codes, lambda place, code:
                   F.when(~place.rlike(_NON_PLACE), code))).alias("address_pin")
    ).where(F.col("address_pin") != F.col("pincode"))
    directory = postal.select(F.col("pincode").alias("address_pin"))
    return (mentions.join(directory, "address_pin", "left_semi").groupBy("record_key")
            .agg(F.sort_array(F.collect_set("address_pin")).alias("conflicting_directory_pins")))


def text_value(column):
    value = F.trim(F.col(column))
    return F.when(~F.lower(value).isin("", "null", "none", "nan", "n/a", "[]"), value)


def flags(*conditions):
    return F.filter(F.array(*[F.when(condition, F.lit(label)) for label, condition in conditions]),
                    lambda value: value.isNotNull())


def distance_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two coordinate pairs, in kilometres."""
    a = (F.pow(F.sin(F.radians(lat2 - lat1) / 2), 2)
         + F.cos(F.radians(lat1)) * F.cos(F.radians(lat2))
         * F.pow(F.sin(F.radians(lon2 - lon1) / 2), 2))
    # Spark least/greatest ignore nulls. Leave unknown distances null.
    present = lat1.isNotNull() & lon1.isNotNull() & lat2.isNotNull() & lon2.isNotNull()
    return F.when(present, 12742.0 * F.asin(F.sqrt(F.least(F.lit(1.0), F.greatest(F.lit(0.0), a)))))


def prepare_source(raw):
    """One reproducible key per distinct raw payload, plus its exact multiplicity."""
    payload = F.to_json(F.struct(*[F.col(k) for k in sorted(raw.columns)]),
                        options={"ignoreNullFields": "false"})
    keyed = raw.withColumn("record_key", F.sha2(payload, 256))
    keyed = keyed.withColumn("source_row_count", F.count("*").over(Window.partitionBy("record_key")))
    return keyed.dropDuplicates(["record_key"]).withColumn("quarantine_reasons", flags(
        ("invalid_record_id", ~F.coalesce(F.col("unique_id").rlike(UUID), F.lit(False))),
        ("invalid_record_type", ~F.lower(F.trim(F.coalesce("organization_type", F.lit("")))).eqNullSafe("facility")),
        ("missing_facility_name", text_value("name").isNull()),
    ))


def build_facilities(prepared, postal, as_of):
    """Return valid-shaped rows, including questionable rows with explicit review flags."""
    valid = prepared.where(F.size("quarantine_reasons") == 0).drop("quarantine_reasons")
    f = valid.withColumns({
        "name_key": place_key("name"),
        "city_key": place_key("address_city"),
        "state_key": state_key("address_stateOrRegion"),
        "pincode": pincode_key("address_zipOrPostcode"),
        "page_date": F.expr("try_cast(recency_of_page_update AS DATE)"),
        "doctors_reported": F.expr("try_cast(numberDoctors AS INT)"),
        "capacity_reported": F.expr("try_cast(capacity AS INT)"),
    })
    f = add_address_evidence(f)
    f = f.withColumns({key: F.when(F.col(key) >= 0, F.col(key))
                       for key in ("doctors_reported", "capacity_reported")})
    shared = Window.partitionBy("source_content_id")
    id_window = Window.partitionBy(F.lower("unique_id"))
    name_pin = Window.partitionBy("name_key", "pincode")
    f = f.withColumn("identity_flags", flags(
        ("description_names_other_facility_and_city", F.col("description_other_site_statement").isNotNull()),
        ("record_id_has_multiple_versions", F.count("*").over(id_window) > 1),
        ("source_content_shared_across_names", F.col("source_content_id").rlike(UUID)
         & (F.min("name_key").over(shared) != F.max("name_key").over(shared))),
        ("possible_duplicate_name_and_pin", F.col("pincode").isNotNull() & F.col("name_key").isNotNull()
         & (F.min("unique_id").over(name_pin) != F.max("unique_id").over(name_pin))),
    ))
    lookup = postal.select("pincode", "candidate_state_keys", "pin_latitude", "pin_longitude",
                           "pin_geography_status", F.col("district_key").alias("postal_district_key"))
    f = f.join(lookup, on="pincode", how="left")
    alternate_pins = _office_matched_address_pins(valid.withColumn(
        "pincode", pincode_key("address_zipOrPostcode")), postal)
    f = f.join(alternate_pins, "record_key", "left")
    lat, lon = F.col("latitude"), F.col("longitude")
    valid_point = lat.between(6.0, 38.0) & lon.between(67.0, 99.0)
    p = F.from_json("coordinates", "type STRING, coordinates ARRAY<DOUBLE>")
    alt_lon, alt_lat = F.get(p.coordinates, 0), F.get(p.coordinates, 1)
    f = f.withColumn("pin_distance_km", F.when(valid_point, distance_km(
        lat, lon, F.col("pin_latitude"), F.col("pin_longitude"))))
    f = f.withColumn("location_flags", flags(
        ("missing_or_unrecognized_state", F.col("state_key").isNull()),
        ("missing_city", F.col("city_key").isNull()),
        ("description_city_conflict", F.col("description_site_city_key") != _comparison_city(F.col("city_key"))),
        ("address_line_pin_conflict", F.col("pincode").isNotNull()
         & (F.exists("address_line_pins", lambda code: code != F.col("pincode"))
            | (F.size("conflicting_directory_pins") > 0))),
        ("country_code_conflict", text_value("address_countryCode").isNotNull()
         & (F.upper(text_value("address_countryCode")) != "IN")),
        ("pin_state_conflict", (F.size("candidate_state_keys") > 0) & F.col("state_key").isNotNull()
         & ~F.array_contains("candidate_state_keys", F.col("state_key"))),
        ("coordinates_outside_india_envelope", lat.isNotNull() & lon.isNotNull() & ~valid_point),
        ("coordinate_fields_disagree", (p.type == "Point") & (F.size(p.coordinates) == 2)
         & ((F.abs(lat - alt_lat) > 0.01) | (F.abs(lon - alt_lon) > 0.01))),
        ("point_far_from_postal_area", F.col("pin_distance_km") > PIN_DISTANCE_REVIEW_KM),
    ))
    f = f.withColumns({
        "identity_review": F.size("identity_flags") > 0,
        "location_review": F.size("location_flags") > 0,
        "quality_notes": flags(
            ("missing_or_invalid_pin", F.col("pincode").isNull()),
            ("pin_absent_from_directory", F.col("pincode").isNotNull() & F.col("pin_geography_status").isNull()),
            ("district_unresolved", F.col("postal_district_key").isNull()),
            ("no_valid_facility_point", ~F.coalesce(valid_point, F.lit(False))),
            ("page_date_missing_or_invalid", F.col("page_date").isNull()),
            ("future_page_date", F.col("page_date") > F.lit(as_of).cast("date")),
        ),
    })
    usable = valid_point & ~F.col("location_review")
    return f.withColumns({
        "usable_latitude": F.when(usable, lat), "usable_longitude": F.when(usable, lon),
        "usable_page_date": F.when(F.col("page_date") <= F.lit(as_of).cast("date"), F.col("page_date")),
    })
