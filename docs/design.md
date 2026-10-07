# Design notes

English | [日本語](design.ja.md)

This document explains how `paper-organizer` identifies PDFs, why it is built this way, what was measured, and what
each safeguard does **not** catch. It is a condensed, public version of the working design record; individual papers
from the author's collection have been left out.

The [README](../README.md) is the short version for people who want to use the tool. This document is for people
who want to change it or check its claims. All measurements are collected in section 16, and the alternatives that
were measured and rejected in section 17.

All measurements come from one collection: about 3,200 PDFs, mostly optics and condensed-matter physics, mostly
English, published from the 1950s to 2026. Sample sizes are stated with every number. Treat them as evidence about
this collection, not as general accuracy figures.

Counts were taken on different dates. The collection held 3,248 files at the first full scan (September 2026) and
3,175 in October, after duplicates had been removed and new files added, so totals differ between sections.

## 1. The central decision

**Identification is lookup, not generation.** The pipeline never infers a bibliographic value. It extracts only an
*identifier* or a *search query* from the PDF, and the values are always those returned by Crossref, arXiv or OpenAlex.

The reason is that metadata has a ground truth. A DOI lookup returns the publisher's registered spelling of every
author, the volume and the page. Looking up and being right is better than inferring and being 80 to 90% right. In
practice the lookups corrected misspelt author names in existing, hand-typed filenames.

**But "looked up, therefore correct" does not hold automatically.** Which identifier or query belongs to *this* PDF is
itself an estimate. Without verification, a DOI picked up from the reference list, or a different edition with the
same title, is adopted silently. Before verification was added, 5 or 6 of 214 files (2.3 to 2.8%) were wrong in exactly
this way.

## 2. The ladder

`resolve()` reads pages 1 and 2 with `pdftotext` and tries the rungs in order. The first candidate that passes the
header check (section 3) wins.

| Rung (`rung` in the index) | Evidence taken from the PDF | Authority | Share of files (n = 3,175) |
|---|---|---|---|
| `arxiv_text` (stamp) | The arXiv stamp printed in the margin: ID, version, category and date | arXiv API | 2.7% (all arXiv rungs) |
| `pdfinfo_doi` | A DOI in the embedded metadata | Crossref | 11.1% |
| `text_doi` | A DOI matched by regular expression in the text | Crossref | 40.4% |
| `arxiv_text` / `arxiv_name` | An arXiv ID in the text or the filename | arXiv API | (included above) |
| `bib_query` | The first 300 characters of text, sent as a bibliographic query | Crossref | 17.3% |
| `title_search` | A heuristically guessed title, sent as a title query | Crossref | 0.4% |
| `manual` | Entered or corrected by a person | — | 0.2% |
| `unverified` | A candidate was found but failed the header check | — | 5.6% |
| `unresolved` | Nothing found | — | 20.9% |
| `supplement` | Page 1 is supplementary material (applied to any result on exit) | — | 1.4% |
| `error` | Reading raised an exception, or an external lookup could not be completed (only the latter is retried) | — | 0 at present |

Acceptance conditions for the two search rungs:

- **Crossref's relevance score is not usable as a threshold.** Measured: correct hits scored 59, unrelated noise 23,
  and wrong papers 37 to 42. They do not separate.
- `bib_query` accepts a result only if the returned title occurs **literally** in the PDF text (after normalisation),
  and not only inside a citation (section 5). No ratio is used.
- `title_search` accepts only at a normalised similarity of 0.92 or more. `difflib.SequenceMatcher` is greedy and its
  ratio depends on argument order, so the larger of the two orders is used.

**Order matters.** `bib_query` runs before `title_search`. When `title_search` ran first, 118 files were resolved by
it; re-running `bib_query` on those gave the same DOI for 99, no candidate for 15, and a different DOI for 4. Of the
4, two were errors of `title_search` (a conference abstract by the same authors in the same year; another paper by the
same authors in the same journal a year later) and two were one paper registered under two DOIs. `bib_query` sends
the page's own bibliographic line, so it landed on the right edition in both error cases.

**Embedded metadata is used only for keys.** Embedded author and date strings are unreliable (a letter O in place of
a zero in a year; initials before or after the surname), so only the DOI is taken from them.

## 3. The header check (`corroborate`)

Every candidate from every rung must pass:

> the first author's surname **and** at least one of (year, journal name, abbreviated journal name, volume, first
> page) occur in the **first 1,500 characters of page 1**.

