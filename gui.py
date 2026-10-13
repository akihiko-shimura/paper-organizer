#!/usr/bin/env python3
"""検索の GUI のサーバ。標準ライブラリだけ。設計と「これが捕まえないもの」は GUI_DESIGN.md。

    python3 gui.py [--port=N] [--no-open] [--db=<DB のフォルダ>]…   # 127.0.0.1 で待ち、ブラウザで開く
    python3 gui.py --types                    # API の表から gui/src/api.d.ts の中身を出す

検索の索引を書き換えない。索引を書くのは /api/fix と /api/tidy の apply だけ、論文のファイルを動かすのは /api/tidy の apply だけで、
どちらも papers fix・nonpaper-ok・remove・send と同じ関数(退避してから書く・移動先を上書きしない)を通る。
外へ出るのは、直す欄に DOI を入れたときだけ(その DOI の Crossref の記録を引き、応答をキャッシュに置く)。ほかは O._fetch を断る関数に
差し替えてある。
- GET は画面のファイル(起動時に作った一覧にある名前)だけを返す。蔵書の情報は返さない
- 索引に触るものは POST の /api/* で、起動ごとの合言葉(X-Papers-Token)と、Host が 127.0.0.1:番号 であることが要る
- ファイルを開くのは、索引にあるパス・.pdf で終わる・実在する、の 3 つを満たすときだけ
"""
import collections, hmac, http.server, json, os, re, secrets, shutil, signal, sqlite3, subprocess, sys, threading, time, traceback
import urllib.parse, webbrowser

DEFAULT_PORT = 8377          # 固定にする: 画面が覚えるもの(localStorage)は番号ごとに別なので、毎回変えると消える
MAX_BODY = 1 << 20           # 要求の本文の上限。超える長さを名乗った要求は読まずに断る
TIMEOUT = 30                 # 何も送らない接続を待つ秒数
PAGE = 50                    # 一覧の 1 回分の行数
HITS = 50                    # 1 件の全頁から返す、前後の文章の数
DPI = 150                    # 頁の絵の細かさ
SORTS = ("relevance", "newest", "oldest", "author")
EDIT_KEYS = ("title", "authors", "first_author", "year", "journal")     # 画面から直せる項目。O.FIX_KEYS の一部
HEADERS = [
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                                "img-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
]
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8"}
# 外へ開く URL の先頭。閉じた集合で、ここに無い先頭の URL は作らない
URL_PREFIX = {"doi": "https://doi.org/", "arxiv": "https://arxiv.org/abs/", "scholar": "https://scholar.google.com/scholar?q="}
LINKS = ("doi", "published_doi", "arxiv", "scholar")

# API の形。"str"・"int"・"bool" は値の型、"jpeg" は絵のバイト列、[x] は並び、{…} は項目の決まった辞書。名前の末尾の ? は「無くてもよい」。
# 要求はこの表に合わなければ 400。応答が合うことは gui_check.py が見る。画面の型(gui/src/api.d.ts)もここから作る。
_BIB = {"path": "str", "title?": "str", "guess?": "str", "filename": "str", "journal?": "str", "year?": "int",
        "volume?": "str", "pages?": "str", "state": "str", "kind": "str"}
_QUERY = {"q": "str", "all": "bool"}
_FILTERS = {"year_from?": "int", "year_to?": "int", "journal?": "str", "state?": "str", "kind?": "str"}
_COUNTS = [{"v": "str", "n": "int"}]
_EDIT = {k + "?": "str" for k in EDIT_KEYS}
_PATH = {"path": "str"}
_OK = {"ok": "bool"}
API = {
    "/api/info": {"req": {}, "res": {"records": "int", "index_updated": "str", "columns": ["str"], "can_open": "bool", "stale": "bool",
                                     "db": "str", "name": "str", "dbs": ["str"], "kinds": ["str"]}},
    "/api/library": {"req": {}, "res": {"lines": ["str"]}},
    "/api/search": {"req": dict(_QUERY, **_FILTERS, **{"sort?": "str", "offset?": "int"}),
                    "res": {"total": "int", "near?": "int", "substring": "bool", "short": ["str"], "unused": ["str"],
                            "facets": {"years": [{"v": "int", "n": "int"}], "journals": _COUNTS, "states": _COUNTS, "kinds": _COUNTS},
                            "rows": [dict(_BIB, **{"first_author?": "str", "n_authors": "int", "only_full": "bool"})]}},
    "/api/record": {"req": dict(_QUERY, path="str"),
                    "res": dict(_BIB, **{"where": "str", "authors": [{"name": "str", "q": "str"}], "issue?": "str", "doi?": "str",
                                         "arxiv_id?": "str", "published_doi?": "str", "abstract?": "str", "links": ["str"],
                                         "cite?": "str", "cite_unsure": "bool", "hit_total": "int", "hits_note?": "str",
                                         "edit": _EDIT, "kind_key": "str",
                                         "hits": [{"page": "int", "parts": [{"hit": "bool", "text": "str"}]}]})},
    "/api/page": {"req": {"path": "str", "page": "int"}, "res": "jpeg"},
    "/api/cite": {"req": {"paths": ["str"]}, "res": {"text": "str", "made": "int", "skipped": "int"}},
    "/api/fix": {"req": {"path": "str", "set": _EDIT, "apply": "bool", "lookup?": "str"}, "res": {"lines": ["str"], "applied": "bool"}},
    "/api/tidy": {"req": {"path": "str", "kind?": "str", "to?": "str", "apply": "bool"}, "res": {"lines": ["str"], "applied": "bool"}},
    "/api/open": {"req": _PATH, "res": _OK},
    "/api/reveal": {"req": _PATH, "res": _OK},
    "/api/open-url": {"req": {"path": "str", "link": "str"}, "res": _OK},
}
# どの要求も、相手の DB を db で選べる(/api/info の dbs にある番号。省くと最初の DB)。DB ごとに索引も検索の索引も別
API = {path: dict(spec, req=dict(spec["req"], **{"db?": "str"})) for path, spec in API.items()}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def check(spec, v, at="body"):
    """値を API の形と照らす。合わなければ理由(英語)、合えば None。知らない項目・足りない項目・型の違いを見る。"""
    if isinstance(spec, dict):
        if not isinstance(v, dict):
            return "%s must be an object" % at
        names = {k.rstrip("?"): k for k in spec}
        unknown = sorted(set(v) - set(names))
        if unknown:
            return "unknown field: %s" % ", ".join(unknown)
        for name, k in names.items():
            if v.get(name) is None:
                if not k.endswith("?"):
                    return "missing field: %s" % name
            else:
                err = check(spec[k], v[name], name)
                if err:
                    return err
        return None
    if isinstance(spec, list):
        if not isinstance(v, list):
            return "%s must be a list" % at
        return next((e for e in (check(spec[0], x, at) for x in v) if e), None)
    ok = isinstance(v, {"str": str, "bool": bool, "int": int, "jpeg": bytes}[spec]) and not (spec == "int" and isinstance(v, bool))
    return None if ok else "field %s must be %s" % (at, spec)


