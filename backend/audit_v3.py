"""Audit the distributed v3 tables against the raw snapshots, v2 and the two companions."""

import argparse
from collections import Counter, defaultdict
import csv
from hashlib import sha256
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from statistics import median

import pyarrow.parquet as pq

from backend.audit_facilities import _check, _check_citations, _payload, _quarantine_reasons, _urls


def _index(rows, fields):
    indexed = {}
    for row in rows:
        key = tuple(row[f] for f in fields)
        key = key[0] if len(key) == 1 else key
        _check(key not in indexed, f"Duplicate {fields} key {key}")
        indexed[key] = row
    return indexed


def _rows(path, columns=None):
    for batch in pq.ParquetFile(path).iter_batches(columns=columns, batch_size=256):
        batch.validate(full=True)
        yield from batch.to_pylist()


def _groups(facilities):
    """Independently recover connected components by graph traversal."""
    ids, addresses = defaultdict(list), defaultdict(list)
    for key, row in facilities.items():
        ids[row["unique_id"].lower()].append(key)
        address = tuple(row[k] for k in ("name_key", "state_key", "city_key", "pincode"))
        if all(address):
            addresses[address].append(key)
    edges, reasons = defaultdict(set), defaultdict(set)
    for groups, reason in ((ids, "same_source_facility_id"), (addresses, "same_normalized_name_pin_city_state")):
        for keys in groups.values():
            if len(keys) > 1:
                for key in keys:
                    edges[key].add(keys[0])
                    edges[keys[0]].add(key)
                    reasons[key].add(reason)
    seen, groups_seen = set(), set()
    multi = 0
    for key in facilities:
        if key in seen:
            continue
        stack, members = [key], set()
        while stack:
            current = stack.pop()
            if current not in members:
                members.add(current)
                stack.extend(edges[current] - members)
        grouped_ids = {facilities[k]["duplicate_group_id"] for k in members}
        _check(len(grouped_ids) == 1 and grouped_ids.isdisjoint(groups_seen), f"Duplicate group disagreement {key}")
        groups_seen.update(grouped_ids)
        expected_basis = set.union(*(reasons[k] for k in members)) or {"single_record"}
        for k in members:
            _check(facilities[k]["duplicate_group_size"] == len(members)
                   and set(facilities[k]["duplicate_group_basis"]) == expected_basis, f"Duplicate basis/size {k}")
        seen.update(members)
        multi += len(members) > 1
    return multi


def _baseline_away(v2, original_candidates):
    groups = defaultdict(list)
    for key, r in v2.items():
        if r["usable_latitude"] is not None and r["usable_longitude"] is not None and r["state_key"] and r["city_key"]:
            groups[r["state_key"], r["city_key"]].append((key, r["usable_latitude"], r["usable_longitude"]))

    def over_threshold(row, points):
        lat, lon = map(radians, (row["usable_latitude"], row["usable_longitude"]))
        other = radians(median(point[1] for point in points))
        east = radians(median(point[2] for point in points))
        h = sin((lat - other) / 2) ** 2 + cos(lat) * cos(other) * sin((lon - east) / 2) ** 2
        return 12742 * asin(sqrt(min(1, h))) > 75

    inclusive, excluded = set(), set()
    for key in {pair[0] for pair in original_candidates}:
        r = v2[key]
        points = groups[r["state_key"], r["city_key"]]
        if len(points) < 3 or r["usable_latitude"] is None or r["usable_longitude"] is None:
            continue
        if over_threshold(r, points):
            inclusive.add(key)
        if over_threshold(r, [point for point in points if point[0] != key]):
            excluded.add(key)
    return ({pair for pair in original_candidates if pair[0] in inclusive},
            {pair for pair in original_candidates if pair[0] in excluded})