The restriction to the header region is the point. The authors of cited papers also appear in the text, so matching
against the whole text proves nothing. A candidate that fails is not discarded: the record becomes `unverified` and is
listed for a person.

Three places where it is not strictly "the first 1,500 characters":

- For the embedded-DOI rung, the `pdfinfo` output is also searched. Embedded metadata describes the file itself, so
  reference-list contamination does not arise.
- For image-only PDFs, the first 1,500 characters of the OCR text of pages 1 and 2 are used. This may reach into page
  2; the guarantee is weaker here.
- A download cover that describes only this paper is appended to the header (section 4).

A stricter rule was measured and rejected: requiring the volume or first page would reject 342 of 2,308 resolved
files (14.8%), and a sample of 14 of those were nearly all correct. Many journals print neither on the first page.

**What it does not catch**

- **Same title, different edition, when page 1 prints no journal, volume or page.** The evidence is then only title
  and author.
- **A Crossref record with no author** (journal front matter, some book chapters). The author test is skipped when
  the record has no author, so the record can pass on the journal name alone. Such records cannot be renamed (no
  first author); they are now routed to a person instead of being silently left out of every list (3 known cases).
- **Correct candidates whose evidence lies beyond 1,500 characters** (long abstracts, bibliographic lines at the foot
  of the page). Of 116 `unverified` records whose candidate was later confirmed, 69 would pass against the whole of
  page 1. Widening the region was not adopted because the effect on wrong candidates was not measured.
- Numbers are searched inside the normalised, concatenated text, and values of two digits or fewer are ignored. Page
  `330` matches inside `1330`.

## 4. Reading the PDF

### Normalisation (`norm`)

