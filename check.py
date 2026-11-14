#!/usr/bin/env python3
"""Summarize the runs of a repro output dir, and optionally fail if an OSD was marked down too long after its grace.

For each run, reads `run-N.json` (per-OSD time from killing the host until
the OSD was marked down), `run-N.log` (when the host was killed) and
`run-N-cluster.log` (the mon's `osd.N failed (... after X >= grace Y)` lines),
and reports per run the last mark-down time and the worst "excess", i.e. the
mark-down time minus that OSD's own grace as computed by the mon.

Example:
    check.py ./result
    check.py --max-excess 5 ./result
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys

FAILED_RE = re.compile(r"osd\.(\d+) failed \(.*\) \((\d+) reporters from different \S+ after ([0-9.]+) >= grace ([0-9.]+)\)")


def graces_after(cluster_log: str, kill_time: str) -> dict:
  graces = {}
  with open(cluster_log) as f:
    for line in f:
      if line[:19].replace("T", " ") < kill_time:
        continue
      m = FAILED_RE.search(line)
      if m:
        graces.setdefault(int(m.group(1)), float(m.group(4)))
  return graces


def main():
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("dir", help="Repro output directory.")
  parser.add_argument("--max-excess", dest="max_excess", type=float, default=None, help="Fail if any OSD was marked down more than this many seconds after its grace.")
  args = parser.parse_args()

  worst_overall = 0.0
  lasts = []
  for result_path in sorted(glob.glob(os.path.join(args.dir, "run-*.json")), key=lambda p: int(re.search(r"run-(\d+)\.json$", p).group(1))):
    base = result_path[:-len(".json")]
    result = json.load(open(result_path))
    kill_line = next(l for l in open(base + ".log") if "SIGSTOPped" in l)
    graces = graces_after(base + "-cluster.log", kill_line[:19])
    downs = {int(k): v for k, v in result["down_after_seconds"].items()}
    missing = [o for o, t in downs.items() if t is None]
    excess = {o: round(t - graces[o], 1) for o, t in downs.items() if (t is not None) and (o in graces)}
    worst = max(excess.values()) if excess else float("inf")
    worst_overall = max(worst_overall, worst if not missing else float("inf"))
    lasts.append(result["last"])
    print(f"{os.path.basename(base)}: last OSD marked down after {result['last']} s (median {statistics.median(t for t in downs.values() if t is not None)} s), worst excess over own grace {worst} s, not marked down: {missing}")
  print(f"runs: {len(lasts)}; last mark-down per run (s): {sorted(lasts)}; worst excess over grace: {worst_overall} s")
  if (args.max_excess is not None) and (worst_overall > args.max_excess):
    print(f"FAIL: an OSD was marked down {worst_overall} s after its grace (allowed: {args.max_excess} s)")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