def _ts(spec):
    if isinstance(spec, dict):
        return "{ %s}" % "".join("%s%s: %s; " % (k.rstrip("?"), "?" if k.endswith("?") else "", _ts(s)) for k, s in spec.items())
    if isinstance(spec, list):
        return "Array<%s>" % _ts(spec[0])
    return {"str": "string", "int": "number", "bool": "boolean", "jpeg": "Blob"}[spec]


def types():
    """API の表を TypeScript の型にしたもの(gui/src/api.d.ts の中身)。"""
    rows = "".join('  "%s": { req: %s; res: %s };\n' % (p, _ts(a["req"]), _ts(a["res"])) for p, a in API.items())
    return "// gui.py の API の表から作る(python3 gui.py --types > gui/src/api.d.ts)。手で直さない。\nexport interface Api {\n%s}\n" % rows


def some(d):
    """値が None・空文字の項目を落とす(応答に null を入れない)。"""
    return {k: v for k, v in d.items() if v is not None and v != ""}


# ---------- 外へ出ない ----------
_net = threading.local()
_online = {}                                         # organize のモジュール → 差し替える前の O._fetch


class Offline(Exception):
    pass


def _offline(url, *args, **kw):
    """O._fetch の代わりに置く。この過程は外へ取りに行かない。呼ばれたことは _net.tried に残す。
    例外は 1 つ: 直す欄の DOI を引いている間だけ、その DOI のための URL(_net.allow。O.doi_urls が決める)を、本物の関数(_net.fetch)に渡す。"""
    if any(url == a or url.startswith(a + "?") for a in getattr(_net, "allow", None) or ()):
        return _net.fetch(url, *args, **kw)
    _net.tried = True
    raise Offline(url)


class Library:
    """索引を持ち、(大きさ, 更新時刻) が変わったら読み直す。読むのは O.load_index だけ(CLI と同じ読み方)。"""

    def __init__(self, O):
        self.O, self.lock, self.stamp, self.by_path, self.root = O, threading.Lock(), None, {}, "/"

    def records(self):
        with self.lock:
            st = os.stat(self.O.INDEX)
            if (st.st_size, st.st_mtime_ns) != self.stamp:
                recs, self.stamp = self.O.load_index()
                self.by_path = {r["path"]: r for r in recs if r.get("path")}
                folders = [r["folder"] for r in recs if r.get("folder")]
                self.root = os.path.commonpath(folders) if folders else "/"
            return self.by_path


class App:
    """DB 1 つ分の持ち物(索引・検索の索引・外への道)。合言葉・画面のファイル・OS への命令は、サーバの全部の DB で同じものを持つ。
    run(引数の並び)と open_url(URL)は OS への命令で、検査では記録役に差し替える。name は画面に出す DB の名前(無ければ決める)。"""

    def __init__(self, O, token, static_dir, run=None, open_url=None, name=None):
        self.name, self.ids = name, ("0", ["0"])       # ids は (この DB の番号, サーバが読んでいる全部の DB の番号)。make_server が入れる
        self.peers = {"0": self}                       # 番号 → 同じサーバのすべての DB(別の DB へ送るときの相手)。make_server が入れる
        if O._fetch is not _offline:                 # 本物の取りに行く関数を取っておき、断る関数に差し替える。同じ DB の App を作り直しても失わない
            _online[O], O._fetch = O._fetch, _offline
        self.fetch = _online.get(O)                  # 使うのは、直す欄の DOI を引くときだけ
        self.O, self.lib, self.port, self.quiet = O, Library(O), None, False
        self.token = token or secrets.token_urlsafe(32)
        self.static = static_files(static_dir)
        self.run = run or (lambda argv: subprocess.run(argv, timeout=20, check=True, capture_output=True))
        self.open_url = open_url or webbrowser.open
        self.render = _render                           # 頁の絵を作る命令(pdftoppm)。検査では差し替える
        self.write_lock = threading.Lock()              # 索引を書く要求(/api/fix)は 1 つずつ
        self.can_open = sys.platform == "darwin"       # open・open -R は macOS の命令。ほかの OS では開く操作を画面に出さない

    def db(self):
        """検索の索引を読み取り専用で開く。作り直さない(古ければ /api/info の stale で知らせる)。作り方が違えば 409。"""
        if not self.O._db_format_ok():
            raise ApiError(409, "the search index is missing or was built by another version. Run: papers index")
        return sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(self.O.DB), uri=True)


