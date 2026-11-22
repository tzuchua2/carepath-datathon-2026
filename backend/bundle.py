"""Build a self-contained folder that runs CareBricks as a Databricks App.

    python -m backend.bundle                  # writes deploy/carebricks
    python -m backend.bundle --check          # fails if deploy/carebricks is out of date

A Databricks App is deployed from one folder, and a file over 10 MB is refused. The release's
`clean_facilities` and `place_lookup` tables are larger than that, so the bundle keeps only the columns
the engine loads and writes them again with zstd compression. Nothing is filtered out of the facility,
evidence or reference rows. Post-office and village names are left out unless `--post-offices` is given,
because that table would exceed the limit; cities, districts and PINs still resolve.

The folder holds the three runtime modules, the web page, the slim tables with a manifest in the release's
format, the public-source review, the model verifier's output when it exists, and `app.yaml`. It needs pandas and PyArrow only.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "carebricks"
MANIFEST = "data/recovery/v3-manifest.json"
REVIEWS = "references/ranked-service-audit/evidence.json"
VERIFIED = "data/verifier_runs/v3_verified_evidence.jsonl"  # optional; backend.api loads it when present
WORKSPACE_VERIFIED = "data/verifier_runs/v3_databricks_verified_evidence.jsonl"
EXPLAINER = "data/verifier_runs/how_we_verify.json"
# Optional enrichment the engine loads by itself when the file is present at the same relative path.
ENRICHMENT = ("data/enrichment/website_checks.jsonl", "data/enrichment/osm_matches.jsonl",
              "data/verifier_runs/v3_model_found_leads.jsonl", "data/analytics/access_gaps.json")
MODULES = ("backend/__init__.py", "backend/api.py", "backend/agent.py", "backend/evidence_summary.py",
           "backend/recommend.py", "backend/referral_note.py", "backend/service_evidence.py")
MAX_FILE_BYTES = 10 * 1024 * 1024  # Databricks Apps refuse larger files
APP_YAML = """\
# CareBricks as a Databricks App. The platform sets DATABRICKS_APP_PORT; backend.api reads it.
command:
  - python
  - -m
  - backend.api
  - --host
  - 0.0.0.0
  - --release
  - data/v3-manifest.json
  - --skip-post-offices
"""
GOOGLE_MAPS_ENV = """env:
  - name: GOOGLE_MAPS_API_KEY
    valueFrom: google-maps-browser-key