All comparisons use `norm()`: NFKD decomposition, combining marks dropped, then only `a–z0–9` kept. Two earlier
failures motivated this: PDFs often store accented letters in decomposed form while Crossref returns them composed
(an author's name split into two spellings and failed the header check), and ligatures such as `ﬀ` vanished whole.
Re-resolving the full collection after the change: +27 resolved (27 of 27 checked correct), −1, and no resolved DOI
changed among the 3,218 unaffected files.

Not handled: non-Latin scripts (a Japanese title normalises to nothing and cannot be matched); PDFs that embed a
symbol as an unrelated letter; two different people whose names differ only by an accent (0 seen among the 27).

### OCR

OCR runs only when the text layer has fewer than 200 characters. It calls Apple's Vision framework
(`VNRecognizeTextRequest`) from a small Swift program on pages rendered at 200 dpi.

| Engine | English word recall | Japanese character recall | Seconds per page |
|---|---|---|---|
| Apple Vision, called directly | 91.7% | 98% (one document) | 1.3 |
| tesseract | 92.4% | not measured (language data not installed) | 1.9 |
| An on-device LLM OCR tool | 45.6%, and 2 of 4 files returned nothing | — | 70 |

Vision was chosen over tesseract because it needs no installation, reads Japanese without extra language data, and
is faster; the English recall is about the same.

The order of recognition languages decides accuracy: with `en-US` first, Japanese recall fell to 2–3%. The default is
`ja-JP,en-US` (override with the `OCR_LANG` environment variable; `en-US` alone is three times faster). 200 and
300 dpi did not differ meaningfully. Vertical Japanese text and degraded scans are untested.

### Download cover pages

Several services replace page 1 with a cover sheet (133 of 3,248 files). If page 1 is a cover, reading starts at
page 2. Covers are of two kinds:

- **Describing only this paper** (repository and aggregator covers, a proceedings publisher's cover). Added to the
  header and searched for a DOI first. Some proceedings print neither year nor volume on the paper itself; discarding
  these covers made 11 resolved files fail the header check.
- **Listing other papers too** ("you may also be interested in"). Used for nothing: they contain other authors'
  names, and one file had been resolved to a neighbouring paper in the same volume through such a cover.

Effect on the 133 files: +29 resolved (29 of 29 checked correct), −1 (the wrong resolution just mentioned).

Not caught: cover types outside the seven phrases the code recognises (six self-describing, one listing other
papers); two covers in a row.

### Crossref strings

Crossref returns HTML: MathML in titles, `&amp;` in journal names, even double escaping (150 of 3,248 records
contained markup or entities).
`clean_text()` unescapes up to three times and removes **only things shaped like tags**. Removing `<[^>]+>` would
delete real text between inequality signs in abstracts (6 records).

## 5. Evidence that appears only inside a citation

Some PDFs begin with the tail of the *previous* article: its reference list fills the header region. Every
text-based rung then lands on a cited paper, and the header check passes because the cited authors are right there.
Seven such errors are known (six through `bib_query`, one through a printed DOI). A random precision sample of 38 did
not find any of them.

Rules keyed to how such a page *begins* were tried first and failed at the next audit: the forms vary too much. The
test was moved next to the evidence instead: a title that `bib_query` or `title_search` is about to accept is
rejected if **every** occurrence of it in the text is immediately followed by a citation tail ("volume, page (year)").

| Measured on | Result |
|---|---|
| The 7 known errors | Stops all 6 from `bib_query`. Does not stop the printed-DOI case |
| All 591 files resolved by the two search rungs at the time | 5 hits, all of which should be stopped (2 of the known errors, 3 slide decks about a paper). 0 false alarms. The other 4 known errors had already been stopped by the earlier rule and were no longer among the 591 |
| The same test applied to the printed-DOI rung (1,343 files at the time) | 19 false alarms: some journals print their own "volume, page (year)" right after the title. So it is not applied there |

Alternatives measured and not adopted: comparing with the year and author in the *original filename* (catches 7 of 7
but new files arrive with meaningless names); requiring two rungs to agree (only 1–2 of 7 caught, and 13 of 40 correct
files have a single working rung); citation density in the header (6 of 7, with false alarms on reference-heavy
first pages).

**Not caught:** citation styles other than "volume, page (year)" (chemistry, engineering and several publisher
styles put the year elsewhere); the printed-DOI variant; near-matches in `title_search` where the accepted title is
not literally in the text.

## 6. Preprints

Decision by the owner: **a preprint is a different paper from its published version.** It is named with `arXiv` as
the journal. The published DOI is stored as a relation (`published_doi`), never as the record's identity.

- **Detection.** The arXiv margin stamp carries ID, version, category and date; citations of arXiv papers carry
  neither version nor date. If a stamp is present the file is an arXiv version and no DOI in its text is followed.
  Among 2,282 files resolved as published versions, the stamp matched 4, and all 4 were preprints (one of which had
  been resolved to a *different* paper by the same authors, via a self-citation). 71 of 79 arXiv records have a
  stamp.
- **Pairs.** If both versions are held, both are resolved, the preprint's `published_doi` equals the other's DOI and
  the first authors agree, `dedup` moves the preprint to a holding folder. Compound surnames are compared by
  containment (arXiv takes the last word as the surname; among 74 preprints compared with the Crossref record of
  their published version, 3 differed only in this way).
- **Finding a published version** (`pubver`). arXiv's DOI field is filled in by authors and is often empty. Crossref
  is searched by title and first author; candidates are written to a sheet and **a person marks each y or n**. Nothing
  is written automatically, because this field decides whether a preprint can be let go.

| Method (83 arXiv records: 63 with a published DOI on arXiv, used as truth, and 19 with neither DOI nor journal reference) | Reproduced of 63 | Wrong | Found among the 19 |
|---|---|---|---|
| Crossref, title and first author | 59 | 1 (a conference abstract of the same work) | 10 |
| OpenAlex, title | 51 | 0 | 4 |
| OpenAlex, lookup of the arXiv DOI | 0 | 0 | 0 |

Of the 10 found on the empty side, 6 were correct, 2 probably correct (retitled), 2 wrong (a book chapter with no
author; a different proceedings paper of the same name). Both error types became conditions.

**Not caught:** arXiv versions without a stamp (8 of 79 at the time of measurement); preprint
servers other than arXiv; large retitlings.

## 7. Looking at the page (`vet.py`)

Text rules kept missing a case a person sees at a glance, because a person looks at the *layout*. Two ways of looking
at an image of page 1 (150 dpi) were compared on the 7 known bad pages and 30 random correctly resolved files:

| Method | Reads the real main title on the 7 bad pages | Title agrees on the 30 good files |
|---|---|---|
| Apple Vision document recognition plus "largest text" heuristic | 6 | 20 |
| Claude Haiku 4.5, one prompt, JSON answer | 7 | 30 |

Every title Haiku returned (37 of 37) occurred literally in the PDF. Its *page-type* judgement was less reliable on a
wider sample (slide decks 0 of 3), so only the title and first author are used.

To keep the central decision intact, the model's role is limited to **pointing at where the main title is printed**:

- The title it reads must occur literally in the PDF's own text; otherwise it is ignored.
- It has a **veto only**. If a resolved record's title disagrees with the title read from the page, the record is
  returned to `unverified` for a person. The model never sets a value and never resolves a file.
- Records entered by hand, and records a person has already cleared, are never vetoed. Japanese titles are not
  judged.
- For unresolved files, the title it read is used as a search hint when candidates are prepared for review.
- Without `--apply` the script only estimates cost with the free token counter. Results are cached so the same page
  is never paid for twice.

Result over the whole collection (3,200 pages): 8 resolved records vetoed, of which 2 were real misidentifications
and 6 were false alarms (5 arXiv records whose title changed between versions; 1 where Crossref lacks the subtitle).
On 50 later additions: 0 vetoed.

**Not caught:** editions sharing a title (the model reads the same title); errors among the roughly 2,350 records it
did not veto (precision there is unmeasured; 30 of 30 only bounds the error rate below about 10% at 95% confidence); run-to-run
variation of the model's answer (unmeasured).

## 8. Stage handling, supplements and duplicates

One definition decides what happens to a record:

| Predicate | True when | Used by |
|---|---|---|
| `renamable` | Rung is a resolved one, and the `source`, year and first author are present | `rename`, `views`, and the three below |
| `needs_human` | Not renamable, and not a supplement, and not entered by hand | `review`, `stats`, `info`, `add` |
| `dedup_key` | Renamable: the DOI, else the arXiv ID | `dedup` |
| `enrichable` | Has a DOI and is renamable, or is a supplement | `enrich` |

These were once written separately, and they drifted: `dedup` did not look at the rung and moved files on the
strength of `unverified` DOIs (29 groups), and resolved records lacking an author appeared in no list at all.

**Supplementary material** repeats the article's title on its first page, so it resolves to the article's DOI.
Because `dedup` keeps the largest file per DOI, in three groups the *article* was about to be moved aside in favour
of its larger supplement. A record whose first 300 characters carry a supplement heading becomes `supplement`: not
renamed, not deduplicated, not sent to a person. 43 records; 0 false positives on inspection. Supplements with only a
title (3 known) are not detected.

**Duplicates** are files sharing a DOI. The largest stays; the rest move to a holding folder. Nothing is deleted.
Keeping the largest is wrong when an author manuscript is a larger file than the published version (2 of 114 groups); a
`dedup-keep.txt` file lets the owner name the file to keep. The same paper under two DOIs is not detected.

## 9. Year

`issued` from Crossref (the earliest known publication date). Only when `issued` is absent, the earliest of the
online date, the print date and `created`. `created` (the DOI registration date) is not used first: for papers
digitised later it is more than three years after `issued` in 99 of 189 records examined.

## 10. The index

One JSON object per line in `papers.jsonl`, written when a file is first scanned and updated by later passes.

| Field | Source | Present |
|---|---|---|
| `path`, `folder`, `filename` | File system | Always |
| `rung`, `source` | Which rung resolved it; `crossref`, `arxiv`, `manual` or null | Always |
| `text` | First 3,000 characters of pages 1–2 (after any cover) | Always except unreadable files |
| `ocr` | Whether OCR was used | Always |
| `doi`, `title`, `year`, `first_author`, `authors`, `journal`, `journal_short`, `volume`, `issue`, `pages`, `article_number` | Crossref or arXiv | Resolved and unverified records |
| `arxiv_id`, `published_doi`, `journal_ref` | arXiv | Preprints |
| `abstract`, `subject` | Crossref, then OpenAlex | Abstract: 2,048 of 3,175 files (65%) |
| `duplicate_of`, `published_copy` | Set by `dedup` on a file it moved aside | When applicable |
| `page_title`, `page_author`, `page_type`, `page_title_literal` | The optional page check | When run |
| `title_guess`, `no_text`, `error`, `fetch_failed` | Diagnostics | When applicable |

- **`text` is kept** so that search needs no re-scan and an agent can grep the library without opening a PDF.
- **No classification of its own.** `subject` holds whatever the registries give, and it is sparse. Folder names are
  the owner's partial grouping. Neither is used to sort files.
- **Writes are atomic** (temporary file, fsync, rename). A rewrite is refused if the file changed since it was read,
  so a concurrent `scan` is not lost; the result is saved beside the index instead.
- **Everything else is derived.** The SQLite search file, the views and the review sheets can be rebuilt.

## 11. Talking to the registries

All network access goes through one function with a shared pace.

- 429 and 5xx are retried with waits of 1, 2, 4, 8 and 16 seconds, honouring `Retry-After`. Other 4xx responses are
  final.
- A 429 slows the whole run, not just the one request. Three consecutive exhausted requests abort the run rather
  than grinding through thousands of failures; everything fetched so far is cached.
- **"Could not fetch" is kept apart from "no such record".** If any lookup for a file ends in a timeout, a retried
  status that never succeeded, or a connection failure, the file is recorded as `error` with the failed lookups
  named, and the next `add` or `scan` retries just those files. Previously both cases returned "nothing", and a single
  slow response left a clearly stamped arXiv PDF as `unresolved` for good. The first version of the fix classified
  HTTP 400 as retryable; re-resolving the whole collection exposed four files that would have been retried forever,
  and the classification was aligned with the retry rule.
- The OpenAlex key is sent as a header to `api.openalex.org` only, never in a URL (cache filenames are derived from
  URLs). An expired key answers 401 or 403, which the rule above treats as final: a known gap (section 15).

**Not caught:** a wrong answer with status 200 (that is the header check's job); a corrupted cache file (it now shows
up as a repeating `error` instead of a silent miss, but must be removed by hand); a failure on the *reading* side
(a stalled network drive can make `pdftotext` time out, which looks like a file with no text).

## 12. Commands that change things

| Command | Design points |
|---|---|
| `add` | Runs scan → enrich → report → dry-run dedup → dry-run rename → index. Never moves a file and refuses `--apply`, because the passes it chains read the same arguments. Its report prints each new record beside the first characters of the PDF's text for a person or agent to compare; it does not judge |
| `rename` | Reads only the index. Journal names over 40 characters are shortened (text before a colon, an acronym in brackets, then initials). Article numbers are used when a journal has no page numbers. Writes a trail before moving; `undo` reverts the last trail. After a move, records that *point at* the moved file are rewritten too |
| `dedup` | See section 8. Writes a trail. No automatic undo |
| `move` | Moves files and the index together, inside the library root only; stops on a name collision |
| `relocate` | After files were moved by hand: matches missing records to new files by name and by a fingerprint of the stored text; ambiguous matches are reported, not applied. `scan` refuses to run if new files look like moved ones, since re-identifying them would orphan hand-made corrections |
| `fix` | One record. Sets `rung` to `manual`, which later automatic passes do not override |
| `review` / `merge` | A sheet of the files that need a person, with a suggested candidate where one is found. A candidate is written only when the person marks it `y` (or `m`: correct, but the file held is a pre-publication manuscript); `n` is remembered and not offered again; `x` marks the file as not a paper, which takes it out of the queue and out of the denominator of the resolution rate. A sheet with unmerged entries is never overwritten |
| `views` | A separate folder of symlinks. On rebuild it deletes only a folder it created and that contains only links |

Measured on the review candidates (title and first author read from the page, searched in Crossref): on 300 resolved
files used as ground truth, 287 candidates, 275 correct; 9 of the 12 wrong were other editions of the same work. On
22 sampled files that needed a person, 18 candidates were correct. Hence the y/n step.

A candidate was found for 146 of the 617 files whose title is readable but which no automatic rung resolved. So
these are in Crossref, and the rungs missed them. Replaying the cached responses shows why: for 108 the first 300
characters of text are not the article's own beginning (the page opens with the tail of another item, a figure
caption, or journal furniture), so `bib_query` asked the wrong question; for 31 the right record was among the top
five but its title is not literally in the extracted text (OCR damage, line breaks); 7 other. Whether such
candidates can be accepted without a person, given further evidence on the page, has not been measured.

## 13. Search

SQLite FTS5 with the trigram tokenizer over title, authors, journal, DOI, abstract, folder and text, ranked by bm25
with column weights (title highest). A query without operators is an OR of its words of three or more characters;
`AND`, `OR`, `NOT`, phrases and column prefixes pass through.

### Embedding (vector) search: built, not the default

**What exists.** `papers index` stores a 512-dimension sentence vector of each record's title (Apple `NLEmbedding`,
English model, no extra dependency; `vec.swift`). `papers search --mode=vec` ranks by cosine similarity (brute force
in pure Python, about 0.06 s for 3,200 records). `--mode=hybrid` merges the full-text and the vector ranking by
reciprocal-rank fusion. The default (`fts`), the skill and the librarian agent do not use vectors.