def static_files(d):
    """配る画面のファイルの一覧(道 → ファイル)。起動時に 1 回作り、要求の文字列からパスを作らない。"""
    out = {}
    for route, rel in [("/", "index.html"), ("/style.css", "style.css")]:
        if os.path.isfile(os.path.join(d, rel)):
            out[route] = os.path.join(d, rel)
    dist = os.path.join(d, "dist")
    for name in sorted(os.listdir(dist)) if os.path.isdir(dist) else []:
        if name.endswith(".js") and os.path.isfile(os.path.join(dist, name)):
            out["/dist/" + name] = os.path.join(dist, name)
    return out


# ---------- 検索 ----------
def query_for(O, q, all_terms):
    """画面の入力を O._fts・O._match に渡す文字列にする。演算子の無い入力で「すべての語」なら、語を AND でつないだ式にする
    (語の切り方は O.TERM のまま)。3 文字以上の語が無い入力は部分一致の経路へ行くので、そのまま返す。"""
    q = O._norm(q)
    terms = O.TERM.findall(q.lower())
    if all_terms and len(terms) > 1 and not O._OPS.search(q):
        return " AND ".join('"%s"' % t for t in terms)
    return q


def state_of(O, r):
    """一覧に出す記録の状態。どの記録もちょうど 1 つに入る。"""
    if r.get("rung") in ("supplement", "unverified"):
        return r["rung"]
    return "resolved" if O.renamable(r) else "unresolved"


def _bib(O, r):
    """一覧と詳細に共通の書誌(API の表の _BIB)。"""
    y = r.get("year")
    return {"path": r["path"], "title": r.get("title"), "guess": None if r.get("title") else r.get("title_guess"),
            "filename": r.get("filename") or os.path.basename(r["path"]), "journal": r.get("journal_short") or r.get("journal"),
            "year": int(y) if str(y or "").isdigit() else None, "volume": str(r["volume"]) if r.get("volume") else None,
            "pages": str(r.get("pages") or r.get("article_number") or ""), "state": state_of(O, r), "kind": O.kind_group(r)}


def find(app, q, all_terms):
    """(一覧に出すパスの並び, そのうち表題・著者・抄録・先頭の本文で当たるパスの集合か None, 部分一致の経路か)。
    並びは O._fts のまま: 先頭側で当たる記録が先、全頁の本文でだけ当たる記録が後ろ。移した重複と、索引に無いパスは外す。
    入力が空なら、全部の記録を索引の順で(絞り込みだけで眺める)。"""
    O, recs, q2 = app.O, app.lib.records(), query_for(app.O, q, all_terms)
    if not q2.strip():
        return [p for p, r in recs.items() if not O.moved_aside(r)], None, False
    m = O._match(q2)
    c = app.db()
    try:
        paths = O._fts(c, q2, 1 << 30)
        near = None if not m or O._FULL_COL.search(m) else {p for (p,) in c.execute("SELECT path FROM p WHERE p MATCH ?", (m,))}
    except sqlite3.OperationalError as e:
        raise ApiError(400, "invalid search expression (%s). Write AND, OR, NOT in capitals and quote terms that contain "
                            "hyphens or symbols (\"second-harmonic\")." % e)
    finally:
        c.close()
    return [p for p in paths if p in recs and not O.moved_aside(recs[p])], near, not m


def _attrs(O, r):
    """絞り込みに使う 4 つの値。年と誌名は無いことがある(None)。"""
    y = r.get("year")
    return {"year": int(y) if str(y or "").isdigit() else None, "journal": r.get("journal_short") or r.get("journal") or None,
            "state": state_of(O, r), "kind": O.kind_group(r)}


def api_search(app, body):
    O, recs, q = app.O, app.lib.records(), app.O._norm(body["q"])
    if body.get("sort") not in (None,) + SORTS:
        raise ApiError(400, "unknown sort: use %s" % ", ".join(SORTS))
    paths, near, substring = find(app, q, body["all"])
    at = {p: _attrs(O, recs[p]) for p in paths}
    lo, hi = body.get("year_from"), body.get("year_to")
    tests = {"year": lambda a: (lo is None and hi is None) or (a["year"] is not None and (lo is None or a["year"] >= lo)
                                                               and (hi is None or a["year"] <= hi))}
    for key in ("journal", "state", "kind"):
        tests[key] = lambda a, key=key: body.get(key) is None or a[key] == body[key]
    passes = lambda p, skip=None: all(t(at[p]) for k, t in tests.items() if k != skip)
    facets = {}
    for name, key in (("years", "year"), ("journals", "journal"), ("states", "state"), ("kinds", "kind")):
        # 各値の件数は、その項目自身の絞り込みを外して数える(年を 1 つ選んでも、ほかの年の件数が消えない)
        c = collections.Counter(at[p][key] for p in paths if at[p][key] is not None and passes(p, key))
        items = sorted(c.items()) if key == "year" else c.most_common(12 if key == "journal" else None)
        if key == "journal" and body.get(key) is not None and body[key] not in dict(items):
            items.append((body[key], c.get(body[key], 0)))                # 選んである誌名は、上位に入らなくても出す
        facets[name] = [{"v": v, "n": n} for v, n in items]
    hit = [p for p in paths if passes(p)]
    by = {"newest": lambda p: -(at[p]["year"] or 0), "oldest": lambda p: at[p]["year"] or 9999,
          "author": lambda p: (recs[p].get("first_author") or "").strip().casefold() or "\uffff"}.get(body.get("sort"))
    if by:
        hit.sort(key=by)                                                   # 同じ値どうしは、もとの並び(関連度)のまま
    off = max(0, body.get("offset") or 0)
    rows = [some(dict(_bib(O, recs[p]), first_author=(recs[p].get("first_author") or "").strip(),
                      n_authors=len(recs[p].get("authors") or []), only_full=near is not None and p not in near))
            for p in hit[off:off + PAGE]]
    return some({"total": len(hit), "near": None if near is None else sum(1 for p in hit if p in near), "rows": rows,
                 "substring": substring, "facets": facets,
                 "short": O._short_terms(q) if O._OPS.search(q) else [], "unused": O._unused_terms(q)})


