"""Extract attributable statements about services from the Virtue Foundation records.

A documented service is a claim in the record. Neither a dated page nor a
direct service quote verifies today's staffing, uptime, beds or appointments.
"""
import json
import re
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit


SERVICES = {
    "dialysis": "Dialysis",
    "mri": "MRI",
    "nicu": "Neonatal intensive care",
    "c_section": "C-section",
    "emergency_c_section": "Emergency C-section",
}

_FIELDS = ("description", "capability", "procedure", "equipment", "specialties")
_C_SECTION = r"(?:c[-\s]?sections?|ca?esareans?(?:\s+(?:sections?|deliver(?:y|ies)))?|lscs)"
_TERMS = {
    "dialysis": r"(?:dialysis|h(?:ae|e)modialysis|peritoneal dialysis)",
    "mri": r"(?:mri|magnetic resonance imaging)",
    "nicu": r"(?:nicus?|(?:neonatal|newborn) intensive care(?: units?)?)",
    "c_section": _C_SECTION,
    "emergency_c_section": _C_SECTION,
}
_TERMS = {k: re.compile(r"\b" + term + r"\b", re.I) for k, term in _TERMS.items()}
_SPECIALTY = {
    "dialysis": re.compile(r"nephrolog", re.I),
    "mri": re.compile(r"radiolog|diagnostic imaging", re.I),
    "nicu": re.compile(r"neonatolog|perinatolog", re.I),
    "c_section": re.compile(r"obstetric|maternity|maternalfetal", re.I),
    "emergency_c_section": re.compile(r"obstetric|maternity|maternalfetal", re.I),
}
# Keep coordinate/equipment specifications such as "1.5 Tesla" and "Dr. Rao".
_SPLIT = re.compile(r";|\n|(?<=[!?])\s+|(?<!Dr)(?<!Mr)(?<!Ms)(?<!St)\.\s+(?=[A-Z])", re.I)
_NEG_BEFORE = re.compile(
    r"\b(?:lack(?:s|ing)?|without|no(?:\s+longer)?|(?:do(?:es)?|can|could|is|are)\s+not|doesn.t|cannot|"
    r"unable to)\s+(?:(?:on[- ]site|in[- ]house|emergency|urgent|elective|operational|functional|"
    r"available|working|functioning|yet|currently|equipped with|access to|any|a|provide|offer|have|perform|performing|support|have an?)\s+){0,5}$",
    re.I,
)
_NEG_AFTER = re.compile(
    r"^\W*(?:(?:services?|unit|facilit(?:y|ies)|scann?ers?|machines?|is|are|has|have|was|were|currently|"
    r"temporarily|permanently|remain[s]?|been|service)\W+){0,6}(?:not (?:available|offered|provided|"
    r"functional|performed|operational)|unavailable|discontinued|closed|suspended|out of (?:service|"
    r"order)|non[- ]functional|absent|under repair|not yet operational)\b",
    re.I,
)
# A qualifier with unclear scope (for example, "MRI and CT are not
# available") must not turn into a positive solely because of "available".
_UNCLEAR_NEGATION = re.compile(
    r"\b(?:not (?:currently |yet )?(?:available|provided|offered|operational|performed)|no longer|"
    r"unavailable|discontinued|out of (?:order|service)|does not (?:offer|provide|have)|lacks?|"
    r"no on[- ]site|not yet|not equipped|suspended|under repair)\b",
    re.I,
)
_PLAN = re.compile(
    r"\b(?:plan(?:s|ned|ning)? (?:for|to)|planned|propos(?:ed|al)|upcoming|will (?:add|install|open|"
    r"offer|start|launch)|to be (?:installed|commissioned|introduced)|under construction|"
    r"awaiting (?:installation|approval)|opening soon|expected to (?:open|launch|start)|"
    r"will be (?:installed|introduced)|future (?:expansion|plans)|aims? to|intends? to|in the pipeline)\b",
    re.I,
)
_REFERRAL = re.compile(
    r"\b(?:refers? (?:patients?|cases?) (?:out|elsewhere|to|for)|referred (?:out|elsewhere|to)|"
    r"refer(?:s|red)? out|referrals? (?:to|for)|refer(?:s|red)? .*?(?:elsewhere|outside|"
    r"another hospital)|elsewhere|outsourc\w*|off[- ]site|external (?:provider|centre|center|hospital)|"
    r"at a partner|at another|through (?:a )?partner|tie[- ]?ups?|near(?:by)? (?:an? |the )?)\b",
    re.I,
)
_INDIRECT = re.compile(
    r"\b(?:study|research|publication|training|course|fellowship|trained|academic|thesis|technicians?|"
    r"technologists?|recruits?|recruitment|vacanc(?:y|ies)|consumables?|manufacturer|"
    r"maintenance contract|awaiting|evaluat(?:es?|ing) (?:for|whether)|recommend(?:s|ed|ation)?|needs?|"
    r"counsel(?:s|ling)?|fundrais\w*|donat\w*|campaign|access to|remote (?:monitoring|support)|"
    r"tele[- ]?monitoring|virtual monitoring|consultations?|consult(?:s|ed)? for|booking(?:s)?|"
    r"arranges?|advises?|transport(?:s|ation)? to|history of|previously|formerly|risk of|candidate for|"
    r"after undergoing|referred by|across (?:the|its) network|network operating|as indicated by (?:its|the) name|"
    r"globally|home[- ]based|home dialysis|mobile dialysis|inform (?:your|imaging)|you have|"
    r"radiology[- ]guided|umbilical cord milking|ucm is performed)\b",
    re.I,
)
_NON_SERVICE = {
    "dialysis": re.compile(
        r"\b(?:fistula|(?:h(?:ae|e)mo)?dialysis access|vascular access for|catheter (?:placement|insertion|removal)|pre[- ]?dialysis|"
        r"dialysis education|post[- ]dialysis)\b",
        re.I,
    ),
    "mri": re.compile(
        r"\b(?:interpret(?:s|ation)?|reporting|review(?:s|ing)? (?:of )?(?:outside|external|mri)|"
        r"mri[- ](?:compatible|safe)|mri data|mri[- ]guided|mri referrals?)\b",
        re.I,
    ),
    "nicu": re.compile(r"\b(?:post[- ]?nicu|nicu (?:follow[- ]?up|live|graduates?)|transfer(?:s|red)? to)\b", re.I),
    "c_section": re.compile(
        r"\b(?:post[- ]?(?:ca?esarean|c[- ]section)|previous (?:ca?esarean|"
        r"c[- ]section)|scar revision)\b",
        re.I,
    ),
}
_BIRTH_AFTER = re.compile(r"\b(?:vaginal (?:birth|delivery)|birth|trial of (?:labor|labour))\s+after\s+"
                          r"(?:(?:a|an|previous|prior)\s+)*$|\bvbac\s*[(–:-]\s*$", re.I)
