"""Build a reversible address, completeness and contact companion to the pinned v2 facility data."""

import argparse
from collections import Counter
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

import phonenumbers as phones
import pyarrow as pa
import pyarrow.parquet as pq
import unicodedataplus as ud

from backend.audit_facilities import _payload


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "data-2026-11-21-v2"
INPUTS = {
    "facilities": "0cce52c4eedc6616ba8c7ef69af87de3b9086e65a2cc0582a1e37792833347ed",
    "derived/clean_facilities": "b28ec90b07626c57fe939ca777d550c1cd6850c45f90ded2171649ce13d0fca1",
    "derived/facility_quarantine": "9e730dd5884fc1fb0a60e4a1a47460ee73fff7dca6058fa218ecae42ca505655",
}
ADDRESS = ("address_line1", "address_line2", "address_line3", "address_city", "address_stateOrRegion",
           "address_zipOrPostcode", "address_country", "address_countryCode")
CONTACT = ("officialPhone", "phone_numbers", "officialWebsite", "websites")
MARKERS = {"na", "n/a", "none", "null", "nil", "nan", "unknown", "not available", "not applicable",
           "not provided", "not specified", "undefined", "-", "--"}
PLATFORMS = {
    "directory": ("practo.com", "justdial.com", "lybrate.com", "eka.care", "indiamart.com"),
    "social": ("facebook.com", "fb.com", "instagram.com", "youtube.com", "youtu.be", "linkedin.com"),
    "hosted_platform": ("business.site", "sites.google.com", "webs.com", "wixsite.com", "wordpress.com", "blogspot.com"),
    "search_map_or_redirect_platform": ("bit.ly", "goo.gl", "g.co", "g.page", "google.com", "google.co.in"),
}
GENERIC = set("hospital hospitals healthcare health care clinic clinics multispeciality multispecialty "
              "speciality specialty medical college government govt district public private limited centre "
              "center institute research diagnostics diagnostic laboratory dental dentistry ayurvedic "
              "ayurveda maternity multispecialist general super group foundation memorial charitable "
              "india indian doctor doctors nursing home with and the".split())


def tidy(value):
    return " ".join(ud.normalize("NFC", value).split())


def search_key(text):
    chars = []
    for ch in ud.normalize("NFKC", text).casefold():
        if ch in "\u200c\u200d":
            continue
        if ch.isdecimal():
            chars.append(str(ud.decimal(ch)))
        else:
            chars.append(ch if ud.category(ch)[0] in "LMN" else " ")
    return " ".join("".join(chars).split())


def scripts(text):
    found = {ud.script(ch) for ch in text if ud.category(ch)[0] in "LMN"}
    return sorted(found - {"Common", "Inherited"})


def missing_reason(field, value):
    if value is None:
        return "sql_null"
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return "blank"
    if value.casefold() in MARKERS:
        return "literal_placeholder"
    if field == "email" and "[email protected]" in tidy(value).casefold() and "@" not in value:
        return "redacted"
    if not value.startswith(("[", "{", '"')):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        # A bracket can also begin ordinary prose. Only flag an expected structured field.
        if field in {"source_types", "source_ids", "source_urls", "coordinates", "affiliationTypeIds",
                     "specialties", "procedure", "equipment", "capability", "websites", "phone_numbers"}:
            return "unparseable_structure"
        return None
    if parsed == [] or parsed == {}:
        return "empty_container"
    if isinstance(parsed, str):
        if not parsed.strip() or parsed.strip().casefold() in MARKERS:
            return "encoded_placeholder"
    if isinstance(parsed, list) and not any(isinstance(v, str) and v.strip()
                                            and v.strip().casefold() not in MARKERS for v in parsed):
        if any(v is not None and not isinstance(v, str) for v in parsed):
            return "unexpected_member_type"
        return "placeholder_only_list"
    return None


def entries(field, raw):
    if missing_reason(field, raw) or not isinstance(raw, str):
        return []
    if raw.lstrip().startswith(("[", "{", '"')):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return []
    else:
        decoded = raw
    values = decoded if isinstance(decoded, list) else [decoded]
    return [v.strip() for v in values if isinstance(v, str) and v.strip() and v.strip().casefold() not in MARKERS]


