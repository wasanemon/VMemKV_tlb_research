#!/usr/bin/env python3
"""Generate a benchmark_results/pages/<id>_charts.html report from a benchmark_results/<id>/
directory, following the same Chart.js-based template established by prior reports (e.g.
2026081313_charts.html). Reuses that report's JS scaffolding verbatim (tab switching, chart
configs, plugins) and only regenerates the embedded data blocks (rawData/timelineData/
reorgScalingData), the header/description, and the Winners Matrix summary table.

Usage:
  generate_report.py --report-id 2026081711 --template pages/2026081313_charts.html \
                      --title-suffix "(without-rivals + livelock fixes)" \
                      --description "..." --out pages/2026081711_charts.html

  Cell win reasons (optional, single source of truth is cell_reasons.json next to
  this script): --cell-reasons cell_reasons.json renders per-cell [Rn] badges with
  hover tooltips plus a footnote legend right below the Winners matrix.
"""

import argparse
import html
import json
import re
from collections import OrderedDict
from pathlib import Path

STORE_VARIANT_TO_LABEL = {
    ("RocksDB", "RocksDB"): "RocksDB",
    ("LMDB", "LMDB"): "LMDB",
    ("RocksDB-BlobDB", "RocksDB-BlobDB"): "RocksDB-BlobDB",
    ("LeanStore", "LeanStore"): "LeanStore",
    ("VMemKV", "Baseline"): "Baseline",
    ("VMemKV", "Bloom"): "+BF",
    ("VMemKV", "Bloom-T1InlineValue"): "+Inline",
    ("VMemKV", "Bloom-T1InlineValue-Prefaulting"): "+Prefault",
    ("VMemKV", "Bloom-T1InlineValue-ReadRandom"): "+PinRandom",
    ("VMemKV", "Bloom-T1InlineValue-ReadSeq"): "+PinSeq",
}

VARIANT_ORDER = ["RocksDB", "LMDB", "RocksDB-BlobDB", "LeanStore", "Baseline", "+BF", "+Inline", "+PinRandom", "+PinSeq"]
RIVAL_STORES = ["RocksDB", "LMDB", "RocksDB-BlobDB", "LeanStore"]
COLORS = {
    "RocksDB": "#64748b", "LMDB": "#10b981", "RocksDB-BlobDB": "#a855f7", "LeanStore": "#0d9488",
    "Baseline": "#94a3b8",
    "+BF": "#f59e0b", "+Inline": "#6366f1",
    "+PinRandom": "#0ea5e9", "+PinSeq": "#84cc16",
}
# Point markers for the detail charts. Shapes must stay within drawMarkerShape()'s cases;
# line color (COLORS) is the primary distinguisher, rival (dashed) vs VMemKV (solid) the
# secondary one, so shape reuse across those two groups is acceptable.
VARIANT_MARKERS = {
    "RocksDB": "rect", "LMDB": "triangle", "RocksDB-BlobDB": "rectRot", "LeanStore": "star",
    "Baseline": "circle", "+BF": "cross", "+Inline": "crossRot",
    "+PinRandom": "rect", "+PinSeq": "rectRot",
}
# Stacking Variants legend: the currently measured set only.
VARIANT_DESCRIPTIONS = [
    ("RocksDB", "LSM-Tree のベースライン比較"),
    ("LMDB", "B+Tree / mmap ベースの比較対象"),
    ("RocksDB-BlobDB", "RocksDB の Blob 分離ストレージ変種(大きな Value 向け)"),
    ("LeanStore", "B+Tree / pointer-swizzling ベースの比較対象 (64KB値は格納不可のため除外)"),
    ("Baseline", "vmemkv 最適化なし"),
    ("+BF", "+ T1 Bloom Filter"),
    ("+Inline", "+BF + T1 Inline Value + adaptive read policy (trifecta, production config)"),
    ("+PinRandom", "+Inline のread policyをrandom-access mappingへピン留め (trifecta ablation)"),
    ("+PinSeq", "+Inline のread policyをsequential mappingへピン留め (trifecta ablation)"),
]
WORKLOADS = ["Insert", "Update", "Delete", "Get_Miss", "Get_Hit_Zipf", "Get_Hit_Uniform", "Scan_Zipf", "Scan_Uniform",
             "YCSB_E"]
THREADS = [1, 4, 16, 32]

SCENARIO_LABELS = {
    ("in_memory", "8B"): "8B_In-Memory",
    ("in_memory", "1KB"): "1KB_In-Memory",
    ("ltm", "1KB"): "1KB_LTM",
    ("ltm", "64KB"): "64KB_LTM",
}


def parse_bench_name(name):
    parts = name.split("/")
    store = parts[0].split("=", 1)[1]
    variant = parts[1].split("=", 1)[1]
    op = None
    mode = None
    dist = None
    threads = None
    for p in parts[2:]:
        if p.startswith("Op="):
            op = p.split("=", 1)[1]
        elif p.startswith("Mode="):
            mode = p.split("=", 1)[1]
        elif p.startswith("Dist="):
            dist = p.split("=", 1)[1]
        elif p.startswith("threads:"):
            threads = int(p.split(":", 1)[1])
    workload = None
    if op == "Insert":
        workload = "Insert"
    elif op == "Delete":
        workload = "Delete"
    elif op == "Update":
        workload = "Update"
    elif op == "Get":
        if mode == "Miss":
            workload = "Get_Miss"
        elif mode == "Hit" and dist == "Zipf":
            workload = "Get_Hit_Zipf"
        elif mode == "Hit" and dist == "Uniform":
            workload = "Get_Hit_Uniform"
    elif op == "Scan":
        if dist == "Zipf":
            workload = "Scan_Zipf"
        elif dist == "Uniform":
            workload = "Scan_Uniform"
    elif op == "YCSB-E":
        workload = "YCSB_E"
    return store, variant, workload, threads


def build_raw_data(report_dir):
    raw_data = {}
    for (scenario, val_size), scenario_key in SCENARIO_LABELS.items():
        fname = report_dir / f"results_{scenario}_{val_size}.json"
        data = json.loads(fname.read_text())
        op_map = {}
        for b in data["benchmarks"]:
            store, variant, workload, threads = parse_bench_name(b["name"])
            if workload is None or threads not in THREADS:
                continue
            label = STORE_VARIANT_TO_LABEL.get((store, variant))
            if label is None:
                continue
            op_map.setdefault(workload, {}).setdefault(label, [None] * len(THREADS))
            idx = THREADS.index(threads)
            op_map[workload][label][idx] = b.get("items_per_second", 0.0)
        raw_data[scenario_key] = op_map
    return raw_data


def build_headline_data(report_dir):
    """Per (scenario_key, workload, label, thread index): items_per_second sample SD from
    same-campaign headline repetitions (results_{scenario}_{val_size}_headline.json, Google
    Benchmark stddev aggregates over 3 repetitions). Plotted centers stay on the main-matrix
    single runs (Winners-table consistency); the SD only sets whisker width. Files or cells
    without headline coverage yield no entries and render bare."""
    headline_data = {}
    for (scenario, val_size), scenario_key in SCENARIO_LABELS.items():
        fname = report_dir / f"results_{scenario}_{val_size}_headline.json"
        if not fname.exists():
            continue
        data = json.loads(fname.read_text())
        for b in data["benchmarks"]:
            if b.get("run_type") != "aggregate" or b.get("aggregate_name") != "stddev":
                continue
            store, variant, workload, threads = parse_bench_name(b["run_name"])
            if workload is None or threads not in THREADS:
                continue
            label = STORE_VARIANT_TO_LABEL.get((store, variant))
            if label is None:
                continue
            sd = b.get("items_per_second", 0.0)
            (headline_data.setdefault(scenario_key, {}).setdefault(workload, {}).setdefault(label, {})
             [THREADS.index(threads)]) = sd
    return headline_data