_DIALYSIS_INDIRECT_AFTER = re.compile(r"^\s+(?:(?:(?:treatments?|services?|sessions?)\s+)?at\s+home|support)\b", re.I)
_DIALYSIS_AT_HOME_BEFORE = re.compile(r"\bat[- ]home\s+(?:(?:renal|kidney|peritoneal)\s+)?$", re.I)
_PATIENT_REPORT = re.compile(r"\b(?:patient(?:s['’]?)? (?:reviews?|testimonials?|feedback)|(?:reported|described) by patients?)\b", re.I)
_NURSING_SCHOOL = re.compile(r"\b(?:(?:school|college|institute) of nursing|nursing (?:school|college))\b", re.I)
_BRANCH_OFFERING = re.compile(r"^\s*([\w'’.-]+(?:\s+[\w'’.-]+){0,4})\s+branch of\s+.+?\s+(?:offers?|provides?|performs?|has|operates?)\b", re.I)
_NETWORK_LOCATIONS = re.compile(r"^\s*(?:centre|center|branch|clinic) locations include\s*:?\s*(.+)", re.I)
_EMERG_BEFORE = re.compile(r"\b(?:emergency|emergent|urgent)\s+(?:operative\s+)?$", re.I)
_EMERG_AFTER = re.compile(
    r"^\W*(?:(?:deliver(?:y|ies)|performed)\W+)?(?:in (?:an? )?emergenc(?:y|ies)|"
    r"for (?:obstetric )?emergencies|on an emergency basis)\b",
    re.I,
)
_DIRECT = re.compile(
    r"\b(?:offers?|offering|provid(?:e[sd]?|ing)|perform(?:s|ed|ing)?|equipped with|operat(?:es?|ing)|"
    r"houses?|has|have|available|on[- ]site|in[- ]house|runs?|undergo|deliver(?:s|ing)?|includes?|"
    r"services? include|speciali[sz]es? in)\b",
    re.I,
)
_NOUN = {
    "dialysis": re.compile(
        r"(?:kidney |renal |advanced |maintenance |outpatient |in[- ]house |on[- ]site |pediatric |"
        r"peritoneal |emergency )*(?:dialysis|h(?:ae|e)modialysis)(?: (?:treatments?|services?|units?|"
        r"cent(?:re|er)s?|sessions?|facilit(?:y|ies)))?[.!]?$",
        re.I,
    ),
    "mri": re.compile(
        r"(?:\d(?:\.\d)?\s*(?:t|tesla)\s+)?(?:mri|magnetic resonance imaging)(?:\s+(?:scans?|scanning|"
        r"services?|imaging|facilit(?:y|ies)|units?))?[.!]?$",
        re.I,
    ),
    "nicu": re.compile(
        r"(?:(?:level[- ]?(?:[1-4]|i{1,3}|iv)|\w+[- ]bed(?:ded)?)\s+)?(?:nicus?|(?:neonatal|"
        r"newborn) intensive care(?: units?)?)(?:\s*\(nicu\))?[.!]?$",
        re.I,
    ),
    "c_section": re.compile(r"(?:(?:elective|scheduled|emergency|urgent|lower segment)\s+)*(?:" + _C_SECTION + r")(?:\s+deliver(?:y|ies))?[.!]?$", re.I),
}
# A service word can describe hardware, a facility name or another service.
# These clues apply even when the supplier placed them in a "capability" field.
_DEVICE = {
    "mri": re.compile(r"\b(?:mri|magnetic resonance imaging)\b.{0,25}\b(?:machines?|scanners?|systems?)\b", re.I),
    "dialysis": re.compile(r"\b(?:dialysis|h(?:ae|e)modialysis) (?:machines?|equipment)\b", re.I),
}
_TREATMENT = {
    "mri": re.compile(r"\bmri (?:scans?|scanning|imaging|services?)\b|performs? diagnostic imaging|"
                      r"performs? (?:orthopedic |cardiac )?mri\b", re.I),
    "dialysis": re.compile(r"\b(?:dialysis|h(?:ae|e)modialysis) (?:treatments?|therap(?:y|ies)|sessions?|services?|"
                           r"(?:is )?(?:provided|performed))\b|\b(?:offering|provides) h(?:ae|e)modialysis\b", re.I),
}
_NARROW_PROVIDER = re.compile(r"\b(?:eye|dental) (?:care )?(?:hospital|clinic|cent(?:re|er))\b", re.I)