# ---------- 1 件の詳細 ----------
def people(app, r):
    """(著者ごとの {名前, その姓で引く式}, BibTeX 用の著者, 姓と名の区切りが出版社の記録で確かめられたか)。
    姓は Crossref の応答のキャッシュに「姓」の欄があればそれ、次に記録の families(目録で同定した本。著者と同じ並びで、その名前の中にある姓だけ)、
    どちらも無ければ名前の中の位置(Crossref 由来は先頭の語、ほかは最後の語)。"""
    O, shown = app.O, r.get("authors") or ([r["first_author"]] if r.get("first_author") else [])
    fams = r.get("families") if len(r.get("families") or []) == len(shown) else None
    _net.tried = False
    bib = O._bib_authors(r)                        # キャッシュに無ければ索引の著者の並びそのものを返す
    sure = bool(r.get("source") == "crossref" and r.get("doi") and not _net.tried and bib is not r.get("authors"))
    out = []
    for i, name in enumerate(shown):
        words = name.split()
        if sure and i < len(bib):
            family = bib[i].split(", ")[0]
        elif fams and fams[i] and fams[i] in name:     # 名前の中に無い姓は、著者を直す前のもの(fix は families を直さない)
            family = fams[i]
        else:
            family = (words[0] if r.get("source") == "crossref" else words[-1]) if words else ""
        out.append({"name": name, "q": 'author:" %s "' % " ".join(family.replace('"', " ").split())})
    return out, bib, sure


_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f]')
_MARKS = [chr(i) for i in list(range(1, 9)) + list(range(14, 32))]


def _spans(app, c, q2, path, text):
    """全頁の本文の中で、式に当たった所の (始まり, 終わり) の並び。当たりを決めるのは検索と同じ FTS5(highlight)。
    印には、この本文に現れない制御文字を選ぶ(PDF から来た本文には制御文字が混ざる)。部分一致の経路は同じ部分文字列を探す。"""
    O = app.O
    m = O._match(q2)
    if not m:
        low, out = text.lower(), []
        for t in {t.lower() for t in q2.split()}:
            i = low.find(t)
            while i >= 0:
                out.append((i, i + len(t)))
                i = low.find(t, i + len(t))
        return out
    free = [ch for ch in _MARKS if ch not in text]
    if len(free) < 2:
        return []
    a, b = free[:2]
    r = c.execute("SELECT highlight(f,%d,?,?) FROM f WHERE f MATCH ? AND path=?" % (len(O.FCOLS) - 1), (a, b, m, path)).fetchone()
    pos, start, out = 0, None, []
    for piece in re.split("([%s%s])" % (re.escape(a), re.escape(b)), r[0] if r else ""):
        if piece == a:
            start = pos
        elif piece == b:
            if start is not None:
                out.append((start, pos))
            start = None
        else:
            pos += len(piece)
    return out


def _word(found, cut):
    """前後の文章の端。近くに空白があればそこ、無ければ(和文など)切った位置のまま。"""
    return found if found > 0 else cut


def snippets(text, spans, limit=HITS, ctx=90, gap=60):
    """当たりの位置から、頁番号つきの前後の文章を作る。(最初の limit 個, 全部の数)。近い当たりは 1 つにまとめ、頁をまたがない。
    頁の区切りは改頁(\\f)。頁番号は PDF の何枚目か。"""
    merged = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(e, merged[-1][1]))
        else:
            merged.append((s, e))
    out, total, i = [], 0, 0
    while i < len(merged):
        end = text.find("\f", merged[i][0])
        end = len(text) if end < 0 else end
        j = i
        while j + 1 < len(merged) and merged[j + 1][0] - merged[j][1] <= gap and merged[j + 1][1] <= end:
            j += 1
        total += 1
        if len(out) < limit:
            start, pos, last = text.rfind("\f", 0, merged[i][0]) + 1, merged[i][0] - ctx, merged[j][1] + ctx
            pos = start if pos <= start else _word(text.find(" ", pos, pos + 20) + 1, pos)       # 語の途中から始めない
            last = end if last >= end else _word(text.rfind(" ", last - 20, last), last)        # 語の途中で終えない
            parts = []
            for s, e in merged[i:j + 1] + [(last, None)]:                                       # 最後の 1 つは、後ろの文脈の終わり
                if s > pos:
                    parts.append({"hit": False, "text": _CTRL.sub("", text[pos:s])})
                if e is not None:
                    parts.append({"hit": True, "text": text[s:e]})
                    pos = e
            out.append({"page": text.count("\f", 0, merged[i][0]) + 1, "parts": [p for p in parts if p["text"]]})
        i = j + 1
    return out, total


