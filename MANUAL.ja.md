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
| 索引（正本） | `<repo>/papers.jsonl` | 1 行 1 論文。DOI・表題・著者・誌名・抄録・本文の先頭 3,000 字など |
| 検索の索引 | `<repo>/papers.db` | `papers.jsonl` と全頁の本文から作る SQLite。**直接は直さない**（作り直すだけ） |
| 全頁の本文 | `<repo>/fulltext/` | PDF の全頁から読んだ本文（スキャンは OCR）。検索専用。消してもよい（`papers fulltext` が読み直す） |
| 設定 | `<repo>/config.json`（任意。見本は `config.example.json`） | 連絡先のメールアドレス（`mailto`）、ビューの出力先（`views`） |
| コマンド `papers` | `~/.local/bin/papers` → `<repo>/organize.py` へのリンク | どのディレクトリからでも動く |
| skill `paper-search` | `~/.claude/skills/paper-search` → `<repo>/skills/paper-search` | AI エージェント（Claude Code）に使い方を渡す |

索引・検索の索引・作業用のファイル（`unresolved.tsv`・`review.html`・`published.tsv`・`backups/`・改名の記録）は、どこから
`papers` を呼んでも `<repo>/` に書かれる。**どれも git には入れない**（`.gitignore` 済み。索引は論文の本文の冒頭を含む）。

### DB を分ける: `PAPERS_HOME`

上の表のうち、索引・検索の索引・全頁の本文と作業用のファイルの置き場をまとめて DB と呼ぶ。既定は `<repo>/`。環境変数 `PAPERS_HOME` で
別のフォルダを指すと、そのフォルダが別の DB になる（本や学位論文を、論文と別の DB にするなど）。フォルダは先に作る。

```bash
mkdir -p ~/papers-books
PAPERS_HOME=~/papers-books papers add <本の PDF のフォルダ>   # 別の DB を作る。この DB へのコマンドは、どれも PAPERS_HOME を付けて呼ぶ
PAPERS_HOME=~/papers-books papers info                       # どの DB を見ているかは「DB folder」の行で分かる
papers gui --db=~/papers-books                               # いつもの DB と一緒に、画面で開く
```

- `PAPERS_HOME` を付けないコマンドは、今までどおり `<repo>/` の DB を見る
- いつも一緒に開くなら、`<repo>/config.json` に `"dbs": [{"name": "本", "home": "~/papers-books"}]` と書く(`name` は省ける。
  読むのは、主に立てた DB のフォルダの `config.json` だけ)
- 画面に出す DB の名前は、`dbs` の `name`、その DB のフォルダの `config.json` の `name`、論文の置き場のフォルダ名の順
- `papers views` の出力先は、別の DB では「いつもの出力先 + `-` + DB のフォルダ名」（その DB のフォルダの `config.json` の `views` で変えられる）
- いつもの DB にある記録を別の DB へ移すには `papers send`（「DB から外す・別の DB へ送る」）。移す先のフォルダは、その DB のフォルダの
  `config.json` に `"library": "<PDF のフォルダ>"` と書いて決める

場所・更新日時・大きさ・件数は `papers info` で見る（読むだけ。`--json` で機械向け）。

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

### 書籍の DB

本の PDF は、論文と別の DB にする。DB のフォルダの `config.json` に `"books": true` と書くと、その DB は本として扱う:
著作権の頁の ISBN で目録（Crossref・Open Library・和書は国立国会図書館）を引き、返った表題が PDF にあるものだけ採る。

```bash
mkdir -p ~/papers-books
cat > ~/papers-books/config.json <<'JSON'
{"books": true, "name": "本", "library": "<本の PDF のフォルダ>", "exclude": ["下書き", "errata"]}
JSON
PAPERS_HOME=~/papers-books papers add <本の PDF のフォルダ>     # 同定 → 同定できなかった本の一覧 → 改名の予定 → 全頁の本文 → 検索の索引
PAPERS_HOME=~/papers-books papers search "group delay"     # 全頁の本文まで引く
```