# A record can contain claims explicitly assigned to a differently named hospital.
_SITE = re.compile(
    r"\b(?:at|by|from|near|within)\s+(?:the\s+)?((?:[A-Z][\w'’&.-]*\s+){1,9}(?:Hospital|Clinic|"
    r"Medical College|Centre|Center|Institute)\b)"
)
_SITE_SUBJECT = re.compile(
    r"^((?:[A-Z][\w'’&.-]*\s+){1,8}(?:Hospital|Clinic|Medical College|Centre|Center|"
    r"Institute))\s+(?:provides|offers|operates|houses|has)\b"
)
_GENERIC_NAME = set(
    "a the and of for hospital hospitals clinic centre center institute dialysis mri nicu medical care "
    "government general district super multispeciality multispecialty speciality specialty college research "
    "health unit diagnostic imaging private located free kidney neonatal intensive department".split()
)
_EXPLICIT_MERGE = re.compile(
    r"\b(?:merged (?:record|facilit)|(?:record|listing|entry) (?:combines|conflates)|(?:conflicting|"
    r"contradictory) (?:facility names|locations|addresses))",
    re.I,
)
_KIND_ORDER = ("negated", "unclear_negation", "referral", "planned", "other_site", "branch_unclear",
               "direct", "equipment", "patient_report", "indirect", "specialty")
_MAX_PER_KIND = 2
_REVIEW_FLAGS = {"identity_review", "location_review", "other_named_site", "specialty_name_mismatch",
                 "education_provider_review", "missing_record_source_url"}


def _items(raw):
    """Read the exported JSON arrays and plain strings, including Spark array input."""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw or raw.casefold() in {"null", "none", "nan"}:
            return []
        if raw.startswith(("[", '"', "{")):
            try:
                raw = json.loads(raw)
            except ValueError:
                # Malformed structured text lacks trustworthy item boundaries.
                return []
    if isinstance(raw, (tuple, list)):
        return [(i, value) for i, value in enumerate(raw) if isinstance(value, str) and value.strip()]
    return [(None, raw)] if isinstance(raw, str) else []


def _name_tokens(name):
    return set(re.findall(r"[a-z0-9]+", (name or "").casefold())) - _GENERIC_NAME


