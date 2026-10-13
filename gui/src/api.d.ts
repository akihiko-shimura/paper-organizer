// gui.py の API の表から作る(python3 gui.py --types > gui/src/api.d.ts)。手で直さない。
export interface Api {
  "/api/info": { req: { db?: string; }; res: { records: number; index_updated: string; columns: Array<string>; can_open: boolean; stale: boolean; db: string; name: string; dbs: Array<string>; kinds: Array<string>; } };
  "/api/library": { req: { db?: string; }; res: { lines: Array<string>; } };
  "/api/search": { req: { q: string; all: boolean; year_from?: number; year_to?: number; journal?: string; state?: string; kind?: string; sort?: string; offset?: number; db?: string; }; res: { total: number; near?: number; substring: boolean; short: Array<string>; unused: Array<string>; facets: { years: Array<{ v: number; n: number; }>; journals: Array<{ v: string; n: number; }>; states: Array<{ v: string; n: number; }>; kinds: Array<{ v: string; n: number; }>; }; rows: Array<{ path: string; title?: string; guess?: string; filename: string; journal?: string; year?: number; volume?: string; pages?: string; state: string; kind: string; first_author?: string; n_authors: number; only_full: boolean; }>; } };
  "/api/record": { req: { q: string; all: boolean; path: string; db?: string; }; res: { path: string; title?: string; guess?: string; filename: string; journal?: string; year?: number; volume?: string; pages?: string; state: string; kind: string; where: string; authors: Array<{ name: string; q: string; }>; issue?: string; doi?: string; arxiv_id?: string; published_doi?: string; abstract?: string; links: Array<string>; cite?: string; cite_unsure: boolean; hit_total: number; hits_note?: string; edit: { title?: string; authors?: string; first_author?: string; year?: string; journal?: string; }; kind_key: string; hits: Array<{ page: number; parts: Array<{ hit: boolean; text: string; }>; }>; } };
  "/api/page": { req: { path: string; page: number; db?: string; }; res: Blob };
  "/api/cite": { req: { paths: Array<string>; db?: string; }; res: { text: string; made: number; skipped: number; } };
  "/api/fix": { req: { path: string; set: { title?: string; authors?: string; first_author?: string; year?: string; journal?: string; }; apply: boolean; lookup?: string; db?: string; }; res: { lines: Array<string>; applied: boolean; } };
  "/api/tidy": { req: { path: string; kind?: string; to?: string; apply: boolean; db?: string; }; res: { lines: Array<string>; applied: boolean; } };
  "/api/open": { req: { path: string; db?: string; }; res: { ok: boolean; } };
  "/api/reveal": { req: { path: string; db?: string; }; res: { ok: boolean; } };
  "/api/open-url": { req: { path: string; link: string; db?: string; }; res: { ok: boolean; } };
}
