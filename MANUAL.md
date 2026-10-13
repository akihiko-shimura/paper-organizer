# Using paper-organizer

English | [日本語](MANUAL.ja.md)

This is the procedure manual for searching the index of your local paper PDFs, adding PDFs, and correcting records.
It is written so that both people and AI agents can use it as is. For installation see [README.md](README.md); for the reasons behind the design see [docs/design.md](docs/design.md).

In this document, `<repo>` is the place where the repository is located and `<library>` is the folder that holds the paper PDFs. `papers info` shows both.
For now, the output of the commands (messages) is in Japanese only.

## 1. What there is

| Item | Location | Contents |
|---|---|---|
| Paper PDFs | Under `<library>/` (any folder layout) | Identified files are renamed to `year_firstauthor_shortjournal_volume_issue_firstpage.pdf` (example: `2019_Smith_OptExpress_27_8_11018.pdf`) |
| Index (master copy) | `<repo>/papers.jsonl` | One paper per line. DOI, title, authors, journal, abstract, the first 3,000 characters of the text, and so on |
| Search index | `<repo>/papers.db` | SQLite built from `papers.jsonl` and the full text. **Do not edit it directly** (only rebuild it) |
| Full text | `<repo>/fulltext/` | The text of every page of each PDF (OCR for scans). Used only for search. Safe to delete (`papers fulltext` reads it again) |
| Settings | `<repo>/config.json` (optional; the sample is `config.example.json`) | Contact email address (`mailto`), output location of the views (`views`) |
| Command `papers` | `~/.local/bin/papers` → link to `<repo>/organize.py` | Works from any directory |
| skill `paper-search` | `~/.claude/skills/paper-search` → `<repo>/skills/paper-search` | Gives AI agents (Claude Code) the instructions for use |

The index, the search index and the working files (`unresolved.tsv`, `review.html`, `published.tsv`, `backups/`, the rename log) are written to
`<repo>/` no matter where you call `papers` from. **None of them goes into git** (already in `.gitignore`; the index contains the opening text of each paper).

### Separate libraries: `PAPERS_HOME`

The index, the search index, the full text and the working files together are one library (a "DB"). By default it lives in `<repo>/`.
Point the environment variable `PAPERS_HOME` at another folder and that folder becomes a separate library (for example, books and theses
apart from papers). Create the folder first.

```bash
mkdir -p ~/papers-books
PAPERS_HOME=~/papers-books papers add <folder of book PDFs>   # build another library; every command for it takes PAPERS_HOME
PAPERS_HOME=~/papers-books papers info                        # the "DB folder" line shows which library you are looking at
papers gui --db=~/papers-books                                # open it in the window together with the usual library
```

- Commands without `PAPERS_HOME` use the library in `<repo>/`, as before
- To always open it together, put `"dbs": [{"name": "Books", "home": "~/papers-books"}]` in `<repo>/config.json` (`name` is optional;
  only the `config.json` in the folder of the library you start with is read)
- The name shown in the window is the `name` in `dbs`, then the `name` in that library's own `config.json`, then the name of the folder that
  holds its PDFs
