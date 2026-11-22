"""Held-out evaluation set for the text rules and the verifier (design doc section 8.1).

A label here says what a facility record SAYS about a service. It is not a
statement that the service runs today.

    python -m backend.eval_set build     # sample pairs; needs pyarrow
    python -m backend.eval_set check     # validate data/eval/labels.csv
    python -m backend.eval_set score --rules
    python -m backend.eval_set score --predictions verifier_rows.jsonl

`build` writes three files and never touches the label sheet:
  pairs.jsonl        the pair, the facility name and place, and the passages to read
  rule_outcomes.csv  what the rules decided, keyed by pair_id, kept apart so labelling stays blind
  manifest.json      seed, quotas, counts and input checksums

Passages are chosen by this module's own broad word lists, not by the rules'
citations and not by the verifier, so the reading material does not reveal
either system's answer. `check` and `score` use only the standard library.
"""
import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "data" / "eval"
SEED = "careproof-eval-2026-11-21"
DATASET_VERSION = "data-2026-11-21-v2"
FIELDS = ("description", "capability", "procedure", "equipment", "specialties")
SERVICES = ("dialysis", "mri", "nicu", "c_section", "emergency_c_section")

LABELS = {
    "STATES_SERVICE": "The record says this facility provides the service.",
    "RELATED_ONLY": "Only a related specialty, department or piece of equipment is named.",
    "SAYS_UNAVAILABLE": "The record says the service is not provided here.",
    "REFERRED_ELSEWHERE": "The record sends patients to, or points at, another site for the service.",
    "PLANNED_OR_HISTORICAL": "The service is planned, or was provided in the past.",
    "CONFLICTING": "The record both states and denies the service.",
    "UNCLEAR": "The service is named but the text does not settle whether this facility provides it.",
    "NOT_MENTIONED": "Nothing in the record concerns the service.",
}
# Coarser groups, for agreement that does not hinge on vocabulary differences between systems.
GROUPS = {
    "STATES_SERVICE": "POSITIVE", "SAYS_UNAVAILABLE": "NOT_HERE", "REFERRED_ELSEWHERE": "NOT_HERE",
    "PLANNED_OR_HISTORICAL": "PLANNED", "RELATED_ONLY": "NO_CLAIM", "NOT_MENTIONED": "NO_CLAIM",
    "UNCLEAR": "UNCLEAR", "CONFLICTING": "UNCLEAR",
}
# Other vocabularies a prediction file may use: the verifier's labels and the rules' evidence_status.
ALIASES = {
    "EXPLICIT_POSITIVE": "STATES_SERVICE", "INDIRECT": "RELATED_ONLY", "EXPLICIT_NEGATIVE": "SAYS_UNAVAILABLE",
    "AMBIGUOUS": "UNCLEAR", "NO_EVIDENCE": "NOT_MENTIONED", "PROVIDED_ELSEWHERE": "REFERRED_ELSEWHERE",
    "DOCUMENTED": "STATES_SERVICE", "SUGGESTIVE": "RELATED_ONLY", "NEGATED": "SAYS_UNAVAILABLE",
    "REFERRAL_ONLY": "REFERRED_ELSEWHERE", "PLANNED": "PLANNED_OR_HISTORICAL", "MIXED": "CONFLICTING",
    "REVIEW": "UNCLEAR",
}

_C = r"c[-\s]?sections?|ca?esar[ei]an|\blscs\b"
_BIRTH = r"obstet|matern|deliver|labou?r|gyna?ec|birth|pregnan"
# (words that name the service, wider words that only decide what else is worth reading)
_WORDS = {
    "dialysis": (r"dialy|ha?emodial|\bcrrt\b|renal replacement", r"renal|kidney|nephro"),
    "mri": (r"\bmri\b|magnetic resonance", r"radiolog|imaging|\bscan"),
    "nicu": (r"\bnicu\b|\bsncu\b|(?:neonatal|newborn|new born) (?:intensive|icu)|special newborn care",
             r"neonat|newborn|new born|premature|preterm|\bpicu\b"),
    "c_section": (_C, _BIRTH),
    "emergency_c_section": (_C, _BIRTH),
}
PATTERNS = {s: (re.compile(core, re.I), re.compile(wide, re.I)) for s, (core, wide) in _WORDS.items()}
MAX_CORE, MAX_WIDE = 12, 6

