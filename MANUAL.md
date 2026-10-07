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
| Index (master copy) | `<repo>/papers.jsonl` | One paper per line. DOI, title, authors, journal, abstract, text of the first page, and so on |
| Search index | `<repo>/papers.db` | SQLite built from `papers.jsonl`. **Do not edit it directly** (only rebuild it) |
| Settings | `<repo>/config.json` (optional; the sample is `config.example.json`) | Contact email address (`mailto`), output location of the views (`views`) |
| Command `papers` | `~/.local/bin/papers` → link to `<repo>/organize.py` | Works from any directory |
| skill `paper-search` | `~/.claude/skills/paper-search` → `<repo>/skills/paper-search` | Gives AI agents (Claude Code) the instructions for use |

The index, the search index and the working files (`unresolved.tsv`, `review.html`, `published.tsv`, `backups/`, the rename log) are written to
`<repo>/` no matter where you call `papers` from. **None of them goes into git** (already in `.gitignore`; the index contains excerpts of the first page of each paper).

Use `papers info` to see locations, update times, sizes and record counts (read-only; `--json` for machines):

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

## 2. Searching

```bash
papers search "second harmonic generation"      # top 10 records
papers search "Smit"                            # part of an author name also matches
papers search "fiber laser" --n=30              # change the number of results
papers search "fiber laser" --full              # print title, DOI and abstract without truncating
papers search "fiber laser" --json              # JSON, one record per line (for agents; the first-page text is left out)
papers search "fiber laser" --json --with-text  # also include the text (the first 3,000 characters of page 1) (output is about 2.5 times larger)
```

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

- **Columns searched**: title, authors, journal, DOI, abstract, folder name, text of the first page (the first 3,000 characters). The weights are title > DOI, authors >
  journal > abstract > folder > text
- **How terms are handled**: terms of 3 or more characters are searched with OR, and a partial match is enough (`Smit` finds Smith). Terms of 2 characters or fewer are ignored
  (the two-character Japanese word 「整合」 cannot be found; the four-character 「位相整合」 can). Japanese terms also work if they have 3 or more characters
- **The text from page 2 onward is not searched.** Look for terms deep in the text with Spotlight (macOS):

  ```bash
  mdfind -onlyin "<library>" "Kramers-Kronig"
  ```

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
- Column names: `title`, `author` (all authors), `first_author`, `journal`, `doi`, `abstract`, `folder`, `text` (text of the first page)
- Enclose terms that contain a hyphen or a symbol in quotation marks (`"second-harmonic"`. Without them the command stops with a syntax error and shows how to fix it)
- Terms of 2 characters or fewer (`CW`, `UV`) cannot be looked up in the index. If you put one in an expression, a warning is shown and that condition matches 0 records
- In the shell, enclose the whole expression in `'…'` (so that `"…"` phrases can be written inside)

You can also query directly with SQL (read-only. Without `-list`, a recent sqlite3 formats the output as a table in the terminal and truncates the path):

```bash
sqlite3 -list -separator ' | ' "<repo>/papers.db" "SELECT year, first_author, title, path FROM p WHERE p MATCH 'title:laser AND title:fiber' ORDER BY bm25(p) LIMIT 20"
```

## 3. For AI agents (when searching)

In Claude Code, the skill `paper-search` is loaded by requests such as "find it among my papers" and gives the agent the following.
For an agent without the skill, have it read this file.

1. Query with `papers search "<term>" --json --n=20`. Query again 2 to 4 times, rephrasing the title, authors, journal and field.
   If nothing is found, search the text with `mdfind`. If the DOI or arXiv ID is known, use `papers has` instead of searching
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
renames → search index. If you give a folder (`papers add <folder>`), it looks only under that folder.

- For each record, the report shows the "record (authors, year, journal, title)" next to the "beginning of the text". **Check whether these two lines are the same paper**
  (`add` does not judge this). For a PDF with a cover page, the beginning of the text is taken from page 2
- Fill in records shown as 「人手行き」 (needs a person) with `fix` (one record) or `review` (in bulk) in §5. Records whose reason is 「F_記録に著者か年が無い」 (record has no author or year) are suspected of
  being attached to a record that is not a paper, such as a journal cover or front matter (for example, the title of the record is the journal name)
- Records shown as 「取得失敗」 (lookup failed) are ones for which no response was obtained from Crossref or arXiv (a delay or outage on their side). They are not identified yet.
  Wait a while and run `papers add` again; it fetches only those records again
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
papers index                  # rebuild the search index (about 15 seconds for a few thousand records)
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

  Look at the estimate and, if it is acceptable, add `--apply` to the same command (Batch API. It was 3.31 dollars for 3,203 records and 0.05 dollars for 50 records).
  Pass the key in the environment variable (if it is stored in the macOS keychain:
  `ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-api-key -w)`).

  `papers add` does not call this (it is paid, so the estimate and the consent come first). You can run it once after several additions.
  If the title of the record and the title on the page disagree, the record is sent back to 人手行き (needs a person) and listed. For 50 records it took
  13 to 14 minutes until the result came back (a single measurement). When it finishes, run `papers index`

## 5. Correcting records

### Correcting one record: `fix`

```bash
papers fix <part of file name> --doi <correct DOI>                  # look the record up again from Crossref with the correct DOI
papers fix <part of file name> --set year=1993 --set first_author=Yamada   # correct fields directly
```

- The first argument is part of a file name or a full path. **The command stops if this does not narrow down to one record** (it shows the candidates that matched)
- By default it only shows before → after. Check it, then add `--apply` to write (it backs up to `backups/` before writing)
- Fields that can be corrected: `doi` `year` `first_author` `journal` `journal_short` `volume` `issue` `pages` `title` `article_number`
  `published_doi`. A corrected record gets `rung: manual` (a value decided by a person. Automatic checks do not overwrite it)
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

### Files moved, renamed or deleted by hand: `relocate`

When you move a file with Finder or the like, the index keeps the old path (`papers index` alone does not fix this, because the master copy of the index
holds the old path). If `papers info` shows 「※ パスにファイルが無い記録」 (records whose file is missing):

```bash
papers relocate            # only show the matches
papers relocate --apply    # back up the index, then write
papers index
```

- A record is matched to a file that has the same name and whose text on pages 1 to 3 also agrees. If you changed the name too, the file is searched for by text alone. Scans with no text
  are matched by name alone (shown as 「名前だけ」, matched by name only). Those that cannot be decided are only shown, not written
- Duplicates that you deleted from `_重複文献/` (duplicates) or `_プレプリント(出版版あり)/` are removed from the index (the arXiv ID of a deleted preprint is moved to the record of the
  published version, so `papers has` answers 「有り(出版版)」 (held as the published version) for the arXiv ID as well)
- A PDF that matches no record is a new file. Add it with `papers add`
- **Do not run `scan` or `add` first**: they identify the moved files again as new files, and the bibliographic data and decisions that you corrected by hand are
  left behind on the old records. When `scan` finds that a new file is a moved copy of the file of an index record, it stops without identifying and
  prompts you to run `relocate` (use `--force` if the file was not moved)

### Undoing

- Renames and `move`: `papers undo` (reverts the most recent one)
- Moves of duplicates: `<repo>/dedup-<time>.jsonl` records the source and the destination of each move (there is no automatic undo. Move the files back by hand and run `relocate`)
- Index: `<repo>/backups/` holds the `papers.jsonl` from before each rewrite (`papers-<time>-before-<what>.jsonl`).
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