- `exclude` は、渡したフォルダの直下で見ないフォルダの名前
- スキャンの本は全頁を OCR するので、最初の `add` は 1 時間の桁かかる。止めても、次の `add` が続きから読む
- 論文の DB と違う点: 重複の集約（`dedup`）と論文以外の移動（`nonpaper`）はしない。印の無い記録の種類は「本」。
  検索は全頁の本文の順位で並べる（表題・先頭で当たる記録を先にしない。「全頁の本文でだけ当たる」の区切りも出ない）
- 和書（ISBN が 9784 で始まる本）の書誌は、国立国会図書館サーチの API から取得する。ほかは Crossref と Open Library

**同定できなかった本**（ISBN が PDF に無い・目録に無い）は、`add` が理由つきで並べる。決めるのは人:

```bash
PAPERS_HOME=~/papers-books papers review     # unresolved.tsv を書く。candidates の列は、表題の検索の候補(見るだけ)
# 正しい本の ISBN か DOI を、isbn か doi の列に書く(版を決めるのは ISBN。候補は別の版・同名の別の本のことがある)
PAPERS_HOME=~/papers-books papers merge      # 書いた番号で目録を引いて、記録に書く
PAPERS_HOME=~/papers-books papers index      # 検索に反映
```

**種類を付ける**（章だけ・前付けだけ・プレプリントは「書籍(部分)」。講義ノート・資料も）: `papers nonpaper-page` でページを開き、
キーを押して書き出し、`papers nonpaper-ok <書き出したファイル>`。ファイルは動かさない。arXiv のプレプリントは、最初から「書籍(部分)」になる。
章だけの PDF も、本の ISBN があれば本の全体として同定されるので、種類は人が付ける。

**名前を付け替える**（任意）: `著者 - 表題 (版, 年).pdf`。著者は姓（2 人は `&`、3 人以上は `et al`、編者は `(ed.)`）、表題は主題だけ、
版は 2 版以上のときだけ。

```bash
PAPERS_HOME=~/papers-books papers rename           # 予定を出し、rename.tsv を書く(動かさない)
# rename.tsv の author・title・edition・year の列を確かめて直す(path の列は変えない)。もう一度 papers rename で名前を見る
PAPERS_HOME=~/papers-books papers rename --apply   # 表に行のある本の名前を変える。名前を戻すには papers undo
```

- 版と年の初期値は、いまのファイル名に書いてある値 → 著作権の頁 →（年だけ）目録。著者と表題の初期値は目録（筆頭の著者しか無いことがある）。
  どれも外れることがあるので、表で確かめる
- 名前を変えたくない本は、適用の前に表から行を消す（予定を出し直すと、行は初期値で戻る）
- 表は表計算で開いて保存してよい。タブ区切りのまま保存する（カンマ区切りにすると、適用は止まる）
- 名前を変えるのは、同定できた本の全体だけ。書籍(部分)・プレプリント・同定できなかったものは、今の名前のまま
- 大小文字だけが違う名前へは変えない

## 2. 検索する

```bash
papers search "second harmonic generation"      # 上位 10 件
papers search "Smit"                            # 著者の一部でも引ける
papers search "fiber laser" --n=30              # 件数を変える
papers search "fiber laser" --full              # 表題・DOI・抄録を切らずに出す
papers search "fiber laser" --json              # 1 行 1 記録の JSON(エージェント向け。本文は外す)
papers search "fiber laser" --json --with-text  # 本文(1〜2 頁目の先頭 3,000 字)も入れる(出力は約 2.5 倍)
papers search "fiber laser" --kind=paper       # 種類で絞る(paper・book・other)
```

### 画面で検索する: `gui`

```bash
papers gui                                  # 127.0.0.1 でサーバを立て、ブラウザで開く。止めるのは Ctrl-C
cargo run --manifest-path app/Cargo.toml    # 同じ画面を専用の窓で開く(Rust が要る)
sh app/bundle.sh                            # Finder から開ける papers.app を作る(macOS。Rust が要る)
```

