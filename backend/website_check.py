"""Check whether a facility's own website states a service.

    uv run --python 3.11 --with pyarrow python -m backend.website_check \
        --claims data/verifier_runs/v3_verified_evidence.jsonl \
        --contacts data/enrichment/address_contact_quality.parquet \
        --out data/enrichment/website_checks.jsonl --cache-dir data/enrichment/website_cache --workers 4

For each eligible facility-service claim, the facility's official website is
read: the homepage and at most three pages on the same host whose link looks
like a services or departments page. The page text is searched with the same
service terms the verifier uses. A match is kept as a sentence copied from the
page text, and the row is written only if that sentence is a verbatim
substring of the text.

Everything fetched is treated as data. Nothing on a page is followed except
same-host links chosen by the fixed pattern below. robots.txt is honoured,
requests to one host are made one at a time with a pause between them, every
response is capped in size, and raw HTML is cached on disk so a run can be
stopped, resumed and replayed with `--offline`.

A website statement is the provider's own claim, read once on the date in
`fetched_at`. It is not confirmation that the unit is running.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from backend import verifier as V

USER_AGENT = "Mozilla/5.0 (compatible; facility-website-check/1.0)"
ROBOTS_NAME = "facility-website-check"
MAX_BYTES = 1000000
MAX_LINKED_PAGES = 3
MAX_QUOTE_CHARS = 300
STATUSES = ("states_service", "site_reachable_no_mention", "unreachable", "blocked_by_robots", "not_checked")

# Hosts that list many providers. A statement found there is not the facility's own website.
_AGGREGATORS = re.compile(
    r"(?:^|\.)(?:practo|justdial|facebook|instagram|linkedin|twitter|x|youtube|google|lybrate|sulekha|indiamart|"
    r"wikipedia|medindia|medicineindia|docindia|onefivenine|credihealth|mfine|1mg|apollo247|bajajfinserv|"
    r"bajajfinservhealth|quickerala|yellowpages|tradeindia|blogspot|wordpress|wixsite|business\.site|linktr)"
    r"\.(?:com|in|org|net|co\.in|ee)$", re.I)
_PARKED = re.compile(r"domain (?:name )?(?:is|may be) for sale|buy this domain|this domain is parked|domain parking|"
                     r"account (?:has been )?suspended|website is (?:coming soon|under construction)", re.I)
_SERVICE_LINK = re.compile(r"service|department|facilit|special|treatment|centre|center|procedure|diagnos|"
                           r"what-we-do|our-", re.I)
_NOT_A_PAGE = re.compile(r"\.(?:pdf|jpe?g|png|gif|webp|svg|zip|docx?|xlsx?|mp4|mp3)(?:$|\?)", re.I)
# A sentence that names the service but says it is absent, elsewhere or only planned is not a statement.
_NOT_A_STATEMENT = re.compile(r"\b(?:no|not|without|refer(?:s|red|ral)?|outsourc\w*|tie[-\s]?up|unavailable)\b", re.I)
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "br", "tr", "td", "th", "table", "section", "article", "header", "footer",
               "nav", "h1", "h2", "h3", "h4", "h5", "h6", "option", "dd", "dt", "blockquote", "form", "title"}


def bare_host(value):
    """Return the lower-case host of a URL or host string, without a leading www."""
    value = (value or "").strip().lower()
    host = urlsplit(value if "//" in value else "//" + value).hostname or ""
    return host[4:] if host.startswith("www.") else host


def is_aggregator(host):
    return bool(_AGGREGATORS.search(bare_host(host)))


class _Page(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts, self.links, self._skip, self._link = [], [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            self._link = [dict(attrs).get("href") or "", []]

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a" and self._link:
            self.links.append((self._link[0], " ".join("".join(self._link[1]).split())))
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._link:
            self._link[1].append(data)


def page_text_and_links(html):
    """Return (visible text with one line per block, [(href, anchor text)])."""
    parser = _Page()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup: keep what was read
        pass
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line), parser.links


def service_links(base_url, links, host):
    """Return at most MAX_LINKED_PAGES same-host URLs that look like services or departments pages."""
    chosen, seen = [], {base_url.split("#")[0].rstrip("/")}
    for href, anchor in links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "whatsapp:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or bare_host(url) != host or _NOT_A_PAGE.search(parts.path):
            continue
        if url.rstrip("/") in seen or not (_SERVICE_LINK.search(parts.path) or _SERVICE_LINK.search(anchor)):
            continue
        seen.add(url.rstrip("/"))
        chosen.append(url)
        if len(chosen) == MAX_LINKED_PAGES:
            break
    return chosen


def find_statement(text, service):
    """Return a sentence of `text` that names the service, or None. The result is a verbatim substring."""
    for line in text.split("\n"):
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if not V.names_service(sentence, service) or _NOT_A_STATEMENT.search(sentence):
                continue
            if V._ENDED_OR_PLANNED.search(sentence):
                continue
            quote = sentence.strip()
            if len(quote) > MAX_QUOTE_CHARS:
                at = V._TERMS[service].search(quote).start()
                start = max(0, min(at - MAX_QUOTE_CHARS // 2, len(quote) - MAX_QUOTE_CHARS))
                quote = quote[start:start + MAX_QUOTE_CHARS].strip()
                if not V.names_service(quote, service):
                    continue
            if quote and quote in text:
                return quote
    return None


def _http_get(url, timeout):
    """Return (final_url, status, content_type, bytes). Raises on network errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.geturl(), response.status, response.headers.get_content_type(),
                    response.read(MAX_BYTES + 1)[:MAX_BYTES])
    except urllib.error.HTTPError as error:
        return error.geturl() or url, error.code, "", b""


