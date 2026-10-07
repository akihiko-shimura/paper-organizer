# paper-organizer の使い方

[English](MANUAL.md) | 日本語

手元の論文 PDF の索引を検索し、PDF を足し、記録を直すための手順書。人が読んでも、AI エージェントが読んでも
そのまま使えるように書いてある。入れ方は [README.ja.md](README.ja.md)、設計の理由は [docs/design.ja.md](docs/design.ja.md)。

この文書では、リポジトリを置いた場所を `<repo>`、論文 PDF の置き場を `<library>` と書く。どちらも `papers info` で分かる。
コマンドの出力（メッセージ）は今のところ日本語だけ。

## 1. 何があるか

| もの | 場所 | 中身 |
|---|---|---|
| 論文 PDF | `<library>/` の下（フォルダは自由） | 同定できたものは `年_第一著者_略誌名_巻_号_開始頁.pdf` に改名する（例: `2019_Smith_OptExpress_27_8_11018.pdf`） |
| 索引（正本） | `<repo>/papers.jsonl` | 1 行 1 論文。DOI・表題・著者・誌名・抄録・1 頁目の本文など |
| 検索の索引 | `<repo>/papers.db` | `papers.jsonl` から作る SQLite。**直接は直さない**（作り直すだけ） |
| 設定 | `<repo>/config.json`（任意。見本は `config.example.json`） | 連絡先のメールアドレス（`mailto`）、ビューの出力先（`views`） |
| コマンド `papers` | `~/.local/bin/papers` → `<repo>/organize.py` へのリンク | どのディレクトリからでも動く |
| skill `paper-search` | `~/.claude/skills/paper-search` → `<repo>/skills/paper-search` | AI エージェント（Claude Code）に使い方を渡す |

索引・検索の索引・作業用のファイル（`unresolved.tsv`・`review.html`・`published.tsv`・`backups/`・改名の記録）は、どこから
`papers` を呼んでも `<repo>/` に書かれる。**どれも git には入れない**（`.gitignore` 済み。索引は論文の 1 頁目の抜粋を含む）。

場所・更新日時・大きさ・件数は `papers info` で見る（読むだけ。`--json` で機械向け）:

```
リポジトリ    /Users/…/paper-organizer
論文の置き場  /Users/…/Papers
索引          /Users/…/paper-organizer/papers.jsonl
              更新 2026-09-23 17:25 ・ 15.3 MB ・ 3248 件（解決済み 2232 ・ 人手行き 842 ・ 重複として移した 129）
検索の索引    /Users/…/paper-organizer/papers.db
              更新 2026-09-23 17:26 ・ 72.7 MB ・ 3248 件
最後の改名    2026-09-23 17:25
最新の退避    /Users/…/backups/papers-20260923-172530-before-rename.jsonl
```

検索の索引が索引より古い（または件数が違う）ときは「※ 索引より古い」と出る。`papers index` で作り直す。

索引の `rung`（段）は、その記録の確かさを表す。

| rung | 意味 | 検索結果での扱い |
|---|---|---|
| `pdfinfo_doi`・`text_doi`・`bib_query`・`title_search`・`arxiv_text` | 自動で同定し、1 頁目で裏を取った | 信じてよい |
| `manual` | 人が決めた・直した | 信じてよい |
| `unverified` | 候補の DOI はあるが裏が取れていない | **その DOI を事実として使わない** |
| `unresolved`・`error` | 同定できていない（学位論文・書籍・スキャンなど）。`error` で「外部から取れなかった」ものは次の `add`（または `scan`）が取り直す | 書誌は無い。ファイル名と本文で判断する |
| `supplement` | 補足資料（本文の DOI を持つ） | 本文とは別のファイル |

`duplicate_of` や `published_copy` がある記録は、`_重複文献/`・`_プレプリント(出版版あり)/` へ移した重複で、
利用者が消すのを待っている。論文そのものは別の記録（`duplicate_of`・`published_copy` が指すパス）にある。

## 2. 検索する