- `papers.app` は `app/target/release/bundle/macos/` にできる。アプリケーションのフォルダへ写して使える。作った Mac 用で、
  このリポジトリの `gui.py` と、その Mac の `python3`・poppler を使う(リポジトリを動かしたら作り直す)。初めて PDF を開く・頁を見るときに、
  macOS が Google ドライブなどのフォルダへのアクセスを尋ねることがある

- `papers gui` をもう一度打つと、前に立てたものを止めて入れ替わる(前の画面のタブは使えなくなるので、新しく開いたほうを使う)
- 検索欄には `papers search` と同じ式が書ける。語を並べたときは「すべての語を含む」が既定(外すと `search` と同じ、いずれかの語)。
  空にすると全部の記録が並ぶ
- 件数は「表題・著者・抄録・本文の先頭で当たる数 + 全頁の本文だけで当たる数」。後ろの数は、参考文献の中の語にも当たる
- 年・誌名・状態・種類で絞り、関連度・年・第一著者で並べ替えられる
- 行を選ぶと、書誌・抄録・全頁の出現箇所(PDF の何頁目か)・BibTeX が出る。著者名を押すと、その姓で引き直す
- space で頁の絵を見る。出現箇所の頁番号を押すと、その頁から見る。キーの一覧は画面の下に出る
- 「直す」は `papers fix` と同じ。DOI を入れると表題・著者・誌名・巻・頁を引く(雑誌の論文はこれが確実)。本は、同じ欄に ISBN を入れても引ける(引く相手は下の `papers fix` と同じ)。どちらも無いものは
  表題・著者・第一著者の姓・年・誌名を手で入れる(著者は全員を「;」で区切る)。違いを確かめてから書く。ファイル名と検索に反映するには、
  続けて `papers rename --apply` と `papers index`
- 「片付ける」(`m`)は、論文でないもの・要らないものを見つけたときに使う。種類を変える、DB から外す、一緒に開いている別の DB へ移す、を
  選び、違いとファイルの行き先を確かめてから実行する(「DB から外す・別の DB へ送る」)
- `i`(または「DB」)で、読んでいる DB の場所・件数・新しさが出る(`papers info` と同じ)
- ほかの DB を一緒に開くと(`papers gui --db=<DB のフォルダ>`。「DB を分ける」)、一覧が DB ごとに上下に並ぶ。同じ条件で全部の DB を引き、
  件数は一覧ごとに出る。上のボタンで、出す DB を切り替えられる
- 外へ出るのは、「直す」で DOI を引くときだけ(DOI などのリンクは既定のブラウザで開く)。検索の索引が古いときは作り直さず、画面に知らせる

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

- **見る列**: 表題・著者・誌名・DOI・抄録・フォルダ名・本文の先頭 3,000 字と、全頁の本文。重みは表題 > DOI・著者 >
  誌名 > 抄録 > フォルダ > 本文
- **並ぶ順**: 表題・著者・誌名・DOI・抄録・フォルダ名・本文の先頭 3,000 字で当たる記録が先に並ぶ。全頁の本文でだけ当たる記録は、
  その後ろに並ぶ（`-- the records below match only when the full text is searched …` の行が境目）。奥の頁に 1 度出るだけの語で、
  表題に語のある論文が埋もれないようにするため
  - `--n` 件が前の組で埋まれば、後ろの組は出ない。奥の語で探すときは、語を絞るか `fulltext:` の列を名指しする（下の表）
  - 3 文字以上の語が無い入力（下の「別の経路」）も同じ: 表題・本文の先頭で当たる記録が先、全頁の本文でだけ当たる記録が後ろ
  - `fulltext:` を名指しした式は別: 当たった記録の全部が、全頁の本文を入れた 1 つの順位で並び、境目の行は出ない
    （長い文書は順位が下がりやすい）
  - `--mode=vec`・`--mode=hybrid` は全頁の本文を見ない
