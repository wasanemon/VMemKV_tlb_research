# 2026-09-20 LTM計測のセル単位隔離への移行

LTMの隔離単位を(Store, Variant)共有からセル単独へ変更した記録である。
`run_ltm_isolated_groups()`は以後、一致したベンチマーク名を1件ずつ
drop_caches+独立cgroup scope+ pristine cloneで実行する。

## 理由

- 同一scopeを66セルが共有すると、後段セルが前段の温まったページキャッシュを
  再利用し、64KB-Zipfで約5倍の水準差が出た(順序依存)。
- 同一store holderの共有では、後段セルが前段の書き込みを観測する。
- いずれも結果が実行順序に依存するため、セル単独で除去する。

## 留保

- 同一セル内のrepetitionsは同一scopeを共有する(相関誤差棒。独立反復の完全実装は
  将来課題)。
- 過去の全結果は共有scope条件のため、絶対値の横比較はできない。移行の開示である。
- in-memoryは圧力なしのため従来通り共有実行のままである。

## 同梱の計装

- headline-repsは既存flag(5並列既定3)を使う。新設なし。
- AMI IDをcontextに記録する(`VMEMKV_CONTEXT_ami_id`、bench側登録つき)。将来の既定
  Flipをデータ上で可視化するためのread-only追加であり、挙動変更はない。
