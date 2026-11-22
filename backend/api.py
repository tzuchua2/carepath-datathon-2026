"""CareBricks HTTP API: the design document's section 7 contract over the recommendation engine.

`handle` is the whole API as one pure function from a request to
`(http_status, body_dict)`. It needs only the standard library and an
already-loaded `CareIndex`, so a test, a notebook or another web framework can
call it directly. `serve` wraps it in a small local server for demos:

    python -m backend.api --port 8000          # v3 release, web page on /
    python -m backend.api --data data/derived  # the older v2 tables

This layer adds no facts. Every facility, quote, score and warning comes from
`backend.recommend`; inputs the engine cannot use yet are reported back under
`inputs_not_applied` instead of being silently dropped.
"""
import argparse
import hashlib
import json
import os
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from backend.recommend import (CONTRACT_VERSION, HALF_DISTANCE_KM, POLICY_VERSION, SERVICE_ALIASES, WEIGHTS,
                               CareIndex, RequestError, place_key, resolve_service)
from backend.service_evidence import SERVICES
from backend.agent import handle_agent, load_private_env
from backend.evidence_summary import handle_summary
from backend.referral_note import NoteError, referral_note

API_NAME = "CareBricks"
ROOT = "/api/v1"
MAX_BODY_BYTES = 64 * 1024
EMERGENCY_WARNING = ("This request was marked as an emergency. This list is built from published records and "
                     "cannot confirm that a facility can take the patient now. Contact local emergency services.")
_NOT_APPLIED = {
    "transport_mode": "Travel mode is not used. Distances are straight lines; no route or travel time is computed.",
    "budget": "Budget is not used. The data has no prices, so cost stays unknown.",
}
_ORIGIN_FIELDS = ("kind", "display_name", "latitude", "longitude", "precision", "uncertainty_km", "warning")


class _Reject(Exception):
    """A request this layer turns away before the engine sees it."""

    def __init__(self, http_status, code, message, options=()):
        super().__init__(message)
        self.http_status, self.body = http_status, _error(code, message, options)


def _error(code, message, options=()):
    return {"contract_version": CONTRACT_VERSION, "status": code, "message": message, "options": list(options)}


def _number(value, name):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Reject(422, "INVALID_REQUEST", "{} must be a number.".format(name))
    return value


