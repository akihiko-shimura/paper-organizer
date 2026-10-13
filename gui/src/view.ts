// 描く所。DOM に書くのはこのファイルだけ。状態(State)から毎回ぜんぶ描き直し、一部だけを直す処理を持たない。
import type { Api } from "./api.js";
import { t } from "./strings.js";

export type SearchRes = Api["/api/search"]["res"];
export type Rec = Api["/api/record"]["res"];
export type Filters = Pick<Api["/api/search"]["req"], "year_from" | "year_to" | "journal" | "state" | "kind">;
type Row = SearchRes["rows"][number];

// 画面から直せる項目。index.html の欄(edit-f-<名前>)と、gui.py の EDIT_KEYS に合わせる
export const EDIT_KEYS = ["title", "authors", "first_author", "year", "journal"] as const satisfies ReadonlyArray<keyof Rec["edit"]>;

// 読み込んである DB。stale は、検索の索引が索引より古い
export interface Db {
  readonly id: string;
  readonly name: string;
  readonly stale: boolean;
}

// 一覧 1 つ(DB 1 つぶん)。error は、その DB を引けなかったときの文面(前の結果は残す)
export interface Pane {
  readonly db: string;
  readonly res: SearchRes | undefined;
  readonly error: string;
}

// 記録の在りか(どの DB の、どのパスか)
export interface Where {
  readonly db: string;
  readonly path: string;
}

export interface Preview extends Where {
  readonly first: number; // 最初に出す頁
  readonly urls: readonly string[]; // 頁の絵(first から順)
  readonly more: boolean; // まだ続きがあるかもしれない
}

export interface State {
  readonly q: string;
  readonly all: boolean;
  readonly filters: Filters;
  readonly sort: string;
  readonly dbs: readonly Db[];
  readonly panes: readonly Pane[]; // 出している一覧(上から順)
  readonly at: number; // 選んでいる行のある一覧
  readonly sel: number; // その一覧の中の行
  readonly rec: (Rec & { readonly db: string }) | undefined;
  readonly message: string;
  readonly canOpen: boolean;
  readonly copied: boolean;
  readonly preview: Preview | undefined;
  readonly picked: readonly Where[];
  readonly pickedCite: string;
  readonly pickedCopied: boolean;
}

export interface Actions {
  select(pane: number, row: number): void;
  more(pane: number): void;
  toggleDb(id: string): void;
  author(q: string): void;
  filter(patch: Filters): void;
  open(): void;
  reveal(): void;
  link(kind: string): void;
  copy(): void;
  preview(page?: number): void;
  morePages(): void;
  pick(): void;
  copyPicked(): void;
  clearPicked(): void;
  edit(): void;
  tidy(): void;
}

// 打つ欄は index.html に固定で置く。描き直しの中で作ると、打っている最中に消える。型で作れなくしてある
type Tag = Exclude<keyof HTMLElementTagNameMap, "input" | "textarea" | "select">;

function el<K extends Tag>(tag: K, cls = "", text = ""): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

function button(label: string, key: string, run: () => void, cls = ""): HTMLButtonElement {
  const b = el("button", cls, label);
  if (key) b.append(el("kbd", "", key));
  b.addEventListener("click", run);
  return b;
}

const byId = <T extends HTMLElement = HTMLElement>(id: string): T => document.getElementById(id) as T;

function reference(r: Row | Rec, issue = ""): string {
  const vol = (r.volume ?? "") + (issue ? `(${issue})` : "");
  return [[r.journal, vol].filter(Boolean).join(" "), r.pages].filter(Boolean).join(", ") + (r.year ? ` (${r.year})` : "");
}

function badges(r: Row | Rec): HTMLElement[] {
  const state = r.state === "resolved" ? undefined : t.states[r.state];
  const kind = r.kind === "paper" ? undefined : t.kinds[r.kind];
  return [state, kind].filter((s): s is string => !!s).map((s) => el("span", "badge", s));
}

function heading(r: Row | Rec, tag: "div" | "h2"): HTMLElement {
  const h = el(tag, "title", r.title ?? r.guess ?? r.filename);
  if (!r.title && r.guess) h.append(el("span", "note", " " + t.guessed));
  return h;
}

// 選んでいる行の在りか(どの DB の、どの記録か)
export function current(s: State): Where | undefined {
  const pane = s.panes[s.at];
  const path = pane?.res?.rows[s.sel]?.path;
  return pane && path ? { db: pane.db, path } : undefined;
}