```bash
papers search "second harmonic generation"      # 上位 10 件
papers search "Smit"                            # 著者の一部でも引ける
papers search "fiber laser" --n=30              # 件数を変える
papers search "fiber laser" --full              # 表題・DOI・抄録を切らずに出す
papers search "fiber laser" --json              # 1 行 1 記録の JSON(エージェント向け。1 頁目の本文は外す)
papers search "fiber laser" --json --with-text  # 本文(1 頁目の先頭 3,000 字)も入れる(出力は約 2.5 倍)
```

### Finder で辿る: `views`

```bash
papers views                 # ビューを作り直す(数千件で 1 分弱)
open ~/PaperViews
```

出力先は既定で `~/PaperViews`（`config.json` の `views` で変えられる）。`誌名/Optics Letters/`・`年/2019/`・`分野/…`
（OpenAlex のトピック）・`第一著者/Smith/` に、元の PDF へのリンクが並ぶ。ダブルクリックでプレビュー、スペースで
クイックルック。ファイル本体は動かさない。

- 入るのは解決済みの論文だけ（同定できていない記録は誌名も年も無い）。分野は OpenAlex にトピックのあるものだけ
- 改名・`move`・`dedup --apply`・`relocate`・`merge` の後は `papers views` で作り直す（古いリンクは切れる）
- 作り直すたびに中身を消す（リンクだけ。リンク先には触れない）。**ビューの中にメモなどを保存しない**
  （普通のファイルがあると、消さずに止まる。Finder の `.DS_Store` は除く）
- ビューは同期フォルダ（Google Drive・iCloud など）の外に置く
- ビューの中の `使い方.txt` に、Finder で使う人向けの短い説明がある（作り直すたびに書き直す）

### 持っているかを確かめる: `has`

```bash
papers has 10.1234/example.2020.001 arXiv:2001.01234 https://doi.org/10.1234/example.2021.002
papers has --file refs.txt           # 1 行 1 つの DOI・arXiv ID(原稿の参考文献など)
papers has 10.1234/example.2020.001 --json
```

検索ではなく、DOI・arXiv ID の**完全一致**で答える（`https://doi.org/…`・`arXiv:…v3`・`10.48550/arXiv.…` の書き方は揃える）。

| 状態 | 意味 |
|---|---|
| `有り` | 同定済みの記録がある（パスを出す） |
| `有り(出版版)` | arXiv ID で引いたが、出版版の PDF も持っていて arXiv 版は `_プレプリント(出版版あり)/` に移してある。出版版のパスを出す |
| `プレプリントのみ` | 出版版の DOI で引いたが、手元にあるのは arXiv 版 |
| `候補(未検証)` | 裏の取れていない記録がその DOI を持つ。持っているとは言えない |
| `補足資料のみ` | 本文ではなく補足資料の PDF だけがある |
| `無し` | 索引に無い。同定できていない PDF の中にある可能性は残る |

### BibTeX: `cite`

```bash
papers cite 10.1234/example.2020.001 2001.01234      # DOI・arXiv ID
papers cite 2019_Smith                               # ファイル名の一部・パス
```

索引から作る（姓・名の区別は Crossref の応答のキャッシュから）。arXiv 版は `@misc`（出版版があれば `note` に DOI）。
未検証の記録には `% 注意` の行が付く。同定できていない記録は出さない。

### 最近入った記録: `recent`

```bash
papers recent --n=20        # 索引に最後に入った順。--json もある
```

### 検索の決まり

- **見る列**: 表題・著者・誌名・DOI・抄録・フォルダ名・1 頁目の本文（最初の 3,000 字）。重みは表題 > DOI・著者 >
  誌名 > 抄録 > フォルダ > 本文
- **語の扱い**: 3 文字以上の語を OR で探し、部分一致でよい（`Smit` で Smith）。2 文字以下の語は無視する
  （「整合」は引けない。「位相整合」なら引ける）。日本語も 3 文字以上なら引ける
