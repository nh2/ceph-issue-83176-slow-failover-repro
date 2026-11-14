#!/usr/bin/env python3
"""Reproduce slow mark-down of a dead host's OSDs on a local Ceph test cluster.

Runs 1 mon plus `--hosts` x `--osds-per-host` memstore OSDs on 127.0.0.1,
with CRUSH locations `host=hN`, so that `mon_osd_reporter_subtree_level = host`
works as on a real cluster. Then it "kills" one host by sending SIGSTOP to all
its OSDs (they stop answering without closing their connections, like a
powered-off machine) and measures when the mon marks each of them down.

Runs 3 mons by default; with `--kill-mon`, mon `a` (the leader) is stopped
together with the victim host, like a dead host that also ran the leader mon.
With `--prime-laggy`, the victim host's OSDs first get differing laggy
histories (stopped until marked down, then resumed one by one), so that
`mon_osd_adjust_heartbeat_grace` gives them different graces, as after real
incidents.

Example (the setup that reproduces the bug):
    repro.py --ceph-bin /path/to/ceph/bin --run-dir /tmp/r --hosts 18 --osds-per-host 10 --pgs 2048 --settle-seconds 60 --prime-laggy --result-json result.json

Other uses:
    repro.py ... --mon-bin /path/to/patched/ceph-mon
    repro.py ... --mon-config mon_osd_adjust_heartbeat_grace=false
"""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass

log = logging.getLogger(__name__)

@dataclass
class Cluster:
  run_dir: str
  conf_path: str
  ceph_bin: str
  mon_bin: str
  osd_procs: dict  # osd id -> subprocess.Popen
  osd_host: dict  # osd id -> host name
  mon_procs: dict  # mon name -> subprocess.Popen
  mon_addrs: dict  # mon name -> "127.0.0.1:port"
  query_mons: list  # mon names that queries go to


def ceph(cluster_conf: str, ceph_bin: str, *args: str, timeout: float = 60, mon_host: str | None = None) -> str:
  mon_args = ["-m", mon_host] if mon_host else []
  result = subprocess.run(
    [os.path.join(ceph_bin, "ceph"), "--conf", cluster_conf, *mon_args, *args],
    check=True,
    capture_output=True,
    text=True,
    timeout=timeout,
  )
  return result.stdout


def write_conf(run_dir: str, fsid: str, mon_addrs: dict, mon_config: list[str]) -> str:
  conf_path = os.path.join(run_dir, "ceph.conf")
  extra = "\n".join(
    f"{key.strip()} = {value.strip()}"
    for key, value in (item.split("=", 1) for item in mon_config)
  )
  with open(conf_path, "w") as f:
    f.write(
      f"""[global]
fsid = {fsid}
mon_host = {",".join(f"v2:{a}" for a in mon_addrs.values())}
mon_cluster_log_file = {run_dir}/log/cluster.log
mon_cluster_log_level = debug
public_network = 127.0.0.0/8
auth_cluster_required = none
auth_service_required = none
auth_client_required = none
ms_bind_msgr1 = false
run_dir = {run_dir}/run
admin_socket = {run_dir}/run/$name.asok
pid_file = {run_dir}/run/$name.pid
log_file = {run_dir}/log/$name.log
mon_data = {run_dir}/data/$name
osd_data = {run_dir}/data/$name
osd_objectstore = memstore
memstore_device_bytes = 268435456
osd_pool_default_size = 3
osd_pool_default_min_size = 2
osd_pool_default_pg_autoscale_mode = off
mon_max_pg_per_osd = 1000
osd_crush_chooseleaf_type = 1
mon_allow_pool_delete = true
mon_warn_on_pool_no_redundancy = false
osd_max_markdown_count = 1000
{extra}
"""
    )
  return conf_path


def wait_for(predicate, timeout: float, what: str, interval: float = 0.5):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      if predicate():
        return
    except subprocess.SubprocessError:
      pass
    time.sleep(interval)
  raise TimeoutError(f"timed out waiting for {what}")


def query_mon_host(cluster: Cluster) -> str:
  return ",".join(f"v2:{cluster.mon_addrs[m]}" for m in cluster.query_mons)


def osd_states(cluster: Cluster) -> dict:
  dump = json.loads(ceph(cluster.conf_path, cluster.ceph_bin, "osd", "dump", "--format", "json", timeout=20, mon_host=query_mon_host(cluster)))
  return {o["osd"]: o["up"] for o in dump["osds"]}


