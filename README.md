# paper-organizer

English | [日本語](README.ja.md)

Turn a folder of randomly named paper PDFs into a library you can search, cite from, and hand to an AI agent, without
importing the files into anything.

`paper-organizer` identifies each PDF against Crossref and arXiv, renames it to a readable name, sets duplicates aside,
and builds a local search index. It is one Python file with no dependencies beyond the standard library, run from the
command line as `papers`.

```
1-s2.0-S0030401899001234-main.pdf   →  1999_Smith_OptCommun_164_1_123.pdf
oe-27-8-11018.pdf                   →  2019_Smith_OptExpress_27_8_11018.pdf
2001.01234v2.pdf                    →  2020_Sato_arXiv_2001.01234.pdf
```

(Made-up examples of the naming scheme: `year_FirstAuthor_Journal_volume_issue_page.pdf`.)

**Status.** Used by its author on one collection of about 3,200 PDFs. macOS only for now. Command output is in
Japanese.

This page is the short version, for people who want to use the tool.

- Day-to-day use: [MANUAL.md](MANUAL.md)
- For developers (internals, every measurement, rejected alternatives, known gaps): [docs/design.md](docs/design.md)

## Why this exists

A paper folder that has grown for years tends to look the same everywhere: `1-s2.0-S…-main.pdf`, `oe-27-8-11018.pdf`,
`document(3).pdf`, the same paper saved three times under three names, scans with no text layer. Finding a paper means
remembering which folder it is in, or searching Spotlight for an author, a journal, a year or a keyword. Answering
"do I already have this paper?" means checking those results by eye.

Two things were wanted:

- **A filename a person can read**, generated from the publisher's own record rather than typed by hand.
- **An index an AI agent can query.** An assistant checking a reference list should be able to ask "is DOI X on disk,
  and where?" and get an exact answer, not a guess.

Reference managers do this by taking the files into their own library. This tool leaves the files where they are, in
the folders you made.

### Why not just Spotlight?

Spotlight searches the words inside files. It does not know which paper a file is, so a paper about a topic and a
paper that mentions it once come back as equals. Measured on the author's collection:

| Question | Spotlight | paper-organizer |
|---|---|---|
| Keyword search (8 phrases) | 669 files per phrase on average; 7% have the words in the title | Ranked; 81% of the top 10 have the words in the title |
| Do I have this DOI? (60 papers) | Found 37. The other 23 PDFs do not print their DOI | Found 60 |
| Papers by an author (15 surnames) | 5% of the files returned are by the author; the rest cite the name | `author:` searches the author field only |
| Find a scanned PDF (37 scans) | Found 1 | Scans are OCRed and indexed |