def link_url(r, link):
    """外へ開く URL を記録から作る。先頭は URL_PREFIX の 3 つだけ。作れなければ None。
    表題での検索は title がある記録だけ(推定の表題 title_guess では作らない)。"""
    q = lambda s: urllib.parse.quote(str(s), safe="-._~/")
    if link == "doi" and r.get("doi"):
        return URL_PREFIX["doi"] + q(r["doi"])
    if link == "published_doi" and r.get("published_doi"):
        return URL_PREFIX["doi"] + q(r["published_doi"])
    if link == "arxiv" and r.get("arxiv_id"):
        return URL_PREFIX["arxiv"] + q(r["arxiv_id"])
    if link == "scholar" and r.get("title"):
        return URL_PREFIX["scholar"] + urllib.parse.quote(str(r["title"]), safe="")
    return None


def _record(app, path):
    r = app.lib.records().get(path)
    if r is None or app.O.moved_aside(r):
        raise ApiError(404, "not in the index")
    return r


def _citable(O, r):
    """引用を作れる記録か。papers cite と同じ条件。"""
    return bool(O.renamable(r) or r.get("rung") == "unverified")


def _names(v):
    """著者の並びを、直す欄の形(「;」でつないだ 1 行)に。O.fix_record が読む形と同じ。"""
    return "; ".join(a for a in (" ".join(x.split()) for x in v) if a)


def _kind_key(O, r):
    """記録の今の種類(O.KINDS のキー)。種類が決まっていなければ、その DB の既定(書籍の DB は book、ほかは paper。O.kind_group と同じ)。
    書籍の DB は、論文以外の印が無くても種類を持つ(プレプリントは同定のときに bookpart が付く)。"""
    k = r.get("kind")
    if k in O.KINDS and (r.get("not_paper") or k == "paper" or O.BOOKS):
        return k
    return "book" if O.BOOKS else "paper"


def _edit(r):
    """画面から直せる項目の、今の値(文字列)。"""
    return some({k: (_names(r[k]) if isinstance(r[k], list) else str(r[k])) if r.get(k) is not None else None for k in EDIT_KEYS})


def api_record(app, body):
    O, r = app.O, _record(app, body["path"])
    authors, bib, sure = people(app, r)
    citable = _citable(O, r)
    q2 = query_for(O, body["q"], body["all"])
    hits, total, note = [], 0, None
    if q2.strip():
        c = app.db()
        try:
            row = c.execute("SELECT fulltext FROM f WHERE path=?", (r["path"],)).fetchone()
            if not row or not row[0]:
                note = "no_fulltext"
            else:
                hits, total = snippets(row[0], _spans(app, c, q2, r["path"], row[0]))
                note = None if total else "none_in_text"
        except sqlite3.OperationalError as e:
            raise ApiError(400, "invalid search expression (%s)" % e)
        finally:
            c.close()
    res = some(dict(_bib(O, r), where=os.path.relpath(r.get("folder") or os.path.dirname(r["path"]), app.lib.root),
                    issue=str(r["issue"]) if r.get("issue") else None, doi=r.get("doi"), arxiv_id=r.get("arxiv_id"),
                    published_doi=r.get("published_doi"), abstract=r.get("abstract"),
                    cite=O.bibtex(r, bib) if citable else None, hits_note=note))
    return dict(res, edit=_edit(r), kind_key=_kind_key(O, r), authors=authors, links=[k for k in LINKS if link_url(r, k)], hits=hits, hit_total=total,
                cite_unsure=bool(citable and r.get("source") == "crossref" and not sure))


def api_info(app, body):
    O, recs = app.O, app.lib.records()
    stale = True
    if O._db_format_ok():
        c = app.db()
        try:
            m = c.execute("SELECT v FROM meta WHERE k='mtime'").fetchone()
            stale = not m or float(m[0]) < os.path.getmtime(O.INDEX)      # papers search が作り直すのと同じ条件
        finally:
            c.close()
    return {"records": sum(1 for r in recs.values() if not O.moved_aside(r)),
            "index_updated": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(os.path.getmtime(O.INDEX))),
            "columns": ["author"] + list(O.SEARCH_COLS), "can_open": app.can_open, "stale": stale,
            "db": app.ids[0], "dbs": app.ids[1], "kinds": list(O.KINDS),
            # 名前は、起動時に付けたもの、その DB の config.json の name、論文の置き場のフォルダ名の順
            "name": str(app.name or _own(O).get("name") or os.path.basename(app.lib.root.rstrip(os.sep)) or os.path.basename(O.ROOT))}


def api_library(app, body):
    """今読んでいる DB の場所・件数・新しさ。papers info と同じ行に、頁の絵を作る道具の場所を足す。"""
    tool = shutil.which("pdftoppm")
    return {"lines": app.O.info_lines()[1] + ["page images  %s" % (tool or "pdftoppm was not found (install poppler to preview pages)")]}


def _pdf(app, path):
    """OS の命令に渡してよいファイルか。索引にあるパス・.pdf で終わる・実在する、の 3 つ。"""
    if path not in app.lib.records():
        raise ApiError(404, "not in the index")
    if not (os.path.isabs(path) and path.endswith(".pdf")):
        raise ApiError(400, "not a .pdf file")
    if not os.path.isfile(path):
        raise ApiError(404, "file is missing")
    return path


def _open(app, argv):
    if not app.can_open:
        raise ApiError(501, "opening files is not supported on this system")
    app.run(argv)
    return {"ok": True}


def api_open(app, body):
    return _open(app, ["open", _pdf(app, body["path"])])


def api_reveal(app, body):
    return _open(app, ["open", "-R", _pdf(app, body["path"])])


def _render(path, page):
    """PDF の 1 頁を JPEG のバイト列で。頁が無ければ空。poppler の pdftoppm を使い、ファイルには書かない。"""
    return subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(DPI), "-jpeg", path],
                          capture_output=True, timeout=30).stdout