def address_parts(row):
    return [(field, tidy(row[field])) for field in ADDRESS if not missing_reason(field, row[field])]


def romanizations(values, executable):
    values = sorted(set(values))
    version = subprocess.check_output([executable, "-V"], text=True).strip()
    if not version.endswith("ICU 78.3"):
        raise ValueError(f"Expected ICU 78.3, got {version}")
    if not values:
        return {}
    result = subprocess.run([executable, "-f", "UTF-8", "-t", "UTF-8", "-x", "Any-Latin; Latin-ASCII"],
                            input="\n".join(values) + "\n", capture_output=True, check=True, encoding="utf8")
    aliases = result.stdout.splitlines()
    if len(aliases) != len(values):
        raise ValueError("Unexpected transliteration line count")
    return dict(zip(values, aliases, strict=True))


def address_quality(row, aliases):
    parts = []
    for field, native in address_parts(row):
        parts.append(dict(field=field, display=native, scripts=scripts(native), romanization=aliases.get(native)))
    has_country = any(p["field"] == "address_country" for p in parts)
    display = [p for p in parts if not (has_country and p["field"] == "address_countryCode")]
    native = ", ".join(p["display"] for p in display)
    romanized = any(p["romanization"] for p in display)
    return dict(address_parts=parts, address_display=native or None, address_search_native=search_key(native) or None,
                address_scripts=sorted({s for p in parts for s in p["scripts"]}),
                address_search_romanized=(search_key(" ".join(p["romanization"] or p["display"] for p in display))
                                          if romanized else None),
                address_alias_review=romanized)


@lru_cache(maxsize=65536)
def phone_key(value):
    if not re.fullmatch(r"[+0-9() .\-]+", value):
        return None
    try:
        number = phones.parse(value, "IN")
    except phones.NumberParseException:
        return None
    if not phones.is_possible_number(number):
        return None
    return phones.format_number(number, phones.PhoneNumberFormat.E164)


@lru_cache(maxsize=65536)
def web_host(value):
    if (any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or "\\" in value or value.startswith(("/", "#", "?"))):
        return None
    try:
        url = urlsplit(value if "://" in value else "//" + value)
        if url.scheme not in ("", "http", "https") or url.username is not None or url.password is not None:
            return None
        if url.port not in (None, 80, 443) or not url.hostname:
            return None
        host = url.hostname.rstrip(".").encode("idna").decode().lower().removeprefix("www.")
        if "." not in host or len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            return None
        if not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)", host.split(".")[-1]):
            return None
        if host.split(".")[-1] in {"localhost", "local", "internal", "test", "example", "invalid", "onion"}:
            return None
        return host
    except (ValueError, UnicodeError):
        pass
    return None


def platform(host):
    return next((kind for kind, domains in PLATFORMS.items() for domain in domains
                 if host == domain or host.endswith("." + domain)), None)


def name_hint(host, row):
    words = set(re.findall("[a-z]{3,}", row["name"].casefold()))
    place_words = set(re.findall("[a-z]+", " ".join(row[k] or "" for k in ("address_city", "address_stateOrRegion")).lower()))
    labels = set(re.split("[.-]", host)) - {"com", "org", "net", "gov", "nic", "edu", "www"}
    return sorted(word for word in words - GENERIC - place_words if word in labels or
                  any(label.startswith(word) and label[len(word):] in GENERIC for label in labels))


def city_path_candidates(host, urls, city):
    if not host or missing_reason("address_city", city):
        return []
    needle = search_key(city)
    if len(needle) < 3:
        return []
    matches = set()
    for url in urls:
        if web_host(url) == host:
            path = urlsplit(url if "://" in url else "//" + url).path
            if " " + needle + " " in " " + search_key(unquote(path)) + " ":
                matches.add(url)
    return sorted(matches)


