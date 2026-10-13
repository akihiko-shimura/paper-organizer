import { closeEdit, closeLibrary, closeTidy, current, dialogOpen, editDoi, editResult, editValues, labels, openEdit, openLibrary, openTidy, render, setQuery, tidyResult, tidyValues, } from "./view.js";
import { t } from "./strings.js";
const byId = (id) => document.getElementById(id);
const input = byId("q");
const allBox = byId("all");
const yearFrom = byId("year-from");
const yearTo = byId("year-to");
const sortBox = byId("sort");
// 合言葉は URL の # の後ろで受け取り、タブの中に移して URL から消す
const given = /^#t=(.+)$/.exec(location.hash)?.[1];
if (given) {
    sessionStorage.setItem("token", given);
    history.replaceState(null, "", location.pathname);
}
const token = sessionStorage.getItem("token") ?? "";
const send = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Papers-Token": token }, body: JSON.stringify(body) });
const text = (e) => (e instanceof Error ? e.message : String(e));
async function refused(r) {
    const data = (await r.json().catch(() => ({})));
    return new Error(data.error ?? String(r.status));
}
// どの要求も、相手の DB を db で選ぶ(省くと最初の DB)
async function post(path, body) {
    const r = await send(path, body);
    if (!r.ok)
        throw await refused(r);
    return (await r.json());
}
// 頁の絵の URL。その頁が無ければ undefined
async function pageUrl(body) {
    const r = await send("/api/page", body);
    if (r.status === 404)
        return undefined;
    if (!r.ok)
        throw await refused(r);
    return URL.createObjectURL(await r.blob());
}
let state = {
    q: "", all: true, filters: {}, sort: "relevance", dbs: [], panes: [], at: 0, sel: 0, rec: undefined, message: "",
    canOpen: false, copied: false, preview: undefined, picked: [], pickedCite: "", pickedCopied: false,
};
function update(patch) {
    state = { ...state, ...patch };
    render(state, actions);
}
const fail = (e) => update({ message: text(e) });
const year = (s) => (/^\d+$/.test(s.trim()) ? Number(s) : undefined);
const same = (a, b) => a.db === b.db && a.path === b.path;
const rowsIn = (pane) => state.panes[pane]?.res?.rows.length ?? 0;
// 遅れて着いた古い応答で、新しい結果を上書きしない
let searches = 0;
let loads = 0;
let previews = 0;
let cites = 0;
async function load() {
    const cur = current(state);
    const mine = ++loads;
    if (!cur)
        return;
    try {
        const rec = await post("/api/record", { ...cur, q: state.q, all: state.all });
        if (mine === loads)
            update({ rec: { ...rec, db: cur.db }, copied: false });
    }
    catch (e) {
        if (mine === loads)
            fail(e);
    }
}
// 選んでいる行が変わった後に呼ぶ。頁を見ている最中なら、新しい行の頁に替える
function selected() {
    void load();
    if (state.preview)
        void openPreview(1);
}
// 出している DB を、同じ条件で引く。keep は、引き直した後も選んだままにしたい記録(直した直後)
async function search(keep) {
    const mine = ++searches;
    const q = input.value;
    const all = allBox.checked;
    const sort = sortBox.value;
    const filters = { ...state.filters, year_from: year(yearFrom.value), year_to: year(yearTo.value) };
    const got = await Promise.all(state.panes.map((p) => post("/api/search", { db: p.db, q, all, sort, ...filters }).then((res) => ({ db: p.db, res, error: "" }), (e) => ({ ...p, error: text(e) }))));
    if (mine !== searches)
        return;
    const panes = state.panes.map((p) => got.find((g) => g.db === p.db) ?? p); // 待っている間に出す DB が変わっていたら、今あるものだけ
    const rows = (i) => panes[i]?.res?.rows.length ?? 0;
    let at = rows(state.at) ? state.at : Math.max(0, panes.findIndex((p) => (p.res?.rows.length ?? 0) > 0));
    let sel = 0;
    if (keep) {
        const pane = panes.findIndex((p) => p.db === keep.db);
        const row = panes[pane]?.res?.rows.findIndex((r) => r.path === keep.path) ?? -1;
        if (row >= 0)
            [at, sel] = [pane, row];
    }
    update({ q, all, sort, filters, panes, at, sel, rec: undefined, message: "" });
    selected();
}
function closePreview() {
    previews++;
    state.preview?.urls.forEach((u) => URL.revokeObjectURL(u));
    if (state.preview)
        update({ preview: undefined });
}
async function morePages(n) {
    const mine = ++previews;
    for (let i = 0; i < n; i++) {
        const p = state.preview;
        if (!p || !p.more)
            return;
        try {
            const url = await pageUrl({ db: p.db, path: p.path, page: p.first + p.urls.length });
            if (mine !== previews) {
                if (url)
                    URL.revokeObjectURL(url);
                return;
            }
            update({ preview: url ? { ...p, urls: [...p.urls, url] } : { ...p, more: false } });
        }
        catch (e) {
            if (mine === previews)
                update({ preview: { ...p, more: false }, message: text(e) });
            return;
        }
    }
}
async function openPreview(first) {
    closePreview();
    const cur = current(state);
    if (!cur)
        return;
    update({ preview: { ...cur, first, urls: [], more: true } });
    await morePages(2);
}
async function fix(apply) {
    const cur = current(state);
    if (!cur)
        return;
    const doi = editDoi();
    if (doi)
        editResult(t.lookingUp); // 外へ問い合わせるので、返るまでに間がある
    try {
        const r = await post("/api/fix", { ...cur, set: editValues(), apply, ...(doi ? { lookup: doi } : {}) });
        if (r.applied) {
            closeEdit();
            update({ dbs: state.dbs.map((d) => (d.id === cur.db ? { ...d, stale: true } : d)) });
            void search(cur);
        }
        else {
            editResult(r.lines.length ? r.lines.join("\n") : t.noChange);
        }
    }
    catch (e) {
        editResult(text(e));
    }
}
// 片付ける: 種類を変える・DB から外す・別の DB へ移す。変えていない種類と、「残す」の行き先は送らない
let kinds = [];
async function tidy(apply) {
    const cur = current(state);
    const rec = state.rec;
    if (!cur || !rec)
        return;
    const v = tidyValues();
    try {
        const r = await post("/api/tidy", { ...cur, apply, ...(v.kind !== rec.kind_key ? { kind: v.kind } : {}), ...(v.to ? { to: v.to } : {}) });
        if (r.applied) {
            closeTidy();
            const touched = [cur.db, v.to.startsWith("db:") ? v.to.slice(3) : ""];
            update({ dbs: state.dbs.map((d) => (touched.includes(d.id) ? { ...d, stale: true } : d)) });
            void search(v.to ? undefined : cur); // 外した・送った記録は、もうこの一覧に無い
        }
        else {
            tidyResult(r.lines.length ? r.lines.join("\n") : t.noChange);
        }
    }
    catch (e) {
        tidyResult(text(e));
    }
}
// 読み込んである DB すべての情報。DB が 2 つ以上なら、名前の行で区切る
const library = () => {
    Promise.all(state.dbs.map((d) => post("/api/library", { db: d.id }).then((r) => (state.dbs.length > 1 ? [`[ ${d.name} ]`, ...r.lines, ""] : r.lines)))).then((parts) => openLibrary(parts.flat()), fail);
};
const act = (path) => {
    const cur = current(state);
    if (cur)
        post(path, cur).catch(fail);
};
// 一覧をまたいで上下に動く。端まで来たら、行のある隣の一覧へ移る
function move(d) {
    let [at, sel] = [state.at, state.sel + d];
    if (sel >= rowsIn(at)) {
        let next = at + 1;
        while (next < state.panes.length && !rowsIn(next))
            next++;
        [at, sel] = next < state.panes.length ? [next, 0] : [at, rowsIn(at) - 1];
    }
    else if (sel < 0) {
        let prev = at - 1;
        while (prev >= 0 && !rowsIn(prev))
            prev--;
        [at, sel] = prev >= 0 ? [prev, rowsIn(prev) - 1] : [at, 0];
    }
    actions.select(at, sel);
}
const actions = {
    select(pane, row) {
        const n = rowsIn(pane);
        const sel = Math.max(0, Math.min(n - 1, row));
        if (!n || (pane === state.at && sel === state.sel))
            return;
        update({ at: pane, sel });
        selected();
    },
    async more(pane) {
        const p = state.panes[pane];
        const res = p?.res;
        if (!p || !res)
            return;
        try {
            const next = await post("/api/search", { db: p.db, q: state.q, all: state.all, sort: state.sort, ...state.filters, offset: res.rows.length });
            if (state.panes[pane]?.res !== res)
                return; // 待っている間に引き直した
            update({ panes: state.panes.map((x, i) => (i === pane ? { ...x, res: { ...res, rows: [...res.rows, ...next.rows] } } : x)) });
        }
        catch (e) {
            fail(e);
        }
    },
    toggleDb(id) {
        const shown = state.panes.map((p) => p.db);
        const next = state.dbs.map((d) => d.id).filter((x) => (x === id ? !shown.includes(x) : shown.includes(x)));
        if (!next.length)
            return; // 1 つは出したままにする
        closePreview();
        update({ panes: next.map((db) => state.panes.find((p) => p.db === db) ?? { db, res: undefined, error: "" }), at: 0, sel: 0, rec: undefined });
        void search();
    },
    author(q) {
        setQuery(q);
        void search();
    },
    filter(patch) {
        update({ filters: { ...state.filters, ...patch } });
        void search();
    },
    open: () => act("/api/open"),
    reveal: () => act("/api/reveal"),
    link(kind) {
        const cur = current(state);
        if (cur)
            post("/api/open-url", { ...cur, link: kind }).catch(fail);
    },
    copy() {
        const cite = state.rec?.cite;
        if (cite)
            navigator.clipboard.writeText(cite).then(() => update({ copied: true }), fail);
    },
    preview(page) {
        if (state.preview && page === undefined)
            closePreview();
        else
            void openPreview(page ?? 1);
    },
    morePages: () => void morePages(3),
    async pick() {
        const cur = current(state);
        if (!cur)
            return;
        const picked = state.picked.some((p) => same(p, cur)) ? state.picked.filter((p) => !same(p, cur)) : [...state.picked, cur];
        const mine = ++cites;
        update({ picked, pickedCite: "", pickedCopied: false });
        if (!picked.length)
            return;
        try {
            // 先に取っておく。押してから取りに行くと、ブラウザが「利用者の操作の中での書き込み」とみなさないことがある
            const groups = state.dbs.map((d) => ({ db: d.id, paths: picked.filter((p) => p.db === d.id).map((p) => p.path) })).filter((g) => g.paths.length);
            const got = await Promise.all(groups.map((g) => post("/api/cite", g)));
            if (mine === cites)
                update({ pickedCite: got.map((r) => r.text).filter(Boolean).join("\n\n") });
        }
        catch (e) {
            if (mine === cites)
                fail(e);
        }
    },
    copyPicked() {
        if (state.pickedCite)
            navigator.clipboard.writeText(state.pickedCite).then(() => update({ pickedCopied: true }), fail);
    },
    clearPicked() {
        cites++;
        update({ picked: [], pickedCite: "", pickedCopied: false });
    },
    edit() {
        const cur = current(state);
        if (state.rec && cur && same(state.rec, cur))
            openEdit(state.rec);
    },
    tidy() {
        const cur = current(state);
        if (state.rec && cur && same(state.rec, cur))
            openTidy(state.rec, kinds, state.dbs.filter((d) => d.id !== cur.db));
    },
};
// 打つたびに、少し待ってから引く。変換の確定では input と compositionend の両方が来て、順はブラウザで違う。
// どちらも同じ待ちに入れて、1 回だけ引く
let timer = 0;
const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(() => void search(), 200);
};
input.addEventListener("input", (e) => {
    if (!e.isComposing)
        schedule(); // 変換中は引かない
});
input.addEventListener("compositionend", schedule);
for (const box of [allBox, yearFrom, yearTo, sortBox])
    box.addEventListener("change", () => void search());