- **語の扱い**: 3 文字以上の語を OR で探し、部分一致でよい（`Smit` で Smith）
  - 語は、次のどちらかの連続で数える
    - 英数字。アクセント付きのラテン文字を含む（`Müller`・`Schrödinger` は 1 語）
    - 仮名・漢字。長音「ー」と踊り字「々」を含む（`レーザー`・`コヒーレント`・`様々な` は 1 語）
  - それ以外の文字（記号・ギリシャ文字など）と、英数字と仮名・漢字の境目で切れる
    （`X-ray` は `ray`、`SHG素子` は `SHG` で探す）。記号ごと探すには引用符で囲む: `papers search '"X-ray"'`。
    綴りの揺れは OR で並べる: `'"FT-IR" OR "FTIR"'`
  - 本文・抄録・表題の、仮名・漢字の間の空白は詰めて索引に入れる（PDF の行の折り返しで `コヒー レント` と載っている語を
    `コヒーレント` で引ける）。字の間に離れて載るアクセント記号（古い LaTeX の PDF の `Garci´a`）は落とす（`Garcia` で引ける）。
    本文の抜粋もこの形で出る
  - アクセントの有無は区別しない（`Müller` と `Muller` は同じ記録に当たる）。ただし:
    - 分解できない文字（`ø`・`ł`・`ß`）は別の文字のまま（`Søren` と `Soren` は別）
    - 3 文字以上の語が無い入力（下）は区別する
    - 古い SQLite（trigram の `remove_diacritics` を知らない版）では区別する。検索の索引を作るときに
      `note: this SQLite … does not support tokenizer …` と出る
  - 全角の英数字・半角の片仮名・合字（`ﬁ`）は、入力も索引も同じ形にそろえて比べる（`ＳＨＧ` は `SHG`、`field` は `ﬁeld` にも当たる）。
    本文の抜粋はそろえた後の形で出る
  - 2 文字以下の語は、3 文字以上の語と一緒に書くと使わない（`Si waveguide` は `waveguide` だけ、`レーザー 発振` は
    `レーザー` だけで探す）。使わなかった語は標準エラーに出る（`note: terms shorter than 3 characters were not used: 発振`）
  - 3 文字以上の語が 1 つも無い入力（`整合`・`Si`・`発振 制御`）は、既定の検索では、空白で分けた語のどれかを含む記録を
    部分一致で探す。このとき:
    - 表題と本文（先頭 3,000 字と全頁）だけを見る（著者・誌名・DOI・抄録・フォルダ名は見ない）
    - 含む語の多い順に並べ（`発振 制御` は両方を含む記録が先）、`--n` 件で切る。それ以上の関連の強さは見ない。本文の抜粋は出ない
    - 短い語は他の語の一部にも当たる（`Si` は basis にも。英字の大文字と小文字は区別しない）。語として引くには、
      前後の空白や記号ごと引用符で囲む: `'" Si "'`・`'" Si-"'`・`'"(Si)"'`（3 文字以上になるので、ふつうの検索で引ける）。
      ただし囲んだ形にしか当たらない（`Si,` や `-Si` は落ちる。`" Si "` は語として載る記録の半分以下）。網羅するなら囲まない `Si` で引いて絞る。英字 2 文字の語は
      ほとんどの記録に当たるので、役に立つのは漢字・仮名の語
    - `%` と `_` は文字として探す（`T_c` は T_c、`50%` は 50%）
- **全頁の本文**は `papers fulltext` が読んで `<repo>/fulltext/` に置く（`papers add` も、まだ読んでいないファイルの分を読む）。
  - テキスト層の乏しい PDF（スキャン）は全頁を OCR する（1 頁 1 秒ほど）。頁ごとに、テキスト層と OCR の字の多いほうを採る。読み誤りの語は引けない
  - 読めなかった・OCR できなかったファイル（壊れた PDF・PDF でないファイル）は件数と理由が出て、次に呼んだときにまた読む。
    先頭 3,000 字が索引にあれば、それでは引ける
  - 置き場が見えないとき（同期フォルダが開いていない）に検索の索引を作り直しても、全頁の本文は前の索引から引き継ぐ
  - まだ読んでいない記録があると `papers info` に出る。`papers.jsonl` の `text` は今までどおり先頭 3,000 字（同定に使う）