def start_cluster(args) -> Cluster:
  run_dir = args.run_dir
  if os.path.exists(run_dir):
    shutil.rmtree(run_dir)
  for sub in ["run", "log", "data"]:
    os.makedirs(os.path.join(run_dir, sub))
  fsid = str(uuid.uuid4())
  mon_names = [chr(ord("a") + k) for k in range(args.mons)]
  mon_addrs = {m: f"127.0.0.1:{args.mon_port + k}" for k, m in enumerate(mon_names)}
  conf_path = write_conf(run_dir, fsid, mon_addrs, args.mon_config)
  ceph_bin = args.ceph_bin
  mon_bin = args.mon_bin or os.path.join(ceph_bin, "ceph-mon")

  monmap = os.path.join(run_dir, "monmap")
  monmaptool_args = [os.path.join(ceph_bin, "monmaptool"), "--create", "--fsid", fsid]
  for m, a in mon_addrs.items():
    monmaptool_args += ["--addv", m, f"[v2:{a}]"]
  subprocess.run([*monmaptool_args, monmap], check=True, capture_output=True)
  mon_procs = {}
  for m in mon_names:
    subprocess.run([mon_bin, "--conf", conf_path, "--mkfs", "-i", m, "--monmap", monmap], check=True, capture_output=True)
    mon_procs[m] = subprocess.Popen([mon_bin, "--conf", conf_path, "-f", "-i", m], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  wait_for(lambda: len(json.loads(ceph(conf_path, ceph_bin, "quorum_status", "--format", "json", timeout=10))["quorum"]) == len(mon_names), 120, "mon quorum")
  log.info(f"mons {mon_names} in quorum ({mon_bin})")

  osd_procs = {}
  osd_host = {}
  osd_id = 0
  for h in range(args.hosts):
    host = f"h{h}"
    for _ in range(args.osds_per_host):
      osd_uuid = str(uuid.uuid4())
      ceph(conf_path, ceph_bin, "osd", "new", osd_uuid, str(osd_id))
      os.makedirs(os.path.join(run_dir, "data", f"osd.{osd_id}"))
      subprocess.run(
        [os.path.join(ceph_bin, "ceph-osd"), "--conf", conf_path, "-i", str(osd_id), "--mkfs", "--osd-uuid", osd_uuid],
        check=True,
        capture_output=True,
      )
      osd_procs[osd_id] = subprocess.Popen(
        [os.path.join(ceph_bin, "ceph-osd"), "--conf", conf_path, "-f", "-i", str(osd_id), "--crush-location", f"root=default host={host}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
      )
      osd_host[osd_id] = host
      osd_id += 1
  n = len(osd_procs)
  wait_for(lambda: sum(osd_states_raw(conf_path, ceph_bin).values()) == n, 300, f"{n} OSDs up", interval=2)
  log.info(f"{n} OSDs up on {args.hosts} hosts")

  ceph(conf_path, ceph_bin, "osd", "pool", "create", "p", str(args.pgs), str(args.pgs))
  ceph(conf_path, ceph_bin, "osd", "pool", "application", "enable", "p", "rados")
  time.sleep(args.settle_seconds)
  return Cluster(run_dir, conf_path, ceph_bin, mon_bin, osd_procs, osd_host, mon_procs, mon_addrs, list(mon_names))


def osd_states_raw(conf_path: str, ceph_bin: str) -> dict:
  dump = json.loads(ceph(conf_path, ceph_bin, "osd", "dump", "--format", "json", timeout=10))
  return {o["osd"]: o["up"] for o in dump["osds"]}


def prime_laggy(cluster: Cluster, host: str, rounds: int, down_seconds: float):
  """Give the host's OSDs a laggy history, as a reboot does.

  Stops the host's OSDs until the mon marks them down, keeps them down for
  `down_seconds` more, then continues them; they boot again ("wrongly marked
  me down"), which makes the mon raise their `laggy_probability` and set
  `laggy_interval` to roughly how long they were down.
  """
  ids = [i for i, h in cluster.osd_host.items() if h == host]
  for r in range(rounds):
    for i in ids:
      cluster.osd_procs[i].send_signal(signal.SIGSTOP)
    wait_for(lambda: all(osd_states(cluster)[i] == 0 for i in ids), 600, "primed OSDs down", interval=1)
    # Continue the OSDs one after another, so that their down durations
    # (and thus `laggy_interval`, and thus their failure grace) differ, as
    # after real reboots: the first one after `down_seconds / len(ids)`, the
    # last one after `down_seconds`.
    for k, i in enumerate(ids):
      time.sleep(down_seconds / len(ids))
      cluster.osd_procs[i].send_signal(signal.SIGCONT)
    wait_for(lambda: all(osd_states(cluster)[i] == 1 for i in ids), 300, "primed OSDs back up", interval=1)
    log.info(f"laggy priming round {r + 1}/{rounds} done")
  dump = json.loads(ceph(cluster.conf_path, cluster.ceph_bin, "osd", "dump", "--format", "json", mon_host=query_mon_host(cluster)))
  xinfo = {x["osd"]: x for x in dump["osd_xinfo"]}
  log.info(f"laggy after priming: {[(i, round(xinfo[i]['laggy_probability'], 3), xinfo[i]['laggy_interval']) for i in ids]}")


def kill_host_and_measure(cluster: Cluster, host: str, max_wait: float, kill_mon: str | None) -> dict:
  ids = [i for i, h in cluster.osd_host.items() if h == host]
  t0 = time.monotonic()
  for i in ids:
    cluster.osd_procs[i].send_signal(signal.SIGSTOP)
  if kill_mon is not None:
    cluster.mon_procs[kill_mon].send_signal(signal.SIGSTOP)
    cluster.query_mons = [m for m in cluster.query_mons if m != kill_mon]
  log.info(f"SIGSTOPped {host}: osd {ids}" + (f" and mon.{kill_mon}" if kill_mon else ""))
  down_after = {}
  while (len(down_after) < len(ids)) and (time.monotonic() - t0 < max_wait):
    try:
      states = osd_states(cluster)
    except subprocess.SubprocessError:
      time.sleep(1)
      continue
    now = time.monotonic() - t0
    for i in ids:
      if (i not in down_after) and (states.get(i) == 0):
        down_after[i] = round(now, 1)
        log.info(f"osd.{i} marked down after {now:.1f} s")
    time.sleep(1)
  for i in ids:
    down_after.setdefault(i, None)
  return down_after


def stop_cluster(cluster: Cluster):
  for p in cluster.osd_procs.values():
    try:
      p.send_signal(signal.SIGCONT)
      p.kill()
    except ProcessLookupError:
      pass
  for p in cluster.mon_procs.values():
    p.send_signal(signal.SIGCONT)
    p.kill()
  for p in [*cluster.osd_procs.values(), *cluster.mon_procs.values()]:
    p.wait()


def main():
  logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
  parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  parser.add_argument("--run-dir", dest="run_dir", required=True, help="Directory for the cluster's data, logs and sockets (deleted first). Use a tmpfs.")
  parser.add_argument("--ceph-bin", dest="ceph_bin", required=True, help="Directory with the Ceph binaries (`ceph`, `ceph-mon`, `ceph-osd`, `monmaptool`).")
  parser.add_argument("--mon-bin", dest="mon_bin", default=None, help="Alternative `ceph-mon` binary (e.g. a patched one).")
  parser.add_argument("--mon-port", dest="mon_port", type=int, default=40100, help="Port of the first mon; the others use the following ports.")
  parser.add_argument("--mons", dest="mons", type=int, default=3)
  parser.add_argument("--kill-mon", dest="kill_mon", action=argparse.BooleanOptionalAction, default=True, help="Also stop mon `a` (the leader, lowest rank) together with the victim host, like a dead mon host.")
  parser.add_argument("--hosts", dest="hosts", type=int, default=10)
  parser.add_argument("--osds-per-host", dest="osds_per_host", type=int, default=6)
  parser.add_argument("--pgs", dest="pgs", type=int, default=512)
  parser.add_argument("--settle-seconds", dest="settle_seconds", type=float, default=30, help="Wait after pool creation, so that peering and heartbeat peers settle.")
  parser.add_argument("--mon-config", dest="mon_config", action="append", default=[], help="Extra `key=value` for `[global]` in ceph.conf; repeatable.")
  parser.add_argument("--prime-laggy", dest="prime_laggy", action=argparse.BooleanOptionalAction, default=False, help="Mark the victim host's OSDs down a few times first, to give them a laggy history.")
  parser.add_argument("--prime-rounds", dest="prime_rounds", type=int, default=2)
  parser.add_argument("--prime-down-seconds", dest="prime_down_seconds", type=float, default=60, help="How long the last primed OSD stays down per round after being marked down; the others are continued at even steps before it.")
  parser.add_argument("--victim-host", dest="victim_host", default="h1")
  parser.add_argument("--max-wait", dest="max_wait", type=float, default=900)
  parser.add_argument("--result-json", dest="result_json", default=None, help="Write the per-OSD mark-down times here.")
  args = parser.parse_args()

  cluster = start_cluster(args)
  try:
    if args.prime_laggy:
      prime_laggy(cluster, args.victim_host, args.prime_rounds, args.prime_down_seconds)
      time.sleep(args.settle_seconds)
    down_after = kill_host_and_measure(cluster, args.victim_host, args.max_wait, "a" if args.kill_mon else None)
  finally:
    stop_cluster(cluster)
  times = [t for t in down_after.values() if t is not None]
  summary = {
    "mon_bin": cluster.mon_bin,
    "mon_config": args.mon_config,
    "mons": args.mons,
    "kill_mon": args.kill_mon,
    "prime_laggy": args.prime_laggy,
    "prime_rounds": args.prime_rounds,
    "prime_down_seconds": args.prime_down_seconds,
    "hosts": args.hosts,
    "osds_per_host": args.osds_per_host,
    "down_after_seconds": down_after,
    "first": min(times) if times else None,
    "last": max(times) if times else None,
    "not_marked_down": [i for i, t in down_after.items() if t is None],
  }
  log.info(f"summary: {json.dumps(summary)}")
  if args.result_json:
    with open(args.result_json, "w") as f:
      json.dump(summary, f, indent=2)
  return 0


if __name__ == "__main__":
  sys.exit(main())