- For another library, `papers views` writes to "the usual output folder + `-` + the library's folder name" (change it with `views` in that
  library's own `config.json`)
- To move records from the usual library into another one, use `papers send` (see "Removing a file, or moving it to another library").
  Set where its PDFs live with `"library": "<folder>"` in the `config.json` in that library's folder

Use `papers info` to see locations, update times, sizes and record counts (read-only; `--json` for machines). Its labels are in Japanese.

The labels are: リポジトリ = repository, 論文の置き場 = library folder, 索引 = index, 検索の索引 = search index, 更新 = updated, 件 = records, 解決済み = resolved, 人手行き = needs a person, 重複として移した = moved aside as duplicates, 最後の改名 = last rename, 最新の退避 = latest backup.

When the search index is older than the index (or the record counts differ), 「※ 索引より古い」 (older than the index) is shown. Rebuild it with `papers index`.

The `rung` (段) of an index record shows how certain that record is.

| rung | Meaning | How to treat it in search results |
|---|---|---|
| `pdfinfo_doi`, `text_doi`, `bib_query`, `title_search`, `arxiv_text` | Identified automatically and confirmed against the first page | Can be trusted |
| `manual` | Decided or corrected by a person | Can be trusted |
| `unverified` | Has a candidate DOI, but it is not confirmed | **Do not use that DOI as fact** |
| `unresolved`, `error` | Not identified (theses, books, scans, and so on). Records with `error` meaning "could not be fetched from the external service" are fetched again by the next `add` (or `scan`) | No bibliographic data. Judge by the file name and the text |
| `supplement` | Supplementary material (carries the DOI of the main paper) | A separate file from the main paper |

Records that have `duplicate_of` or `published_copy` are duplicates that were moved to `_重複文献/` (duplicates) or `_プレプリント(出版版あり)/` (preprints whose published version is held),
and they are waiting for the user to delete them. The paper itself is in another record (the path that `duplicate_of` or `published_copy` points to).

### A book database

Keep book PDFs in a database of their own. Put `"books": true` in the `config.json` of the database folder and that database treats
its PDFs as books: it takes the ISBN from the copyright page, looks it up (Crossref, Open Library, and the National Diet Library for
Japanese books), and accepts the answer only when the returned title is printed in the PDF.

```bash
mkdir -p ~/papers-books
cat > ~/papers-books/config.json <<'JSON'
{"books": true, "name": "Books", "library": "<folder of book PDFs>", "exclude": ["misc", "errata"]}
JSON
PAPERS_HOME=~/papers-books papers add <folder of book PDFs>   # identify → list what was not identified → planned names → full text → search index
PAPERS_HOME=~/papers-books papers search "group delay"     # searches every page
```

- `exclude` names folders directly under the folder you pass that are not scanned
- Scanned books are OCRed on every page, so the first `add` takes on the order of an hour. If you stop it, the next `add` continues
- Unlike a paper database: no `dedup`, no `nonpaper` moves, and a record without a kind is a book
- Metadata of Japanese books (ISBNs starting with 9784) comes from the NDL Search API of the National Diet Library, Japan; the rest from Crossref and Open Library

**Books that were not identified** (no ISBN in the PDF, or not in the catalogues) are listed by `add` with the reason. You decide:

```bash
PAPERS_HOME=~/papers-books papers review     # writes unresolved.tsv; the candidates column holds title-search hints (read only)
# write the ISBN or the DOI of the right book in the isbn or doi column (an ISBN fixes the edition; a hint can be another edition or another book)
PAPERS_HOME=~/papers-books papers merge      # looks the number up and writes the record
PAPERS_HOME=~/papers-books papers index      # update the search
```

**Kinds** (a single chapter, front matter only or a preprint is a "book part"; lecture notes and documents too): open the page with
`papers nonpaper-page`, press the keys, export, then `papers nonpaper-ok <exported file>`. Files are not moved. arXiv preprints start as
"book part". A chapter-only PDF that carries the book's ISBN is identified as the whole book, so the kind is yours to set.

**Renaming** (optional): `Author - Title (edition, year).pdf`. Surnames only (two authors with `&`, three or more as `et al`, editors
with `(ed.)`), the main title without the subtitle, the edition only from the second on.

```bash
PAPERS_HOME=~/papers-books papers rename           # prints the plan and writes rename.tsv (nothing is moved)
# check and edit the author, title, edition and year columns of rename.tsv (leave the path column alone); run papers rename again to see the names
PAPERS_HOME=~/papers-books papers rename --apply   # renames the books that have a row in the table; papers undo puts the names back
```

- Edition and year start from what your current file name says, then the copyright page, then (year only) the catalogue. Author and title
  start from the catalogue (which may list only the first author). Any of them can be wrong, so check the table
- To leave a book as it is, delete its row before `--apply` (a new plan brings the row back with the default values)
- You may open and save the table in a spreadsheet. Keep it tab-separated (`--apply` stops on a comma-separated table)
- Only whole books that were identified are renamed. Book parts, preprints and unidentified files keep their names
- A name that differs from the current one only in letter case is not changed

## 2. Searching

```bash
papers search "second harmonic generation"      # top 10 records
papers search "Smit"                            # part of an author name also matches
papers search "fiber laser" --n=30              # change the number of results
papers search "fiber laser" --full              # print title, DOI and abstract without truncating
papers search "fiber laser" --json              # JSON, one record per line (for agents; the text is left out)
papers search "fiber laser" --json --with-text  # also include the text (the first 3,000 characters of pages 1–2) (output is about 2.5 times larger)
papers search "fiber laser" --kind=paper       # filter by kind (paper, book, other)
```

### Searching in a window: `gui`

```bash
papers gui                                  # start a server on 127.0.0.1 and open it in the browser. Stop it with Ctrl-C
cargo run --manifest-path app/Cargo.toml    # open the same screen in its own window (needs Rust)
sh app/bundle.sh                            # build papers.app, which opens from Finder (macOS; needs Rust)
```

- `papers.app` is written to `app/target/release/bundle/macos/`; copy it to your Applications folder if you like. It is for the Mac it
  was built on: it runs the `gui.py` of this repository with that Mac's `python3` and poppler (rebuild it if you move the repository).
  The first time it opens a PDF or shows a page, macOS may ask whether it may access the folder (for example Google Drive)

- Running `papers gui` again stops the one started earlier and takes its place (the earlier browser tab stops working; use the new one)
- The search box takes the same expressions as `papers search`. Several terms mean "match all terms" by default (untick it for
  any term, as in `search`). An empty box lists every record
- The count is "records matched by title, authors, abstract or the first text + records matched only in the full text". The second
  number also counts terms that appear in reference lists
- Narrow by year, journal, status and kind; sort by relevance, year or first author
- Selecting a row shows the bibliographic data, the abstract, where the terms appear in the full text (with the PDF page) and BibTeX.
  Clicking an author searches for that family name
- Space shows the pages as images. Clicking the page number of a match starts from that page. The keys are listed at the bottom of
  the screen
- "Edit" does what `papers fix` does. With a DOI it looks up the title, authors, journal, volume and pages (the reliable way
  for a journal article). A book can be looked up by typing its ISBN into the same field (where it asks is the same as `papers fix` below). Without one, fill in the title, authors, the first author's family name, year and journal by hand (all authors,
  separated by ";"). It shows the changes before writing. To carry them into file names and the search index, run
  `papers rename --apply` and `papers index` afterwards