class Fetcher:
    """GET with a disk cache. One entry per URL: the outcome, the time it was fetched and the raw HTML."""

    def __init__(self, cache_dir, http_get=_http_get, timeout=8, delay=1.0, offline=False, now=None):
        self.cache_dir, self.http_get, self.timeout, self.delay = cache_dir, http_get, timeout, delay
        self.offline, self.now = offline, now or (lambda: datetime.now(timezone.utc))
        self.requests, self.cache_hits, self._count = 0, 0, threading.Lock()
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, url):
        return os.path.join(self.cache_dir, hashlib.sha256(url.encode("utf-8")).hexdigest() + ".json.gz")

    def get(self, url):
        """Return {url, final_url, status, html, error, fetched_at}. Never raises."""
        path = self._path(url)
        if os.path.exists(path):
            with gzip.open(path, "rt", encoding="utf-8") as stored:
                entry = json.load(stored)
            with self._count:
                self.cache_hits += 1
            return entry
        entry = {"url": url, "final_url": url, "status": None, "html": "", "error": None,
                 "fetched_at": self.now().isoformat()}
        if self.offline:
            return dict(entry, error="not_in_cache")
        if self.delay:
            time.sleep(self.delay)
        with self._count:
            self.requests += 1
        try:
            final_url, status, content_type, body = self.http_get(url, self.timeout)
            entry.update(final_url=final_url, status=status)
            if status and 200 <= status < 300 and (not content_type or "html" in content_type or "text" in content_type):
                entry["html"] = body.decode("utf-8", errors="replace")
            elif status and 200 <= status < 300:
                entry["error"] = "not_html"
        except Exception as error:  # DNS failure, timeout, TLS error, connection reset
            entry["error"] = type(error).__name__
        tmp = path + ".tmp{}".format(threading.get_ident())
        with gzip.open(tmp, "wt", encoding="utf-8") as sink:
            json.dump(entry, sink)
        os.replace(tmp, path)
        return entry


def _robots(fetcher, origin):
    """Return a can_fetch(url) function for this origin, following the usual robots.txt conventions."""
    entry = fetcher.get(origin + "/robots.txt")
    if entry["status"] in (401, 403):
        return lambda url: False
    if not entry["html"] or bare_host(entry["final_url"]) != bare_host(origin):
        return lambda url: True  # missing or unreadable robots.txt allows everything
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(entry["html"].splitlines())
    return lambda url: parser.can_fetch(ROBOTS_NAME, url)


def check_site(host, services, fetcher):
    """Read one website. Return {service: {status, url, quote, fetched_at, http_status, pages_read, note}}."""
    host = bare_host(host)
    base = {"url": None, "quote": None, "fetched_at": None, "http_status": None, "pages_read": 0, "note": None}

    def all_services(status, **fields):
        return {service: dict(base, status=status, **fields) for service in services}

    if not host or "." not in host:
        return all_services("not_checked", note="no_official_website")
    if is_aggregator(host):
        return all_services("not_checked", note="listed_site_is_a_directory_or_social_page")
    home, last = None, None
    for scheme in ("https", "http"):
        origin = scheme + "://" + host
        allowed = _robots(fetcher, origin)
        if not allowed(origin + "/"):
            return all_services("blocked_by_robots", url=origin + "/", fetched_at=fetcher.now().isoformat())
        last = fetcher.get(origin + "/")
        if last["html"]:
            home = last
            break
    if home is None and last["error"] == "not_in_cache":
        return all_services("not_checked", note="not_in_cache")
    if home is None:
        return all_services("unreachable", url=last["url"], fetched_at=last["fetched_at"], http_status=last["status"],
                            note=last["error"] or "http_{}".format(last["status"]))
    seen = {"url": home["final_url"], "fetched_at": home["fetched_at"], "http_status": home["status"]}
    if bare_host(home["final_url"]) != host:
        return all_services("unreachable", note="host_mismatch", **seen)
    text, links = page_text_and_links(home["html"])
    if _PARKED.search(text[:3000]) or len(text) < 40:
        return all_services("unreachable", note="parked_or_empty_page", **seen)
    pages = [(home["final_url"], text)]
    for url in service_links(home["final_url"], links, host):
        if not allowed(url):
            continue
        entry = fetcher.get(url)
        if entry["html"] and bare_host(entry["final_url"]) == host:
            pages.append((entry["final_url"], page_text_and_links(entry["html"])[0]))
    result = {}
    for service in services:
        found = next(((url, quote) for url, body in pages for quote in [find_statement(body, service)] if quote), None)
        if found:
            result[service] = dict(base, status="states_service", url=found[0], quote=found[1],
                                   fetched_at=home["fetched_at"], http_status=home["status"], pages_read=len(pages))
        else:
            result[service] = dict(base, status="site_reachable_no_mention", pages_read=len(pages), **seen)
    return result