- **本文の 2 頁目以降は見ない**。本文の奥にある語は Spotlight で探す（macOS）:

  ```bash
  mdfind -onlyin "<library>" "Kramers-Kronig"
  ```

- `--mode=vec`（意味の近さ）・`--mode=hybrid` もあるが、測った範囲では既定（`fts`）より悪いので使わない
- 結果には、同定できていない記録（年・著者が `-`）も出る。ファイル名と本文の抜粋で判断する
- dedup が移した重複（`_重複文献/`・`_プレプリント(出版版あり)/`、利用者が消す予定）は出さない。出すには `--all`

### AND・OR・NOT で絞る

検索語に演算子を書くと、その式で絞る（書かなければ上の OR 検索）。

| 書き方 | 意味 |
|---|---|
| `papers search 'laser AND fiber'` | 両方を含む |
| `papers search 'laser NOT fiber'` | laser を含み fiber を含まない |
| `papers search '"second harmonic"'` | 語句としてそのまま |
| `papers search 'title:laser AND title:fiber'` | 表題に両方 |
| `papers search 'author:Smith AND title:laser'` | 著者（全員）に Smith、表題に laser |
| `papers search 'first_author:Smith AND title:laser'` | 第一著者が Smith |
| `papers search '(laser OR amplifier) AND fiber NOT title:review'` | 括弧で組み合わせる |

- `AND`・`OR`・`NOT` は**大文字**（小文字の and は普通の語として OR 検索になる）。`NOT` の前には語が要る
- 列名: `title`・`author`（著者全員）・`first_author`・`journal`・`doi`・`abstract`・`folder`・`text`（1 頁目の本文）
- ハイフンや記号を含む語は引用符で囲む（`"second-harmonic"`。囲まないと書き方の誤りとして止まり、直し方が出る）
- 2 文字以下の語（`CW`・`UV`）は索引で引けない。式に入れると注意が出て、その条件は 0 件になる
- シェルでは式全体を `'…'` で囲む（`"…"` の語句を中に書けるように）

SQL で直接引くこともできる（読むだけ。`-list` を付けないと、新しい sqlite3 は端末で表にしてパスを切る）:

```bash
sqlite3 -list -separator ' | ' "<repo>/papers.db" "SELECT year, first_author, title, path FROM p WHERE p MATCH 'title:laser AND title:fiber' ORDER BY bm25(p) LIMIT 20"
```

## 3. AI エージェントへ（検索するとき）

Claude Code では skill `paper-search` が「手元の論文を探して」などの依頼で読み込まれ、以下を渡す。
skill の無いエージェントには、このファイルを読ませる。

1. `papers search "<語>" --json --n=20` で引く。表題・著者・誌名・分野の言い換えで 2〜4 回引き直す。
   見つからなければ `mdfind` で本文を探す。DOI・arXiv ID が分かっているなら検索ではなく `papers has`
2. 答えには**ファイルのパス**を添える。書誌（年・誌名・DOI）は `rung` が §1 で「信じてよい」ものだけを事実として書く。
   `unverified` の DOI は「候補」と書く
3. `published_doi` は出版版の DOI（手元の PDF はプレプリント）。`page_title` は Claude Haiku が 1 頁目から読んだ表題
   （任意のページの検証を回した記録だけにある）
4. 見つからなければ「索引には無い」と言う。持っていないとは断定しない（同定できていない PDF もある）
5. **検索では何も書き換えない**。`papers.jsonl`・`papers.db`・PDF を直接編集しない。直すときは §5 の `fix` を使う
6. 有料の API（`vet.py`）は、見積もりを出して利用者の同意を得るまで呼ばない

## 4. 新しい PDF を足す

PDF は `<library>` の下のどのフォルダに置いてもよい。ファイル名はダウンロードしたままでよい（同定に使わない）。

```bash
papers add                    # 新しい PDF を足し、重複と改名の予定まで出す(ファイルは動かさない)
```

最初の 1 回だけは、置き場を付ける（索引が空で、置き場がまだ分からないため）: `papers add <library>`。