def _object(value, name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _Reject(422, "INVALID_REQUEST", "{} must be an object.".format(name))
    return value


MAX_PLACE_CHARS = 200
# Former official names people still type. Tried only after the name as typed is not found.
_OLD_PLACE_NAMES = {"bombay": "Mumbai", "calcutta": "Kolkata", "madras": "Chennai", "poona": "Pune", "baroda": "Vadodara",
                    "banaras": "Varanasi", "benares": "Varanasi", "bangalore": "Bengaluru", "cochin": "Kochi",
                    "trivandrum": "Thiruvananthapuram", "pondicherry": "Puducherry", "gurgaon": "Gurugram"}


def _resolve_place(index, text):
    """Resolve typed place text, forgiving common ways of typing it. The engine still decides what exists."""
    if not isinstance(text, str):
        return index.resolve_origin(text)
    text = " ".join(text.split())
    if len(text) > MAX_PLACE_CHARS:
        raise RequestError("ORIGIN_NOT_FOUND", "The place name is too long. Enter a city, district or six-digit PIN.")
    if re.fullmatch(r"\d{3} \d{3}", text):  # a PIN typed with a space, not a pair of coordinates
        text = text.replace(" ", "")
    try:
        return index.resolve_origin(text)
    except RequestError as error:
        if error.code != "ORIGIN_NOT_FOUND":
            raise
        if re.fullmatch(r"\d{6}", text):
            raise RequestError("ORIGIN_NOT_FOUND", "PIN {} is not in the postal reference. Check the six digits, or enter "
                                                   "a city or district.".format(text)) from None
        first, _, rest = text.partition(",")
        candidates = []
        if first.strip().casefold() in _OLD_PLACE_NAMES:
            candidates.append(_OLD_PLACE_NAMES[first.strip().casefold()] + ("," + rest if rest else ""))
        words = text.split()
        if "," not in text:  # "Patna Bihar": try the last one to three words as the state
            candidates += ["{}, {}".format(" ".join(words[:-n]), " ".join(words[-n:])) for n in (1, 2, 3) if len(words) > n]
        for candidate in candidates:
            try:
                return index.resolve_origin(candidate)
            except RequestError as retry:
                if retry.code == "ORIGIN_AMBIGUOUS":
                    raise retry from None
        raise error


def _origin(index, origin):
    """Turn the request's origin (a string, or the section 7.2 object) into a resolved origin."""
    if isinstance(origin, str):
        return _resolve_place(index, origin)
    origin = _object(origin, "origin")
    latitude, longitude = _number(origin.get("latitude"), "origin.latitude"), _number(origin.get("longitude"),
                                                                                      "origin.longitude")
    if origin.get("kind") == "coordinates" or (latitude is not None and not origin.get("query")):
        if latitude is None or longitude is None:
            raise RequestError("ORIGIN_REQUIRED", "Coordinates need both a latitude and a longitude.")
        return index.resolve_origin(None, latitude, longitude)
    query = origin.get("query")
    if query is not None and not isinstance(query, str):
        raise _Reject(422, "INVALID_REQUEST", "origin.query must be text.")
    return _resolve_place(index, query)


def _public_origin(origin):
    return {key: origin[key] for key in _ORIGIN_FIELDS}


def health(index):
    return 200, {"status": "OK", "api": API_NAME, "contract_version": CONTRACT_VERSION,
                 "ranking_policy_version": POLICY_VERSION, "index_version": index.index_version,
                 "as_of": index.as_of.isoformat(), "facility_records": len(index.facilities)}


def services(index, query):
    """List supported services; `q` keeps those whose code, name or a synonym contains the text."""
    wanted = place_key((query.get("q") or [""])[0])
    listed = []
    for code, name in SERVICES.items():
        names = [code, name, *SERVICE_ALIASES[code]]
        if wanted and not any(wanted in (place_key(n) or "") for n in names):
            continue
        listed.append({"code": code, "display_name": name, "synonyms": list(SERVICE_ALIASES[code]),
                       "distance_half_km": HALF_DISTANCE_KM[code],
                       "records_with_any_mention": len(index.evidence.get(code, ())),
                       "records_recommendable": sum(bool(r["eligible"]) for r in index.evidence.get(code, ()))})
    return 200, {"contract_version": CONTRACT_VERSION, "services": listed,
                 "ranking_modes": [{"code": mode, "weights": weights} for mode, weights in WEIGHTS.items()]}


def methodology(index, query):
    service = resolve_service((query.get("service_code") or query.get("service") or [""])[0])
    mode = (query.get("ranking_mode") or ["evidence_first"])[0]
    if mode not in WEIGHTS:
        raise RequestError("INVALID_RANKING_MODE", "Unknown ranking mode '{}'.".format(mode), sorted(WEIGHTS))
    return 200, {"contract_version": CONTRACT_VERSION, "service_code": service, "ranking_mode": mode,
                 "how_ranked": index.how_ranked(service, mode)}


def resolve_location(index, body):
    """Resolve an origin. An ambiguous name comes back as 422 with the choices; nothing is picked silently."""
    origin = _origin(index, body.get("origin", body))  # accepts {kind, query, ...} or {"origin": ...}
    return 200, {"contract_version": CONTRACT_VERSION, "status": "RESOLVED", "origin": _public_origin(origin)}


def search(index, body):
    """Answer a section 7.2 search request with the engine's section 7.3 response."""
    service = body.get("service")
    if isinstance(service, dict):  # an unrecognised code falls back to the text the user typed
        code, text = service.get("code"), service.get("free_text")
        try:
            service = resolve_service(code) if code else text
        except RequestError:
            service = text or code
    if not isinstance(service, str) or not service.strip():
        raise RequestError("INVALID_SERVICE", "Choose a service.",
                           [{"code": k, "display_name": v} for k, v in SERVICES.items()])
    constraints, preferences = _object(body.get("constraints"), "constraints"), _object(body.get("preferences"),
                                                                                         "preferences")
    limit = constraints.get("result_limit", 5)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise RequestError("INVALID_LIMIT", "The number of results must be between 1 and 50.")
    pool = constraints.get("result_pool", limit)
    if isinstance(pool, bool) or not isinstance(pool, int) or isinstance(limit, int) and pool < limit:
        raise RequestError("INVALID_LIMIT", "result_pool must be a whole number from result_limit to 50.")
    include_leads = body.get("include_unverified_leads", True)
    if not isinstance(include_leads, bool):
        raise _Reject(422, "INVALID_REQUEST", "include_unverified_leads must be true or false.")
    mode = preferences.get("ranking_mode", "evidence_first")
    if not isinstance(mode, str):
        raise RequestError("INVALID_RANKING_MODE", "Unknown ranking mode.", sorted(WEIGHTS))
    response = index.recommend(service, _origin(index, body.get("origin")),
                               max_distance_km=_number(constraints.get("max_distance_km"), "constraints.max_distance_km"),
                               limit=pool, ranking_mode=mode, include_unverified_leads=include_leads)
    response["views"] = _views(response["results"], limit)
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    request_id = body.get("request_id")
    response["request_id"] = request_id if isinstance(request_id, str) and request_id else \
        "req_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    response["inputs_not_applied"] = [{"field": "constraints." + key, "value": constraints[key], "reason": reason}
                                      for key, reason in _NOT_APPLIED.items() if constraints.get(key) is not None]
    if constraints.get("urgency") == "emergency":
        response["global_warnings"] = [EMERGENCY_WARNING] + response["global_warnings"]
    reviews = getattr(index, "public_source_reviews", None)
    for group in _RESULT_LISTS if reviews else ():  # the engine response is untouched when no review file is loaded
        for item in response.get(group) or []:
            item["public_source_review"] = reviews.get((item["facility_id"], response["query"]["service_code"]))
    for item in response["results"]:
        if item.get("model_check"):
            row = next(r for r in index.by_record[item["facility_id"]] if r["service"] == response["query"]["service_code"])
            item["model_findings"] = _model_findings(row)
    return 200, response