- "File away" (`m`) is for a file that is not a paper, or that you do not want: change its kind, remove it from the library, or move it
  to another library opened together. It shows the changes and where the file will go before doing anything (see "Removing a file,
  or moving it to another library")
- `i` (or "Library") shows which library is loaded: where it is, how many records it has and how fresh it is (the same as `papers info`)
- With another library opened together (`papers gui --db=<its folder>`; see "Separate libraries"), the lists are stacked, one per library.
  The same query runs in all of them and each list shows its own count. The buttons at the top choose which libraries are shown
- It goes out to the network only when "Edit" looks up a DOI (links such as the DOI open in your default browser). When the search
  index is out of date it is not rebuilt; the screen says so

### Browsing in Finder: `views`

```bash
papers views                 # rebuild the views (a little under 1 minute for a few thousand records)
open ~/PaperViews
```

The default output location is `~/PaperViews` (change it with `views` in `config.json`). Links to the original PDFs are placed under `誌名/Optics Letters/` (誌名 = journal), `年/2019/` (年 = year), `分野/…`
(分野 = topic; these are OpenAlex topics) and `第一著者/Smith/` (第一著者 = first author). Double-click to open in Preview; press Space for
Quick Look. The files themselves are not moved.

- Only resolved papers are included (records that are not identified have neither journal nor year). The topic view covers only papers that have a topic in OpenAlex
- After a rename, `move`, `dedup --apply`, `relocate` or `merge`, rebuild with `papers views` (the old links break)
- Every rebuild deletes the contents (links only; the link targets are not touched). **Do not save notes or anything else inside the views**
  (if an ordinary file is there, the command stops without deleting. Finder's `.DS_Store` is excepted)
- Put the views outside synced folders (Google Drive, iCloud, and so on)
- `使い方.txt` (a short how-to) inside the views is a short explanation for people who use them in Finder (it is rewritten on every rebuild)

### Checking whether you hold a paper: `has`

```bash
papers has 10.1234/example.2020.001 arXiv:2001.01234 https://doi.org/10.1234/example.2021.002
papers has --file refs.txt           # one DOI or arXiv ID per line (for example, the reference list of a manuscript)
papers has 10.1234/example.2020.001 --json
```

This does not search. It answers by **exact match** on the DOI or arXiv ID (the forms `https://doi.org/…`, `arXiv:…v3` and `10.48550/arXiv.…` are normalized).

| Status | Meaning |
|---|---|
| `有り` (held) | There is an identified record (the path is printed) |
| `有り(出版版)` (held as the published version) | You looked it up by arXiv ID, but the PDF of the published version is also held and the arXiv version has been moved to `_プレプリント(出版版あり)/` (preprints whose published version is held). The path of the published version is printed |
| `プレプリントのみ` (preprint only) | You looked it up by the DOI of the published version, but what is held is the arXiv version |
| `候補(未検証)` (candidate only / unverified) | An unconfirmed record has that DOI. You cannot say that the paper is held |
| `補足資料のみ` (supplementary material only) | Only the PDF of the supplementary material is held, not the main paper |
| `無し` (not in the index) | Not in the index. It may still be among the PDFs that are not identified |

### BibTeX: `cite`

```bash
papers cite 10.1234/example.2020.001 2001.01234      # DOI or arXiv ID
papers cite 2019_Smith                               # part of a file name, or a path
```

The entry is built from the index (the family name / given name split comes from the cache of Crossref responses). An arXiv version becomes `@misc` (with the DOI in `note` if there is a published version).
An unverified record gets a `% 注意` line (注意 = caution). Records that are not identified are not output.

### Recently added records: `recent`

```bash
papers recent --n=20        # most recently added to the index first. --json is also available
```

### Search rules

- **Columns searched**: title, authors, journal, DOI, abstract, folder name, the first 3,000 characters of the text, and the full text of every page.
  The weights are title > DOI, authors > journal > abstract > folder > text
- **Order of results**: records that match in the title, authors, journal, DOI, abstract, folder name or the first 3,000 characters come first.
  Records that match only when the full text is searched come after them (the line `-- the records below match only when the full text is searched …`
  marks the boundary). This keeps a paper with the terms in its title from being buried under papers that mention a term once on a later page
  - If the first group fills `--n`, the second group is not shown. To look for a term deep in the text, narrow the query or name the `fulltext:` column (table below)
  - Input with no term of 3 or more characters (the "separate path" below) follows the same rule: records that match in the title or the start of the text
    come first, records that match only in the full text come after
  - An expression that names `fulltext:` is different: all its matches are ranked together with the full text included, and no boundary line is printed
    (long documents tend to rank lower)
  - `--mode=vec` and `--mode=hybrid` do not look at the full text
- **How terms are handled**: terms of 3 or more characters are searched with OR, and a partial match is enough (`Smit` finds Smith)
  - A term is a run of one of these:
    - Letters and digits, including accented Latin letters (`Müller` and `Schrödinger` are one term each)
    - Japanese kana and kanji, including the long-vowel mark 「ー」 and the iteration mark 「々」 (`レーザー`, `コヒーレント` and `様々な` are one term each)
  - Any other character (symbols, Greek letters and so on) ends a run, and so does a boundary between
    letters/digits and Japanese characters (`X-ray` searches for `ray`, `SHG素子` searches for `SHG`).
    To search with the symbol, quote the term: `papers search '"X-ray"'`. List spelling variants with OR: `'"FT-IR" OR "FTIR"'`
  - In the text, abstract and title, spaces between Japanese characters are removed before indexing (a word that a line wrap in the PDF left as `コヒー レント`
    is found by `コヒーレント`). An accent mark that stands alone between letters (`Garci´a` in old LaTeX PDFs) is dropped (found by `Garcia`).
    Text snippets are shown in this form
  - Accents are ignored (`Müller` and `Muller` find the same records), except that:
    - Letters that cannot be decomposed (`ø`, `ł`, `ß`) stay distinct (`Søren` and `Soren` differ)
    - An input with no term of 3 or more characters (below) is accent-sensitive
    - With an old SQLite (one whose trigram tokenizer lacks `remove_diacritics`) accents are significant. Building the search
      index then prints `note: this SQLite … does not support tokenizer …`
  - Full-width letters and digits, half-width katakana and ligatures (`ﬁ`) are brought to one form in both the input and the index
    (`ＳＨＧ` is `SHG`, and `field` also finds `ﬁeld`). Text snippets show the unified form
  - A term of 2 characters or fewer is not used when it is written together with a term of 3 or more characters (`Si waveguide` searches for
    `waveguide` only, `レーザー 発振` for `レーザー` only). The unused terms are reported on standard error
    (`note: terms shorter than 3 characters were not used: 発振`)
  - An input with no term of 3 or more characters (`整合`, `Si`, `発振 制御`) is, in the default search, split at spaces, and records containing
    any of the terms are found by substring match. In that case:
    - Only the title and the text (the first 3,000 characters and the full text) are searched (not authors, journal, DOI, abstract or folder name)
    - Records containing more of the terms come first (`発振 制御` lists records with both first), and the list is cut off at `--n` records.
      Relevance is not ranked beyond that. No text snippet is shown
    - A short term also matches inside other words (`Si` matches "basis"; ASCII letters match regardless of case). To match it as a word,
      quote it together with the surrounding spaces or symbols: `'" Si "'`, `'" Si-"'`, `'"(Si)"'` (3 or more characters, so the normal search finds them).
      Only the quoted form is matched (`Si,` and `-Si` are missed; `" Si "` finds fewer than half of the records that contain the word). For full coverage, search for plain `Si` and narrow down. A 2-letter ASCII term
      matches almost every record, so this path is useful mainly for kanji and kana terms
    - `%` and `_` are searched for literally (`T_c` finds T_c, `50%` finds 50%)
- **The full text** is read by `papers fulltext` and kept in `<repo>/fulltext/` (`papers add` also reads it for every file not read yet).
  - PDFs with almost no text layer (scans) are OCRed on every page (about 1 second per page). For each page, whichever of the text layer and the OCR
    has more characters is kept. Misread words cannot be found
  - Files that could not be read or OCRed (broken PDFs, files that are not PDFs) are listed with the reason and are tried again on the next call.
    If their first 3,000 characters are in the index, they can still be found by those
  - If the search index is rebuilt while the library cannot be seen (the sync folder is not open), the full text is carried over from the previous index
  - `papers info` says so when some records have not been read yet. `text` in `papers.jsonl` is still the first 3,000 characters (used for identification)
- **Filter by kind**: `--kind=paper` (papers), `--kind=book` (books and handbooks), `--kind=other` (theses, lecture notes, documents, patents).
  Separate several with commas (`--kind=paper,book`). Without it, everything is searched. The kinds are the ones assigned in §5 "Setting non-papers aside"
- `--mode=vec` (semantic similarity) and `--mode=hybrid` also exist, but within the range that was measured they are worse than the default (`fts`), so do not use them
- The results also include records that are not identified (year and author are `-`). Judge by the file name and the excerpt of the text
- Duplicates moved by dedup (`_重複文献/` (duplicates) and `_プレプリント(出版版あり)/`, which the user is expected to delete) are not shown. Use `--all` to show them

### Narrowing with AND, OR, NOT

If you write an operator in the query, the results are narrowed by that expression (if you do not, it is the OR search above).

| Syntax | Meaning |
|---|---|
| `papers search 'laser AND fiber'` | Contains both |
| `papers search 'laser NOT fiber'` | Contains laser and does not contain fiber |
| `papers search '"second harmonic"'` | As a phrase, exactly as written |
| `papers search 'title:laser AND title:fiber'` | Both in the title |
| `papers search 'author:Smith AND title:laser'` | Smith among the authors (all of them), laser in the title |
| `papers search 'first_author:Smith AND title:laser'` | The first author is Smith |
| `papers search '(laser OR amplifier) AND fiber NOT title:review'` | Combine with parentheses |

- `AND`, `OR` and `NOT` are **upper case** (a lower-case `and` is treated as an ordinary term and the query becomes an OR search). `NOT` needs a term before it
- Column names: `title`, `author` (all authors), `first_author`, `journal`, `doi`, `abstract`, `folder`, `text` (the first 3,000 characters of the text),
  `fulltext` (the full text of every page). `'fulltext:"Kramers-Kronig"'` looks only at the full text
- For the group listed first, `NOT` is judged on the first 3,000 characters (`laser NOT review` keeps a paper that says "review" only on a later page).
  To judge it on every page, name the `fulltext:` column (`'laser NOT fulltext:review'`)
- Enclose terms that contain a hyphen or a symbol in quotation marks (`"second-harmonic"`. Without them the command stops with a syntax error and shows how to fix it)
- Terms of 2 characters or fewer (`CW`, `UV`) cannot be looked up in the index. If you put one in an expression, a warning is shown and that condition matches 0 records. Two exceptions:
  - In the author columns (`author:`, `first_author:`) a surname of 2 characters or fewer is matched as a word (`author:Li` finds Li only, not Lin or Oliver).
    Family names and given names are not told apart
  - A short term quoted together with the surrounding spaces or symbols is found (`'" CW "'`, `'title:"UV-"'`)
- In the shell, enclose the whole expression in `'…'` (so that `"…"` phrases can be written inside)

You can also query directly with SQL (read-only. Without `-list`, a recent sqlite3 formats the output as a table in the terminal and truncates the path):

```bash
sqlite3 -list -separator ' | ' "<repo>/papers.db" "SELECT year, trim(first_author), title, path FROM p WHERE p MATCH 'title:laser AND title:fiber' ORDER BY bm25(p) LIMIT 20"
```

There are two tables. `p` has the columns above; `f` has the same columns plus `fulltext` (the text of every page, with a form feed between pages).

## 3. For AI agents (when searching)

In Claude Code, the skill `paper-search` is loaded by requests such as "find it among my papers" and gives the agent the following.
For an agent without the skill, have it read this file.

1. Query with `papers search "<term>" --json --n=20`. Query again 2 to 4 times, rephrasing the title, authors, journal and field.
   Records that match only when the full text is searched are listed after the separator line (`"matched": "fulltext"` with `--json`). If the DOI or arXiv ID is known, use `papers has` instead of searching
2. Attach the **file path** to the answer. State bibliographic data (year, journal, DOI) as fact only when the `rung` is one that §1 marks "Can be trusted".
   Write the DOI of an `unverified` record as a "candidate"
3. `published_doi` is the DOI of the published version (the PDF held is a preprint). `page_title` is the title that Claude Haiku read from the first page
   (present only on records for which the optional page verification was run)
4. If it is not found, say "it is not in the index". Do not assert that the user does not hold it (there are also PDFs that are not identified)
5. **Searching rewrites nothing.** Do not edit `papers.jsonl`, `papers.db` or the PDFs directly. To correct something, use `fix` in §5
6. Do not call the paid API (`vet.py`) until you have shown an estimate and obtained the user's consent

## 4. Adding new PDFs

Put the PDFs in any folder under `<library>`. Leave the file names as downloaded (they are not used for identification).

```bash
papers add                    # add the new PDFs and go as far as showing the planned duplicate moves and renames (no file is moved)
```

The very first time only, give the library folder (the index is empty, so the library folder is not yet known): `papers add <library>`.

What `add` does: back up the index → identify and add only the new files → fetch abstracts → **report the new records** → dry run of duplicates and
renames → read the full text of every file not read yet (scans are OCRed on every page; the whole index is covered even if you give a folder) → search index. If you give a folder (`papers add <folder>`), it looks only under that folder.

- For each record, the report shows the "record (authors, year, journal, title)" next to the "beginning of the text". **Check whether these two lines are the same paper**
  (`add` does not judge this). For a PDF with a cover page, the beginning of the text is taken from page 2
- Fill in records shown as 「人手行き」 (needs a person) with `fix` (one record) or `review` (in bulk) in §5. Records whose reason is 「F_記録に著者か年が無い」 (record has no author or year) are suspected of
  being attached to a record that is not a paper, such as a journal cover or front matter (for example, the title of the record is the journal name)
- Records shown as 「取得失敗」 (lookup failed) are ones for which no response was obtained from Crossref or arXiv (a delay or outage on their side). They are not identified yet.
  Wait a while and run `papers add` again; it fetches only those records again
- The report ends with a section on non-papers (「論文以外」): the number of unidentified candidates of more than 50 pages, and the planned moves of records already given a kind.
  If there are candidates, give them a kind with `papers nonpaper-page` (section 5, "Setting non-papers aside")
- If the plan is right, move the **duplicates first**:

  ```bash
  papers dedup --apply
  ```

  Then rename, and rebuild the search index and the views:

  ```bash
  papers rename --apply && papers index && papers views
  ```

- There is no `add --apply` (it stops). If it stops partway, run `papers add` again (see the added records with `papers recent`)

To run the steps of `add` one at a time:

```bash
papers scan "<library>"       # identify only the new files and add them to the index
papers stats                  # record counts per rung
papers enrich                 # abstracts from OpenAlex (only records without an abstract)
papers dedup                  # check the duplicates (dry run) → if there is no problem, papers dedup --apply
papers review                 # write the ones that could not be identified to unresolved.tsv (with candidates where there are any)
papers rename                 # check the renames (dry run) → if there is no problem, papers rename --apply
papers index                  # rebuild the search index (about 30 seconds for 3,000 records)
```

- `scan` skips the paths that are already in the index, so you can run it any number of times. If you stop it partway, it resumes where it left off. It takes a few seconds per file
  (the first scan of about 3,200 files took 2 to 4 hours)
- `dedup` and `rename` are dry runs by default. They act only with `--apply`. **They only move files and never delete them.** `rename` can be reverted with `undo`
- Fill in the ones that could not be identified with the `review` flow in §5
- Optional: page verification (`vet.py`, Claude Haiku, paid). Only records that are not yet verified are covered. With no arguments it prints only the estimate:

  ```bash
  cd "<repo>"
  ANTHROPIC_API_KEY=… uv run --quiet --with anthropic --python 3.12 python vet.py
  ```

  Look at the estimate and, if it is acceptable, add `--apply` to the same command (Batch API. The estimate shows the cost; a few dollars for a few thousand records).
  Pass the key in the environment variable (if it is stored in the macOS keychain:
  `ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-api-key -w)`).

  `papers add` does not call this (it is paid, so the estimate and the consent come first). You can run it once after several additions.
  If the title of the record and the title on the page disagree, the record is sent back to 人手行き (needs a person) and listed. For 50 records it took
  13 to 14 minutes until the result came back (a single measurement). When it finishes, run `papers index`

## 5. Correcting records

### Correcting one record: `fix`

```bash
papers fix <part of file name> --doi <correct DOI>                  # look the record up again with the correct DOI
papers fix <part of file name> --isbn <ISBN of the book>             # look a book up by its ISBN (for books without a DOI)
papers fix <part of file name> --set year=1993 --set first_author=Yamada   # correct fields directly
```

- The first argument is part of a file name or a full path. **The command stops if this does not narrow down to one record** (it shows the candidates that matched)
- `--doi` asks Crossref first. For a DOI Crossref does not have, it asks doi.org which agency registered it and then takes the record from
  DataCite (data, software, institutional repositories) or JaLC (Japanese journals and theses; the English title and names are used when JaLC has them). An arXiv DOI (`10.48550/arXiv.…`) goes to
  arXiv. In a book library, the DOI of a whole book is accepted too. DOIs of other agencies cannot be looked up (the command names the
  agency and stops); use `--set` for those
- By default it only shows before → after. Check it, then add `--apply` to write (it backs up to `backups/` before writing)
- Fields that can be corrected: `doi` `year` `first_author` `journal` `journal_short` `volume` `issue` `pages` `title` `article_number`
  `published_doi` `authors`. `first_author` is the **family name** of the first author (it goes into the file name and the citation key).
  `authors` is everyone, each written as "Given Family" and separated by ";" (`--set authors="Ann Example; Bo Sample"`).
  A corrected record gets `rung: manual` (a value decided by a person. Automatic checks do not overwrite it)
- A record corrected without a journal (a thesis, a book) is not renamed by `rename` (the file keeps its name). Its citation is made as `@misc`
- After correcting, apply the change to the file name and to the search:

  ```bash
  papers rename --apply && papers index && papers views
  ```

### Filling in many at once: `review` → `merge` (records that could not be identified)

```bash
papers review                                  # create unresolved.tsv (rows with a candidate come first)
papers review-page && open "<repo>/review.html"   # a page for confirming the candidates with y / n
papers review-ok ~/Downloads/review-ok.tsv     # import the result of 「書き出す」 (export) on the page
papers merge                                   # write the entries in unresolved.tsv to the index
papers rename --apply && papers index && papers views
```

- The confirmation page: `y` correct, `m` correct (the PDF held is a pre-publication manuscript), `n` wrong candidate, `x` not a paper (a book excerpt,
  lecture notes, a datasheet), `u` undo, `j` / `k` next / previous, `o` open the PDF. The decisions are saved in the browser, so you can stop partway.
  Look at the journal and the year too when deciding (a different edition with the same title, or a paper with the same name as a thesis, can appear as a candidate)
- `m` writes the bibliographic data just as `y` does and marks the record as a manuscript (`version_note: manuscript`). `x` does not identify the file; it marks
  the record `not_paper` and takes it out of 人手行き (needs a person), so it does not appear in the next `review`, and `papers stats` also prints the resolution
  rate with these excluded. For a row with no candidate, writing `x` in the `ok` column of `unresolved.tsv` has the same effect
- Candidates are looked up with the title and first author that the optional page verification (`vet.py`) read. Records for which the verification was not run are less likely to get a candidate
- For a row with no candidate, fill in the DOI in the `doi` column of `<repo>/unresolved.tsv`. For items with no DOI (theses, books), fill in
  columns such as `year`, `first_author` and `journal` directly. Edit the file with a text editor (saving it from Excel breaks the format)

### Published versions of arXiv papers: `pubver`

```bash
papers pubver              # write the candidates for the published version to published.tsv. Put y / n in the ok column
papers pubver --merge
```

If the PDF of the published version is also held, the next `papers dedup --apply` moves the arXiv version to `_プレプリント(出版版あり)/` (preprints whose published version is held).

### Moving files: `move` (the index is corrected with them)

```bash
papers move 2019_Smith --to "Nonlinear optics/Crystals"                     # only show the plan
papers move 2019_Smith 2019_Sato_OptLett --to "New theme" --apply --mkdir   # move
papers index && papers views
```

- The first argument is part of a file name or a full path (more than one is allowed). The command stops if it does not narrow down to one record
- `--to` is a folder under the library folder (a path relative to the library folder is fine). **Files are not moved outside the library folder.** A folder that does not exist is
  created only with `--apply --mkdir` (a dry run shows the plan even if the folder does not exist)
- If a file with the same name exists at the destination, the command stops. With `--apply` it backs up the index and then moves. `papers undo` reverts it
- You may also move files in Finder. In that case, correct the index with `relocate` below

### Setting non-papers aside: `nonpaper` (books, theses, lecture notes, documents, patents)

A file that is in neither Crossref nor arXiv cannot be identified and stays in the list for a person forever. The tool lists long unidentified files
as candidates, and moves only the ones a person has given a kind to into `_論文以外/<kind folder>/` under the library. A person decides what is not a paper; the tool only proposes.

```bash
papers nonpaper-page && open "<repo>/nonpaper.html"   # a page for giving each candidate a kind
papers nonpaper-ok ~/Downloads/nonpaper-ok.tsv        # write the page's export to the index (no file is moved)
papers nonpaper                                       # only show the planned moves
papers nonpaper --apply && papers index               # move
```

- **Candidates**: records left for a person whose PDF has more than 50 pages and that have no kind yet. `papers add` records the page count of new PDFs;
  for older records `nonpaper-page` looks it up the first time (about 0.01 s per file)
- **Keys on the page**: `b` book or handbook, `t` thesis, `l` lecture notes or slides, `d` report, manual or other document, `a` patent,
  `p` keep as a paper. `u` undo, `j`/`k` next/previous, `o` open the PDF
- `p` (keep as a paper) leaves the record in the list for a person and only stops it from being proposed again. Identify it with `papers fix <name> --doi <DOI>` and the like
- `nonpaper-ok` writes nothing if any path is not in the index, any key is unknown, or any line cannot be read (not two tab-separated fields, or the same path twice).
  It does not write to identified records
- `nonpaper --apply` backs up the index, moves the files and corrects the index. If the destination already has a file of the same name, `_2` is appended (nothing is deleted, even identical copies;
  names that differ only in letter case count as the same name). If a move fails partway, the files already moved are written to the index and the command stops. `papers undo` reverts it
- To change a kind, give `nonpaper-ok` a file written by hand, one line per file: path, tab, key (the page does not list records that already have a kind).
  `papers nonpaper` then plans a move between the kind folders.
  A record changed back to a paper is not moved back automatically (use `papers move`)
- A record marked as a non-paper is no longer listed for a person and is not renamed or treated as a duplicate. It still appears in `papers search`
- **Not proposed**: non-papers of 50 pages or fewer (short slides, manuals, patents) and PDFs whose page count cannot be read. Mark these by writing the file for `nonpaper-ok`
  by hand (one line per file: path, tab, key). Book chapters that were identified are not proposed. One at a time, "File away" in the
  window sets the kind (also on identified records)
- Why 50 pages: in the author's library, most unidentified files longer than that were not papers (the rest were reviews, identified later)
- The folder names are Japanese: `_論文以外` (non-papers), `本・ハンドブック` (books), `学位論文` (theses), `講義ノート・スライド` (lectures), `報告書・マニュアル・資料` (documents), `特許` (patents)

### Removing a file, or moving it to another library: `remove`, `send`

For a file that is not a paper, or that you do not want. The window's "File away" (`m`) does the same.

```bash
papers remove <part of file name>                              # only shows the plan
papers remove <part of file name> --apply && papers index      # remove it from the library
papers send <part of file name> --to <folder of another DB>    # only shows the plan
papers send --nonpaper --to <folder of another DB> --apply     # every record marked as a non-paper, to another library
```

- `remove` marks the record and moves the file to `_索引から外した/` ("taken out of the index") under the library folder. A removed record
  is left out of search, the window, renaming and duplicates, and is not counted as needing a person or as held (`has`). Its line stays in
  the index (if it were deleted, the next `papers add` would index the file again). `papers undo` brings back both the file and the record.
  Deleting the file is up to you (in Finder); afterwards `papers relocate --apply` drops the line
- `send` moves the file into the other library's folder for PDFs, and moves the record and its full text to the other library. A marked
  non-paper goes into the folder of its kind there. The other library's folder for PDFs is `"library": "<folder>"` in the `config.json`
  in that library's folder (without it, the common parent of the folders of its records). For a library with no records yet, write
  `library` first. To undo, send it back from the other library (`PAPERS_HOME=<other> papers send <name> --to <repo>`)
- `send` stops when one library keeps its PDFs inside the other's folder (the next `papers add` in the outer one would index the sent
  files again). Give another library a folder outside the usual one
- Moved duplicates, removed records and the records they point to cannot be removed or sent
- Afterwards update the search: `papers index` (after `send`, also in the other library: `PAPERS_HOME=<other> papers index`)

### Files moved, renamed or deleted by hand: `relocate`

When you move a file with Finder or the like, the index keeps the old path (`papers index` alone does not fix this, because the master copy of the index
holds the old path). If `papers info` shows 「※ パスにファイルが無い記録」 (records whose file is missing):

```bash
papers relocate            # only show the matches
papers relocate --apply    # back up the index, then write
papers index
```

To drop the records of files you deleted yourself, add `--forget`:

```bash
papers relocate --forget            # only show the records that would be dropped
papers relocate --forget --apply    # back up the index, then drop them
```

- A record that a moved duplicate or preprint still points to is kept even if its file is gone (a note is printed). If no file at all was found,
  the command stops (the library may simply not be visible)
- Apart from those, `--forget` drops every record that could not be matched to any file. A file that has merely not finished syncing is also listed as not found,
  so read the list before `--apply` (the backup lets you restore)

- A record is matched to a file that has the same name and whose text on pages 1 to 3 also agrees. If you changed the name too, the file is searched for by text alone. Scans with no text
  are matched by name alone (shown as 「名前だけ」, matched by name only). Those that cannot be decided are only shown, not written
- Duplicates that you deleted from `_重複文献/` (duplicates) or `_プレプリント(出版版あり)/` are removed from the index (the arXiv ID of a deleted preprint is moved to the record of the
  published version, so `papers has` answers 「有り(出版版)」 (held as the published version) for the arXiv ID as well)
- A PDF that matches no record is a new file. Add it with `papers add`
- **Do not run `scan` or `add` first**: they identify the moved files again as new files, and the bibliographic data and decisions that you corrected by hand are
  left behind on the old records. When `scan` finds that a new file is a moved copy of the file of an index record, it stops without identifying and
  prompts you to run `relocate` (use `--force` if the file was not moved)

### Undoing

- Renames, `move`, `nonpaper --apply` and `remove --apply`: `papers undo` (reverts the most recent one). If another file has since been put in the original place,
  it is not overwritten: the command prints 「戻さなかった」 (not reverted) and leaves that file where it is now. If the original folder was deleted, it is created again
- The commands that move files (`rename`, `move`, `dedup`, `nonpaper`, `remove`, `send`, `undo`) never overwrite a file at the destination. Names that differ only in
  letter case count as the same name. When the name is taken, `rename`, `dedup` and `nonpaper` append `_2`, `move` stops, and `undo` skips that
  file and goes on. If a file appears at the destination after the plan was made, every command stops just before moving. If a command stops partway,
  the files already moved are in the index
- `rename --apply` leaves no trail when there is nothing to rename (so the preceding `move` or `nonpaper` can still be reverted with `undo`).
  Records whose file is missing are not renamed; their number is printed (correct the index with `papers relocate` first)
- Moves of duplicates: `<repo>/dedup-<time>.jsonl` records the source and the destination of each move (there is no automatic undo. Move the files back by hand and run `relocate`)
- Index: `<repo>/backups/` holds the `papers.jsonl` from before each rewrite (`papers-<time>-before-<what>.jsonl`). Every command that moves files or rewrites an identification
  backs up first (`enrich` does not).
  To restore, copy the backup over `<repo>/papers.jsonl` and run `papers index`

## 6. Asking an AI agent (example requests)

Ask for library work (adding PDFs, selecting by criteria, proposing candidates for reorganization, checking holdings, BibTeX) **separately from long conversations such as development**.
If you ask inside a long conversation, the whole conversation is read again at every step, so the work uses many times the tokens of the same work elsewhere (measured: moving 18 files inside a
590,000-token conversation cost about 3.5 million tokens of re-reading. The same procedure run in a small context took 73,000 tokens).

- Ask in a new Claude Code session (the skill `paper-search` is loaded)
- From inside a long conversation, hand the work to the **librarian agent** (`agents/paper-librarian.md`; put a link in `~/.claude/agents/`; it runs on Claude Sonnet).
  It returns the candidates and everything up to the dry run, so look at them, approve, and then ask it to execute

Examples:

- "Find the papers about <topic> in my collection. Include the paths."
- "I put new PDFs in `<library>/<folder>`. Ask the librarian to add them to the index." (The librarian agent runs `papers add` and
  returns the list of new records and the planned duplicate moves and renames. When you have looked at them and approved, it moves the files.)
- "The year of `1993_Yamada…` is wrong. It should be 1994. Correct it with fix in §5 of the MANUAL: show me the before and after first, then apply it."

What the agent follows: show a dry run before rewriting anything; `--apply` only after the user's approval; paid APIs only after an estimate and consent;
never delete PDFs (only move them. Deleting is done by the user).

## 7. Troubleshooting

| Symptom | Cause and remedy |
|---|---|
| `papers: command not found` | `~/.local/bin` is not on PATH, or the link does not exist. `ln -s "<repo>/organize.py" ~/.local/bin/papers` |
| The skill is not loaded | Create `ln -s "<repo>/skills/paper-search" ~/.claude/skills/paper-search` and reopen Claude Code |
| New PDFs do not appear in search | `papers index` was not run (the search index stays old until it is rebuilt from `papers.jsonl`). `papers info` shows 「※ 索引より古い」 (older than the index) |
| The file at a path in the search results does not exist | It was moved or deleted by hand (`papers relocate`). `papers index` was not run after a rename. Or the synced folder is still waiting to sync |
| `unresolved.tsv に記入済みで未 merge の行がある` (unresolved.tsv has filled-in rows that are not merged yet) | A stop so that the entries are not lost. Run `papers merge` first. To recreate the file, use `papers review --force` (the existing file is backed up) |
| 「取得失敗」 (lookup failed) stays in `papers add` however many times you retry | The other side (Crossref, arXiv) is down, or the response in `.cache/` is corrupt. Wait a few hours and try again. If it is still the same, look at the `error` of that record with `papers recent` |
| `索引が読み込み後に変更されていた` (the index was changed after it was loaded) | Two commands were run at the same time. Try again after one of them has finished (the result of the command that could not write is left in `papers.jsonl.conflict-*`) |
| Image-only PDFs cannot be read | OCR uses Vision on macOS (built with `swiftc` on first use. The Xcode command line tools are required). If it is not available, the tool falls back to `tesseract` |
| Queries to Crossref are slow or hang | Put `mailto` (a contact email address) in `config.json`. Crossref puts users who give a contact into a stable pool |

## 8. Glossary of Japanese output

Command output, generated folder names and code comments are in Japanese.

| Japanese | Meaning |
|---|---|
| 有り / 無し | held / not in the index |
| 候補(未検証) | candidate only, unverified |
| 人手行き | needs a person |
| 取得失敗 | lookup failed, will be retried |
| 重複 / `_重複文献/` | duplicate / the duplicates holding folder |
| `_プレプリント(出版版あり)/` | holding folder for preprints whose published version you hold |
| 誌名・年・分野・第一著者 | journal, year, topic, first author (the view folders) |