# Inline Chart.js plugin drawing headline whiskers (main-run value ± headline SD) on points
# carrying a headlineSd entry. Dependency-free: the report pages already load Chart.js from
# CDN, and a vendored error-bar plugin would add a second version-pinned dependency for one
# whisker shape.
HEADLINE_ERRORBAR_PLUGIN_JS = """    // Headline error bars: whiskers at the plotted value ± headline SD (n=3 same-build
    // repetitions) on max-thread points with headline coverage; all other points render bare.
    Chart.register({
      id: 'headlineErrorBars',
      afterDatasetsDraw(chart) {
        const yScale = chart.scales.y;
        if (!yScale) return;
        chart.data.datasets.forEach((d, i) => {
          const sds = d.headlineSd;
          if (!sds) return;
          const meta = chart.getDatasetMeta(i);
          if (meta.hidden) return;
          meta.data.forEach((pt, idx) => {
            const sd = sds[idx];
            if (sd == null || !(sd > 0)) return;
            const v = d.data[idx];
            if (v == null) return;
            const col = (typeof d.borderColor === 'string') ? d.borderColor : '#6366f1';
            const x = pt.x;
            const yHi = yScale.getPixelForValue(v + sd);
            const yLo = yScale.getPixelForValue(v - sd);
            const c = chart.ctx;
            const cap = 4;
            c.save();
            c.strokeStyle = col;
            c.lineWidth = 1.5;
            c.beginPath();
            c.moveTo(x, yHi); c.lineTo(x, yLo);
            c.moveTo(x - cap, yHi); c.lineTo(x + cap, yHi);
            c.moveTo(x - cap, yLo); c.lineTo(x + cap, yLo);
            c.stroke();
            c.restore();
          });
        });
      }
    });
"""


def _normalize_value_size(file_val_size):
    # bench_kv writes e.g. "1KB_20__8B_" for the 80/20-mix value-size label -- normalize
    # to the plain "1KB"/"64KB"/"8B" keys used elsewhere (SCENARIO_LABELS).
    if file_val_size.startswith("1KB"):
        return "1KB"
    if file_val_size.startswith("64KB"):
        return "64KB"
    return file_val_size


def _resolve_variant_label(store, variant):
    base_store = store.split("-", 1)[0] if store.startswith("VMemKV") else store
    return STORE_VARIANT_TO_LABEL.get((base_store, variant))


def _load_per_variant(report_dir, glob, key_fn):
    """Yield (key, record) pairs for per-variant data files.

    Iterates SCENARIO_LABELS in order; per entry, globs glob.format(scenario=scenario,
    val_size=val_size). JSON files yield their whole document as one record, JSONL files
    one record per non-blank line. key_fn(scenario_key, record, val_size, path) returns
    the dict key, or None to skip the record.
    """
    for (scenario, val_size), scenario_key in SCENARIO_LABELS.items():
        for path in report_dir.glob(glob.format(scenario=scenario, val_size=val_size)):
            if path.suffix == ".jsonl":
                records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            else:
                records = [json.loads(path.read_text())]
            for record in records:
                key = key_fn(scenario_key, record, val_size, path)
                if key is not None:
                    yield key, record


def _timeline_variant_key(scenario_key, record, val_size, path):
    store = record["store"]
    variant = record["variant"]
    if _normalize_value_size(record["value_size"]) != val_size:
        return None
    label = _resolve_variant_label(store, variant)
    if label is None:
        return None
    return (scenario_key, label)


def build_timeline_data(report_dir):
    timeline_data = {scenario_key: {} for (_, _), scenario_key in SCENARIO_LABELS.items()}
    for key, record in _load_per_variant(report_dir, "ycsb_e_timeline_{scenario}_*.json",
                                         _timeline_variant_key):
        scenario_key, label = key
        timeline_data[scenario_key][label] = record["timeline"]
    return timeline_data


def build_forced_events_data(report_dir):
    """Per (scenario, variant): the actual forced_events list (scheduled_sec, fired_sec, kind,
    elapsed_sec). Fired second can lag scheduled second arbitrarily under sustained write
    contention (cascading is intentionally unguarded -- see kForcedTriggers in bench_kv.cpp)."""
    forced_events_data = {scenario_key: {} for (_, _), scenario_key in SCENARIO_LABELS.items()}
    for key, record in _load_per_variant(report_dir, "ycsb_e_timeline_{scenario}_*.json",
                                         _timeline_variant_key):
        scenario_key, label = key
        forced_events_data[scenario_key][label] = record.get("forced_events", [])
    return forced_events_data


def _background_job_key(scenario_key, record, val_size, path):
    # run_probe_point()'s (common/reorg_probe_common.sh) synthesized outer-timeout fallback
    # (setup itself hung, before bench_kv could print its own record) doesn't know this
    # script's "job" field -- skip rather than KeyError; the table just renders that combo
    # as n/a (build_background_jobs_data() finds no entry for it), same as a missing file.
    if "job" not in record or "scenario" not in record:
        return None
    return (record["job"], record["scenario"])


def build_background_jobs_data(report_dir):
    """Reads background_jobs_in_memory.jsonl / background_jobs_ltm.jsonl
    (run_background_jobs_probe.sh via run_bench_aws_c6id.sh's run_remote_probe(), one file
    per scenario) into {(job, scenario): rec}. Each rec carries
    job_elapsed_sec plus insert/update/scan_degradation_pct, measured against a fixed 1KB x
    10,000,000-record corpus (in_memory unconstrained, ltm cgroup-constrained to the same
    LTM_MEMORY_BUDGET_BYTES as the rest of the suite) -- a single reproducible reference point
    rather than a sweep across the matrix's 4 scenario/value-size combos, see
    render_background_jobs_summary_html()."""
    return dict(_load_per_variant(report_dir, "background_jobs_{scenario}.jsonl",
                                  _background_job_key))


def _organic_split_key(scenario_key, record, val_size, path):
    # Same reasoning as _background_job_key(): a synthesized outer-timeout fallback
    # record lacks "scenario" -- skip rather than KeyError, renders as n/a like a missing file.
    if "scenario" not in record:
        return None
    return record["scenario"]


def build_organic_split_data(report_dir, prefix="organic_split"):
    """Reads {prefix}_in_memory.jsonl / {prefix}_ltm.jsonl (run_organic_split_probe.sh via
    run_bench_aws_c6id.sh's run_remote_probe(); prefix is "organic_split" for monotonic keys,
    "organic_split_random" for random keys) into {scenario: rec}. Each rec's "splits" list
    holds one entry per organic per-shard split observed during a fixed 90s sustained-insert run,
    see render_organic_split_summary_html()."""
    return dict(_load_per_variant(report_dir, f"{prefix}_{{scenario}}.jsonl", _organic_split_key))


def organic_split_averages(organic_split_data, scenario):
    """(avg_pause_us, avg_degradation_pct, n) across a scenario's observed splits, or None if
    that scenario has no usable data (missing file, timeout, or zero splits observed)."""
    rec = organic_split_data.get(scenario)
    if not rec or rec.get("timed_out"):
        return None
    splits = rec.get("splits", [])
    if not splits:
        return None
    n = len(splits)
    avg_pause_us = sum(s["pause_us"] for s in splits) / n
    avg_degr = sum(s["degradation_pct"] for s in splits) / n
    return avg_pause_us, avg_degr, n


