"""Run the Databricks transformations against the team's official Parquet snapshots."""

import argparse
from datetime import date
import json
from pathlib import Path

from pyspark.sql import SparkSession

from backend.fingerprints import snapshot_fingerprints
from backend.pipeline import build_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True, help="A new directory for this build")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    spark = (SparkSession.builder.master("local[2]").appName("CMU facility quality")
             .config("spark.ui.enabled", "false").config("spark.sql.shuffle.partitions", "4")
             .config("spark.sql.warehouse.dir", str(args.output.resolve() / "warehouse"))
             .getOrCreate())
    try:
        def save(name, frame):
            path = str(args.output.resolve() / name)
            frame.write.mode("errorifexists").parquet(path)
            print("Wrote", name, flush=True)
            return spark.read.parquet(path)

        def source(name):
            return spark.read.parquet(str((args.input / (name + ".parquet")).resolve()))

        metrics = build_snapshot(spark, source, save, args.as_of)
        fingerprints = snapshot_fingerprints(
            source, lambda name: spark.read.parquet(str(args.output.resolve() / name)))
        (args.output / "fingerprints.json").write_text(json.dumps(fingerprints, indent=2, sort_keys=True) + "\n")
        print(json.dumps(metrics, indent=2, sort_keys=True))
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