**Why it is not the default.** Rank of the right paper for 8 queries with a known answer, on a 20-file test set:

```
FTS5 trigram (OR + bm25)        1  4  2  1   1  1   4   1
Sentence embedding of titles    1  1  1  1  17  8  15  13
Reciprocal-rank fusion of both  1  1  1  1   7  1   9   6
```

- The general-purpose model does not know technical vocabulary. It did not connect *neodymium* with *Nd:YAG*, or
  *bond breaking* with *reaction dynamics*. Full-text search ranked the right paper 1st and 4th for those queries.
- Fusion is dragged down by the weaker ranking: worse than full text alone on 3 of 8 (1→7, 4→9, 1→6).
- Only titles are embedded. `NLEmbedding` averages over its whole input instead of truncating it (the vector of a
  full text and that of its first 600 characters had cosine 0.99). Adding the abstract moved every paper towards "a
  physics abstract": ranks fell on all 8 queries, and records without an abstract rose to the top.

**How weak this evidence is.**

- 20 files and 8 queries. It has not been repeated on the full collection (3,175 files), where full-text search
  returns more noise as well, so the outcome may differ.
- One model. A model trained on scientific text (SPECTER2, for example) was not tried. What lost is this model, not
  vector search as such.
- One query on the full collection, as an illustration and not a measurement (n = 1): for a topical phrase of three
  words, the number of top-10 results with all three words in the title was 7 for full text, 3 for vectors and 6 for
  the hybrid. Most of the other vector results shared only the most generic of the three words. The criterion is
  word overlap, which favours full-text search.

