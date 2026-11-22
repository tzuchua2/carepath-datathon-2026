"""Rank documented facilities for a service near a location, and explain the ranking.

The engine reads the prepared tables (service_evidence, clean_facilities,
postal_reference, external_corroboration). It never upgrades evidence: a
facility is recommended only when the build marked the pair eligible. Other
records appear separately as leads to verify or as options that were set
aside, each with the quote that explains why.

The core uses only the standard library, so it runs unchanged in a notebook,
a Databricks App or a unit test. Scores order options; they are not
probabilities that a service is available.
"""

from collections import defaultdict
from datetime import date
import difflib
import json
import math
import re
from statistics import median

from backend.service_evidence import SERVICES

POLICY_VERSION = "rank-v2"
CONTRACT_VERSION = "1.0"

SERVICE_ALIASES = {
    "dialysis": ("dialysis", "hemodialysis", "haemodialysis", "kidney dialysis"),
    "mri": ("mri", "mri scan", "magnetic resonance imaging"),
    "nicu": ("nicu", "neonatal intensive care", "neonatal icu", "newborn icu", "newborn intensive care"),
    "c_section": ("c section", "c-section", "caesarean", "cesarean", "caesarean section", "cesarean section", "lscs"),
    "emergency_c_section": ("emergency c section", "emergency c-section", "emergency caesarean",
                            "emergency cesarean", "emergency lscs"),
}
# Distance at which the distance points halve. Recurring or urgent care decays faster.
HALF_DISTANCE_KM = {"dialysis": 30.0, "nicu": 30.0, "c_section": 40.0, "emergency_c_section": 20.0, "mri": 60.0}
WEIGHTS = {
    "evidence_first": {"service_evidence": 0.50, "distance": 0.35, "location": 0.15},
    "nearest_first": {"service_evidence": 0.30, "distance": 0.55, "location": 0.15},
}
RADIUS_STEPS_KM = (50.0, 100.0, 200.0, 400.0)
# Other reasonable weightings (service evidence, distance, location). Each result reports the best and worst
# position it would take under these, so the user can see how much the order depends on our choice of weights.
ALTERNATIVE_WEIGHTS = ((0.60, 0.25, 0.15), (0.40, 0.45, 0.15), (0.50, 0.25, 0.25), (0.50, 0.45, 0.05),
                       (0.34, 0.33, 0.33))
# With no PIN to confirm it, a point this far from the facility's own city is not trusted for distance.
MAX_CITY_OFFSET_KM = 75.0
# The service-evidence component is the share of these five checks a claim passes. A check that could not be
# run counts as not passed and is reported as "not checked", never as a failure.
EVIDENCE_CHECKS = ("record_states_service", "statement_is_specific", "model_reading_agrees",
                   "facility_source_states_it", "independent_source_lists_it")
MODEL_FOUND = "model_found_statement_rules_missed"
_RULED_OUT = {"negated": "The record says the service is not available.",
              "referral_only": "The record describes referring patients elsewhere for this service.",
              "planned": "The record describes the service as planned, not operating."}
_HOLD_REASONS = {
    "identity_review": "The record may combine more than one facility.",
    "location_review": "The record's address, PIN and coordinates disagree.",
    "other_named_site": "The service statement names a different facility.",
    "branch_attribution_unclear": "The statement may describe another branch.",
    "unclear_negation_scope": "A negative statement in the record may apply to this service.",
    "conflicting_service_statements": "The record contains both positive and limiting statements.",
    "specialty_name_mismatch": "The facility's name suggests a different specialty.",
    "education_provider_review": "The record appears to describe a nursing school.",
    "missing_record_source_url": "The record has no source link.",
    "manual_review": "A reviewer put this claim on hold.",
    "model_disagreement": "The model check and the text rules disagree about what the record states.",
    "verifier_output_invalid": "The model check gave no usable answer for this record.",
    "two_model_readings_disagree_with_rules": "Two model readings found that the record does not clearly state this service.",
}
# Copied from backend.geography, which needs Spark to import.
_STATE_ALIASES = {
    "a p": "andhra pradesh", "chattisgarh": "chhattisgarh", "j and k": "jammu and kashmir", "m p": "madhya pradesh",
    "maharastra": "maharashtra", "nct delhi": "delhi", "nct of delhi": "delhi", "new delhi": "delhi",
    "orissa": "odisha", "pondicherry": "puducherry", "tamilnadu": "tamil nadu", "u p": "uttar pradesh",
    "up": "uttar pradesh",
}


# Older and newer names of the same city. Either may be the one the records use.
_PLACE_ALIASES = {
    "bombay": "mumbai", "calcutta": "kolkata", "madras": "chennai", "poona": "pune", "baroda": "vadodara",
    "banaras": "varanasi", "benares": "varanasi", "bangalore": "bengaluru", "mysore": "mysuru",
    "trivandrum": "thiruvananthapuram", "cochin": "kochi", "calicut": "kozhikode", "pondicherry": "puducherry",
    "allahabad": "prayagraj", "gurgaon": "gurugram", "vizag": "visakhapatnam", "mangalore": "mangaluru",
    "belgaum": "belagavi", "simla": "shimla",
}
SAME_PLACE_KM = 30.0  # a name listed under two states this close together is one place


def service_phrase(service):
    """The service's name for the middle of a sentence: 'dialysis', 'MRI', 'emergency C-section'."""
    label = SERVICES[service]
    return label if label.split()[0].isupper() or label.startswith("C-") else label[0].lower() + label[1:]


class RequestError(ValueError):
    """A request the engine cannot answer as given. `code` is stable for the UI."""

    def __init__(self, code, message, options=()):
        super().__init__(message)
        self.code, self.message, self.options = code, message, list(options)

    def to_dict(self):
        return {"contract_version": CONTRACT_VERSION, "status": self.code, "message": self.message,
                "options": self.options}


def place_key(text):
    """Mirror backend.geography.place_key for plain strings."""
    if not isinstance(text, str):
        return None
    key = "".join(ch if ch.isalnum() else " " for ch in text.casefold().replace("&", " and "))
    key = " ".join(key.split())
    return key if key and key not in {"na", "n a", "null", "none", "nan", "unknown"} else None


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def resolve_service(text):
    key = place_key(text)
    for service, label in SERVICES.items():
        if key in {place_key(service), place_key(label), *(place_key(a) for a in SERVICE_ALIASES[service])}:
            return service
    raise RequestError("INVALID_SERVICE", f"'{text}' is not a supported service.",
                       [{"code": k, "display_name": v} for k, v in SERVICES.items()])