def api_page(app, body):
    path = _pdf(app, body["path"])
    if body["page"] < 1:
        raise ApiError(400, "page starts at 1")
    try:
        data = app.render(path, body["page"])
    except FileNotFoundError:
        raise ApiError(501, "pdftoppm (poppler) is not installed")
    except subprocess.TimeoutExpired:
        raise ApiError(504, "rendering the page took too long")
    if not data:
        raise ApiError(404, "no such page")
    return data


def api_cite(app, body):
    """選んだ記録の BibTeX をつないだもの。作れない記録(同定できていない・索引に無い)は数だけ返す。"""
    O, recs, out = app.O, app.lib.records(), []
    for p in body["paths"]:
        r = recs.get(p)
        if r is not None and not O.moved_aside(r) and _citable(O, r):
            out.append(O.bibtex(r, people(app, r)[1]))
    return {"text": "".join(b + "\n\n" for b in out), "made": len(out), "skipped": len(body["paths"]) - len(out)}


def _doi(O, s):
    """直す欄に打たれた DOI を、DOI そのものに(貼り付けた https://doi.org/… や doi: の先頭を外す)。DOI の形でなければ None。"""
    s = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', s.strip(), flags=re.I)
    return s if len(s) <= 300 and O.DOI_RE.fullmatch(s) else None


def _isbn(O, s):
    """直す欄に打たれた ISBN を 13 桁に(先頭の「ISBN」「ISBN-13:」、ハイフンと空白を外す)。検査数字の合う ISBN でなければ None。"""
    return O.isbn13(re.sub(r'^isbn(?:-1[03])?:?\s*', '', s.strip(), flags=re.I))


def api_fix(app, body):
    """1 件の書誌を直す。papers fix と同じ関数を通り、apply のときだけ索引を書く(書く前に退避)。
    変わっていない項目は書かない。lookup(DOI か ISBN)があれば、その番号の記録を引き(O.by_doi・O.by_isbn)、
    書き換えた項目だけをその上に入れる。外へ通すのは、その番号のための URL(O.doi_urls・O.isbn_urls)だけ。
    返すのは、直す前と後の違いの行。"""
    O = app.O
    doi = isbn = None
    if (body.get("lookup") or "").strip():
        doi = _doi(O, body["lookup"])
        isbn = None if doi else _isbn(O, body["lookup"])
        if not (doi or isbn):
            raise ApiError(400, "this is neither a DOI (like 10.1234/abcd) nor an ISBN (10 or 13 digits; the last digit is a check digit)")
        if app.fetch is None:
            raise ApiError(501, "looking up a DOI or an ISBN is not available in this server")
    sets = {k: _names(v.split(";")) if k == "authors" else " ".join(v.split()) for k, v in body["set"].items() if v is not None}
    if any(len(v) > 1000 for v in sets.values()):
        raise ApiError(400, "value too long")
    with app.write_lock:
        recs, stamp = O.load_index()
        r = next((x for x in recs if x.get("path") == body["path"] and not O.moved_aside(x)), None)
        if r is None:
            raise ApiError(404, "not in the index")
        if sets.get("year") and not sets["year"].isdigit():
            raise ApiError(400, "year must be digits")
        sets = {k: v for k, v in sets.items() if v != _edit(r).get(k, "")}
        if not sets and not (doi or isbn):
            return {"lines": [], "applied": False}
        if doi or isbn:
            del O._FAILED[:]
            _net.allow, _net.fetch = O.doi_urls(doi) if doi else [u for us in O.isbn_urls(isbn).values() for u in us], app.fetch
        try:
            new = O.fix_record(r, doi, sets, isbn)
        except LookupError as e:                         # 理由は O.by_doi・O.by_isbn の文面。届かなかった(時間切れ・相手の不調)は、記録が無い、とは違う
            raise ApiError(502 if O._FAILED else 404, str(e))
        finally:
            _net.allow = None
        lines = O.fix_diff(r, new)
        if body["apply"]:
            try:
                O.fix_apply(recs, stamp, r, new)
            except SystemExit as e:                      # 読んだ後に索引が変わっていた(save_index が書かずに止まる)
                raise ApiError(409, str(e.code))
    return {"lines": lines, "applied": body["apply"]}


def api_tidy(app, body):
    """論文でないもの・要らないものを片付ける: 種類を変える(kind)、DB から外す(to="remove")、別の DB へ送る(to="db:<番号>")。
    papers nonpaper-ok・remove・send と同じ関数を通り、apply のときだけ索引を書き、ファイルを動かす。返すのは、変わる項目とファイルの行き先の行。"""
    O, to = app.O, body.get("to") or ""
    target = app.peers.get(to[3:]) if to.startswith("db:") else None
    if to.startswith("db:") and (target is None or target is app):
        raise ApiError(404, "no such DB to send to")
    if not to.startswith("db:") and to not in ("", "remove"):
        raise ApiError(400, "unknown destination")
    if body.get("kind") is not None and body["kind"] not in O.KINDS:
        raise ApiError(400, "unknown kind")
    locks = sorted({a.ids[0]: a for a in (app, target) if a is not None}.items())      # 2 つの DB を書くときは、番号の順に待つ
    for _, a in locks:
        a.write_lock.acquire()
    try:
        recs, stamp = O.load_index()
        r = next((x for x in recs if x.get("path") == body["path"] and not O.moved_aside(x)), None)
        if r is None:
            raise ApiError(404, "not in the index")
        old = dict(r)
        if body.get("kind") is not None and body["kind"] != _kind_key(O, r):
            O.nonpaper_marks([r], {r["path"]: O.KINDS[body["kind"]][0]}, resolved_too=True)
        lines = O.fix_diff(old, r)
        try:
            plan = O.remove_plan(recs, [r]) if to == "remove" else O.send_plan(recs, [r], target.O) if target else []
        except ValueError as e:                              # 外せない・送れない理由(指されている記録・置き場が決まらない など)
            raise ApiError(409, str(e))
        lines += ["  %-14s %s → %s" % ("file", x["path"], new or "(the file is missing: only the record is marked)") for x, new in plan]
        lines += ["  %-14s None → user" % "removed"] if to == "remove" else []
        if not body["apply"] or not lines:
            return {"lines": lines, "applied": False}
        try:
            if to == "remove":
                O.remove_apply(recs, stamp, plan)
            elif target:
                O.send_apply(recs, stamp, plan, target.O)
            else:
                O._backup("kind")
                O.save_index(recs, stamp)
        except SystemExit as e:                              # 読んだ後に索引が変わっていた(save_index が書かずに止まる)
            raise ApiError(409, str(e.code))
        except FileExistsError:
            raise ApiError(409, "a file with that name appeared at the destination; nothing was overwritten")
        return {"lines": lines, "applied": True}
    finally:
        for _, a in reversed(locks):
            a.write_lock.release()


