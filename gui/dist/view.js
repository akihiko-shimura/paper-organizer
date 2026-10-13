import { t } from "./strings.js";
// 画面から直せる項目。index.html の欄(edit-f-<名前>)と、gui.py の EDIT_KEYS に合わせる
export const EDIT_KEYS = ["title", "authors", "first_author", "year", "journal"];
function el(tag, cls = "", text = "") {
    const e = document.createElement(tag);
    if (cls)
        e.className = cls;
    if (text)
        e.textContent = text;
    return e;
}
function button(label, key, run, cls = "") {
    const b = el("button", cls, label);
    if (key)
        b.append(el("kbd", "", key));
    b.addEventListener("click", run);
    return b;
}
const byId = (id) => document.getElementById(id);
function reference(r, issue = "") {
    const vol = (r.volume ?? "") + (issue ? `(${issue})` : "");
    return [[r.journal, vol].filter(Boolean).join(" "), r.pages].filter(Boolean).join(", ") + (r.year ? ` (${r.year})` : "");
}
function badges(r) {
    const state = r.state === "resolved" ? undefined : t.states[r.state];
    const kind = r.kind === "paper" ? undefined : t.kinds[r.kind];
    return [state, kind].filter((s) => !!s).map((s) => el("span", "badge", s));
}
function heading(r, tag) {
    const h = el(tag, "title", r.title ?? r.guess ?? r.filename);
    if (!r.title && r.guess)
        h.append(el("span", "note", " " + t.guessed));
    return h;
}
// 選んでいる行の在りか(どの DB の、どの記録か)
export function current(s) {
    const pane = s.panes[s.at];
    const path = pane?.res?.rows[s.sel]?.path;
    return pane && path ? { db: pane.db, path } : undefined;
}
function row(r, pi, i, pane, s, on, divided) {
    const picked = s.picked.some((p) => p.db === pane.db && p.path === r.path);
    const d = el("div", ["row", pi === s.at && i === s.sel ? "cur" : "", picked ? "picked" : ""].filter(Boolean).join(" "));
    const who = (r.first_author ?? "") + (r.n_authors > 1 ? t.others(r.n_authors - 1) : "");
    const meta = el("div", "meta", [who, reference(r)].filter(Boolean).join(" · "));
    meta.append(...badges(r));
    if (r.only_full && !divided)
        meta.append(el("span", "badge", t.fullOnlyBadge));
    d.append(heading(r, "div"), meta);
    d.addEventListener("click", () => on.select(pi, i));
    return d;
}
function rows(pane, pi, s, on) {
    const out = pane.error ? [el("p", "note pane-error", pane.error)] : [];
    const res = pane.res;
    if (!res)
        return out;
    const divided = s.sort === "relevance"; // 関連度の並びでは、全頁の本文だけで当たる記録が後ろに固まる
    res.rows.forEach((r, i) => {
        if (divided && r.only_full && !res.rows[i - 1]?.only_full)
            out.push(el("div", "divider", t.fullOnly));
        out.push(row(r, pi, i, pane, s, on, divided));
    });
    if (res.rows.length < res.total)
        out.push(button(t.more, "", () => on.more(pi)));
    return out;
}
function count(res) {
    if (!res || !res.total)
        return res ? t.count(0) : "";
    if (res.near === undefined)
        return t.count(res.total) + (res.substring ? " " + t.substring : "");
    return t.count(res.near) + (res.total > res.near ? t.countFull(res.total - res.near) : "");
}
// 一覧の枠(DB ごとに 1 つ)は、出している DB の数に合わせて足し引きするだけで、作り直さない(作り直すと、見ている位置が先頭に戻る)。
// 枠の中身は毎回ぜんぶ描き直す
function lists(s, on) {
    const host = byId("list");
    while (host.children.length > s.panes.length)
        host.lastElementChild?.remove();
    while (host.children.length < s.panes.length) {
        const pane = el("section", "pane");
        pane.append(el("div", "pane-head"), el("div", "rows"));
        host.append(pane);
    }
    s.panes.forEach((pane, pi) => {
        const [head, body] = [host.children[pi]?.children[0], host.children[pi]?.children[1]];
        if (!(head instanceof HTMLElement) || !(body instanceof HTMLElement))
            return;
        const name = s.dbs.find((d) => d.id === pane.db)?.name ?? pane.db;
        head.className = pi === s.at ? "pane-head on" : "pane-head";
        head.textContent = s.dbs.length > 1 ? [name, count(pane.res)].filter(Boolean).join(" · ") : ""; // DB が 1 つなら見出しは要らない
        head.onclick = () => on.select(pi, pi === s.at ? s.sel : 0);
        body.replaceChildren(...rows(pane, pi, s, on));
    });
    host.querySelector(".cur")?.scrollIntoView({ block: "nearest" });
}
// 出す DB の切り替え(DB が 2 つ以上あるときだけ)
function dbChips(s, on) {
    if (s.dbs.length < 2)
        return [];
    const shown = s.panes.map((p) => p.db);
    return s.dbs.map((d) => button(d.name, "", () => on.toggleDb(d.id), shown.includes(d.id) ? "chip on" : "chip"));
}
function facets(s, on) {
    const f = s.panes[s.at]?.res?.facets;
    if (!f)
        return [];
    const group = (key, items, names) => items.map(({ v, n }) => {
        const patch = {};
        patch[key] = s.filters[key] === v ? undefined : v;
        return button(`${names[v] ?? v} ${n}`, "", () => on.filter(patch), s.filters[key] === v ? "chip on" : "chip");
    });
    return [...group("kind", f.kinds, t.kinds), ...group("state", f.states, t.states), ...group("journal", f.journals, {})];
}
function detail(s, on) {
    const r = s.rec;
    const cur = current(s);
    if (!r || !cur || r.path !== cur.path || r.db !== cur.db)
        return [];
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
    if (s.canOpen)
        actions.append(button(t.openPdf, "o", on.open), button(t.reveal, "f", on.reveal));
    r.links.forEach((k, i) => actions.append(button(t.link[k] ?? k, i ? "" : "d", () => on.link(k))));
    actions.append(button(t.edit, "e", on.edit), button(t.tidy, "m", on.tidy));
    const out = [heading(r, "h2"), authors, ref, el("p", "path", `${r.where}/${r.filename}`), actions];
    if (r.abstract)
        out.push(el("h3", "", t.abstract), el("p", "", r.abstract));
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
    if (note)
        out.push(el("p", "note", note));
    if (r.cite) {
        const head = el("h3", "", t.cite + " ");
        head.append(button(s.copied ? t.copied : t.copy, "c", on.copy));
        out.push(head, el("pre", "", r.cite));
        if (r.cite_unsure)
            out.push(el("p", "note", t.citeUnsure));
    }
    return out;
}
function pages(p, on) {
    const out = [];
    p.urls.forEach((url, i) => {
        const img = el("img");
        img.src = url;
        img.alt = t.page(p.first + i);
        out.push(el("div", "page", t.page(p.first + i)), img);
    });
    if (!p.urls.length)
        out.push(el("p", "note", p.more ? t.loading : t.noPreview));
    if (p.more && p.urls.length)
        out.push(button(t.morePages, "", on.morePages));
    return out;
}
function picks(s, on) {
    if (!s.picked.length)
        return [];
    return [
        el("span", "", t.picked(s.picked.length)),
        button(s.pickedCopied ? t.copied : t.copyPicked, "C", on.copyPicked),
        button(t.clearPicked, "", on.clearPicked),
    ];
}
let shown; // 今 #preview に出ている頁。同じなら描き直さない(絵を読み直すと、見ている位置が動く)
export function render(s, on) {
    const res = s.panes[s.at]?.res; // 件数・注意・絞り込みの候補は、選んでいる行のある一覧のもの
    byId("count").textContent = s.dbs.length > 1 ? "" : count(res); // DB が 2 つ以上なら、件数は一覧ごとの見出しに出す
    const old = s.dbs.filter((d) => d.stale && s.panes.some((p) => p.db === d.id)).map((d) => d.name);
    const notes = [s.message, old.length ? t.stale + (s.dbs.length > 1 ? ` (${old.join(", ")})` : "") : ""];
    if (res?.short.length)
        notes.push(t.short(res.short.join(", ")));
    if (res?.unused.length)
        notes.push(t.unused(res.unused.join(", ")));
    byId("notes").textContent = notes.filter(Boolean).join(" / ");
    byId("picks").replaceChildren(...picks(s, on));
    byId("dbs").replaceChildren(...dbChips(s, on));
    byId("facets").replaceChildren(...facets(s, on));
    const years = res?.facets.years ?? [];
    byId("year-from").placeholder = String(years[0]?.v ?? "");
    byId("year-to").placeholder = String(years[years.length - 1]?.v ?? "");
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
    if (!s.preview)
        byId("detail").replaceChildren(...detail(s, on));
}
// 検索欄の中身を入れ替える(著者を押したとき)
export function setQuery(q) {
    byId("q").value = q;
}
// 直す欄(index.html に固定で置いた dialog)
const dialog = () => byId("edit");
const field = (k) => byId("edit-f-" + k);
export function openEdit(r) {
    for (const k of EDIT_KEYS)
        field(k).value = r.edit[k] ?? "";
    byId("edit-doi").value = ""; // 空なら引かない。今の DOI は薄い字で見せるだけ
    byId("edit-doi").placeholder = r.doi ?? "10.1234/abcd · 978-…";
    byId("edit-title").textContent = r.filename;
    byId("edit-out").textContent = "";
    dialog().showModal();
}
export function editValues() {
    const out = {};
    for (const k of EDIT_KEYS)
        out[k] = field(k).value;
    return out;
}
// 直す欄に打たれた DOI か ISBN(空なら引かない。どちらかはサーバが見分ける)
export const editDoi = () => byId("edit-doi").value.trim();
export function editResult(text) {
    byId("edit-out").textContent = text;
}
export function closeEdit() {
    dialog().close();
}
// 片付ける欄(種類と行き先)。選ぶ欄は index.html に固定で置いてあり、開くときに選択肢だけを入れ替える
function options(id, items, chosen) {
    const box = byId(id);
    box.replaceChildren(...items.map(([value, label]) => {
        const o = el("option", "", label);
        o.value = value;
        return o;
    }));
    box.value = chosen;
}
export function openTidy(r, kinds, others) {
    options("tidy-kind", kinds.map((k) => [k, t.kindNames[k] ?? k]), r.kind_key);
    options("tidy-to", [["", t.tidyStay], ["remove", t.tidyRemove], ...others.map((d) => [`db:${d.id}`, t.tidySend(d.name)])], "");
    byId("tidy-title").textContent = r.filename;
    byId("tidy-out").textContent = "";
    byId("tidy").showModal();
}
export const tidyValues = () => ({
    kind: byId("tidy-kind").value,
    to: byId("tidy-to").value,
});
export function tidyResult(text) {
    byId("tidy-out").textContent = text;
}
export function closeTidy() {
    byId("tidy").close();
}
// 読み込んでいる DB の情報(papers info と同じ行)
export function openLibrary(lines) {
    byId("lib-out").textContent = lines.join("\n");
    byId("lib").showModal();
}
export function closeLibrary() {
    byId("lib").close();
}
// 開いている dialog があるあいだ、画面のキー操作は効かせない
export const dialogOpen = () => !!document.querySelector("dialog[open]");
// index.html に固定で置いた部品の文言。1 回だけ入れる
export function labels() {
    byId("q").placeholder = t.placeholder;
    byId("all-label").textContent = t.allTerms;
    byId("year-label").textContent = t.year;
    byId("sort-label").textContent = t.sort;
    for (const o of byId("sort").options)
        o.textContent = t.sorts[o.value] ?? o.value;
    for (const k of EDIT_KEYS)
        byId("edit-l-" + k).textContent = t.editFields[k] ?? k;
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
