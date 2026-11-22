"""Reconcile each India Post office and NFHS indicator cell using Python and PyArrow."""

import csv
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median_low

import pyarrow.parquet as pq


# Canonical state identifiers have an independently transcribed 36-row government reference.
_STATE_REFERENCE = Path(__file__).resolve().parents[1] / "references" / "india_pmndp_state_benchmark.csv"
_STATE_ALIASES = {
    "a p": "andhra pradesh", "chattisgarh": "chhattisgarh", "j and k": "jammu and kashmir",
    "m p": "madhya pradesh", "maharastra": "maharashtra", "nct delhi": "delhi",
    "nct of delhi": "delhi", "new delhi": "delhi", "orissa": "odisha", "pondicherry": "puducherry",
    "tamilnadu": "tamil nadu", "the dadra and nagar haveli and daman and diu": "dadra and nagar haveli and daman and diu",
    "u p": "uttar pradesh", "up": "uttar pradesh",
}
_NULL_KEYS = {"", "na", "n a", "null", "none", "nan", "unknown"}
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def _place(raw):
    words = re.findall(r"[^\W_]+", str(raw or "").lower().replace("&", " and "))
    key = " ".join(words)
    return None if key in _NULL_KEYS else key


def _state(raw, states):
    key = _place(raw)
    canonical = _STATE_ALIASES.get(key, key)
    if canonical is not None and canonical not in states:
        raise ValueError(f"Unrecognized source state label {raw!r}. Review normalization before accepting this snapshot.")
    return canonical


def _check(key, field, expected, actual):
    if expected != actual:
        raise ValueError(f"{key} {field}: expected {expected!r}, found {actual!r}")