- **種類で絞る**: `--kind=paper`（論文）・`--kind=book`（本・ハンドブック）・`--kind=other`（学位論文・講義ノート・資料・特許）。
  コンマで複数（`--kind=paper,book`）。付けなければ全部。種類は §5「論文以外を避ける」で付けたもの
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
- 列名: `title`・`author`（著者全員）・`first_author`・`journal`・`doi`・`abstract`・`folder`・`text`（本文の先頭 3,000 字）・
  `fulltext`（全頁の本文）。`'fulltext:"Kramers-Kronig"'` は全頁の本文だけを見る
- `NOT` は、先に並ぶ組では先頭 3,000 字までで判定する（`laser NOT review` は、奥の頁に review がある論文も先の組に残る）。
  全頁で判定するなら `fulltext:` を名指しする（`'laser NOT fulltext:review'`）
- ハイフンや記号を含む語は引用符で囲む（`"second-harmonic"`。囲まないと書き方の誤りとして止まり、直し方が出る）
- 2 文字以下の語（`CW`・`UV`）は索引で引けない。式に入れると注意が出て、その条件は 0 件になる。例外が 2 つ:
  - 著者の列（`author:`・`first_author:`）は、2 文字以下の姓を語として引ける（`author:Li` は Li だけに当たり、Lin や Oliver には
    当たらない）。姓か名かは区別しない
  - 前後の空白や記号ごと引用符で囲めば引ける（`'" CW "'`・`'title:"UV-"'`）
- 検索の注意と書き方の誤りは英語で出る（`note: …`・`invalid search expression …`）
- シェルでは式全体を `'…'` で囲む（`"…"` の語句を中に書けるように）

SQL で直接引くこともできる（読むだけ。`-list` を付けないと、新しい sqlite3 は端末で表にしてパスを切る）:

```bash
sqlite3 -list -separator ' | ' "<repo>/papers.db" "SELECT year, trim(first_author), title, path FROM p WHERE p MATCH 'title:laser AND title:fiber' ORDER BY bm25(p) LIMIT 20"
```

表は 2 つある。`p` は今までの列、`f` は同じ列に `fulltext`（全頁の本文。頁の間は改頁の文字）を足したもの。

## 3. AI エージェントへ（検索するとき）

Claude Code では skill `paper-search` が「手元の論文を探して」などの依頼で読み込まれ、以下を渡す。
skill の無いエージェントには、このファイルを読ませる。

1. `papers search "<語>" --json --n=20` で引く。表題・著者・誌名・分野の言い換えで 2〜4 回引き直す。
   全頁の本文を入れて初めて当たる記録は、区切りの行の後ろに出る（`--json` では `"matched": "fulltext"`）。DOI・arXiv ID が分かっているなら検索ではなく `papers has`
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
dry run → まだ読んでいないファイルの全頁の本文を読む（スキャンは全頁を OCR。フォルダを付けても、索引の全体が対象）→ 検索の索引。フォルダを付けると（`papers add <フォルダ>`）その下だけを見る。

- 報告は 1 件ごとに「記録（著者・年・誌名・表題）」と「本文の冒頭」を並べる。**この 2 行が同じ論文かを見る**
  （`add` は判定しない）。表紙のある PDF では、本文の冒頭は 2 頁目になる
- 「人手行き」と出た記録は §5 の `fix`（1 件）か `review`（まとめて）で埋める。理由が「F_記録に著者か年が無い」のものは、
  誌の表紙や前付けなど、論文でない記録に着いている疑いがある（記録の表題が誌名になっている、など）