def audit(release, data):
    v2 = data / "derived"
    enrichment = data / "enrichment"
    manifest = (data / "recovery/v2.sha256").read_text().splitlines()
    for line in manifest:
        checksum, relpath = line.split()
        _check(sha256((data.parent / relpath).read_bytes()).hexdigest() == checksum, f"v2 checksum {relpath}")
    raw = data / "facilities.parquet"
    schema = pq.read_schema(raw)
    expected = Counter(_payload(row, schema) for row in _rows(raw))
    raw_count = expected.total()
    key_fields, pair_fields = ["record_key"], ["record_key", "service"]
    base = _index(_rows(v2 / "clean_facilities.parquet", key_fields + [
        "source_row_count", "identity_flags", "location_flags", "usable_latitude", "usable_longitude",
        "state_key", "city_key", "pin_distance_km"]), key_fields)
    facilities = _index(_rows(release / "clean_facilities.parquet"), key_fields)
    quarantine = _index(_rows(v2 / "facility_quarantine.parquet"), key_fields)
    _check(base.keys() == facilities.keys() and facilities.keys().isdisjoint(quarantine), "Facility keys changed")
    for k, r in _index(_rows(v2 / "clean_facilities.parquet", key_fields + schema.names), key_fields).items():
        _check(_payload(r, schema) == _payload(facilities[k], schema), f"Key points to another v2 payload {k}")
    for path in (release / "clean_facilities.parquet", v2 / "facility_quarantine.parquet",
                 enrichment / "address_contact_quality.parquet"):
        current = pq.read_schema(path)
        _check(all(field.type == current.field(field.name).type for field in schema), f"Source type changed in {path}")
    for k, row in [*facilities.items(), *quarantine.items()]:
        _check(row["source_row_count"] == expected.pop(_payload(row, schema), None), f"Original source mismatch {k}")
        reasons = _quarantine_reasons(row)
        _check(set(row.get("quarantine_reasons", [])) == reasons and bool(reasons) == (k in quarantine),
               f"Quarantine classification {k}")
    _check(not expected, "Omitted original payload")
    fields = ["address_display", "address_search_native", "address_search_romanized", "address_scripts",
              "unavailable_field_count", "majority_unavailable", "phone_status", "phone_flags", "website_status",
              "website_flags", "official_phone_e164", "official_web_host"]
    contact_keys = set()
    for r in _rows(enrichment / "address_contact_quality.parquet"):
        k = r["record_key"]
        f = facilities.get(k, quarantine.get(k))
        _check(k not in contact_keys and f is not None and r["source_row_count"] == f["source_row_count"]
               and _payload(r, schema) == _payload(f, schema), f"Companion source mismatch {k}")
        contact_keys.add(k)
        _check(r["v2_disposition"] == ("clean" if k in facilities else "quarantined"), f"Companion disposition {k}")
        _check(r["unavailable_field_count"] == len({g["field"] for g in r["unavailable_fields"]})
               and r["majority_unavailable"] == (r["unavailable_field_count"] * 2 > len(schema)),
               f"Missingness flag {k}")
        if k in facilities:
            _check(all(f[col] == r[col] for col in fields), f"Companion address/contact mismatch {k}")
    _check(contact_keys == facilities.keys() | quarantine.keys(), "Companion coverage mismatch")
    multi_groups = _groups(facilities)
    for k, f in facilities.items():
        for domain in ("identity", "location"):
            flags = domain + "_flags"
            _check(set(base[k][flags]).issubset(f[flags]) and bool(f[flags]) == f[domain + "_review"],
                   f"Missing or inconsistent {domain} flag {k}")
        site = (f["usable_latitude"], f["usable_longitude"])
        best = (f["best_estimate_latitude"], f["best_estimate_longitude"])
        precision = f["best_estimate_precision"]
        point_cols = {"reported_site": ("latitude", "longitude"), "pin_area": ("pin_latitude", "pin_longitude"),
                      "city_area": ("city_peer_latitude", "city_peer_longitude")}
        expected_best = tuple(f[c] for c in point_cols[precision]) if precision in point_cols else (None, None)
        _check(best == expected_best and ((None not in best) == (precision != "unknown")), f"Proxy basis {k}")
        _check((site == (None, None) or (site == best and precision == "reported_site" and not f["location_review"])),
               f"Proxy leaked to strict site coordinates {k}")

    prior_e = _index(_rows(v2 / "service_evidence.parquet"), pair_fields)
    evidence = _index(_rows(release / "service_evidence.parquet"), pair_fields)
    old_gold = _index(_rows(v2 / "recommendation_candidates.parquet", pair_fields), pair_fields)
    gold = _index(_rows(release / "recommendation_candidates.parquet"), pair_fields)
    queue = _index(_rows(release / "service_review_queue.parquet"), pair_fields)
    citations = _index(_rows(release / "citation_attribution.parquet"), [*pair_fields, "citation_ordinal"])
    _check(evidence.keys() == prior_e.keys(), "Evidence pairs changed")
    _check(gold.keys() == {p for p, e in evidence.items() if e["eligible"]} and gold.keys() <= old_gold.keys(),
           "Candidate pairs differ from eligible v3 evidence or were newly promoted")
    _check(queue.keys() == {p for p, e in evidence.items()
                           if p not in gold and any(c["kind"] == "direct" for c in e["citations"])},
           "Direct-evidence queue coverage")
    docs = []
    for pair, e in evidence.items():
        key, service = pair
        old, f = prior_e[pair], facilities[key]
        _check(all(e[c] == v for c, v in old.items() if c not in {"flags", "eligible"})
               and set(old["flags"]).issubset(e["flags"]), f"Changed v2 evidence or provenance {pair}")
        _check(e["record_source_urls"] == _urls(f["source_urls"]) and e["availability_status"] == "unverified",
               f"Evidence URL/status {pair}")
        _check_citations(e, f)
        for domain in ("identity", "location"):
            _check(not f[domain + "_review"] or domain + "_review" in e["flags"], f"Missing {domain} hold {pair}")
        own = False
        for ordinal, c in enumerate(e["citations"]):
            a = citations.pop((key, service, ordinal), None)
            _check(a is not None and all(a[field] == value for field, value in c.items())
                   and a["unique_id"] == f["unique_id"] and a["record_source_urls"] == e["record_source_urls"],
                   f"Citation attribution source pointer {pair} item {ordinal}")
            own |= a["site_attribution"] == "own_site_named_in_direct_statement"
            docs.append(c["kind"])
        _check(own == e["has_own_site_direct_quote"], f"Own citation indicator {pair}")
        if pair in queue:
            _check(queue[pair] == e, f"Queue payload {pair}")
        for field in f.keys() & e.keys():
            _check(f[field] == e[field], f"Evidence/facility {field} {pair}")
    _check(not citations, "Unmatched attribution rows")
    phone_shown = website_shown = 0
    for pair, c in gold.items():
        f, e = facilities[pair[0]], evidence[pair]
        _check(all(c[col] == val for col, val in e.items()), f"Candidate evidence {pair}")
        _check(all(c[col] == f[col] for col in c.keys() & f.keys()), f"Candidate site {pair}")
        _check(not f["identity_review"] and not f["location_review"] and c["best_estimate_precision"] == "reported_site"
               and c["usable_latitude"] is not None and c["usable_longitude"] is not None,
               f"Held or approximate candidate {pair}")
        _check(c["reported_phone"] == f["officialPhone"] and c["reported_website"] == f["officialWebsite"],
               f"Source contacts {pair}")
        for dest, domain, src in (("display_phone", "phone", "official_phone_e164"),
                                  ("display_website_host", "website", "official_web_host")):
            safe = f[domain + "_status"] == "listed_unconfirmed"
            _check(c[dest] == (f[src] if safe else None) and (not safe or not f[domain + "_flags"]),
                   f"Contact shown despite review status {pair} {domain}")
        phone_shown += bool(c["display_phone"])
        website_shown += bool(c["display_website_host"])
    away, peer_away = _baseline_away(base, old_gold)
    all_outliers = away | peer_away
    _check(all_outliers.isdisjoint(gold) and all(facilities[p[0]]["location_review"] for p in all_outliers),
           "Missed v2 city outlier in inclusive or leave-one-record-out comparison")
    # All office candidates, including the 715 with unknown administrative fields.
    from backend.audit_geography import _place, _state
    with (data.parent / "references/india_pmndp_state_benchmark.csv").open(newline="", encoding="utf-8") as stream:
        states = {r["state_key"] for r in csv.DictReader(stream)}
    office_keys = {(r["officename"].strip(" "), str(r["pincode"]), _state(r["statename"], states), _place(r["district"]))
                   for r in _rows(data / "india_post_pincode_directory.parquet", ["officename", "statename", "district", "pincode"])}
    old_missing = {r["pincode"] for r in _rows(v2 / "postal_reference.parquet", ["pincode", "pin_latitude"])
                   if r["pin_latitude"] is None}
    seen, seen_offices, covered = set(), {}, set()
    distinct_place_ids, office_bases = set(), Counter()
    for r in _rows(release / "place_lookup.parquet"):
        key = (r["place_id"], r["search_key"])
        _check(key not in seen, f"Repeated origin search key {key}")
        seen.add(key)
        mapped = r["point_basis"] != "unresolved"
        _check(mapped == (r["latitude"] is not None) == (r["longitude"] is not None)
               and (not mapped or (6 <= r["latitude"] <= 38 and 67 <= r["longitude"] <= 99)), f"Origin point {key}")
        if r["place_id"] in distinct_place_ids or r["place_type"] != "post_office":
            distinct_place_ids.add(r["place_id"])
            continue
        office_key = (r["place_name"], r["pincode"], r["state_key"], r["district_key"])
        _check(office_key not in seen_offices and office_key in office_keys, f"Unknown, duplicated office {office_key}")
        _check((r["state_key"] and r["district_key"]) or not mapped, f"Mapped unplaced office {office_key}")
        distinct_place_ids.add(r["place_id"])
        seen_offices[office_key] = r["place_id"]
        office_bases[r["point_basis"]] += 1
        if r["pincode"] in old_missing and r["point_basis"] == "reported_post_office_point":
            covered.add(r["pincode"])
    _check(seen_offices.keys() == office_keys, "Some source offices absent from origin search")

    with (data.parent / "references/verified_service_sources.csv").open(newline="", encoding="utf-8") as stream:
        references = _index(csv.DictReader(stream), ["reference_id"])
    ext_name = "external_service_matches.parquet"
    ext_path = release / ext_name if (release / ext_name).exists() else enrichment / ext_name
    external = _index(_rows(ext_path), ["reference_id"])
    _check(external.keys() == references.keys(), "External references not reconciled")
    disputed = []
    for k, r in external.items():
        e = evidence.get((r["record_key"], r["service"]))
        f, ref = facilities[r["record_key"]], references[k]
        _check(all(r[col] == ref[col] for col in ("unique_id", "name", "address_city", "address_stateOrRegion",
                                                   "pincode", "service", "record_key", "claim", "source_url",
                                                   "source_locator", "branch_source_url", "source_period"))
               and all(r[col] == f[col] for col in ("unique_id", "name", "address_city", "address_stateOrRegion", "pincode"))
               and r["availability_status"] == "unverified", f"External source link mismatch {k}")
        _check(r["identity_review"] == f["identity_review"] and r["location_review"] == f["location_review"]
               and r["local_eligible"] == (e["eligible"] if e else None) and
               r["facility_text_status"] == (e["evidence_status"] if e else None), f"External table uses stale release status {k}")
        _check(r["external_location_conflict"] == (ref["external_location_conflict"] == "true"), f"Branch conflict {k}")
        if r["external_location_conflict"]:
            _check("external_source_address_conflict" in f["location_flags"] and f["location_review"] and
                   f["best_estimate_precision"] == "unknown" and r["comparison_status"] == "record_requires_review"
                   and all(pair[0] != r["record_key"] for pair in gold), f"External campus conflict leaked {k}")
            disputed.append(r["name"])
    summary = {r["metric"]: r["value"] for r in _rows(release / "build_summary.parquet")}
    expected_counts = dict(candidate_facility_service_pairs=len(gold),
                           distinct_candidate_facilities=len({p[0] for p in gold}),
                           service_evidence_rows=len(evidence), clean_facility_records=len(facilities),
                           candidate_pairs_held_since_v2=len(old_gold) - len(gold),
                           location_review_records=sum(r["location_review"] for r in facilities.values()),
                           identity_review_records=sum(r["identity_review"] for r in facilities.values()),
                           direct_pairs_held_for_review=len(queue), cited_items=len(docs),
                           place_lookup_rows=len(seen), multi_record_duplicate_groups=multi_groups)
    expected_counts["eligible_pairs_held_since_v2"] = expected_counts.pop("candidate_pairs_held_since_v2")
    expected_counts.update({"candidate_pairs_" + s: n for s, n in Counter(p[1] for p in gold).items()})
    _check(all(summary.get(col) == n for col, n in expected_counts.items()), "Summary differs from independent counts")

    files = sorted(release.glob("*.parquet")) + [enrichment / "address_contact_quality.parquet"]
    if ext_path.parent != release:
        files.append(ext_path)
    return {
        "scope": "Per-row comparison of distributed tables. Coordinates, contact ownership and service availability are not field-verified.",
        "v2_checksum_files": len(manifest), "raw_rows": raw_count, "raw_fields": len(schema),
        "raw_cells_reconciled_counting_repeats": raw_count * len(schema), "distinct_clean": len(facilities),
        "clean_including_repeats": sum(r["source_row_count"] for r in facilities.values()),
        "quarantined_rows": sum(r["source_row_count"] for r in quarantine.values()),
        "source_rows_missing_or_changed": 0, "address_contact_distinct_rows": len(contact_keys),
        "facility_service_pairs": len(evidence), "attributed_citations_traced_to_raw_field_and_index": len(docs),
        "v2_candidate_pairs": len(old_gold), "v3_candidate_pairs": len(gold), "direct_pairs_in_review_queue": len(queue),
        "v2_city_outlier_pairs_now_held": len(away),
        "v2_leave_one_record_out_pairs_now_held_min_two_peers": len(peer_away),
        "v2_city_outlier_pairs_without_pin_distance": sum(base[p[0]]["pin_distance_km"] is None for p in away),
        "held_location_precision": dict(sorted(Counter(r["best_estimate_precision"] for r in facilities.values()
                                                      if r["location_review"]).items())),
        "candidate_approximate_or_held_distance_points": 0, "multi_record_duplicate_groups": multi_groups,
        "candidate_pairs_with_display_phone": phone_shown, "candidate_pairs_with_display_website": website_shown,
        "external_matches": len(external), "external_location_conflicts_held": sorted(disputed),
        "search_rows": len(seen), "distinct_places": len(distinct_place_ids), "distinct_raw_offices_reconciled": len(office_keys),
        "distinct_offices_by_point_basis": dict(sorted(office_bases.items())), "v2_unmapped_pins": len(old_missing),
        "v2_unmapped_pins_with_at_least_one_mapped_named_office": len(covered),
        "audited_parquet_sha256": {p.name: sha256(p.read_bytes()).hexdigest() for p in files},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=Path("data/revisions/v3"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--json", type=Path, help="Write the report after all checks pass")
    args = parser.parse_args()
    report = json.dumps(audit(args.release, args.data), indent=2, sort_keys=True) + "\n"
    if args.json:
        args.json.write_text(report)
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