def _point(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    return (lat, lon) if 6 <= lat <= 38 and 67 <= lon <= 99 else None


def _postal(raw_file, clean_file, states, legacy):
    columns = ["pincode", "statename", "district", "latitude", "longitude"]
    source = pq.read_table(raw_file, columns=columns)
    offices = defaultdict(list)
    coordinate_states, coordinate_pins = defaultdict(set), defaultdict(set)
    invalid_pins = 0
    for office in source.to_pylist():
        pin = str(office["pincode"]).strip()
        if not re.fullmatch(r"[1-9][0-9]{5}", pin):
            invalid_pins += 1
            continue
        state = _state(office["statename"], states)
        district = _place(office["district"])
        point = _point(office["latitude"], office["longitude"]) if state and district else None
        if point is not None:
            coordinate_states[point].add(state)
            coordinate_pins[point].add(pin)
        offices[pin].append((state, district, point))

    suspect = {point for point, names in coordinate_states.items()
               if len(names) >= 3 and len(coordinate_pins[point]) >= 3}
    geography, coordinate_status = Counter(), Counter()
    seen = set()
    excluded_rows = 0
    clean = pq.read_table(clean_file)
    if not legacy and "excluded_shared_coordinate_rows" not in clean.column_names:
        raise ValueError("Postal output lacks the cross-state coordinate audit field. Use legacy_postal=True only for v1.")
    for row in clean.to_pylist():
        pin = row["pincode"]
        if pin not in offices or pin in seen:
            raise ValueError(f"Unexpected or repeated postal key {pin!r}")
        seen.add(pin)
        rows = offices[pin]
        candidates = sorted({s for s, _, _ in rows if s is not None})
        districts = sorted({(s, d) for s, d, _ in rows if s and d})
        located_count = sum(bool(s and d) for s, d, _ in rows)
        all_points = [pt for _, _, pt in rows if pt is not None]
        points = all_points if legacy else [pt for pt in all_points if pt not in suspect]
        removed = len(all_points) - len(points)
        excluded_rows += removed

        if len(candidates) > 1 or len(districts) > 1:
            geo = "ambiguous"
        elif not districts:
            geo = "unknown"
        elif located_count < len(rows):
            geo = "partial"
        else:
            geo = "unique"
        resolved = geo == "unique" or (legacy and geo == "partial")
        span = None
        if points:
            lats, lons = zip(*points)
            lat_mean = math.radians((max(lats) + min(lats)) / 2)
            span = 111.2 * math.hypot(max(lats) - min(lats), (max(lons) - min(lons)) * math.cos(lat_mean))

        if not resolved:
            status = "unresolved_geography"
        elif not points:
            status = "shared_across_states" if removed else "missing"
        elif span > 50:
            status = "dispersed"
        else:
            status = "approximate"

        expected = {
            "office_row_count": len(rows), "located_office_rows": located_count,
            "valid_coordinate_rows": len(points), "candidate_state_keys": candidates,
            "candidate_districts": [{"state_key": s, "district_key": d} for s, d in districts],
            "state_key": candidates[0] if len(candidates) == 1 else None,
            "district_key": districts[0][1] if resolved else None,
            "pin_geography_status": geo, "pin_coordinate_status": status,
            # With <10,000 offices per PIN the default Spark rank error is less than one observation.
            "pin_latitude": median_low(lats) if status == "approximate" else None,
            "pin_longitude": median_low(lons) if status == "approximate" else None,
        }
        if not legacy:
            expected["excluded_shared_coordinate_rows"] = removed
        for field, value in expected.items():
            _check(pin, field, value, row[field])
        actual_span = row["coordinate_span_km"]
        if (span is None or actual_span is None or not math.isclose(span, actual_span, rel_tol=1e-12, abs_tol=1e-9)):
            _check(pin, "coordinate_span_km", span, actual_span)
        geography[geo] += 1
        coordinate_status[status] += 1
    if len(seen) != len(offices):
        raise ValueError(f"Missing postal keys, including {min(offices.keys() - seen)}")
    return {
        "source_postal_rows": source.num_rows, "invalid_source_pin_rows": invalid_pins,
        "postal_office_rows_reconciled": sum(map(len, offices.values())), "verified_pin_rows": len(seen),
        "pin_geography_counts": dict(sorted(geography.items())),
        "pin_coordinate_counts": dict(sorted(coordinate_status.items())),
        "cross_state_coordinate_pairs_observed": len(suspect), "cross_state_coordinate_rows_excluded": excluded_rows,
    }


def _interpret_cell(raw, indicator):
    text = "" if raw is None else str(raw).strip()
    if text.lower() in {"", "na", "n/a", "null"}:
        return None, "missing"
    if text == "*":
        return None, "suppressed"
    bracketed = text.startswith("(") and text.endswith(")")
    number = text[1:-1].strip() if bracketed else text
    if not _NUMBER.fullmatch(number):
        return None, "invalid"
    value = float(number)
    if not math.isfinite(value) or (indicator.endswith("_pct") and not 0 <= value <= 100):
        return None, "invalid"
    return value, "small_sample" if bracketed else "reported"


def _source_type_value(value, numeric):
    # The source's string columns preserve whitespace exactly. Numeric fields had no
    # original spelling, so compare the stored numeric text by its round-trip value.
    if numeric and value is not None:
        return float(value)
    return value


def _survey(raw_file, clean_file, states):
    source = pq.read_table(raw_file)
    measures = set(source.column_names) - {"state_ut", "district_name"}
    if not measures:
        raise ValueError("NFHS source has no indicator columns")
    areas = defaultdict(list)
    for row in source.to_pylist():
        key = (_state(row["state_ut"], states), _place(row["district_name"]))
        if None in key:
            raise ValueError(f"Unresolved NFHS source district {row['district_name']!r}, {row['state_ut']!r}")
        areas[key].append(row)

    seen = set()
    status_counts = Counter()
    for row in pq.read_table(clean_file).to_pylist():
        area = row["state_key"], row["district_key"]
        indicator = row["indicator"]
        key = (*area, indicator)
        if area not in areas or indicator not in measures or key in seen:
            raise ValueError(f"Unexpected or repeated district-indicator key {key!r}")
        seen.add(key)
        sources = areas[area]
        for col in ("district_name", "state_ut"):
            _check(key, col, min(s[col].strip() for s in sources), row[col])
        _check(key, "source_row_count", len(sources), row["source_row_count"])
        values = {s[indicator] for s in sources}
        numeric = any(isinstance(v, (float, int)) for v in values)
        if len(values) > 1:
            actual = {_source_type_value(v, numeric) for v in (row["conflicting_raw_values"] or [])}
            _check(key, "conflicting_raw_values", values, actual)
            _check(key, "raw_value", None, row["raw_value"])
            value, status = None, "conflicting_values"
        else:
            raw = next(iter(values))
            _check(key, "raw_value", raw, _source_type_value(row["raw_value"], numeric))
            _check(key, "conflicting_raw_values", None, row["conflicting_raw_values"])
            value, status = _interpret_cell(raw, indicator)
        _check(key, "value", value, row["value"])
        _check(key, "value_status", status, row["value_status"])
        status_counts[status] += 1
    _check("NFHS", "district-indicator keys", len(areas) * len(measures), len(seen))
    return {
        "survey_source_rows": source.num_rows, "survey_indicator_columns": len(measures),
        "survey_source_cells_reconciled": source.num_rows * len(measures), "survey_output_rows_verified": len(seen),
        "survey_value_status_counts": dict(sorted(status_counts.items())),
    }


def audit_geography(raw_dir, derived_dir, *, legacy_postal=False):
    """Audit every office row and survey cell, using the original capture and built tables.

    Set legacy_postal=True only when inspecting the published v1 snapshot. It used
    all in-bounds office points and permitted partial districts. By default an
    outdated v1 postal table fails this audit.
    """
    raw_dir, derived_dir = Path(raw_dir), Path(derived_dir)
    with _STATE_REFERENCE.open(encoding="utf-8", newline="") as source:
        states = {r["state_key"] for r in csv.DictReader(source)}
    _check("reference", "canonical states", 36, len(states))
    postal = _postal(raw_dir / "india_post_pincode_directory.parquet", derived_dir / "postal_reference.parquet",
                     states, legacy_postal)
    survey = _survey(raw_dir / "nfhs_5_district_health_indicators.parquet",
                     derived_dir / "district_indicators.parquet", states)
    return {**postal, **survey}