def render_organic_split_chart_html(organic_split_data, chart_suffix=""):
    """Single line chart (writer-visible pause duration in ms) plotted against shard count
    reached, one line per scenario -- the full per-split detail behind the summary rows
    organic_split_averages() feeds into the Background Jobs table.

    Truncated to shard counts both scenarios actually reached: in a 90s fixed-duration run, LTM's
    somewhat lower insert throughput under memory pressure means it simply runs out of time before
    its next split, ending on fewer total splits than in-memory -- not a bug, just less corpus
    grown in the same wall-clock window.

    Self-contained: canvas plus its own inline <script> (Chart.js is already loaded in <head>
    by the time this renders, and the Summary tab is visible on load, so creating the chart
    immediately is safe -- same reasoning as every other chart on this page, just not routed
    through the shared initCharts() since this section's data has a different shape from the
    workload/thread-count charts there)."""
    if not organic_split_data:
        return ""
    scenario_labels = {"in_memory": "in-memory", "ltm": "LTM"}
    colors = {"in_memory": "#6366f1", "ltm": "#f59e0b"}
    usable = {}
    timeout_note = ""
    for scenario in ["in_memory", "ltm"]:
        rec = organic_split_data.get(scenario)
        if not rec:
            continue
        if rec.get("timed_out"):
            timeout_note += (f'<p class="text-xs text-rose-600 font-semibold">{scenario_labels[scenario]}: '
                             f'did not complete (timeout)</p>')
            continue
        splits = rec.get("splits", [])
        if splits:
            usable[scenario] = splits
    if not usable:
        return timeout_note

    # Common cap: the lowest "highest shard count reached" across scenarios with data, so every
    # plotted point exists for every scenario shown (see the docstring above).
    common_max_shard = min(max(s["shard_count_after"] for s in splits) for splits in usable.values())

    pause_datasets = []
    for scenario, splits in usable.items():
        capped = [s for s in splits if s["shard_count_after"] <= common_max_shard]
        col = colors[scenario]
        pause_datasets.append({
            "label": scenario_labels[scenario],
            "data": [{"x": s["shard_count_after"], "y": s["pause_us"] / 1000.0} for s in capped],
            "borderColor": col, "backgroundColor": col, "tension": 0.3,
            "pointRadius": 4, "pointHoverRadius": 6,
        })

    data_json = json.dumps({"pause": pause_datasets})
    return timeout_note + f'''
<div class="space-y-1.5 mt-3">
  <h4 class="text-xs font-bold text-slate-600">Writer-visible pause duration</h4>
  <div class="h-64 relative"><canvas id="organic-split-pause-chart{chart_suffix}"></canvas></div>
</div>
<script>
(function() {{
  const d = {data_json};
  const xScale = {{ type: 'linear', title: {{ display: true, text: 'Shard count after this split', font: {{size:11,family:'Inter'}}, color: '#64748b' }},
       ticks: {{ stepSize: 1, font: {{size:10,family:'Inter'}}, color: '#94a3b8' }},
       grid: {{ color: 'rgba(100,116,139,0.08)' }} }};
  const yScale = (yTitle, extra) => Object.assign({{ title: {{ display: true, text: yTitle, font: {{size:11,family:'Inter'}}, color: '#64748b' }},
       ticks: {{ font: {{size:10,family:'Inter'}}, color: '#94a3b8' }},
       grid: {{ color: 'rgba(100,116,139,0.08)' }} }}, extra || {{}});
  const commonOptions = (yTitle, yExtra) => ({{
    responsive: true, maintainAspectRatio: false,
    plugins: {{ legend: {{ position: 'bottom', labels: {{ boxWidth: 12, font: {{size:11,family:'Inter',weight:'500'}}, usePointStyle: true }} }} }},
    scales: {{ x: xScale, y: yScale(yTitle, yExtra) }},
  }});
  const pauseCanvas = document.getElementById('organic-split-pause-chart{chart_suffix}');
  if (pauseCanvas && d.pause.length) {{
    new Chart(pauseCanvas, {{ type: 'line', data: {{ datasets: d.pause }},
      options: commonOptions('Pause duration (ms)', {{ min: 0, max: 10000 }}) }});
  }}
}})();
</script>'''


def compute_winners_matrix(raw_data):
    # VMemKV variants within _TIE_BAND of the best share the winner label (同着):
    # sub-1% gaps are not attributable to any variant's mechanism.
    _TIE_BAND = 0.01
    rows = []
    for workload in WORKLOADS:
        cells = []
        for scenario_key in ["8B_In-Memory", "1KB_In-Memory", "1KB_LTM", "64KB_LTM"]:
            op_data = raw_data.get(scenario_key, {}).get(workload)
            if not op_data:
                cells.append(None)
                continue
            idx32 = THREADS.index(32)
            best_vmemkv = None
            best_rival = None
            best_rival_label = None
            for label, values in op_data.items():
                v = values[idx32]
                if v is None:
                    continue
                if label in RIVAL_STORES:
                    if best_rival is None or v > best_rival:
                        best_rival, best_rival_label = v, label
                else:
                    if best_vmemkv is None or v > best_vmemkv:
                        best_vmemkv = v
            if best_vmemkv is None or best_rival is None or best_rival == 0:
                cells.append(None)
                continue
            tied = [label for label in VARIANT_ORDER
                    if label not in RIVAL_STORES and label in op_data
                    and op_data[label][idx32] is not None
                    and op_data[label][idx32] >= best_vmemkv * (1 - _TIE_BAND)]
            ratio = best_vmemkv / best_rival
            cells.append({
                "ratio": ratio,
                "vmemkv_label": tied[0],
                "vmemkv_labels": tied,
                "rival_label": best_rival_label,
            })
        rows.append((workload, cells))
    return rows


def _badge_tier(value, tiers):
    """First-match tier lookup over ascending (bound, inclusive, result) triples."""
    for bound, inclusive, result in tiers:
        if value < bound or (inclusive and value == bound):
            return result
    raise ValueError(f"value {value!r} matched no tier")


_RATIO_TIERS = [
    (0.85, True, ("❌ LOSE", "bg-rose-600 text-white border-transparent shadow-sm")),
    (0.95, False, ("LOSE", "bg-rose-50 text-rose-700 border-rose-200")),
    (1.05, True, ("≈ EVEN", "bg-slate-100 text-slate-600 border-slate-200")),
    (1.15, False, ("WIN", "bg-emerald-50 text-emerald-700 border-emerald-200")),
    (float("inf"), True, ("✅ WIN", "bg-emerald-600 text-white border-transparent shadow-sm")),
]

_SLOWDOWN_TIERS = [
    (20, False, "bg-emerald-50 text-emerald-700 border-emerald-200"),
    (50, False, "bg-amber-50 text-amber-700 border-amber-200"),
    (float("inf"), True, "bg-rose-600 text-white border-transparent shadow-sm"),
]


def badge_html(text, cls):
    """Inline badge <span> shared by the winners matrix and the background-jobs table."""
    return (f'<span class="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] border '
            f'{cls}">{text}</span>')


def _badge_for_ratio(ratio):
    """Matches the 5-tier scheme established by prior reports (e.g. 2026081313): a ±5% band
    around 1.0x is noise-level ("EVEN", gray); beyond that a weak (pale) or strong (solid,
    with emoji) WIN/LOSE badge depending on whether the deviation exceeds 15%."""
    return _badge_tier(ratio, _RATIO_TIERS)


# Per-reason colors (keys are cell_reasons.json's "color" field): pale badge classes
# for matrix badges + legend chips, solid dot class for the tooltip header, and a
# strong cell-highlight background (inline style, so it never shifts layout).
_REASON_PALETTE = {
    "blue": ("bg-blue-50 text-blue-700 border-blue-200", "bg-blue-400", "#dbeafe"),
    "emerald": ("bg-emerald-50 text-emerald-700 border-emerald-200", "bg-emerald-400", "#d1fae5"),
    "green": ("bg-green-50 text-green-700 border-green-200", "bg-green-400", "#dcfce7"),
    "amber": ("bg-amber-50 text-amber-700 border-amber-200", "bg-amber-400", "#fef3c7"),
    "orange": ("bg-orange-50 text-orange-700 border-orange-200", "bg-orange-400", "#ffedd5"),
    "yellow": ("bg-yellow-50 text-yellow-700 border-yellow-200", "bg-yellow-400", "#fef9c3"),
    "violet": ("bg-violet-50 text-violet-700 border-violet-200", "bg-violet-400", "#ede9fe"),
    "purple": ("bg-purple-50 text-purple-700 border-purple-200", "bg-purple-400", "#f3e8ff"),
}


def _reason_colors(info):
    """(badge_classes, dot_class, highlight_bg) for a reason entry; unknown colors fall back to indigo."""
    return _REASON_PALETTE.get((info or {}).get("color", "blue"), _REASON_PALETTE["blue"])