- 「取得失敗」と出た記録は、Crossref や arXiv から応答が取れなかったもの（相手の遅延・障害）。同定はまだしていない。
  時間をおいて `papers add` をもう一度打つと、その記録だけ取り直す
- 報告の最後に「論文以外」が出る: 未同定で 50 頁を超える候補の件数と、種類の付いた論文以外の移動の予定。候補があれば
  `papers nonpaper-page` で種類を付ける（§5「論文以外を避ける」）
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
papers index                  # 検索の索引を作り直す(3,000 件で 30 秒ほど)
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

  見積もりを見て、よければ同じコマンドに `--apply`（Batch API。費用は見積もりに出る。数千件で数ドルの桁）。
  キーは環境変数で渡す（macOS のキーチェーンに入れてあるなら
  `ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-api-key -w)`）。

  `papers add` はこれを呼ばない（有料なので、見積もりと同意が先）。何回か足したあとにまとめて回せばよい。
  記録の表題とページの表題が食い違えば、その記録を人手行きに戻して一覧に出す。50 件のときは結果が出るまで
  13〜14 分だった（1 回だけの値）。終わったら `papers index`

## 5. 記録を直す

### 1 件を直す: `fix`

```bash
papers fix <ファイル名の一部> --doi <正しい DOI>                  # 正しい DOI で引き直す
papers fix <ファイル名の一部> --isbn <本の ISBN>                  # 本を ISBN で引き直す(DOI の無い本)
papers fix <ファイル名の一部> --set year=1993 --set first_author=Yamada   # 項目を直接直す
```

- 1 つ目の引数は、ファイル名の一部かフルパス。**1 件に絞れないと止まる**（当たった候補を出す）
- `--doi` が引く相手は、まず Crossref。Crossref に無い DOI は、doi.org に登録機関を聞き、DataCite（データ・ソフトウェア・機関リポジトリ）か
  JaLC（和文誌・国内の学位論文。英語の欄があれば英語の表題・著者になる）から引く。arXiv の DOI（`10.48550/arXiv.…`）は arXiv から引く。書籍の DB では、本の DOI も受ける。
  ほかの登録機関の DOI は引けない（機関の名前を出して止まる）ので、`--set` で入れる
- `--isbn` は 10 桁でも 13 桁でも、ハイフンつきでもよい。目録は、和書（9784 で始まる）なら国立国会図書館が先で、次に Crossref と Open Library。
  本の記録になるので、前の記録の誌名・巻・号・頁は消える。論文の DB に置いた本にも使える
- 既定は変更前 → 変更後を出すだけ。確かめて `--apply` を付けると書く（書く前に `backups/` へ退避）
- 直せる項目: `doi` `year` `first_author` `journal` `journal_short` `volume` `issue` `pages` `title` `article_number`
  `published_doi` `authors`。`first_author` は第一著者の**姓**(ファイル名と引用のキーになる)。`authors` は全員を、1 人ぶんを「名 姓」の順で
  「;」で区切る(`--set authors="Ann Example; Bo Sample"`)。直した記録は `rung: manual`（人が決めた値。自動の検査で上書きされない）
- 誌名を入れなかった記録(学位論文・本)は、`rename` で改名されない(ファイル名はそのまま)。引用は `@misc` で作る
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

### 論文以外を避ける: `nonpaper`（本・学位論文・講義ノート・資料・特許）

Crossref にも arXiv にも無いファイルは、同定できずに人手行きに残り続ける。未同定で頁数の多いものを候補として出し、
人が種類を付けたものだけを、置き場の下の `_論文以外/<種類>/` へ移す。論文かどうかを決めるのは人で、道具は候補を出すだけ。

```bash
papers nonpaper-page && open "<repo>/nonpaper.html"   # 候補に種類を付けるページ
papers nonpaper-ok ~/Downloads/nonpaper-ok.tsv        # ページの「書き出す」の結果を索引に書く(ファイルは動かさない)
papers nonpaper                                       # 移動の予定を見せるだけ
papers nonpaper --apply && papers index               # 動かす
```

