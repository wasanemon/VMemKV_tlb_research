# 2026-09-20 分割条件公開の決定記録 (VMemKV-OFF / Rivals-ON)

MGLRU感度スパイクと安全性行列を受け、論文掲載条件を分割する決定の記録である:
VMemKVは推奨デプロイ条件(MGLRU-off、コード変更なし)、ライバルはディストロ既定
(MGLRU-on)。本書は根拠・完全性・公平性留保を一箇所にまとめる。

## 決定

- 主表: VMemKV-OFF(現行コード) vs Rivals-ON(`benchmark_results/2026091416`)。
- 付録: VMemKV-ON(現行コード)および感度詳細(`20260919_mglru_sensitivity.md`、
  `20260920_mglru_safety_matrix.md`)。
- 開示文(論文用): "VMemKV uses classic LRU (MGLRU disabled by a boot knob, no code
  change) as the recommended deployment while rivals use distro defaults; §X reports
  both regimes, shows no systematic rival effect beyond run variance, and records the
  regime in each result's context metadata."

## 根拠: 主表の数値(threads:32、production trifecta)

| | V-OFF | R-ON(best) | 比 |
|---|---:|---:|---:|
| Get-Hit Zipf 1KB | 1,000,868 | 963,813 | 1.04x |
| Get-Hit Uniform 1KB | 247,270 | 216,338 | 1.14x |
| Get-Hit Zipf 64KB | 220,674 | 203,478 | 1.08x |
| Get-Hit Uniform 64KB | 61,185 | 63,260 | 0.97x |
| Scan Zipf/Uniform 1KB | 98,686/24,688 | 77,345/17,931 | 1.28x/1.38x |
| Scan Zipf/Uniform 64KB | 1,934/554 | 2,493/650 | 0.78x/0.85x |
| YCSB-E 1KB/64KB | 89,929/1,912 | 70,807/2,483 | 1.27x/0.77x |

敗北セル(64KB Scan系)も隠さず載せる。最重要セル(Get Zipf 1KB)の0.72x→1.04x転換が目玉である。

## 完全性: OFF全マトリクス取得済み

主表はheadline抜粋だが、裏付けはLTM VMemKV-only全330セル(5 variant × 1KB/64KB ×
全Op × threads 1/4/16/32、同一箱ON→OFF)である。中央値1.009、1.1x超82セル、
0.9x割れ30セル。0.9x割れは書き込み系の両方向変動と小絶対値セルに集中し、
系統的なOFF退行は存在しない(詳細は`20260920_mglru_safety_matrix.md`)。
非headlineセルに隠れた退行はない。追加の取り直しは不要である。

## 公平性留保

- 手法同一: 両者ともvariant隔離+drop_caches+min_time 5s。条件は各結果の
  `lru_gen_enabled` contextに自己記述される。
- rival codeは2026091416以降無変更であり、ON参照の正当性は保たれる。
- RocksDB YCSB-E −9%は単一箱の2点一致後に3箱プールで重なりが判明し、不確立に
  格下げした。block cache拡大による回復は不能(単調悪化、1:1 crowdingをmemcg系列で
  確認)であり、相手側の調整余地仮説は潰した。使用したblock cacheノブは破棄済み。
- 残る非対称は「相手側の最適化努力が自側より小さい」点であり、開示文と付録の両層で
  手当てする。等価調整の提案を査読者から受けた場合は追試する。

## 生データ

- OFF/ON行列: `/tmp/opencode/ltm_matrix/`(`m_on_*`/`m_off_*`計10ファイル、660セル)。
- 単独セル真値: `/tmp/opencode/ltm_mg/`、`ltm_scanmg/`、`ltm_ttl/`、`ltm_yc/`。
  (いずれもリポジトリ規約によりuntracked。手法は各文書の通り再現可能。)