**Questions vectors should answer and full-text search cannot.**

- Paraphrase: the query's words occur in neither the title nor the abstract.
- Across languages: a Japanese query for an English paper. The model used here is English-only, so it cannot do
  this either.

**What to measure before changing the default (not done).** About 20 paraphrase queries with a known answer on the
full collection, comparing full text, `NLEmbedding` and a scientific-text model. Such a model needs a package
dependency and a model download, which does not fit "standard library only"; if adopted it would stay optional.

### What is not handled

An ASCII spelling does not find an accented name (`remove_diacritics` would fix this); only the first
3,000 characters of each PDF are indexed (Spotlight covers the rest on macOS).

### Compared with Spotlight

**What Spotlight alone does and does not do.** Measured with `mdfind` restricted to the library, using
`kMDItemTextContent == "*…*"cd` (a substring match on the indexed text):

| Test | Result |
|---|---|
| Look up the DOI of 60 random resolved papers | Own file returned for 37 (alone for 35, with one other file for 2). Nothing for 23: all 18 that were resolved by `bib_query`, 4 of 11 by embedded DOI, 1 of 31 by printed DOI |
| Search 15 first-author surnames (each the first author of 3 to 15 held papers) | 3,294 files returned; 150 have the name among their authors (5%); 68 of the 70 first-author papers were among them |
| Find 37 scanned PDFs (no text layer) by the two longest words in this tool's OCR text | 1 found. Control, 40 PDFs with a text layer and the same method: 34 found |
| Keyword search with 8 topical phrases of two or three words (plain `mdfind`, which requires all words) | 5,353 files returned in total; 355 have the words in the title (7%) and 360 in the abstract (7%). 355 of the 362 title papers are included. With the phrase in quotes: 1,009 files, 158 of the 362 |
| The same 8 phrases with `papers search` | Top 10 (default OR query, bm25 with column weights): 65 of 80 have the words in the title, 13 in the abstract. Top 20: 117 and 30 of 160. With `AND`: 1,888 files, including 361 of the 362 title papers |