def _clean(value):
    """Turn pandas/NumPy cells into plain Python values with None for missing."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if hasattr(value, "asDict"):  # a Spark Row inside an array column
        value = value.asDict(recursive=True)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if hasattr(value, "tolist") and not isinstance(value, float):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    try:
        if value != value:  # NaN and NaT
            return None
    except (TypeError, ValueError):
        return None
    return value


def _records(frame):
    if frame is None:
        return []
    rows = frame.to_dict("records") if hasattr(frame, "to_dict") else frame
    return [_clean(dict(row)) for row in rows]


_FACILITY_COLUMNS = (
    "record_key", "unique_id", "name", "city_key", "state_key", "pincode", "address_line1", "address_city",
    "address_stateOrRegion", "postal_district_key", "usable_latitude", "usable_longitude", "pin_distance_km",
    "pin_geography_status", "identity_review", "location_review", "identity_flags", "location_flags",
    "officialPhone", "officialWebsite", "facilityTypeId", "operatorTypeId", "doctors_reported", "capacity_reported",
)
# Added by the v3 release. Loaders select whichever of these a snapshot has.
_V3_FACILITY_COLUMNS = (
    "address_display", "identity_severity", "duplicate_group_id", "duplicate_group_size", "best_estimate_latitude",
    "best_estimate_longitude", "best_estimate_basis", "best_estimate_precision", "best_estimate_confidence",
    "phone_status", "official_phone_e164", "website_status", "official_web_host",
)
_PLACE_COLUMNS = ("search_key", "place_id", "place_type", "place_name", "state_key", "district_key", "pincode",
                  "latitude", "longitude", "point_basis")
_PLACE_PRECISION = {  # point_basis -> (precision, floor on uncertainty in km, wording)
    "reported_post_office_point": ("post_office", 5.0, "the reported position of this post office"),
    "pin_area": ("pincode_approximate", 5.0, "the centre of this PIN area"),
    "district_pin_median": ("district", 10.0, "the median of the district's postal-area points"),
    "facility_city_peer_median": ("locality", 3.0, "the median location of facility records listed in this city"),
}
# Outside sources that count as independent of the facility's own material.
_INDEPENDENT_MATCH = "documented_with_external_reference"
_POSTAL_COLUMNS = ("pincode", "state_key", "district_key", "pin_geography_status", "pin_latitude", "pin_longitude",
                   "coordinate_span_km", "pin_coordinate_status")


def merge_verified(evidence_rows, verified):
    """Replace evidence rows with the verifier's rows for the same record and service.

    `verified` is a path to JSON Lines or an iterable of rows. Rows for pairs the release does not list are
    ignored: the verifier may hold or lower a pair here, never add one.
    """
    import json

    if isinstance(verified, (str, bytes)) or hasattr(verified, "open"):
        with open(verified, encoding="utf-8") as lines:
            verified = [json.loads(line) for line in lines if line.strip()]
    checked = {(row["record_key"], row["service"]): row for row in verified}
    return [checked.get((row["record_key"], row["service"]), row) for row in evidence_rows]


class _PlaceIndex:
    """Search key -> places with a point. Rows are kept as tuples because the release lists 340,000 names."""

    def __init__(self, places=None):
        self.rows, self.states = defaultdict(list), set()
        if places is None:
            return
        if hasattr(places, "columns"):  # a pandas frame: read whole columns, not one dict per row
            rows = zip(*(places[c].tolist() for c in _PLACE_COLUMNS))
        else:
            rows = (tuple(r.get(c) for c in _PLACE_COLUMNS) for r in places)
        for row in rows:
            row = tuple(None if v != v else v for v in row)  # NaN -> None
            place = dict(zip(_PLACE_COLUMNS, row))
            if place["latitude"] is None or place["place_type"] == "state":
                continue
            self.rows[place["search_key"]].append(row[1:])
            if place["state_key"]:
                self.states.add(place["state_key"])

    def get(self, key, default=()):
        return [dict(zip(_PLACE_COLUMNS[1:], row), search_key=key) for row in self.rows.get(key, default)]

    def __len__(self):
        return len(self.rows)


class CareIndex:
    """In-memory lookup over one prepared snapshot."""

    def __init__(self, evidence, facilities, postal, corroboration=None, as_of=None, index_version="unversioned",
                 external_matches=None, places=None, website_checks=None, osm=None, public_reviews=None,
                 access_gaps=None):
        self.as_of = as_of or date.today()
        self.index_version = index_version
        self.facilities = {row["record_key"]: row for row in _records(facilities)}
        self.postal = {row["pincode"]: row for row in _records(postal)}
        self.evidence, self.by_record = defaultdict(list), defaultdict(list)
        for row in _records(evidence):
            if row["record_key"] in self.facilities:
                self.evidence[row["service"]].append(row)
                self.by_record[row["record_key"]].append(row)
        self.matches = defaultdict(list)  # v3 outside-source observations, one list per (record, service)
        for row in _records(external_matches):
            if row.get("record_key"):
                row["checked_on"] = None if row.get("checked_on") is None else str(row["checked_on"])
                self.matches[(row["record_key"], row["service"])].append(row)
        self.places = _PlaceIndex(places)  # v3 place lookup: search key -> places that have a point
        # Outside checks, all optional. Each row keeps its source, URL and the date it was read.
        self.website = {(r["record_key"], r["service"]): r for r in _records(website_checks) if r.get("record_key")}
        self.osm = {r["record_key"]: r for r in _records(osm) if r.get("record_key")}
        self.reviews = {(r["record_key"], r["service"]): r for r in _records(public_reviews) if r.get("record_key")}
        self.gaps = access_gaps or {}  # written by backend.access_gaps
        self._gap_index = {(service, d["state"], d["district"]): d
                           for service, block in (self.gaps.get("services") or {}).items() for d in block["districts"]}
        self.external = defaultdict(list)
        for row in _records(corroboration):
            self.external[(row["record_key"], row["service"])].append(row)

        cities, districts = defaultdict(list), defaultdict(list)
        for row in self.facilities.values():
            if row.get("usable_latitude") is not None and row.get("city_key") and row.get("state_key"):
                cities[(row["state_key"], row["city_key"])].append((row["usable_latitude"], row["usable_longitude"]))
        for row in self.postal.values():
            if row.get("pin_latitude") is not None and row.get("district_key") and row.get("state_key"):
                districts[(row["state_key"], row["district_key"])].append((row["pin_latitude"], row["pin_longitude"]))
        self.cities = {key: self._centre(points) for key, points in cities.items()}
        self.districts = {key: self._centre(points) for key, points in districts.items()}
        self.states = {state for state, _ in (*self.cities, *self.districts)}
        self.states |= self.places.states

    @staticmethod
    def _centre(points):
        lat, lon = median(p[0] for p in points), median(p[1] for p in points)
        spread = median(haversine_km(lat, lon, *p) for p in points)
        return {"latitude": lat, "longitude": lon, "points": len(points), "spread_km": spread}

    @classmethod
    def from_parquet(cls, directory, **kwargs):
        import pandas as pd
        from pathlib import Path

        root = Path(directory)
        extra = root / "external_corroboration.parquet"
        return cls(
            pd.read_parquet(root / "service_evidence.parquet"),
            pd.read_parquet(root / "clean_facilities.parquet", columns=list(_FACILITY_COLUMNS)),
            pd.read_parquet(root / "postal_reference.parquet", columns=list(_POSTAL_COLUMNS)),
            pd.read_parquet(extra) if extra.exists() else None, **kwargs)

    @classmethod
    def from_release(cls, manifest="data/recovery/v3-manifest.json", root=".", post_offices=True, verified=None,
                     leads=None, enrichment=True, **kwargs):
        """Load a release (v3 or later) from its manifest of table paths, relative to the repository root.

        `post_offices=False` skips the 320,000 post-office names in the place lookup to save memory; cities,
        districts and PINs still resolve. `verified` is a JSON Lines file written by backend.verify_release: each
        of its rows replaces the release's evidence row for the same record and service, so the model check can
        hold a pair and every result carries its verdict. `leads` is the JSON Lines file written by
        backend.verify_leads: statements the model found in claims the rules rejected, listed as leads only.
        With `enrichment` (the default) the optional outside-check files are loaded when they exist under `root`:
        data/enrichment/website_checks.jsonl, data/enrichment/osm_matches.jsonl,
        references/ranked-service-audit/evidence.json and data/verifier_runs/v3_model_found_leads.jsonl.
        """
        import json
        import pandas as pd
        import pyarrow.parquet as pq
        from pathlib import Path

        root = Path(root)
        tables = json.loads((root / manifest).read_text())
        path = lambda name: root / tables[name]["path"]
        have = set(pq.ParquetFile(path("clean_facilities")).schema_arrow.names)
        columns = [c for c in _FACILITY_COLUMNS + _V3_FACILITY_COLUMNS if c in have]
        places = None
        if "place_lookup" in tables:
            filters = None if post_offices else [("place_type", "!=", "post_office")]
            places = pd.read_parquet(path("place_lookup"), columns=list(_PLACE_COLUMNS), filters=filters)
            places = places[places["latitude"].notna()]
        kwargs.setdefault("index_version", Path(manifest).stem.replace("-manifest", "") + ("+verified" if verified else ""))
        evidence = pd.read_parquet(path("service_evidence"))
        if verified:
            evidence = merge_verified(_records(evidence), verified)

        def optional(relative, given=None):
            if given is not None:
                return given
            file = root / relative
            return file if enrichment and file.exists() else None

        leads = optional("data/verifier_runs/v3_model_found_leads.jsonl", leads)
        if leads:
            evidence = merge_verified(_records(evidence), leads)
        for name, relative in (("website_checks", "data/enrichment/website_checks.jsonl"),
                               ("osm", "data/enrichment/osm_matches.jsonl"),
                               ("public_reviews", "references/ranked-service-audit/evidence.json")):
            source = optional(relative, kwargs.get(name))
            if source is not None and not isinstance(source, list):
                text = Path(source).read_text(encoding="utf-8")
                source = json.loads(text) if str(source).endswith(".json") else \
                    [json.loads(line) for line in text.splitlines() if line.strip()]
            kwargs[name] = source
        gaps = optional("data/analytics/access_gaps.json", kwargs.get("access_gaps"))
        if gaps is not None and not isinstance(gaps, dict):
            gaps = json.loads(Path(gaps).read_text(encoding="utf-8"))
        kwargs["access_gaps"] = gaps
        return cls(
            evidence, pd.read_parquet(path("clean_facilities"), columns=columns),
            pd.read_parquet(path("postal_reference"), columns=list(_POSTAL_COLUMNS)),
            pd.read_parquet(path("external_corroboration")) if "external_corroboration" in tables else None,
            external_matches=pd.read_parquet(path("external_service_matches")) if "external_service_matches" in tables else None,
            places=places, **kwargs)

    @classmethod
    def from_spark(cls, spark, schema, post_offices=True, **kwargs):
        """Load from a build schema such as workspace.cmu_datathon_backend_<suffix> (v2 or v3 tables)."""
        table = lambda name: spark.table(f"{schema}.{name}")
        exists = lambda name: spark.catalog.tableExists(f"{schema}.{name}")
        kwargs.setdefault("index_version", schema)
        facilities = table("clean_facilities")
        columns = [c for c in _FACILITY_COLUMNS + _V3_FACILITY_COLUMNS if c in facilities.columns]
        places = None
        if exists("place_lookup"):
            places = table("place_lookup").select(*_PLACE_COLUMNS).where("latitude IS NOT NULL")
            if not post_offices:
                places = places.where("place_type != 'post_office'")
            places = places.toPandas()
        return cls(
            table("service_evidence").toPandas(), facilities.select(*columns).toPandas(),
            table("postal_reference").select(*_POSTAL_COLUMNS).toPandas(),
            table("external_corroboration").toPandas() if exists("external_corroboration") else None,
            external_matches=table("external_service_matches").toPandas() if exists("external_service_matches") else None,
            places=places, **kwargs)

    # ---- origin -----------------------------------------------------------

    def resolve_origin(self, query=None, latitude=None, longitude=None):
        """Resolve coordinates, a six-digit PIN, or 'place[, state]' to a point with stated precision."""
        if latitude is None and isinstance(query, str):
            digits = re.sub(r"[\s\-]", "", query)
            if re.fullmatch(r"\d{6}", digits):  # a PIN, however it is spaced
                if digits[0] == "0":
                    raise RequestError("ORIGIN_NOT_FOUND", f"{digits} is not a PIN code. PIN codes do not start with 0.")
                return self._pin_origin(digits)
            pair = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*", query)
            if pair:
                latitude, longitude = float(pair.group(1)), float(pair.group(2))
        if latitude is not None:
            if longitude is None or not (6.0 <= latitude <= 37.5 and 68.0 <= longitude <= 97.5):
                raise RequestError("ORIGIN_OUT_OF_RANGE", "Coordinates must be a latitude and longitude inside India.")
            return {"kind": "coordinates", "display_name": f"{latitude:.4f}, {longitude:.4f}", "latitude": latitude,
                    "longitude": longitude, "precision": "coordinates", "uncertainty_km": 0.0, "warning": None,
                    "city_key": None, "state_key": None}
        if not isinstance(query, str) or not query.strip():
            raise RequestError("ORIGIN_REQUIRED", "Enter a city, district, six-digit PIN or coordinates.")
        text = query.strip()
        typed = text.split(",")[0].strip()[:80]  # what to echo back in a message
        parts = [p for p in (place_key(p) for p in text.split(",")) if p and p != "india"]
        if not parts:
            raise RequestError("ORIGIN_REQUIRED", "Enter a city, district, six-digit PIN or coordinates.")
        state = None
        if len(parts) > 1:
            state = self._state(parts[-1])
            if state is None:
                raise RequestError("ORIGIN_NOT_FOUND", f"'{parts[-1]}' is not a recognized state or union territory.",
                                   sorted(self.states))
        place = parts[0]
        if state is None:  # "Patna Bihar" without a comma
            words = place.split()
            for n in (4, 3, 2, 1):
                if len(words) > n and self._state(" ".join(words[-n:])):
                    state, place = self._state(" ".join(words[-n:])), " ".join(words[:-n])
                    break

        def lookup(name):
            hits = {}
            for kind, table in (("district", self.districts), ("city", self.cities)):  # a city overrides its district
                for (state_key, key), centre in table.items():
                    if key == name and state in (None, state_key):
                        hits[state_key] = (kind, centre)
            return hits

        names = [place, _PLACE_ALIASES.get(place)] + [old for old, new in _PLACE_ALIASES.items() if new == place]
        found, renamed = {}, None
        for name in (n for n in names if n):
            found = lookup(name)
            if found:
                renamed, place = (name if name != place else None), name
                break
        if not found:
            for name in (n for n in names if n):
                looked_up = self._place_origin(name, typed, state)
                if looked_up:
                    return looked_up
            known = sorted({key for state_key, key in (*self.cities, *self.districts) if state in (None, state_key)})
            raise RequestError("ORIGIN_NOT_FOUND", f"No city or district named '{typed}' was found.",
                               difflib.get_close_matches(place, known, n=5, cutoff=0.75))
        merged = None
        if len(found) > 1:
            centres = [c for _, c in found.values()]
            if all(haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) <= SAME_PLACE_KM
                   for a in centres for b in centres):
                # One city that the records list under more than one state, such as Hyderabad.
                keep = max(found, key=lambda k: (found[k][1]["points"], k))
                merged, found = sorted(found), {keep: found[keep]}
            else:
                raise RequestError(
                    "ORIGIN_AMBIGUOUS", f"'{typed}' exists in more than one state. Choose one.",
                    [{"query": f"{place.title()}, {s.title()}", "kind": kind}
                     for s, (kind, _) in sorted(found.items())])
        (state_key, (kind, centre)), = found.items()
        basis = ("the median location of %d facility records listed in this city" if kind == "city"
                 else "the median of %d postal-area points in this district") % centre["points"]
        return {"kind": kind, "display_name": f"{place.title()}, {state_key.title()}",
                "latitude": centre["latitude"], "longitude": centre["longitude"],
                "precision": "locality" if kind == "city" else "district",
                "uncertainty_km": round(max(centre["spread_km"], 3.0 if kind == "city" else 10.0), 1),
                "warning": f"The {kind} is treated as one point: {basis}. Distances are approximate."
                           + (f" '{typed}' is listed in the records as {place.title()}." if renamed else "")
                           + (f" The records list it under {' and '.join(m.title() for m in merged)}; these are "
                              "treated as one place." if merged else ""),
                "city_key": place if kind == "city" else None, "state_key": state_key}

    def _place_origin(self, key, typed, state):
        """Resolve a name through the release's place lookup. Several distinct places raise ORIGIN_AMBIGUOUS."""
        rows = [r for r in self.places.get(key, ()) if state in (None, r.get("state_key"))]
        if not rows:
            return None
        best = {}
        for row in sorted(rows, key=lambda r: ({"city": 0, "district": 1, "pin": 2}.get(r["place_type"], 3),
                                                r.get("state_key") or "", r.get("district_key") or "", r["place_id"])):
            best.setdefault(row["place_id"], row)
        rows = list(best.values())
        towns = [r for r in rows if r["place_type"] in ("city", "district")]
        if len({r["state_key"] for r in towns}) == 1:
            rows = towns[:1]  # a city or district outranks post offices that share its name

        def label(row):
            where = [str(row["place_name"]).title()]
            if row.get("district_key") and row["district_key"] != place_key(row["place_name"]):
                where.append(row["district_key"].title() + " district")
            where.append((row.get("state_key") or "").title())
            return ", ".join(w for w in where if w) + (f" {row['pincode']}" if row.get("pincode") else "")

        if len(rows) > 1:
            raise RequestError(
                "ORIGIN_AMBIGUOUS", f"'{typed}' matches {len(rows)} places. Choose one.",
                [{"query": f"{r['latitude']:.5f}, {r['longitude']:.5f}", "label": label(r), "kind": r["place_type"],
                  "state": r.get("state_key"), "district": r.get("district_key"), "pincode": r.get("pincode")}
                 for r in rows[:25]])
        row, = rows
        precision, floor, basis = _PLACE_PRECISION.get(row["point_basis"], ("locality", 10.0, "a directory point"))
        return {"kind": row["place_type"], "display_name": label(row), "latitude": row["latitude"],
                "longitude": row["longitude"], "precision": precision, "uncertainty_km": floor,
                "warning": f"This place is treated as one point: {basis}. Distances are approximate.",
                "city_key": key if row["place_type"] == "city" else None, "state_key": row.get("state_key")}

    def _state(self, key):
        return key if key in self.states else _STATE_ALIASES.get(key)

    def _pin_origin(self, pin):
        row = self.postal.get(pin)
        if row is None:
            raise RequestError("ORIGIN_NOT_FOUND", f"PIN {pin} is not in the India Post directory.")
        if row.get("pin_latitude") is not None:
            return {"kind": "pincode", "display_name": f"PIN {pin}", "latitude": row["pin_latitude"],
                    "longitude": row["pin_longitude"], "precision": "pincode_approximate",
                    "uncertainty_km": round(max(row.get("coordinate_span_km") or 0.0, 2.0), 1),
                    "warning": "A PIN covers an area. Its post offices give an approximate point.",
                    "city_key": None, "state_key": row.get("state_key")}
        area = next((r for r in self.places.get(pin, ()) if r["place_type"] == "pin"), None)
        if area is not None:
            return {"kind": "pincode", "display_name": f"PIN {pin}", "latitude": area["latitude"],
                    "longitude": area["longitude"], "precision": "pincode_approximate", "uncertainty_km": 5.0,
                    "warning": "A PIN covers an area. Its post offices give an approximate point.",
                    "city_key": None, "state_key": row.get("state_key")}
        centre = self.districts.get((row.get("state_key"), row.get("district_key")))
        if centre is None:
            raise RequestError("ORIGIN_UNRESOLVED", f"PIN {pin} has no reliable location in the directory. "
                               "Enter the city or district instead.")
        return {"kind": "pincode", "display_name": f"PIN {pin} ({row['district_key'].title()} district)",
                "latitude": centre["latitude"], "longitude": centre["longitude"], "precision": "district",
                "uncertainty_km": round(max(centre["spread_km"], 10.0), 1),
                "warning": "This PIN's post offices are too scattered to place, so its district centre is used.",
                "city_key": None, "state_key": row.get("state_key")}

    # ---- scoring ----------------------------------------------------------

    def _evidence_strength(self, row):
        """Graded strength of the record's own text. Repeats of one kind beyond two were dropped by the build."""
        cites = row.get("citations") or []
        direct = [c for c in cites if c["kind"] == "direct"]
        support = direct + [c for c in cites if c["kind"] == "equipment"]
        parts = []
        if direct:
            parts.append(("direct_statement", 0.50, "The record states the service directly."))
        if len(direct) > 1 and difflib.SequenceMatcher(
                None, direct[0]["quote"].casefold(), direct[1]["quote"].casefold()).ratio() < 0.8:
            parts.append(("second_statement", 0.10, "A second, differently worded statement appears in the record."))
        if any(len(c["quote"].split()) >= 4 or any(ch.isdigit() for ch in c["quote"]) for c in support):
            parts.append(("descriptive_detail", 0.15, "A statement gives detail beyond naming the service."))
        if any(c["kind"] == "equipment" for c in cites):
            parts.append(("equipment_listed", 0.10, "Matching equipment is listed."))
        if any(c["kind"] == "specialty" for c in cites):
            parts.append(("related_specialty", 0.05, "A related specialty is listed."))
        outside = [m for m in self.matches.get((row["record_key"], row["service"]), ())
                   if m.get("comparison_status") == _INDEPENDENT_MATCH and not m.get("external_location_conflict")]
        if outside:
            parts.append(("outside_source", 0.10, f"An outside source also lists this service: "
                          f"{outside[0]['source_title']} ({outside[0].get('source_period') or 'undated'})."))
        page = row.get("page_update_date")
        if page:
            age_days = (self.as_of - date.fromisoformat(page)).days
            recent = 0 <= age_days <= 730
            parts.append(("dated_source", 0.10 if recent else 0.05,
                          f"The source page was updated on {page}." + ("" if recent else " That is over two years old.")))
        return min(1.0, sum(p[1] for p in parts)), parts

    def _evidence_checks(self, row, parts=None):
        """The five checks behind the service-evidence component, each with what was found and where."""
        if parts is None:
            parts = self._evidence_strength(row)[1]
        seen = {p[0] for p in parts}
        key = (row["record_key"], row["service"])
        checks = [("record_states_service", "direct_statement" in seen,
                   "The facility's record states the service, and the quote was checked against the record."
                   if "direct_statement" in seen else "The record does not state the service directly.")]
        specific = seen & {"descriptive_detail", "second_statement", "equipment_listed"}
        checks.append(("statement_is_specific", bool(specific),
                       "The record gives more than the service's name: a detail, a second statement or matching "
                       "equipment." if specific else "The record only names the service, with no supporting detail."))

        v = row.get("verifier") or {}
        if v.get("outcome") == "agree_supported":
            model = (True, "A model reading of the record agrees that it states the service.")
        elif v.get("outcome") == "second_opinion_supported":
            model = (True, "A model re-check supported the statement after the first reading disagreed or failed.")
        elif not v or v.get("status") in (None, "not_run", "no_passages"):
            model = (None, "No model reading is available for this claim.")
        else:
            model = (False, "The model reading did not support the statement.")
        checks.append(("model_reading_agrees",) + model)

        site, review = self.website.get(key), self.reviews.get(key) or {}
        if site and site.get("status") == "states_service":
            own = (True, "The facility's listed website mentions it in a positive sentence (read on "
                         f"{site.get('fetched_at', 'an unknown date')[:10]}, keyword match): "
                         f"“{(site.get('quote') or '')[:160]}”")
        elif review.get("finding") == "provider":
            own = (True, "The provider's own published material documents it (reviewed "
                         f"{review.get('reviewed_on')}): {review.get('finding_detail') or ''}".strip())
        elif site and site.get("status") == "site_reachable_no_mention":
            own = ("not_found", f"Not found on the {site.get('pages_read') or 'few'} page(s) of the facility's "
                                f"listed website read on {site.get('fetched_at', '')[:10]}. This is weak evidence: "
                                "only a few pages were read.")
        else:
            own = (None, "The facility's own website was not checked or could not be reached.")
        checks.append(("facility_source_states_it",) + own)

        outside = [m for m in self.matches.get(key, ())
                   if m.get("comparison_status") == _INDEPENDENT_MATCH and not m.get("external_location_conflict")]
        tag = (self.osm.get(row["record_key"]) or {}).get("service_tag") or {}
        if review.get("finding") in ("recent_independent", "historical_independent"):
            older = " This source is historical." if review["finding"] == "historical_independent" else ""
            independent = (True, f"An independent public source documents it (reviewed {review.get('reviewed_on')}): "
                                 f"{review.get('finding_detail') or ''}".strip() + older)
        elif outside:
            independent = (True, f"An independent source lists it: {outside[0]['source_title']} "
                                 f"({outside[0].get('source_period') or 'undated'}).")
        elif row["service"] in tag:
            independent = (True, "OpenStreetMap contributors tagged this facility with the service.")
        elif review.get("finding") == "unresolved":
            independent = (False, f"Public sources were searched on {review.get('reviewed_on')} and did not settle it.")
        else:
            independent = (None, "No independent source has been checked for this claim.")
        checks.append(("independent_source_lists_it",) + independent)
        return [{"key": k, "passed": passed is True if passed != "not_found" else False,
                 "status": "passed" if passed is True else "not_checked" if passed is None
                 else "not_found" if passed == "not_found" else "not_passed", "text": text}
                if passed is not None else {"key": k, "passed": None, "status": "not_checked", "text": text}
                for k, passed, text in checks]

    def _map_check(self, facility):
        """What OpenStreetMap shows near the recorded point, when the release includes that check."""
        o = self.osm.get(facility["record_key"])
        if not o:
            return None
        return {"source": "OpenStreetMap", "status": o.get("status"), "name": o.get("osm_name"),
                "distance_m": o.get("distance_m"), "url": o.get("osm_url"), "checked_on": (o.get("fetched_at") or "")[:10],
                "attribution": "© OpenStreetMap contributors"}

    def access_gaps(self, service, state=None, limit=25, order="farthest"):
        """Districts by distance to the nearest recommendable facility for a service, with survey context."""
        service = resolve_service(service)
        block = (self.gaps.get("services") or {}).get(service)
        if block is None:
            raise RequestError("ACCESS_GAPS_UNAVAILABLE", "No district summary is available for this service.",
                               sorted((self.gaps.get("services") or {})))
        state = self._state(place_key(state)) if state else None
        rows = [d for d in block["districts"] if state in (None, d["state"])]
        if order == "blind_spots":
            rows = [d for d in rows if d.get("dataset_blind_spot")]
        elif order == "nearest":
            rows = sorted(rows, key=lambda d: (d["median_km"], d["state"], d["district"]))
        return {"contract_version": CONTRACT_VERSION, "service_code": service, "service_display_name": SERVICES[service],
                "generated_on": self.gaps.get("generated_on"), "unit": self.gaps.get("unit"),
                "caveat": self.gaps.get("caveat"), "survey_source": self.gaps.get("survey_source"),
                "recommendable_facilities": block["recommendable_facilities"], "national": block["national"],
                "state": state, "districts_total": len(rows), "districts": rows[:max(1, min(int(limit), 800))]}

    def _area_context(self, origin, service):
        """The district summary for the PIN area nearest the search origin, when a summary was built."""
        if not self._gap_index:
            return None
        best, best_km = None, 60.0  # farther than this from any PIN area, say nothing
        for row in self.postal.values():
            if row.get("pin_latitude") is None or not row.get("district_key"):
                continue
            if abs(row["pin_latitude"] - origin["latitude"]) > 0.6:
                continue
            km = haversine_km(origin["latitude"], origin["longitude"], row["pin_latitude"], row["pin_longitude"])
            if km < best_km:
                best, best_km = row, km
        entry = best and self._gap_index.get((service, best["state_key"], best["district_key"]))
        if not entry:
            return None
        return dict(entry, district_display=f"{entry['district'].title()}, {entry['state'].title()}",
                    national_median_km=self.gaps["services"][service]["national"]["median_km"],
                    caveat=self.gaps.get("caveat"), survey_source=self.gaps.get("survey_source"))

    def _city_offset_km(self, facility):
        """Distance from the mapped point to the other records of the same city, when there are enough to compare."""
        centre = self.cities.get((facility.get("state_key"), facility.get("city_key")))
        if centre is None or centre["points"] < 3 or facility.get("usable_latitude") is None:
            return None
        return haversine_km(centre["latitude"], centre["longitude"],
                            facility["usable_latitude"], facility["usable_longitude"])

    def _location_reliability(self, facility):
        km, offset = facility.get("pin_distance_km"), self._city_offset_km(facility)
        if km is None and offset is None:
            value, text = 0.4, "The mapped point could not be checked against a PIN area or other records in the city."
        elif km is None:
            value = 0.7 if offset <= 25 else 0.5
            text = (f"No PIN check was possible. The mapped point is {offset:.0f} km from the centre of other "
                    "records listed in the same city.")
        elif km <= 5:
            value, text = 1.0, f"The mapped point is {km:.1f} km from the centre of its PIN area."
        elif km <= 25:
            value, text = 0.8, f"The mapped point is {km:.0f} km from the centre of its PIN area."
        else:
            value, text = 0.5, f"The mapped point is {km:.0f} km from the centre of its PIN area, so the pin may be off."
        if km is not None and offset is not None and offset > MAX_CITY_OFFSET_KM:
            value = min(value, 0.5)
            text += f" The listed city is {offset:.0f} km from that point, so the city name may be wrong."
        if facility.get("pin_geography_status") == "ambiguous":
            value, text = min(value, 0.7), text + " The PIN spans more than one district."
        o = self.osm.get(facility["record_key"]) or {}
        if o.get("status") == "name_match" and o.get("distance_m") is not None:
            value = max(value, 1.0 if o["distance_m"] <= 250 else 0.9)
            text += (f" OpenStreetMap maps a facility with a matching name {o['distance_m']:.0f} m from the "
                     "recorded point.")
        elif o.get("status") == "nothing_mapped_nearby":
            text += " OpenStreetMap maps no health facility within 1 km of the recorded point."
        return value, text

    def _citations(self, row, kinds=None):
        out = []
        for n, cite in enumerate(row.get("citations") or []):
            if kinds and cite["kind"] not in kinds:
                continue
            index = cite.get("item_index")
            index = None if index is None else int(index)
            field = cite["field"] + (f"[{index}]" if index is not None else "")
            out.append({
                "evidence_id": f"{row['record_key'][:12]}:{row['service']}:{n}", "record_id": row["record_key"],
                "source_field": field, "exact_quote": cite["quote"], "classification": cite["kind"],
                "source_attribution_status": "RECORD_LEVEL_ONLY",
            })
        return out

    citations = _citations  # public name for callers outside this module, e.g. backend.api

    @staticmethod
    def _model_check(row):
        """Summary of the model verifier's verdict when the evidence rows came from backend.verifier."""
        v = row.get("verifier")
        if not v:
            return None
        receipt = v.get("receipt") or {}
        return {"outcome": v.get("outcome"), "status": v.get("status"), "label": v.get("label"),
                "rule_eligible": v.get("rule_eligible"), "model": receipt.get("model"),
                "prompt_version": receipt.get("prompt_version"), "validation": receipt.get("validation"),
                "first_outcome": v.get("first_outcome"), "second_opinion": v.get("second_opinion")}

    def _external_references(self, row):
        return [{"source_title": m.get("source_title"), "source_url": m.get("source_url"),
                 "source_role": m.get("source_role"), "source_period": m.get("source_period"), "claim": m.get("claim"),
                 "comparison_status": m.get("comparison_status"), "branch_check": m.get("branch_check"),
                 "checked_on": m.get("checked_on"),
                 "independent": m.get("comparison_status") == _INDEPENDENT_MATCH}
                for m in self.matches.get((row["record_key"], row["service"]), ())]

    @staticmethod
    def _facility_public(facility):
        """Identity, location and reported contact details of one record, without any search context."""
        lat, lon = facility.get("usable_latitude"), facility.get("usable_longitude")
        area = None
        if lat is None and facility.get("best_estimate_latitude") is not None:
            # For showing an area on a map only. It is never used to compute a distance.
            area = {"latitude": facility["best_estimate_latitude"], "longitude": facility["best_estimate_longitude"],
                    "basis": facility.get("best_estimate_basis"), "precision": facility.get("best_estimate_precision"),
                    "confidence": facility.get("best_estimate_confidence")}
        if "phone_status" in facility:  # v3: show a number only when the build found nothing wrong with it
            phone = facility.get("official_phone_e164") if facility.get("phone_status") == "listed_unconfirmed" else None
            website = facility.get("official_web_host") if facility.get("website_status") == "listed_unconfirmed" else None
        else:
            phone, website = _value(facility.get("officialPhone")), _value(facility.get("officialWebsite"))
        size = facility.get("duplicate_group_size") or 1
        return {
            "facility_id": facility["record_key"], "unique_id": facility.get("unique_id"),
            "display_name": facility.get("name"), "facility_type": facility.get("facilityTypeId"),
            "identity_severity": facility.get("identity_severity"),
            "duplicate_group": {"id": facility.get("duplicate_group_id"), "size": int(size)} if size > 1 else None,
            "location": {
                "address_label": _value(facility.get("address_display")) or ", ".join(
                    str(facility[k]) for k in ("address_line1", "address_city", "address_stateOrRegion")
                    if facility.get(k) and str(facility[k]).casefold() != "null"),
                "city": facility.get("address_city"), "state": facility.get("address_stateOrRegion"),
                "pincode": facility.get("pincode"), "latitude": lat, "longitude": lon,
                "location_status": "CONFLICTING" if facility.get("location_review") else "CONSISTENT",
                "location_flags": facility.get("location_flags") or [],
                "approximate_area": area,
            },
            "contact": {"phone": phone, "website": website, "phone_status": facility.get("phone_status"),
                        "website_status": facility.get("website_status"), "phone_branch_verified": False},
        }

    def _base(self, row, facility, origin):
        lat, lon = facility.get("usable_latitude"), facility.get("usable_longitude")
        km = None if lat is None else haversine_km(origin["latitude"], origin["longitude"], lat, lon)
        item = self._facility_public(facility)
        item.update({
            "distance": {"value_km": None if km is None else round(km, 1), "type": "straight_line",
                         "is_approximate": True, "origin_uncertainty_km": origin["uncertainty_km"]},
            "source_urls": (row.get("record_source_urls") or [])[:5],
            "source_published_at": row.get("page_update_date"),
            "model_check": self._model_check(row),
            "external_references": self._external_references(row),
            "map_check": self._map_check(facility),
            "_km": km,
        })
        return item

    def facility_profile(self, record_key):
        """Everything the index holds about one record: where it is and what its text says for each service."""
        facility = self.facilities.get(record_key)
        if facility is None:
            raise RequestError("FACILITY_NOT_FOUND", "No facility record has this id.")
        profile = self._facility_public(facility)
        profile["map_check"] = self._map_check(facility)
        if profile["duplicate_group"]:
            profile["duplicate_group"]["other_records"] = sorted(
                (f["record_key"] for f in self.facilities.values()
                 if f.get("duplicate_group_id") == facility["duplicate_group_id"] and f["record_key"] != record_key))
        rows = sorted(self.by_record.get(record_key, ()), key=lambda r: list(SERVICES).index(r["service"]))
        profile["services"] = [{
            "code": r["service"], "display_name": SERVICES[r["service"]], "evidence_status": r["evidence_status"].upper(),
            "eligible_for_shortlist": bool(r["eligible"]), "flags": r.get("flags") or [],
            "manual_review_reason": r.get("manual_review_reason"), "source_published_at": r.get("page_update_date"),
            "source_urls": (r.get("record_source_urls") or [])[:5], "evidence_preview": self._citations(r),
            "model_check": self._model_check(r), "external_references": self._external_references(r),
            "evidence_checks": self._evidence_checks(r),
        } for r in rows]
        profile["context"] = {"doctors_reported": facility.get("doctors_reported"),
                              "beds_reported": facility.get("capacity_reported"),
                              "operator": _value(facility.get("operatorTypeId"))}
        return profile

    # ---- search -----------------------------------------------------------

    def recommend(self, service, origin, max_distance_km=None, limit=5, ranking_mode="evidence_first",
                  include_unverified_leads=True):
        """Return the v1 search response as a plain dict. Raises RequestError for unusable input."""
        service = resolve_service(service)
        if isinstance(origin, str):
            origin = self.resolve_origin(origin)
        if ranking_mode not in WEIGHTS:
            raise RequestError("INVALID_RANKING_MODE", f"Unknown ranking mode '{ranking_mode}'.", sorted(WEIGHTS))
        if max_distance_km is not None and not 0 < max_distance_km <= 3500:
            raise RequestError("INVALID_DISTANCE", "The maximum distance must be between 0 and 3,500 km.")
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise RequestError("INVALID_LIMIT", "The number of results must be between 1 and 50.")
        weights, half = WEIGHTS[ranking_mode], HALF_DISTANCE_KM[service]

        eligible, leads, set_aside, unplaced, found = [], [], [], [], []
        counts = defaultdict(int)
        rows = list(self.evidence[service])
        if service == "emergency_c_section":
            # Planned capability is a different question from emergency capability; keep these as leads only.
            stated = {r["record_key"] for r in rows if r["eligible"]}
            related = [dict(r, _related=True) for r in self.evidence["c_section"]
                       if r["eligible"] and r["record_key"] not in stated]
            # A record that rules out emergency C-sections stays set aside; its planned capability is not a lead.
            ruled_out = {r["record_key"] for r in rows if r["evidence_status"] in _RULED_OUT}
            related = [r for r in related if r["record_key"] not in ruled_out]
            related_keys = {r["record_key"] for r in related}
            rows = [r for r in rows if r["record_key"] not in related_keys] + related
        for row in rows:
            facility = self.facilities[row["record_key"]]
            item = self._base(row, facility, origin)
            status, kinds = row["evidence_status"], {c["kind"] for c in row.get("citations") or []}
            external = self.external.get((row["record_key"], service), [])
            matches = self.matches.get((row["record_key"], service), [])
            offset = self._city_offset_km(facility)
            misplaced = facility.get("pin_distance_km") is None and offset is not None and offset > MAX_CITY_OFFSET_KM
            if row.get("_related"):
                tier, reasons = "lead", ["The record documents C-sections but does not say they are done as emergencies."]
            elif row["eligible"] and item["_km"] is not None and misplaced:
                tier = "lead"
                reasons = [f"The mapped point is {offset:.0f} km from other records listed in "
                           f"{facility.get('address_city')}, and no PIN confirms it. Confirm the address."]
                item["location"]["location_status"] = "AMBIGUOUS"
            elif row["eligible"] and item["_km"] is not None:
                tier, reasons = "eligible", []
            elif (row.get("verifier") or {}).get("outcome") == MODEL_FOUND:
                tier = "model_found"
                reasons = ["A model reading found a statement of this service that the text rules did not accept. "
                           "Verify it before use."]
                reasons += [_HOLD_REASONS[f] for f in (row["verifier"].get("blockers") or []) if f in _HOLD_REASONS]
            elif status in _RULED_OUT:
                tier, reasons = "set_aside", [_RULED_OUT[status]]
            elif status in ("documented", "mixed", "review"):
                tier = "lead"
                reasons = [_HOLD_REASONS[f] for f in row.get("flags") or [] if f in _HOLD_REASONS] or \
                          ["The statements about this service need a reviewer."]
            elif "equipment" in kinds:
                tier, reasons = "lead", ["The service appears only in the equipment list, not as a stated service."]
            elif matches:
                tier, reasons = "lead", [f"An outside source ({matches[0]['source_title']}) lists this service, but the "
                                         "facility's own record text does not state it."]
            elif external:
                tier, reasons = "lead", [f"Listed in {e['source_title']} ({e['programme']} programme), but the "
                                         "facility's own record text does not mention the service." for e in external[:1]]
            else:
                counts["weak_mentions"] += 1  # a related specialty or passing mention only
                continue
            item.update(_tier=tier, _row=row, _facility=facility, reasons=reasons)
            if item["_km"] is None:
                counts["location_unusable"] += 1
                if origin.get("city_key") and (facility.get("state_key"), facility.get("city_key")) == \
                        (origin["state_key"], origin["city_key"]):
                    unplaced.append(item)
            else:
                {"eligible": eligible, "lead": leads, "set_aside": set_aside, "model_found": found}[tier].append(item)

        for item in eligible:
            detail, parts = self._evidence_strength(item["_row"])
            checks = self._evidence_checks(item["_row"], parts)
            strength = sum(c["passed"] is True for c in checks) / len(checks)
            item["_checks"], item["_detail"] = checks, detail
            reliability, location_text = self._location_reliability(item["_facility"])
            utility = 0.5 ** (item["_km"] / half)
            points = {"service_evidence": strength, "distance": utility, "location": reliability}
            item["_score"] = 100 * sum(weights[k] * points[k] for k in weights)
            item["_strength"], item["_parts"], item["_location_text"], item["_points"] = strength, parts, location_text, points
        # Equal scores are ordered by how much the record itself says, then by distance.
        eligible.sort(key=lambda i: (-round(i["_score"], 6), -i["_detail"], i["_km"], i["facility_id"]))
        kept, groups = [], {}
        for item in eligible:  # one position per group of records that describe the same facility
            group = (item["duplicate_group"] or {}).get("id")
            if group in groups:
                groups[group]["also_listed_as"].append({"facility_id": item["facility_id"],
                                                        "display_name": item["display_name"]})
                counts["duplicates_folded"] += 1
                continue
            item["also_listed_as"] = []
            if group:
                groups[group] = item
            kept.append(item)
        eligible = kept

        if max_distance_km is not None:
            radius, expanded = float(max_distance_km), False
        else:
            needed = min(3, limit)
            radius = next((r for r in RADIUS_STEPS_KM if sum(i["_km"] <= r for i in eligible) >= needed),
                          RADIUS_STEPS_KM[-1])
            expanded = radius > RADIUS_STEPS_KM[0]
        in_range = [i for i in eligible if i["_km"] <= radius]
        shown = in_range[:limit]
        results = [self._result(rank, item, service, weights, half) for rank, item in enumerate(shown, 1)]
        stability = self._stability(in_range, results)

        horizon = max((i["_km"] for i in shown), default=radius)
        others = sorted((i for i in leads + set_aside if i["_km"] <= horizon), key=lambda i: (i["_km"], i["facility_id"]))
        nearest_any = min(eligible, key=lambda i: i["_km"], default=None)
        warnings = ["These options summarize what facility records say. Nobody has confirmed that the service is "
                    "running today. Call before sending a patient.",
                    "Distances are straight lines, not road distance or travel time."]
        if origin.get("warning"):
            warnings.append(origin["warning"])
        if expanded and results:
            warnings.append(f"Fewer than {min(3, limit)} documented options were found within "
                            f"{RADIUS_STEPS_KM[0]:.0f} km, so the search was widened to {radius:.0f} km.")
        if service == "emergency_c_section":
            warnings.append("Very few records state emergency C-section capability. For an emergency in progress, "
                            "contact local emergency services; do not rely on this list.")
        if not results:
            status = "NO_ELIGIBLE_CANDIDATES"
            message = f"No facility record within {radius:.0f} km documents {service_phrase(service)} clearly enough to recommend."
            if nearest_any is not None:
                message += (f" The nearest documented option is {nearest_any['display_name']}, about "
                            f"{nearest_any['_km']:.0f} km away.")
        else:
            status, message = "RESULTS_WITH_UNCERTAINTY", None

        return {
            "contract_version": CONTRACT_VERSION, "index_version": self.index_version,
            "ranking_policy_version": POLICY_VERSION, "status": status, "message": message,
            "query": {"service_code": service, "service_display_name": SERVICES[service],
                      "origin": {k: origin[k] for k in ("kind", "display_name", "latitude", "longitude", "precision",
                                                         "uncertainty_km", "warning")},
                      "max_distance_km": radius, "radius_auto_expanded": expanded, "ranking_mode": ranking_mode},
            "summary": {"documented_in_range": len(in_range), "shown_count": len(results),
                        "leads_in_range": sum(i["_tier"] == "lead" for i in others),
                        "set_aside_in_range": sum(i["_tier"] == "set_aside" for i in others),
                        "records_without_usable_location": counts["location_unusable"],
                        "weak_mentions_not_listed": counts["weak_mentions"],
                        "duplicate_records_folded": counts["duplicates_folded"],
                        "model_found_in_range": sum(i["_km"] <= radius for i in found)},
            "results": results,
            "closer_but_not_recommended": [self._other(i) for i in others if results and i["_km"] < shown[0]["_km"]][:5],
            "unverified_leads": [self._other(i) for i in others if i["_tier"] == "lead"][:limit] if include_unverified_leads else [],
            "set_aside": [self._other(i) for i in others if i["_tier"] == "set_aside"][:limit],
            "model_found_leads": [self._other(i) for i in sorted(
                (i for i in found if i["_km"] <= radius), key=lambda i: (i["_km"], i["facility_id"]))][:limit]
            if include_unverified_leads else [],
            "location_unverified_leads": [self._other(i) for i in unplaced][:limit] if include_unverified_leads else [],
            "ranking_stability": stability,
            "area_context": self._area_context(origin, service),
            "how_ranked": self.how_ranked(service, ranking_mode),
            "global_warnings": warnings,
        }

    @staticmethod
    def _stability(in_range, results):
        """Re-order the in-range claims under other weightings and report how far each shown result moves."""
        if not results:
            return None
        position = {}
        for evidence, distance, location in ALTERNATIVE_WEIGHTS:
            order = sorted(in_range, key=lambda i: (
                -round(evidence * i["_points"]["service_evidence"] + distance * i["_points"]["distance"]
                       + location * i["_points"]["location"], 6), -i["_detail"], i["_km"], i["facility_id"]))
            for rank, item in enumerate(order, 1):
                position.setdefault(item["facility_id"], []).append(rank)
        firsts = {next(k for k, ranks in position.items() if ranks[n] == 1) for n in range(len(ALTERNATIVE_WEIGHTS))}
        for result in results:
            ranks = position[result["facility_id"]] + [result["rank"]]
            result["ranking"]["rank_range"] = {"best": min(ranks), "worst": max(ranks)}
        top = results[0]
        stable = firsts == {top["facility_id"]}
        names = {i["facility_id"]: i["display_name"] for i in in_range}
        same = len({(i["_points"]["service_evidence"], i["_points"]["location"]) for i in in_range}) == 1
        if same and len(in_range) > 1:
            return {"weightings_tested": [{"service_evidence": e, "distance": d, "location": l}
                                          for e, d, l in ALTERNATIVE_WEIGHTS],
                    "top_choice_stable": True, "other_first_choices": [], "decided_by": "distance",
                    "text": "These claims pass the same evidence checks and have the same location agreement, so "
                            "this list is ordered by distance alone and no choice of weights would change it."}
        return {"weightings_tested": [{"service_evidence": e, "distance": d, "location": l}
                                      for e, d, l in ALTERNATIVE_WEIGHTS],
                "top_choice_stable": stable,
                "other_first_choices": sorted(names[k] for k in firsts if k != top["facility_id"]),
                "text": (f"{top['display_name']} stays first under all {len(ALTERNATIVE_WEIGHTS)} other weightings "
                         "we tried." if stable else
                         f"The first place depends on the weights: under other weightings "
                         f"{', '.join(sorted(names[k] for k in firsts if k != top['facility_id']))} would come first. "
                         "Compare the top results before choosing.")}

    def compare(self, service, origin, first, second):
        """Explain the difference between two facilities for one search, component by component."""
        response = self.recommend(service, origin, max_distance_km=3500, limit=50)
        pool = {r["facility_id"]: r for r in response["results"]}
        missing = [k for k in (first, second) if k not in pool]
        if missing:
            raise RequestError("NOT_COMPARABLE", "Only recommended facilities can be compared; a lead or a record "
                               "with a hold has no score.", missing)
        a, b = pool[first], pool[second]
        value = lambda r, key: next(c for c in r["ranking"]["components"] if c["key"] == key)
        rows = []
        for key, label in (("service_evidence", "Service evidence"), ("distance", "Distance"),
                           ("location", "Location agreement")):
            pa, pb = value(a, key)["weighted_points"], value(b, key)["weighted_points"]
            rows.append({"key": key, "label": label, "first_points": pa, "second_points": pb,
                         "favours": "first" if pa > pb else "second" if pb > pa else "neither"})
        checks = [{"key": ca["key"], "first": ca["status"], "second": cb["status"], "first_text": ca["text"],
                   "second_text": cb["text"]}
                  for ca, cb in zip(a["ranking"]["evidence_checks"], b["ranking"]["evidence_checks"])]
        ahead, behind = (a, b) if a["ranking"]["score"] >= b["ranking"]["score"] else (b, a)
        gap = round(ahead["ranking"]["score"] - behind["ranking"]["score"], 1)
        reasons = [r["label"].lower() for r in rows
                   if (r["favours"] == "first") == (ahead is a) and r["favours"] != "neither"]
        against = [r["label"].lower() for r in rows
                   if (r["favours"] == "first") != (ahead is a) and r["favours"] != "neither"]
        text = (f"{ahead['display_name']} scores {gap} points higher"
                + (f" because of {' and '.join(reasons)}" if reasons else "")
                + (f"; {behind['display_name']} does better on {' and '.join(against)}" if against else "") + ". "
                f"{ahead['display_name']} is {ahead['distance']['value_km']} km away and passes "
                f"{ahead['service']['evidence_checks_passed']} of 5 evidence checks; {behind['display_name']} is "
                f"{behind['distance']['value_km']} km away and passes {behind['service']['evidence_checks_passed']}. "
                "Neither has been confirmed as available today.") if gap else (
                f"{a['display_name']} and {b['display_name']} score the same; compare the distance and the evidence "
                "checks below. Neither has been confirmed as available today.")
        return {"contract_version": CONTRACT_VERSION, "ranking_policy_version": POLICY_VERSION,
                "query": response["query"], "first": a, "second": b, "components": rows, "evidence_checks": checks,
                "score_gap": gap, "higher": ahead["facility_id"] if gap else None, "summary": text}

    def _result(self, rank, item, service, weights, half):
        row, strength = item["_row"], item["_strength"]
        direct_ids = [c["evidence_id"] for c in self._citations(row, {"direct"})]
        passed = sum(c["passed"] is True for c in item["_checks"])
        level = "STRONG" if passed >= 4 else "MODERATE" if passed == 3 else "MINIMAL"
        why = [{"text": f"This claim passes {passed} of {len(item['_checks'])} evidence checks: "
                        + " ".join(c["text"] for c in item["_checks"] if c["passed"] is True),
                "evidence_ids": direct_ids},
               {"text": f"About {item['_km']:.0f} km from the search location in a straight line.", "evidence_ids": []},
               {"text": item["_location_text"], "evidence_ids": []}]
        uncertainty = [{"code": "current_availability_not_confirmed",
                        "text": "No one has confirmed the service is operating or has capacity today."}]
        if not row.get("page_update_date"):
            uncertainty.append({"code": "source_date_unknown", "text": "The source page has no usable date, so the claim may be old."})
        if not item["_checks"][1]["passed"]:
            uncertainty.append({"code": "service_named_only", "text": "The service appears only as a short list entry."})
        unchecked = [c["key"] for c in item["_checks"] if c["passed"] is None]
        if unchecked:
            uncertainty.append({"code": "evidence_checks_not_run", "text": "Not checked for this claim: "
                                + ", ".join(k.replace("_", " ") for k in unchecked) + "."})
        if item["_points"]["location"] < 0.8:
            uncertainty.append({"code": "map_point_uncertain", "text": item["_location_text"]})
        if item["distance"]["origin_uncertainty_km"] >= 10:
            uncertainty.append({"code": "origin_approximate", "text": "The search location is approximate to within about "
                                f"{item['distance']['origin_uncertainty_km']:.0f} km."})
        uncertainty.append({"code": "source_links_cover_whole_record",
                            "text": "Source links belong to the whole record, not to a specific quote."})
        label = service_phrase(service)
        public = {k: v for k, v in item.items() if not k.startswith("_") and k != "reasons"}
        public.update({
            "rank": rank,
            "service": {"code": service, "evidence_status": "EXPLICIT_DOCUMENTED_CLAIM", "evidence_level": level,
                        "evidence_checks_passed": passed, "evidence_checks_total": len(item["_checks"]),
                        "current_availability_status": "UNKNOWN",
                        "independently_corroborated": any(e["independent"] for e in item["external_references"])},
            "identity_status": "SHARES_A_SOURCE_WITH_ANOTHER_RECORD"
            if item.get("identity_severity") == "shared_source_only" else "PLAUSIBLE_SINGLE_FACILITY",
            "eligibility": {"status": "ELIGIBLE_FOR_SHORTLIST", "policy_version": POLICY_VERSION,
                            "passed": ["explicit_service_claim", "no_identity_conflict", "usable_location", "has_source_link"],
                            "warnings": [u["code"] for u in uncertainty]},
            "ranking": {
                "score": round(item["_score"], 1), "score_type": "heuristic_utility_not_probability",
                "components": [{"key": k, "weight": weights[k], "value": round(item["_points"][k], 3),
                                "weighted_points": round(100 * weights[k] * item["_points"][k], 1)} for k in weights],
                "evidence_checks": item["_checks"],
                # What the record itself says. Since rank-v2 these only order claims with equal scores.
                "evidence_features": [{"key": k, "value": v, "text": t} for k, v, t in item["_parts"]],
                "distance_rule": f"Distance points halve every {half:.0f} km for {label}.",
                "why_selected": why,
            },
            "uncertainty": uncertainty,
            "evidence_preview": self._citations(row, {"direct", "equipment", "specialty"}),
            "affordability": {"cost_inr": None, "cost_status": "UNKNOWN", "coverage_status": "UNKNOWN"},
            "context": {"doctors_reported": item["_facility"].get("doctors_reported"),
                        "beds_reported": item["_facility"].get("capacity_reported"),
                        "operator": _value(item["_facility"].get("operatorTypeId")), "used_in_ranking": False},
            "next_steps": [f"Call the facility to confirm {label} is available now.",
                           "Ask about charges, scheme coverage and how to reach the facility."],
        })
        return public

    def _other(self, item):
        row = item["_row"]
        public = {k: v for k, v in item.items() if not k.startswith("_")}
        public.update({
            "tier": {"lead": "NEEDS_VERIFICATION", "model_found": "MODEL_FOUND_NEEDS_VERIFICATION"}.get(
                item["_tier"], "SET_ASIDE"),
            "evidence_status": "RELATED_SERVICE_ONLY" if row.get("_related") else row["evidence_status"].upper(),
            "flags": row.get("flags") or [],
            "manual_review_reason": row.get("manual_review_reason"),
            "evidence_preview": self._citations(row)[:4],
        })
        quotes = (row.get("verifier") or {}).get("lead_quotes") or []
        if item["_tier"] == "model_found" and quotes:
            public["evidence_preview"] = [{
                "evidence_id": f"{row['record_key'][:12]}:{row['service']}:m{n}", "record_id": row["record_key"],
                "source_field": q["field"] + (f"[{int(q['item_index'])}]" if q.get("item_index") is not None else ""),
                "exact_quote": q["quote"], "classification": "model_found",
                "model_explanation": q.get("model_explanation"), "source_attribution_status": "RECORD_LEVEL_ONLY",
            } for n, q in enumerate(quotes[:4])]
        return public

    @staticmethod
    def how_ranked(service, ranking_mode="evidence_first"):
        weights, half = WEIGHTS[ranking_mode], HALF_DISTANCE_KM[service]
        return {
            "policy_version": POLICY_VERSION,
            "gates": [
                "The record must state the service directly. A related specialty, equipment alone, a patient review, "
                "a plan or a referral elsewhere does not count.",
                "The record must not look like several facilities merged together.",
                "Its address, PIN and coordinates must agree. Without a PIN check, the mapped point must lie within "
                f"{MAX_CITY_OFFSET_KM:.0f} km of other records listed in the same city.",
                "It must carry at least one source link.",
            ],
            "score": [
                {"key": "service_evidence", "weight": weights["service_evidence"],
                 "text": "The share of five checks the claim passes: the record states the service; the statement "
                         "is specific; a model reading agrees; the facility's own website or published material "
                         "mentions it; an independent source lists it. A check that was not run counts as not passed "
                         "and is shown as not checked. Claims with equal scores are ordered by how much the record "
                         "itself says, then by distance."},
                {"key": "distance", "weight": weights["distance"],
                 "text": f"Straight-line distance. Points halve every {half:.0f} km for this service."},
                {"key": "location", "weight": weights["location"],
                 "text": "How well the mapped point agrees with the facility's PIN area and, where checked, with "
                         "OpenStreetMap."},
            ],
            "not_used": ["Bed and doctor counts (missing for most records)", "Social media activity",
                         "Cost, insurance and waiting time (not in the data)"],
            "limits": "Weights are engineering judgments, not clinical findings. The score orders options and is not "
                      "a probability that the service is available.",
        }