def _views(results, show):
    """Three orderings of the same results, so a page can switch view without asking again.

    Each `order` lists facility ids. Nothing is rescored: the keys are numbers each result already carries.
    Ask for a larger `constraints.result_pool` than `result_limit` so the first `show` of every view are complete;
    the best few by distance are not always among the best few by score.
    """
    def key(item, name):
        return next(c["weighted_points"] for c in item["ranking"]["components"] if c["key"] == name)

    def km(item):
        value = item["distance"]["value_km"]
        return float("inf") if value is None else value

    ids = lambda rows: [item["facility_id"] for item in rows]
    by_score = sorted(results, key=lambda item: -item["ranking"]["score"])
    return {
        "default": "recommended", "show": show, "pool": len(results),
        "options": [
            {"code": "recommended", "label": "Recommended", "sort_key": "ranking.score",
             "description": "Highest overall score first: what the record says, distance and location confidence.",
             "order": ids(results)},
            {"code": "distance_first", "label": "Nearest first", "sort_key": "distance.value_km",
             "description": "Shortest straight-line distance first. Every option still passed the same checks.",
             "order": ids(sorted(by_score, key=km))},
            {"code": "service_first", "label": "Service evidence first", "sort_key": "ranking.components.service_evidence",
             "description": "Records that say the most about the service first, whatever the distance. "
                            "Ties go to the higher overall score.",
             "order": ids(sorted(by_score, key=lambda item: -key(item, "service_evidence")))},
        ],
    }


