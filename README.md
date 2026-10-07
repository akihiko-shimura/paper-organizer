# paper-organizer

English | [日本語](README.ja.md)

Turn a folder of randomly named paper PDFs into a library you can search, cite from, and hand to an AI agent, without
importing the files into anything.

`paper-organizer` identifies each PDF against Crossref and arXiv, renames it to a readable name, sets duplicates aside,
and builds a local full-text index. It is one Python file with no dependencies beyond the standard library, driven from
the command line as `papers`.

```
1-s2.0-S0030401899001234-main.pdf   →  1999_Smith_OptCommun_164_1_123.pdf
oe-27-8-11018.pdf                   →  2019_Smith_OptExpress_27_8_11018.pdf
2001.01234v2.pdf                    →  2020_Sato_arXiv_2001.01234.pdf
```

(The names above are made-up examples of the naming scheme: `year_FirstAuthor_Journal_volume_issue_page.pdf`.)

**Status.** Used by its author on one collection of about 3,200 PDFs. macOS only for now. Command output is
currently in Japanese (see [Limitations](#limitations-and-known-issues)).

## Contents

1. [Why this exists](#why-this-exists)
2. [How it differs from other tools](#how-it-differs-from-other-tools)
3. [How it works](#how-it-works)
4. [How accurate it is](#how-accurate-it-is)
5. [Why this design, and what was rejected](#why-this-design-and-what-was-rejected)
6. [Getting started](#getting-started)
7. [Using it](#using-it)
8. [Safety](#safety)
9. [What leaves your machine](#what-leaves-your-machine)
10. [Limitations and known issues](#limitations-and-known-issues)
11. [Cost](#cost)
12. [Roadmap](#roadmap)
13. [License and acknowledgements](#license-and-acknowledgements)

## Why this exists

A paper folder that has grown for years tends to look the same everywhere: `1-s2.0-S…-main.pdf`, `oe-27-8-11018.pdf`,
`document(3).pdf`, the same paper saved three times under three names, scans with no text layer. Finding a paper means
remembering which folder it is in. Answering "do I already have this paper?" means searching by eye.

Two things were wanted:

- **A filename a person can read**, generated from the publisher's own record rather than typed by hand.
- **An index an AI agent can query.** When an assistant drafts a literature section or checks a reference list, it
  should be able to ask "is DOI X on disk, and where?" and get an exact answer, not a guess.

Reference managers solve the first by taking the files into their own library. This tool leaves the files where they
are, in the folders you made, and keeps its index as a plain-text file of its own.

## How it differs from other tools

The comparison below is based on each project's public documentation as of October 2026. Check the links for the
current state.

| | Reference managers (e.g. [Zotero](https://www.zotero.org/support/retrieve_pdf_metadata)) | Identifier finders and renamers (e.g. [pdf2doi](https://github.com/MicheleCotrufo/pdf2doi), [pdf-renamer](https://github.com/MicheleCotrufo/pdf-renamer)) | Layout extractors (e.g. [GROBID](https://grobid.readthedocs.io/en/latest/Introduction/)) | paper-organizer |
|---|---|---|---|---|
| Where the files live | Imported into the manager's library | Stay in place | Stay in place | Stay in place, in your own folders |
| Where metadata comes from | First pages' text is sent to the manager's lookup service | Finds a DOI or arXiv ID (metadata, filename, text, then a web search) and resolves it | A trained model reads the header fields off the page; optional consolidation with Crossref | Finds an identifier or a query in the PDF; **values always come from Crossref or arXiv** |
| Check that the record is this PDF's | — | Confirms the identifier exists | — | The returned record must match the page-1 header (first author plus year, journal, volume or page). Otherwise it is held for a person |
| Runs on | Desktop app | Python package | Java service or Docker | One Python file, standard library only |
| Built for | Writing and citing | Renaming | Text mining at scale | Cleaning up an existing pile, and answering an agent's "do I have it?" |

What is specific to this tool:

- **A verification step with a measured reason.** A DOI printed in a PDF is not always that PDF's DOI: it may come
  from the reference list, or the Crossref record may be a different edition with the same title. Before the check
  was added, 5 or 6 of 214 files (2.3 to 2.8%) were silently given another paper's name.
- **An honest "don't know".** Records that fail the check are `unverified`; files with no match are `unresolved`.
  Neither is renamed, and both are listed for a person. On the author's collection that is 27% of files.
- **Nothing is deleted, and every move can be reviewed first.** See [Safety](#safety).
- **Commands shaped for agents**: exact lookup by DOI or arXiv ID, compact JSON, BibTeX, and a
  [Claude Code skill](skills/paper-search/SKILL.md) that tells the agent which records it may state as fact.

When another tool is the better choice:

- You want a citation plug-in for your word processor, annotation sync, or group libraries: use a reference manager.
- You are on Windows or Linux (not supported yet).
- Most of your files are theses, books, reports or non-English papers. These are largely absent from Crossref, so
  they end up in the "don't know" pile.

## How it works

The principle: **identification is lookup, not generation.** The tool takes only *keys* from the PDF (a DOI, an arXiv
ID, or a search query). Every bibliographic value (title, authors, journal, volume, pages, year) comes from the
registry that the key resolves to. Nothing infers a field value.

```
PDF ──► text of pages 1–2 (pdftotext; OCR if there is no text layer; download cover pages are skipped)
     ──► try each rung in order until one candidate passes the header check
           1. arXiv stamp in the margin            → arXiv API   (a preprint is its own record)
           2. DOI in the PDF's embedded metadata   → Crossref
           3. DOI printed in the text              → Crossref
           4. arXiv ID in the text or filename     → arXiv API
           5. first 300 characters as a query      → Crossref; accept only if the returned title is literally
                                                     in the text, and not only inside a citation
           6. guessed title as a query             → Crossref; accept only at ≥ 0.92 normalised similarity
     ──► header check: first author's surname AND (year OR journal OR volume OR first page)
         must appear in the first 1,500 characters of page 1
     ──► resolved │ unverified (candidate failed the check) │ unresolved │ supplement │ error (will be retried)
```

Around that core:

| Step | What runs |
|---|---|
| OCR | Apple's Vision framework, called from a 20-line Swift program, only when a PDF has fewer than 200 characters of text. Falls back to `tesseract` if `swiftc` is absent |
| Abstracts | `enrich` looks each resolved DOI up in OpenAlex |
| Duplicates | `dedup` groups resolved files by DOI, keeps the largest and moves the rest to a holding folder. A preprint whose published version you also hold is moved to a second holding folder |
| Renaming | `rename` builds `year_FirstAuthor_Journal_volume_issue_page.pdf` from the index. Only resolved records are renamed |
| Search | `index` builds a SQLite FTS5 (trigram) table over title, authors, journal, DOI, abstract, folder name and the first 3,000 characters of text |
| Browsing | `views` builds a folder of symlinks by journal, year, topic and first author, for Finder |
| Retries | HTTP 429 and 5xx are retried with back-off. A lookup that still fails marks the file `error` so the next run retries it, instead of recording "no match" |
| Optional page check | `vet.py` sends an image of page 1 to Claude Haiku and asks for the main article's title. It can only *veto*: see below |

All state is one JSON-lines file (`papers.jsonl`), one record per PDF, including which rung resolved it. The SQLite
file is derived from it and can be rebuilt at any time. Details and field list: [docs/design.md](docs/design.md).

## How accurate it is

Everything below was measured on a single collection: about 3,200 PDFs, mostly optics and condensed-matter physics,
published from the 1950s to 2026, mostly in English. **Other fields will differ**, in both the resolution rate and the
kinds of error. Numbers are given with their sample size; a rate from a small sample is a bound, not an estimate.
The counts were taken on different dates between September and October 2026, while duplicates were being removed
and new files added, so totals differ slightly from one table to the next.

**Outcome of identification** (n = 3,175 files, October 2026):

| Outcome | Files | Share |
|---|---|---|
| Resolved (these are the files that get renamed) | 2,291 | 72.2% |
| — DOI printed in the text | 1,283 | 40.4% |
| — query from the first 300 characters | 548 | 17.3% |
| — DOI in embedded metadata | 353 | 11.1% |
| — arXiv | 87 | 2.7% |
| — title search | 13 | 0.4% |
| — entered by hand | 7 | 0.2% |
| Unverified (a candidate exists but failed the header check) | 177 | 5.6% |
| Unresolved | 664 | 20.9% |
| Supplementary material (left alone) | 43 | 1.4% |

843 files need a person: the unverified and unresolved ones, plus 2 edge cases. For 617 of
them the title is readable but Crossref does not hold the work: theses, books, conference abstracts, reports,
datasheets.

**Are the resolved ones right?** Errors hide among the resolved records, so that is where samples were drawn.

| Check | Sample | Result |
|---|---|---|
| Stratified random sample across rungs, each compared with page 1 by eye | 38 judgeable | 0 wrong. With n = 38 this only shows the error rate is below roughly 10% |
| Before the header check existed | 214 | 5 or 6 wrong (2.3–2.8%) |
| Renamed files compared with page 1 | 20 random | 20 names match the content |
| Page check with Claude Haiku over all resolved records | about 2,350 | 8 flagged; on inspection 2 were real misidentifications and 6 were false alarms |
| Page check on 50 newly added files | 50 | 0 flagged |

Audits also found error types the random sample missed. The largest: a PDF whose first page begins with the tail of
the *previous* article (common in older letters journals), so the header contains someone else's references. Seven
such cases are known; the current rules stop six. This is why the optional page check exists.

What these numbers do **not** establish: the precision of the records the page check did not flag (unmeasured), and
anything about collections in other fields or languages.

**Other measurements**

| What | Result |
|---|---|
| OCR (Apple Vision) on scanned pages, against the text layer of the same pages | 91.7% word recall for English at 1.3 s per page; 98% character recall for Japanese (one document) |
| Abstract coverage after `enrich` | 2,048 of 3,175 files (65%) |
| Search: rank of the right paper for 8 test queries | Full-text (FTS5): `1, 4, 2, 1, 1, 1, 4, 1`. Apple sentence embeddings on titles: `1, 1, 1, 1, 17, 8, 15, 13` |
| Finding the published version of an arXiv preprint by title and first author (Crossref) | Of 63 preprints whose published DOI is known, 59 were matched to it and 1 to a different DOI |

## Why this design, and what was rejected

Each choice below was made after measuring the alternative on the same collection.

| Alternative | What was measured | Decision |
|---|---|---|
| Let a model (LLM or layout parser) produce the title, authors and journal | A registry lookup returns the publisher's own record, including the spelling of names. Existing filenames with misspelt authors were corrected by the lookup | Take only keys from the PDF. Never generate a value |
| Use an LLM to propose a title when no DOI is found | Sending the first 300 characters to Crossref's bibliographic query, and accepting only titles that literally occur in the text, reproduced 17 of the 20 files the LLM step had resolved, with 0 wrong | The LLM step was removed |
| Accept Crossref's top hit above a relevance score | The score values overlap: a correct hit scored 59, unrelated text 23, and wrong papers 37 to 42 | Never use the score. Compare strings |
| Accept on title similarity alone | A conference abstract, the journal paper and a later book chapter can share one title exactly (similarity 1.00) | Require the header check |
| Make the header check stricter (require volume or page) | Would reject 342 of 2,308 resolved files (14.8%); 14 sampled were nearly all correct. Many journals print neither on page 1 | Not adopted |
| Trust the PDF's embedded metadata for values | Embedded fields are typed by hand: a year written with a letter O for the zero, author names in inconsistent formats | Use embedded metadata only to find a DOI |
| On-device LLM OCR | 45.6% recall at 70 s per page; 2 of 4 files returned nothing, silently | Call Apple Vision directly |
| Text rules to detect "page 1 starts with another article" | Three successive rule sets each missed new cases | Look at the page instead (next row) |
| Let a vision model decide the identity | Claude Haiku read the correct main title on 7 of 7 known bad pages and 30 of 30 good ones (Apple Vision layout heuristics: 6 of 7 and 20 of 30). But the same title can belong to several editions | Haiku gets a **veto only**: if the title it reads is literally in the PDF and disagrees with the record, the record goes to a person |
| Semantic (embedding) search by default | See the ranks above: better on 2 of 8 queries, much worse on 4, and a hybrid of the two was worse than full-text on 3 of 8 | Full-text search is the default; embeddings remain optional |
| Sort files into topic folders automatically | Topics were available for only 56% of files, and the existing folders carry the owner's own grouping | Leave files where they are; offer symlink views instead |
| Delete duplicates | — | Never. Duplicates are moved aside; deleting is the owner's decision |

The longer account, including what each safeguard does *not* catch, is in [docs/design.md](docs/design.md).

## Getting started

**Requirements**

- macOS (developed on macOS 27)
- Python 3.9 or later (no packages to install)
- [poppler](https://poppler.freedesktop.org/) for `pdftotext`, `pdfinfo` and `pdftoppm`: `brew install poppler`
- Optional: Xcode command-line tools (`swiftc`) for OCR and for the optional embedding search
- Optional: [uv](https://docs.astral.sh/uv/) and an Anthropic API key, only for the page check (`vet.py`)

**Install**

```bash
git clone https://github.com/<owner>/paper-organizer.git
cd paper-organizer
mkdir -p ~/.local/bin
ln -s "$PWD/organize.py" ~/.local/bin/papers     # make sure ~/.local/bin is on your PATH
papers selftest                                   # offline self-check; prints "selftest: ok"
```

**Configure** (optional but recommended)

```bash
cp config.example.json config.json
```

Set `mailto` to your email address. It is sent to Crossref and OpenAlex as a contact, which places your requests in
their more reliable "polite" pool. `config.json` is ignored by git.

For abstracts, an OpenAlex API key (free) is recommended: set `OPENALEX_API_KEY`, or store it in the macOS keychain as
`openalex-api-key`.

**First run**

Try it on a copy of a small folder first.

```bash
papers add ~/Papers          # identify every PDF under the folder and build the index
```

The first scan is the slow part: a few seconds per file, so roughly 2 to 4 hours for 3,000 PDFs. It can be
interrupted and resumed; responses are cached. Nothing is moved or renamed yet. `add` ends by printing what `dedup`
and `rename` *would* do. When you are satisfied:

```bash
papers dedup --apply         # move duplicates aside (never deletes)
papers rename --apply        # rename resolved files
papers index && papers views # rebuild the search index and the Finder views
```

After that, adding new PDFs is `papers add` again; it only looks at files not yet in the index.

## Using it

The full guide is [MANUAL.md](MANUAL.md). The commands you will use most:

| Command | What it does |
|---|---|
| `papers add [folder]` | Index new PDFs and show planned duplicates and renames. Moves nothing |
| `papers search "fiber laser"` | Full-text search. Supports `AND`, `OR`, `NOT`, `"phrases"`, `title:`, `author:`, `journal:` |
| `papers has 10.1234/abcd arXiv:2001.01234` | Exact answer: do I hold this DOI or arXiv ID, and where? |
| `papers cite 10.1234/abcd` | BibTeX |
| `papers info` | Where the index and library are, how many records, whether the search index is stale |
| `papers fix NAME --doi DOI` | Correct one record (shows the change first; `--apply` writes) |
| `papers review` → `papers merge` | Fill in unresolved files in bulk, with `y`/`n` on suggested candidates |
| `papers move NAME --to FOLDER` | Move files and update the index together |
| `papers relocate` | Repair the index after you moved files by hand |
| `papers views` | Rebuild the symlink folders for Finder |
| `papers undo` | Revert the last rename or move |

Add `--json` to `search`, `has`, `info` and `recent` for one record per line.

**With Claude Code**

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
ln -s "$PWD/skills/paper-search" ~/.claude/skills/paper-search
ln -s "$PWD/agents/paper-librarian.md" ~/.claude/agents/paper-librarian.md
```

- The **skill** loads when you ask things like "do I have the paper on …?" or "find papers about … in my library". It
  tells the agent to answer with file paths, to state bibliographic details as fact only for verified records, and
  never to modify the library while searching.
- The **librarian sub-agent** does library chores (adding PDFs, selecting papers by condition, proposing moves) in a
  small context of its own. It stops at a dry run and returns the plan; it applies changes only when the request says
  the owner has approved a specific list.

Both files are written in Japanese with English trigger phrases; Claude follows them in either language.

## Safety

- **No PDF is ever deleted.** Duplicates are moved to a holding folder inside the library. Deleting them is up to you.
- **Dry run by default.** `dedup`, `rename`, `move`, `relocate` and `fix` print the plan and change nothing until you
  add `--apply`. `add` refuses `--apply` altogether.
- **Backups before writes.** Commands that rewrite the index first copy it to `backups/`. Index and cache files are
  written atomically, so an interrupted run leaves the previous version intact.
- **Undo.** `rename` and `move` record every file they touch; `papers undo` reverts the last run. `dedup` writes the
  same kind of record (reverting it is manual).
- **Paid calls need consent.** `vet.py` prints a cost estimate and submits nothing without `--apply`.
- **The checks are themselves checked.** Each safeguard has a test, and each test was confirmed to fail when the code
  it guards is deliberately broken.

## What leaves your machine

| Destination | What is sent | When |
|---|---|---|
| Crossref | DOIs found in your PDFs; the first 300 characters of a PDF's text and guessed titles, as search queries; your contact email if configured | `add`, `scan`, `review`, `pubver`, `fix --doi` |
| arXiv | arXiv IDs | `add`, `scan` |
| OpenAlex | DOIs of resolved records; your API key and contact email if configured | `enrich` (part of `add`) |
| Anthropic | An image of page 1 of each PDF being checked | Only if you run `vet.py --apply` |

Apart from that optional page image, the PDF files are never uploaded. The index (`papers.jsonl`) stores the first 3,000 characters of each PDF and
its abstract so that search works offline. **Do not publish your index**: it contains excerpts of copyrighted papers.
It is ignored by git for that reason.

## Limitations and known issues

- **macOS only.** OCR and the optional embeddings use Apple frameworks; key storage uses the macOS keychain;
  searching beyond the first 3,000 characters of a PDF relies on Spotlight.
- **Command output, generated folder names and code comments are in Japanese.** A short glossary:

  | Japanese | Meaning |
  |---|---|
  | 有り / 無し | held / not in the index |
  | 候補(未検証) | candidate only, unverified |
  | 人手行き | needs a person |
  | 取得失敗 | lookup failed, will be retried |
  | 重複 / `_重複文献/` | duplicate / the duplicates holding folder |
  | `_プレプリント(出版版あり)/` | holding folder for preprints whose published version you hold |
  | 誌名・年・分野・第一著者 | journal, year, topic, first author (the view folders) |

- **One person, one machine.** The index stores absolute paths and lives in the repository folder. Sharing an index
  between machines is not supported.
- **Not everything can be identified.** Theses, books, many conference abstracts and most non-English papers are not
  in Crossref. Titles in Japanese (and other non-Latin scripts) are not matched at all.
- **Same title, different edition.** If page 1 prints no journal, volume or page, a proceedings version and the
  journal version cannot be told apart.
- **Preprints.** Only arXiv is recognised. A preprint and its published version are treated as two papers, linked.
- **Duplicates are detected by DOI only.** The same paper registered under two DOIs stays as two files.
- **Measured on one collection.** See [How accurate it is](#how-accurate-it-is).

The full list of open issues is kept in [docs/design.md](docs/design.md).

## Cost

Identification, abstracts and search use free public APIs. The only paid feature is the optional page check
(`vet.py`, Claude Haiku via the Batch API): 3.31 USD for 3,203 files, and 0.05 USD for 50 files, on the author's
collection.

## Roadmap

- Windows support (replace the Apple-only OCR and key storage)
- Keep data outside the repository folder, and store paths relative to the library, so one library can be used from
  more than one machine
- English command output
- A try-out by someone in a different field, to measure accuracy outside physics

## License and acknowledgements

MIT. See [LICENSE](LICENSE).

Bibliographic data comes from [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/) and arXiv.
Thank you to arXiv for use of its open access interoperability.

This project was developed with [Claude Code](https://claude.com/claude-code).