- **候補**: 人手行きで、PDF が 50 頁を超え、まだ種類を決めていない記録。頁数は `papers add` が新しい PDF について記録し、
  古い記録は `nonpaper-page` が最初に調べる（1 件 0.01 秒ほど）
- **ページのキー**: `b` 本・ハンドブック、`t` 学位論文、`l` 講義ノート・スライド、`d` 報告書・マニュアル・資料、`a` 特許、
  `p` 論文として残す。`u` 取り消し、`j`/`k` 次・前、`o` PDF を開く
- `p`（論文として残す）は、人手行きに残したまま、候補に出さなくなるだけ。同定は `papers fix <名前> --doi <DOI>` などで
- `nonpaper-ok` は、索引に無いパス・知らないキー・読めない行（タブで 2 欄に割れない、同じパスが 2 行）が 1 つでもあれば何も書かない。
  同定済みの記録には書かない
- `nonpaper --apply` は索引を退避してから動かし、索引も直す。移動先に同じ名前があれば `_2` を付ける（中身が同じでも消さない。
  大文字と小文字だけが違う名前も同じ名前として扱う）。途中で失敗したら、動かした分だけを索引に書いて止まる。`papers undo` で戻せる
- 種類を付け直すには、パス・タブ・キーの行を手で書いたファイルを `nonpaper-ok` に渡す（ページは種類の決まった記録を出さない）。
  そのあと `papers nonpaper` が、種類のフォルダの間で動かす予定を出す。
  論文に戻した記録は、自動では元のフォルダに戻らない（`papers move` で動かす）
- 論文以外にした記録は、人手行きの一覧にも改名・重複の対象にも入らない。`papers search` には今までどおり出る
- **候補に出ないもの**: 50 頁以下の論文以外（短いスライド・マニュアル・特許）と、頁数が読めない PDF。これらは `nonpaper-ok` に渡す
  ファイルを手で書いて印を付ける（1 行に パス・タブ・キー）。同定できた本の章は候補に出ない。1 件ずつなら、画面の「片付ける」で
  種類を付けられる（同定済みの記録にも付く）
- 線を 50 頁にしたのは、作者の蔵書で、それを超える未同定のファイルの大半が論文以外だったため（残りは総説で、あとから同定できた）

### DB から外す・別の DB へ送る: `remove`・`send`

論文でないもの・要らないものを 1 件ずつ片付ける。画面の「片付ける」(`m`)と同じことをする。

```bash
papers remove <ファイル名の一部>                                  # 予定を見せるだけ
papers remove <ファイル名の一部> --apply && papers index          # DB から外す
papers send <ファイル名の一部> --to <別の DB のフォルダ>          # 予定を見せるだけ
papers send --nonpaper --to <別の DB のフォルダ> --apply          # 論文以外の印の付いた記録を、まとめて別の DB へ
```

- `remove`: 記録に「外した」印を付け、ファイルを置き場の下の `_索引から外した/` へ移す。外した記録は、検索・画面・改名・重複の対象に
  入らず、人手行きにも `has` にも数えない。行は索引に残す（消すと、次の `papers add` がそのファイルをまた拾う）。`papers undo` で、
  ファイルも印も戻る。ファイルを消すのは自分で（Finder）。消した後の行は `papers relocate --apply` が外す
- `send`: ファイルを相手の DB の論文の置き場へ移し、記録と全頁の本文を相手の DB へ移す。種類の付いた論文以外は、相手の置き場の下の
  種類のフォルダに入る。相手の置き場は、相手の DB のフォルダの `config.json` の `"library": "<フォルダ>"`（無ければ、相手の全記録の
  フォルダの共通の親）。記録がまだ無い DB へ送るときは、先に `library` を書く。戻すには、相手の DB から送り返す
  （`PAPERS_HOME=<相手> papers send <名前> --to <repo>`）
