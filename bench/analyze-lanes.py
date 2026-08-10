#!/usr/bin/env python3
"""Turn the raw lane JSON into the findings tables, with the gates applied.

Reports min AND median for every lane. Spread is (median - min) / min: how much
a typical rep exceeds the best rep, which is the jitter measure the >20% trigger
is about. Ratios are formed from MEDIANS of paired samples.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

LANES = [("A_cold", "A cold"), ("A_warm", "A warm"), ("B1_total", "B1 load+compute"),
         ("B2", "B2 compute-only"), ("C", "ClickHouse")]


def ms(x: float) -> float:
    return x * 1000.0


def summarize(path: Path) -> None:
    d = json.loads(path.read_text())
    scale = d["scale"]
    print(f"\n{'='*100}\nSCALE {scale}   n={d['reps']}   quiet baseline loadavg={d.get('quiet_baseline_loadavg')}")
    las = [g["loadavg"] for g in d.get("gate_log", [])]
    if las:
        print(f"loadavg across {len(las)} gate points: min={min(las):.2f} median={statistics.median(las):.2f} max={max(las):.2f}")

    print(f"\n{'op':26s} {'lane':16s} {'min(ms)':>10s} {'median(ms)':>11s} {'spread':>8s}")
    print("-" * 100)
    high_spread = []
    for op, rec in d["ops"].items():
        for key, label in LANES:
            if key not in rec or not rec[key]:
                continue
            vals = sorted(rec[key])
            mn, med = min(vals), statistics.median(vals)
            spread = (med - mn) / mn * 100 if mn > 0 else 0.0
            flag = " <<" if spread > 20 else ""
            if spread > 20:
                high_spread.append((op, label, spread))
            print(f"{op:26s} {label:16s} {ms(mn):10.2f} {ms(med):11.2f} {spread:7.1f}%{flag}")
        print()

    print("-" * 100)
    print("RATIOS (median of paired samples; A warm is the reference)")
    print(f"{'op':26s} {'B1/A':>10s} {'B2/A':>10s} {'C/A':>10s}")
    for op, rec in d["ops"].items():
        a = statistics.median(rec["A_warm"])
        b1 = statistics.median(rec["B1_total"]) / a
        b2 = statistics.median(rec["B2"]) / a
        c = statistics.median(rec["C"]) / a
        print(f"{op:26s} {b1:9.1f}x {b2:9.2f}x {c:9.2f}x")

    print("-" * 100)
    print("ASYMMETRY GATE — SUM/MAX must be ~flat cold->warm in lane A.")
    print("A large cold->warm delta on these ops cannot be a decode-cache win:")
    print("they decode almost nothing. It would have to be attributed elsewhere.")
    for op in ("SUM(amount)", "MAX(account_id)"):
        rec = d["ops"].get(op)
        if not rec or not rec.get("A_cold"):
            continue
        cold = statistics.median(rec["A_cold"])
        warm = statistics.median(rec["A_warm"])
        ratio = cold / warm
        verdict = "FLAT (gate holds)" if ratio < 1.5 else "NOT FLAT — must attribute, not claim"
        print(f"  {op:22s} cold={ms(cold):8.2f}ms warm={ms(warm):8.2f}ms  cold/warm={ratio:5.2f}x  {verdict}")

    if high_spread:
        print("-" * 100)
        print(f"SPREAD TRIGGER: {len(high_spread)} lane/op combinations exceed 20%")
        for op, lane, sp in high_spread:
            print(f"  {op:26s} {lane:16s} {sp:6.1f}%")


for p in sys.argv[1:]:
    summarize(Path(p))