def check_claims(claims, contacts, fetcher, include_review=False, limit_sites=None, workers=1):
    """Return one row per claim. `claims` are (record_key, service); `contacts` maps record_key to a contact row."""
    usable = ("listed_unconfirmed", "review_before_use") if include_review else ("listed_unconfirmed",)
    by_host, rows = {}, {}
    for key, service in claims:
        contact = contacts.get(key) or {}
        host, state = bare_host(contact.get("official_web_host")), contact.get("website_status")
        if host and state in usable:
            by_host.setdefault(host, set()).add(service)
        else:
            note = "no_official_website" if not host else "website_status_" + str(state)
            rows[(key, service)] = {"status": "not_checked", "url": None, "quote": None, "fetched_at": None,
                                    "http_status": None, "pages_read": 0, "note": note}
    # Sites the address check trusts more come first, so a limited pilot reads those.
    order = sorted(by_host, key=lambda h: (h,))
    first = {bare_host(c.get("official_web_host")) for c in contacts.values() if c.get("website_status") == "listed_unconfirmed"}
    order.sort(key=lambda h: h not in first)
    skipped = set(order[limit_sites:]) if limit_sites is not None else set()
    order = order[:limit_sites]

    def one(host):
        try:
            return host, check_site(host, sorted(by_host[host]), fetcher)
        except Exception as error:  # one bad site must not stop the run
            return host, {s: {"status": "unreachable", "url": None, "quote": None, "fetched_at": None,
                              "http_status": None, "pages_read": 0, "note": "error_" + type(error).__name__}
                          for s in by_host[host]}

    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            sites = dict(pool.map(one, order))
    else:
        sites = dict(one(h) for h in order)
    out = []
    for key, service in claims:
        contact = contacts.get(key) or {}
        host = bare_host(contact.get("official_web_host"))
        row = rows.get((key, service))
        if row is None and host in skipped:
            row = {"status": "not_checked", "url": None, "quote": None, "fetched_at": None, "http_status": None,
                   "pages_read": 0, "note": "outside_this_run"}
        elif row is None:
            row = sites[host][service]
        out.append(dict({"record_key": key, "service": service, "host": host or None,
                         "website_status": contact.get("website_status")}, **row))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--claims", required=True, help="verified evidence JSONL; eligible rows are checked")
    parser.add_argument("--contacts", required=True, help="address_contact_quality.parquet")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--workers", type=int, default=1, choices=range(1, 9), metavar="1-8",
                        help="sites read at once; each site is still read one request at a time")
    parser.add_argument("--include-review", action="store_true", help="also read sites marked review_before_use")
    parser.add_argument("--limit-sites", type=int, help="read only the first N sites, for a pilot")
    parser.add_argument("--offline", action="store_true", help="use the cache only and make no request")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds to wait before each request")
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args(argv)

    import pyarrow.parquet as pq
    with open(args.claims, encoding="utf-8") as source:
        claims = [(r["record_key"], r["service"]) for r in map(json.loads, source) if r.get("eligible")]
    wanted = {key for key, _ in claims}
    columns = ["record_key", "official_web_host", "website_status"]
    contacts = {r["record_key"]: r for r in pq.read_table(args.contacts, columns=columns).to_pylist()
                if r["record_key"] in wanted}
    fetcher = Fetcher(args.cache_dir, timeout=args.timeout, delay=args.delay, offline=args.offline)
    started = time.perf_counter()
    rows = check_claims(claims, contacts, fetcher, args.include_review, args.limit_sites, args.workers)
    with open(args.out, "w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({
        "claims": len(rows), "status": Counter(r["status"] for r in rows),
        "notes": Counter(r["note"] for r in rows if r["note"]),
        "sites_read": len({r["host"] for r in rows if r["status"] != "not_checked"}),
        "requests_made": fetcher.requests, "answered_from_cache": fetcher.cache_hits,
        "seconds": round(time.perf_counter() - started, 1),
    }))


if __name__ == "__main__":
    main()