def render_winners_matrix_html(rows, cell_reasons=None, cell_stats=None):
    """Winners matrix <tr> rows. cell_reasons (load_cell_reasons()'s output, or None)
    adds per-cell [Rn] badges with hover tooltips; unknown/missing cells render bare.
    cell_stats (load_cell_stats()'s output, or None) appends cross-run ±CV% (n=4) to
    winner labels where measured."""
    reasons = (cell_reasons or {}).get("reasons", {})
    cells_map = (cell_reasons or {}).get("cells", {})
    scenario_keys = ["8B_In-Memory", "1KB_In-Memory", "1KB_LTM", "64KB_LTM"]
    workload_display = {
        "Insert": "Insert", "Update": "Update (Zipf)", "Delete": "Delete",
        "Get_Miss": "Get (Miss)", "Get_Hit_Zipf": "Get (Hit, Zipf)", "Get_Hit_Uniform": "Get (Hit, Uniform)",
        "Scan_Zipf": "Scan (Zipf)", "Scan_Uniform": "Scan (Uniform)",
        "YCSB_E": "YCSB-E",
    }
    out = []
    for workload, cells in rows:
        out.append(f'<tr class="hover:bg-indigo-50/10 transition-colors"><td class="py-3.5 px-4 font-bold text-slate-800 bg-slate-50/20">{workload_display[workload]}</td>')
        for scenario_key, cell in zip(scenario_keys, cells):
            if cell is None:
                out.append('<td class="py-3.5 px-4 text-slate-300 text-xs">n/a</td>')
                continue
            ratio = cell["ratio"]
            label_text, badge_class = _badge_for_ratio(ratio)
            tied_labels = cell.get("vmemkv_labels", [cell["vmemkv_label"]])
            stats = (cell_stats or {}).get("stats", {})
            stat_parts = []
            for lbl in tied_labels:
                st = stats.get(f"{workload}|{scenario_key}|{lbl}")
                if st is not None:
                    stat_parts.append(f'{lbl} ±{st["cv_pct"]}%')
                else:
                    stat_parts.append(lbl)
            if any(f"{workload}|{scenario_key}|{lbl}" in stats for lbl in tied_labels):
                n = stats.get(f"{workload}|{scenario_key}|{tied_labels[0]}", {}).get("n", "")
                vmemkv_text = ", ".join(stat_parts) + (f" (n={n})" if n else "")
            else:
                vmemkv_text = tied_labels[0]
            if len(tied_labels) > 1:
                vmemkv_text += " 同着"
            reason_ids = cells_map.get(f"{workload}|{scenario_key}", [])
            reason_badges = []
            for rid in reason_ids:
                info = reasons.get(rid)
                if info is None:
                    reason_badges.append(html.escape(str(rid)))
                    continue
                # Rich tooltip content travels in data-* attributes; the shared
                # floating panel (render_cell_reasons_legend()) renders them. Native
                # title= is deliberately not used (unstyled, delayed, single-line).
                # data-rid drives cross-highlighting of every cell carrying this reason.
                tip_title = html.escape(f'{rid} [{info.get("concept", "?")}・{info.get("cat", "?")}] {info.get("short", "")}', quote=True)
                tip_body = html.escape(str(info.get("detail", "")), quote=True)
                tip_doc = html.escape(str(info.get("doc", "")), quote=True)
                badge_cls, dot_cls, hl_bg = _reason_colors(info)
                rid_attr = html.escape(str(rid), quote=True)
                hl_attr = html.escape(hl_bg, quote=True)
                reason_badges.append(
                    f'<span class="reason-badge inline-flex items-center px-1 py-0.5 rounded text-[10px] border '
                    f'{badge_cls} font-bold cursor-help" tabindex="0" data-rid="{rid_attr}" data-hl="{hl_attr}" '
                    f'data-tip-title="{tip_title}" data-tip-body="{tip_body}" data-tip-doc="{tip_doc}" '
                    f'data-tip-dot="{dot_cls}">{html.escape(str(rid))}</span>')
            reasons_html = ""
            cell_reasons_attr = ""
            if reason_badges:
                reasons_html = '<span class="flex flex-wrap gap-1 mt-1">' + "".join(reason_badges) + "</span>"
                cell_reasons_attr = f' data-reasons="{html.escape(" ".join(str(r) for r in reason_ids), quote=True)}"'
            out.append(f'''<td class="py-3.5 px-4 transition-colors"{cell_reasons_attr}>
  <div class="flex flex-col gap-0.5">
    {badge_html(f"\n      {label_text} ({ratio:.2f}x)\n    ", f"{badge_class} font-bold  w-fit")}
    <span class="text-[11px] text-slate-500 font-medium">vmemkv ({html.escape(vmemkv_text)})</span>
    <span class="text-[10px] text-slate-400">vs {cell["rival_label"]}</span>{reasons_html}
  </div>
</td>''')
        out.append("</tr>")
    return "\n".join(out)


def load_cell_reasons(path):
    """Loads a cell_reasons.json file: {"reasons": {R1: {cat, short, detail, doc}},
    "cells": {"<WORKLOAD>|<scenario_key>": [R..]}, "notes": [...]}. Returns {} when
    path is None (caller renders the matrix bare)."""
    if path is None:
        return {}
    data = json.loads(Path(path).read_text())
    return {"reasons": data.get("reasons", {}), "cells": data.get("cells", {}),
            "notes": data.get("notes", []), "concepts": data.get("concepts", {})}


def load_cell_stats(path):
    """Loads a cell_stats.json file: {"<WORKLOAD>|<scenario_key>|<label>":
    {mean_k, stddev_k, cv_pct, n}}. Returns {} when path is None."""
    if path is None:
        return {}
    return {"stats": json.loads(Path(path).read_text())}


def render_cell_reasons_legend(cell_reasons, rows):
    """Footnote legend for the [Rn] badges actually used by this report's matrix, grouped
    by concept (cell_reasons.json's "concepts"), plus suite-wide notes. Empty when no
    reasons file was given (nothing to footnote)."""
    reasons = (cell_reasons or {}).get("reasons", {})
    cells_map = (cell_reasons or {}).get("cells", {})
    notes = (cell_reasons or {}).get("notes", [])
    concepts = (cell_reasons or {}).get("concepts", {})
    if not reasons:
        return ""
    scenario_keys = ["8B_In-Memory", "1KB_In-Memory", "1KB_LTM", "64KB_LTM"]
    used = []
    for workload, cells in rows:
        for scenario_key, cell in zip(scenario_keys, cells):
            if cell is None:
                continue
            for rid in cells_map.get(f"{workload}|{scenario_key}", []):
                if rid not in used:
                    used.append(rid)

    def render_reason(rid):
        info = reasons.get(rid)
        if info is None:
            return f'          <li><strong>{html.escape(str(rid))}</strong></li>'
        doc = html.escape(str(info.get("doc", "")))
        badge_cls, _, hl_bg = _reason_colors(info)
        rid_attr = html.escape(str(rid), quote=True)
        hl_attr = html.escape(hl_bg, quote=True)
        chip = (f'<span class="reason-chip inline-flex items-center px-1 py-0.5 rounded text-[10px] border '
                f'{badge_cls} font-bold cursor-help" tabindex="0" data-rid="{rid_attr}" data-hl="{hl_attr}">{html.escape(str(rid))}</span>')
        return (f'          <li>{chip} <strong>{html.escape(str(info.get("short", "")))}</strong>: '
                f'{html.escape(str(info.get("detail", "")))} '
                f'<span class="text-slate-400">({doc})</span></li>')

    def group_order(rid):
        concept = (reasons.get(rid) or {}).get("concept", "M")
        m = re.match(r"^R(\d+)$", rid)
        num = (0, int(m.group(1))) if m else (1, 0)
        return (0 if concept != "M" else 1, concept, num, rid)

    groups = OrderedDict()
    for rid in sorted(used, key=group_order):
        concept = (reasons.get(rid) or {}).get("concept", "M")
        groups.setdefault(concept, []).append(rid)

    sections = []
    for concept, rids in groups.items():
        meta = concepts.get(concept, {})
        if meta:
            heading = (f'          <p class="text-xs font-bold text-slate-700 mt-3 mb-1">{html.escape(concept)} '
                       f'{html.escape(str(meta.get("title", "")))} '
                       f'<span class="font-normal text-slate-600">-- {html.escape(str(meta.get("desc", "")))}</span></p>')
        else:
            heading = (f'          <p class="text-xs font-bold text-slate-700 mt-3 mb-1">Annotations '
                       f'<span class="font-normal text-slate-600">-- 技術概念ではなく状態の注記。再測定対象。</span></p>')
        items = "\n".join(render_reason(rid) for rid in rids)
        sections.append(f'{heading}\n          <ul class="space-y-1 text-xs text-slate-500 list-disc pl-4">\n{items}\n          </ul>')
    groups_html = "\n".join(sections)
    notes_html = "".join(f'          <li class="text-slate-400">{html.escape(str(note))}</li>' for note in notes)
    # Safelist of the solid dot classes the tooltip script assigns at runtime, so the
    # Tailwind Play CDN generates them from this static scan instead of relying on its
    # MutationObserver catching the hover-time class swap.
    dot_safelist = " ".join(sorted({dot for _, dot, _ in _REASON_PALETTE.values()}))
    # Sentinel-wrapped so reruns replace (not duplicate) the legend: main() strips any
    # existing block between these markers before inserting the fresh one.
    return f'''<!-- cell-reasons-legend:start -->
        <div class="mt-4 border-t border-slate-100 pt-4">
          <p class="text-xs font-bold text-slate-700 mb-1">Why each cell wins -- reason legend by concept (hover or focus a badge to highlight its cells)</p>
{groups_html}
          <ul class="space-y-1 text-[11px] mt-2 list-disc pl-4">
{notes_html}
          </ul>
          <div class="hidden"><span class="{dot_safelist}"></span></div>
        </div>''' + _REASON_TIP_PANEL_HTML + '''
        <!-- cell-reasons-legend:end -->'''