For keywords, both tools find the papers whose title carries the words. The difference is what surrounds them:
Spotlight returns every file that mentions the words anywhere in its full text, unranked through `mdfind`, so
on-topic papers are a small share of a long list. `papers search` sees less text (3,000 characters), which removes
most passing mentions, and ranks title matches first. The relevance value Spotlight computes
(`kMDQueryResultContentRelevance`) came back empty through `NSMetadataQuery` for three query forms, so its
on-screen ordering is unmeasured.

So recall for text that exists is good, and Spotlight is the right tool for phrases deep inside a paper. It cannot
answer by identifier when the identifier is not printed, it cannot separate an author from a cited name, and on
this machine it did not index scanned pages. Caveats: the surname test is a substring match, so a short name inside
a longer word also counts; the scan test uses words from this tool's own OCR, some of which may be misread; in the
keyword test "on topic" means the words occur in the title or abstract held in the index, which is a mechanical
proxy (for unidentified files the title read by the optional page check is used; files with neither count as off topic for both tools).

## 14. How the code is checked

The working rules behind this project, in brief:

1. **The same boundary gets the same check.** When a check is added, every path that produces the same kind of output
   is listed mechanically (`boundaries.sh`), and a path left unchecked needs a written reason.
2. **Write down what a safeguard lets through.** Most sections above carry a "not caught" or "not handled" note for
   this reason.