def api_open_url(app, body):
    url = link_url(_record(app, body["path"]), body["link"])
    if url is None:
        raise ApiError(404, "this record has no such link")
    app.open_url(url)
    return {"ok": True}


HANDLERS = {"/api/info": api_info, "/api/library": api_library, "/api/search": api_search, "/api/record": api_record, "/api/page": api_page,
            "/api/cite": api_cite, "/api/fix": api_fix, "/api/tidy": api_tidy,
            "/api/open": api_open, "/api/reveal": api_reveal, "/api/open-url": api_open_url}


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    timeout = TIMEOUT
    server_version, sys_version = "papers-gui", ""
    # 基底クラスが自分で返す誤り(501・壊れた要求行の 400)の本文。既定の本文は要求の文字列を写すので、番号だけにする。
    # 版の無い壊れた要求行(HTTP/0.9 扱い)には、基底クラスが状態の行もヘッダも付けずにこの本文だけを返す
    error_message_format, error_content_type = "%(code)d\n", "text/plain"

    def end_headers(self):                       # 基底クラスが自分で返す 501・400 にも付く
        for k, v in HEADERS:
            self.send_header(k, v)
        super().end_headers()

    def log_request(self, code="-", size="-"):   # 固定の書式。要求の文字列(検索語・壊れた要求行)をそのまま出さない
        if not self.app.quiet:
            known = getattr(self, "path", None) in self.app.static or getattr(self, "path", None) in API
            print("%s %s %s" % (self.command if getattr(self, "command", None) in ("GET", "POST") else "?",
                                self.path if known else "?", getattr(code, "value", code)), file=sys.stderr)

    def log_error(self, *args):                  # 基底クラスの文面は要求の文字列を含む。log_request の 1 行で足りる
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """localhost は許さない: ::1 が先に引かれる機械では、同じ番号の別のサーバに届く。"""
        return self.headers.get("Host") == "127.0.0.1:%d" % self.app.port

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        if self.path.startswith("/api/"):
            return self._send(405, {"error": "use POST"})
        f = self.app.static.get(self.path)
        if f is None:
            return self._send(404, {"error": "not found"})
        with open(f, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES[os.path.splitext(f)[1]])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            code, obj = 200, self._post()
        except ApiError as e:
            code, obj = e.code, {"error": e.msg}
        except BaseException as e:               # SystemExit・RateLimitAbort でもスレッドを黙って消さない
            tb = traceback.extract_tb(e.__traceback__)
            where = "%s:%d" % (os.path.basename(tb[-1].filename), tb[-1].lineno) if tb else "?"
            if not self.app.quiet:               # 種類と場所だけ。文面には検索語やパスが入りうる
                print("error: %s at %s" % (type(e).__name__, where), file=sys.stderr)
            code, obj = 500, {"error": "internal error (%s)" % type(e).__name__}
        if isinstance(obj, bytes):
            self.send_response(code)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(obj)))
            self.end_headers()
            self.wfile.write(obj)
        else:
            self._send(code, obj)

    def _post(self):
        if not self._host_ok():
            raise ApiError(403, "forbidden host")
        spec = API.get(self.path)
        if spec is None:
            raise ApiError(404, "not found")
        given = (self.headers.get("X-Papers-Token") or "").encode("utf-8", "replace")
        if not hmac.compare_digest(given, self.app.token.encode()):
            raise ApiError(401, "missing or wrong token")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        try:
            n = int(self.headers.get("Content-Length") or "")
        except ValueError:
            raise ApiError(411, "Content-Length required")
        if n < 0 or n > MAX_BODY:
            raise ApiError(413, "request body too large")
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ApiError(400, "request body is not JSON")
        err = check(spec["req"], body)
        if err:
            raise ApiError(400, err)
        app = self.apps.get(body.pop("db", self.app.ids[0]))     # 索引にあるパスかどうかも、選んだ DB の索引で見る
        if app is None:
            raise ApiError(404, "no such DB")
        return HANDLERS[self.path](app, body)


