# Scan-64KB LTM regression (0903 win -> 0914+ loss): page-containment gaps defeat readahead

Date: 2026-09-21. Box: i4i.8xlarge, Ubuntu (MGLRU ON `0x0007`), 1 GiB memcg scope,
`VMEMKV_BENCH_TARGET_RATIO=8.0`. A/B: `d6d215b` (old) vs HEAD (new), Scan/Zipf/64KB(20% 8B)/t32.

## Result

- ra=128 (default): old 2726/s vs new 2104/s (-23%). Reproduced on fresh masters +
  freshly rebuilt HEAD binary (`epn2`/`epo2` dirs).
- ra=0 (`read_ahead_kb=0` on nvme2n1): old 630/s vs new 608/s (-3.5%). Gap closes.
- I/O shape (ra=128, diskstats deltas, scan-phase-pure): old 17.4 reads/scan at
  173 sec/read; new 24.7 reads/scan (+42%) at 140 sec/read. Same logical bytes touched.

## Culprit

T2 page-containment inserts a ~4 KB gap after every 64 KB record (69,632 B stride,
page-aligned starts). The old layout packs gaplessly (65,576 B stride). Harness,
request stream (`ZipfDistribution`, per-thread seeding), `touch_bytes`, and the scan
read path (large records via `base_mmap_scan`, blind direct read, no mincore) are all
identical between revs, as are the established alias mappings. The only remaining
difference is the fault-address pattern, and killing block readahead removes old's
advantage almost entirely. Therefore: the gaps change readahead-window formation
(old merges ~173 sectors/read, new ~140), so new pays +42% device reads/scan, more
fault+reclaim CPU (profile: 27% vs 18% in fault/reclaim), longer waits, -23% throughput.
1 KB scans are nearly unaffected (-5%) because single-page records fit any minimal
window; 64 KB records (17 pages) need the window to ramp across the record.

## Fix direction (not yet implemented)

Restrict containment padding to sub-page records (`record_len <= 4096`): multi-page
records pack contiguously as before. 64 KB records touch 17 pages either way, so
containment buys them nothing and costs the readahead merging. 1 KB behavior (and its
Get win from span elimination) is unchanged.

## Fix A/B result (2026-09-21, fresh masters, REPS=1, min_time 8 s, t32)

Sub-page-only containment implemented in `page_skip_bytes()` (`t2_flat_file.cpp`).
T2 totals confirm it: fix master == old master byte count (`0x199BFFC00`), 277 MB
below the gapped master.

| cell | old (d6d215b) | fix (HEAD+subpage) | gapped HEAD |
|---|---|---|---|
| Scan/Zipf/64KB | 2726/s | 2232/s (-18%) | 2104/s (-23%) |
| Scan/Zipf/1KB | 82,884/s | 77,508/s (-6.5%) | 79,538/s (-4%) |
| Get-Hit/Zipf/1KB | 647,220/s | 753,327/s (+16%) | n/a (same layout as fix) |

The fix recovers only +6% on Scan64 (2104 -> 2232): the byte count is restored
(1.50 vs 1.47 MB/scan) but the merge size is not (134 vs 173 sec/read, 22.9 vs 17.4
reads/scan). Major-fault profiles (`perf record -e major-faults`) show fix faults
~3x/scan vs old (510K vs 218K total, 97.9% inside `flush_scan_batch`'s touch loop in
both), i.e. the residual is fault-driven, not fixed code overhead (sort/arena/T1 all
<=0.01%). Layout, read order, mappings, callback, harness, bulk order
(single-threaded ascending), batching (128 > 100-record scans, single flush),
background workers (all sleeping), and scheduling (31/s involuntary) were all ruled
out. The residual -18% is a code-path component still under investigation; next step
is fault-address tracing (sampled `page_fault_user` stride histograms old vs fix).

1 KB is neutral-to-good by construction (sub-page rule untouched): Get-Hit holds a
+16% win vs old, Scan-1KB moves within run noise (-2.5% vs gapped HEAD).

## Residual -18%: fault-attribution findings (diagnostics, all reverted)

- `perf record -e major-faults`: fix faults ~3x/scan vs old (510K vs 218K total;
  19.7 vs 6.6/scan), 98% inside the record-touch loop in both (`flush_scan_batch`
  vs `scan_impl`). Minors/scan are equal (~14-15); sort/arena/T1/shard frames all
  <=0.01%, so the residual is fault-driven, not fixed code overhead.
- Sampled `page_fault_user` traces with maps attribution (98.7% of faults in the
  SCAN mapping): fix faults concentrate at record-relative pages {0, 7} (443K +
  420K of 906K; windows [0..6],[7..14],[15..16], ~7-8 pages each) with 85.7%
  +16-page per-thread sequentiality; old faults uniformly across pages 0-15
  (~40K each, no window structure, 40% sequentiality). Faulted-record sets are
  identically Zipf tail-heavy in both (same stream); minors are equal, majors differ.
- `MADV_RANDOM` on the scan mapping (diagnostic, reverted): 539/s (collapse) --
  readahead is load-bearing, not pollution. `read_ahead_kb` 32/512: 1731/s and
  2287/s vs 2232/s at 128 -- windows are not max-bound (adaptive/history-bound).