3. **Decide first where errors hide**, and sample there. A resolution *rate* says nothing about precision.
4. **Break it and watch the check fail** before trusting the check (`mutate.py` applies one change to a scratch copy
   and expects a failure). Checks that survived a break were treated as findings and fixed.
5. **Every number carries its n.**
6. **Before adding another rule to a judgement, ask whether to look at different evidence instead.** The page check in
   section 7 came from this.

| Check | What it covers | Runs in CI |
|---|---|---|
| `organize.py selftest` | Pure logic of identification, naming, stage handling, search queries | Yes |
| `retry_check.py` | Retry, pacing, abort and failure classification against a local fake server | Yes |
| `index_check.py` | Atomic writes, conflict detection, dry-run defaults, backups, and each state-changing command in a temporary folder | Yes |
| `vet.py --selftest` | No submission without `--apply`; no double charging | Yes |
| `ladder_check.py` | Real PDFs that were once misidentified (your own list, in `ladder_cases.json`) | No (needs your files) |
| `rescan_check.py` | Re-resolves the whole index with the current code and classifies every difference against a baseline revision | No (needs your files) |

Code comments are in Japanese and refer to sections of the private working record ("DESIGN.md", "HANDOFF.md",
"規約 n" for the rules above). This document covers the same ground in condensed form.

## 15. Known gaps

Collected from the notes above, plus items recorded only in the working notes.

**Identification**