# stratum -> pairs per service. The rare outcomes are pooled across services instead.
QUOTAS = {"eligible": 9, "held": 5, "weak_mention": 4, "equipment_only": 3, "specialty_only": 2, "undetected": 3}
POOLED = {"negated": 4, "planned": 8, "referral_only": 8, "mixed": 8, "review": 12}


def items(raw):
    """Decode a raw field into (item_index, text) pairs; a plain string has index None."""
    if not isinstance(raw, str):
        return []
    text = raw.strip()
    if not text or text.casefold() in {"null", "none", "nan"}:
        return []
    if text.startswith(("[", '"', "{")):
        try:
            raw = json.loads(text)
        except ValueError:
            return []
    if isinstance(raw, list):
        return [(i, v) for i, v in enumerate(raw) if isinstance(v, str) and v.strip()]
    return [(None, raw)] if isinstance(raw, str) else []


def passages(record, service):
    """Return (passages, omitted): whole field items worth reading for this service, naming ones first."""
    core, wide = PATTERNS[service]
    named, related = [], []
    for field in FIELDS:
        for index, text in items(record.get(field)):
            bucket = named if core.search(text) else related if wide.search(text) else None
            if bucket is not None:
                bucket.append({"field": field, "item_index": index, "text": text})
    chosen = named[:MAX_CORE] + related[:MAX_WIDE]
    return chosen, len(named) + len(related) - len(chosen)


def _order(*parts):
    return hashlib.sha256("|".join((SEED,) + parts).encode("utf-8")).hexdigest()


def stratum(row):
    """Name the sampling stratum of one service_evidence row."""
    status = row["evidence_status"]
    if status == "documented":
        return "eligible" if row["eligible"] else "held"
    if status != "suggestive":
        return status
    kinds = {c["kind"] for c in row["citations"]}
    if "indirect" in kinds:
        return "weak_mention"
    return "equipment_only" if "equipment" in kinds else "specialty_only"


def _development_keys():
    """Records already used to develop the rules or shown as examples; they cannot be held out."""
    keys = set()
    for path, column in ((ROOT / "handoff/a2/sample_index.csv", "record_key"),
                         (ROOT / "data/quality/service_claim_review_2026-11-21.csv", "record_key"),
                         (ROOT / "data/quality/review_holds.csv", "reviewed_record_key")):
        with path.open(newline="", encoding="utf-8") as source:
            keys.update(row[column] for row in csv.DictReader(source))
    return keys


def sample(evidence, facilities, exclude=()):
    """Pick pairs by hash order within each stratum, at most one pair per record.

    `evidence` is service_evidence rows, `facilities` maps record_key to its row.
    Returns [(stratum, evidence_row_or_None, record, service)].
    """
    taken, picked = set(exclude), []

    def take(candidates, limit, name):
        count = 0
        for key, service, row in sorted(candidates, key=lambda c: _order(c[0], c[1])):
            if count == limit:
                break
            if key in taken or key not in facilities:
                continue
            taken.add(key)
            picked.append((name, row, facilities[key], service))
            count += 1

    by_stratum = defaultdict(list)
    for row in evidence:
        by_stratum[stratum(row)].append((row["record_key"], row["service"], row))
    for name, limit in POOLED.items():  # rare outcomes first, before a record is used elsewhere
        take(by_stratum[name], limit, name)
    for service in SERVICES:
        for name, limit in QUOTAS.items():
            if name != "undetected":
                take([c for c in by_stratum[name] if c[1] == service], limit, name)
    detected = {(row["record_key"], row["service"]) for row in evidence}
    for service in SERVICES:  # pairs the rules never listed although something related is in the text
        missed = [(key, service, None) for key, record in facilities.items()
                  if (key, service) not in detected and passages(record, service)[0]]
        take(missed, QUOTAS["undetected"], "undetected")
    return picked


