"""Audit reported facility points and resolve named origins from the challenge tables."""

from collections import defaultdict
from math import asin, cos, radians, sin, sqrt
from statistics import median
import sys

from pyspark import cloudpickle
from pyspark.sql import functions as F, Window

from backend.facilities import PIN_DISTANCE_REVIEW_KM, distance_km
from backend.geography import pincode_key, place_key, state_key


_AURANGABAD = ("aurangabad", "chhatrapati sambhajinagar", "chhatrapati sambhaji nagar",
               "chatrapati sambhaji nagar")
# Government of Maharashtra's 24 Feb 2023 city and 15 Sep 2023 district renaming notifications.
_CITY_NOTICE = "https://aurangabadmahapalika.org/assets/300/2024/07/mediafiles/sachiv_06_dt__28_02_2024.pdf"
_DISTRICT_NOTICE = "https://cdnbbsr.s3waas.gov.in/s3e6c2dc3dee4a51dcec3a876aa2339a78/uploads/2025/06/202506021323610127.pdf"


def _canonical(key):
    return F.when((F.col("state_key") == "maharashtra") & key.isin(*_AURANGABAD),
                  "chhatrapati sambhajinagar").otherwise(key)


def _km(point, other):
    if point is None or other is None:
        return None
    a, b = map(radians, point)
    c, d = map(radians, other)
    h = sin((a - c) / 2) ** 2 + cos(a) * cos(c) * sin((b - d) / 2) ** 2
    return 12742 * asin(sqrt(min(1.0, h)))


def _centre(points):
    return (median(p[0] for p in points), median(p[1] for p in points)) if points else None


def _peer_results(rows):
    """One vote per independent name and distinct mapped point in this state/city."""
    named_points = defaultdict(set)
    baseline = defaultdict(list)
    for r in rows:
        if r.usable_latitude is None or r.usable_longitude is None:
            continue
        point = (r.usable_latitude, r.usable_longitude)
        baseline[r.city_key].append(point)
        if not r.identity_review and r.name_key:
            named_points[r.name_key].add(tuple(round(float(c), 4) for c in point))
    # A name with conflicting sites is unsafe as a reference. Nearby duplicate
    # observations at identical (11 metre) rounded coordinates cast only one vote.
    votes = {name: next(iter(points)) for name, points in named_points.items() if len(points) == 1}
    baseline_centres = {city: _centre(points) for city, points in baseline.items() if len(points) >= 3}
    result = []
    for r in rows:
        point = None
        if r.latitude is not None and r.longitude is not None and 6 <= r.latitude <= 38 and 67 <= r.longitude <= 99:
            point = (r.latitude, r.longitude)
        peers = {p for name, p in votes.items() if name != r.name_key}
        # Another source payload at the target's own map point is not independent.
        if point:
            peers.discard(tuple(round(float(c), 4) for c in point))
        centre = _centre(peers)
        near = sum(_km(p, centre) <= 35 for p in peers)
        clustered = near >= 2 and near * 3 >= len(peers) * 2
        delta = _km(point, centre)
        baseline_delta = _km(point, baseline_centres.get(r.city_key))
        if delta is not None and delta > PIN_DISTANCE_REVIEW_KM and clustered:
            status = "far_from_city_peers"
        elif ((delta is not None and delta > PIN_DISTANCE_REVIEW_KM)
              or (baseline_delta is not None and baseline_delta > PIN_DISTANCE_REVIEW_KM)):
            status = "city_points_disagree"
        elif point is None:
            status = "no_reported_point"
        elif clustered:
            status = "near_city_peers"
        else:
            status = "insufficient_clustered_peers"
        result.append((r.record_key, len(peers), near, baseline_delta, delta, status,
                       centre[0] if clustered else None, centre[1] if clustered else None))
    return result