- 片方の置き場がもう片方の中にあると、`send` は止まる（外側の DB の次の `papers add` が、送ったファイルをまた拾う）。別の DB の置き場は、
  いつもの置き場の外に作る
- 移した重複・外した記録と、それらが指している記録は、外せない・送れない
- 済んだら検索に反映する: `papers index`。送ったときは相手の DB でも（`PAPERS_HOME=<相手> papers index`）

### ファイルを手で動かした・名前を変えた・消した: `relocate`

Finder などでファイルを動かすと、索引は古いパスのままになる（`papers index` だけでは直らない。索引の正本が
古いパスを持っているため）。`papers info` に「※ パスにファイルが無い記録」と出たら:

```bash
papers relocate            # 対応を見せるだけ
papers relocate --apply    # 索引を退避してから書く
papers index
```

自分で消したファイルの記録を索引から外すには `--forget` を足す:

```bash
papers relocate --forget            # 外す記録を「見つからない(索引から外す)」として見せるだけ
papers relocate --forget --apply    # 索引を退避してから外す
```

- 移した重複・プレプリントが指している記録は、ファイルが無くても外さない（注意が出る）。見つかったファイルが 1 つも無いときは止まる
  （置き場が見えていないだけかもしれない）
- `--forget` が外すのは、それ以外の、どのファイルにも対応付かなかった「見つからない」記録の全部。同期が済んでいないだけのファイルも
  「見つからない」に出るので、一覧を見てから `--apply` する（退避から戻せる）

- 名前が同じで 1〜3 頁目の本文も一致するファイルに対応付ける。名前も変えたなら本文だけで探す。スキャンで本文の無い
  ものは名前だけ（「名前だけ」と出る）。決まらないものは出すだけで書かない
- `_重複文献/`・`_プレプリント(出版版あり)/` から消した重複は索引から外す（消したプレプリントの arXiv ID は出版版の
  記録に移すので、`papers has` は arXiv ID でも「有り(出版版)」と答える）
- どの記録にも当たらない PDF は新しいファイル。`papers add` で足す
- **`scan`・`add` を先にしない**: 動かしたファイルを新しいファイルとして同定し直し、手で直した書誌・判定が古い記録に
  取り残される。`scan` は、新しいファイルが索引の記録のファイルを動かしたものだと分かると、同定せずに止まって
  `relocate` を促す（動かしたのではないなら `--force`）

### 戻す

- 改名・`move`・`nonpaper --apply`・`remove --apply`: `papers undo`（最新の 1 回を戻す）。元の場所にあとから別のファイルが置かれていたら、
  上書きせずに「戻さなかった」と出して、そのファイルは今の場所に残す。元のフォルダが消されていれば作り直す
- ファイルを動かすコマンド（`rename`・`move`・`dedup`・`nonpaper`・`remove`・`send`・`undo`）は、移動先のファイルを上書きしない。大文字と小文字だけが
  違う名前も同じ名前として扱う。同じ名前があるとき、`rename`・`dedup`・`nonpaper` は `_2` を付け、`move` は止まり、`undo` はその
  ファイルを飛ばして続ける。予定のあとに移動先へファイルが現れた場合は、どのコマンドも動かす直前に止まる。途中で止まっても、
  動かした分は索引に書いてある
- `rename --apply` は、改名するものが無ければ記録を残さない（直前の `move`・`nonpaper` を `undo` で戻せるままにする）。
  ファイルの無い記録は改名せず、件数を出す（`papers relocate` で索引を直してから）
- 重複の移動: `<repo>/dedup-<時刻>.jsonl` に移動元と移動先の記録がある（自動の取り消しは無い。手で戻して `relocate`）
- 索引: `<repo>/backups/` に書き換え前の `papers.jsonl` がある（`papers-<時刻>-before-<何>.jsonl`）。ファイルを動かす・同定を書き換える
  コマンドは、どれも書く前に退避する（`enrich` を除く）。
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
