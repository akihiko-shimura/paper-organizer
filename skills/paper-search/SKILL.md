---
name: paper-search
description: 利用者が手元に持っている論文 PDF の蔵書（`papers` コマンドで管理）を探す。Search the user's local collection of paper PDFs ("do I have this paper?", "find papers about X in my library").「〜の論文を持っているか」「手元の論文から〜を探して」「〜について書いた論文はあったか」「この論文の PDF はどこ」と聞かれたとき、文献を挙げる前に手元の所蔵を確かめたいときに使う。`papers` コマンドで索引を検索し、ファイルのパスと書誌を返す。論文 PDF の追加・記録の修正を頼まれたときも、手順の入口として使う。
---

# paper-search — 手元の論文を探す

`papers` は paper-organizer の `organize.py` へのリンク。どのディレクトリからでも動く。場所は `papers info --json` の
`repo`（リポジトリ）・`library`（論文の置き場）・`index`・`db`。手順の全体は `<repo>/MANUAL.md`（追加・修正はそこの §4・§5）。
新しい PDF を足すよう頼まれたら `papers add`（索引に足し、重複・改名の予定まで出す。ファイルは動かさない）。
予定を利用者に見せ、了承のあとに `papers dedup --apply` → `papers rename --apply && papers index && papers views`。

## 探す

```bash
papers search "<語>" --json --n=20      # 1 行 1 記録の JSON
papers search "<語>" --full             # 人に見せるとき(表題・DOI・抄録を切らない)
papers has <DOI や arXiv ID…>           # 持っているかを完全一致で(--file 一覧、--json)
papers cite <DOI・arXiv ID・ファイル名の一部>   # BibTeX
papers recent --n=20                    # 最近入った記録
```

- `--json` は 1 頁目の本文を外してある（要るときだけ `--with-text`。出力が約 2.5 倍になる）
- **DOI・arXiv ID が分かっているなら `search` ではなく `has`**。`search` は語ごとの OR なので、DOI で引くと
  無関係の論文も並ぶ。`has` の状態: `有り`・`有り(出版版)`・`プレプリントのみ`・`候補(未検証)`（持っているとは
  言えない）・`補足資料のみ`・`無し`

- 3 文字以上の語を OR・部分一致で探す（2 文字以下は無視）。表題・著者・誌名・DOI・抄録・1 頁目の本文を見る
- 絞るときは演算子: `'title:laser AND fiber NOT review'`・`'"second harmonic"'`・`'author:Smith'`
  （大文字の AND・OR・NOT、列名は title・author・first_author・journal・doi・abstract・folder・text。式は '…' で囲む）
- dedup が移した重複（利用者が消す予定）は結果に出ない。出すには `--all`
- 表題の言い換え・著者の姓・誌名・分野の語で 2〜4 回引き直す。日本語も 3 文字以上なら引ける
- 索引の場所・更新日時・件数は `papers info --json`。`db_stale` が真なら検索の索引が古い（新しい PDF が出ない）。
  `missing_files` が 0 でなければ、利用者がファイルを動かした（`papers relocate` を提案する。`scan` を先にしない）
- 1 頁目より奥の語は Spotlight（macOS）: `mdfind -onlyin "<library>" "<語>"`（`<library>` は `papers info --json` の値）

## 答えるとき

- **ファイルのパス**を必ず添える
- 書誌（年・誌名・DOI）を事実として書いてよいのは、`rung` が `manual` か自動の段（`pdfinfo_doi`・`text_doi`・
  `bib_query`・`title_search`・`arxiv_text` など）のものだけ。`unverified` の DOI は「候補」と書く。
  `unresolved`・`error` は書誌が無い（ファイル名と本文の抜粋で判断する）
- `published_doi` は出版版の DOI（手元の PDF はプレプリント）。`duplicate_of`・`published_copy` がある記録は
  利用者が消す予定の重複で、本体は別の記録
- 見つからなければ「索引には無い」と言う。持っていないとは断定しない（同定できていない PDF もある）

## してはいけないこと

- 検索のついでに `papers.jsonl`・`papers.db`・PDF を書き換えない。直すのは利用者に頼まれたときだけで、
  `papers fix …`（dry run → 利用者の了承 → `--apply`）
- `dedup`・`rename`・`move`・`relocate` の `--apply`、`vet.py`（有料の API）は、利用者の了承なしに実行しない。PDF は消さない
- ファイルを動かすよう頼まれたら `papers move <名前> --to <フォルダ>`（dry run を見せてから `--apply`）。`mv` や Finder の操作で
  動かさない（索引が古いパスのままになる）