def review_facility_geography(facilities):
    """Keep every record and old flag. Only cleared facility points remain usable."""
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    fields = ("record_key", "city_key", "name_key", "latitude", "longitude",
              "usable_latitude", "usable_longitude", "identity_review")
    grouped = (facilities.where("state_key IS NOT NULL AND city_key IS NOT NULL")
               .groupBy("state_key", _canonical(F.col("city_key")).alias("canonical_city"))
               .agg(F.collect_list(F.struct(*fields)).alias("members")))
    schema = ("array<struct<record_key:string,city_peer_count:int,city_peer_near_count:int,"
              "city_group_distance_km:double,city_peer_distance_km:double,city_peer_status:string,"
              "city_peer_latitude:double,city_peer_longitude:double>>")
    scored = grouped.select(F.explode(F.udf(_peer_results, schema)("members")).alias("p")).select("p.*")
    f = facilities.join(scored, "record_key", "left").fillna({
        "city_peer_status": "missing_city_or_state", "city_peer_count": 0, "city_peer_near_count": 0,
    })
    disputed = F.col("city_peer_status").isin("far_from_city_peers", "city_points_disagree")
    f = f.withColumns({
        "location_flags": F.when(disputed, F.array_union("location_flags", F.array("city_peer_status")))
                          .otherwise(F.col("location_flags")),
        "location_review": F.col("location_review") | disputed,
        "usable_latitude": F.when(~disputed, F.col("usable_latitude")),
        "usable_longitude": F.when(~disputed, F.col("usable_longitude")),
    })
    # Contradictory address fields give no basis for picking a side. A plausible
    # in-state PIN and a tight city cluster must also agree if both are present.
    mismatches = F.array(*[F.lit(s) for s in (
        "description_city_conflict", "address_line_pin_conflict", "country_code_conflict", "pin_state_conflict")])
    address_conflict = (F.arrays_overlap("location_flags", mismatches)
                        | F.array_contains("identity_flags", "description_names_other_facility_and_city"))
    pin_ok = ((F.col("pin_geography_status") == "unique")
              & F.array_contains("candidate_state_keys", F.col("state_key"))
              & F.col("pin_latitude").isNotNull() & F.col("pin_longitude").isNotNull())
    city_ok = F.col("city_peer_latitude").isNotNull()
    areas_apart = F.coalesce(pin_ok & city_ok & (distance_km(
        F.col("pin_latitude"), F.col("pin_longitude"), F.col("city_peer_latitude"),
        F.col("city_peer_longitude")) > PIN_DISTANCE_REVIEW_KM), F.lit(False))
    proxy_ok = ~address_conflict & ~areas_apart
    basis = (F.when(address_conflict | areas_apart, "unresolved")
             .when(F.col("usable_latitude").isNotNull(), "reported_facility_coordinates")
             .when(proxy_ok & pin_ok, "india_post_pin_area")
             .when(proxy_ok & city_ok, "facility_city_peer_median")
             .otherwise("unresolved"))
    f = f.withColumn("best_estimate_basis", basis)
    b = F.col("best_estimate_basis")
    # Cross-area conflict also withholds the point used by older distance clients.
    area_flag = F.array(F.lit("postal_area_far_from_city_peers"))
    f = f.withColumns({
        "location_flags": F.when(areas_apart, F.array_union("location_flags", area_flag)).otherwise(F.col("location_flags")),
        "location_review": F.col("location_review") | areas_apart,
        "usable_latitude": F.when(~areas_apart & ~address_conflict, F.col("usable_latitude")),
        "usable_longitude": F.when(~areas_apart & ~address_conflict, F.col("usable_longitude")),
    })
    return f.withColumns({
        **{"best_estimate_" + axis:
           F.when(b == "reported_facility_coordinates", F.col("usable_" + axis))
            .when(b == "india_post_pin_area", F.col("pin_" + axis))
            .when(b == "facility_city_peer_median", F.col("city_peer_" + axis))
           for axis in ("latitude", "longitude")},
        "best_estimate_precision": F.when(b == "reported_facility_coordinates", "reported_site")
                              .when(b == "india_post_pin_area", "pin_area")
                              .when(b == "facility_city_peer_median", "city_area").otherwise("unknown"),
        "best_estimate_confidence": F.when(b == "unresolved", "unknown")
            .when((b == "reported_facility_coordinates") & ((F.col("pin_distance_km") <= 25)
                   | (F.col("city_peer_distance_km") <= 25) & city_ok), "moderate")
            .when((b == "india_post_pin_area") & city_ok, "moderate").otherwise("low"),
    })