`add` がすること: 索引を退避 → 新しいファイルだけ同定して足す → 抄録を取る → **新しい記録の報告** → 重複と改名の
dry run → 検索の索引。フォルダを付けると（`papers add <フォルダ>`）その下だけを見る。

- 報告は 1 件ごとに「記録（著者・年・誌名・表題）」と「本文の冒頭」を並べる。**この 2 行が同じ論文かを見る**
  （`add` は判定しない）。表紙のある PDF では、本文の冒頭は 2 頁目になる
- 「人手行き」と出た記録は §5 の `fix`（1 件）か `review`（まとめて）で埋める。理由が「F_記録に著者か年が無い」のものは、
  誌の表紙や前付けなど、論文でない記録に着いている疑いがある（記録の表題が誌名になっている、など）
- 「取得失敗」と出た記録は、Crossref や arXiv から応答が取れなかったもの（相手の遅延・障害）。同定はまだしていない。
  時間をおいて `papers add` をもう一度打つと、その記録だけ取り直す
- 予定がよければ、**重複を先に**動かす:

  ```bash
  papers dedup --apply
  ```

  そのあとで改名し、検索の索引とビューを作り直す:

  ```bash
  papers rename --apply && papers index && papers views
  ```

- `add --apply` は無い（止まる）。途中で止まったら、もう一度 `papers add`（足した記録は `papers recent` で見る）

`add` の中身を 1 つずつ打つなら:

```bash
papers scan "<library>"       # 新しいファイルだけ同定して索引に足す
papers stats                  # 段ごとの件数
papers enrich                 # 抄録を OpenAlex から(抄録の無い記録だけ)
papers dedup                  # 重複の確認(dry run)→ 問題なければ papers dedup --apply
papers review                 # 同定できなかったものを unresolved.tsv へ(候補があれば付く)
papers rename                 # 改名の確認(dry run)→ 問題なければ papers rename --apply
papers index                  # 検索の索引を作り直す(数千件で 15 秒ほど)
```

- `scan` は索引にあるパスを飛ばすので、何度実行してもよい。途中で止めても続きから再開する。1 件あたり数秒
  （約 3,200 件の最初のスキャンは 2〜4 時間だった）
- `dedup`・`rename` は既定で dry run。`--apply` で初めて動かす。**消さずに移すだけ**。`rename` は `undo` で戻せる
- 同定できなかったものは §5 の `review` の流れで埋める
- 任意: ページの検証（`vet.py`、Claude Haiku、有料）。まだ検証していない記録だけが対象。引数なしで見積もりだけが出る:

  ```bash
  cd "<repo>"
  ANTHROPIC_API_KEY=… uv run --quiet --with anthropic --python 3.12 python vet.py
  ```

  見積もりを見て、よければ同じコマンドに `--apply`（Batch API。3,203 件で 3.31 ドル、50 件で 0.05 ドルだった）。
  キーは環境変数で渡す（macOS のキーチェーンに入れてあるなら
  `ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-api-key -w)`）。

  `papers add` はこれを呼ばない（有料なので、見積もりと同意が先）。何回か足したあとにまとめて回せばよい。
  記録の表題とページの表題が食い違えば、その記録を人手行きに戻して一覧に出す。50 件のときは結果が出るまで
  13〜14 分だった（1 回だけの値）。終わったら `papers index`

## 5. 記録を直す

### 1 件を直す: `fix`

```bash
papers fix <ファイル名の一部> --doi <正しい DOI>                  # 正しい DOI で Crossref から引き直す
papers fix <ファイル名の一部> --set year=1993 --set first_author=Yamada   # 項目を直接直す
```

- 1 つ目の引数は、ファイル名の一部かフルパス。**1 件に絞れないと止まる**（当たった候補を出す）
- 既定は変更前 → 変更後を出すだけ。確かめて `--apply` を付けると書く（書く前に `backups/` へ退避）
- 直せる項目: `doi` `year` `first_author` `journal` `journal_short` `volume` `issue` `pages` `title` `article_number`
  `published_doi`。直した記録は `rung: manual`（人が決めた値。自動の検査で上書きされない）