def _other_site(text, name, city):
    for pattern in (_SITE, _SITE_SUBJECT):
        for match in pattern.finditer(text):
            site = match.group(1)
            if name and site.casefold() in name.casefold():
                continue
            city_tokens = _name_tokens(city)
            tokens = _name_tokens(site) - city_tokens
            own = _name_tokens(name) - city_tokens
            # A chain or a city token alone does not identify the same branch.
            if not tokens and own:
                return True
            if tokens and 2 * len(tokens & own) <= len(tokens):
                return True
    return False


def _branch_unclear(text, name, site_address):
    """Flag a named branch whose location cannot be tied to this record."""
    folded = " ".join(text.casefold().split())
    if name and "branch of " + name.strip().casefold() in folded:
        # A service described at a branch can be incorrectly attributed to its parent.
        if re.search(r"\b(?:operates? as|is) (?:an? )?branch of\b", folded):
            return True
    branch = _BRANCH_OFFERING.match(text)
    if not branch:
        return False
    label = " ".join(branch.group(1).casefold().split())
    return label not in site_address


def _combines_branch_addresses(text, address_lines):
    """Separate branches enumerated by the record appear in distinct address lines."""
    listed = _NETWORK_LOCATIONS.match(text)
    if not listed:
        return False
    matches = []
    for label in re.split(r",|\s+and\s+", listed.group(1).casefold()):
        label = label.strip(" .")
        if len(label) < 4 or label in {"others", "other sites"}:
            continue
        for index, line in enumerate(address_lines):
            if re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", line):
                if any(index != other_index and label not in other and other not in label
                       for other, other_index in matches):
                    return True
                matches.append((label, index))
    return False


def _kind(service, field, text, match, name, city, site_address):
    base = "c_section" if service == "emergency_c_section" else service
    before, after = text[:match.start()], text[match.end():]
    if service == "emergency_c_section" and not (_EMERG_BEFORE.search(before) or _EMERG_AFTER.search(after)):
        return "indirect"
    if _NEG_BEFORE.search(before) or _NEG_AFTER.search(after):
        return "negated"
    if _branch_unclear(text, name, site_address):
        return "branch_unclear"
    if name:
        start = text.lower().find(name.strip().lower())
        if start >= 0 and start <= match.start() < start + len(name.strip()):
            return "indirect"
    if _UNCLEAR_NEGATION.search(text):
        return "unclear_negation"
    if _other_site(text, name, city):
        return "other_site"
    if _REFERRAL.search(text):
        return "referral"
    if _PLAN.search(text):
        return "planned"
    if _PATIENT_REPORT.search(text):
        return "patient_report"
    if base == "c_section" and _BIRTH_AFTER.search(before):
        return "indirect"
    if base == "dialysis" and (_DIALYSIS_INDIRECT_AFTER.search(after) or _DIALYSIS_AT_HOME_BEFORE.search(before)):
        return "indirect"
    if _INDIRECT.search(text) or _NON_SERVICE[base].search(text):
        return "indirect"
    if field == "specialties":
        return "specialty"
    if field == "equipment" or (base in _DEVICE and _DEVICE[base].search(text) and not _TREATMENT[base].search(text)):
        return "equipment"
    if _DIRECT.search(text) or _NOUN[base].fullmatch(text):
        return "direct"
    return "indirect"


def _date(raw):
    try:
        return date.fromisoformat(str(raw)).isoformat()
    except ValueError:
        return None


def _urls(raw):
    urls = []
    for _, value in _items(raw):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme.lower() in {"http", "https"} and parsed.hostname and "." in parsed.hostname
                    and not parsed.username and not any(char.isspace() for char in value)):
                urls.append(value)
        except ValueError:
            continue
    return list(dict.fromkeys(urls))


def _summary(kinds):
    limitations = kinds.intersection({"negated", "planned", "referral"})
    if kinds.intersection({"other_site", "branch_unclear", "unclear_negation"}):
        return "review"
    if ("direct" in kinds and limitations) or len(limitations) > 1:
        return "mixed"
    if limitations:
        status = next(iter(limitations))
        return "referral_only" if status == "referral" else status
    return "documented" if "direct" in kinds else "suggestive"