function row(r: Row, pi: number, i: number, pane: Pane, s: State, on: Actions, divided: boolean): HTMLElement {
  const picked = s.picked.some((p) => p.db === pane.db && p.path === r.path);
  const d = el("div", ["row", pi === s.at && i === s.sel ? "cur" : "", picked ? "picked" : ""].filter(Boolean).join(" "));
  const who = (r.first_author ?? "") + (r.n_authors > 1 ? t.others(r.n_authors - 1) : "");
  const meta = el("div", "meta", [who, reference(r)].filter(Boolean).join(" · "));
  meta.append(...badges(r));
  if (r.only_full && !divided) meta.append(el("span", "badge", t.fullOnlyBadge));
  d.append(heading(r, "div"), meta);
  d.addEventListener("click", () => on.select(pi, i));
  return d;
}

function rows(pane: Pane, pi: number, s: State, on: Actions): HTMLElement[] {
  const out: HTMLElement[] = pane.error ? [el("p", "note pane-error", pane.error)] : [];
  const res = pane.res;
  if (!res) return out;
  const divided = s.sort === "relevance"; // 関連度の並びでは、全頁の本文だけで当たる記録が後ろに固まる
  res.rows.forEach((r, i) => {
    if (divided && r.only_full && !res.rows[i - 1]?.only_full) out.push(el("div", "divider", t.fullOnly));
    out.push(row(r, pi, i, pane, s, on, divided));
  });
  if (res.rows.length < res.total) out.push(button(t.more, "", () => on.more(pi)));
  return out;
}

function count(res: SearchRes | undefined): string {
  if (!res || !res.total) return res ? t.count(0) : "";
  if (res.near === undefined) return t.count(res.total) + (res.substring ? " " + t.substring : "");
  return t.count(res.near) + (res.total > res.near ? t.countFull(res.total - res.near) : "");
}

// 一覧の枠(DB ごとに 1 つ)は、出している DB の数に合わせて足し引きするだけで、作り直さない(作り直すと、見ている位置が先頭に戻る)。
// 枠の中身は毎回ぜんぶ描き直す
function lists(s: State, on: Actions): void {
  const host = byId("list");
  while (host.children.length > s.panes.length) host.lastElementChild?.remove();
  while (host.children.length < s.panes.length) {
    const pane = el("section", "pane");
    pane.append(el("div", "pane-head"), el("div", "rows"));
    host.append(pane);
  }
  s.panes.forEach((pane, pi) => {
    const [head, body] = [host.children[pi]?.children[0], host.children[pi]?.children[1]];
    if (!(head instanceof HTMLElement) || !(body instanceof HTMLElement)) return;
    const name = s.dbs.find((d) => d.id === pane.db)?.name ?? pane.db;
    head.className = pi === s.at ? "pane-head on" : "pane-head";
    head.textContent = s.dbs.length > 1 ? [name, count(pane.res)].filter(Boolean).join(" · ") : ""; // DB が 1 つなら見出しは要らない
    head.onclick = () => on.select(pi, pi === s.at ? s.sel : 0);
    body.replaceChildren(...rows(pane, pi, s, on));
  });
  host.querySelector(".cur")?.scrollIntoView({ block: "nearest" });
}

// 出す DB の切り替え(DB が 2 つ以上あるときだけ)
function dbChips(s: State, on: Actions): HTMLElement[] {
  if (s.dbs.length < 2) return [];
  const shown = s.panes.map((p) => p.db);
  return s.dbs.map((d) => button(d.name, "", () => on.toggleDb(d.id), shown.includes(d.id) ? "chip on" : "chip"));
}

function facets(s: State, on: Actions): HTMLElement[] {
  const f = s.panes[s.at]?.res?.facets;
  if (!f) return [];
  const group = (key: "kind" | "state" | "journal", items: Array<{ v: string; n: number }>, names: Record<string, string>) =>
    items.map(({ v, n }) => {
      const patch: Filters = {};
      patch[key] = s.filters[key] === v ? undefined : v;
      return button(`${names[v] ?? v} ${n}`, "", () => on.filter(patch), s.filters[key] === v ? "chip on" : "chip");
    });
  return [...group("kind", f.kinds, t.kinds), ...group("state", f.states, t.states), ...group("journal", f.journals, {})];
}

