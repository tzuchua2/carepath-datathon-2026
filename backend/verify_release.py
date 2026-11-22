"""Run the verifier over a release's recommendation candidates.

    uv run --python 3.11 --with pyarrow python -m backend.verify_release \
        --release data/revisions/v3 --out data/verifier_runs/v3_databricks_verified_evidence.jsonl \
        --cache data/verifier_runs/v3_workspace_replies.jsonl --workers 1 --live

Each output row is the release's `service_evidence` row for the pair with the
verifier's decision applied, plus a `verifier` object (see `verifier.decide`).
Without `--live` no call is made: stored replies are replayed and every pair
with no stored reply keeps the rule result, marked `model_unavailable`.
`--dry-run` only counts what would be sent. The run can be stopped and started
again; replies already in the cache are not requested twice.

Live runs use the active Databricks workspace identity and a Databricks serving endpoint.
The live cache is namespaced by endpoint to isolate earlier providers.
"""
import argparse
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from backend import verifier as V
from backend.workspace_model import DEFAULT_ENDPOINT, workspace_model


def _rows(path):
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--release", required=True, help="folder with clean_facilities, service_evidence and recommendation_candidates parquet files")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--workers", type=int, default=1, choices=(1, 2, 3, 4))
    parser.add_argument("--live", action="store_true", help="call the provider for pairs with no stored reply")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="only the first N pairs, for a smoke test")
    args = parser.parse_args(argv)

    pairs = [(r["record_key"], r["service"]) for r in _rows(os.path.join(args.release, "recommendation_candidates.parquet"))]
    pairs = sorted(set(pairs))[:args.limit]
    wanted = {key for key, _ in pairs}
    records = {r["record_key"]: r for r in _rows(os.path.join(args.release, "clean_facilities.parquet")) if r["record_key"] in wanted}
    wanted_pairs = set(pairs)
    rules = {(r["record_key"], r["service"]): r for r in _rows(os.path.join(args.release, "service_evidence.parquet"))
             if (r["record_key"], r["service"]) in wanted_pairs}
    missing = [p for p in pairs if p[0] not in records or p not in rules]
    if missing:
        sys.exit("{} candidate pairs have no record or no service_evidence row, e.g. {}".format(len(missing), missing[:3]))

    model = workspace_model(args.endpoint) if args.live and not args.dry_run else None
    cache_path = args.cache + "." + args.endpoint if args.live else args.cache
    cache = V.ReplyCache(cache_path, model)
    if args.dry_run:
        need = sum(1 for key, service in pairs
                   if V.select_passages(records[key], service)[0]
                   and V.request_key(V.SYSTEM_PROMPT, V.build_request(records[key], service, V.select_passages(records[key], service)[0])) not in cache._replies)
        print(json.dumps({"pairs": len(pairs), "by_service": Counter(s for _, s in pairs),
                          "rule_eligible": sum(bool(rules[p]["eligible"]) for p in pairs),
                          "stored_replies": len(cache), "pairs_needing_a_call": need}))
        return

    def run(pair):
        row = V.verify_record(records[pair[0]], pair[1], cache, baseline=rules[pair], scope="all")
        return row

    started = time.perf_counter()
    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(run, pairs))
    else:
        rows = [run(p) for p in pairs]
    with open(args.out, "w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(json.dumps({
        "pairs": len(rows), "prompt_version": V.PROMPT_VERSION, "workers": args.workers,
        "status": Counter(r["verifier"]["status"] for r in rows),
        "validation": Counter(r["verifier"]["receipt"]["validation"] for r in rows),
        "outcome": Counter(r["verifier"]["outcome"] for r in rows),
        "label": Counter(r["verifier"]["label"] for r in rows),
        "eligible_before": sum(bool(r["verifier"]["rule_eligible"]) for r in rows),
        "eligible_after": sum(bool(r["eligible"]) for r in rows),
        "answered_from_cache": cache.hits, "new_model_calls": cache.misses,
        "seconds": round(time.perf_counter() - started, 1),
    }))


if __name__ == "__main__":
    main()
