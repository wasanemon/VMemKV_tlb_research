# 2026-09-19 MGLRU感度スパイク (LTM/1KB Get/Scan/YCSB-E)

T2ページ内完結配置(`20260919_t2_page_containment_ab.md`)の後に残ったZipf差
(reads/op 0.318 vs BlobDB 0.200、滞留率)を、VMemKVのコード変更なしに埋められるかを
カーネル側置換(MGLRUのon/off)で試した感度測定の記録である。

## 前提の訂正

Ubuntu既定でMGLRUは**既に有効**(`/sys/kernel/mm/lru_gen/enabled` = `0x0007`)であり、
productionの全結果(`benchmark_results/2026091416`等)を含む過去の全計測はMGLRU=ON条件である。
本スパイクはON→OFF→ONのABBAで比較し、`n`/`y`の書き込みで切り替えた(再読で確認)。

## 手法

- インスタンス: AWSスポット i4i.8xlarge(ap-northeast-1d)、THP `madvise`、swappiness 100。
  3日程を別個体で実施(Get спайк / Scan+YCSB-E / 再現取り)。
- ビルド: `-O3 -DNDEBUG -march=native -fno-omit-frame-pointer`、rev `58021e6`(ページ内完結込み)。
- LTM条件: `MemoryHigh=1GiB`/`MemoryMax=2GiB`/`MemorySwapMax=1TiB`の独立cgroup scopeを
  セルごとに新規作成し、直前に`sync && drop_caches`。コーパスは8,259,552キー/8.59GB
  (target_ratio 8.0)。Get/Scanは`--benchmark_min_time=8.0s`、YCSB-Eは30秒固定。
- 計装: `perf stat`(task-clock/context-switches/minor/major-faults)、`/usr/bin/time -v`、
  `/proc/diskstats`+`/proc/vmstat`前後差分。

## 結果1: Get-Zipf t32 (ABBA)

| ReadRandom Zipf t32 | items/s | reads/op | bytes/op |
|---|---:|---:|---:|
| MGLRU ON(1) | 668,292 | 0.365 | 2832 |
| MGLRU OFF | 950,228 | 0.231 | 1736 |
| MGLRU ON(2) | 742,460 | 0.314 | 2265 |

ON平均705kに対しOFF 950kで**+35%**。ON側2本のばらつきを大きく超え、順序効果でもない
(最後のONが中間のOFFを下回る)。OFF値はBlobDB(978〜1006k)とほぼ互角である。
Uniform t32はON/OFF同一(234k/234k)で、置換が効くのは局所性のある負荷だけという予測通りである。
BlobDB Zipfは+2.8%のみ(noise圏)である。

## 結果2: Scan/YCSB-E t32

| セル | MGLRU ON | MGLRU OFF | 変化 |
|---|---:|---:|---|
| VMemKV Scan Zipf | 69,681 / 80,683(再) | 98,949 / 100,723(再) | **+33%** |
| VMemKV Scan Uniform | 24,251 | 24,111 | 同一 |
| VMemKV YCSB-E | 74,568 / 75,229(再現取りで再) | 87,903 / 89,422(再) | **+18〜19%で再現** |
| RocksDB Scan Zipf | 76,669 / 69,996(再) | 68,696 / 69,928(再) | 効果なし(範囲重複) |
| RocksDB Scan Uniform | 18,759 / 16,219(再) | 17,218 / 16,504(再) | 効果なし |
| RocksDB YCSB-E | 74,817 / 75,058(再) | 67,476 / 68,403(再) | **−9%で再現**(両回ともtight) |

Scan-ZipfのOFF×2はtightに一致し、機構はGetと異なる可能性がある
(reads/op差は小さくbytes/op差が主因: 80k→46k)。断定しない。
RocksDB Scanの−10%懸念は再現取りで解消したが、RocksDB YCSB-Eの−9%は2回ともtightに
再現し本物と判断する。

## 解釈の境界

- 効果はVMemKVのfault駆動Zipfパスに特異的である。Uniform・BlobDB・RocksDB Scanには出ない。
- 機構(世代バッチングの昇格遅延か、faultストーム下のスキャンコストか)は未特定である。
- MGLRU-offはVMemKVを全方向で改善する一方、RocksDB YCSB-Eに−9%のハンディを与える。
  同一箱での比較自体は条件対称だが、相手を不利にする条件の正本採用はheadlineの一部を
  条件由来にする。**正本条件は既定ONのまま維持し、OFFは感度分析として併記する**。

## 生データ

`implementation/vmemkv/benchmark/logs/mglru_sensitivity/` —
3日程分のセル単独JSON、`perf_stat`、`diskstats`/`vmstat`前後差分、`time -v`出力。
(当ディレクトリはリポジトリ規約によりuntracked。手法は本書の通り再現可能。)
