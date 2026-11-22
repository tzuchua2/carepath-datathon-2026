"""How far each district is from the nearest facility whose record documents a service.

For every PIN area with a usable point, measure the straight-line distance to the
nearest facility that is recommendable for a service, then summarise by district
and attach a few district survey (NFHS-5) figures for context.

This measures documentation in the challenge dataset. A district that looks far
from care here may have providers that are missing from the dataset or whose
records did not pass the evidence gates. Read it as "where our evidence is thin",
not as proof that care is absent.

Build:  python -m backend.access_gaps            (needs numpy, pandas, pyarrow)
Output: data/analytics/access_gaps.json, read by CareIndex.from_release.
"""

import json
from datetime import date
from pathlib import Path

from backend.service_evidence import SERVICES

OUT = "data/analytics/access_gaps.json"
SERVICES_COVERED = ("dialysis", "nicu", "mri", "c_section")  # one record states emergency C-section: too few to map
THRESHOLDS_KM = (50, 100)
# District survey figures shown next to each service. They describe need or use, never facility quality.
CONTEXT = {
    "dialysis": {"w15_plus_with_high_or_very_high_gt_140_mg_dl_blood_sugar_or_pct":
                 "Women 15+ with high blood sugar or on medication (%)",
                 "hh_member_covered_health_insurance_pct": "Households with a member covered by health insurance (%)"},
    "nicu": {"institutional_birth_5y_pct": "Births in a health facility (%)",
             "mothers_who_had_at_least_4_anc_visits_lb5y_pct": "Mothers with at least four antenatal visits (%)"},
    "c_section": {"births_delivered_by_csection_5y_pct": "Births delivered by C-section (%)",
                  "institutional_birth_5y_pct": "Births in a health facility (%)"},
    "mri": {"hh_member_covered_health_insurance_pct": "Households with a member covered by health insurance (%)"},
}


def build(index, postal, indicators, as_of=None):
    """Return the access-gap summary. `index` is a CareIndex; `postal` and `indicators` are pandas frames."""
    import numpy as np

    pins = postal[postal["pin_latitude"].notna() & postal["district_key"].notna() & postal["state_key"].notna()]
    plat, plon = np.radians(pins["pin_latitude"].to_numpy()), np.radians(pins["pin_longitude"].to_numpy())
    wanted = sorted({k for labels in CONTEXT.values() for k in labels})
    survey = indicators[indicators["indicator"].isin(wanted) & indicators["value"].notna()]
    survey = {(r.state_key, r.district_key, r.indicator): float(r.value) for r in survey.itertuples()}

    result = {"generated_on": str(as_of or date.today()), "index_version": index.index_version,
              "unit": "straight-line km from the centre of each PIN area to the nearest recommendable facility",
              "thresholds_km": list(THRESHOLDS_KM), "pin_areas_measured": int(len(pins)),
              "caveat": "These distances describe the challenge dataset. A long distance can mean that care is far "
                        "away, or that nearer providers are missing from the dataset or lack a documented claim.",
              "survey_source": "NFHS-5 district fact sheets, as supplied with the challenge",
              "services": {}}
    for service in SERVICES_COVERED:
        rows = [index.facilities[r["record_key"]] for r in index.evidence[service] if r["eligible"]]
        rows = [f for f in rows if f.get("usable_latitude") is not None]
        if not rows:
            continue
        flat = np.radians(np.array([f["usable_latitude"] for f in rows]))
        flon = np.radians(np.array([f["usable_longitude"] for f in rows]))
        a = (np.sin((flat[None, :] - plat[:, None]) / 2) ** 2
             + np.cos(plat[:, None]) * np.cos(flat[None, :]) * np.sin((flon[None, :] - plon[:, None]) / 2) ** 2)
        km = 6371.0088 * 2 * np.arcsin(np.minimum(1.0, np.sqrt(a)))
        nearest, nearest_km = km.argmin(axis=1), km.min(axis=1)

        frame = pins[["state_key", "district_key", "pin_latitude", "pin_longitude"]].copy()
        frame["km"], frame["nearest"] = nearest_km, nearest
        districts = []
        for (state, district), group in frame.groupby(["state_key", "district_key"], sort=True):
            d = group["km"].to_numpy()
            closest = group.loc[group["km"].idxmin()]
            entry = {"state": state, "district": district, "pin_areas": int(len(group)),
                     "latitude": round(float(group["pin_latitude"].median()), 4),
                     "longitude": round(float(group["pin_longitude"].median()), 4),
                     "median_km": round(float(np.median(d)), 1), "p90_km": round(float(np.percentile(d, 90)), 1),
                     "nearest_facility": rows[int(closest["nearest"])].get("name"),
                     "nearest_facility_km": round(float(closest["km"]), 1)}
            for t in THRESHOLDS_KM:
                entry[f"share_over_{t}_km"] = round(float((d > t).mean()), 3)
            context = {label: survey[(state, district, key)] for key, label in CONTEXT[service].items()
                       if (state, district, key) in survey}
            if context:
                entry["survey"] = context
            share = survey.get((state, district, "births_delivered_by_csection_5y_pct"))
            if service == "c_section" and share is not None and share >= 10 and entry["median_km"] > 100:
                # The survey shows C-sections are common here, yet no documented provider is near: the dataset,
                # not the district, is missing something.
                entry["dataset_blind_spot"] = True
            districts.append(entry)
        districts.sort(key=lambda e: (-e["median_km"], e["state"], e["district"]))
        result["services"][service] = {
            "display_name": SERVICES[service], "recommendable_facilities": len(rows),
            "national": {"median_km": round(float(np.median(nearest_km)), 1),
                         "p90_km": round(float(np.percentile(nearest_km, 90)), 1),
                         **{f"share_over_{t}_km": round(float((nearest_km > t).mean()), 3) for t in THRESHOLDS_KM},
                         **{f"districts_with_median_over_{t}_km": sum(e["median_km"] > t for e in districts)
                            for t in THRESHOLDS_KM},
                         "districts_measured": len(districts),
                         "dataset_blind_spots": sum(bool(e.get("dataset_blind_spot")) for e in districts)},
            "districts": districts}
    return result


def main():
    import argparse
    import pandas as pd
    from backend.recommend import CareIndex

    parser = argparse.ArgumentParser(description="Summarise distance to the nearest documented provider by district.")
    parser.add_argument("--manifest", default="data/recovery/v3-manifest.json")
    parser.add_argument("--verified", default="data/verifier_runs/v3_verified_evidence.jsonl")
    parser.add_argument("--out", default=OUT)
    parser.add_argument("--as-of", default=str(date.today()))
    args = parser.parse_args()
    tables = json.loads(Path(args.manifest).read_text())
    verified = args.verified if Path(args.verified).exists() else None
    index = CareIndex.from_release(args.manifest, post_offices=False, verified=verified)
    result = build(index, pd.read_parquet(tables["postal_reference"]["path"]),
                   pd.read_parquet(tables["district_indicators"]["path"]), as_of=args.as_of)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    for service, block in result["services"].items():
        print(service, block["recommendable_facilities"], "facilities;", block["national"])
    print(out, out.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