def _value(text):
    return text if isinstance(text, str) and text.strip() and text.strip().casefold() not in {"null", "none", "nan"} else None


def render_text(response):
    """Plain-language summary for a notebook cell or terminal."""
    if "query" not in response:
        lines = [response["message"]] + [
            f"  - {(o.get('label') or o.get('query') or o.get('display_name') or o) if isinstance(o, dict) else o}"
            for o in response.get("options", [])]
        return "\n".join(lines)
    q = response["query"]
    lines = [f"{q['service_display_name']} near {q['origin']['display_name']} (within {q['max_distance_km']:.0f} km)", ""]
    if response["message"]:
        lines += [response["message"], ""]
    for r in response["results"]:
        lines.append(f"{r['rank']}. {r['display_name']} · {r['distance']['value_km']} km · score {r['ranking']['score']} "
                     f"· evidence {r['service']['evidence_level'].lower()}")
        lines.append(f"   {r['location']['address_label']}  ·  phone {r['contact']['phone'] or 'not listed'}")
        for e in r["evidence_preview"][:3]:
            lines.append(f"   “{e['exact_quote']}”  [{e['source_field']}, {e['classification']}]")
        lines.append("   Points: " + ", ".join(f"{c['key']} {c['weighted_points']}" for c in r["ranking"]["components"]))
        lines.append("   Not confirmed: " + " ".join(u["text"] for u in r["uncertainty"][:3]))
        lines.append("")
    for title, key in (("Closer, but not recommended", "closer_but_not_recommended"),
                       ("Leads to verify by phone", "unverified_leads"),
                       ("Found by a model reading, not by the text rules", "model_found_leads"), ("Set aside", "set_aside"),
                       ("Listed in this city, location unverified", "location_unverified_leads")):
        listed = {o["facility_id"] for o in response["closer_but_not_recommended"]} if key in (
            "unverified_leads", "set_aside") else set()
        rows = [o for o in response[key] if o["facility_id"] not in listed]
        if rows:
            lines.append(title + ":")
            for o in rows:
                km = o["distance"]["value_km"]
                quote = next((e["exact_quote"] for e in o["evidence_preview"]), "")
                lines.append(f"   - {o['display_name']} ({'distance unknown' if km is None else f'{km} km'}): "
                             f"{' '.join(o['reasons'])}" + (f" “{quote[:140]}”" if quote else ""))
            lines.append("")
    lines += ["Note: " + w for w in response["global_warnings"]]
    return "\n".join(lines)


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Recommend facilities from a prepared snapshot.")
    parser.add_argument("--data", default="data/derived", help="Folder of v2 tables")
    parser.add_argument("--release", help="Release manifest, e.g. data/recovery/v3-manifest.json (overrides --data)")
    parser.add_argument("--service", required=True)
    parser.add_argument("--origin", required=True, help="City[, State], district, PIN or 'lat,lon'")
    parser.add_argument("--max-km", type=float)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--mode", default="evidence_first", choices=sorted(WEIGHTS))
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.release:
        index = CareIndex.from_release(args.release, as_of=args.as_of)
    else:
        index = CareIndex.from_parquet(args.data, as_of=args.as_of, index_version=args.data)
    try:
        response = index.recommend(args.service, args.origin, args.max_km, args.limit, args.mode)
    except RequestError as error:
        response = error.to_dict()
    print(json.dumps(response, indent=2, ensure_ascii=False) if args.json else render_text(response))


if __name__ == "__main__":
    main()