def build(derived=ROOT / "data" / "derived", out=EVAL_DIR):
    import pyarrow.parquet as pq

    evidence = pq.read_table(derived / "service_evidence.parquet").to_pylist()
    columns = ["record_key", "unique_id", "name", "address_city", "address_stateOrRegion", *FIELDS]
    facilities = {r["record_key"]: r for r in pq.read_table(derived / "clean_facilities.parquet",
                                                            columns=columns).to_pylist()}
    picked = sample(evidence, facilities, _development_keys())
    picked.sort(key=lambda p: _order("id", p[2]["record_key"], p[3]))  # shuffle so order hides the stratum
    out.mkdir(parents=True, exist_ok=True)
    pairs, outcomes = [], []
    for number, (name, row, record, service) in enumerate(picked, 1):
        pair_id = "E{:03d}".format(number)
        chosen, omitted = passages(record, service)
        pairs.append({"pair_id": pair_id, "record_key": record["record_key"], "unique_id": record["unique_id"],
                      "service": service, "name": record["name"], "city": record["address_city"],
                      "state": record["address_stateOrRegion"], "passages": chosen, "passages_omitted": omitted})
        flags = [f for f in (row["flags"] if row else [])]
        outcomes.append({"pair_id": pair_id, "record_key": record["record_key"], "service": service,
                         "stratum": name, "evidence_status": row["evidence_status"] if row else "",
                         "eligible": str(bool(row and row["eligible"])).lower(), "flags": " ".join(flags)})
    with (out / "pairs.jsonl").open("w", encoding="utf-8") as target:
        for pair in pairs:
            target.write(json.dumps(pair, ensure_ascii=False) + "\n")
    with (out / "rule_outcomes.csv").open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=list(outcomes[0]))
        writer.writeheader()
        writer.writerows(outcomes)
    manifest = {
        "seed": SEED, "dataset_version": DATASET_VERSION, "pairs": len(pairs),
        "by_stratum": dict(sorted(Counter(o["stratum"] for o in outcomes).items())),
        "by_service": dict(sorted(Counter(o["service"] for o in outcomes).items())),
        "quotas_per_service": QUOTAS, "quotas_pooled": POOLED,
        "inputs_sha256": {name: hashlib.sha256((derived / name).read_bytes()).hexdigest()
                          for name in ("service_evidence.parquet", "clean_facilities.parquet")},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_pairs(directory=EVAL_DIR):
    with (directory / "pairs.jsonl").open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def load_labels(directory=EVAL_DIR):
    with (directory / "labels.csv").open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def gold(row):
    """A corrected human label wins over the draft."""
    checked = row.get("human_checked", "").strip().lower() in {"yes", "y", "true", "1"}
    return (row.get("human_label", "").strip() if checked else "") or row["draft_label"].strip()


def check(pairs, labels):
    """Return a list of problems in the label sheet; empty means it is usable."""
    problems, by_id = [], {p["pair_id"]: p for p in pairs}
    seen = Counter(row["pair_id"] for row in labels)
    problems += ["{}: no label row".format(i) for i in by_id if i not in seen]
    problems += ["{}: {} label rows".format(i, n) for i, n in seen.items() if n > 1]
    for row in labels:
        pair = by_id.get(row["pair_id"])
        if pair is None:
            problems.append("{}: not in pairs.jsonl".format(row["pair_id"]))
            continue
        if (row["record_key"], row["service"]) != (pair["record_key"], pair["service"]):
            problems.append("{}: record_key or service differs from pairs.jsonl".format(row["pair_id"]))
        for column in ("draft_label", "human_label"):
            if row.get(column, "").strip() and row[column].strip() not in LABELS:
                problems.append("{}: unknown {} {!r}".format(row["pair_id"], column, row[column]))
        if not row["draft_label"].strip():
            problems.append("{}: no draft label".format(row["pair_id"]))
        quote = row.get("quote", "")
        if not quote:
            if gold(row) != "NOT_MENTIONED":
                problems.append("{}: {} needs a supporting quote".format(row["pair_id"], gold(row)))
            continue
        index = row.get("quote_item_index", "").strip()
        index = int(index) if index else None
        texts = [p["text"] for p in pair["passages"]
                 if p["field"] == row.get("quote_field") and p["item_index"] == index]
        if not any(quote in text for text in texts):
            problems.append("{}: quote is not verbatim in {}[{}]".format(
                row["pair_id"], row.get("quote_field"), "" if index is None else index))
    return problems


def _label_of(row):
    verifier = row.get("verifier")
    if isinstance(verifier, str) and verifier.strip().startswith("{"):
        verifier = json.loads(verifier)
    for value in ((verifier or {}).get("label") if isinstance(verifier, dict) else None,
                  row.get("label"), row.get("predicted_label"), row.get("evidence_status")):
        if isinstance(value, str) and value.strip():
            name = value.strip().upper()
            name = ALIASES.get(name, name)
            if name not in LABELS:
                raise ValueError("unknown label {!r}".format(value))
            return name
    return None


def load_predictions(path, pairs):
    """Read CSV or JSONL predictions into {pair_id: label}.

    A row is matched by `pair_id`, or by `record_key` and `service`. Its label
    is read from `verifier.label` (a verifier row), `label`, `predicted_label`
    or `evidence_status`, in any of the three vocabularies. A row with no
    label, such as a pair the verifier did not check, is left out and counted
    as unanswered by `score`.
    """
    path = Path(path)
    with path.open(newline="", encoding="utf-8") as source:
        rows = (list(csv.DictReader(source)) if path.suffix.lower() == ".csv"
                else [json.loads(line) for line in source if line.strip()])
    ids = {(p["record_key"], p["service"]): p["pair_id"] for p in pairs}
    known, predictions = set(ids.values()), {}
    for row in rows:
        pair_id = row.get("pair_id") or ids.get((row.get("record_key"), row.get("service")))
        label = _label_of(row)
        if pair_id in known and label:
            predictions[pair_id] = label
    return predictions


def rule_predictions(directory=EVAL_DIR):
    """The rules' answer for every pair; a pair the rules never listed counts as NOT_MENTIONED."""
    with (directory / "rule_outcomes.csv").open(newline="", encoding="utf-8") as source:
        return {row["pair_id"]: ALIASES.get(row["evidence_status"].upper(), "NOT_MENTIONED")
                for row in csv.DictReader(source)}


def _agreement(golds, predictions, names):
    per = {}
    for name in names:
        hit = sum(g == p == name for g, p in zip(golds, predictions))
        said, actual = sum(p == name for p in predictions), sum(g == name for g in golds)
        if said or actual:
            per[name] = {"support": actual, "predicted": said,
                         "precision": round(hit / said, 3) if said else None,
                         "recall": round(hit / actual, 3) if actual else None}
    total = len(golds)
    return {"accuracy": round(sum(g == p for g, p in zip(golds, predictions)) / total, 3) if total else None,
            "per_label": per}


HUMAN_ONLY_NOTE = ("Only the human-checked pairs are scored. They were chosen for review because the draft was unsure "
                   "or the text rules disagreed with it, so they are harder than the full set and are not a fair "
                   "sample for the rules.")


def _checked(row):
    return row.get("human_checked", "").strip().lower() in {"yes", "y", "true", "1"}


def score(labels, predictions, human_only=False):
    """Compare predictions with the gold labels. `human_only` keeps just the pairs a person has checked.

    Reports exact-label agreement, agreement on the coarser groups, agreement
    per service, and how often a predicted "states the service" is right. A
    pair with no prediction is reported as unanswered and left out of the rates.
    """
    if human_only:
        labels = [row for row in labels if _checked(row)]
    rows = [(row, predictions[row["pair_id"]]) for row in labels if row["pair_id"] in predictions]
    golds, said = [gold(row) for row, _ in rows], [p for _, p in rows]
    result = {
        "pairs": len(labels), "answered": len(rows), "unanswered": len(labels) - len(rows),
        "human_checked": sum(_checked(row) for row in labels), "human_only": human_only,
        "exact": _agreement(golds, said, list(LABELS)),
        "grouped": _agreement([GROUPS[g] for g in golds], [GROUPS[p] for p in said],
                              sorted(set(GROUPS.values()))),
        "per_service": {},
    }
    for service in SERVICES:
        chosen = [(g, p) for (row, _), g, p in zip(rows, golds, said) if row["service"] == service]
        if chosen:
            positive = [(g, p) for g, p in chosen if p == "STATES_SERVICE"]
            result["per_service"][service] = {
                "answered": len(chosen),
                "exact_accuracy": round(sum(g == p for g, p in chosen) / len(chosen), 3),
                "grouped_accuracy": round(sum(GROUPS[g] == GROUPS[p] for g, p in chosen) / len(chosen), 3),
                "positive_precision": (round(sum(g == p for g, p in positive) / len(positive), 3)
                                       if positive else None),
            }
    return result


def render(result):
    lines = ["{answered} of {pairs} pairs answered, {unanswered} unanswered; "
             "{human_checked} gold labels human-checked, the rest are drafts".format(**result)]
    if result.get("human_only"):
        lines.append(HUMAN_ONLY_NOTE)
    for title in ("exact", "grouped"):
        block = result[title]
        lines.append("\n{} labels: accuracy {}".format(title.capitalize(), block["accuracy"]))
        lines.append("  {:<24}{:>8}{:>10}{:>10}{:>8}".format("label", "gold", "predicted", "precision", "recall"))
        for name, m in block["per_label"].items():
            lines.append("  {:<24}{:>8}{:>10}{:>10}{:>8}".format(
                name, m["support"], m["predicted"],
                "-" if m["precision"] is None else m["precision"], "-" if m["recall"] is None else m["recall"]))
    lines.append("\nPer service")
    lines.append("  {:<22}{:>9}{:>8}{:>9}{:>20}".format("service", "answered", "exact", "grouped", "positive precision"))
    for service, m in result["per_service"].items():
        lines.append("  {:<22}{:>9}{:>8}{:>9}{:>20}".format(
            service, m["answered"], m["exact_accuracy"], m["grouped_accuracy"],
            "-" if m["positive_precision"] is None else m["positive_precision"]))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="sample pairs and write pairs.jsonl, rule_outcomes.csv, manifest.json")
    commands.add_parser("check", help="validate labels.csv against pairs.jsonl")
    scorer = commands.add_parser("score", help="score predictions against the labels")
    source = scorer.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path, help="CSV or JSONL of predictions or verifier rows")
    source.add_argument("--rules", action="store_true", help="score the rule outcomes kept in rule_outcomes.csv")
    scorer.add_argument("--human-only", action="store_true",
                        help="score only the pairs a person has checked (a hard, hand-picked subset)")
    scorer.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "build":
        print(json.dumps(build(), indent=2))
        return 0
    pairs, labels = load_pairs(), load_labels()
    problems = check(pairs, labels)
    if args.command == "check" or problems:
        print("\n".join(problems) if problems else "labels.csv is consistent: {} pairs".format(len(labels)))
        return 1 if problems else 0
    result = score(labels, rule_predictions() if args.rules else load_predictions(args.predictions, pairs),
                   human_only=args.human_only)
    if args.human_only:
        result["note"] = HUMAN_ONLY_NOTE
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
