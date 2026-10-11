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

- `--json` は本文（`text`）を外してある（要るときだけ `--with-text`。出力が約 2.5 倍になる）
- **DOI・arXiv ID が分かっているなら `search` ではなく `has`**。`search` は語ごとの OR なので、DOI で引くと
  無関係の論文も並ぶ。`has` の状態: `有り`・`有り(出版版)`・`プレプリントのみ`・`候補(未検証)`（持っているとは
  言えない）・`補足資料のみ`・`無し`

- 3 文字以上の語を OR・部分一致で探す。表題・著者・誌名・DOI・抄録・本文の先頭 3,000 字を見る
  - 語は、英数字（アクセント付きのラテン文字を含む）の連続か、仮名・漢字（長音「ー」・踊り字「々」を含む）の連続で数える
    （`Müller`・`レーザー`・`コヒーレント`・`様々な` は 1 語）
  - それ以外の文字（記号・ギリシャ文字など）と、英数字と仮名・漢字の境目で切れる（`X-ray` は `ray`、`SHG素子` は `SHG` で探す）。
    記号ごと探すには引用符で囲む（`'"X-ray"'`。綴りの揺れは `'"FT-IR" OR "FTIR"'`）
  - 本文・抄録・表題の仮名・漢字の間の空白は詰めて索引に入っている（行の折り返しで切れた `コヒー レント` も `コヒーレント` で引ける）
  - アクセントの有無は区別しない（`Müller` と `Muller` は同じ）。分解できない文字（`ø`・`ł`・`ß`）は別のまま（`Søren` と `Soren` は両方で引く）
  - 全角の英数字・半角の片仮名・合字は、入力も索引も同じ形にそろえて比べる（`ＳＨＧ` は `SHG`）
  - 2 文字以下の語は、3 文字以上の語と一緒だと使われない（`Si waveguide` は `waveguide` だけ、`レーザー 発振` は `レーザー` だけ）。
    使われなかった語は標準エラーに `note: terms shorter than 3 characters were not used: 発振` と出る
  - 3 文字以上の語が無い入力（`Si`・`整合`・`発振 制御`）は別の経路になる: 空白で分けた語のどれかを含む記録を、表題と本文だけの部分一致で探す
    - 含む語の多い順に並べ、`--n` 件で切る。それ以上の関連の強さは見ない。抜粋は出ない
    - 短い語は他の語の一部にも当たる（`Si` は basis にも）。英字 2 文字の語はほとんどの記録に当たる。
      語として引くには前後の空白や記号ごと引用符で囲む（`'" Si "'`・`'" Si-"'`・`'"(Si)"'`）。囲んだ形にしか当たらない
      （`Si,`・`-Si` は落ちる。`" Si "` は語として載る記録の半分以下）ので、この件数を網羅と受け取らない
    - `%` と `_` は文字として探す（`T_c`・`50%`）
    - この経路で 0 件でも、著者・誌名・DOI・抄録・フォルダ名にだけある語と、`--n` 件より後ろの記録は見えていない
  - 注意と書き方の誤りは英語で標準エラーに出る（`note: …`・`invalid search expression …`）
- 絞るときは演算子: `'title:laser AND fiber NOT review'`・`'"second harmonic"'`・`'author:Smith'`
  （大文字の AND・OR・NOT、列名は title・author・first_author・journal・doi・abstract・folder・text。式は '…' で囲む）
  - 2 文字以下の姓は著者の列で語として引ける（`'author:Li'`・`'first_author:Wu'`。Lin や Oliver には当たらない。姓か名かは区別しない）。
    ほかの列に、引用符で囲まずに書いた 2 文字以下の語（`title:Si`）は 0 件になり、注意が出る
- dedup が移した重複（利用者が消す予定）は結果に出ない。出すには `--all`
- 表題の言い換え・著者の姓・誌名・分野の語で 2〜4 回引き直す。日本語も 3 文字以上の語なら引ける（語の数え方は上）
- 索引の場所・更新日時・件数は `papers info --json`。`db_stale` が真なら検索の索引が古い（新しい PDF が出ない）。
  `missing_files` が 0 でなければ、利用者がファイルを動かした（`papers relocate` を提案する。`scan` を先にしない）
- 本文は 1〜2 頁目の先頭 3,000 字（多くの論文で 1 頁目の途中）までしか索引に無い。それより奥の語は Spotlight（macOS）: `mdfind -onlyin "<library>" "<語>"`（`<library>` は `papers info --json` の値）

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
