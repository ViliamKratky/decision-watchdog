#!/usr/bin/env python3
"""Score a results directory produced by run_eval.py.

Automatic scoring uses the regex labels in each scenario. A human (or agent)
review can override it with `<results-dir>.judgments.json` next to the results
directory:

    {
      "<scenario-id>": {
        "raw": [true, false],          # relevance of each raw model entry
        "facts": [true, true, false],  # must_capture coverage (accepted entries)
        "violations": ["..."],         # must_not_capture items actually violated
        "fabricated": 1,               # accepted entries with invented reasons/alternatives
        "note": "free text"
      }
    }

Usage: python3 eval/score.py eval/results/<run-dir> [--json]
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
THRESHOLD = 0.62


def text_of(entry: dict, full: bool = True) -> str:
    parts = [entry.get("title", ""), entry.get("summary", "")]
    if full:
        parts.append(entry.get("context", ""))
        for key in ("alternatives", "rationale", "tradeoffs"):
            parts.extend(entry.get(key) or [])
    return " ".join(str(p) for p in parts).lower()


def fact_hit(fact: dict, text: str) -> bool:
    return all(any(re.search(p, text, re.I) for p in group) for group in fact["patterns"])


def load_scenarios() -> dict[str, dict]:
    return {
        json.loads(p.read_text())["id"]: json.loads(p.read_text())
        for p in (EVAL_DIR / "scenarios").glob("*.json")
    }


def raw_entries(result: dict) -> tuple[bool, list[dict]]:
    out = result.get("filtered_output") or result.get("raw_output") or {}
    return bool(out.get("worth_recording")), [e for e in out.get("entries", []) or [] if isinstance(e, dict)]


def judge(scenario: dict, result: dict, judgment: dict | None) -> dict:
    labels = scenario["labels"]
    worth, raws = raw_entries(result)
    accepted = result.get("accepted", [])
    accepted_titles = [a.get("title", "").strip() for a in accepted]
    if judgment and "raw" in judgment:
        relevant = [bool(x) for x in judgment["raw"]]
        relevant += [False] * (len(raws) - len(relevant))
    else:
        relevant = []
        for e in raws:
            t = text_of(e)
            ok = labels["should_record"] and any(fact_hit(f, t) for f in labels["must_capture"])
            if ok and any(re.search(m["pattern"], text_of(e, full=False), re.I) for m in labels["must_not_capture"]):
                ok = False
            relevant.append(ok)
    is_accepted = [e.get("title", "").strip() in accepted_titles for e in raws]

    acc_text = " ".join(text_of(a) for a in accepted)
    if judgment and "facts" in judgment:
        facts = [bool(x) for x in judgment["facts"]]
    else:
        facts = [fact_hit(f, acc_text) for f in labels["must_capture"]]
    if judgment and "violations" in judgment:
        violations = list(judgment["violations"])
    else:
        violations = [
            m["item"] for m in labels["must_not_capture"]
            if any(re.search(m["pattern"], text_of(a, full=False), re.I) for a in accepted)
        ]
    rel_acc = [r for r, a in zip(relevant, is_accepted) if a]
    types_ok = [
        e.get("type") in labels["expected_types"]
        for e, r, a in zip(raws, relevant, is_accepted) if r and a
    ]
    non_english = labels.get("english_only") and bool(
        re.search(r"[áäčďéíĺľňóôŕšťúýž]", acc_text)
    )
    return {
        "id": scenario["id"],
        "category": scenario["category"],
        "held_out": scenario.get("held_out", False),
        "should_record": labels["should_record"],
        "recorded": len(accepted) > 0,
        "accepted": len(accepted),
        "accepted_relevant": sum(rel_acc),
        "raw_conf_rel": [float(e.get("confidence", 0)) for e, r in zip(raws, relevant) if r and worth],
        "raw_conf_irrel": [float(e.get("confidence", 0)) for e, r in zip(raws, relevant) if not r and worth],
        "raw": [(float(e.get("confidence", 0)), r) for e, r in zip(raws, relevant)] if worth else [],
        "types_ok": types_ok,
        "facts": facts,
        "violations": violations,
        "over_split": len(accepted) > max(labels.get("max_entries", 2), 0) if labels["should_record"] else False,
        "dedup_fail": scenario["category"] == "hard/dedup" and len(accepted) > 0,
        "non_english": bool(non_english),
        "latency": result.get("latency_s"),
        "error": result.get("error", ""),
        "fabricated": int((judgment or {}).get("fabricated", 0)),
        "accepted_titles": [f"[{a.get('type')} {a.get('confidence')}] {a.get('title')}" for a in accepted],
    }


def metrics(rows: list[dict]) -> dict:
    pos = [r for r in rows if r["should_record"]]
    neg = [r for r in rows if not r["should_record"]]
    recorded = [r for r in rows if r["recorded"]]
    tp_scen = [r for r in pos if r["accepted_relevant"] > 0]
    acc_total = sum(r["accepted"] for r in rows)
    acc_rel = sum(r["accepted_relevant"] for r in rows)
    types = [t for r in rows for t in r["types_ok"]]
    facts = [f for r in pos for f in r["facts"]]
    conf_rel = [c for r in rows for c in r["raw_conf_rel"]]
    conf_irr = [c for r in rows for c in r["raw_conf_irrel"]]
    lat = [r["latency"] for r in rows if r["latency"] is not None]
    mean = lambda xs: round(statistics.mean(xs), 3) if xs else None  # noqa: E731
    return {
        "n": len(rows),
        "positives": len(pos),
        "negatives": len(neg),
        "scenario_precision": round(sum(r["should_record"] for r in recorded) / len(recorded), 3) if recorded else None,
        "entry_precision": round(acc_rel / acc_total, 3) if acc_total else None,
        "recall": round(len(tp_scen) / len(pos), 3) if pos else None,
        "neg_fp_rate": round(sum(r["recorded"] for r in neg) / len(neg), 3) if neg else None,
        "type_accuracy": round(sum(types) / len(types), 3) if types else None,
        "must_capture_coverage": round(sum(facts) / len(facts), 3) if facts else None,
        "must_not_violations": sum(len(r["violations"]) for r in rows),
        "over_split": sum(r["over_split"] for r in rows),
        "dedup_failures": sum(r["dedup_fail"] for r in rows),
        "non_english": sum(r["non_english"] for r in rows),
        "accepted_entries": acc_total,
        "mean_conf_relevant": mean(conf_rel),
        "mean_conf_irrelevant": mean(conf_irr),
        "conf_irrelevant_ge_0.9": sum(c >= 0.9 for c in conf_irr),
        "n_conf_irrelevant": len(conf_irr),
        "avg_latency_s": mean(lat),
        "errors": sum(bool(r["error"]) for r in rows),
        "fabricated_entries": sum(r["fabricated"] for r in rows),
    }


def sweep(rows: list[dict]) -> list[dict]:
    out = []
    pos = [r for r in rows if r["should_record"]]
    neg = [r for r in rows if not r["should_record"]]
    for t in (0.5, 0.62, 0.7, 0.8, 0.85, 0.9, 0.95):
        rec = sum(any(c >= t and ok for c, ok in r["raw"]) for r in pos)
        fp = sum(any(c >= t for c, _ in r["raw"]) for r in neg)
        kept = [(c, ok) for r in rows for c, ok in r["raw"] if c >= t]
        out.append({
            "threshold": t,
            "recall": round(rec / len(pos), 3) if pos else None,
            "neg_fp_rate": round(fp / len(neg), 3) if neg else None,
            "entry_precision": round(sum(ok for _, ok in kept) / len(kept), 3) if kept else None,
        })
    return out


def score_dir(results_dir: Path) -> dict:
    results_dir = Path(results_dir)
    scenarios = load_scenarios()
    jpath = results_dir.parent / f"{results_dir.name}.judgments.json"
    judgments = json.loads(jpath.read_text()) if jpath.exists() else {}
    rows = []
    for path in sorted(results_dir.glob("*.json")):
        result = json.loads(path.read_text())
        sid = result["scenario"]
        if sid not in scenarios:
            continue
        rows.append(judge(scenarios[sid], result, judgments.get(sid)))
    dev = [r for r in rows if not r["held_out"]]
    held = [r for r in rows if r["held_out"]]
    return {
        "run": results_dir.name,
        "judged": bool(judgments),
        "all": metrics(rows),
        "dev": metrics(dev),
        "held_out": metrics(held),
        "sweep_all": sweep(rows),
        "rows": rows,
    }


def render(report: dict) -> str:
    lines = [f"== {report['run']}  (judgments: {'yes' if report['judged'] else 'auto only'})"]
    for r in report["rows"]:
        mark = "OK " if (r["recorded"] == r["should_record"] and (not r["should_record"] or r["accepted_relevant"])) else "BAD"
        lines.append(
            f"{mark} {r['id']:<36}{'H' if r['held_out'] else ' '} exp={'Y' if r['should_record'] else 'N'} "
            f"acc={r['accepted']} rel={r['accepted_relevant']} facts={sum(r['facts'])}/{len(r['facts'])} "
            f"viol={len(r['violations'])} {r['latency']}s"
        )
        for t in r["accepted_titles"]:
            lines.append(f"      {t}")
    for part in ("all", "dev", "held_out"):
        lines.append(f"-- {part}: " + json.dumps(report[part]))
    lines.append("-- threshold sweep (all): ")
    for s in report["sweep_all"]:
        lines.append("   " + json.dumps(s))
    return "\n".join(lines)


def dump(results_dir: Path) -> str:
    """Readable listing of every raw entry, for writing a judgments file."""
    scenarios = load_scenarios()
    lines = []
    for path in sorted(Path(results_dir).glob("*.json")):
        result = json.loads(path.read_text())
        s = scenarios[result["scenario"]]
        worth, raws = raw_entries(result)
        accepted = {a.get("title", "").strip() for a in result.get("accepted", [])}
        out = result.get("raw_output") or {}
        lines.append(f"===== {s['id']} exp={s['labels']['should_record']} types={s['labels']['expected_types']} worth={worth}")
        if out.get("assessment"):
            lines.append(f"  ASSESS: {out['assessment']}")
        for f in s["labels"]["must_capture"]:
            lines.append(f"  FACT: {f['fact']}")
        for i, e in enumerate(raws):
            acc = "ACC" if e.get("title", "").strip() in accepted else "drop"
            lines.append(f"  #{i} {acc} [{e.get('type')} {e.get('confidence')}] {e.get('title')}")
            lines.append(f"     S: {e.get('summary')}")
            lines.append(f"     ALT: {e.get('alternatives')}  WHY: {e.get('rationale')}")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--dump" in sys.argv:
        print(dump(Path(sys.argv[1])))
        raise SystemExit(0)
    rep = score_dir(Path(sys.argv[1]))
    print(json.dumps(rep, indent=2) if "--json" in sys.argv else render(rep))