def make_server(O, port=DEFAULT_PORT, token=None, static_dir=None, run=None, open_url=None, more=()):
    """(サーバ, 最初の DB の App) を返す。port=0 なら空いている番号(検査用)。more は、一緒に開くほかの DB の [(名前か None, モジュール)]。"""
    static_dir = static_dir or os.path.join(os.path.dirname(os.path.realpath(__file__)), "gui")
    app = App(O, token, static_dir, run, open_url)
    apps = [app] + [App(O2, app.token, static_dir, run, open_url, name) for name, O2 in more]
    ids = [str(i) for i in range(len(apps))]
    for i, a in enumerate(apps):
        a.ids, a.peers = (ids[i], ids), dict(zip(ids, apps))
    handler = type("BoundHandler", (Handler,), {"app": app, "apps": dict(zip(ids, apps))})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    for a in apps:
        a.port = httpd.server_address[1]
    return httpd, app


def watch_parent(stream, on_gone):
    """殻(Tauri)から起動されたとき、親が消えたら終わる。親は標準入力をつないだままにするので、
    親が終わると(異常終了でも)読み出しが終わる。"""
    def run():
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            pass
        on_gone()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _holder(port):
    """その番号で待っている過程の (番号, 命令の行)。分からなければ None(lsof が無い・見えない過程)。"""
    try:
        out = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
                             capture_output=True, text=True, timeout=5).stdout
        pid = int(out.split()[0])
        return pid, subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
        return None


def _older(port):
    """その番号を握っているのが、前に立てた papers gui(この gui.py を走らせている別の過程)なら、その過程の番号。違えば None。
    python3 gui.py のように相対パスで起動した過程は、その過程の作業フォルダから見たパスで比べる。"""
    who, me = _holder(port), os.path.realpath(__file__)
    if not who or who[0] == os.getpid():
        return None
    if who[1].endswith(" " + me) or " %s " % me in who[1]:
        return who[0]
    script = re.search(r'(?:^|\s)(\S*gui\.py)(?:\s|$)', who[1])
    try:
        cwd = subprocess.run([shutil.which("lsof") or "/usr/sbin/lsof", "-a", "-p", str(who[0]), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
        cwd = next((l[1:] for l in cwd.splitlines() if l.startswith("n")), None)
    except (OSError, subprocess.TimeoutExpired):
        cwd = None
    return who[0] if script and cwd and os.path.realpath(os.path.join(cwd, script.group(1))) == me else None


def _own(O):
    """その DB 自身の config.json(DB のフォルダのもの)。名前と、一緒に開く DB は、本体の場所の config.json から引き継がない
    (引き継ぐと、別の DB を主に立てたときにも、本体の DB の連れが開く)。"""
    return O._config(O.ROOT)


def asked_dbs(O, argv):
    """一緒に開くほかの DB を (名前, フォルダ) の並びで: その DB の config.json の dbs と、--db=<DB のフォルダ>。"""
    return [(x.get("name"), x.get("home")) for x in _own(O).get("dbs") or [] if isinstance(x, dict) and x.get("home")] \
        + [(None, a.split("=", 1)[1]) for a in argv if a.startswith("--db=")]


def serve(O, argv):
    """--parent-watch は殻(Tauri)から起動するとき用: 合言葉は環境変数 PAPERS_GUI_TOKEN で受け取り、URL を出さず、
    標準入力が閉じたら終わる。"""
    port = int(next((a.split("=", 1)[1] for a in argv if a.startswith("--port=")), DEFAULT_PORT))
    child = "--parent-watch" in argv
    more, seen = [], {os.path.realpath(O.INDEX)}      # 同じ索引のファイルを 2 つの DB として開かない
    for name, home in asked_dbs(O, argv):
        O2 = O.other_db(str(home))
        if os.path.realpath(O2.INDEX) in seen:
            continue
        seen.add(os.path.realpath(O2.INDEX))
        if not os.path.exists(O2.INDEX):
            sys.exit("no index in this DB yet (%s). Build it with: PAPERS_HOME=%s papers add <folder of PDFs>" % (O2.INDEX, O2.ROOT))
        more.append((name, O2))
    listen = lambda: make_server(O, port, os.environ.get("PAPERS_GUI_TOKEN") if child else None, more=more)
    try:
        httpd, app = listen()
    except OSError as e:
        old = None if child else _older(port)
        if old is None:                               # papers gui でない過程が握っている。止めずに、どれかを文面で言う
            who = _holder(port)
            hint = ("The port is held by process %d (%s). If that is an old papers gui, stop it with: kill %d" % (who[0], who[1][-70:].split(" ", 1)[-1], who[0])
                    if who else "Another papers gui may still be running: stop it with Ctrl-C in its terminal")
            sys.exit("cannot listen on 127.0.0.1:%d (%s). %s\nOr use another port with --port=<number>" % (port, e.strerror or e, hint))
        try:                                          # 前に立てた papers gui を止めて、入れ替わる(画面を開き直すたびに、手で止めさせない)
            os.kill(old, signal.SIGTERM)
        except OSError:
            pass
        for _ in range(50):
            time.sleep(0.1)
            try:
                httpd, app = listen()
                break
            except OSError:
                continue
        else:
            sys.exit("cannot listen on 127.0.0.1:%d: the older papers gui (process %d) did not stop. Stop it with: kill %d" % (port, old, old))
        print("papers gui: stopped the older papers gui (process %d) that held port %d" % (old, port), flush=True)
    url = "http://127.0.0.1:%d/#t=%s" % (app.port, app.token)
    if child:
        watch_parent(sys.stdin.buffer, httpd.shutdown)
    else:
        print("papers gui: %s\n(this address works until you stop the server with Ctrl-C)" % url, flush=True)
        if "--no-open" not in argv:
            webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    if "--types" in sys.argv:
        sys.stdout.write(types())
    else:
        import organize
        if not os.path.exists(organize.INDEX):
            sys.exit("no index yet (%s). Start with: papers add <folder of PDFs>" % organize.INDEX)
        serve(organize, sys.argv[1:])