# Shared floating tooltip panel for every .reason-badge above. Tailwind-styled to match
# the report; position:fixed (not group-hover) so it is never clipped by the matrix's
# overflow-x-auto container. One panel + one delegated listener set for the whole page.
_REASON_TIP_PANEL_HTML = '''
        <div id="reason-tip" class="hidden fixed z-50 w-72 max-w-[calc(100vw-16px)]" role="tooltip">
          <div class="bg-slate-900 rounded-lg shadow-xl border border-slate-700 overflow-hidden">
            <div class="flex items-center gap-2 px-3 py-2 border-b border-white/10">
              <span id="reason-tip-dot" class="inline-block w-2 h-2 rounded-full bg-indigo-400"></span>
              <p id="reason-tip-title" class="text-xs font-bold text-white"></p>
            </div>
            <p id="reason-tip-body" class="text-[11px] leading-relaxed px-3 py-2 text-slate-200"></p>
            <p id="reason-tip-doc" class="text-[10px] px-3 pb-2 text-slate-400"></p>
          </div>
        </div>
        <script>
        (function () {
          var tip = document.getElementById('reason-tip');
          if (!tip) { return; }
          var titleEl = document.getElementById('reason-tip-title');
          var bodyEl = document.getElementById('reason-tip-body');
          var docEl = document.getElementById('reason-tip-doc');
          var dotEl = document.getElementById('reason-tip-dot');
          function show(anchor) {
            titleEl.textContent = anchor.getAttribute('data-tip-title') || '';
            bodyEl.textContent = anchor.getAttribute('data-tip-body') || '';
            docEl.textContent = anchor.getAttribute('data-tip-doc') || '';
            dotEl.className = 'inline-block w-2 h-2 rounded-full ' + (anchor.getAttribute('data-tip-dot') || 'bg-indigo-400');
            tip.classList.remove('hidden');
            var r = anchor.getBoundingClientRect();
            var w = tip.offsetWidth;
            var h = tip.offsetHeight;
            var x = r.left + r.width / 2 - w / 2;
            x = Math.max(8, Math.min(x, window.innerWidth - w - 8));
            var y = r.top - h - 8;
            if (y < 8) { y = r.bottom + 8; }
            tip.style.left = x + 'px';
            tip.style.top = y + 'px';
          }
          function hide() { tip.classList.add('hidden'); }
          function ridOf(el) { return el.getAttribute ? el.getAttribute('data-rid') : null; }
          // Cross-highlight: paint every matrix cell carrying rid with the hovered
          // badge's highlight color, dim the rest. Background-only styling, so cell
          // geometry never moves.
          function clearHighlight() {
            var tds = document.querySelectorAll('td[data-reasons]');
            for (var i = 0; i < tds.length; i++) { tds[i].style.backgroundColor = ''; tds[i].style.opacity = ''; }
          }
          function highlight(rid, color) {
            clearHighlight();
            var tds = document.querySelectorAll('td[data-reasons]');
            for (var i = 0; i < tds.length; i++) {
              var rs = (tds[i].getAttribute('data-reasons') || '').split(' ');
              var hit = false;
              for (var k = 0; k < rs.length; k++) { if (rs[k] === rid) { hit = true; break; } }
              if (hit) { tds[i].style.backgroundColor = color; }
              else { tds[i].style.opacity = '0.45'; }
            }
          }
          document.addEventListener('mouseover', function (e) {
            var tgt = e.target && e.target.closest ? e.target : null;
            var t = tgt ? tgt.closest('.reason-badge') : null;
            var inTip = tgt ? tgt.closest('#reason-tip') : null;
            if (t) { show(t); } else if (!inTip) { hide(); }
            var h = tgt ? tgt.closest('[data-rid]') : null;
            if (h) { highlight(ridOf(h), h.getAttribute('data-hl') || '#e0e7ff'); } else if (!inTip) { clearHighlight(); }
          });
          document.addEventListener('focusin', function (e) {
            var tgt = e.target && e.target.closest ? e.target : null;
            if (!tgt) { return; }
            var t = tgt.closest('.reason-badge');
            if (t) { show(t); }
            var h = tgt.closest('[data-rid]');
            if (h) { highlight(ridOf(h), h.getAttribute('data-hl') || '#e0e7ff'); }
          });
          document.addEventListener('focusout', function () { hide(); clearHighlight(); });
          document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') { hide(); clearHighlight(); }
          });
          document.addEventListener('scroll', hide, true);
        })();
        </script>'''


def _badge_for_slowdown(pct):
    """pct: percentage drop in concurrent workload QPS vs. isolated (higher = worse). Same 3-tier
    color language as _badge_for_ratio()'s strong tiers, just collapsed to 3 steps since slowdown
    has no "better than isolated" side to distinguish."""
    return _badge_tier(pct, _SLOWDOWN_TIERS)