function detail(s: State, on: Actions): HTMLElement[] {
  const r = s.rec;
  const cur = current(s);
  if (!r || !cur || r.path !== cur.path || r.db !== cur.db) return [];
  const authors = el("p", "authors");
  r.authors.forEach((a, i) => {
    const link = el("span", "author", a.name);
    link.addEventListener("click", () => on.author(a.q));
    authors.append(i ? "; " : "", link);
  });
  const ref = el("p", "meta", reference(r, r.issue));
  ref.append(...badges(r));
  const actions = el("div", "actions");
  actions.append(button(t.preview, "space", () => on.preview()));
  if (s.canOpen) actions.append(button(t.openPdf, "o", on.open), button(t.reveal, "f", on.reveal));
  r.links.forEach((k, i) => actions.append(button(t.link[k] ?? k, i ? "" : "d", () => on.link(k))));
  actions.append(button(t.edit, "e", on.edit), button(t.tidy, "m", on.tidy));
  const out = [heading(r, "h2"), authors, ref, el("p", "path", `${r.where}/${r.filename}`), actions];
  if (r.abstract) out.push(el("h3", "", t.abstract), el("p", "", r.abstract));
  if (r.hits.length) {
    out.push(el("h3", "", t.hits(r.hit_total, r.hits.length)));
    for (const h of r.hits) {
      const p = el("p", "hit");
      const page = el("span", "page", t.page(h.page));
      page.addEventListener("click", () => on.preview(h.page));
      p.append(page, ...h.parts.map((x) => (x.hit ? el("mark", "", x.text) : x.text)));
      out.push(p);
    }
  }
  const note = r.hits_note ? t.hitsNote[r.hits_note] : undefined;
  if (note) out.push(el("p", "note", note));
  if (r.cite) {
    const head = el("h3", "", t.cite + " ");
    head.append(button(s.copied ? t.copied : t.copy, "c", on.copy));
    out.push(head, el("pre", "", r.cite));
    if (r.cite_unsure) out.push(el("p", "note", t.citeUnsure));
  }
  return out;
}

function pages(p: Preview, on: Actions): HTMLElement[] {
  const out: HTMLElement[] = [];
  p.urls.forEach((url, i) => {
    const img = el("img");
    img.src = url;
    img.alt = t.page(p.first + i);
    out.push(el("div", "page", t.page(p.first + i)), img);
  });
  if (!p.urls.length) out.push(el("p", "note", p.more ? t.loading : t.noPreview));
  if (p.more && p.urls.length) out.push(button(t.morePages, "", on.morePages));
  return out;
}

function picks(s: State, on: Actions): HTMLElement[] {
  if (!s.picked.length) return [];
  return [
    el("span", "", t.picked(s.picked.length)),
    button(s.pickedCopied ? t.copied : t.copyPicked, "C", on.copyPicked),
    button(t.clearPicked, "", on.clearPicked),
  ];
}

let shown: Preview | undefined; // 今 #preview に出ている頁。同じなら描き直さない(絵を読み直すと、見ている位置が動く)

export function render(s: State, on: Actions): void {
  const res = s.panes[s.at]?.res; // 件数・注意・絞り込みの候補は、選んでいる行のある一覧のもの
  byId("count").textContent = s.dbs.length > 1 ? "" : count(res); // DB が 2 つ以上なら、件数は一覧ごとの見出しに出す
  const old = s.dbs.filter((d) => d.stale && s.panes.some((p) => p.db === d.id)).map((d) => d.name);
  const notes = [s.message, old.length ? t.stale + (s.dbs.length > 1 ? ` (${old.join(", ")})` : "") : ""];
  if (res?.short.length) notes.push(t.short(res.short.join(", ")));
  if (res?.unused.length) notes.push(t.unused(res.unused.join(", ")));
  byId("notes").textContent = notes.filter(Boolean).join(" / ");
  byId("picks").replaceChildren(...picks(s, on));
  byId("dbs").replaceChildren(...dbChips(s, on));
  byId("facets").replaceChildren(...facets(s, on));
  const years = res?.facets.years ?? [];
  byId<HTMLInputElement>("year-from").placeholder = String(years[0]?.v ?? "");
  byId<HTMLInputElement>("year-to").placeholder = String(years[years.length - 1]?.v ?? "");
  lists(s, on);
  byId("detail").hidden = !!s.preview;
  byId("preview").hidden = !s.preview;
  if (s.preview !== shown) {
    const pane = byId("preview");
    const same = shown?.db === s.preview?.db && shown?.path === s.preview?.path && shown?.first === s.preview?.first;
    const top = same ? pane.scrollTop : 0;
    pane.replaceChildren(...(s.preview ? pages(s.preview, on) : []));
    pane.scrollTop = top;
    shown = s.preview;
  }
  if (!s.preview) byId("detail").replaceChildren(...detail(s, on));
}

// 検索欄の中身を入れ替える(著者を押したとき)
export function setQuery(q: string): void {
  byId<HTMLInputElement>("q").value = q;
}

