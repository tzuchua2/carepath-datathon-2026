"""Build a copy-ready referral note from one search result. No model call.

`referral_note(response, facility_id)` takes a response from
`CareIndex.recommend` and the `facility_id` of one item in it, and returns
`{"title", "kind", "sections": [{"heading", "lines"}], "text"}`. The note uses
only what is in the response. It has no patient fields: nothing about a
patient is asked for, stored or written.

A facility from `results` gets a referral note. A facility from one of the
lead lists gets a "lead to verify" note that says it is not a recommendation.
The text is plain sentences, wrapped at 80 columns and at most 40 lines.
"""
import textwrap
from datetime import datetime, timezone

WIDTH = 80
MAX_LINES = 40
MAX_QUOTES = 3
MAX_QUOTE_CHARS = 150
LEAD_LISTS = ("unverified_leads", "model_found_leads", "location_unverified_leads", "closer_but_not_recommended",
              "set_aside")

_CHECKS = (
    ("record_states_service", "The facility's record states the service"),
    ("statement_is_specific", "The statement gives a specific detail"),
    ("model_reading_agrees", "A model reading of the record agrees"),
    ("facility_source_states_it", "The facility's own website or material mentions it"),
    ("independent_source_lists_it", "An independent source lists it"),
)
_STATUS = {"passed": "Yes", "not_found": "Not found", "failed": "Not found", "not_checked": "Not checked"}
_PRECISION = {"locality": "a city or locality centre", "pincode": "a PIN code area", "district": "a district centre",
              "point": "an exact point", "address": "an address"}
_QUESTIONS = (
    "Is {service} running today, and until what time?",
    "Is there a slot or bed for this patient now?",
    "What are the charges, and which schemes or insurance are accepted?",
    "Which referral papers and reports should the patient bring?",
    "What is the best way to reach you, and which entrance or desk?",
    "Whom should the patient ask for on arrival?",
)
_LEAD_REASON = {
    "MODEL_FOUND_NEEDS_VERIFICATION": "Only a model reading found this statement. The text rules did not.",
    "NEEDS_VERIFICATION": "The record mentions the service, but a check on the record is unresolved.",
    "SET_ASIDE": "The record was set aside by a check on its identity, location or source.",
}


class NoteError(ValueError):
    """The response has no item with this facility_id."""


def _find(response, facility_id):
    for item in response.get("results") or []:
        if item.get("facility_id") == facility_id:
            return item, "referral"
    for name in LEAD_LISTS:
        for item in response.get(name) or []:
            if item.get("facility_id") == facility_id:
                return item, "lead"
    raise NoteError("no result or lead with facility_id {!r} in this response".format(facility_id))