- Ruled out: layout (fix master == old master byte count), harness, request
  stream, touch bytes, mappings, bulk order (single-threaded ascending), batching
  (128 > 100-record scans), background workers (sleeping), scheduling (31/s
  involuntary preemption), double-invoke, seqlock fallback (no memcpy in profile),
  fd structure (single shared fd in both), checkpoint/punch DONTNEED (populate-only
  or idle), 8B-record routing (same split both revs).
- Window note: the 8s gap (-19%, reproduced 3x: 2689/2726/2682 vs 2178/2232/2104)
  narrows to -9% over 60s windows (old 2441 vs fix 2226); the 60s runs carry
  Elapsed/populate accounting anomalies, so 8s stays the reference protocol.
- Open: why fix's window-entry pages stay cold while old's equivalents stay hot
  (MGLRU generation/eviction-side subtleties need kernel-side confirmation).
  Next candidates: fault-address tracing per major/minor split, or forcing larger
  initial windows to test demand-point coverage.

## Residual culprit: batch burst submission (diagnostic reverted)

Old `scan_impl` reads each record immediately inside the T1-walk callback
(walk1/read1/walk2/read2...); new `scan_impl` accumulates the whole scan into a
batch, sorts by offset, then reads all 100 back-to-back. Diagnostic
`kScanBatchSize = 128 -> 1` (trickle, T1-yield order; identical reads on a fresh
corpus) fully recovers: 2719/s with 18.35 reads/scan at 174.7 sec/read, matching
old (2726/s, 17.37, 173.1). So the remaining -18% is the burst: 100 back-to-back
demand faults with no interleaved T1-walk work trigger synchronous memcg reclaim
storms (profile: 19% reclaim vs 11%) and chain readahead demand points
({0,7} spikes, 85.7% +16 sequentiality), tripling majors/scan (19.7 vs 6.6);
old's trickle lets reclaim/IO keep up and preserves hot residency.

Shipping a fix needs care: batching exists for ordering-fragmentation on churned
corpora (whole-scan offset sort), where trickle reads in scattered T1 order.
Candidates: small batches (8-16: mostly-trickle + local offset order), validated
on fresh AND churned (update/delete) corpora, including YCSB-E-64KB which also
regressed. Not yet implemented; tree keeps only the sub-page containment fix.

## Decision (2026-09-22): batching removed entirely

Churn A/B on a fresh same-type box (`i-036e...`, probe
`benchmark/churn_scan_probe.cpp`: ordered populate -> checkpoint -> optional
remove+re-insert of the key-space head -> uniform 100-record scans, 32t, 8 s,
1 GiB memcg):

| layout | batch=128 | batch=1 (trickle) | batch removed (shipping code) |
|---|---|---|---|
| shuffled 100% | 137 | 139 | 150 |
| ordered, no churn | 585 | 697 | 694 |
| churn head 5% | 545 | 682 | 672 |
| churn head 20% | 392 | 422 | 452 |

Batching never wins in any layout, including the partial-churn mixed
base+tail case it was designed for: ascending-offset sorting of a scattered
set does not create sequentiality (random order is as random as sorted
order), while burst submission costs page residency in every scenario.
The 2026-09-07 probe doc's "measure the real benefit on LTM" step was never
done; this is that measurement, and the benefit is zero.

Removed: `kScanBatchSize`/`ScanBatchSlot`/`flush_scan_batch` (the 128-batch
constant itself, the slot type, and the arena/replay machinery); `scan_impl()`
reads each record inline in T1 key order via `try_read_base_record()` +
`read_t2_record_seqlock()` + thread-local buffers (pre-2725f84 shape). LLD
3.5/constants table updated. T2 placement test rewritten for the sub-page-only
containment invariant; full suite green except the LeanStore case (rivals
disabled in this probe-tree build).

Post-removal bench cells (scope protocol, t32, 8 s): Scan/Zipf/64KB 2650/s
(old-rev parity: 2689-2726 measured 09-21; gapped HEAD was 2104; a rebuild
rerun measured 2632 on the same box), Get-Hit/Zipf/1KB 834k/s, Scan/Zipf/1KB
76.0k/s, YCSB-E/Zipf/64KB 2501/s (first absolute on this box; earlier
09-14-era loss expected to close with the same mechanism).

## Methodology notes

- `ep_run.sh` (`sudo systemd-run --wait --pipe`, no `--scope`) is the working cell
  runner; `--wait` cannot combine with `--scope`. Bench filter must be
  `^Store=...Value=64KB.*threads:32$` (`64KB.` alone matches nothing).
- Fresh `VMEMKV_DB_DIR`s fail under the scope runner (`open: No such file` during
  first populate); prime them with one direct run, then measure via the runner.
- 14:33-15:02 saw a cluster of `t1 checkpoint ... failed validation` crashes on
  pre-existing masters with both binaries; fresh populate recovered cleanly and the
  A/B reproduced. Cause of the transient cluster is unknown; masters written by a
  crashed run's clone step should be treated as suspect and rebuilt.
