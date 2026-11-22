"""Check whether OpenStreetMap maps a health facility where a record says one is.

This answers "what outside evidence says this facility exists here". It says
nothing about which services the facility offers: a `service_tag` is recorded
only when an OpenStreetMap tag on a name-matched object literally names one.

Three steps, each restartable:

    python -m backend.osm_check select --out data/enrichment/osm_inputs.jsonl
    python -m backend.osm_check fetch  --inputs data/enrichment/osm_inputs.jsonl --max-requests 2
    python -m backend.osm_check match  --inputs data/enrichment/osm_inputs.jsonl

`select` reads the release tables (needs pandas and PyArrow). `fetch` and
`match` use only the standard library. `fetch` asks the Overpass API about 25
facilities per request, one request at a time with a pause, and stores every
raw reply on disk, so a stopped run resumes and a finished one replays with no
network. `match` reads only that cache.

Map data © OpenStreetMap contributors, available under the Open Database
License (https://www.openstreetmap.org/copyright).
"""
import argparse
import difflib
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "facility-osm-check/1.0"
RADIUS_M = 1000
MATCH_RADIUS_M = 500
CONTAINED_RADIUS_M = 150
BATCH_SIZE = 25
PAUSE_SECONDS = 5.0
DEFAULT_CACHE = "data/enrichment/osm_cache"
DEFAULT_INPUTS = "data/enrichment/osm_inputs.jsonl"
DEFAULT_OUTPUT = "data/enrichment/osm_matches.jsonl"
STATUSES = ("name_match", "health_facility_nearby_different_name", "nothing_mapped_nearby", "not_checked")

# Words that say "this is a health facility" or describe a company, and so cannot tell two facilities apart.
GENERIC_WORDS = frozenset("""
hospital hospitals clinic clinics centre center centres centers dr doctor doctors pvt ltd private limited the and of
for a an at in nursing home medical medicare health healthcare care multi super speciality specialty multispeciality
multispecialty superspeciality superspecialty general unit llp inc trust
sub divisional district civil community primary government referral sadar area taluk taluka phc chc
""".split())
_VARIANTS = {"shri": "sri", "shree": "sri", "sree": "sri", "st": "saint", "govt": "government"}
NAME_TAGS = ("name", "name:en", "official_name", "alt_name", "old_name", "short_name", "operator")
KEPT_TAGS = ("amenity", "healthcare", "healthcare:speciality", "emergency", "beds", "operator", "operator:type")
# OpenStreetMap values that literally name one of our services. Related specialties (nephrology, radiology,
# neonatology, obstetrics) are deliberately absent: they do not state the service.
SERVICE_VALUES = {
    "dialysis": {"dialysis", "haemodialysis", "hemodialysis"},
    "mri": {"mri", "magnetic_resonance_imaging"},
    "nicu": {"nicu", "neonatal_intensive_care"},
    "c_section": {"caesarean_section", "cesarean_section", "c_section"},
}


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def _words(name):
    """Lower-cased words without accents or punctuation; initials such as "C.S.I." become one word."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    text = text.replace("&", " and ").replace("'", "").replace("\u2019", "")
    words, run = [], ""
    for word in re.split(r"[^a-z0-9]+", text) + [""]:
        if len(word) == 1 and word.isalpha():
            run += word
            continue
        if run:
            words.append(run)
            run = ""
        if word:
            words.append(_VARIANTS.get(word, word))
    return words


def name_tokens(name, drop=()):
    """Distinguishing words of a facility name: no generic words, and none of the place words in `drop`."""
    return frozenset(w for w in _words(name) if w not in GENERIC_WORDS and w not in drop)


def place_tokens(*places):
    """Words of a facility's own city and state, which a listing often appends to the name."""
    return frozenset(w for place in places for w in _words(place)) | {"india"}


def _same_word(a, b):
    return a == b or (min(len(a), len(b)) >= 5 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85)


def name_similarity(a, b):
    """Return (similarity, shared_words) for two token sets. Similarity is shared / all distinct words, 0 to 1."""
    if not a or not b:
        return 0.0, []
    unused, shared = set(b), []
    for word in sorted(a):
        hit = next((other for other in sorted(unused) if _same_word(word, other)), None)
        if hit is not None:
            unused.discard(hit)
            shared.append(word)
    return len(shared) / (len(a) + len(b) - len(shared)), shared


def compare_names(own, other, drop=()):
    """Compare a record's name with one name from the map."""
    a, b = name_tokens(own, drop), name_tokens(other, drop)
    similarity, shared = name_similarity(a, b)
    return {"similarity": similarity, "shared": shared,
            # every distinguishing word of the shorter name appears in the longer one
            "contained": bool(shared) and len(shared) == min(len(a), len(b)),
            "same_wording": [w for w in _words(own) if w not in drop] == [w for w in _words(other) if w not in drop]}