def _origin_key(value):
    # Keep Unicode combining marks (e.g. Devanagari vowel signs) alongside letters.
    value = F.trim(F.regexp_replace(F.regexp_replace(F.lower(value), "&", " and "),
                                    r"[^\p{L}\p{M}\p{N}]+", " "))
    return F.when(~value.isin("", "na", "n a", "null", "none", "nan", "unknown"), value)


def _origin_rows(frame, kind, name, keys, lat, lon, basis, source, notice=None):
    string_null = F.lit(None).cast("string")
    place = frame.select(
        F.lit(kind).alias("place_type"), name.alias("place_name"), keys.alias("search_keys"),
        *[F.col(c) if c in frame.columns else string_null.alias(c) for c in ("state_key", "district_key", "pincode")],
        lat.cast("double").alias("latitude"), lon.cast("double").alias("longitude"),
        basis.alias("point_basis"), F.lit(source).alias("source_table"),
        (notice if notice is not None else string_null).alias("alias_source_url"),
    )
    return (place.withColumn("search_key", F.explode(F.array_distinct("search_keys"))).drop("search_keys")
            .where("search_key IS NOT NULL")
            .withColumn("place_id", F.sha2(F.to_json(F.struct("place_type", "place_name", "state_key",
                                                               "district_key", "pincode")), 256))
            .withColumn("is_alias", ~F.col("search_key").eqNullSafe(_origin_key(F.col("place_name")))))


def _name_variants(name):
    aliases = F.array(*[F.lit(v) for v in _AURANGABAD])
    return F.when((F.col("state_key") == "maharashtra") & name.isin(*_AURANGABAD), aliases).otherwise(F.array(name))