byId("edit-check").addEventListener("click", () => void fix(false));
byId("edit-write").addEventListener("click", () => void fix(true));
byId("edit-cancel").addEventListener("click", closeEdit);
byId("tidy-check").addEventListener("click", () => void tidy(false));
byId("tidy-write").addEventListener("click", () => void tidy(true));
byId("tidy-cancel").addEventListener("click", closeTidy);
byId("lib-open").addEventListener("click", library);
byId("lib-close").addEventListener("click", closeLibrary);
const keys = {
    "/": () => input.focus(),
    j: () => move(1),
    ArrowDown: () => move(1),
    k: () => move(-1),
    ArrowUp: () => move(-1),
    " ": () => actions.preview(),
    Escape: closePreview,
    o: actions.open,
    f: actions.reveal,
    d: () => {
        const first = state.rec?.links[0];
        if (first)
            actions.link(first);
    },
    c: actions.copy,
    x: () => void actions.pick(),
    C: actions.copyPicked,
    e: actions.edit,
    m: actions.tidy,
    i: library,
};
document.addEventListener("keydown", (e) => {
    // 229 は変換を確定するキー。WebKit(Safari・Tauri の窓)は、そのとき isComposing を立てない
    if (e.isComposing || e.keyCode === 229 || e.metaKey || e.ctrlKey || e.altKey || dialogOpen())
        return;
    const at = e.target;
    if (at instanceof HTMLInputElement || at instanceof HTMLSelectElement) {
        if (e.key === "Enter" && at.type !== "checkbox") {
            clearTimeout(timer);
            void search();
        }
        else if (e.key === "Escape" || (e.key === "ArrowDown" && at === input)) {
            e.preventDefault(); // 検索欄の Escape は、既定では中身を消す
            at.blur();
        }
        return;
    }
    if (at instanceof HTMLButtonElement && (e.key === " " || e.key === "Enter"))
        return; // ボタンを押す操作
    const run = keys[e.key];
    if (!run)
        return;
    e.preventDefault();
    run();
});
// 読み込んである DB を聞いてから引く。最初は全部の DB を出す
async function start() {
    const first = await post("/api/info", {});
    kinds = first.kinds;
    const infos = await Promise.all(first.dbs.map((db) => (db === first.db ? first : post("/api/info", { db }))));
    update({
        dbs: infos.map((x) => ({ id: x.db, name: x.name, stale: x.stale })),
        panes: infos.map((x) => ({ db: x.db, res: undefined, error: "" })),
        canOpen: first.can_open,
    });
    await search();
}
labels();
if (token) {
    start().catch(fail);
    input.focus();
}
else {
    update({ message: t.noToken });
}