def match_basis(comparison, distance_m):
    """Why two names count as the same facility, or None. Stricter the further apart the points are."""
    similarity, shared = comparison["similarity"], comparison["shared"]
    if distance_m > MATCH_RADIUS_M:
        return None
    if not shared:
        # Names made only of generic words ("Civil Hospital"): identical wording, next door.
        return "same_name" if comparison["same_wording"] and distance_m <= CONTAINED_RADIUS_M else None
    distinctive = sum(len(w) for w in shared) >= 5
    if similarity == 1.0 and (distinctive or comparison["same_wording"]):
        return "same_name"
    if len(shared) >= 2 and similarity >= 0.6:
        return "most_words"
    # One name is the other plus extra words ("X Hospital" and "X Hospital, a unit of Y"): accept only next door.
    if comparison["contained"] and distinctive and distance_m <= CONTAINED_RADIUS_M:
        return "longer_form"
    return None


def is_name_match(comparison, distance_m):
    return match_basis(comparison, distance_m) is not None


def build_query(batch, radius_m=RADIUS_M):
    clauses = []
    for item in batch:
        around = "around:%d,%.6f,%.6f" % (radius_m, item["latitude"], item["longitude"])
        clauses.append('  nwr(%s)[amenity~"^(hospital|clinic|doctors)$"];' % around)
        clauses.append("  nwr(%s)[healthcare];" % around)
    return "[out:json][timeout:120];\n(\n%s\n);\nout center tags;\n" % "\n".join(clauses)


def batches(facilities, size=BATCH_SIZE):
    ordered = sorted(facilities, key=lambda f: f["record_key"])
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def overpass_post(query, url=OVERPASS_URL, timeout=180):
    """Send one query and return the reply bytes. The only function here that touches the network."""
    request = urllib.request.Request(url, data=query.encode("utf-8"), method="POST",
                                     headers={"User-Agent": USER_AGENT, "Content-Type": "text/plain; charset=utf-8"})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return reply.read()


class Cache:
    """Raw Overpass replies on disk, one file per query, named by the query's SHA-256."""

    def __init__(self, directory=DEFAULT_CACHE):
        self.directory = Path(directory)

    def _path(self, query):
        return self.directory / (hashlib.sha256(query.encode("utf-8")).hexdigest() + ".json")

    def get(self, query):
        path = self._path(query)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def put(self, query, response, fetched_at):
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {"fetched_at": fetched_at, "source": OVERPASS_URL, "query": query, "response": response}
        path = self._path(query)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return entry


def fetch(facilities, cache, post=overpass_post, max_requests=None, pause=PAUSE_SECONDS, sleep=time.sleep,
          now=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), log=None):
    """Fill the cache for every batch not already in it. Returns counts; never raises on a failed request."""
    counts = {"batches": 0, "cached": 0, "fetched": 0, "failed": 0, "skipped": 0}
    sent = 0
    for batch in batches(facilities):
        counts["batches"] += 1
        query = build_query(batch)
        if cache.get(query) is not None:
            counts["cached"] += 1
            continue
        if max_requests is not None and sent >= max_requests:
            counts["skipped"] += 1
            continue
        if sent:
            sleep(pause)
        sent += 1
        response = None
        for attempt in (1, 2):
            try:
                response = json.loads(post(query).decode("utf-8"))
                if not isinstance(response.get("elements"), list):
                    raise ValueError("reply has no element list")
                break
            except (urllib.error.URLError, OSError, ValueError) as error:
                response = None
                if log:
                    log("request %d attempt %d failed: %s" % (sent, attempt, error))
                if attempt == 1:
                    sleep(pause * 6)
        if response is None:
            counts["failed"] += 1
            continue
        cache.put(query, response, now())
        counts["fetched"] += 1
        if log:
            log("request %d: %d facilities, %d map objects" % (sent, len(batch), len(response["elements"])))
    return counts


def _point(element):
    centre = element.get("center") or element
    lat, lon = centre.get("lat"), centre.get("lon")
    return (lat, lon) if lat is not None and lon is not None else None


def _candidate_names(tags):
    names = []
    for key in NAME_TAGS:
        for part in (tags.get(key) or "").split(";"):
            if part.strip():
                names.append((key, part.strip()))
    return names


def service_tags(tags):
    """Services an object's tags literally name, as {service: 'tag=value'}."""
    found = {}
    for key in ("healthcare", "healthcare:speciality"):
        for value in re.split(r"[;,]", (tags.get(key) or "").casefold()):
            value = value.strip().replace(" ", "_").replace("-", "_")
            for service, values in SERVICE_VALUES.items():
                if value in values:
                    found.setdefault(service, "%s=%s" % (key, value))
    return found