def contact_index(rows):
    phone_records, host_records = Counter(), Counter()
    for row in rows:
        keys = {phone_key(v) for f in ("officialPhone", "phone_numbers") for v in entries(f, row[f])}
        phone_records.update(keys - {None})
        hosts = {web_host(v) for v in entries("officialWebsite", row["officialWebsite"])}
        host_records.update(hosts - {None})
    return phone_records, host_records


def listed_distinct(raw):
    try:
        values = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(values, list):
        return None
    return len({v for v in values if isinstance(v, str)})


def contact_quality(row, phone_records, host_records):
    listed = entries("phone_numbers", row["phone_numbers"])
    plausible = sorted({k for v in listed if (k := phone_key(v))})
    officials = sorted(set(entries("officialPhone", row["officialPhone"])))
    number = phone_key(officials[0]) if len(officials) == 1 else None
    listed_match = number in plausible if number and plausible else None
    pf = []
    if len(plausible) > 10:
        pf.append("over_ten_possible_numbers")
    if len(officials) > 1:
        pf.append("multiple_designated_contacts")
    if number:
        parsed = phones.parse(number)
        if parsed.country_code != 91:
            pf.append("non_indian_country_code")
        if not phones.is_valid_number(parsed):
            pf.append("numbering_plan_pattern_unrecognized")
        if listed_match is not True:
            pf.append("absent_from_comparable_list" if listed_match is False else "no_comparable_list")
        if phone_records[number] > 1:
            pf.append("listed_by_multiple_records")
        if row["identity_review"]:
            pf.append("v2_identity_review")
    elif not missing_reason("officialPhone", row["officialPhone"]):
        pf.append("no_single_possible_number")
    urls = sorted(set(entries("officialWebsite", row["officialWebsite"])))
    host = web_host(urls[0]) if len(urls) == 1 else None
    reported_urls = entries("websites", row["websites"])
    hosts = {h for v in reported_urls if (h := web_host(v))}
    host_match = host in hosts if host and hosts else None
    hint = name_hint(host, row) if host else []
    wf = []
    if host:
        kind = platform(host)
        if kind:
            wf.append(kind)
        if host_match is not True:
            wf.append("absent_from_comparable_list" if host_match is False else "no_comparable_list")
        if host_records[host] > 1:
            wf.append("designated_by_multiple_records")
        if not hint:
            wf.append("no_distinctive_name_match")
        if row["identity_review"]:
            wf.append("v2_identity_review")
    elif not missing_reason("officialWebsite", row["officialWebsite"]):
        wf.append("no_single_recognized_host")

    def status(field, usable, flags):
        if missing_reason(field, row[field]):
            return "missing"
        if not usable:
            return "unusable"
        return "review_before_use" if flags else "listed_unconfirmed"

    return dict(listed_possible_phones=plausible,
                listed_unparsed_phone_text_count=sum(phone_key(v) is None for v in set(listed)),
                official_phone_e164=number, phone_in_reported_list=listed_match,
                phone_record_count=phone_records[number] if number else None, phone_flags=pf,
                phone_status=status("officialPhone", number, pf), official_web_host=host,
                website_in_reported_list=host_match, official_host_record_count=host_records[host] if host else None,
                website_name_tokens=hint, website_city_path_candidates=city_path_candidates(
                    host, [*urls, *reported_urls], row["address_city"]),
                website_flags=wf, website_status=status("officialWebsite", host, wf))


S = pa.string()
SS = pa.list_(S)
PARTS = pa.list_(pa.struct([("field", S), ("display", S), ("scripts", SS), ("romanization", S)]))
EXTRA = [
    ("record_key", S), ("source_row_count", pa.int64()), ("v2_disposition", S), ("quarantine_reasons", SS),
    ("unavailable_fields", pa.list_(pa.struct([("field", S), ("reason", S)]))),
    ("unavailable_field_count", pa.int32()), ("majority_unavailable", pa.bool_()),
    ("address_parts", PARTS), ("address_display", S), ("address_search_native", S), ("address_scripts", SS),
    ("address_search_romanized", S), ("address_alias_review", pa.bool_()),
    ("listed_phone_distinct_text_count", pa.int32()), ("listed_possible_phones", SS),
    ("listed_unparsed_phone_text_count", pa.int32()), ("official_phone_e164", S),
    ("phone_in_reported_list", pa.bool_()), ("phone_record_count", pa.int32()), ("phone_flags", SS),
    ("phone_status", S), ("official_web_host", S), ("website_in_reported_list", pa.bool_()),
    ("official_host_record_count", pa.int32()), ("website_name_tokens", SS), ("website_city_path_candidates", SS),
    ("website_flags", SS), ("website_status", S),
]