_RESULT_LISTS = ("results", "closer_but_not_recommended", "unverified_leads", "set_aside", "model_found_leads",
                 "location_unverified_leads")


def compare(index, body):
    """Explain the difference between two recommended facilities for one search, part by part."""
    ids = body.get("facility_ids")
    if not isinstance(ids, list) or len(ids) != 2 or not all(isinstance(i, str) and i for i in ids) or ids[0] == ids[1]:
        raise _Reject(422, "INVALID_REQUEST", "facility_ids must name two different facilities.")
    service = body.get("service")
    if isinstance(service, dict):
        service = service.get("code") or service.get("free_text")
    if not isinstance(service, str) or not service.strip():
        raise RequestError("INVALID_SERVICE", "Choose a service.", [{"code": k, "display_name": v} for k, v in SERVICES.items()])
    return 200, index.compare(service, _origin(index, body.get("origin")), ids[0], ids[1])


def note(index, body):
    """A copy-ready referral note for one facility in a search. Built from the search response; no model call."""
    facility_id = body.get("facility_id")
    if not isinstance(facility_id, str) or not facility_id:
        raise _Reject(422, "INVALID_REQUEST", "facility_id is required.")
    service = body.get("service")
    if isinstance(service, dict):
        service = service.get("code") or service.get("free_text")
    if not isinstance(service, str) or not service.strip():
        raise RequestError("INVALID_SERVICE", "Choose a service.", [{"code": k, "display_name": v} for k, v in SERVICES.items()])
    response = index.recommend(service, _origin(index, body.get("origin")), limit=50)
    try:
        return 200, dict({"contract_version": CONTRACT_VERSION}, **referral_note(response, facility_id))
    except NoteError:
        raise _Reject(404, "FACILITY_NOT_FOUND", "This facility is not among the results or leads of that search.")


def access_gaps(index, query):
    """District summary of distance to the nearest documented provider, for planning. Describes the dataset only."""
    first = lambda name, default=None: (query.get(name) or [default])[0]
    try:
        limit = int(first("limit", 25))
    except ValueError:
        raise RequestError("INVALID_LIMIT", "limit must be a whole number.")
    if not 1 <= limit <= 800:
        raise RequestError("INVALID_LIMIT", "limit must be between 1 and 800.")
    order = first("order", "farthest")
    if order not in ("farthest", "nearest", "blind_spots"):
        raise _Reject(422, "INVALID_REQUEST", "order must be farthest, nearest or blind_spots.")
    service = first("service") or first("service_code")
    if not service:
        raise RequestError("INVALID_SERVICE", "Choose a service.", [{"code": k, "display_name": v} for k, v in SERVICES.items()])
    body = index.access_gaps(service, state=first("state") or None, limit=limit, order=order)
    return 200, dict({"contract_version": CONTRACT_VERSION, "index_version": index.index_version}, **body)
REVIEWS_PATH = "references/ranked-service-audit/evidence.json"
EXPLAINER_PATH = "data/verifier_runs/how_we_verify.json"
_FINDING_FIELDS = ("field", "item_index", "effective_classification", "exact_quote", "branch_scope", "temporal_scope",
                   "model_explanation", "classification", "code_override")


def load_json(path):
    """A JSON file's content, or None when the file is missing."""
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _model_findings(row):
    """The verifier's reading of each passage it relied on, when the evidence row came from the model run."""
    verifier = row.get("verifier") or {}
    if verifier.get("status") != "classified":
        return []
    return [{key: finding.get(key) for key in _FINDING_FIELDS} for finding in verifier.get("findings") or []
            if finding.get("effective_classification") not in (None, "NO_EVIDENCE")]


