# 2026-09-20 MGLRU-off安全性行列 (LTM VMemKV-only全セル)

MGLRU感度スパイク(`20260919_mglru_sensitivity.md`)でVMemKVの改善が確定した一方、
RocksDB YCSB-E −9%の開示義務が生じた。本測定は、同一箱・同一コードでON→OFFの
2ランを行い、**VMemKV側に系統的なOFF退行がないか**を全セルで確認した安全検査である。
正本チャートへのマージは行わない(条件の異なる数値を混ぜない)。

## 手法

- インスタンス: AWSスポット i4i.8xlarge 1台に2ラン逐次実行(box差排除、保守的なON→OFF順)。
  THP `madvise`、swappiness 100、32GB NVMe swap。
- ビルド: rev `2af1d8f`(ページ内完結配置+`lru_gen_enabled` context記録込み)、
  `-O3 -DNDEBUG -march=native -fno-omit-frame-pointer`。
- 対象: LTM VMemKV-only全行列(5 variant × Value 1KB/64KB × 全Op、330セル/ラン)。
  in-memoryは除外した(圧力なしでは置換は働かずON/OFF同一が構成上自明のため)。
  production手法に準拠: variant単位の独立cgroup scope + 直前`drop_caches`、
  `--benchmark_min_time=5.0s`。MGLRU状態は`/sys/kernel/mm/lru_gen/enabled`の
  読み値で確認した(ON=`0x0007`、OFF=`0x0000`)。
- 行列JSONのcontextには`lru_gen_enabled`が自動記録されている。

## 結果1: 1KB read headline(t16/t32、全variant) — OFF勝利または中立、退行ゼロ

- Get-Hit Zipf: +18〜+37%(全5 variant、両スレッド)。系統的勝利。
- Get-Hit Uniform: t32は全て1.04x以上。t16に0.83xが1点あるが他は0.94〜1.10x。
- Scan Zipf: +9〜+36%。Scan Uniform: 1.01〜1.14x。YCSB-E: +6〜+21%(全variant)。
- 全体330セルの中央値1.009、平均1.072、1.1x超82セル、0.9x割れ30セル。

## 結果2: 64KB — 系統効果なし

±10%級が小絶対値セル中心に両方向へ分布し、variant間で方向が一致しない
(例: Get-Hit Zipf t32は0.85x〜1.22xに分散)。ノイズ署名であり、系統的なOFF効果は
認められない。t32-Zipfの旗2点(0.85x/0.89x)は単独隔離の再現取りで方向が反転し棄却した。
なおReadRandom-64KBの絶対値が他variantの約1/4なのは設計通りである
(大レコードをreadaheadなし`base`に固定するablation、low_level_design.md 7.5節)。

## 結果3: 書き込み系 — 変動が支配的

Insert/Update/Deleteに±2〜3xが両方向へ存在する。WAL fsync律速+背景maintenance+
swapの既知変動であり、MGLRU無相関と判断した(両符号の存在が根拠)。書き込み精度が
要る場合はheadline-repsを別途取ること。

## 方法論上の副産物: 群内ウォームによる水準差

行列内セル(同一scopeを66セルが共有、後段ほど温かい)と単独隔離セル(完全cold)では
64KB-Zipf-t32で約5倍の水準差が出た。ON/OFF比較自体は同順序・同条件のため有効だが、
絶対値の横比較(行列値 vs 単独値)はできない。以後の感度測定は単独隔離に統一すること。

## 結論

VMemKV側に系統的なOFF退行は存在しない。OFF採用の残る論点はRocksDB YCSB-E −9%の
開示のみであり、政策判断に委ねる。

## 生データ

`/tmp/opencode/ltm_matrix/` — ON/OFF各5群のbenchmark JSON(計10ファイル、660セル)。
`m_on_*`/`m_off_*`の命名で条件を区別する(正本`benchmark_results/`とは別管理)。
手法詳細と単独セル真値は`20260919_mglru_sensitivity.md`を参照。