- Printed-DOI contamination from a preceding article's tail (section 5)
- Citation styles other than "volume, page (year)"
- arXiv versions without a stamp; preprint servers other than arXiv
- A printed DOI that is simply wrong for the file (one case: a manuscript carrying a co-author's DOI in boilerplate)
- Crossref subtitles are not appended to titles (8 of 2,154 records have one)
- Non-Latin titles cannot be matched; the page check does not judge them
- LaTeX in arXiv titles
- Supplements that show only a title
- Records with no author in Crossref reach a person but are not rejected by the header check
- Reading failures (timeouts on slow storage) are not retried

**Operations**

- `dedup` chooses by size; choosing the publisher's version is not designed yet. `dedup` has no undo
- `scan` appends to the index; a crash can leave a partial last line
- Cache contents are not validated
- Expiry of an OpenAlex key (401/403) is counted as "no record"
- Newly added files are not page-checked unless `vet.py` is run again
- An interrupted `add`, when re-run, does not repeat its report of new records

**Search**

- Accented names are not found by their ASCII spelling
- Paraphrases and queries in another language than the paper are not answered. Embedding search was tested on 20
  files and 8 queries only, with one general-purpose model (section 13)
- Unresolved files do not appear in the views
- That `search` hides moved-aside duplicates is not covered by an automated check

**Sharing**

- Data lives in the repository folder and paths are absolute; one index cannot be used from two machines
- macOS-only components: Vision OCR, `NLEmbedding`, keychain, Spotlight

## 16. Measurements in one place

The README gives four of these numbers. This is the full set, with sample sizes.

**Outcome of identification** (n = 3,175 files, October 2026):

| Outcome | Files | Share |
|---|---|---|
| Resolved (the files that get renamed) | 2,291 | 72.2% |
| — DOI printed in the text | 1,283 | 40.4% |
| — query from the first 300 characters | 548 | 17.3% |
| — DOI in embedded metadata | 353 | 11.1% |
| — arXiv | 87 | 2.7% |
| — title search | 13 | 0.4% |
| — entered by hand | 7 | 0.2% |
| Unverified (a candidate exists but failed the header check) | 177 | 5.6% |
| Unresolved | 664 | 20.9% |
| Supplementary material (left alone) | 43 | 1.4% |

843 files need a person: the unverified and unresolved ones, plus 2 edge cases. For 617 of them a title is readable
but the automatic queries found no acceptable record. That group is a mixture: work Crossref does not hold (theses,
books, conference abstracts, reports, datasheets, lecture notes), and ordinary papers the query missed (146 of the
617; section 12). Some of the files are not papers at all, so the 72% understates how well real papers are
identified; by how much has not been measured.

**Precision of the resolved records.** Errors hide among the resolved records, so samples were drawn there.

| Check | Sample | Result |
|---|---|---|
| Stratified random sample across rungs, each compared with page 1 by eye | 38 judgeable | 0 wrong. With n = 38 this only bounds the error rate below roughly 10% |
| Before the header check existed | 214 | 5 or 6 wrong (2.3–2.8%) |
| Renamed files compared with page 1 | 20 random | 20 names match the content |
| Page check with Claude Haiku over all resolved records | about 2,350 | 8 flagged: 2 real misidentifications, 6 false alarms |
| Page check on newly added files | 50 | 0 flagged |

Audits found error types the random sample missed; the largest is the page that begins with the tail of the previous
article (section 5; 7 known, 6 stopped by the current rules). Not established: the precision of the records the page
check did not flag, and anything about collections in other fields or languages.

**Other measurements**

| What | Result |
|---|---|
| OCR (Apple Vision) on scanned pages, against the text layer of the same pages | 91.7% word recall for English at 1.3 s per page; 98% character recall for Japanese (one document) |
| Abstract coverage after `enrich` | 2,048 of 3,175 files (65%) |
| Search: rank of the right paper for 8 test queries on a 20-file test set | Full text `1, 4, 2, 1, 1, 1, 4, 1`; title embeddings `1, 1, 1, 1, 17, 8, 15, 13` (section 13) |
| Published version of an arXiv preprint, by title and first author in Crossref | Of 63 preprints whose published DOI is known: 59 matched to it, 1 to a different DOI (section 6) |
| Cost of the page check (Claude Haiku, Batch API) | 3.31 USD for 3,203 files; 0.05 USD for 50 files |

## 17. Alternatives measured and rejected

Each choice was made after measuring the alternative on the same collection.

| Alternative | What was measured | Decision |
|---|---|---|
| Let a model (LLM or layout parser) produce the title, authors and journal | A registry lookup returns the publisher's own record, including the spelling of names. Misspelt authors in existing filenames were corrected by the lookup | Take only keys from the PDF. Never generate a value |
| Use an LLM to propose a title when no DOI is found | The first 300 characters sent to Crossref's bibliographic query, accepting only titles that literally occur in the text, reproduced 17 of the 20 files the LLM step had resolved, with 0 wrong | LLM step removed |
| Accept Crossref's top hit above a relevance score | Scores overlap: a correct hit 59, unrelated text 23, wrong papers 37 to 42 | Never use the score. Compare strings |
| Accept on title similarity alone | A conference abstract, the journal paper and a later book chapter can share one title exactly (similarity 1.00) | Require the header check |
| Make the header check stricter (require volume or page) | Would reject 342 of 2,308 resolved files (14.8%); 14 sampled were nearly all correct | Not adopted |
| Trust the PDF's embedded metadata for values | Typed by hand: a year with a letter O for the zero, author names in inconsistent formats | Use it only to find a DOI |
| On-device LLM OCR | 45.6% recall at 70 s per page; 2 of 4 files returned nothing, silently | Call Apple Vision directly |
| Text rules to detect "page 1 starts with another article" | Three successive rule sets each missed new cases | Look at the page instead (next row) |
| Let a vision model decide the identity | Claude Haiku read the correct main title on 7 of 7 known bad pages and 30 of 30 good ones (Apple Vision layout heuristics: 6 of 7 and 20 of 30). But one title can belong to several editions | Veto only (section 7) |
| Embedding (vector) search by default | On 8 queries over 20 files: better on 2, much worse on 4; the hybrid was worse than full text on 3 (section 13) | Full text is the default; embeddings stay optional. Not re-measured on the full collection |
| Sort files into topic folders automatically | Topics were available for only 56% of files, and the existing folders carry the owner's own grouping | Leave files in place; offer symlink views |
| Delete duplicates | — | Never. Duplicates are moved aside; deleting is the owner's decision |