def extract_service_evidence(record):
    """Return one row per matched service, retaining a bounded sample of every evidence kind.

    item_index is zero-based for JSON arrays and null for a plain text field.
    A quote is an exact substring of the decoded field item. URLs belong to the
    entire source record. A repeated assertion cannot raise the evidence tier.
    """
    if hasattr(record, "asDict"):
        record = record.asDict()
    name = record.get("name")
    city = record.get("address_city")
    address_lines = [(record.get(key) or "").casefold() for key in ("address_line1", "address_line2", "address_line3")]
    site_address = " ".join([(name or "").casefold(), (city or "").casefold(), *address_lines])
    hits = defaultdict(lambda: defaultdict(list))
    seen = defaultdict(set)
    identity_example = None
    for field in _FIELDS:
        for index, item in _items(record.get(field)):
            if identity_example is None and (_EXPLICIT_MERGE.search(item) or _combines_branch_addresses(item, address_lines)):
                identity_example = dict(field=field, item_index=index, quote=item, kind="identity_conflict")
            for raw_part in _SPLIT.split(item):
                quote = raw_part.strip()
                if not quote:
                    continue
                for service, pattern in _TERMS.items():
                    matches = list(pattern.finditer(quote))
                    if matches:
                        kinds = {_kind(service, field, quote, match, name, city, site_address) for match in matches}
                    elif field == "specialties" and _SPECIALTY[service].search(quote):
                        kinds = {"specialty"}
                    else:
                        continue
                    for kind in kinds:
                        key = (quote.casefold(), kind)
                        bucket = hits[service][kind]
                        if key not in seen[service] and len(bucket) < _MAX_PER_KIND:
                            bucket.append(dict(field=field, item_index=index, quote=quote, kind=kind))
                            seen[service].add(key)

    # A dialysis unit attributed to a different hospital also calls maternity
    # claims in the same merged record into question. Carry the cited conflict
    # across services instead of silently ranking a different claim as clear.
    if identity_example is None:
        identity_example = next((docs[kind][0] for docs in hits.values()
                                 for kind in ("other_site", "branch_unclear") if kind in docs), None)
    page_date = _date(record.get("usable_page_date", record.get("recency_of_page_update")))
    urls = _urls(record.get("source_urls"))
    rows = []
    for service, groups in hits.items():
        status = _summary(set(groups))
        flags = []
        if identity_example or record.get("identity_review"):
            flags.append("identity_review")
        if record.get("location_review"):
            flags.append("location_review")
        if "other_site" in groups:
            flags.append("other_named_site")
        if "branch_unclear" in groups:
            flags.append("branch_attribution_unclear")
        if "unclear_negation" in groups:
            flags.append("unclear_negation_scope")
        if status == "mixed":
            flags.append("conflicting_service_statements")
        if name and _NARROW_PROVIDER.search(name) and service != "mri":
            flags.append("specialty_name_mismatch")
        if name and _NURSING_SCHOOL.search(name):
            flags.append("education_provider_review")
        if not urls:
            flags.append("missing_record_source_url")
        if not page_date:
            flags.append("page_update_unknown")
        citations = [c for kind in _KIND_ORDER for c in groups.get(kind, [])]
        if identity_example and identity_example not in citations:
            citations.append(dict(identity_example, kind="identity_conflict"))
        rows.append({
            "record_key": record.get("record_key"), "unique_id": record.get("unique_id"),
            "service": service, "evidence_status": status, "availability_status": "unverified",
            "eligible": status == "documented" and _REVIEW_FLAGS.isdisjoint(flags),
            "flags": flags, "citations": citations,
            "record_source_urls": urls, "page_update_date": page_date,
        })
    return rows


def build_service_evidence(facilities_df):
    """Build long-form evidence from raw fields plus record_key and review booleans."""
    import sys
    from pyspark import cloudpickle
    from pyspark.sql import functions as F

    # Spark Connect serializes the UDF lazily. Register only this module so
    # Python workers receive its standard-library helpers with the function,
    # even when a Databricks Git folder exists only on the client/driver.
    cloudpickle.register_pickle_by_value(sys.modules[__name__])

    fields = ("record_key", "unique_id", "name", *_FIELDS, "source_urls", "recency_of_page_update",
              "identity_review", "location_review")
    fields += tuple(col for col in ("address_city", "address_line1", "address_line2", "address_line3",
                                    "usable_page_date") if col in facilities_df.columns)
    schema = ("array<struct<record_key:string,unique_id:string,service:string,evidence_status:string,"
              "availability_status:string,eligible:boolean,flags:array<string>,citations:array<struct<field:string,item_index:int,"
              "quote:string,kind:string>>,record_source_urls:array<string>,page_update_date:string>>")
    extract = F.udf(extract_service_evidence, schema)
    return facilities_df.select(F.explode(extract(F.struct(*fields))).alias("e")).select("e.*")
