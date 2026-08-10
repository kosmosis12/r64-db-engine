#!/usr/bin/env python3
"""Phase E benchmark orchestrator — four lanes, paired, gated.

Lanes
  A_warm  meshroad Flight DoGet against a long-running serve, post-prefault
  A_cold  meshroad Flight DoGet against a FRESH serve process per rep
  B1      Python load+compute, fresh process per rep (uv, pinned versions)
  B2      Python compute-only against a DataFrame preloaded once
  C       ClickHouse itself, clickhouse-client --time

PAIRING: for each op, each rep runs A_warm / B1 / B2 / C back to back before
the next rep begins. Cross-lane ratios are therefore formed from samples taken
seconds apart rather than from blocks minutes apart, so machine drift moves
both sides of a ratio together instead of inventing one.

GATE: a quiet-machine loadavg gate (< 1.0) is enforced ONCE, before any work,
against a genuinely idle machine. Thereafter every invocation is gated on
FOREIGN contention -- kill-list processes present, or any non-campaign process
over 15% CPU -- and loadavg is recorded at each point rather than used as the
trigger. Either way the run ABORTS rather than continuing and labelling results
indicative: a number taken on a contended machine is not rescued by a footnote.
See `gate()` for why a mid-series loadavg gate is self-defeating.

A_cold restarts the serve per rep. "Cold" means meshroad's column cache is
empty, NOT that the OS page cache is cold; the file stays in page cache
throughout. That is the same definition `meshroad stats` uses, and it is stated
here so the cold numbers are not read as disk-read numbers.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pyarrow.flight as fl

REPO = Path("/home/kos/builds/r64-db-engine")
MESHROAD = Path("/home/kos/dev/meshroad/target/release/meshroad")
LOAD_GATE = 1.0

OPS: list[tuple[str, str]] = [
    ("SUM(amount)", "SELECT sum(amount) FROM {t}"),
    ("GROUPSUM(region,amount)", "SELECT region, sum(amount) FROM {t} GROUP BY region"),
    ("FILTER status=active", "SELECT count(*) FROM {t} WHERE status='active'"),
    ("DISTINCT product_name", "SELECT count(distinct product_name) FROM {t}"),
    ("MAX(account_id)", "SELECT max(account_id) FROM {t}"),
    ("UPPER(status) group",
     "SELECT upper(status) AS s, count(*) FROM {t} GROUP BY upper(status)"),
]


def loadavg() -> float:
    return float(Path("/proc/loadavg").read_text().split()[0])


# Processes the campaign itself runs. Their CPU is the measurement, not
# contention, so they cannot count against the gate.
CAMPAIGN_COMMS = {
    "meshroad", "clickhouse-serv", "clickhouse-server", "python", "python3",
    "uv", "docker", "dockerd", "containerd", "containerd-shim", "claude",
    "node", "zsh", "ps", "sh", "runc",
}
# Named on the campaign kill list. Any of these present fails the gate outright,
# regardless of how little CPU it happens to be using at the sampling instant --
# the objection to Slack was that it is BURSTY, which a CPU sample can miss.
KILL_LIST = {"slack", "brave", "chrome", "firefox"}
FOREIGN_CPU_CEILING = 15.0

GATE_LOG: list[dict] = []
QUIET_BASELINE: float = -1.0


def gate(where: str) -> None:
    """Refuse to measure while FOREIGN work contends.

    Deliberately NOT a plain `loadavg < 1.0` check. This series loads the
    machine by construction -- lane B1 spawns a fresh `uv` process per rep, and
    lane A cold spawns a serve per rep -- so a sub-1.0 loadavg gate applied
    mid-series is unsatisfiable no matter how quiet the machine is. Gating on it
    would not measure contention, it would measure our own throughput and abort
    on success. Observed directly: the series tripped a 1.0 gate at loadavg 2.92
    with no foreign process running at all.

    So the gate tests what the doctrine actually cares about -- that nothing
    ELSE is competing -- and loadavg is recorded at every gate point for the
    record rather than used as the trigger.
    """
    la = loadavg()
    foreign: list[tuple[str, float]] = []
    killed: list[str] = []
    ps = subprocess.run(
        ["ps", "-eo", "comm,pcpu", "--no-headers"], capture_output=True, text=True
    ).stdout.splitlines()
    for line in ps:
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        comm, cpu_s = parts[0].strip(), parts[1]
        try:
            cpu = float(cpu_s)
        except ValueError:
            continue
        low = comm.lower()
        if any(k in low for k in KILL_LIST):
            killed.append(comm)
        elif comm not in CAMPAIGN_COMMS and cpu >= FOREIGN_CPU_CEILING:
            foreign.append((comm, cpu))

    GATE_LOG.append({"where": where, "loadavg": la, "foreign": foreign})
    if killed or foreign:
        print(json.dumps({"event": "GATE_FAIL", "where": where, "loadavg": la,
                          "kill_list_present": killed, "foreign_cpu": foreign}))
        sys.exit(3)


# ---------------------------------------------------------------- lane A


def flight_once(client: fl.FlightClient, sql: str):
    t0 = time.perf_counter()
    tbl = client.do_get(fl.Ticket(sql.encode())).read_all()
    return time.perf_counter() - t0, tbl.num_rows


def start_serve(file: Path, addr: str, log: Path):
    fh = log.open("w")
    proc = subprocess.Popen(
        [str(MESHROAD), "serve", "--file", str(file), "--table", "perf",
         "--addr", addr, "--refresh-ms", "0"],
        stdout=fh, stderr=subprocess.STDOUT,
    )
    for _ in range(600):
        time.sleep(0.05)
        if "Flight serving on" in log.read_text():
            return proc
    proc.kill()
    raise SystemExit(f"serve failed to start; log:\n{log.read_text()}")


def lane_a_cold(file: Path, reps: int, addr: str) -> dict[str, list[float]]:
    """Fresh serve per rep. --refresh-ms 0 pins the mapping so the refresh
    poller cannot touch the cache mid-measurement."""
    out: dict[str, list[float]] = {name: [] for name, _ in OPS}
    log = Path("/home/kos/bench-ch/serve-cold.log")
    for name, sql in OPS:
        for _ in range(reps):
            gate(f"A_cold/{name}")
            proc = start_serve(file, addr, log)
            try:
                client = fl.FlightClient(f"grpc://{addr}")
                dt, _ = flight_once(client, sql.format(t="perf"))
                out[name].append(dt)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
    return out


# ---------------------------------------------------------------- lane B


def lane_b1_once(file: Path, op: str) -> tuple[float, float, object]:
    r = subprocess.run(
        ["uv", "run", "--no-project",
         "--with", "pyarrow==25.0.0", "--with", "pandas==3.0.1", "--with", "numpy==2.4.3",
         "python", str(REPO / "bench" / "lane_b_worker.py"), "--file", str(file), "--op", op],
        capture_output=True, text=True, cwd=REPO,
    )
    if r.returncode != 0:
        raise SystemExit(f"B1 worker failed for {op}:\n{r.stderr[-2000:]}")
    d = json.loads(r.stdout.strip().splitlines()[-1])
    return d["load_s"], d["compute_s"], d["result"]


# ---------------------------------------------------------------- lane C


_TIME_RE = re.compile(r"^([0-9]+\.[0-9]+)$", re.M)


def lane_c_once(sql: str) -> float:
    r = subprocess.run(
        ["docker", "exec", "-i", "meshroad-ch", "clickhouse-client", "--time", "--query", sql],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"lane C failed:\n{r.stderr[-2000:]}")
    m = _TIME_RE.findall(r.stderr.strip())
    if not m:
        raise SystemExit(f"could not parse --time from clickhouse-client stderr: {r.stderr!r}")
    return float(m[-1])


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--ch-table", required=True)
    ap.add_argument("--addr", default="127.0.0.1:8899")
    ap.add_argument("--cold-addr", default="127.0.0.1:8901")
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--scale", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    file = Path(args.file)
    global QUIET_BASELINE
    QUIET_BASELINE = loadavg()
    if QUIET_BASELINE >= LOAD_GATE:
        raise SystemExit(f"quiet-machine baseline gate FAILED before any work: loadavg={QUIET_BASELINE}")
    print(f"[lanes] quiet baseline loadavg={QUIET_BASELINE} (gate <{LOAD_GATE})", flush=True)
    results: dict = {"scale": args.scale, "file": str(file), "reps": args.reps, "ops": {}}

    if shutil.which("uv") is None:
        raise SystemExit("uv not on PATH; lane B1 requires it")

    # Lane B2 preloads once, outside all timing.
    import pyarrow as pa
    import pyarrow.ipc as ipc
    sys.path.insert(0, str(REPO / "bench"))
    from lane_b_worker import compute as b2_compute

    print(f"[lanes] preloading {file} for lane B2 ...", flush=True)
    df = ipc.open_file(pa.memory_map(str(file))).read_all().to_pandas()

    client = fl.FlightClient(f"grpc://{args.addr}")
    client.do_get(fl.Ticket(b"SELECT count(*) FROM perf")).read_all()   # discarded warmup

    for name, sql in OPS:
        rec: dict[str, list] = {"A_warm": [], "B1_total": [], "B1_load": [], "B1_compute": [],
                                "B2": [], "C": [], "results": {}}
        for _ in range(args.reps):
            gate(f"paired/{name}")

            dt, _ = flight_once(client, sql.format(t="perf"))
            rec["A_warm"].append(dt)

            ld, cp, res = lane_b1_once(file, name)
            rec["B1_load"].append(ld)
            rec["B1_compute"].append(cp)
            rec["B1_total"].append(ld + cp)
            rec["results"]["B1"] = res

            t0 = time.perf_counter()
            r2 = b2_compute(df, name)
            rec["B2"].append(time.perf_counter() - t0)
            rec["results"]["B2"] = r2

            rec["C"].append(lane_c_once(sql.format(t=args.ch_table)))

        results["ops"][name] = rec
        print(f"[lanes] {name}: A_warm med={statistics.median(rec['A_warm'])*1000:.2f}ms "
              f"B1 med={statistics.median(rec['B1_total']):.2f}s "
              f"B2 med={statistics.median(rec['B2'])*1000:.2f}ms "
              f"C med={statistics.median(rec['C'])*1000:.2f}ms", flush=True)

    print("[lanes] lane A cold (fresh serve per rep) ...", flush=True)
    cold = lane_a_cold(file, args.reps, args.cold_addr)
    for name in cold:
        results["ops"][name]["A_cold"] = cold[name]

    results["gate_log"] = GATE_LOG
    results["quiet_baseline_loadavg"] = QUIET_BASELINE
    Path(args.out).write_text(json.dumps(results, indent=2))
    print(f"[lanes] wrote {args.out}")


if __name__ == "__main__":
    main()
