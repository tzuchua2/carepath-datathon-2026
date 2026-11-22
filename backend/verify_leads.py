"""Read rule-rejected pairs with the verifier to surface leads the rules missed.

    uv run --python 3.11 --with pyarrow python -m backend.verify_leads \
        --release data/revisions/v3 --out data/verifier_runs/v3_model_found_leads.jsonl \
        --cache data/verifier_runs/v3_leads_replies_deepseek_flash.jsonl --skip-held --workers 4 --live

A pair is read when the rules did not accept it and at least one passage of
the record names the service (the verifier's "named" scope). Where the model
returns an explicit positive with a quote that passes the verifier's checks,
the pair is written to `--out` with outcome
`model_found_statement_rules_missed`. Every row written has `eligible` false:
these are leads for a person to verify and are never recommendations.

Pairs the rules read as only suggestive run first. Pairs the rules read as a
statement but held for identity, location or source reasons run last, or not
at all with `--skip-held`, because they stay held whatever the model says.
`--log` keeps one short line for every pair read, including those where the
model found nothing. Without `--live`, stored replies are replayed and pairs
with no stored reply are counted as not read. The API key is read from the
environment by `verifier.openai_compatible_model` and is never printed or
written.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from backend import verifier as V

LEAD = "model_found_statement_rules_missed"
_ORDER = {"suggestive": 0, "documented": 2}  # every other rule status runs between these two


def select_pairs(evidence_rows, records, services=None, skip_held=False):
    """Return the rule rows to read, in run order: suggestive first, documented-but-held last."""
    chosen = []
    for row in evidence_rows:
        record = records.get(row["record_key"])
        if record is None or row.get("eligible") or (services and row["service"] not in services):
            continue
        if skip_held and row.get("evidence_status") == "documented":
            continue
        if V.triage(record, row["service"], row, "named") is None:
            chosen.append(row)
    chosen.sort(key=lambda r: (_ORDER.get(r.get("evidence_status"), 1), r["service"], r["record_key"]))
    return chosen


def read_pair(record, rule_row, model):
    """Return the verifier's row for one pair, never eligible."""
    row = V.verify_record(record, rule_row["service"], model, baseline=rule_row, scope="named")
    row["eligible"] = False
    return row


def lead_row(row):
    """Trim a lead to what a listing needs: passages with no evidence are dropped."""
    v = dict(row["verifier"])
    v["findings"] = [f for f in v["findings"] if f["effective_classification"] != "NO_EVIDENCE"]
    v["lead_quotes"] = [{"field": f["field"], "item_index": f["item_index"], "quote": f["exact_quote"],
                         "model_explanation": f["model_explanation"]}
                        for f in v["findings"] if f["effective_classification"] == "EXPLICIT_POSITIVE"]
    return dict(row, eligible=False, verifier=v)


def run(pairs, records, model, workers=1):
    """Read every pair. Return (lead rows, one log line per pair, summary)."""
    def one(rule_row):
        return rule_row, read_pair(records[rule_row["record_key"]], rule_row, model)

    started = time.perf_counter()
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(one, pairs))
    else:
        done = [one(p) for p in pairs]
    leads, log = [], []
    for rule_row, row in done:
        v = row["verifier"]
        is_lead = v["outcome"] == LEAD and v["status"] == "classified"
        if is_lead:
            leads.append(lead_row(row))
        log.append({"record_key": row["record_key"], "service": row["service"],
                    "rule_status": rule_row.get("evidence_status"), "status": v["status"], "label": v["label"],
                    "outcome": v["outcome"], "lead": is_lead, "blockers": v["blockers"],
                    "validation": v["receipt"]["validation"]})
    read = [line for line in log if line["status"] in ("classified", "invalid_output")]
    summary = {
        "pairs": len(pairs), "pairs_read": len(read),
        "not_read_no_stored_reply_or_outage": sum(line["status"] == "model_unavailable" for line in log),
        "invalid_replies": sum(line["status"] == "invalid_output" for line in log),
        "statements_found": len(leads),
        "statements_found_by_service": dict(Counter(r["service"] for r in leads)),
        "statements_found_by_rule_status": dict(Counter(line["rule_status"] for line in log if line["lead"])),
        "leads_with_a_hold": sum(bool(r["verifier"]["blockers"]) for r in leads),
        "model_label": dict(Counter(str(line["label"]) for line in log)),
        "prompt_version": V.PROMPT_VERSION, "guards_version": V.GUARDS_VERSION,
        "seconds": round(time.perf_counter() - started, 1),
    }
    return leads, log, summary


def _rows(path):
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--release", required=True, help="folder with clean_facilities and service_evidence parquet files")
    parser.add_argument("--out", required=True, help="leads only, one JSON row per line")
    parser.add_argument("--cache", required=True)
    parser.add_argument("--log", help="optional: one short JSON line per pair read")
    parser.add_argument("--workers", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--live", action="store_true", help="call the provider for pairs with no stored reply")
    parser.add_argument("--dry-run", action="store_true", help="print the counts and make no call")
    parser.add_argument("--limit", type=int, help="only the first N pairs in run order, for a pilot")
    parser.add_argument("--services", nargs="+", choices=sorted(V.SERVICES))
    parser.add_argument("--skip-held", action="store_true", help="leave out pairs the rules read as a statement but held")
    args = parser.parse_args(argv)

    evidence = [r for r in _rows(os.path.join(args.release, "service_evidence.parquet")) if not r["eligible"]]
    wanted = {r["record_key"] for r in evidence}
    records = {r["record_key"]: r for r in _rows(os.path.join(args.release, "clean_facilities.parquet"))
               if r["record_key"] in wanted}
    pairs = select_pairs(evidence, records, args.services, args.skip_held)[:args.limit]
    cache = V.ReplyCache(args.cache, V.openai_compatible_model() if args.live and not args.dry_run else None)
    if args.dry_run:
        need = 0
        for row in pairs:
            record = records[row["record_key"]]
            user = V.build_request(record, row["service"], V.select_passages(record, row["service"])[0])
            need += V.request_key(V.SYSTEM_PROMPT, user) not in cache._replies
        print(json.dumps({"pairs": len(pairs), "by_service": Counter(r["service"] for r in pairs),
                          "by_rule_status": Counter(r["evidence_status"] for r in pairs),
                          "stored_replies": len(cache), "pairs_needing_a_call": need}))
        return
    leads, log, summary = run(pairs, records, cache, args.workers)
    with open(args.out, "w", encoding="utf-8") as sink:
        for row in leads:
            sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    if args.log:
        with open(args.log, "w", encoding="utf-8") as sink:
            for line in log:
                sink.write(json.dumps(line, ensure_ascii=False) + "\n")
    summary.update(answered_from_cache=cache.hits, new_model_calls=cache.misses,
                   out_bytes=os.path.getsize(args.out))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