Spotlight is the better tool for words that occur only deep inside a paper. It reads every page; this index holds
the first 3,000 characters and the abstract. The order in which Spotlight shows results on screen could not be
measured. Method and caveats: [docs/design.md, section 13](docs/design.md#13-search).

## How it differs from other tools

Based on each project's public documentation as of October 2026.

| | Reference managers ([Zotero](https://www.zotero.org/support/retrieve_pdf_metadata)) | Renamers ([pdf2doi](https://github.com/MicheleCotrufo/pdf2doi), [pdf-renamer](https://github.com/MicheleCotrufo/pdf-renamer)) | Layout extractors ([GROBID](https://grobid.readthedocs.io/en/latest/Introduction/)) | paper-organizer |
|---|---|---|---|---|
| Files | Imported into its library | Stay in place | Stay in place | Stay in place |
| Metadata from | Its lookup service | Lookup of a DOI or arXiv ID | A model reads the page | Lookup in Crossref or arXiv only |
| Check that the record is this PDF's | — | Identifier exists | — | Record must match the page-1 header |
| Runs as | Desktop app | Python package | Java service or Docker | One Python file |
| Built for | Writing and citing | Renaming | Text mining at scale | Cleaning up a pile; an agent's "do I have it?" |

What is specific to this tool:

- **Every lookup is verified.** A DOI printed in a PDF may come from its reference list. Without the check, 5 or 6
  of 214 files were silently given another paper's name.
- **An honest "don't know".** A file that fails the check is not renamed. It is listed for a person.
- **Commands shaped for agents**: exact lookup by DOI or arXiv ID, JSON output, BibTeX, and a
  [Claude Code skill](skills/paper-search/SKILL.md).

Another tool is the better choice if you want a citation plug-in or annotation sync (use a reference manager), if you
are on Windows or Linux, or if most of your files are theses, books or non-English papers.

## How it works

**Identification is lookup, not generation.** The tool takes only a key from the PDF: a DOI, an arXiv ID or a search
query. Every bibliographic value comes from the registry the key points to. Nothing is inferred.

```
PDF → text of pages 1–2 (OCR if there is no text layer)
    → find a key, in this order, until one passes the check
        1. arXiv stamp in the margin          → arXiv
        2. DOI in the PDF's metadata          → Crossref
        3. DOI printed in the text            → Crossref
        4. arXiv ID in the text or filename   → arXiv
        5. first 300 characters as a query    → Crossref (the title must occur literally in the text)
        6. guessed title as a query           → Crossref (similarity ≥ 0.92)
    → check: first author's surname AND (year OR journal OR volume OR first page)
             must appear in the first 1,500 characters of page 1
    → resolved │ unverified │ unresolved      (only resolved files are renamed)
```

After identification:

- **Abstracts** are fetched from OpenAlex.
- **Duplicates** (same DOI) are moved to a holding folder. The largest file stays.
- **Search** uses a SQLite full-text index over title, authors, journal, abstract and the first 3,000 characters.
- **Views** are folders of symlinks by journal, year, topic and first author, for Finder.
- **Optional page check** (`vet.py`): Claude Haiku reads the title from an image of page 1. It can only send a record
  back to a person. It never sets a value.

All state is one JSON-lines file, `papers.jsonl`, with one record per PDF.

## How accurate it is

Measured on one collection: about 3,200 PDFs, mostly optics and condensed-matter physics, mostly English. Other
fields will differ.

| | Result |
|---|---|
| Identified and renamed | 2,291 of 3,175 files (72%) |
| Left for a person | 843 files (27%) |
| Wrong among the identified, in a random sample checked by eye | 0 of 38 |
| Wrong before the check existed | 5 or 6 of 214 (2.3–2.8%) |

- A result of 0 of 38 only shows that the error rate is below roughly 10%.
- The files left for a person include theses, books, reports and lecture notes, which Crossref does not hold, and
  papers the automatic query missed.

All measurements, with sample sizes: [docs/design.md, section 16](docs/design.md#16-measurements-in-one-place).

## Why this design

- **Lookup instead of an LLM or a layout parser.** A registry returns the publisher's own record, including the
  spelling of names. A model that writes the fields can be plausibly wrong.
- **String comparison instead of a relevance score.** Crossref's scores for right and wrong hits overlap.
- **Full-text search by default, not embeddings.** In a small test (8 queries on 20 files) a general-purpose
  embedding ranked the right paper lower. Embedding search is included as an option.
- **Files stay where they are.** Nothing is sorted into folders for you, and nothing is deleted.

The twelve alternatives that were measured and rejected:
[docs/design.md, section 17](docs/design.md#17-alternatives-measured-and-rejected).

## Getting started

**Requirements**

- macOS
- Python 3.9 or later (no packages to install)
- [poppler](https://poppler.freedesktop.org/): `brew install poppler`
- Optional: Xcode command-line tools (`swiftc`), for OCR
- Optional: [uv](https://docs.astral.sh/uv/) and an Anthropic API key, for the page check (`vet.py`)

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

Set `mailto` to your email address. Crossref and OpenAlex give requests with a contact address more reliable service.
For abstracts, a free OpenAlex API key is recommended: set `OPENALEX_API_KEY`, or store it in the macOS keychain as
`openalex-api-key`.

**First run**

Try it on a copy of a small folder first.

```bash
papers add ~/Papers          # identify every PDF under the folder and build the index
```

The first scan takes a few seconds per file, so 2 to 4 hours for 3,000 PDFs. It can be interrupted and resumed.
Nothing is moved or renamed yet: `add` ends by printing what `dedup` and `rename` would do. When you are satisfied:

```bash
papers dedup --apply         # move duplicates aside (never deletes)
papers rename --apply        # rename resolved files
papers index && papers views # rebuild the search index and the Finder views
```

To add new PDFs later, run `papers add` again. It only looks at files not yet in the index.

## Using it

| Command | What it does |
|---|---|
| `papers add [folder]` | Index new PDFs; show planned renames and duplicates |
| `papers search "fiber laser"` | Search. `AND`, `OR`, `NOT`, `"phrases"`, `title:`, `author:`, `journal:` |
| `papers has 10.1234/abcd` | Is this DOI or arXiv ID held, and where |
| `papers cite 10.1234/abcd` | BibTeX |
| `papers info` | Location and size of the index |
| `papers fix NAME --doi DOI` | Correct one record |
| `papers review` → `papers merge` | Confirm candidates for unresolved files in bulk |
| `papers move NAME --to FOLDER` | Move files and update the index |
| `papers relocate` | Repair the index after moving files by hand |
| `papers undo` | Revert the last rename or move |

Add `--json` to `search`, `has`, `info` and `recent` for one record per line. Full guide: [MANUAL.md](MANUAL.md).

**With Claude Code**

```bash
mkdir -p ~/.claude/skills ~/.claude/agents
ln -s "$PWD/skills/paper-search" ~/.claude/skills/paper-search
ln -s "$PWD/agents/paper-librarian.md" ~/.claude/agents/paper-librarian.md
```

- The **skill** loads when you ask "do I have the paper on …?" or "find papers about … in my library". The agent
  answers with file paths and states bibliographic details as fact only for verified records.
- The **librarian sub-agent** adds PDFs, selects papers and proposes moves. It stops at a dry run unless the request
  says you approved a specific list.

Both files are written in Japanese with English trigger phrases. Claude follows them in either language.

## Safety

- **No PDF is ever deleted.** Duplicates are moved to a holding folder. Deleting them is up to you.
- **Dry run by default.** `dedup`, `rename`, `move`, `relocate` and `fix` change nothing without `--apply`.
- **Backup and undo.** The index is copied to `backups/` before it is rewritten. `papers undo` reverts the last
  rename or move.
- **Paid calls need consent.** `vet.py` prints a cost estimate and sends nothing without `--apply`.

## What leaves your machine

| To | What | When |
|---|---|---|
| Crossref | DOIs; the first 300 characters of text and guessed titles, as queries; contact email if set | `add`, `scan`, `review`, `pubver`, `fix --doi` |
| arXiv | arXiv IDs | `add`, `scan` |
| OpenAlex | DOIs; API key and contact email if set | `add` |
| Anthropic | An image of page 1 | Only `vet.py --apply` |

The PDF files themselves are not uploaded. **Do not publish your index** (`papers.jsonl`): it holds the first 3,000
characters and the abstract of each paper, which are copyrighted text.

## Limitations

- **macOS only.** OCR and key storage use Apple frameworks.
- **Output is in Japanese.** A glossary is in [MANUAL.md](MANUAL.md#8-glossary-of-japanese-output).
- **One person, one machine.** The index stores absolute paths.
- **Not everything can be identified.** Theses, books and many conference abstracts are not in Crossref. Titles in
  Japanese and other non-Latin scripts are not matched.
- **Editions with the same title** cannot be told apart if page 1 prints no journal, volume or page.
- **Only arXiv preprints are recognised.** A preprint and its published version are two records, linked.
- **Duplicates are detected by DOI only.**
- **Search sees the first 3,000 characters.** Use Spotlight for words deeper in a paper.

Full list: [docs/design.md, section 15](docs/design.md#15-known-gaps).

## Cost

Identification, abstracts and search use free public APIs. The only paid feature is the optional page check
(`vet.py`, Claude Haiku): 3.31 USD for 3,203 files on the author's collection.

## Roadmap

- Windows support
- Data outside the repository folder and relative paths, so one library can be used from several machines
- English command output
- A try-out in a field other than physics

## License and acknowledgements

MIT. See [LICENSE](LICENSE).

Bibliographic data comes from [Crossref](https://www.crossref.org/), [OpenAlex](https://openalex.org/) and arXiv.
Thank you to arXiv for use of its open access interoperability.

This project was developed with [Claude Code](https://claude.com/claude-code).