def enrich(row, raw_fields, disposition, aliases, indexes):
    gaps = [dict(field=f, reason=reason) for f in raw_fields if (reason := missing_reason(f, row[f]))]
    result = {k: row[k] for k in (*raw_fields, "record_key", "source_row_count")}
    result.update(v2_disposition=disposition, quarantine_reasons=row.get("quarantine_reasons", []),
                  unavailable_fields=gaps, unavailable_field_count=len(gaps),
                  majority_unavailable=len(gaps) > len(raw_fields) / 2,
                  listed_phone_distinct_text_count=listed_distinct(row["phone_numbers"]))
    if disposition == "clean":
        result.update(address_quality(row, aliases), **contact_quality(row, *indexes))
    else:
        result.update(phone_status="quarantined", website_status="quarantined")
    return result


def rows(path, columns=None):
    for batch in pq.ParquetFile(path).iter_batches(batch_size=256, columns=columns):
        yield from batch.to_pylist()


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def audit(path, raw, clean, quarantine):
    schema = pq.read_schema(raw)
    out = pq.read_schema(path)
    if not all(out.field(f.name).equals(f) for f in schema):
        raise ValueError("Original field schema changed")
    source = Counter(_payload(r, schema) for r in rows(raw))
    expected = {r["record_key"]: (disp, r["source_row_count"], r.get("quarantine_reasons", []), _payload(r, schema))
                for table, disp in ((clean, "clean"), (quarantine, "quarantined")) for r in rows(table)}
    for r in rows(path):
        key, count = r["record_key"], r["source_row_count"]
        payload = _payload(r, schema)
        if (r["v2_disposition"], count, r["quarantine_reasons"], payload) != expected.pop(key):
            raise ValueError(f"Lineage changed for {key}")
        if count != source.pop(payload):
            raise ValueError(f"Raw multiplicity changed for {key}")
        if r["v2_disposition"] == "quarantined" and any(r[f] is not None for f in
                ("address_display", "official_phone_e164", "official_web_host", "listed_possible_phones")):
            raise ValueError(f"Quarantined derived contact/address published for {key}")
    if expected or source:
        raise ValueError("Missing input records")


def build(data, target, uconv):
    raw, clean, quarantine = paths = [data / (key + ".parquet") for key in INPUTS]
    for path, expected in zip(paths, INPUTS.values(), strict=True):
        if digest(path) != expected:
            raise ValueError(f"Input does not match {RELEASE}: {path}")
    original = pq.read_schema(raw)
    schema = pa.schema([*original, *EXTRA], metadata={b"source_release": RELEASE.encode()})
    indexes = contact_index(rows(clean, CONTACT))
    aliases = romanizations((v for r in rows(clean, ADDRESS) for _, v in address_parts(r)
                             if set(scripts(v)) - {"Latin"}), uconv)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as scratch:
        staged = Path(scratch) / target.name
        with pq.ParquetWriter(staged, schema, compression="zstd") as writer:
            for table, disposition in ((clean, "clean"), (quarantine, "quarantined")):
                for batch in pq.ParquetFile(table).iter_batches(batch_size=256):
                    records = [enrich(r, original.names, disposition, aliases, indexes) for r in batch.to_pylist()]
                    writer.write_table(pa.Table.from_pylist(records, schema=schema))
        audit(staged, raw, clean, quarantine)
        staged.replace(target)
    print(f"{RELEASE} | {pq.ParquetFile(target).metadata.num_rows} keys | SHA-256 {digest(target)} | {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "data/enrichment/address_contact_quality.parquet")
    parser.add_argument("--uconv", default="uconv")
    args = parser.parse_args()
    build(args.data, args.output, args.uconv)