def match_facility(facility, elements, fetched_at):
    """Compare one facility with the map objects of its batch and return its output row."""
    row = {"record_key": facility["record_key"], "name": facility.get("name"), "status": "nothing_mapped_nearby",
           "nearby_count": 0, "nearest_m": None, "osm_type": None, "osm_id": None, "osm_url": None, "osm_name": None,
           "matched_tag": None, "match_basis": None, "distance_m": None, "similarity": None, "shared_words": [], "tags": {},
           "service_tag": {}, "radius_m": RADIUS_M, "fetched_at": fetched_at}
    drop = place_tokens(facility.get("city"), facility.get("state"))
    best = None
    for element in elements:
        point, tags = _point(element), element.get("tags") or {}
        if point is None:
            continue
        distance = haversine_m(facility["latitude"], facility["longitude"], *point)
        if distance > RADIUS_M:
            continue
        row["nearby_count"] += 1
        if row["nearest_m"] is None or distance < row["nearest_m"]:
            row["nearest_m"] = round(distance)
        scored = [(compare_names(facility.get("name"), text, drop), key, text) for key, text in _candidate_names(tags)]
        comparison, key, text = max(scored, key=lambda s: (is_name_match(s[0], distance), s[0]["similarity"]),
                                    default=({"similarity": 0.0, "shared": [], "contained": False, "same_wording": False},
                                             None, tags.get("name")))
        rank = (is_name_match(comparison, distance), comparison["similarity"], -distance)
        if best is None or rank > best[0]:
            best = (rank, element, tags, distance, comparison, key, text)
    if best is None:
        return row
    rank, element, tags, distance, comparison, key, text = best
    similarity, shared = comparison["similarity"], comparison["shared"]
    row.update(status="name_match" if rank[0] else "health_facility_nearby_different_name",
               osm_type=element["type"], osm_id=element["id"],
               osm_url="https://www.openstreetmap.org/%s/%s" % (element["type"], element["id"]),
               osm_name=text, matched_tag=key, match_basis=match_basis(comparison, distance), distance_m=round(distance), similarity=round(similarity, 3),
               shared_words=shared, tags={k: tags[k] for k in KEPT_TAGS if k in tags})
    if rank[0]:
        row["service_tag"] = service_tags(tags)
    return row


def match(facilities, cache):
    """Build one row per facility from cached replies only. A batch with no cached reply is `not_checked`."""
    rows = []
    for batch in batches(facilities):
        entry = cache.get(build_query(batch))
        for facility in batch:
            if entry is None:
                rows.append({"record_key": facility["record_key"], "name": facility.get("name"), "status": "not_checked",
                             "radius_m": RADIUS_M, "fetched_at": None})
            else:
                rows.append(match_facility(facility, entry["response"]["elements"], entry["fetched_at"]))
    return rows


def summarize(rows):
    counts = {status: 0 for status in STATUSES}
    for row in rows:
        counts[row["status"]] += 1
    counts["total"] = len(rows)
    counts["with_service_tag"] = sum(1 for row in rows if row.get("service_tag"))
    return counts


def select_facilities(manifest="data/recovery/v3-manifest.json", root="."):
    """Facilities with a usable point and at least one documented or eligible claim. Needs pandas and PyArrow."""
    import pandas as pd
    root = Path(root)
    tables = json.loads((root / manifest).read_text())
    evidence = pd.read_parquet(root / tables["service_evidence"]["path"], columns=["record_key", "evidence_status", "eligible"])
    keys = set(evidence[(evidence["evidence_status"] == "documented") | evidence["eligible"]]["record_key"])
    columns = ["record_key", "name", "address_city", "address_stateOrRegion", "usable_latitude", "usable_longitude"]
    facilities = pd.read_parquet(root / tables["clean_facilities"]["path"], columns=columns)
    facilities = facilities[facilities["record_key"].isin(keys) & facilities["usable_latitude"].notna()
                            & facilities["usable_longitude"].notna()].sort_values("record_key")
    return [{"record_key": r.record_key, "name": r.name, "city": r.address_city, "state": r.address_stateOrRegion,
             "latitude": round(float(r.usable_latitude), 6), "longitude": round(float(r.usable_longitude), 6)}
            for r in facilities.itertuples(index=False)]


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as lines:
        return [json.loads(line) for line in lines if line.strip()]


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="list the facilities to check (needs pandas and PyArrow)")
    select.add_argument("--manifest", default="data/recovery/v3-manifest.json")
    select.add_argument("--out", default=DEFAULT_INPUTS)
    for name in ("fetch", "match"):
        command = commands.add_parser(name)
        command.add_argument("--inputs", default=DEFAULT_INPUTS)
        command.add_argument("--cache", default=DEFAULT_CACHE)
        command.add_argument("--limit", type=int, help="use only the first N facilities, for a pilot")
        if name == "fetch":
            command.add_argument("--max-requests", type=int, help="stop after this many new requests; rerun to resume")
            command.add_argument("--pause", type=float, default=PAUSE_SECONDS)
        else:
            command.add_argument("--out", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if args.command == "select":
        rows = select_facilities(args.manifest)
        write_jsonl(args.out, rows)
        print(json.dumps({"facilities": len(rows), "out": args.out}))
        return 0
    facilities = read_jsonl(args.inputs)[:args.limit]
    cache = Cache(args.cache)
    if args.command == "fetch":
        counts = fetch(facilities, cache, max_requests=args.max_requests, pause=args.pause,
                       log=lambda text: print(text, file=sys.stderr))
        print(json.dumps(counts))
        return 1 if counts["failed"] else 0
    rows = match(facilities, cache)
    write_jsonl(args.out, rows)
    print(json.dumps(summarize(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