"""
REQUIREMENTS = "pandas>=2.0\npyarrow>=14\n"


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _tables(post_offices):
    """Table name -> (columns to keep or None for all, row filter or None)."""
    from backend.recommend import _FACILITY_COLUMNS, _PLACE_COLUMNS, _POSTAL_COLUMNS, _V3_FACILITY_COLUMNS
    return {
        "service_evidence": (None, None),
        "clean_facilities": (list(_FACILITY_COLUMNS + _V3_FACILITY_COLUMNS), None),
        "postal_reference": (list(_POSTAL_COLUMNS), None),
        "external_corroboration": (None, None),
        "external_service_matches": (None, None),
        "place_lookup": (list(_PLACE_COLUMNS), None if post_offices else [("place_type", "!=", "post_office")]),
    }


def source_files(root=ROOT, web="app"):
    """Bundle path -> source path for every file copied as it is."""
    files = {name: root / name for name in MODULES}
    files[REVIEWS] = root / REVIEWS
    for optional in ENRICHMENT:
        if (root / optional).is_file():
            files[optional] = root / optional
    if (root / WORKSPACE_VERIFIED).is_file():
        files[VERIFIED] = root / WORKSPACE_VERIFIED
    elif (root / VERIFIED).is_file():
        files[VERIFIED] = root / VERIFIED
        if (root / EXPLAINER).is_file():
            files[EXPLAINER] = root / EXPLAINER
    for path in sorted((root / "app").rglob("*")):
        if path.is_file() and path.name != ".DS_Store" and (web == "app" or "vendor" in path.parts):
            files[path.relative_to(root).as_posix()] = path
    if web == "frontend":
        for name in ("index.html", "design/tokens.css", "design/assets/brand/favicon.svg"):
            files[f"app/{name}"] = root / "frontend" / name
        for path in sorted((root / "frontend/src").glob("*")):
            if path.is_file() and path.suffix in {".js", ".css"}:
                files[f"app/src/{path.name}"] = path
    return files


def build(out=OUT, root=ROOT, post_offices=False, web="app", google_maps=False):
    import pyarrow.parquet as pq

    out, root = Path(out), Path(root)
    if out.exists():
        shutil.rmtree(out)
    release = json.loads((root / MANIFEST).read_text())
    manifest = {}
    for name, (columns, filters) in _tables(post_offices).items():
        source = root / release[name]["path"]
        if _sha256(source) != release[name]["sha256"]:
            raise SystemExit("{} does not match the release checksum".format(source))
        have = pq.ParquetFile(source).schema_arrow.names
        table = pq.read_table(source, columns=[c for c in columns if c in have] if columns else None, filters=filters)
        target = out / "data" / (name + ".parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, target, compression="zstd", compression_level=19)
        manifest[name] = {"path": "data/{}.parquet".format(name), "rows": table.num_rows, "sha256": _sha256(target),
                          "source_sha256": release[name]["sha256"]}
    (out / "data" / "v3-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for name, source in source_files(root, web).items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (out / "app.yaml").write_text(APP_YAML + (GOOGLE_MAPS_ENV if google_maps else ""))
    (out / "requirements.txt").write_text(REQUIREMENTS)
    return out


def problems(out=OUT, root=ROOT, web="app"):
    """Reasons the bundle cannot be deployed as it stands: stale copies, changed tables, oversized files."""
    out, found = Path(out), []
    if not (out / "app.yaml").is_file():
        return ["{} has not been built".format(out)]
    for name, source in source_files(root, web).items():
        if not (out / name).is_file() or (out / name).read_bytes() != source.read_bytes():
            found.append("{} differs from the repository copy; rebuild the bundle".format(name))
    release = json.loads((Path(root) / MANIFEST).read_text())
    for name, entry in json.loads((out / "data" / "v3-manifest.json").read_text()).items():
        if entry["source_sha256"] != release[name]["sha256"]:
            found.append("{} was built from a different release file".format(name))
        elif _sha256(out / entry["path"]) != entry["sha256"]:
            found.append("{} does not match its bundle checksum".format(entry["path"]))
    if (out / VERIFIED).is_file() and VERIFIED not in source_files(Path(root), web):
        found.append("{} is bundled but no longer in the repository".format(VERIFIED))
    for path in out.rglob("*"):
        if path.is_file() and path.stat().st_size > MAX_FILE_BYTES:
            found.append("{} is over 10 MB".format(path.relative_to(out)))
    return found


def main():
    parser = argparse.ArgumentParser(description="Build the CareBricks Databricks App folder.")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--post-offices", action="store_true", help="include village and post-office names")
    parser.add_argument("--web", choices=("app", "frontend"), default="app", help="web client to include")
    parser.add_argument("--google-maps", action="store_true", help="bind the google-maps-browser-key App secret resource")
    parser.add_argument("--check", action="store_true", help="only verify the existing folder")
    args = parser.parse_args()
    if not args.check:
        build(args.out, post_offices=args.post_offices, web=args.web, google_maps=args.google_maps)
    found = problems(args.out, web=args.web)
    if args.google_maps and (Path(args.out) / "app.yaml").read_text() != APP_YAML + GOOGLE_MAPS_ENV:
        found.append("app.yaml is missing the Google Maps resource binding")
    for line in found:
        print("PROBLEM:", line)
    files = [p for p in Path(args.out).rglob("*") if p.is_file()]
    print("{}: {} files, {:.1f} MB, largest {:.1f} MB".format(
        args.out, len(files), sum(p.stat().st_size for p in files) / 1e6,
        max((p.stat().st_size for p in files), default=0) / 1e6))
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
