# 2026-09-20 Scan大レコードpread試作の棄却記録

分割条件レポートで残った3敗セル(Scan Zipf/Uniform 64KB、YCSB-E 64KB)の解消を狙い、
Scanの大レコード読みをGetと同型のmincore-gated preadに寄せた試作を同一箱アンカーつき
A/Bで評価し、棄却した記録である。再試行防止のため機構まで残す。

## 背景

敗北3セルの待機率は94%以上であり、差はcold-miss単価にあるとみた。64KB=16ページに対し
VMemKV-Scanの大レコード経路はmmap固定(最大16 fault)であり、Get側のmincore-gated preadに
相当する経路がScanには存在しない(low_level_design.md 7.5)。cold-miss単価の1 pread化が狙いである。

## 試作内容(ツリーから除去済み)

`flush_scan_batch`のbase高速パスを`try_read_scan_record`に置換した:

- 小レコード: 従来通りblind mmap読み(変更なし)。
- 大レコード: 常駐確認でhotはmmap span、coldはbatch arenaへのbounded pread。
  pread結果はoffset記録のみとし、後続slotのarena拡張に対応した。
- いずれの失敗も既存seqlock経路へ流す。単体試験(64KB値のbase/tail exactness)と
  フルスイートは通過した。

## 結果: 同一箱で悪化(棄却)

MGLRU-off、trifecta、threads:32:

| | 現行 | 試作 |
|---|---:|---:|
| Scan Zipf 64KB | 1994/s (22.2 reads/op) | 1331/s (49.1 reads/op) |
| Scan Uniform 64KB | 565/s | 490/s |
| YCSB-E 64KB | 1973/s | 1540/s |
| Scan Zipf 1KB | 103990/s | 104030/s (無影響) |

## 機構

現行は1回のfaultで約74KB(約18ページ分)を読み、1回のdisk readで約4.5レコードを
賄っている。Scanがbatchをoffset昇順に読むためデバイス上は順次であり、既定readaheadが
実質prefetchとして効いている。exact-size preadはこれを殺し、reads/opが2.2倍・
disk時間が約3倍になった(50KB/回)。Get(ランダム1件)とScan(順序batch)は逆のI/O形状を
要求するという既存設計の正当性が実測で裏付けられた。

## 結論

本方向は採らない。64KB Scan系の残差に対する次の候補は、readaheadを活かす側の調整
(batch窓・先読み量)に絞る。
