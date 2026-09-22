# 2026-09-23 scan-fix full VMemKV re-measure run (report 20260923_scanfix)

Spot i4i.8xlarge x5, `run_5parallel_bench.sh --without-rivals`, AMI
`ami-0879aeb9a3801dce6` (identical to the 2026-09-21 authoritative run),
LTM cells split-conditioned (VMemKV MGLRU-off, in-memory default-on).
Tree: `87bb932`+`dacecdd` (sub-page-only T2 containment, inline scan reads).

## Provenance of the published report

`benchmark_results/20260921_mglru_off` merged via `merge_partial_results.py`
(`--keep-prefix Store=VMemKV/`): every `Store=VMemKV*` entry from this run,
every rival entry kept from 2026-09-21 (rival code unchanged; rival timelines
byte-identical). New-dir file set == 0921 file set. Background-jobs and
organic-split jsonl replaced by this run's (they measure VMemKV foreground
degradation, which the scan change touches).

## Same-box rivals anchor (2026-09-22 smoke, i-036e..., split condition)

| cell | VMemKV (OFF) | RocksDB (ON) | LMDB (ON) | BlobDB (ON) |
|---|---|---|---|---|
| Scan/Zipf/64KB t32 | 2740 | 2483 | 2409 | 1910 |
| YCSB-E/Zipf/64KB t32 | 2599 | 2470 | 2327 | 1952 |

The flipped 64KB cells are wins with the comparison run on one box under the
publication split, so cross-box noise cannot explain them.

## Incidents

- Rival 1KB `Get/Hit` cells under the 1 GiB scope livelocked on synchronous
  memcg reclaim (rival-store clone materializes ~8.6 GiB of dirty page cache;
  load ~1.4, threads parked in `futex`/`mem_cgroup_handle_over_high`, >55 min
  without progress) on the smoke box; JSON incomplete, cells discarded. The
  0921 full run completed the same cells on its box, and those rival numbers
  are what the merged report uses, so this run did not need them. VMemKV 1KB
  cells ran clean throughout (two boxes, multiple runs). A follow-up
  run of the rival 1KB matrix on a fresh box is still open if same-box 1KB
  anchors are wanted.
- Ad-hoc runs of this era keep using `ep_run.sh` (scope via
  `systemd-run --wait --pipe`, no `--scope`); the runner scripts provisioned
  their own instances and self-terminated them.
- `--without-rivals` skip-flag interplay: the 5th (probes) task ignores the
  flag (no rival CRUD cells there to skip) and completed in ~11 min
  self-terminating its instance.

## Headline after merge (winners matrix, new report)

No losing cells: the two 2026-09-21 losses (Scan-64KB, YCSB-E-64KB vs
RocksDB) flip to wins; LTM 1KB/64KB Get cells hold. Details in
`benchmark_results/pages/20260923_scanfix_charts.html`.