- 直したら、ファイル名と検索に反映する:

  ```bash
  papers rename --apply && papers index && papers views
  ```

### まとめて埋める: `review` → `merge`（同定できなかったもの）

```bash
papers review                                  # unresolved.tsv を作る(候補のある行が上)
papers review-page && open "<repo>/review.html"   # 候補を y / n で確かめるページ
papers review-ok ~/Downloads/review-ok.tsv     # ページの「書き出す」の結果を取り込む
papers merge                                   # unresolved.tsv の記入を索引に書く
papers rename --apply && papers index && papers views
```

- 確認用のページ: `y` 正しい・`m` 正しい（手元の PDF は出版前の原稿）・`n` 候補が違う・`x` 論文ではない（本の抜粋・
  講義ノート・資料）・`u` 取り消し・`j` / `k` 次・前・`o` PDF を開く。判定はブラウザに保存されるので途中でやめてよい。
  誌名と年も見て判断する（同じ表題の別の版・学位論文と同名の論文が候補に出ることがある）
- `m` は `y` と同じに書誌を書き、記録に原稿の印（`version_note: manuscript`）を付ける。`x` は同定せず、記録に
  `not_paper` の印を付けて人手行きから外す（次の `review` に出ない。`papers stats` は、これを除いた解決率も出す）。
  候補の無い行は、`unresolved.tsv` の `ok` 列に `x` と書けば同じ扱いになる
- 候補は、任意のページの検証（`vet.py`）が読んだ表題と第一著者で引く。検証を回していない記録には候補が出にくい
- 候補が無い行は `<repo>/unresolved.tsv` の `doi` 列に DOI を、DOI が無いもの（学位論文・書籍）は
  `year`・`first_author`・`journal` などの列を直接埋める。テキストエディタで編集する（Excel で保存すると形が崩れる）

### arXiv 版の出版版: `pubver`

```bash
papers pubver              # 出版版の候補を published.tsv へ。ok 列に y / n を付ける
papers pubver --merge
```

出版版の PDF も手元にあれば、次の `papers dedup --apply` が arXiv 版を `_プレプリント(出版版あり)/` へ移す。

### ファイルを動かす: `move`（索引も一緒に直る）

```bash
papers move 2019_Smith --to "Nonlinear optics/Crystals"                     # 予定を見せるだけ
papers move 2019_Smith 2019_Sato_OptLett --to "New theme" --apply --mkdir   # 動かす
papers index && papers views
```

- 1 つ目の引数は、ファイル名の一部かフルパス（複数可）。1 件に絞れないと止まる
- `--to` は論文の置き場の下のフォルダ（置き場からの相対パスでよい）。**置き場の外には動かさない**。無いフォルダは
  `--apply --mkdir` のときだけ作る（dry run はフォルダが無くても予定を出す）
- 移動先に同じ名前のファイルがあれば止まる。`--apply` で索引を退避してから動かす。`papers undo` で戻せる
- Finder で動かしてもよい。その場合は次の `relocate` で索引を直す

### ファイルを手で動かした・名前を変えた・消した: `relocate`

Finder などでファイルを動かすと、索引は古いパスのままになる（`papers index` だけでは直らない。索引の正本が
古いパスを持っているため）。`papers info` に「※ パスにファイルが無い記録」と出たら:

```bash
papers relocate            # 対応を見せるだけ
papers relocate --apply    # 索引を退避してから書く
papers index
```

- 名前が同じで 1〜3 頁目の本文も一致するファイルに対応付ける。名前も変えたなら本文だけで探す。スキャンで本文の無い
  ものは名前だけ（「名前だけ」と出る）。決まらないものは出すだけで書かない
- `_重複文献/`・`_プレプリント(出版版あり)/` から消した重複は索引から外す（消したプレプリントの arXiv ID は出版版の
  記録に移すので、`papers has` は arXiv ID でも「有り(出版版)」と答える）
