# Reproduction: Ceph marks OSDs of a dead host down minutes late

Reproduction for https://tracker.ceph.com/issues/83176 (fix: https://github.com/ceph/ceph/pull/73900):
when a whole host dies (no connection-refused fast path, e.g. power loss), the monitors mark some of its OSDs down only minutes after their heartbeat grace.

Uses only upstream NixOS 26.05 (pinned in [`default.nix`](default.nix), Ceph 20.2.4), with and without the fix (the PR's commit as [`0001-mon-OSDMonitor-Prevent-late-reporters-from-resetting.patch`](0001-mon-OSDMonitor-Prevent-late-reporters-from-resetting.patch)).


## What it does

[`repro.py`](repro.py) starts a local cluster on `127.0.0.1` (run `./repro.py --help` for all options):

* 3 mons, 18 CRUSH hosts x 10 memstore OSDs (`--crush-location host=hN`, so `mon_osd_reporter_subtree_level = host` works as on a real cluster), and a replicated size-3 pool with 2048 PGs;
* gives the victim host's OSDs differing laggy histories, as real incidents do: stops them until they are marked down, then resumes them one by one, so that `mon_osd_adjust_heartbeat_grace` gives them different graces;
* stops the victim host's 10 OSDs and the leader mon with `SIGSTOP` (they stop answering without closing connections, like a powered-off machine);
* polls `ceph osd dump` every second and records when each of the 10 OSDs is marked down.

[`check.py`](check.py) compares each OSD's mark-down time with the grace the monitor logged for it (`osd.N failed (... after X >= grace Y)`).


## Running

```sh
nix-build -A repro-stock    # stock Ceph 20.2.4
nix-build -A repro-fixed    # Ceph 20.2.4 with the fix
nix-build -A check-fixed    # fails if, with the fix, an OSD is marked down more than 5 s after its grace
```

* Each repro build runs 6 clusters one after another (`--arg runs 6`), about 7 minutes and about 30 GB RAM each; the build output contains per run `run-N.json`, `run-N.log`, `run-N-cluster.log`, and `summary.txt`.
* `ceph-fixed` is a full Ceph build (about 15 minutes on 24 cores).
* The bug shows up only in some runs (it depends on the heartbeat peer assignment), so `repro-stock` does not fail; look at its `summary.txt`.


## Results

Time from `SIGSTOP` until the last of the victim host's 10 OSDs was marked down, and the worst delay of any of them beyond its own grace (from `summary.txt`):

| version        | runs | last of the 10 OSDs marked down after (s)    | worst delay beyond the OSD's own grace (s) |
|----------------|------|----------------------------------------------|--------------------------------------------|
| 20.2.4         | 6    | 42.5, 50.9, 51.6, 163.3, 170.1, 203.3        | 167.1                                      |
| 20.2.4 + fix   | 6    | 33.5, 35.0, 35.1, 35.7, 36.4, 37.5           | 3.2                                        |

For comparison, the same reproduction on v20.2.1 (results in the PR): 8 runs without the fix up to 194.6 s (worst delay 159 s), 7 runs with the fix 33.0-36.4 s (worst delay 1.7 s).