def verification(index):
    """How claims are verified, with worked cases and scores, as written by the verifier's own run."""
    content = getattr(index, "verification_explainer", None)
    if not content:
        raise _Reject(404, "NOT_FOUND", "No verification summary is loaded.")
    return 200, dict({"contract_version": CONTRACT_VERSION}, **content)
VERIFIED_PATH = "data/verifier_runs/v3_verified_evidence.jsonl"
_REVIEW_FIELDS = ("finding", "blue_check", "current_availability", "reviewed_on", "finding_detail", "sources")


def load_public_source_reviews(path=REVIEWS_PATH):
    """Read the dated public-source review of ranked results, keyed by (record_key, service).

    A reviewer compared each listed service with public pages and recorded a finding: `recent_independent`
    (the only one that earns `blue_check`), `historical_independent`, `provider` or `unresolved`. A pair with
    no entry has not been reviewed. The review never changes the ranking.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    reviews = {}
    for row in rows:
        pair = (row["record_key"], row["service"])
        finding = row["finding"]
        if (pair in reviews or pair[1] not in SERVICES
                or finding not in {"recent_independent", "historical_independent", "provider", "unresolved"}
                or row["blue_check"] is not (finding == "recent_independent")
                or row["current_availability"] != "unknown" or not row["sources"]):
            raise ValueError(f"Invalid public-source review for {pair}")
        reviews[pair] = {key: row[key] for key in _REVIEW_FIELDS}
    return reviews


def _service_rows(index, key, service=None):
    codes = [resolve_service(service)] if service else list(SERVICES)
    return [(code, row) for code in codes for row in index.evidence.get(code, ()) if row["record_key"] == key]


def _text(value):
    return value if isinstance(value, str) and value.strip() and value.strip().casefold() != "null" else None


def facility(index, key, query):
    """One record's profile from the engine. `facility_id` is the release's `record_key`, as in search results."""
    try:
        profile = index.facility_profile(key)
    except RequestError:
        raise _Reject(404, "FACILITY_NOT_FOUND", "No facility record has this id in the loaded snapshot.")
    code = (query.get("service_code") or [None])[0]
    if code:
        _service_rows(index, key, code)  # rejects an unknown service code
        profile["services"] = [s for s in profile["services"] if s["code"] == resolve_service(code)]
    for service in profile["services"]:  # names kept from contract 1.0
        service["public_source_review"] = getattr(index, "public_source_reviews", {}).get((key, service["code"]))
        service["recommendable"] = service["eligible_for_shortlist"]
        service["current_availability_status"] = "UNKNOWN"
        service["evidence_count"] = len(service.get("evidence_preview") or [])
    profile["context"]["used_in_ranking"] = False
    return 200, dict({"contract_version": CONTRACT_VERSION, "index_version": index.index_version}, **profile,
                     quality_flags=["NO_LIVE_AVAILABILITY_DATA", "COST_NOT_IN_DATASET"])


def facility_evidence(index, key, query):
    """Every excerpt the build kept for this record, with the field and item it was copied from."""
    if key not in index.facilities:
        raise _Reject(404, "FACILITY_NOT_FOUND", "No facility record has this id in the loaded snapshot.")
    rows = _service_rows(index, key, (query.get("service_code") or [None])[0])
    return 200, {
        "contract_version": CONTRACT_VERSION, "index_version": index.index_version, "facility_id": key,
        "services": [{"code": code, "evidence_status": row["evidence_status"].upper(),
                      "recommendable": bool(row["eligible"]), "flags": row.get("flags") or [],
                      "source_urls": row.get("record_source_urls") or [],
                      "source_published_at": row.get("page_update_date"),
                      "public_source_review": getattr(index, "public_source_reviews", {}).get((key, code)),
                      "source_attribution_status": "RECORD_LEVEL_ONLY",
                      "evidence": index.citations(row), "model_findings": _model_findings(row)}
                     for code, row in rows],
        "note": "Source links belong to the whole record. No link is tied to an individual quote.",
    }


def handle(index, method, path, query=None, body=None):
    """Route one request. `query` is a parse_qs-style dict; `body` is the decoded JSON value or None."""
    try:
        if not path.startswith(ROOT + "/"):
            raise _Reject(404, "NOT_FOUND", "Unknown path. The API lives under {}.".format(ROOT))
        parts = [unquote(p) for p in path[len(ROOT) + 1:].strip("/").split("/")]
        query = query or {}
        posts = {("locations", "resolve"): resolve_location, ("referrals", "search"): search, ("search",): search, ("compare",): compare, ("referral-note",): note,
                 ("evidence", "summary"): handle_summary}
        gets = {("health",): lambda i, q: health(i), ("services",): services, ("methodology",): methodology,
                ("verification",): lambda i, q: verification(i), ("access-gaps",): access_gaps}
        route = tuple(parts)
        if route in posts or route in gets or (route[0] == "facilities" and len(route) in (2, 3)):
            wanted = "POST" if route in posts else "GET"
            if method != wanted:
                raise _Reject(405, "METHOD_NOT_ALLOWED", "Use {} for this path.".format(wanted))
        else:
            raise _Reject(404, "NOT_FOUND", "Unknown path.")
        if index is None:
            raise _Reject(503, "DATA_INDEX_UNAVAILABLE", "The facility index is not loaded. Try again shortly.")
        if route in posts:
            if not isinstance(body, dict):
                raise _Reject(400, "INVALID_REQUEST", "Send a JSON object as the request body.")
            return posts[route](index, body)
        if route in gets:
            return gets[route](index, query)
        if len(route) == 2:
            return facility(index, route[1], query)
        if route[2] != "evidence":
            raise _Reject(404, "NOT_FOUND", "Unknown path.")
        return facility_evidence(index, route[1], query)
    except _Reject as reject:
        return reject.http_status, reject.body
    except RequestError as error:
        return 422, error.to_dict()
    except Exception:  # never leak a trace or invent a fallback answer
        return 500, _error("INTERNAL_ERROR", "The request could not be completed.")


STATIC_DIR = Path(__file__).resolve().parent.parent / "app"


def public_map_config(env=None):
    """Expose only the browser Maps key and map ID, never server credentials."""
    env = os.environ if env is None else env
    key = str(env.get("GOOGLE_MAPS_API_KEY", "")).strip()
    google = env.get("MAP_PROVIDER") == "google" or (env.get("MAP_PROVIDER") != "osm" and bool(key))
    return {"provider": "google" if google else "osm", "googleMapsApiKey": key if google else "",
            "googleMapsMapId": str(env.get("GOOGLE_MAPS_MAP_ID") or "DEMO_MAP_ID")}


_STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
                 ".txt": "text/plain; charset=utf-8"}


def static_file(path, root=None):
    """Return (http_status, content_type, bytes) for a file of the web app. "/" is index.html.

    Only files with a known extension inside the app folder are served. Any web framework can call this next to
    handle(), so the page and the API come from one process.
    """
    root = Path(root or STATIC_DIR).resolve()
    target = (root / (unquote(path).lstrip("/") or "index.html")).resolve()
    kind = _STATIC_TYPES.get(target.suffix.lower())
    if kind is None or root not in target.parents or not target.is_file():
        return 404, "text/plain; charset=utf-8", b"Not found"
    return 200, kind, target.read_bytes()


def request_origin_allowed(origin, host):
    """Use an explicit public origin behind a proxy, never client-supplied forwarding headers."""
    if not origin:
        return True  # Non-browser API clients do not send Origin.
    try:
        received = urlsplit(origin)
        if (received.scheme not in {"http", "https"} or not received.netloc or received.username
                or received.password or received.path not in {"", "/"} or received.query or received.fragment):
            return False
        public_origin = os.environ.get("CAREPATH_PUBLIC_ORIGIN", "").strip()
        if public_origin:
            expected = urlsplit(public_origin)
            return (received.scheme, received.netloc.lower()) == (expected.scheme, expected.netloc.lower())
        return received.netloc.lower() == (host or "").lower()
    except ValueError:
        return False


def make_handler(index, allow_origin="*", static_root=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = API_NAME

        def _send(self, status, body, content_type="application/json; charset=utf-8", *, same_origin=False):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, no-store" if same_origin else "no-cache")
            self.send_header("Content-Length", str(len(data)))
            if not same_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # New case / mode changes can cancel an in-flight agent request.

        def _run(self, body=None):
            url = urlsplit(self.path)
            if url.path == "/api/agent" and self.command == "POST":
                return self._send(*handle_agent(index, body))
            self._send(*handle(index, self.command, url.path, parse_qs(url.query), body))

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/map-config":
                return self._send(200, public_map_config(), same_origin=True)
            if path == ROOT or path.startswith(ROOT + "/"):
                return self._run()
            status, content_type, data = static_file(path, static_root)
            self._send(status, data, content_type)

        do_HEAD = do_GET

        def do_OPTIONS(self):
            self._send(204, {})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if urlsplit(self.path).path in ("/api/agent", ROOT + "/evidence/summary") and not request_origin_allowed(origin, self.headers.get("Host")):
                return self._send(403, _error("ORIGIN_NOT_ALLOWED", "Use the assistant from this Carepath app."))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY_BYTES:
                return self._send(413, _error("REQUEST_TOO_LARGE", "The request body is too large."))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else None
            except ValueError:
                return self._send(400, _error("INVALID_REQUEST", "The request body is not valid JSON."))
            self._run(body)

        def log_message(self, *args):  # request bodies can hold a patient's location; keep them out of logs
            pass

    return Handler


def serve(index, host="127.0.0.1", port=8000, static_root=None):
    server = ThreadingHTTPServer((host, port), make_handler(index, static_root=static_root))
    print("{} on http://{}:{}/ (API under {})".format(API_NAME, host, server.server_port, ROOT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Serve the CareBricks API from a prepared snapshot.")
    parser.add_argument("--release", default="data/recovery/v3-manifest.json",
                        help="manifest of a v3 or later release (the default)")
    parser.add_argument("--data", help="folder of v2 parquet tables; overrides --release")
    parser.add_argument("--skip-post-offices", action="store_true",
                        help="do not load village and post-office names (saves about 300 MB)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DATABRICKS_APP_PORT", 8000)),
                        help="defaults to DATABRICKS_APP_PORT when a Databricks App sets it")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 11, 21))
    parser.add_argument("--app", default=str(STATIC_DIR), help="folder holding the web page")
    parser.add_argument("--env-file", default=".env.local", help="private local DeepSeek configuration; deployment environment takes precedence")
    parser.add_argument("--verified", default=VERIFIED_PATH,
                        help="the model verifier's output for the release; skipped when missing")
    parser.add_argument("--explainer", default=EXPLAINER_PATH, help="how-we-verify content; skipped when missing")
    parser.add_argument("--reviews", default=REVIEWS_PATH, help="public-source review file; skipped when missing")
    args = parser.parse_args()
    load_private_env(args.env_file)
    if args.data:
        index = CareIndex.from_parquet(args.data, as_of=args.as_of, index_version=args.data)
    else:
        index = CareIndex.from_release(args.release, post_offices=not args.skip_post_offices, as_of=args.as_of,
                                       verified=args.verified if Path(args.verified).is_file() else None)
    index.public_source_reviews = load_public_source_reviews(args.reviews)
    index.verification_explainer = load_json(args.explainer)
    serve(index, args.host, args.port, args.app)


if __name__ == "__main__":
    main()
