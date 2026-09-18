# 2026-09-19 T2ページ内完結配置のA/B実測 (LTM/1KB Get/Hit)

LTM/1KB Get/Hit(threads:32)でVMemKVがRocksDB-BlobDBに劣後する要因のうち、
T2レコードのページ跨ぎ(2ページにまたがるcold readが2回の個別I/Oになる)を除去する
配置変更のパッチと、そのAWS A/B実測結果の記録である。

## 背景

`benchmark_results/2026091416`では、LTM/1KB Get/Hit/threads:32において
VMemKV-Bloom-T1InlineValue-ReadRandomがRocksDB-BlobDB比でZipf 0.72x・Uniform 0.85xと劣後していた。
同条件の標的プロファイリング(2026-09-18〜19、AWS i4i.8xlarge)では以下が確定した:

- disk read time/opの順位はスループットの順位と完全逆転一致する。律速はディスク読み時間そのものである。
- VMemKVのmajor faults数≒disk reads数であり、1 faultが1回の個別I/Oに対応する。
- master `t1chk`全8,259,552エントリの国勢調査で、**21.0%のレコードがページを跨ぐ**。
  1KBレコード(約1064B)がランダムオフセットに置かれるためである。

## 手法

- インスタンス: AWSスポット i4i.8xlarge(ap-northeast-1d)。THP `madvise`、swappiness 100、32GB NVMe swap。
- ビルド: `-O3 -DNDEBUG -march=native -fno-omit-frame-pointer`。修正前=rev `b5bd271`、
  修正後=同rev+本パッチ(作業ツリー)。
- LTM条件: `MemoryHigh=1GiB`/`MemoryMax=2GiB`/`MemorySwapMax=1TiB`の独立cgroup scopeを
  セルごとに新規作成し、直前に`sync && drop_caches`。コーパスは8,259,552キー/8.59GB
  (target_ratio 8.0)。`--benchmark_min_time=8.0s`。
- 計装: `perf stat`(task-clock/context-switches/minor/major-faults、`sudo`経由)、
  `/usr/bin/time -v`、`--benchmark_filter`はセル単独正規表現8本
  (4 store × Zipf/Uniform × threads:32)。
- ライバル(RocksDB/RocksDB-BlobDB)は前後2回で±3%以内に再現し、測定の安定を確認した。

## パッチの内容

`T2FlatFile::append_default`(`src/t2_flat_file/t2_flat_file.cpp`)の確保を単一
`fetch_add`からCASループに変え、配置を決定する。規則は2分岐のみである:

- `required <= 4096`のレコードはページ境界を跨がない(跨ぐ場合は次ページ先頭へスキップ)。
- `required > 4096`のレコードは開始オフセットを4KBアラインする
  (従来8Bアラインのため64KBはほぼ必ず17ページに触れていた)。

全追記経路(insert/out-of-place update/WAL replay/bulk load/defrag移設)がこの関数を
通るため、他箇所の変更は不要である。in-place updateは移動しないため不変性を自動維持する。
スキップ空白はliveレコードを含まないため`punch_if_occupied`の契約に影響しない。
空白はlive集計(`note_replace`のhint基準)に載らないため、`bytes_used`だけが膨らみ、
defrag発火はその分だけ早まる(有界のコスト、効果は後述)。容量超過時は従来より厳密に、
消費せずthrowする。

## 結果1: スループット(threads:32、単独セル真値)

| セル | 修正前 | 修正後 | 変化 |
|---|---:|---:|---|
| Baseline Zipf | 538,806 | 611,158 | +13.4% |
| Baseline Uniform | 158,580 | 182,504 | +15.1% |
| ReadRandom Zipf | 703,207 | 746,625 | +6.2% |
| ReadRandom Uniform | 183,767 | 227,635 | +23.9% |
| RocksDB Zipf / Uniform | 775,311 / 159,347 | 759,255 / 164,495 | noise |
| BlobDB Zipf / Uniform | 972,163 / 221,052 | 978,339 / 223,158 | noise |

- **Uniform t32: ReadRandom 227k > BlobDB 223k > RocksDB 164k**。本セルでのBlobDB初勝利である。
- **Zipf t32: 746k vs RocksDB 759k(−1.7%、統計的タイ)**。BlobDB 978kとの差は残り、
  読み滞留率の差(後述の#3領域)として切り分けられた。

## 結果2: 機構の確認

| セル | reads/op 前→後 | bytes/op 前→後 | disk us/op 前→後 |
|---|---:|---:|---:|
| Baseline Zipf | 0.511 → 0.407 | 3284 → 2847 | 88.2 → 69.9 |
| Baseline Uniform | 1.305 → 1.118 | 9004 → 8532 | 224.0 → 192.3 |
| ReadRandom Zipf | 0.334 → 0.318 | 2179 → 2324 | 57.6 → 55.5 |
| ReadRandom Uniform | 1.102 → 0.874 | 7624 → 6762 | 189.5 → 149.9 |

- Uniformのreads/op改善(−21%)は二重fault除去の理論値と一致する。
- span国勢調査(8Mエントリ`t1chk`パース): **21.02% → 0.00%**。
- major faults≒disk readsの関係は維持され、1 fault=1 I/Oのままである。

## 結果3: フットプリント実測

`t1chk`のhint合計と`max(offset+len)`の残差による前後同一手法の比較:

- 修正前残差: 51MB(6.4B/key、孤児レコードのみ)。
- 修正後残差: 1903MB(238B/key)。差分≈1852MBがページスキップ空白であり、
  8.6GBコーパスに対して**約+21%**である。
- 事前見積もり(+13%、一様分布仮定)を上回った原因は、等間隔strideとスキップの共振である:
  スキップが残差を0にリセットするため短周期(例: 残差{0,1064,2128,3192}の繰返し、
  4件に1回904B浪費≈226B/件)に落ち着く。実測230B/件と整合する。

このため修正後の計測は実効圧力が約21%高い条件(8.6GB→約10.4GB相当)で行われており、
比較としてはVMemKVに厳しい側への変化である。勝利は保守的条件でのものである。

## 生データ

`implementation/vmemkv/benchmark/logs/t2_page_containment_ab/` — 前後16セル分の
benchmark JSON、`perf_stat`、`diskstats`/`vmstat`前後差分、`time -v`出力(計112ファイル)。

## 次の課題

Zipf t32のBlobDB差(−24%)はreads/opの差(0.318 vs 0.200、滞留率)に残り、本パッチの
対象外である。ホットページ地図(`fincore`)の取得を先行し、covering拡張・madvise誘導・
高密度配置のいずれで埋めるかを選択する。