def render_background_jobs_summary_html(background_jobs_data, organic_split_data=None,
                                    organic_split_random_data=None):
    """4 rows (reorganize/in_memory, reorganize/ltm, checkpoint/in_memory, checkpoint/ltm) x 4
    columns (job duration, Insert/Update/Scan QPS degradation %% while the job runs concurrently),
    all measured against one fixed 1KB x 10,000,000-record corpus (see build_background_jobs_data()).
    Deliberately not swept across the matrix's 4 scenario/value-size combos -- this table exists to
    answer one question (how much does reorganize()/checkpoint() cost, and what does it cost
    concurrent writers/readers while it runs), not to reproduce the CRUD matrix.

    Two more rows per key pattern (organic per-shard split, in_memory/ltm -- plus the random-keys
    twin when organic_split_random_*.jsonl exist) summarize organic_split_data as an
    average across every split observed in that scenario's run -- see organic_split_averages() and,
    for the full per-split detail behind the average, render_organic_split_chart_html()."""
    if not background_jobs_data and not organic_split_data and not organic_split_random_data:
        return ""
    job_labels = {
        "reorganize": "reorganize() (forced, all shards)",
        "checkpoint": "checkpoint()",
        "defragment": "defragment() (forced, one cycle)",
    }
    scenario_labels = {"in_memory": "in-memory", "ltm": "LTM"}
    out = ['<div class="overflow-x-auto"><table class="w-full text-left border-collapse text-xs">',
           '<thead><tr class="border-b border-slate-200 bg-slate-50/50">',
           '<th class="py-2 px-3 font-bold text-slate-700">Job / Scenario</th>',
           '<th class="py-2 px-3 font-bold text-slate-700">Duration (1 call)</th>',
           '<th class="py-2 px-3 font-bold text-slate-700">Insert QPS degradation</th>',
           '<th class="py-2 px-3 font-bold text-slate-700">Update QPS degradation</th>',
           '<th class="py-2 px-3 font-bold text-slate-700">Scan QPS degradation</th>',
           "</tr></thead><tbody class=\"divide-y divide-slate-100\">"]
    for job in ["reorganize", "checkpoint", "defragment"]:
        for scenario in ["in_memory", "ltm"]:
            rec = background_jobs_data.get((job, scenario))
            row_label = f'{job_labels[job]} <span class="text-slate-400">/ {scenario_labels[scenario]}</span>'
            if not rec:
                out.append(f'<tr><td class="py-2 px-3">{row_label}</td>'
                            f'<td class="py-2 px-3 text-slate-300" colspan="4">n/a</td></tr>')
                continue
            if rec.get("timed_out"):
                out.append(f'<tr><td class="py-2 px-3">{row_label}</td>'
                            f'<td class="py-2 px-3" colspan="4"><span class="text-rose-600 font-semibold">'
                            f'did not complete (timeout)</span></td></tr>')
                continue
            duration_cell = f'{rec["job_elapsed_sec"]:.3f}s'
            # Phase breakdown: only present for job=="checkpoint" (see bench_kv.cpp's
            # run_background_job_probe() -- reorganize()'s T1Only path never runs
            # checkpoint_internal(), so these fields are simply absent for that job).
            t1_ms, wal_ms, msync_ms = rec.get("t1_reorganize_phase_ms"), rec.get("wal_rotate_phase_ms"), rec.get("msync_phase_ms")
            if job == "checkpoint" and None not in (t1_ms, wal_ms, msync_ms):
                duration_cell += (f'<div class="text-[10px] text-slate-400 font-normal">T1 merge {t1_ms:.0f}ms &middot; '
                                  f'WAL rotate {wal_ms:.0f}ms &middot; msync {msync_ms:.0f}ms</div>')
            cells = [duration_cell]
            for key in ["insert_degradation_pct", "update_degradation_pct", "scan_degradation_pct"]:
                pct = rec.get(key)
                if pct is None:
                    cells.append('<span class="text-slate-300">n/a</span>')
                else:
                    cells.append(badge_html(f"{pct:.0f}%",
                                            f"{_badge_for_slowdown(pct)} font-bold w-fit"))
            out.append(f'<tr><td class="py-2 px-3">{row_label}</td>' +
                        "".join(f'<td class="py-2 px-3">{c}</td>' for c in cells) + '</tr>')

    for row_label_prefix, split_data in [("per-shard split (organic)", organic_split_data),
                                           ("per-shard split (organic, random keys)",
                                            organic_split_random_data)]:
        if not split_data:
            continue
        for scenario in ["in_memory", "ltm"]:
            row_label = f'{row_label_prefix} <span class="text-slate-400">/ {scenario_labels[scenario]}</span>'
            averages = organic_split_averages(split_data, scenario)
            if averages is None:
                out.append(f'<tr><td class="py-2 px-3">{row_label}</td>'
                            f'<td class="py-2 px-3 text-slate-300" colspan="4">n/a</td></tr>')
                continue
            avg_pause_us, avg_degr, n = averages
            duration_cell = f'{avg_pause_us / 1000:.0f}ms<div class="text-[10px] text-slate-400 font-normal">avg of {n} splits</div>'
            degr_badge = badge_html(f"{avg_degr:.0f}%", f"{_badge_for_slowdown(avg_degr)} font-bold w-fit")
            # Update/Scan degradation deliberately not measured here (permanently n/a, not a
            # missing-data gap) -- see bench_kv.cpp's kOrganicSplitProbeDurationSec comment: adding
            # concurrent Update/Scan worker pools would oversubscribe the box enough to measurably
            # slow the Insert stream that actually drives splitting, and Scan specifically reflects
            # cross-workload CPU/lock-contention interaction rather than the split's own cost.
            out.append(f'<tr><td class="py-2 px-3">{row_label}</td>'
                        f'<td class="py-2 px-3">{duration_cell}</td>'
                        f'<td class="py-2 px-3">{degr_badge}</td>'
                        f'<td class="py-2 px-3"><span class="text-slate-300">n/a</span></td>'
                        f'<td class="py-2 px-3"><span class="text-slate-300">n/a</span></td></tr>')

    out.append("</tbody></table></div>")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--template-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--title", required=True)
    parser.add_argument("--description-html", required=True)
    parser.add_argument("--env-html", required=True)
    parser.add_argument("--cell-reasons", required=False, default=None,
                        help="Optional cell_reasons.json (default: render the matrix bare)")
    parser.add_argument("--cell-stats", required=False, default=None,
                        help="Optional cell_stats.json (default: no ±CV annotations)")
    parser.add_argument("--headline-error-bars", action="store_true",
                        help="Draw headline whiskers (main-run value ± headline SD, n=3) on "
                             "max-thread points with results_*_headline.json coverage")
    args = parser.parse_args()

    html = args.template.read_text()

    raw_data = build_raw_data(args.report_dir)
    timeline_data = build_timeline_data(args.report_dir)
    forced_events_data = build_forced_events_data(args.report_dir)
    background_jobs_data = build_background_jobs_data(args.report_dir)
    organic_split_data = build_organic_split_data(args.report_dir)
    organic_split_random_data = build_organic_split_data(args.report_dir, prefix="organic_split_random")
    winners_rows = compute_winners_matrix(raw_data)
    cell_reasons = load_cell_reasons(args.cell_reasons)
    cell_stats = load_cell_stats(args.cell_stats)

    html = html.replace(args.template_id, args.report_id)

    def replace_block(html, start_marker, end_marker, new_js_value_expr):
        start = html.index(start_marker)
        end = html.index(end_marker, start)
        return html[:start] + new_js_value_expr + html[end:]

    html = replace_block(
        html, "const rawData = {", "const timelineData = {",
        "const rawData = " + json.dumps(raw_data, indent=2) + ";\n    "
        + ("const headlineErrorBars = " + json.dumps(build_headline_data(args.report_dir), indent=2)
           + ";\n    " if args.headline_error_bars else "")
    )
    html = replace_block(
        html, "const timelineData = {", "const workloads =",
        "const timelineData = " + json.dumps(timeline_data, indent=2) + ";\n    " +
        "const forcedEventsData = " + json.dumps(forced_events_data, indent=2) + ";\n    "
    )
    # Detail charts iterate this template-static list: keep it in sync with WORKLOADS so
    # newly covered workloads (e.g. YCSB_E aggregates) render without template edits.
    html, n_workloads = re.subn(r"const workloads = \[[^\]]*\];",
                                lambda m: "const workloads = " + json.dumps(WORKLOADS) + ";", html, count=1)
    if n_workloads == 0:
        raise ValueError("template lacks expected JS const: workloads")

    # Header title / links / description. Regex-based (not an exact previous-string match): the
    # template chain mutates its own <title>/<h1> text on every generation (each report's own
    # --title becomes baked-in literal text, not a stable marker), so matching against a fixed
    # assumed-previous string would silently no-op once any report in the chain used a custom
    # title.
    html = re.sub(r"<title>.*?</title>", lambda m: f"<title>{args.title}</title>", html, count=1)
    html = re.sub(
        r'(<h1 class="text-3xl font-bold tracking-tight text-slate-900">).*?(</h1>)',
        lambda m: m.group(1) + args.title + m.group(2),
        html, count=1,
    )

    download_links_start = html.index('<div class="flex flex-wrap gap-2">')
    download_links_end = html.index("</div>", download_links_start)
    links = []
    for fname, label in [
        (f"results_in_memory_8B.json", "In-mem 8B"),
        (f"results_in_memory_1KB.json", "In-mem 1KB"),
        (f"results_ltm_1KB.json", "LTM 1KB"),
        (f"results_ltm_64KB.json", "LTM 64KB"),
        (f"results_in_memory_8B_headline.json", "Headline err bars, In-mem 8B"),
        (f"results_in_memory_1KB_headline.json", "Headline err bars, In-mem 1KB"),
        (f"results_ltm_1KB_headline.json", "Headline err bars, LTM 1KB"),
        (f"results_ltm_64KB_headline.json", "Headline err bars, LTM 64KB"),
        (f"background_jobs_in_memory.jsonl", "Background Jobs, In-Memory"),
        (f"background_jobs_ltm.jsonl", "Background Jobs, LTM"),
    ]:
        if not (args.report_dir / fname).exists():
            continue
        links.append(
            f'<a href="../{args.report_id}/{fname}" download class="inline-flex items-center gap-1 text-xs text-slate-500 bg-slate-100 rounded px-2.5 py-1.5 hover:bg-slate-200">'
            f'<i data-lucide="download" class="w-3.5 h-3.5"></i> Raw {"JSONL" if fname.endswith("jsonl") else "JSON"} ({label})</a>'
        )
    html = html[:download_links_start] + '<div class="flex flex-wrap gap-2">\n          ' + "\n          ".join(links) + "\n        " + html[download_links_end:]

    desc_start = html.index('<p class="text-slate-500 text-sm max-w-3xl">')
    desc_end = html.index("</p>", desc_start) + len("</p>")
    # Consume any orphaned extra closings a previous generation left behind, so repeated
    # regenerations converge instead of accumulating "</p>" cruft.
    while html[desc_end:desc_end + len("</p>")] == "</p>":
        desc_end += len("</p>")
    html = html[:desc_start] + f'<p class="text-slate-500 text-sm max-w-3xl">{args.description_html}</p>' + html[desc_end:]

    # Environment section: replace the <ul> under "Environment" heading.
    env_marker = '<i data-lucide="cpu" class="w-3.5 h-3.5"></i> Environment\n        </h2>\n        <ul class="space-y-1 text-slate-500">'
    env_start = html.index(env_marker) + len(env_marker)
    env_end = html.index("</ul>", env_start)
    html = html[:env_start] + args.env_html + html[env_end:]

    # Detail-chart store set: older templates predate some backends, so sync the chart
    # scaffolding from this script's single source of truth instead of trusting the template.
    # These three are load-bearing (datasets iterate variantOrder) -- fail loudly if the
    # template ever renames them, rather than silently dropping stores again.
    for pattern, value in [
        (r"const colors = \{[^}]*\};", "const colors = " + json.dumps(COLORS) + ";"),
        (r"const rivalStores = \[[^\]]*\];", "const rivalStores = " + json.dumps(RIVAL_STORES) + ";"),
        (r"const variantOrder = \[[^\]]*\];", "const variantOrder = " + json.dumps(VARIANT_ORDER) + ";"),
    ]:
        html, n = re.subn(pattern, lambda m: value, html, count=1)
        if n == 0:
            raise ValueError(f"template lacks expected JS const for pattern: {pattern}")
    html, n = re.subn(r"const variantMarkers = \{[^}]*\};",
                      lambda m: "const variantMarkers = " + json.dumps(VARIANT_MARKERS) + ";", html, count=1)
    if n == 0:
        print("warning: template lacks variantMarkers const -- markers fall back to circle")

    if args.headline_error_bars:
        # Inline whisker plugin (no new dependency) plus per-point SD plumbing. Each step is
        # warn-only, and all three are idempotent (guarded by their own markers): a template
        # predating the expected scaffolding still generates a bare report, and regenerating
        # over an already-patched report changes nothing.
        anchor = "function makeChartConfig(valSize, op) {"
        if anchor not in html:
            print("warning: template lacks makeChartConfig -- headline error bars not drawn")
        elif HEADLINE_ERRORBAR_PLUGIN_JS.strip().splitlines()[0] in html:
            print("note: template already carries the headline plugin -- skipping injection")
        else:
            html = html.replace(anchor, HEADLINE_ERRORBAR_PLUGIN_JS + "\n    " + anchor, 1)
        if "headlineSd:" in html:
            print("note: template already attaches headline SD -- skipping")
        else:
            old_dataset, n_dataset = re.subn(
                r"(\s+)data: opData\[variantName\],",
                lambda m: (m.group(0) + "\n" + m.group(1)
                           + "headlineSd: (((headlineErrorBars[valSize] || {})[op] || {})[variantName] || {}),"),
                html, count=1)
            if n_dataset == 0:
                print("warning: template lacks opData dataset -- headline SD not attached")
            else:
                html = old_dataset
        if "const _hsd =" in html:
            print("note: template tooltips already show headline SD -- skipping")
        else:
            old_tip, n_tip = re.subn(
                r"const fv = formatVal\(v\);",
                lambda m: (m.group(0)
                           + "\n                  const _hsd = (((headlineErrorBars[valSize] || {})[op] || {})[label] || {})[context.dataIndex];"
                           + "\n                  const _herr = (_hsd != null && _hsd > 0) ? ` ±${formatVal(_hsd)} (n=3)` : '';"),
                html, count=1)
            if n_tip == 0:
                print("warning: template lacks tooltip formatVal -- headline SD not shown in tooltips")
            else:
                html = old_tip
                n_lbl = html.count(" ${label}: ${fv}/s")
                html = html.replace(" ${label}: ${fv}/s", " ${label}: ${fv}/s${_herr}")
                if n_lbl == 0:
                    print("warning: template lacks tooltip label returns -- headline SD not shown in tooltips")

    # Stacking Variants legend: the currently measured set only.
    stacking_marker = "Stacking Variants"
    stacking_idx = html.find(stacking_marker)
    if stacking_idx == -1:
        print("warning: template lacks Stacking Variants section -- legend not updated")
    else:
        ul_start = html.index('<ul class="space-y-1.5 text-slate-500 list-disc pl-4">', stacking_idx)
        ul_end = html.index("</ul>", ul_start) + len("</ul>")
        legend_items = "\n".join(
            f'          <li><strong>{label}</strong>: {desc}</li>' for label, desc in VARIANT_DESCRIPTIONS)
        html = (html[:ul_start]
                + '<ul class="space-y-1.5 text-slate-500 list-disc pl-4">\n'
                + legend_items + '\n        </ul>'
                + html[ul_end:])

    # Stale history phrasing in the Workloads legend and per-tab Scan overviews.
    html = html.replace("(旧レポートにあった reorganize モード分割は撤去済み)", "")
    html = html.replace(
        "。現行 bench_kv は reorganize 状態を単一の Scan ベンチマークとして測定する形に統合済み"
        "(旧レポートの Scan_WithReorg / Scan_T1T2Reorg モード分割は撤去済み)。", "。")
    html = html.replace("一様分布で選ばれたレンジのスキャン(同上)。", "一様分布で選ばれたレンジのスキャン。")

    # Overall Notes & Annotations section and every per-chart memo textarea are retired.
    notes_idx = html.find("Overall Notes")
    if notes_idx == -1:
        print("warning: template lacks Overall Notes section -- nothing to remove")
    else:
        sec_start = html.rfind("<section", 0, notes_idx)
        sec_end = html.index("</section>", notes_idx) + len("</section>")
        html = html[:sec_start] + html[sec_end:]
    html, memo_n = re.subn(r'<textarea id="memo-[^"]*"[^>]*></textarea>', "", html)
    if memo_n == 0:
        print("warning: template has no memo textareas -- nothing to remove")
    for memo_fn in ["    function saveMemo(key, val) {\n      try { localStorage.setItem(",
                     "    function initMemos() {"]:
        fn_start = html.find(memo_fn)
        if fn_start == -1:
            print(f"warning: template lacks memo JS for {memo_fn.split('(')[0].strip()} -- skipping")
            continue
        fn_end = html.index("\n    }\n", fn_start) + len("\n    }\n")
        html = html[:fn_start] + html[fn_end:]
    html = html.replace("    initMemos();\n", "")

    # Winners matrix table body.
    tbody_marker = '<tbody class="divide-y divide-slate-100">\n<tr class="hover:bg-indigo-50/10 transition-colors">'
    tbody_start = html.index(tbody_marker)
    tbody_content_start = tbody_start + len('<tbody class="divide-y divide-slate-100">\n')
    tbody_end = html.index("</tbody>", tbody_content_start)
    html = html[:tbody_content_start] + render_winners_matrix_html(winners_rows, cell_reasons, cell_stats) + "\n" + html[tbody_end:]

    # Reason legend: inside the Winners section, right below the matrix table.
    # Strip any block a previous run left behind first (idempotent reruns).
    legend_html = render_cell_reasons_legend(cell_reasons, winners_rows)
    if legend_html:
        start_marker, end_marker = "<!-- cell-reasons-legend:start -->", "<!-- cell-reasons-legend:end -->"
        while start_marker in html and end_marker in html:
            block_start = html.index(start_marker)
            block_end = html.index(end_marker) + len(end_marker)
            rest = html[block_end:]
            # Consume the single separator ("\n" + indent) this step appends after
            # every insert, so strip+reinsert round-trips byte-identically.
            if rest.startswith("\n"):
                rest = rest[1:]
                rest = rest[len(rest) - len(rest.lstrip(" \t")):]
            html = html[:block_start] + rest
        tbody_end = html.index("</tbody>", tbody_content_start)
        winners_section_close = html.index("</section>", tbody_end)
        html = html[:winners_section_close] + legend_html + "\n      " + html[winners_section_close:]

    # Tier 1 / Tier 1+2 reorganize-duration summary grid: dropped as 100% redundant with the
    # per-tab "Reorganize Duration vs. Corpus Size" chart (removed once, so a no-op if the
    # template already lacks it).
    grid_open_marker = '      <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">'
    grid_close_marker = '</section>\n      </div>\n    </div>\n'
    if grid_open_marker in html:
        grid_start = html.index(grid_open_marker)
        grid_close_start = html.index(grid_close_marker, grid_start)
        # Keep the trailing "    </div>\n" (tab-summary's own closing tag); only the grid + its
        # two <section> children are being removed.
        grid_end = grid_close_start + len('</section>\n      </div>\n')
        html = html[:grid_start] + html[grid_end:]

    # Corpus-Size Invariance / Churn-Ratio Scaling summary sections: replace the existing
    # section's table in place if the template already has it (added by a prior report round),
    # otherwise insert a fresh section right after the Workload Winners section closes.
    section_open_marker = '<section class="bg-white rounded-xl shadow-sm border border-slate-100 p-6 space-y-4">'

    # heading is matched only inside its own <h3> tag (see h3_marker below), not as a bare
    # substring against the whole document: a section's title/description text (in this function
    # or elsewhere already in the template) can name another section's heading in prose, which a
    # plain html.find(heading) would match inside that other section's own body -- scoping the
    # search to the exact <h3>...</h3> wrapper this function itself always writes avoids that
    # entirely, rather than requiring every future caller to remember not to mention another
    # section's name in prose.
    def upsert_section(html, heading, icon_bg, icon_text, icon_name, title, description_html, table_html):
        if not table_html:
            return html
        section_html = f'''
      <section class="bg-white rounded-xl shadow-sm border border-slate-100 p-6 space-y-4">
        <div class="flex items-center gap-3">
          <div class="p-2 {icon_bg} {icon_text} rounded-lg">
            <i data-lucide="{icon_name}" class="w-6 h-6"></i>
          </div>
          <div>
            <h3 class="text-base font-bold text-slate-900">{title}</h3>
            <p class="text-xs text-slate-500">{description_html}</p>
          </div>
        </div>
        {table_html}
      </section>
'''
        h3_marker = f'<h3 class="text-base font-bold text-slate-900">{heading}</h3>'
        heading_idx = html.find(h3_marker)
        if heading_idx != -1:
            # Remove the existing section first (wherever it currently sits) rather than replacing
            # its content in place -- so this call's position in the code below, not wherever some
            # earlier report round happened to leave it, determines where it ends up. Replacing in
            # place instead would let a re-run update a section's content but never reorder it
            # relative to another section.
            section_start = html.rindex(section_open_marker, 0, heading_idx)
            # Also consume any whitespace-only text immediately preceding the marker: without this,
            # a re-run's fixed-format replacement (below) leaves the *previous* run's own leading
            # indentation orphaned in place, accumulating a little more on every regeneration
            # instead of converging.
            ws_start = section_start
            while ws_start > 0 and html[ws_start - 1] in " \t\n":
                ws_start -= 1
            section_end = html.index("</section>", heading_idx) + len("</section>")
            html = html[:ws_start] + html[section_end:]
        # Insert right after the Workload Winners section closes -- recomputed fresh against
        # (possibly just-shrunk) html, so each call in a single run's sequence lands at the same
        # fixed point ahead of whatever an *earlier* call already inserted there. That gives LIFO
        # ordering across calls in one run (the last-called section ends up closest to Winners);
        # the call sites below rely on this to control final section order deliberately.
        winners_section_close = html.index("</section>", tbody_content_start) + len("</section>")
        return html[:winners_section_close] + section_html + html[winners_section_close:]

    # Chart sections' upsert_section calls come *before* Background Jobs' below on purpose: each
    # "not yet present" insertion lands at the same fixed point (right after Workload Winners),
    # ahead of whatever a previous call already put there -- so calling these first, then
    # Background Jobs second, produces the intended final order (Winners, Background Jobs summary
    # table, detail charts at the bottom) rather than the reverse. Random before monotonic puts
    # the random detail section last.
    if organic_split_random_data:
        html = upsert_section(
            html,
            heading="Organic Per-Shard Splits (random keys)",
            icon_bg="bg-indigo-50", icon_text="text-indigo-600", icon_name="split",
            title="Organic Per-Shard Splits (random keys)",
            description_html=(
                "Pause durations with random keys spreading writes over all shards."
            ),
            table_html=render_organic_split_chart_html(organic_split_random_data, chart_suffix="-random"),
        )
    html = upsert_section(
        html,
        heading="Organic Per-Shard Splits",
        icon_bg="bg-indigo-50", icon_text="text-indigo-600", icon_name="split",
        title="Organic Per-Shard Splits",
        description_html=(
            "Writer-visible pause per automatic per-shard split during 90s sustained inserts."
        ),
        table_html=render_organic_split_chart_html(organic_split_data),
    )

    # Stale predecessor section from the template chain (same topic, older heading):
    # the upsert below only replaces exact-heading matches, so remove it outright.
    # Warn-only (idempotent reruns must not fail).
    stale_heading = "Background Jobs: reorganize() / checkpoint()"
    stale_idx = html.find(f">{stale_heading}</h3>")
    if stale_idx == -1:
        print(f"warning: template lacks stale section {stale_heading} -- nothing to remove")
    else:
        stale_start = html.rfind("<section", 0, stale_idx)
        stale_end = html.index("</section>", stale_idx) + len("</section>")
        html = html[:stale_start] + html[stale_end:]

    html = upsert_section(
        html,
        heading="Background Jobs: reorganize() / checkpoint() / defragment()",
        icon_bg="bg-rose-50", icon_text="text-rose-600", icon_name="swords",
        title="Background Jobs: reorganize() / checkpoint() / defragment()",
        description_html=(
            "Fixed reference point (1KB values, 10,000,000 records; in-memory unconstrained, "
            "LTM cgroup-constrained to the same memory budget as the rest of the suite) -- "
            "not swept across the CRUD matrix's 4 scenario/value-size combos. Duration is a "
            "single call in isolation; the three degradation columns are the percentage drop in "
            "concurrent Insert/Update/Scan QPS while that one call runs, measured over a "
            "matched-duration window on both sides (see run_background_jobs_probe.sh). "
            "reorganize() forces every shard through a synchronous merge in one call -- a manual "
            "escape hatch (pre-backup flush, capacity planning), not what happens during ordinary "
            "operation. The last rows summarize the organic per-shard split alternative that "
            "actually runs day to day (averaged across every split observed in that run; see the "
            "detail section further down for the full breakdown and charts)."
        ),
        table_html=render_background_jobs_summary_html(background_jobs_data, organic_split_data,
                                                         organic_split_random_data),
    )

    args.out.write_text(html)
    print(f"Wrote {args.out} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