def build_place_lookup(offices, postal, facilities):
    """Produce one search key per candidate place, including unmapped named origins."""
    lat = F.col("latitude")
    lon = F.col("longitude")
    null_point = F.lit(None).cast("double")
    known_pin = postal.where("pin_latitude IS NOT NULL AND pin_longitude IS NOT NULL")
    districts = (known_pin.groupBy("state_key", "district_key").agg(
        F.countDistinct(F.struct("pin_latitude", "pin_longitude")).alias("mapped_points"),
        F.median("pin_latitude").alias("district_lat"), F.median("pin_longitude").alias("district_lon"),
    ).where("mapped_points >= 3"))
    raw = offices.select(
        pincode_key("pincode").alias("pincode"), state_key("statename").alias("state_key"),
        place_key("district").alias("district_key"), F.trim("officename").alias("office_name"),
        F.expr("try_cast(latitude AS DOUBLE)").alias("latitude"),
        F.expr("try_cast(longitude AS DOUBLE)").alias("longitude"),
    ).where("pincode IS NOT NULL")
    has_district = F.col("state_key").isNotNull() & F.col("district_key").isNotNull()
    same_point = Window.partitionBy("latitude", "longitude", has_district)
    point_ok = (has_district & lat.between(6.0, 38.0) & lon.between(67.0, 99.0)
                & (F.min("pincode").over(same_point) == F.max("pincode").over(same_point))
                & (F.min("state_key").over(same_point) == F.max("state_key").over(same_point))
                & (F.min("district_key").over(same_point) == F.max("district_key").over(same_point)))
    by_office = (raw.withColumn("point_ok", point_ok)
                 .groupBy("state_key", "district_key", "pincode", "office_name").agg(
                     F.collect_set(F.when(F.col("point_ok"),
                                          F.struct("latitude", "longitude"))).alias("office_points")))
    office = (by_office.join(districts, ["state_key", "district_key"], "left")
              .join(postal.select("pincode", "pin_latitude", "pin_longitude"), "pincode", "left"))
    coords = F.get("office_points", 0)
    # For a PIN without a usable point, an office can supply an origin when its
    # own consistent coordinates agree with other mapped PINs in that district.
    # Within a mapped PIN, require consistency with the PIN and any district anchor.
    district_distance = distance_km(coords.latitude, coords.longitude, F.col("district_lat"), F.col("district_lon"))
    pin_distance = distance_km(coords.latitude, coords.longitude, F.col("pin_latitude"), F.col("pin_longitude"))
    close_to_pin = pin_distance <= 50
    close_to_district = district_distance <= PIN_DISTANCE_REVIEW_KM
    office_ok = ((F.size("office_points") == 1)
                 & (F.coalesce(close_to_pin, F.lit(False)) | F.coalesce(close_to_district, F.lit(False)))
                 & (pin_distance.isNull() | close_to_pin) & (district_distance.isNull() | close_to_district))
    pin_district_distance = distance_km(F.col("pin_latitude"), F.col("pin_longitude"),
                                        F.col("district_lat"), F.col("district_lon"))
    pin_ok = (has_district & F.col("pin_latitude").isNotNull()
              & (pin_district_distance.isNull() | (pin_district_distance <= PIN_DISTANCE_REVIEW_KM)))
    office_key = _origin_key(F.col("office_name"))
    # Strip only a terminal office type. Parenthetical localities and the original office name stay searchable.
    locality = _origin_key(F.regexp_replace("office_name", r"(?i)\s+(?:[bshp]\.?\s*o\.?|g\.?p\.?o\.?)\s*$", ""))
    result = _origin_rows(
        office, "post_office", F.col("office_name"), F.array(office_key, locality),
        F.when(office_ok, coords.latitude).when(pin_ok, F.col("pin_latitude")),
        F.when(office_ok, coords.longitude).when(pin_ok, F.col("pin_longitude")),
        F.when(office_ok, "reported_post_office_point").when(pin_ok, "pin_area").otherwise("unresolved"),
        "india_post_pincode_directory",
    )
    candidates = postal.select("pincode", F.explode_outer("candidate_districts").alias("d"),
                               "pin_latitude", "pin_longitude", F.col("state_key").alias("pin_state")).select(
        "pincode", F.coalesce("d.state_key", "pin_state").alias("state_key"), F.col("d.district_key").alias("district_key"),
        "pin_latitude", "pin_longitude",
    ).join(districts, ["state_key", "district_key"], "left")
    pin_rows = _origin_rows(candidates, "pin", F.col("pincode"), F.array(F.col("pincode")),
                            F.when(pin_ok, F.col("pin_latitude")), F.when(pin_ok, F.col("pin_longitude")),
                            F.when(pin_ok, "pin_area").otherwise("unresolved"), "india_post_pincode_directory")
    district_names = (raw.where(has_district).select("state_key", "district_key").distinct()
                      .join(districts, ["state_key", "district_key"], "left"))
    district = F.col("district_key")
    district_rows = _origin_rows(
        district_names, "district", _canonical(district), _name_variants(district),
        F.col("district_lat"), F.col("district_lon"),
        F.when(F.col("district_lat").isNotNull(), "district_pin_median").otherwise("unresolved"),
        "india_post_pincode_directory", F.when((F.col("state_key") == "maharashtra")
                                               & district.isin(*_AURANGABAD), _DISTRICT_NOTICE),
    )
    state_rows = _origin_rows(raw.select("state_key").distinct(), "state", F.col("state_key"),
                              F.array("state_key"), null_point, null_point, F.lit("unresolved"),
                              "india_post_pincode_directory")
    city = _canonical(_origin_key(F.col("address_city")))
    # A single reported facility cannot locate a whole city. Reuse city clusters
    # independently supported after leaving each target out.
    city_rows = (facilities.where("state_key IS NOT NULL").withColumn("canonical_city", city)
                 .where("canonical_city IS NOT NULL").groupBy("state_key", "canonical_city").agg(
                     F.median("city_peer_latitude").alias("city_lat"),
                     F.median("city_peer_longitude").alias("city_lon")))
    name = F.col("canonical_city")
    city_rows = _origin_rows(
        city_rows, "city", name, _name_variants(name), F.col("city_lat"), F.col("city_lon"),
        F.when(F.col("city_lat").isNotNull(), "facility_city_peer_median").otherwise("unresolved"),
        "facilities", F.when((F.col("state_key") == "maharashtra") & name.isin(*_AURANGABAD), _CITY_NOTICE),
    )
    return result.unionByName(pin_rows).unionByName(district_rows).unionByName(state_rows).unionByName(city_rows)