def _clip(text, limit=MAX_QUOTE_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _searched(response, prepared_on):
    query = response.get("query") or {}
    origin = query.get("origin") or {}
    where = origin.get("display_name") or "the search location"
    precision = _PRECISION.get(origin.get("precision"), str(origin.get("precision") or "an approximate point"))
    line = "{} near {}, taken as {}".format(query.get("service_display_name") or query.get("service_code"), where,
                                            precision)
    if origin.get("uncertainty_km"):
        line += " (give or take about {:g} km)".format(origin["uncertainty_km"])
    return [line + ".", "Prepared on {} from facility records. Nobody has phoned this facility.".format(prepared_on)]


def _facility(item):
    location, contact, distance = item.get("location") or {}, item.get("contact") or {}, item.get("distance") or {}
    lines = [str(item.get("display_name") or "Name not recorded")]
    address = location.get("address_label") or ", ".join(
        str(location[k]) for k in ("city", "state", "pincode") if location.get(k))
    if address:
        lines.append("Address as listed: " + _clip(address, 140))
    if distance.get("value_km") is not None:
        lines.append("About {:g} km away in a straight line. Road distance and travel time are not known.".format(
            distance["value_km"]))
    if contact.get("phone") and contact.get("phone_status") == "listed_unconfirmed":
        lines.append("Phone (listed, not confirmed): {}".format(contact["phone"]))
    else:
        lines.append("Phone: none we can pass on. Look the number up before calling.")
    return lines


def _quotes(item):
    lines = []
    for quote in (item.get("evidence_preview") or [])[:MAX_QUOTES]:
        if quote.get("exact_quote"):
            lines.append('"{}" ({})'.format(_clip(quote["exact_quote"]), quote.get("source_field") or "record"))
    return lines or ["No quote is available for this claim."]


def _checks(item):
    done = {c.get("key"): c for c in (item.get("ranking") or {}).get("evidence_checks") or []}
    if not done:
        return ["The evidence checks were not run for this entry."]
    lines = []
    for key, label in _CHECKS:
        status = (done.get(key) or {}).get("status") or "not_checked"
        lines.append("{}: {}.".format(_STATUS.get(status, "Not checked"), label))
    return lines


def _unconfirmed(item):
    first = "Whether the service is running today, and whether there is a slot or bed."
    lines = [first]
    for entry in item.get("uncertainty") or []:
        text = entry.get("text") if isinstance(entry, dict) else str(entry)
        code = entry.get("code") if isinstance(entry, dict) else None
        if text and code != "current_availability_not_confirmed" and len(lines) < 3:
            lines.append(_clip(text, 150))
    return lines


def _lead_reasons(item):
    lines = [_LEAD_REASON.get(item.get("tier"), _LEAD_REASON["NEEDS_VERIFICATION"])]
    for reason in (item.get("reasons") or [])[:2]:
        text = reason.get("text") if isinstance(reason, dict) else str(reason)
        if text:
            lines.append(_clip(text, 150))
    if item.get("manual_review_reason"):
        lines.append("A reviewer's note on this record: " + _clip(item["manual_review_reason"], 110))
    return lines


def _render(title, sections):
    out = textwrap.wrap(title, WIDTH)
    for section in sections:
        out.append("")
        out.append(section["heading"])
        for line in section["lines"]:
            out.extend(textwrap.wrap("- " + line, WIDTH, subsequent_indent="  ", break_long_words=True) or ["-"])
    return out


def referral_note(response, facility_id, prepared_on=None):
    """Return the note for one item of a search response. Raises NoteError if the item is not in it."""
    item, kind = _find(response, facility_id)
    prepared_on = prepared_on or datetime.now(timezone.utc).date().isoformat()
    query = response.get("query") or {}
    service = query.get("service_display_name") or query.get("service_code") or "the service"
    emergency = query.get("service_code") == "emergency_c_section"
    name = _clip(item.get("display_name") or "facility", 36)
    if kind == "referral":
        title = "Referral note: {} at {}".format(service, name)
        sections = [{"heading": "What was searched", "lines": _searched(response, prepared_on)},
                    {"heading": "Facility", "lines": _facility(item)},
                    {"heading": "What the record says (copied word for word)", "lines": _quotes(item)},
                    {"heading": "How it was checked", "lines": _checks(item)},
                    {"heading": "What nobody has confirmed", "lines": _unconfirmed(item)}]
    else:
        title = "Lead to verify, not a recommendation: {} at {}".format(service, name)
        sections = [{"heading": "What was searched", "lines": _searched(response, prepared_on)},
                    {"heading": "Why this is only a lead",
                     "lines": ["This facility is not recommended. Verify it by phone before relying on it."]
                     + _lead_reasons(item)},
                    {"heading": "Facility", "lines": _facility(item)},
                    {"heading": "What the record says (copied word for word)", "lines": _quotes(item)},
                    {"heading": "What nobody has confirmed", "lines": _unconfirmed(item)}]
    questions = [q.format(service=service.lower() if service.isalpha() and not service.isupper() else service)
                 for q in _QUESTIONS]
    sections.append({"heading": "Questions to ask on the phone", "lines": questions})
    if emergency:
        sections.insert(0, {"heading": "Emergency", "lines": [
            "If this is an emergency in progress, use emergency referral channels now. Do not rely on this list."]})

    # Keep the note within MAX_LINES: drop the last quote, then extra unconfirmed and reason lines.
    def shrink():
        for heading, keep in (("What the record says (copied word for word)", 1), ("What nobody has confirmed", 1),
                              ("Why this is only a lead", 2), ("What was searched", 1), ("Facility", 3)):
            for section in sections:
                if section["heading"] == heading and len(section["lines"]) > keep:
                    section["lines"].pop()
                    return True
        return False

    lines = _render(title, sections)
    while len(lines) > MAX_LINES and shrink():
        lines = _render(title, sections)
    return {"title": title, "kind": kind, "prepared_on": prepared_on, "facility_id": facility_id,
            "sections": sections, "text": "\n".join(lines[:MAX_LINES])}