- どの記録にも当たらない PDF は新しいファイル。`papers add` で足す
- **`scan`・`add` を先にしない**: 動かしたファイルを新しいファイルとして同定し直し、手で直した書誌・判定が古い記録に
  取り残される。`scan` は、新しいファイルが索引の記録のファイルを動かしたものだと分かると、同定せずに止まって
  `relocate` を促す（動かしたのではないなら `--force`）

### 戻す

- 改名・`move`: `papers undo`（最新の 1 回を戻す）
- 重複の移動: `<repo>/dedup-<時刻>.jsonl` に移動元と移動先の記録がある（自動の取り消しは無い。手で戻して `relocate`）
- 索引: `<repo>/backups/` に書き換え前の `papers.jsonl` がある（`papers-<時刻>-before-<何>.jsonl`）。
  戻すときは `<repo>/papers.jsonl` に上書きコピーして `papers index`

## 6. AI エージェントに頼むとき（例文）

蔵書の作業（PDF の追加・条件で選ぶ・整理の候補出し・所蔵の確認・BibTeX）は、**開発などの長い会話とは別に**頼む。
長い会話の中で頼むと、1 回ごとにその会話を全部読み直すので、同じ作業の何倍ものトークンを使う（実測: 59 万トークンの
会話の中で 18 件を移動すると約 350 万トークン分の読み直し。同じ手順を小さな文脈で動かすと 7.3 万トークン）。

- Claude Code の新しいセッションで頼む（skill `paper-search` が読み込まれる）
- 長い会話の中からなら、**司書エージェント**（`agents/paper-librarian.md`。`~/.claude/agents/` にリンクを置く。Claude Sonnet で動く）に
  任せる。候補と dry run までを返すので、見て承認してから実行を頼む

例:

- 「手元の論文から○○について書かれたものを探して。パスも付けて」
- 「新しい PDF を `<library>/○○` に入れた。司書に頼んで索引に足して」（司書エージェントが `papers add` を回し、
  新しい記録の一覧と、重複・改名の予定を返す。見て承認すると動かす）
- 「`1993_Yamada…` の年が違う。正しくは 1994。MANUAL の §5 の fix で直して、変更前後を見せてから反映して」

エージェントが従うこと: 書き換える前に dry run を見せる、`--apply` は利用者の了承の後、有料の API は見積もりと同意の後、
PDF は消さない（移すだけ。消すのは利用者）。

## 7. うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| `papers: command not found` | `~/.local/bin` が PATH に無いか、リンクが無い。`ln -s "<repo>/organize.py" ~/.local/bin/papers` |
| skill が読み込まれない | `ln -s "<repo>/skills/paper-search" ~/.claude/skills/paper-search` を作り、Claude Code を開き直す |
| 検索で新しい PDF が出ない | `papers index` をしていない（検索の索引は `papers.jsonl` から作り直すまで古いまま）。`papers info` で「※ 索引より古い」と出る |
| 検索結果のパスのファイルが無い | 手で動かした・消した（`papers relocate`）。改名の後に `papers index` をしていない。または同期フォルダの同期待ち |
| `unresolved.tsv に記入済みで未 merge の行がある` | 記入を失わないための停止。先に `papers merge`。作り直すなら `papers review --force`（既存は退避される） |
| `papers add` で「取得失敗」が何度やっても残る | 相手（Crossref・arXiv）が落ちているか、`.cache/` の応答が壊れている。数時間おいてやり直す。それでも同じなら、`papers recent` でその記録の `error` を見る |
| `索引が読み込み後に変更されていた` | 2 つのコマンドを同時に動かした。片方が終わってからやり直す（書けなかったほうの結果は `papers.jsonl.conflict-*` に残る） |
| 画像だけの PDF が読めない | OCR は macOS の Vision を使う（初回に `swiftc` でビルド。Xcode のコマンドラインツールが要る）。無ければ `tesseract` に退避する |
| Crossref への問い合わせが遅い・止まる | `config.json` に `mailto`（連絡先のメールアドレス）を入れる。Crossref は連絡先のある利用者を安定した枠に入れる |
