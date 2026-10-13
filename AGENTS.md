# AGENTS.md

このディレクトリは、VMemKV における TLB shootdown の実態を調査するためのものである。

## 目的

1. TLB shootdown は、どういう条件で起きるのかを明らかにする。
2. VMemKV で TLB shootdown が確認されていないのは、なぜかを明らかにする。

## 背景

- VMemKV の開発では TLB shootdown が確認されていない。理由は分かっていない。
- vmcache、cache_ext、Crotty らは、OSのページ管理を使うシステムの性能の問題として TLB shootdown を挙げる。
- 論文は、いったん TLB shootdown に触れないシナリオで進める。起きにくい理由が VMemKV の機構にあると分かれば、貢献として差し込む。

## 注意点

- 元のリポジトリ(`upstream`、`LineairDB/VMemKV`)は実装の担当者のものである。この調査の担当者のものではない。
- 実行環境は、実装の担当者のものと異なる。`implementation/vmemkv/docs/benchmark/` と `benchmark_results/` の数値は、実装の担当者の環境(AWS EC2)で測ったものである。この調査の環境で測った数値と、そのまま比べない。
- 数値を書くときは、どの環境で測ったかを必ず添える。
- 手元の機械は macOS である。`/proc/interrupts` などの Linux の仕組みは、そのままでは使えない。
- AWS は使えない前提で進める。無料の範囲に収まる場合に限り、使ってよい。費用がかかる操作は行わない。

## 既知の計測

- `implementation/vmemkv/docs/benchmark/20260805_ltm_get_hit_profiling.md` が唯一の記録である。`/proc/interrupts` の TLB 行の前後差分を取っている。実装の担当者の環境で測ったものである。
- この記録は `threads:1` だけで、対応づけを複数にする前の設計のものである。現在の設計とマルチスレッドの条件では未計測である。

## 進め方

- 計測を先に行う。コードを読んだだけ、推論しただけで、原因を断定しない。仮説は仮説と明示し、計測で確かめる。
- 数値は計測したものだけを書き、生データのファイルを示す。未計測の項目は未計測と明示し、数値を作らない。
- 機構の記述は `implementation/vmemkv/docs/specification/` と実装に一致させる。
- 文献は `must_read_papers/` の PDF か原典で内容を確認したものだけを引用する。
- 調査の記録は `tlb_research/` に、日付つきの Markdown(`YYYYMMDD_<題>.md`)で書く。生データは `tlb_research/logs/` に置く。
- `implementation/` を変えるのは、計測に必要な場合だけにする。変える前にユーザーの了承を得る。
- `paper/` は編集しない。

## リポジトリ

- `origin`(`wasanemon/VMemKV_tlb_research`)は、この調査用のリポジトリである。
- `upstream` には push しない。
- `git commit` と `git push` は、ユーザーが指示したときだけ行う。

## 環境

- ローカルのディスクを使いすぎない。
- C++ を整形するときは `pip install clang-format==18.1.3` を使う。