// 直す欄(index.html に固定で置いた dialog)
const dialog = (): HTMLDialogElement => byId<HTMLDialogElement>("edit");
const field = (k: string): HTMLInputElement => byId<HTMLInputElement>("edit-f-" + k);

export function openEdit(r: Rec): void {
  for (const k of EDIT_KEYS) field(k).value = r.edit[k] ?? "";
  byId<HTMLInputElement>("edit-doi").value = ""; // 空なら引かない。今の DOI は薄い字で見せるだけ
  byId<HTMLInputElement>("edit-doi").placeholder = r.doi ?? "10.1234/abcd · 978-…";
  byId("edit-title").textContent = r.filename;
  byId("edit-out").textContent = "";
  dialog().showModal();
}

export function editValues(): Rec["edit"] {
  const out: Rec["edit"] = {};
  for (const k of EDIT_KEYS) out[k] = field(k).value;
  return out;
}

// 直す欄に打たれた DOI か ISBN(空なら引かない。どちらかはサーバが見分ける)
export const editDoi = (): string => byId<HTMLInputElement>("edit-doi").value.trim();

export function editResult(text: string): void {
  byId("edit-out").textContent = text;
}

export function closeEdit(): void {
  dialog().close();
}

// 片付ける欄(種類と行き先)。選ぶ欄は index.html に固定で置いてあり、開くときに選択肢だけを入れ替える
function options(id: string, items: ReadonlyArray<readonly [string, string]>, chosen: string): void {
  const box = byId<HTMLSelectElement>(id);
  box.replaceChildren(
    ...items.map(([value, label]) => {
      const o = el("option", "", label);
      o.value = value;
      return o;
    }),
  );
  box.value = chosen;
}

export function openTidy(r: Rec, kinds: readonly string[], others: readonly Db[]): void {
  options("tidy-kind", kinds.map((k) => [k, t.kindNames[k] ?? k] as const), r.kind_key);
  options("tidy-to", [["", t.tidyStay], ["remove", t.tidyRemove], ...others.map((d) => [`db:${d.id}`, t.tidySend(d.name)] as const)], "");
  byId("tidy-title").textContent = r.filename;
  byId("tidy-out").textContent = "";
  byId<HTMLDialogElement>("tidy").showModal();
}

export const tidyValues = (): { kind: string; to: string } => ({
  kind: byId<HTMLSelectElement>("tidy-kind").value,
  to: byId<HTMLSelectElement>("tidy-to").value,
});

export function tidyResult(text: string): void {
  byId("tidy-out").textContent = text;
}

export function closeTidy(): void {
  byId<HTMLDialogElement>("tidy").close();
}

// 読み込んでいる DB の情報(papers info と同じ行)
export function openLibrary(lines: readonly string[]): void {
  byId("lib-out").textContent = lines.join("\n");
  byId<HTMLDialogElement>("lib").showModal();
}

export function closeLibrary(): void {
  byId<HTMLDialogElement>("lib").close();
}

// 開いている dialog があるあいだ、画面のキー操作は効かせない
export const dialogOpen = (): boolean => !!document.querySelector("dialog[open]");

// index.html に固定で置いた部品の文言。1 回だけ入れる
export function labels(): void {
  byId<HTMLInputElement>("q").placeholder = t.placeholder;
  byId("all-label").textContent = t.allTerms;
  byId("year-label").textContent = t.year;
  byId("sort-label").textContent = t.sort;
  for (const o of byId<HTMLSelectElement>("sort").options) o.textContent = t.sorts[o.value] ?? o.value;
  for (const k of EDIT_KEYS) byId("edit-l-" + k).textContent = t.editFields[k] ?? k;
  byId("edit-check").textContent = t.editCheck;
  byId("edit-write").textContent = t.editWrite;
  byId("edit-cancel").textContent = t.editCancel;
  byId("edit-hint").textContent = t.editHint;
  byId("edit-l-doi").textContent = t.editDoi;
  byId("tidy-l-kind").textContent = t.tidyKind;
  byId("tidy-l-to").textContent = t.tidyTo;
  byId("tidy-hint").textContent = t.tidyHint;
  byId("tidy-check").textContent = t.editCheck;
  byId("tidy-write").textContent = t.tidyWrite;
  byId("tidy-cancel").textContent = t.editCancel;
  byId("lib-open").textContent = t.library;
  byId("lib-title").textContent = t.libraryTitle;
  byId("lib-close").textContent = t.close;
  byId("keys").textContent = t.keys;
}
