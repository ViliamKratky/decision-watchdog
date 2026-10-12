#!/usr/bin/env python3
"""Side-by-side metrics for several result directories.

Usage: python3 eval/compare.py eval/results/<run-a> eval/results/<run-b> ... [--part all|dev|held_out]
"""
from __future__ import annotations

import sys
from pathlib import Path

import score

COLUMNS = [
    ("scenario_precision", "scen.prec"),
    ("entry_precision", "entry.prec"),
    ("recall", "recall"),
    ("neg_fp_rate", "neg.FP"),
    ("type_accuracy", "type.acc"),
    ("must_capture_coverage", "facts"),
    ("must_not_violations", "viol"),
    ("dedup_failures", "dedup"),
    ("accepted_entries", "entries"),
    ("fabricated_entries", "fabric."),
    ("mean_conf_relevant", "conf.TP"),
    ("mean_conf_irrelevant", "conf.FP"),
    ("avg_latency_s", "lat.s"),
]


def main() -> int:
    part = "all"
    args = [a for a in sys.argv[1:]]
    if "--part" in args:
        i = args.index("--part")
        part = args[i + 1]
        del args[i : i + 2]
    header = f"{'run':<40}" + "".join(f"{label:>11}" for _, label in COLUMNS)
    print(f"[{part}]")
    print(header)
    for run in args:
        rep = score.score_dir(Path(run))
        m = rep[part]
        cells = "".join(f"{str(m.get(key)):>11}" for key, _ in COLUMNS)
        print(f"{rep['run'] + ('' if rep['judged'] else ' (auto)'):<40}{cells}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
