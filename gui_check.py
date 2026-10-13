#!/usr/bin/env python3
"""検索の GUI のサーバ(gui.py)の検査。一時ディレクトリの作り物の蔵書で、ループバックのサーバを立てて叩く。

    python3 gui_check.py
    python3 gui_check.py --dist    # 画面の型検査と、配る JS(gui/dist)がソースから作り直したものと同じこと。node が要る

本物の索引・論文のファイルには触れない。外へは出ない(出ようとしたら落ちる)。OS への命令(open・ブラウザ)は記録役に差し替える。
検査と壊し方の表は GUI_DESIGN.md「検査と壊し方」。捕まえないもの: 画面の操作、Tauri の殻、ブラウザごとの違い。
"""
import contextlib, glob, http.client, io, json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse

import gui
import organize as O

TSC = ["npx", "-y", "-p", "typescript@5.9.3", "tsc"]      # 画面を作る道具と版。上げるときは、作り直した gui/dist の差分を見てから
PAGES_A = ["Laser alpha study. The laser is on page one.", "Page two has gamma and a second \x01 laser mention."]


def write(path, lines):
    with open(path, "w") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines))


class Env:
    """作り物の蔵書とサーバ。
    a: 同定済み(Crossref。キャッシュに姓の欄がある。全頁の本文 2 頁)   b: arXiv 由来。ファイルが無い(全頁の本文なし)
    c: .pdf でない                                                      d: a の重複として移した記録
    e: 未同定(本物と同じく source は None)。語は全頁の本文にだけある    g: Crossref 由来だがキャッシュが無い
    out.pdf は実在するが索引に無い。"""

    def __init__(self):
        self.d = d = tempfile.mkdtemp(prefix="gui-check-")
        p = lambda name: os.path.join(d, name)
        O.ROOT, O.INDEX, O.DB, O.FULLTEXT, O.CACHE = d, p("papers.jsonl"), p("papers.db"), p("fulltext"), p(".cache")
        O._embed = lambda texts: [None] * len(texts)              # ベクトルは作らない(Swift が要る)
        self.net = []                                             # 外へ出ようとした呼び出し。最後に空であることを見る
        O.urllib.request.urlopen = lambda *a, **k: self.net.append(a) or (_ for _ in ()).throw(RuntimeError("network"))
        for name in ("a.pdf", "c.txt", "d.pdf", "e.pdf", "g.pdf", "out.pdf", "secret.txt"):
            open(p(name), "w").write(name)                        # 中身を変える: 全頁の本文の鍵はファイルの中身から作る
        os.makedirs(p("static/dist"))
        open(p("static/index.html"), "w").write("<!doctype html><title>t</title>")
        open(p("static/style.css"), "w").write("body{}")
        open(p("static/dist/app.js"), "w").write("export {};")
        open(p("static/dist/notes.txt"), "w").write("not served")
        self.a, self.b, self.c, self.dup, self.e, self.g, self.out = (
            p(n) for n in ("a.pdf", "b.pdf", "c.txt", "d.pdf", "e.pdf", "g.pdf", "out.pdf"))
        rec = lambda path, **kw: dict({"path": path, "folder": d, "filename": os.path.basename(path)}, **kw)
        write(O.INDEX, [
            rec(self.a, rung="text_doi", source="crossref", year=2001, first_author="Example", authors=["Example Ann", "de Souza Bob"],
                title="Laser alpha study", journal="Journal of Tests", journal_short="J. Tests", volume="12", pages="34",
                doi="10.1/a<b>c#d", published_doi="10.2/pub", abstract="An abstract about beta.", text="Laser alpha study first page"),
            rec(self.b, rung="arxiv_text", source="arxiv", arxiv_id="cond-mat/0701234", year=2007, first_author="Example",
                authors=["Carol Example"], title="Gamma rays", journal="arXiv"),
            rec(self.c, rung="unresolved", source=None, families=["Stale"]),     # 姓の並びは、DOI で引き直すと捨てられる
            rec(self.dup, rung="text_doi", source="crossref", year=2001, first_author="Example", title="Laser alpha study",
                duplicate_of=self.a),
            rec(self.e, rung="unresolved", source=None, title_guess="Guessed title"),    # 本物の未同定は source を None で持つ
            rec(self.g, rung="bib_query", source="crossref", year=2010, first_author="van Dyke", authors=["van Dyke Carl"],
                title="Unrelated topic", journal="Journal of Tests", doi="10.3/g"),
        ])
        for path, pages in ((self.a, PAGES_A), (self.e, ["Only deep inside: laser here."]), (self.g, ["nothing relevant on this page"])):
            O._fulltext_save(O._file_key(path), "pdftotext", pages, False)
        # 外への道の作り物(_outside)。a の Crossref の応答を、本物の読み書きの道でキャッシュに置く。この後、サーバが外への道を断ち、
        # 作り物を呼ぶのは直す欄の DOI を引くときだけになる。呼ばれた URL は self.fetched に残る
        self.fetched = []
        O._fetch = self._outside
        O.get_json("https://api.crossref.org/works/" + urllib.parse.quote("10.1/a<b>c#d") + "?mailto=" + O.MAILTO)
        del self.fetched[:]
        with contextlib.redirect_stderr(io.StringIO()):
            O.build_db().close()
        # 2 つ目の DB(別のフォルダ)。本物と同じ道(PAPERS_HOME で organize.py をもう 1 つ読み込む)で開く
        self.d2 = d2 = tempfile.mkdtemp(prefix="gui-check-db2-")
        self.O2 = O2 = O.other_db(d2)
        O2._embed = O._embed
        self.x = os.path.join(d2, "x.pdf")
        open(self.x, "w").write("x.pdf")
        write(O2.INDEX, [{"path": self.x, "folder": d2, "filename": "x.pdf", "rung": "manual", "source": "manual", "year": 1990,
                          "first_author": "Booker", "title": "Laser handbook", "kind": "book", "not_paper": "user"}])
        with contextlib.redirect_stderr(io.StringIO()):
            O2.build_db().close()
        self.ran, self.opened = [], []
        self.httpd, self.app = gui.make_server(O, 0, None, p("static"), self.ran.append, self.opened.append, more=[("Books", O2)])
        for a in self.httpd.RequestHandlerClass.apps.values():
            a.quiet, a.can_open = True, True
        self.app.quiet, self.app.can_open = True, True
        self.port = self.app.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _outside(self, url, *a, **k):
        """外の相手の作り物(Crossref・doi.org・DataCite・JaLC・arXiv)。呼ばれた URL は self.fetched に残る。10.5555/ の後ろで答えが決まる:
        found は Crossref に雑誌の論文がある、book は Crossref にあるが誌名が無い、down は Crossref に届かない(状態の番号が無い失敗)、
        ほかは Crossref に記録が無い(404)。そのとき doi.org が答える登録機関は jalc → JaLC、datacite → DataCite、istic → ISTIC、
        radown → doi.org に届かない、ほかは「その DOI は無い」。"""
        self.fetched.append(url)
        err = RuntimeError("no answer")
        base, name = url.split("?")[0].rsplit("/", 1)
        if "openlibrary.org/api/books?bibkeys=ISBN:" in url or "isbn:" in url or "opensearch?isbn=" in url:
            return self._catalogue(url)
        if url == "http://export.arxiv.org/api/query?id_list=2101.00001":
            return ARXIV_ATOM.encode()
        if urllib.parse.quote("10.1/a<b>c#d") in url:
            return json.dumps({"message": {"author": [{"family": "Example", "given": "Ann"}, {"family": "de Souza", "given": "Bob"}]}}).encode()
        if base == "https://api.crossref.org/works/10.5555" and name in ("found", "book"):
            return json.dumps({"message": dict({
                "DOI": "10.5555/" + name, "title": ["Found by its DOI"], "issued": {"date-parts": [[2001, 5]]}, "volume": "7", "page": "11-19",
                "author": [{"family": "Finder", "given": "Fay"}, {"family": "Seeker", "given": "Sam"}]},
                **({"container-title": ["Journal of Lookups"], "short-container-title": ["J. Lookups"]} if name == "found" else {}))}).encode()
        if base == "https://doi.org/ra/10.5555" and name != "radown":
            ra = {"jalc": "JaLC", "datacite": "DataCite", "istic": "ISTIC", "book": "Crossref"}.get(name)
            return json.dumps([dict({"DOI": "10.5555/" + name}, **({"RA": ra} if ra else {"status": "DOI does not exist"}))]).encode()
        if (base, name) == ("https://data.crosscite.org/application/vnd.citationstyles.csl+json/10.5555", "datacite"):
            return json.dumps({"DOI": "10.5555/DATACITE", "title": "和文の表題", "container-title": "試験の雑誌", "volume": "55", "page": "42-46",
                               "issued": {"date-parts": [[2012]]}, "author": [{"family": "試験", "given": "太郎"}, {"family": "検査", "given": "花子"}]}).encode()
        if (base, name) == ("https://api.japanlinkcenter.org/dois/10.5555", "jalc"):       # JaLC は英語と和文の欄を持つ
            who = lambda n, ja, en: {"sequence": n, "names": [dict(zip(("lang", "last_name", "first_name"), v)) for v in (("ja",) + ja, ("en",) + en)]}
            return json.dumps({"status": "OK", "data": {
                "doi": "10.5555/jalc", "title_list": [{"lang": "ja", "title": "和文の表題"}, {"lang": "en", "title": "An English Title"}],
                "creator_list": [who("1", ("試験", "太郎"), ("SHIKEN", "Taro")), who("2", ("検査", "花子"), ("KENSA", "Hanako"))],
                "journal_title_name_list": [{"journal_title_name": "Journal of Tests", "type": "full", "lang": "en"}],
                "volume": "55", "first_page": "42", "last_page": "46", "publication_date": {"publication_year": "2012"}}}).encode()
        if not (name == "down" or (name == "radown" and base.startswith("https://doi.org/"))):
            err.code = 404
        raise err

    def _catalogue(self, url):
        """本の目録の作り物(国立国会図書館・Crossref・Open Library)。9790000000001 は Open Library にだけある。9780000000019 は、どの目録にも
        届かない。ほかの ISBN は、どの目録にも無い。"""
        i = re.search(r'(?:ISBN:|isbn:|isbn=)([0-9X]+)', url).group(1)
        if i in ("9780000000019", "0000000019"):
            raise RuntimeError("no answer")
        if "ndlsearch.ndl.go.jp" in url:
            return b"<rss><channel></channel></rss>"
        if "api.crossref.org" in url:
            return json.dumps({"message": {"items": []}}).encode()
        return json.dumps({"ISBN:" + i: {"title": "A Made-up Book", "authors": [{"name": "Ann Bookwriter"}, {"name": "Bo Coauthor"}],
                                         "publish_date": "March 2001", "publishers": [{"name": "Imaginary Press"}]}} if i == "9790000000001" else {}).encode()

    def child_env(self):
        """papers gui を別の過程で立てるときの環境。最初の DB を見せる。INDEX(索引の場所だけを変える)は渡さない。"""
        return dict({k: v for k, v in os.environ.items() if k != "INDEX"}, PAPERS_HOME=self.d)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.d)
        shutil.rmtree(self.d2)

    def req(self, method, path, body=None, token=True, host=None, ctype="application/json", raw=None):
        """(状態, ヘッダの辞書, 本文の JSON か None)。token=True は正しい合言葉、False は無し、文字列はその値。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or "127.0.0.1:%d" % self.port)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        if method == "POST":
            if ctype:
                c.putheader("Content-Type", ctype)
            c.putheader("Content-Length", str(len(data)))
        if token is not False:
            c.putheader("X-Papers-Token", self.app.token if token is True else token)
        c.endheaders()
        if data:
            c.send(data)
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        return r.status, {k.lower(): v for k, v in r.getheaders()}, obj

    def raw_post(self, path, body):
        """(状態, ヘッダの辞書, 本文のバイト列)。絵を返す API 用。"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json", "X-Papers-Token": self.app.token})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def ok(self, path, body):
        """200 で返り、応答が API の形に合い、null を含まないことを見て、応答を返す。"""
        st, _, obj = self.req("POST", path, body)
        assert st == 200, (path, body, st, obj)
        err = gui.check(gui.API[path]["res"], obj)
        assert err is None and "null" not in json.dumps(obj), "%s の応答が表と違う: %s %s" % (path, err, obj)
        return obj

    def raw(self, data, wait=5):
        """ソケットにそのまま書き、返ってきた文字列を返す。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        s.sendall(data)
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
        s.close()
        return out.decode("latin-1")


def check_entry(e):
    good = "127.0.0.1:%d" % e.port
    e.ok("/api/info", {})
    for t in (False, "", "wrong", e.app.token + "x", "é"):
        st, _, obj = e.req("POST", "/api/info", {}, token=t)
        assert st == 401 and "records" not in (obj or {}), "合言葉 %r で %s" % (t, st)
    print("ok: 合言葉なし・違う合言葉は 401")
    for h in ("localhost:%d" % e.port, "evil.test:%d" % e.port, "127.0.0.1", "127.0.0.1:%d" % (e.port + 1), "[::1]:%d" % e.port):
        assert e.req("POST", "/api/info", {}, host=h)[0] == 403, "Host %s の POST が通る" % h
        assert e.req("GET", "/", host=h)[0] == 403, "Host %s の GET が通る" % h
    assert e.req("GET", "/", host=good)[0] == 200
    print("ok: Host が 127.0.0.1:番号 でなければ 403(localhost も)")
    for path in gui.API:
        st, _, obj = e.req("GET", path)
        assert st == 405 and set(obj or {}) == {"error"}, "GET %s が %s" % (path, st)
    assert set(gui.API) == set(gui.HANDLERS), "API の表と処理の一覧が合わない"
    print("ok: API は GET で動かない(%d 本)" % len(gui.API))
    assert e.httpd.server_address[0] == "127.0.0.1", e.httpd.server_address
    print("ok: 待ち受けは 127.0.0.1")
    assert set(e.app.static) == {"/", "/style.css", "/dist/app.js"}, e.app.static
    for path in e.app.static:
        assert e.req("GET", path)[0] == 200, path
    for path in ("/papers.jsonl", "/../papers.jsonl", "/gui.py", "/dist/notes.txt", "/dist/../../secret.txt", "/secret.txt",
                 "/index.html", "/?x=1", "/dist/", "/%2e%2e/secret.txt", e.a):
        assert e.req("GET", path)[0] == 404, "GET %s が 404 でない" % path
    assert e.req("GET", "//")[0] in (200, 404)      # Python が // を / に直す版では画面が返る(蔵書の情報は無い)
    print("ok: GET は一覧の 3 つだけ")
    # 長さだけ名乗って本文を送らない。読みに行けば時間切れまで返らない
    head = "POST /api/info HTTP/1.0\r\nHost: %s\r\nX-Papers-Token: %s\r\nContent-Type: application/json\r\n" % (good, e.app.token)
    t0 = time.time()
    out = e.raw((head + "Content-Length: %d\r\n\r\n" % (gui.MAX_BODY + 1)).encode(), wait=3)
    assert out.startswith("HTTP/1.0 413") and time.time() - t0 < 2, out[:60]
    assert e.raw((head + "Content-Length: abc\r\n\r\n").encode(), wait=3).startswith("HTTP/1.0 411")
    assert e.raw((head + "Content-Length: -5\r\n\r\n").encode(), wait=3).startswith("HTTP/1.0 413")
    print("ok: 1 MB を超える長さは読まずに 413")
    idle = socket.create_connection(("127.0.0.1", e.port))      # 何も送らない接続があっても、次の要求に答える
    assert e.req("POST", "/api/info", {})[0] == 200, "何も送らない接続があると止まる"
    idle.close()
    assert e.req("POST", "/api/info", raw=b"{not json")[0] == 400
    assert e.req("POST", "/api/info", raw=b"[1]")[0] == 400
    assert e.req("POST", "/api/info", raw=b"\xff\xfe")[0] == 400
    assert e.req("POST", "/api/info", {}, ctype="text/plain")[0] == 415
    assert e.req("POST", "/api/info", {}, ctype=None)[0] == 415
    assert e.req("POST", "/api/nope", {})[0] == 404
    assert e.req("POST", "/api/info", {"q": "x"})[0] == 400, "表に無い項目を送った要求が通る"
    assert e.req("POST", "/api/search", {"q": 1, "all": True})[0] == 400 and e.req("POST", "/api/search", {"q": "x"})[0] == 400
    print("ok: 本文が JSON の辞書でない・表に無い項目・型の違い・知らない道は断る")


def check_headers(e):
    """全応答(基底クラスが自分で返す 501・431 を含む)にヘッダがあり、CORS のヘッダが無い。"""
    good = ("127.0.0.1:%d" % e.port).encode()
    seen = []
    for method, path, kw in [("GET", "/", {}), ("GET", "/nope", {}), ("GET", "/api/info", {}), ("GET", "/", {"host": "x"}),
                             ("POST", "/api/info", {"body": {}}), ("POST", "/api/info", {"body": {}, "token": False}),
                             ("POST", "/api/open", {"body": {"path": "/nope.pdf"}}), ("POST", "/api/info", {"raw": b"{"})]:
        st, h, _ = e.req(method, path, **kw)
        seen.append((st, h))
    for data in (b"PUT / HTTP/1.0\r\nHost: %s\r\n\r\n" % good, b"OPTIONS /api/info HTTP/1.0\r\nHost: %s\r\n"
                 b"Origin: http://evil.test\r\nAccess-Control-Request-Method: POST\r\n\r\n" % good,
                 b"GET / HTTP/1.0\r\nHost: %s\r\nX: %s\r\n\r\n" % (good, b"a" * 70000)):
        head = e.raw(data, wait=3).split("\r\n\r\n")[0].split("\r\n")
        seen.append((int(head[0].split()[1]), {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in head[1:] if ":" in l}))
    # 版の無い壊れた要求行には、基底クラスが状態の行もヘッダも付けずに本文だけを返す。番号だけで、要求の文字列を写さない
    assert e.raw(b"garbage<script>\r\n\r\n", wait=3) == "400\n", "壊れた要求行への本文が番号だけでない"
    assert e.raw(b"GET / HTTP/9.9\r\n\r\n", wait=3) == "505\n"
    codes = sorted({st for st, _ in seen})
    assert {200, 400, 401, 403, 404, 405, 431, 501} <= set(codes), codes
    for st, h in seen:
        for k, v in gui.HEADERS:
            assert h.get(k.lower()) == v, "状態 %s の応答に %s が無い" % (st, k)
        assert not [k for k in h if k.startswith("access-control-")], "CORS のヘッダがある: %s" % h
        assert "python" not in h.get("server", "").lower(), h.get("server")
    print("ok: %d 種類の状態(%s)すべてに CSP・nosniff・no-store。CORS のヘッダなし" % (len(codes), "・".join(map(str, codes))))


def check_open(e):
    for api, argv in (("/api/open", ["open", e.a]), ("/api/reveal", ["open", "-R", e.a])):
        del e.ran[:]
        assert (e.ok(api, {"path": e.a}), e.ran) == ({"ok": True}, [argv]), e.ran
        # 1 つの条件だけが外れる例で見る: 索引に無い(実在する .pdf) / .pdf でない(索引にあり実在する) / 実在しない(索引にある .pdf)
        for what, path, want in (("索引に無い", e.out, 404), (".pdf でない", e.c, 400), ("実在しない", e.b, 404)):
            del e.ran[:]
            st = e.req("POST", api, {"path": path})[0]
            assert st == want and e.ran == [], "%s: %s %s が %s、呼び出し %s" % (what, api, os.path.basename(path), st, e.ran)
        for body in ({}, {"path": 1}, {"path": None}, {"path": e.a, "extra": 1}, {"path": "a.pdf"}, {"path": "-R"}):
            del e.ran[:]
            assert e.req("POST", api, body)[0] in (400, 404) and e.ran == [], (api, body)
    e.app.can_open = False
    assert e.req("POST", "/api/open", {"path": e.a})[0] == 501 and e.ran == []
    e.app.can_open = True
    print("ok: 開くのは、索引にある・.pdf で終わる・実在する、の 3 つを満たすパスだけ")


def check_links(e):
    """外へ開く URL は、記録から作った 3 種類だけ。画面から URL を受け取らない。"""
    want = {(e.a, "doi"): "https://doi.org/10.1/a%3Cb%3Ec%23d", (e.a, "published_doi"): "https://doi.org/10.2/pub",
            (e.a, "scholar"): "https://scholar.google.com/scholar?q=Laser%20alpha%20study",
            (e.b, "arxiv"): "https://arxiv.org/abs/cond-mat/0701234"}
    for (path, link), url in want.items():
        del e.opened[:]
        assert (e.ok("/api/open-url", {"path": path, "link": link}), e.opened) == ({"ok": True}, [url]), (link, e.opened)
    for path, link in ((e.e, "scholar"), (e.e, "doi"), (e.b, "doi"), (e.a, "arxiv"), (e.out, "doi"), (e.dup, "doi"),
                       (e.a, "https://evil.test/"), (e.a, "javascript:alert(1)"), (e.a, "")):
        del e.opened[:]
        assert e.req("POST", "/api/open-url", {"path": path, "link": link})[0] == 404 and e.opened == [], (path, link)
    del e.opened[:]
    assert e.req("POST", "/api/open-url", {"path": e.a, "link": "doi", "url": "https://evil.test/"})[0] == 400 and e.opened == []
    for r in O.load_index()[0]:
        for link in gui.LINKS + ("x",):
            u = gui.link_url(r, link)
            assert u is None or any(u.startswith(p) for p in gui.URL_PREFIX.values()), u
    print("ok: 外へ開く URL は記録から作った %d 種類の先頭だけ(表題の無い記録に検索のリンクは無い)" % len(gui.URL_PREFIX))


def check_search(e):
    find = lambda q, all=True, **kw: e.ok("/api/search", dict({"q": q, "all": all}, **kw))
    paths = lambda r: [x["path"] for x in r["rows"]]
    r = find("laser")
    assert (paths(r), r["total"], r["near"]) == ([e.a, e.e], 2, 1), "先頭側で当たる記録が先、全頁の本文だけの記録が後ろ、移した重複は出ない: %s" % r
    assert [x["only_full"] for x in r["rows"]] == [False, True] and r["rows"][1]["guess"] == "Guessed title" and "title" not in r["rows"][1]
    assert r["rows"][0] == {"path": e.a, "title": "Laser alpha study", "filename": "a.pdf", "journal": "J. Tests", "year": 2001,
                            "volume": "12", "pages": "34", "state": "resolved", "kind": "paper", "first_author": "Example",
                            "n_authors": 2, "only_full": False}, r["rows"][0]
    print("ok: 一覧の並びと件数(移した重複は出ない。全頁の本文だけで当たる記録は後ろ)")
    r = find("alpha gamma")                       # alpha は表題、gamma は 2 頁目にだけある
    assert (paths(r), r["near"]) == ([e.a], 0), r
    assert set(paths(find("alpha gamma", False))) == {e.a, e.b}
    assert gui.query_for(O, "alpha gamma", True) == '"alpha" AND "gamma"' and gui.query_for(O, "alpha gamma", False) == "alpha gamma"
    assert gui.query_for(O, "alpha OR gamma", True) == "alpha OR gamma" and gui.query_for(O, "Si 基板", True) == "Si 基板"
    print("ok: 「すべての語」は AND、「いずれかの語」は OR。式はそのまま")
    assert set(paths(find("author:Example"))) == {e.a, e.b} and paths(find('author:" de Souza "')) == [e.a]
    assert e.req("POST", "/api/search", {"q": "laser AND", "all": True})[0] == 400
    r = find("   ")                               # 入力が空なら全部(移した重複を除く)
    assert r["total"] == 5 and "near" not in r and e.dup not in paths(r), r
    r = find("on")                                # 3 文字以上の語が無い入力は部分一致
    assert r["substring"] and "near" not in r and {e.a, e.e, e.g} <= set(paths(r)), r
    assert find("laser of")["unused"] == ["of"] and find("title:ab AND laser")["short"] == ["ab"]
    page, gui.PAGE = gui.PAGE, 1
    try:
        got = [paths(find("laser", offset=i)) for i in range(3)]
    finally:
        gui.PAGE = page
    assert got == [[e.a], [e.e], []] and find("laser", offset=1)["total"] == 2, got
    print("ok: 式・部分一致・注意・続きの取得。件数は全部の行を足した数")
    # 絞り込み: laser は a(2001・J. Tests・同定済み)と e(年も誌名も無い・未同定)に当たる
    counts = lambda r, k: {x["v"]: x["n"] for x in r["facets"][k]}
    r = find("laser")
    assert (counts(r, "years"), counts(r, "journals"), counts(r, "states"), counts(r, "kinds")) == (
        {2001: 1}, {"J. Tests": 1}, {"resolved": 1, "unresolved": 1}, {"paper": 2}), r["facets"]
    assert paths(find("laser", year_from=2000, year_to=2005)) == [e.a] and find("laser", year_from=2002)["total"] == 0
    assert paths(find("laser", year_to=2001)) == [e.a] and find("laser", year_to=2000)["total"] == 0
    assert paths(find("laser", journal="J. Tests")) == [e.a]
    assert paths(find("laser", kind="paper")) == [e.a, e.e] and find("laser", kind="book")["total"] == 0
    r = find("laser", state="unresolved")
    assert (paths(r), r["total"], r["near"]) == ([e.e], 1, 0), r
    # 各値の件数は、その項目自身の絞り込みを外して数える。ほかの項目の絞り込みは効く
    assert counts(r, "states") == {"resolved": 1, "unresolved": 1} and counts(r, "years") == {} and counts(r, "kinds") == {"paper": 1}, r["facets"]
    assert counts(find("laser", journal="Nature"), "journals") == {"J. Tests": 1, "Nature": 0}
    print("ok: 絞り込み(年・誌名・状態・種類)と、各値の件数")
    assert paths(find("", sort="newest")) == [e.g, e.b, e.a, e.c, e.e] and paths(find("", sort="oldest")) == [e.a, e.b, e.g, e.c, e.e]
    assert paths(find("", sort="author")) == [e.a, e.b, e.g, e.c, e.e] and paths(find("", sort="relevance")) == [e.a, e.b, e.c, e.e, e.g]
    r = find("laser", sort="oldest")
    assert (paths(r), [x["only_full"] for x in r["rows"]]) == ([e.a, e.e], [False, True])
    assert e.req("POST", "/api/search", {"q": "", "all": True, "sort": "random"})[0] == 400
    print("ok: 並べ替え(年・第一著者。無い記録は後ろ)")


def check_record(e):
    rec = lambda path, q="", all=True: e.ok("/api/record", {"path": path, "q": q, "all": all})
    r = rec(e.a, "laser")
    assert [a["q"] for a in r["authors"]] == ['author:" Example "', 'author:" de Souza "'], "姓は出版社の記録の欄から: %s" % r["authors"]
    assert r["cite"].startswith("@article{") and "de Souza, Bob" in r["cite"] and not r["cite_unsure"], r.get("cite")
    assert r["links"] == ["doi", "published_doi", "scholar"] and r["where"] == "." and r["abstract"] == "An abstract about beta."
    assert r["edit"] == {"title": "Laser alpha study", "authors": "Example Ann; de Souza Bob", "first_author": "Example", "year": "2001",
                         "journal": "Journal of Tests"}
    assert rec(e.e)["edit"] == {}
    text = lambda h: "".join(p["text"] for p in h["parts"])
    clean = [gui._CTRL.sub("", p) for p in PAGES_A]
    assert [h["page"] for h in r["hits"]] == [1, 2] and r["hit_total"] == 2, r["hits"]
    assert text(r["hits"][0]) == clean[0] and text(r["hits"][1]) == clean[1], "前後の文章が頁の本文と合わない: %s" % r["hits"]
    # 2 頁目の当たりは、本文に混ざった制御文字(印に使えない字)より後ろにある。印の字を選び損ねると 1 字ずれる
    assert [p["text"] for h in r["hits"] for p in h["parts"] if p["hit"]] == ["Laser", "laser", "laser"], r["hits"]
    assert not gui._CTRL.search(json.dumps(r["hits"], ensure_ascii=False)) and "\x01" in PAGES_A[1]
    print("ok: 全頁の出現箇所(頁番号・当たりの印・制御文字を出さない)")
    assert [h["page"] for h in rec(e.a, "alpha gamma")["hits"]] == [1, 2] and rec(e.a, "alpha gamma", False)["hit_total"] == 2
    assert [h["page"] for h in rec(e.a, "on")["hits"]] == [1, 2]                  # 部分一致: "on" は 1 頁目に 2 つ、2 頁目に 1 つ
    r = rec(e.e, "laser")
    assert r["hit_total"] == 1 and "cite" not in r and r["authors"] == [] and r["state"] == "unresolved" and r["links"] == []
    r = rec(e.b, "gamma")
    assert r["hits_note"] == "no_fulltext" and r["authors"] == [{"name": "Carol Example", "q": 'author:" Example "'}]
    assert r["cite"].startswith("@misc{") and not r["cite_unsure"] and r["links"] == ["arxiv", "scholar"]
    r = rec(e.g, "author:Dyke")
    assert r["hits"] == [] and r["hits_note"] == "none_in_text" and r["authors"][0]["q"] == 'author:" van "' and r["cite_unsure"], r
    assert "hits_note" not in rec(e.a) and rec(e.a)["hits"] == []
    # 目録で同定した本: 姓は記録の families から(著者の並びは目録ごとに「姓 名」「名 姓」が混ざる)。並びが著者と合わなければ使わない
    book = {"path": "/x/book.pdf", "source": "ndl", "authors": ["Tester Ann", "Bob Sample"], "families": ["Tester", "Sample"]}
    qs = lambda r: [a["q"] for a in gui.people(e.app, r)[0]]
    assert qs(book) == ['author:" Tester "', 'author:" Sample "'], "本の著者の姓を families から取っていない: %s" % qs(book)
    assert qs(dict(book, families=["Tester"])) == ['author:" Ann "', 'author:" Sample "'], "並びの合わない families を使った"
    assert qs(dict(book, families=["", "Sample"])) == ['author:" Ann "', 'author:" Sample "'], "空の姓で引く式を作った"
    assert qs(dict(book, authors=["Tester Ann", "Carl Other"])) == ['author:" Tester "', 'author:" Other "'], "著者を直す前の姓で引く式を作った"
    for path in (e.dup, e.out):
        assert e.req("POST", "/api/record", {"path": path, "q": "", "all": True})[0] == 404
    assert e.req("POST", "/api/record", {"path": e.a, "q": "laser AND", "all": True})[0] == 400
    print("ok: 著者の姓(キャッシュの欄 / 位置)、引用、リンクの有無、全頁の本文が無い・本文に当たりが無いときの知らせ")


def check_page(e):
    """頁の絵: 索引にある .pdf の、ある頁だけ。絵を作る命令は差し替え、本物(pdftoppm)は入っていれば小さな PDF で試す。"""
    seen = []
    e.app.render = lambda path, page: (seen.append((path, page)), b"\xff\xd8page%d" % page if page <= 2 else b"")[1]
    st, h, data = e.raw_post("/api/page", {"path": e.a, "page": 2})
    assert (st, data, h["content-type"], seen) == (200, b"\xff\xd8page2", "image/jpeg", [(e.a, 2)]) and h.get("content-security-policy")
    for body, want in (({"path": e.a, "page": 3}, 404), ({"path": e.a, "page": 0}, 400), ({"path": e.out, "page": 1}, 404),
                       ({"path": e.c, "page": 1}, 400), ({"path": e.b, "page": 1}, 404), ({"path": e.a, "page": "1"}, 400)):
        del seen[:]
        assert e.raw_post("/api/page", body)[0] == want and (want == 404 and body["path"] == e.a or seen == []), (body, seen)
    for exc, want in ((FileNotFoundError("pdftoppm"), 501), (subprocess.TimeoutExpired("pdftoppm", 30), 504)):
        def boom(path, page, exc=exc):
            raise exc
        e.app.render = boom
        assert e.raw_post("/api/page", {"path": e.a, "page": 1})[0] == want
    e.app.render = gui._render
    if not shutil.which("pdftoppm"):
        print("ok: 頁の絵(索引にある .pdf の、ある頁だけ)。not checked: pdftoppm is not installed, so real rendering was not tried")
        return
    objs, out, offs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
                       b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>"], b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    out += (b"xref\n0 4\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % o for o in offs)
            + b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % len(out))
    keep = open(e.a, "rb").read()
    open(e.a, "wb").write(out)                    # a.pdf を、1 頁だけの本物の PDF にする
    try:
        st, _, data = e.raw_post("/api/page", {"path": e.a, "page": 1})
        assert st == 200 and data[:3] == b"\xff\xd8\xff", (st, data[:8])
        assert e.raw_post("/api/page", {"path": e.a, "page": 2})[0] == 404
    finally:
        open(e.a, "wb").write(keep)
    print("ok: 頁の絵(索引にある .pdf の、ある頁だけ。pdftoppm で本物の PDF も)")


def check_cite(e):
    r = e.ok("/api/cite", {"paths": [e.a, e.e, e.b, e.out, e.dup]})     # 作れるのは a と b
    assert (r["made"], r["skipped"]) == (2, 3) and r["text"].count("@article{") == 1 and r["text"].count("@misc{") == 1, r
    assert r["text"].index("@article{") < r["text"].index("@misc{") and "de Souza, Bob" in r["text"]
    assert e.ok("/api/cite", {"paths": []}) == {"text": "", "made": 0, "skipped": 0}
    print("ok: 選んだ記録の引用をまとめる(作れない記録は数だけ)")


def check_fix(e):
    """直す: apply でなければ書かない。書くときは papers fix と同じ道(退避してから)。直せるのは決まった項目だけ。"""
    assert set(gui.EDIT_KEYS) <= set(O.FIX_KEYS)
    backups = lambda: sorted(glob.glob(os.path.join(e.d, "backups", "*-before-fix.jsonl")))
    before = open(O.INDEX, "rb").read()
    fix = lambda path, apply, **kw: e.ok("/api/fix", {"path": path, "set": kw, "apply": apply})
    r = fix(e.e, False, title="  A new\n title ", year="1999")
    assert not r["applied"] and any("title" in l and "A new title" in l for l in r["lines"]) and any("1999" in l for l in r["lines"]), r
    assert open(O.INDEX, "rb").read() == before and backups() == [], "apply でないのに書いた"
    assert fix(e.a, True, title="Laser alpha study", year="2001") == {"lines": [], "applied": False}      # 変わっていない
    assert open(O.INDEX, "rb").read() == before and backups() == []
    for body, want in (({"path": e.e, "set": {"rung": "manual"}, "apply": True}, 400), ({"path": e.e, "set": {"path": "/x"}, "apply": True}, 400),
                       ({"path": e.e, "set": {"year": "19x9"}, "apply": True}, 400), ({"path": e.e, "set": {"title": "x" * 1001}, "apply": True}, 400),
                       ({"path": e.out, "set": {"title": "x"}, "apply": True}, 404), ({"path": e.dup, "set": {"title": "x"}, "apply": True}, 404),
                       ({"path": e.e, "set": {"title": 1}, "apply": True}, 400), ({"path": e.e, "set": {"title": "x"}}, 400)):
        assert e.req("POST", "/api/fix", body)[0] == want, body
    assert open(O.INDEX, "rb").read() == before and backups() == [], "断った要求で書いた"
    real = O.fix_apply
    O.fix_apply = lambda *a: (_ for _ in ()).throw(SystemExit("changed under us"))
    try:
        st, _, obj = e.req("POST", "/api/fix", {"path": e.e, "set": {"title": "x"}, "apply": True})
        assert st == 409 and "changed under us" in obj["error"], (st, obj)
    finally:
        O.fix_apply = real
    old = {r["path"]: r for r in O.load_index()[0]}
    r = fix(e.e, False, authors=" Ann  B. Tester ;Bo Young; ")
    assert any("authors" in l and "2: Ann B. Tester; Bo Young" in l for l in r["lines"]), "何人と読んだかが、違いの行に出ない: %s" % r["lines"]
    assert any("first_author" in l and l.endswith("Tester") for l in r["lines"]), "第一著者の姓を、先頭の著者から入れていない: %s" % r["lines"]
    r = fix(e.e, True, title="  A new\n title ", year="1999", authors=" Ann  B. Tester ;Bo Young; ")
    new = {r["path"]: r for r in O.load_index()[0]}
    assert r["applied"] and len(backups()) == 1 and open(backups()[0], "rb").read() == before, "退避が無い・退避の中身が違う"
    assert (new[e.e]["title"], new[e.e]["year"], new[e.e]["first_author"], new[e.e]["authors"], new[e.e]["rung"], new[e.e]["fixed_by"]) == (
        "A new title", 1999, "Tester", ["Ann B. Tester", "Bo Young"], "manual", "user"), new[e.e]
    assert all(new[p] == old[p] for p in old if p != e.e) and set(new) == set(old), "ほかの記録が変わった"
    rec = e.ok("/api/record", {"path": e.e, "q": "", "all": True})
    assert (rec["title"], rec["year"], rec["state"]) == ("A new title", 1999, "resolved") and e.ok("/api/info", {})["stale"], \
        "直した未同定の記録が、同定済みにならない: %s" % rec["state"]
    assert rec["cite"].startswith("@misc{Tester1999") and "Ann B. Tester and Bo Young" in rec["cite"], "直した記録から引用を作れない: %s" % rec.get("cite")
    assert rec["edit"]["authors"] == "Ann B. Tester; Bo Young" and [a["q"] for a in rec["authors"]] == ['author:" Tester "', 'author:" Young "'], rec
    assert fix(e.e, True, authors="Ann B. Tester ;  Bo Young") == {"lines": [], "applied": False} and len(backups()) == 1, "区切りの空白だけが違う著者を、変更として書いた"
    r = fix(e.e, True, title="")                  # 空にすると、その項目を消す
    assert r["applied"] and "title" not in e.ok("/api/record", {"path": e.e, "q": "", "all": True}) and len(backups()) == 2
    print("ok: 直す(既定は書かない、決まった項目だけ、退避してから書く、ほかの記録は変えない)")


ARXIV_ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Preprint by its DOI</title><summary>An abstract.</summary>
<published>2021-01-01T00:00:00Z</published><author><name>Pat Reader</name></author></entry></feed>"""


def check_fix_doi(e):
    """直す欄の DOI: 引く相手は、その DOI のための決まった宛先だけ(O.doi_urls)。DOI の形でなければ引かずに断る。
    Crossref に無ければ、doi.org に登録機関を聞いて、その機関から引く。引けない理由を分けて言う。"""
    url = O._no_empty_mailto("https://api.crossref.org/works/10.5555/found?mailto=" + O.MAILTO)
    backups = lambda: sorted(glob.glob(os.path.join(e.d, "backups", "*-before-fix.jsonl")))
    n_backups, before = len(backups()), open(O.INDEX, "rb").read()
    fix = lambda apply, doi, **kw: e.req("POST", "/api/fix", {"path": e.c, "set": kw, "apply": apply, "lookup": doi})
    for bad in ("not a doi", "https://example.org/10.5555/found", "10.5555/found two", "10.5555/" + "x" * 300):
        assert fix(False, bad)[0] == 400 and e.fetched == [], "DOI の形でない文字列で外へ出た・通した: %r %s" % (bad, e.fetched)
    st, _, obj = fix(False, "10.5555/found")
    assert st == 200 and e.fetched == [url], "引く相手が、その DOI の Crossref の記録 1 つでない: %s %s" % (obj, e.fetched)
    lines = "\n".join(obj["lines"])
    assert "Found by its DOI" in lines and "2: Finder Fay; Seeker Sam" in lines and "crossref" in lines and not obj["applied"], lines
    assert open(O.INDEX, "rb").read() == before and len(backups()) == n_backups, "apply でないのに書いた"
    # 貼り付けた URL の形でも同じ DOI。2 回目はキャッシュから。書き換えた項目だけが、引いた値の上に入る
    st, _, obj = fix(True, " https://doi.org/10.5555/found ", year="1999")
    new = {r["path"]: r for r in O.load_index()[0]}[e.c]
    assert st == 200 and obj["applied"] and e.fetched == [url] and len(backups()) == n_backups + 1, (st, obj, e.fetched)
    assert (new["doi"], new["title"], new["first_author"], new["journal"], new["year"], new["source"], new["rung"]) == (
        "10.5555/found", "Found by its DOI", "Finder", "Journal of Lookups", 1999, "crossref", "manual"), new
    assert "families" not in new and "families" in lines, "引き直す前の著者の姓の並びが残った・違いに出ない: %s" % new
    after = open(O.INDEX, "rb").read()
    st, _, obj = fix(True, "10.5555/down")
    assert st == 502 and "could not reach" in obj["error"], "届かなかったことを言わない: %s %s" % (st, obj)
    st, _, obj = fix(True, "10.5555/absent")
    assert st == 404 and "does not exist" in obj["error"], "その DOI が無いことを言わない(前の失敗を引きずっている?): %s %s" % (st, obj)
    # Crossref に無い DOI: doi.org に登録機関を聞き、その機関の決まった宛先から引く。著者は「姓 名」で、姓の並びが付く。JaLC は英語の欄を採る
    ra, q = "https://doi.org/ra/10.5555/", "?mailto=" + O.MAILTO
    for name, at, title, who in (("jalc", "https://api.japanlinkcenter.org/dois/10.5555/jalc", "An English Title", "2: SHIKEN Taro; KENSA Hanako"),
                                 ("datacite", "https://data.crosscite.org/application/vnd.citationstyles.csl+json/10.5555/datacite",
                                  "和文の表題", "2: 試験 太郎; 検査 花子")):
        del e.fetched[:]
        st, _, obj = fix(False, "10.5555/" + name)
        lines = "\n".join(obj.get("lines") or [])
        assert st == 200 and title in lines and who in lines and name in lines and "families" in lines, (st, obj)
        assert e.fetched == [O._no_empty_mailto("https://api.crossref.org/works/10.5555/%s%s" % (name, q)), ra + name, at], \
            "登録機関の宛先が違う・余計に出た: %s" % e.fetched
    for name, st_, want, n in (("istic", 404, "registered with ISTIC", 2), ("book", 404, "not a journal article", 2),
                               ("radown", 502, "could not ask doi.org", None)):
        del e.fetched[:]
        st, _, obj = fix(True, "10.5555/" + name)
        assert st == st_ and want in obj["error"] and (n is None or len(e.fetched) == n), "引けない理由: %s %s %s" % (st, obj, e.fetched)
        assert all(u.split("?")[0] in O.doi_urls("10.5555/" + name) for u in e.fetched), e.fetched
    # arXiv の DOI は arXiv から引く(出どころは arxiv、DOI は持たない)。ほかへは問い合わせない
    del e.fetched[:]
    st, _, obj = fix(False, "https://doi.org/10.48550/arXiv.2101.00001v3")
    lines = "\n".join(obj.get("lines") or [])
    assert st == 200 and "Preprint by its DOI" in lines and "arxiv_id" in lines and "2101.00001" in lines, (st, obj)
    assert e.fetched == ["http://export.arxiv.org/api/query?id_list=2101.00001"], e.fetched
    assert open(O.INDEX, "rb").read() == after and len(backups()) == n_backups + 1, "引けなかった・確かめただけなのに書いた"
    # 引いている間も、通すのはその DOI の URL だけ
    seen = []
    cr, urls = "https://api.crossref.org/works/", O.doi_urls("10.5555/found")
    gui._net.allow, gui._net.fetch = urls, lambda u, *a, **k: seen.append(u) or b"{}"
    try:
        for ok in urls + [cr + "10.5555/found?mailto=x"]:
            gui._offline(ok)
        for other in (cr + "10.5555/found2", cr + "10.5555/foun", cr + "10.5555/found/../x", "https://example.org/", cr[:-1] + "?query=10.5555/found",
                      "https://doi.org/10.5555/found", "https://doi.org/ra/10.5555/other", "http://export.arxiv.org/api/query?id_list=2101.00001",
                      "http://api.japanlinkcenter.org/dois/10.5555/found", "https://api.japanlinkcenter.org/dois/10.5555/found/x",
                      "https://japanlinkcenter.org/data/10.5555/found"):
            try:
                gui._offline(other)
                raise AssertionError("DOI を引いている間に、別の URL を通した: %s" % other)
            except gui.Offline:
                pass
    finally:
        gui._net.allow = None
    assert len(seen) == len(urls) + 1 == 5, seen
    gui.api_fix(e.app, {"path": e.c, "set": {}, "apply": False, "lookup": "10.5555/found"})     # 同じスレッドで呼んで、後始末を見る
    assert getattr(gui._net, "allow", None) is None, "DOI を引き終わった後も、外への道が開いたまま"
    print("ok: 直す欄の DOI(その DOI のための宛先だけを通す。Crossref に無ければ登録機関から引く。形でなければ引かない。無い・届かないを分ける。書くのは apply だけ)")


def check_fix_isbn(e):
    """直す欄の ISBN: 引く相手は、その ISBN のための決まった宛先だけ(O.isbn_urls)。検査数字が合わなければ引かずに断る。
    本の記録は、前の記録の論文の項目を空にして上に書く。目録に無い・届かないを分けて言う。"""
    fix = lambda apply, s, **kw: e.req("POST", "/api/fix", {"path": e.g, "set": kw, "apply": apply, "lookup": s})
    flat = lambda i: [u for us in O.isbn_urls(i).values() for u in us]
    before = open(O.INDEX, "rb").read()
    for bad in ("9790000000002", "979-0-00-000000", "ISBN", "97900000000010", "10.5555", "x" * 50):
        del e.fetched[:]
        assert fix(False, bad)[0] == 400 and e.fetched == [], "ISBN でも DOI でもない文字列で外へ出た・通した: %r %s" % (bad, e.fetched)
    del e.fetched[:]
    lines = None
    for n, typed in enumerate(("9790000000001", " ISBN 979-0-00-000000-1 ", "isbn-13: 979 0 00 000000 1")):      # 2 回目からはキャッシュ
        st, _, obj = fix(False, typed)
        assert st == 200 and not obj["applied"] and e.fetched == flat("9790000000001") and len(e.fetched) == 2 and (n == 0 or obj["lines"] == lines), \
            "ISBN の書き方 %r: %s %s" % (typed, obj, e.fetched)
        lines = obj["lines"]
    lines = "\n".join(lines)
    want = ("A Made-up Book", "2: Ann Bookwriter; Bo Coauthor", "2: Bookwriter; Coauthor", "Imaginary Press", "9790000000001", "openlibrary",
            "journal        Journal of Tests → None", "first_author   van Dyke → Bookwriter")
    assert all(w in lines for w in want), "本の記録の違いの行: %s" % lines
    # 和書の番号は国立国会図書館が先。どの目録にも無ければ、全部に聞いてから「無い」と言う
    del e.fetched[:]
    st, _, obj = fix(True, "978-4-00-000000-0")
    assert st == 404 and "not in the catalogues" in obj["error"] and e.fetched == flat("9784000000000") and len(e.fetched) == 4 \
        and "ndlsearch.ndl.go.jp" in e.fetched[0], "目録に無い本: %s %s %s" % (st, obj, e.fetched)
    st, _, obj = fix(True, "9780000000019")
    assert st == 502 and "could not reach the book catalogues" in obj["error"], "目録に届かなかったことを言わない: %s %s" % (st, obj)
    assert open(O.INDEX, "rb").read() == before and getattr(gui._net, "allow", None) is None, "確かめただけ・引けなかったのに書いた・外への道が開いたまま"
    # ISBN を引いている間も、通すのはその ISBN のための URL だけ(別の番号・同じ相手の別の問い合わせは断る)
    urls, seen = flat("9784000000000"), []
    gui._net.allow, gui._net.fetch = urls, lambda u, *a, **k: seen.append(u) or b"{}"
    try:
        for ok in urls:
            gui._offline(ok)
        for other in [u.replace("9784000000000", "9784000000017") for u in urls if "9784000000000" in u] + [urls[0] + "&cnt=500", urls[1].replace("rows=10", "rows=1000"),
                                                                                    "https://openlibrary.org/api/books", urls[-1].replace("https:", "http:")]:
            try:
                gui._offline(other)
                raise AssertionError("ISBN を引いている間に、別の URL を通した: %s" % other)
            except gui.Offline:
                pass
    finally:
        gui._net.allow = None
    assert seen == urls, seen
    print("ok: 直す欄の ISBN(その ISBN の目録の問い合わせだけを通す。検査数字が合わなければ引かない。論文の項目を空にする。無い・届かないを分ける)")


def check_tidy(e):
    """片付ける: 種類を変える・DB から外す・別の DB へ送る。apply でなければ書かない・動かさない。papers の同じ関数を通る。"""
    tidy = lambda path, apply, **kw: e.req("POST", "/api/tidy", dict({"path": path, "apply": apply}, **kw))
    rec = lambda path, db="0": e.req("POST", "/api/record", {"path": path, "q": "", "all": True, "db": db})
    snap = lambda: (open(O.INDEX, "rb").read(), open(e.O2.INDEX, "rb").read(),
                    sorted(os.path.join(dp, f) for top in (e.d, e.d2) for dp, _, fs in os.walk(top) for f in fs if f.endswith((".pdf", ".txt"))))
    before = snap()
    assert rec(e.g)[2]["kind_key"] == "paper" and e.ok("/api/info", {})["kinds"] == list(O.KINDS)
    # 印の無い記録の今の種類は、DB の既定: 書籍の DB(config.json の books)は book。人が付けた印はどちらの DB でもそのまま
    d3 = tempfile.mkdtemp(prefix="gui-check-db3-")
    open(os.path.join(d3, "config.json"), "w").write('{"books": true}')
    O3 = O.other_db(d3)
    marked = [{"kind": "paper"}, {"kind": "bookpart", "not_paper": "user"}, {"kind": "thesis", "not_paper": "user"}, {"kind": "bookpart"}]
    assert O3.BOOKS and not O.BOOKS and [gui._kind_key(O3, r) for r in [{}] + marked] == ["book", "paper", "bookpart", "thesis", "bookpart"] \
        and [gui._kind_key(O, r) for r in [{}] + marked] == ["paper", "paper", "bookpart", "thesis", "paper"], "今の種類が DB の既定・付いた種類と合わない"
    assert all(gui._kind_key(M, r) == ("paper" if M.kind_group(r) == "paper" else r.get("kind") or "book") for M in (O, O3) for r in [{}] + marked)
    # 書籍の DB の一覧は 1 つの順位で並ぶので、「全頁の本文でだけ当たる」に分けない(印も区切りも件数の内訳も出さない)。論文の DB は分ける
    bk = os.path.join(d3, "k.pdf")
    open(bk, "w").write("k.pdf")
    write(O3.INDEX, [{"path": bk, "folder": d3, "filename": "k.pdf", "rung": "unresolved", "source": None, "title_guess": "A handbook"}])
    O3._embed = O._embed
    O3._fulltext_save(O3._file_key(bk), "pdftotext", ["Only deep inside: laser here."], False)
    with contextlib.redirect_stderr(io.StringIO()):
        O3.build_db().close()
    paths, near, _ = gui.find(gui.App(O3, "t", os.path.join(e.d, "static")), "laser", True)
    assert paths == [bk] and near is None, "書籍の DB の一覧を、全頁の本文でだけ当たる記録に分けた: %s %s" % (paths, near)
    assert gui.find(e.app, "laser", True)[1] is not None, "論文の DB の一覧を分けていない"
    shutil.rmtree(d3)
    # 予定を見るだけでは、どちらの DB の索引もファイルも変わらない
    st, _, obj = tidy(e.g, False, kind="book", to="remove")
    lines = "\n".join(obj["lines"])
    assert st == 200 and not obj["applied"] and "book" in lines and O.REMOVED_DIR in lines and "removed" in lines, (st, obj)
    st, _, obj = tidy(e.g, False, to="db:1")
    assert st == 200 and not obj["applied"] and any(e.d2 in l for l in obj["lines"]), "送り先(相手の置き場)が予定に出ない: %s" % obj
    assert tidy(e.g, False) [2] == {"lines": [], "applied": False} and tidy(e.g, True, kind="paper")[2] == {"lines": [], "applied": False}
    for kw, want in (({"kind": "novel"}, 400), ({"to": "trash"}, 400), ({"to": "db:9"}, 404), ({"to": "db:0"}, 404), ({"kind": 3}, 400)):
        assert tidy(e.g, True, **kw)[0] == want, kw
    assert tidy(e.dup, True, kind="book")[0] == 404 and tidy(e.out, True, to="remove")[0] == 404, "移した重複・索引に無いパスを片付けた"
    assert snap() == before, "予定を見ただけ・断った要求で、索引かファイルが変わった"
    # 種類: 同定済みの記録にも付く(画面は 1 件ずつ現物を見て決める)。退避してから書く
    n = len(glob.glob(os.path.join(e.d, "backups", "*")))
    st, _, obj = tidy(e.g, True, kind="book")
    got = {r["path"]: r for r in O.load_index()[0]}[e.g]
    assert st == 200 and obj["applied"] and (got["kind"], got["not_paper"], got["rung"]) == ("book", "user", "bib_query"), (st, obj, got)
    assert len(glob.glob(os.path.join(e.d, "backups", "*-before-kind.jsonl"))) == 1 and len(glob.glob(os.path.join(e.d, "backups", "*"))) == n + 1
    assert (rec(e.g)[2]["kind_key"], rec(e.g)[2]["kind"]) == ("book", "book"), "変えた種類が、詳細に出ない: %s" % rec(e.g)[2].get("kind_key")
    assert snap()[2] == before[2], "種類を変えただけでファイルが動いた"
    # 外す: ファイルは置き場の下の決まったフォルダへ。記録は残るが、一覧にも詳細にも出ない
    st, _, obj = tidy(e.g, True, to="remove")
    away = os.path.join(e.d, O.REMOVED_DIR, "g.pdf")
    got = {r["path"]: r for r in O.load_index()[0]}
    assert st == 200 and obj["applied"] and os.path.exists(away) and not os.path.exists(e.g) and got[away]["removed"] == "user", (st, obj)
    assert rec(e.g)[0] == 404 and rec(away)[0] == 404 and away not in [r["path"] for r in e.ok("/api/search", {"q": "", "all": True})["rows"]], \
        "外した記録が、一覧か詳細に出る"
    assert len(glob.glob(os.path.join(e.d, "backups", "*-before-remove.jsonl"))) == 1 and glob.glob(os.path.join(e.d, "renames-*.jsonl"))
    # 送れない理由は 409 で文面を返す: 移した重複が指している記録(a)、ファイルの無い記録(b)。何も動かない
    mid = snap()
    for path, why in ((e.a, "points to this record"), (e.b, "file is missing")):
        st, _, obj = tidy(path, True, to="db:1")
        assert st == 409 and why in obj["error"], "送れない記録を送った・理由を言わない: %s %s" % (st, obj)
    assert snap() == mid, "送れない記録で、索引かファイルが変わった"
    # 送る: ファイル・記録・全頁の本文が相手の DB へ。こちらの索引からは消える。相手の一覧(空の式)に出る
    st, _, obj = tidy(e.e, True, kind="thesis", to="db:1")
    there = os.path.join(e.d2, O.KINDS["thesis"][1], "e.pdf")
    mine, theirs = {r["path"] for r in O.load_index()[0]}, {r["path"]: r for r in e.O2.load_index()[0]}
    assert st == 200 and obj["applied"] and os.path.exists(there) and not os.path.exists(e.e), (st, obj)
    assert e.e not in mine and there in theirs and (theirs[there]["kind"], theirs[there]["first_author"]) == ("thesis", "Tester"), theirs.get(there)
    assert rec(e.e)[0] == 404 and rec(there, "1")[0] == 200 and there in [r["path"] for r in e.ok("/api/search", {"q": "", "all": True, "db": "1"})["rows"]]
    assert e.O2._fulltext_load(e.O2._file_key(there))["pages"] == ["Only deep inside: laser here."], "全頁の本文が相手の DB に写っていない"
    assert len(glob.glob(os.path.join(e.d, "backups", "*-before-send.jsonl"))) == 1 and len(glob.glob(os.path.join(e.d2, "backups", "*-before-receive.jsonl"))) == 1
    # 相手の DB から、同じ操作で送り返せる
    st, _, obj = tidy(there, True, to="db:0", db="1")
    assert st == 200 and obj["applied"] and os.path.exists(os.path.join(e.d, O.KINDS["thesis"][1], "e.pdf")), "相手の DB から送り返せない: %s %s" % (st, obj)
    print("ok: 片付ける(種類は同定済みにも付く、外すとファイルは決まったフォルダへ移り一覧から消える、送るとファイル・記録・全頁の本文が相手の DB へ移る。"
          "予定だけでは変わらない)")


def check_errors(e):
    """処理の中の例外は 500。文面を返さず、サーバは動き続ける。"""
    keep = e.app.run
    for exc in (RuntimeError("secret /private/path"), SystemExit(3), KeyboardInterrupt()):
        def boom(argv, exc=exc):
            raise exc
        e.app.run = boom
        st, h, obj = e.req("POST", "/api/open", {"path": e.a})
        assert st == 500 and "secret" not in json.dumps(obj) and h.get("content-security-policy"), (st, obj)
    e.app.run = keep
    e.ok("/api/info", {})
    print("ok: 処理の中の例外(SystemExit も)は 500。文面を返さない")


def check_readonly(e):
    """どの API を叩いても(直す API は apply なしで)、書く・動かす・検索の索引を作り直す関数が呼ばれない。蔵書のフォルダも変わらない。"""
    called = []
    spies = [(O, "_atomic_write"), (O, "save_index"), (O, "_move_file"), (O, "_backup"), (O, "build_db"), (O, "_fulltext_save"),
             (os, "remove"), (os, "replace"), (os, "rename"), (os, "unlink"), (shutil, "move"), (shutil, "copy2")]
    real = [(m, n, getattr(m, n)) for m, n in spies]
    for m, n, f in real:
        setattr(m, n, lambda *a, _n=n, _f=f, **k: (called.append(_n), _f(*a, **k))[1])
    snap = lambda: sorted((os.path.join(dp, f), os.path.getsize(os.path.join(dp, f)), os.path.getmtime(os.path.join(dp, f)))
                          for dp, _, fs in os.walk(e.d) for f in fs)
    before = snap()
    try:
        for path, body in [("/api/info", {}), ("/api/library", {}), ("/api/search", {"q": "laser", "all": True}), ("/api/search", {"q": "on", "all": False}),
                           ("/api/record", {"path": e.a, "q": "laser", "all": True}), ("/api/record", {"path": e.g, "q": "", "all": True}),
                           ("/api/page", {"path": e.a, "page": 1}), ("/api/cite", {"paths": [e.a, e.b]}),
                           ("/api/fix", {"path": e.e, "set": {"title": "dry run"}, "apply": False}),
                           ("/api/tidy", {"path": e.e, "kind": "thesis", "to": "remove", "apply": False}),
                           ("/api/tidy", {"path": e.e, "to": "db:1", "apply": False}),
                           ("/api/open", {"path": e.a}), ("/api/reveal", {"path": e.a}), ("/api/open", {"path": e.out}),
                           ("/api/open-url", {"path": e.a, "link": "doi"}), ("/api/open-url", {"path": e.e, "link": "scholar"})]:
            e.req("POST", path, body)
        for path in ("/", "/style.css", "/dist/app.js", "/nope"):
            e.req("GET", path)
    finally:
        for m, n, f in real:
            setattr(m, n, f)
    assert called == [], "書く・動かす・作り直す関数が呼ばれた: %s" % called
    assert before == snap(), "蔵書のフォルダが変わった"
    assert O._fetch is gui._offline, "外への道が断たれていない"
    print("ok: %d 個の関数(書く・動かす・作り直す)は、直す API の apply を除いて、どの API からも呼ばれない。外への道は断ってある" % len(spies))


def check_dbs(e):
    """DB を db で選ぶ。索引・検索・開いてよいパス・書く先は DB ごとに別で、混ざらない。"""
    info = e.ok("/api/info", {})
    assert (info["db"], info["dbs"]) == ("0", ["0", "1"]) and e.ok("/api/info", {"db": "0"}) == info, info
    two = e.ok("/api/info", {"db": "1"})
    assert (two["db"], two["name"], two["records"]) == ("1", "Books", 1) and info["name"] == os.path.basename(e.d), (two, info)
    paths = lambda db: [r["path"] for r in e.ok("/api/search", {"q": "laser", "all": True, "db": db})["rows"]]
    assert paths("1") == [e.x] and e.x not in paths("0") and paths("0") == [r["path"] for r in e.ok("/api/search", {"q": "laser", "all": True})["rows"]]
    assert e.ok("/api/record", {"path": e.x, "q": "", "all": True, "db": "1"})["title"] == "Laser handbook"
    # 開いてよいのは、選んだ DB の索引にあるパスだけ(ほかの DB にあるパスでは開かない)
    del e.ran[:]
    assert e.req("POST", "/api/open", {"path": e.x})[0] == 404 and e.req("POST", "/api/open", {"path": e.a, "db": "1"})[0] == 404 and e.ran == []
    e.ok("/api/open", {"path": e.x, "db": "1"})
    assert e.ran == [["open", e.x]], e.ran
    for body, want in (({"db": "9"}, 404), ({"db": 1}, 400), ({"db": ""}, 404)):
        assert e.req("POST", "/api/info", body)[0] == want, body
    assert any(e.d2 in l for l in e.ok("/api/library", {"db": "1"})["lines"]) and not any(e.d2 in l for l in e.ok("/api/library", {})["lines"])
    # 書く先も DB ごと: 2 つ目の DB を直しても、1 つ目の索引と退避は変わらない
    main, n = open(O.INDEX, "rb").read(), len(glob.glob(os.path.join(e.d, "backups", "*")))
    r = e.ok("/api/fix", {"path": e.x, "set": {"year": "1991"}, "apply": True, "db": "1"})
    got = [json.loads(l) for l in open(e.O2.INDEX)]
    assert r["applied"] and got[0]["year"] == 1991 and len(glob.glob(os.path.join(e.d2, "backups", "*-before-fix.jsonl"))) == 1, got
    assert open(O.INDEX, "rb").read() == main and len(glob.glob(os.path.join(e.d, "backups", "*"))) == n, "別の DB の索引・退避に書いた"
    assert e.O2._fetch is gui._offline and e.O2.ROOT == os.path.realpath(e.d2) and e.O2.CODE == O.CODE, "2 つ目の DB の外への道が断たれていない・場所が違う"
    print("ok: DB は db で選ぶ(索引・検索・開いてよいパス・書く先は DB ごと。知らない DB は 404)")


def check_library(e):
    """DB の情報は papers info と同じ行に、頁の絵を作る道具の行を足したもの。"""
    lines = e.ok("/api/library", {})["lines"]
    assert lines[:-1] == O.info_lines()[1] and any(O.INDEX in l for l in lines), lines
    assert lines[-1].startswith("page images") and ("pdftoppm" in lines[-1]), lines[-1]
    print("ok: DB の情報は papers info と同じ行(索引の場所・件数・新しさ)")


def check_stale(e):
    """索引が変わったら読み直す。検索の索引は作り直さず、古いことを知らせる。作り方が違えば 409。"""
    assert e.ok("/api/info", {}) == {"records": 5, "index_updated": e.ok("/api/info", {})["index_updated"],
                                      "columns": ["author"] + list(O.SEARCH_COLS), "can_open": True, "stale": False,
                                      "db": "0", "dbs": ["0", "1"], "name": os.path.basename(e.d), "kinds": list(O.KINDS)}
    recs = O.load_index()[0]
    new = os.path.join(e.d, "new.pdf")
    open(new, "w").write("new")
    assert e.req("POST", "/api/open", {"path": new})[0] == 404
    write(O.INDEX, recs + [{"path": new, "folder": e.d, "filename": "new.pdf", "rung": "unresolved"}])
    os.utime(O.INDEX, (time.time() + 5, time.time() + 5))
    del e.ran[:]
    e.ok("/api/open", {"path": new})
    info = e.ok("/api/info", {})
    assert e.ran == [["open", new]] and info["records"] == 6 and info["stale"], "索引の変更を読み直さない・古いと知らせない: %s" % info
    assert e.ok("/api/search", {"q": "laser", "all": True})["total"] == 2, "古い検索の索引でも引ける"
    fmt, O.DB_FORMAT = O.DB_FORMAT, "other"
    try:
        assert e.req("POST", "/api/search", {"q": "laser", "all": True})[0] == 409 and e.ok("/api/info", {})["stale"]
    finally:
        O.DB_FORMAT = fmt
    print("ok: 索引が変わったら読み直す。検索の索引が古ければ知らせ、作り方が違えば 409")


def check_types():
    """画面の型(gui/src/api.d.ts)が API の表から作ったものと同じ。"""
    f = os.path.join(os.path.dirname(os.path.abspath(gui.__file__)), "gui", "src", "api.d.ts")
    if not os.path.exists(f):
        print("not checked: gui/src/api.d.ts is not here")
        return
    assert open(f).read() == gui.types(), "gui/src/api.d.ts が API の表と違う。python3 gui.py --types > gui/src/api.d.ts"
    # 直せる項目は 3 か所に出る(gui.py の EDIT_KEYS・view.ts の EDIT_KEYS・index.html の欄)。欄が無いと、直す欄を開いた所で止まる
    here = os.path.dirname(os.path.dirname(f))
    html, view = open(os.path.join(here, "index.html")).read(), open(os.path.join(here, "src", "view.ts")).read()
    missing = [k for k in gui.EDIT_KEYS if 'id="edit-f-%s"' % k not in html or 'id="edit-l-%s"' % k not in html]
    assert not missing, "gui/index.html に、直す欄(edit-f-・edit-l-)が無い: %s" % missing
    assert "EDIT_KEYS = [%s]" % ", ".join('"%s"' % k for k in gui.EDIT_KEYS) in view, "gui/src/view.ts の EDIT_KEYS が gui.py と違う"
    # 種類の名前(画面の文言)は、organize.py の KINDS の全部に、日本語と英語の両方である
    names = re.findall(r'kindNames: \{([^}]*)\}', open(os.path.join(here, "src", "strings.ts")).read())
    assert len(names) == 2 and all(set(re.findall(r'(\w+):', n)) == set(O.KINDS) for n in names), \
        "gui/src/strings.ts の kindNames が organize.py の KINDS と違う: %s" % names
    print("ok: gui/src/api.d.ts は API の表から作ったものと同じ。直せる項目は gui.py・view.ts・index.html で同じ")


def check_parent_watch():
    """標準入力が閉じると、終わる合図が出る。閉じるまでは出ない。"""
    r, w = os.pipe()
    gone = threading.Event()
    gui.watch_parent(os.fdopen(r, "rb", buffering=0), gone.set)
    os.write(w, b"still here")
    assert not gone.wait(0.3), "親がいるのに終わる合図が出た"
    os.close(w)
    assert gone.wait(3), "標準入力が閉じても終わる合図が出ない"
    print("ok: 標準入力が閉じると終わる")


def check_same_db(e):
    """--db に、もう開いている DB(同じ索引のファイル)を渡しても、もう 1 つの DB としては開かない(同じ索引を 2 つの DB として書かない)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    p = subprocess.Popen([sys.executable, os.path.realpath(gui.__file__), "--no-open", "--port=%d" % port, "--db=" + e.d, "--db=" + e.d2, "--db=" + e.d2],
                         env=e.child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = p.stdout.readline()
        assert "#t=" in line, "papers gui が立たない: %s" % line
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/info", "{}", {"Content-Type": "application/json", "X-Papers-Token": line.split("#t=")[1].strip()})
        info = json.loads(c.getresponse().read())
        assert info["dbs"] == ["0", "1"], "同じ DB を 2 回開いた(同じ索引を 2 つの DB として書きうる): %s" % info.get("dbs")
    finally:
        p.terminate()
        p.wait(5)
    print("ok: --db に同じ DB を重ねて渡しても、1 つとしてだけ開く")
    # 一緒に開く DB と名前は、その DB 自身の config.json から。本体の場所の config.json(作者の机では、本物の DB を指す)は引き継がない
    import types
    home = tempfile.mkdtemp(prefix="gui-check-own-")
    try:
        other = types.SimpleNamespace(ROOT=home, _config=O._config, CONFIG={"dbs": [{"home": "/inherited"}], "name": "Inherited"})
        assert gui.asked_dbs(other, ["--db=/x", "--port=1"]) == [(None, "/x")] and gui._own(other) == {}, "本体の場所の config.json の dbs・name を引き継いだ"
        open(os.path.join(home, "config.json"), "w").write(json.dumps({"dbs": [{"name": "Books", "home": "/b"}, "/ignored", {"name": "no home"}], "name": "Mine"}))
        assert gui.asked_dbs(other, ["--db=/x"]) == [("Books", "/b"), (None, "/x")] and gui._own(other)["name"] == "Mine", gui.asked_dbs(other, [])
    finally:
        shutil.rmtree(home)
    cfg = os.path.join(e.d, "config.json")
    open(cfg, "w").write('{"name": "Main shelf"}')
    try:
        assert e.ok("/api/info", {})["name"] == "Main shelf", "DB のフォルダの config.json の name が画面の名前にならない"
    finally:
        os.remove(cfg)
    assert e.ok("/api/info", {})["name"] == os.path.basename(e.d)
    print("ok: 一緒に開く DB と名前は、その DB の config.json からだけ読む")


def check_port_busy(e):
    """papers gui でない過程が番号を握っていたら、その過程を止めずに、どれが握っているかと止め方を言って止まる(lsof のある機械で)。
    握っている過程は 3 通り: 無関係の過程、別のフォルダの gui.py という名前のスクリプト(相対パスで起動・絶対パスで起動)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    body = "import socket, time; s = socket.socket(); s.bind(('127.0.0.1', %d)); s.listen(1); print('up', flush=True); time.sleep(60)" % port
    other = os.path.join(e.d, "elsewhere")
    os.makedirs(other)
    open(os.path.join(other, "gui.py"), "w").write(body)
    named = False
    for argv, cwd in ((["-c", body], None), (["gui.py"], other), ([os.path.join(other, "gui.py")], None)):
        holder = subprocess.Popen([sys.executable] + argv, cwd=cwd, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "up"
            try:                                             # 握っている過程を止めてしまうと、待ち受けを始めて終わらなくなる。時間で切る
                r = subprocess.run([sys.executable, os.path.realpath(gui.__file__), "--no-open", "--port=%d" % port],
                                   env=e.child_env(), capture_output=True, text=True, timeout=20)
            except subprocess.TimeoutExpired:
                raise AssertionError("papers gui でない過程(%s)を止めて、その番号で待ち受けを始めた" % argv[0][:20])
            assert r.returncode != 0 and "cannot listen" in r.stderr and "--port=" in r.stderr and holder.poll() is None, \
                "papers gui でない過程が握っている番号で、止まらない・その過程を止めた: %s" % r.stderr
            if shutil.which("lsof") or os.path.exists("/usr/sbin/lsof"):
                assert "kill %d" % holder.pid in r.stderr, "番号を握っている過程の止め方が文面に無い: %s" % r.stderr
                named = True
        finally:
            holder.terminate()
            holder.wait(5)
    print("ok: papers gui でない過程が番号を握っていたら、止めずに、握っている過程と止め方を言って止まる" if named else
          "not checked: lsof is not here, so the process holding the port is not named")


def check_replace(e):
    """前に立てた papers gui が番号を握っていたら、止めて入れ替わる(papers gui でない過程は止めない: check_port_busy)。本物の過程を 2 つ立てて見る。"""
    assert gui.App(O, "t", os.path.join(e.d, "static")).fetch is e.app.fetch is not None, "同じ DB の App を作り直すと、DOI を引く道を失う"
    if not (shutil.which("lsof") or os.path.exists("/usr/sbin/lsof")):
        print("not checked: lsof is not here, so an older papers gui is not recognised")
        return
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    here = os.path.realpath(gui.__file__)
    start = lambda script, cwd=None: subprocess.Popen([sys.executable, script, "--no-open", "--port=%d" % port], cwd=cwd,
                                                      env=e.child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # 前のものは、papers gui(絶対パス)で立てた場合と、python3 gui.py(相対パス)で立てた場合の両方
    for first in ((here, None), ("gui.py", os.path.dirname(here))):
        old, new = start(*first), None
        try:
            line = old.stdout.readline()
            assert "papers gui: http" in line, "1 つ目の papers gui が立たない: %s" % line
            new = start(here)
            line = new.stdout.readline()
            assert "stopped the older papers gui (process %d)" % old.pid in line, "前の papers gui(%s)を止めて入れ替わらない: %s" % (first[0], line)
            assert "papers gui: http://127.0.0.1:%d/" % port in new.stdout.readline() and old.wait(5) is not None, "入れ替わった後に待ち受けていない・前の過程が残っている"
        finally:
            for p in (old, new):
                if p is not None and p.poll() is None:
                    p.terminate()
                    p.wait(5)
    print("ok: 前に立てた papers gui が番号を握っていたら、止めて入れ替わる")


def check_dist():
    """配る JS(gui/dist)が、ソース(gui/src)から作り直したものと同じ(名前の集合も中身も)。型検査が通る。
    dist の中の相対 import の相手が全部ある。"""
    here = os.path.join(os.path.dirname(os.path.abspath(gui.__file__)), "gui")
    with tempfile.TemporaryDirectory() as d:
        try:
            r = subprocess.run(TSC + ["-p", here, "--outDir", d], capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("npx (node) is not installed, so gui/dist cannot be checked")
        assert r.returncode == 0, "tsc failed:\n%s" % (r.stdout + r.stderr)
        built = {f: open(os.path.join(d, f)).read() for f in os.listdir(d)}
    dist = os.path.join(here, "dist")
    have = {f: open(os.path.join(dist, f)).read() for f in os.listdir(dist)}
    differ = sorted(f for f in set(have) | set(built) if have.get(f) != built.get(f))
    assert not differ, "gui/dist is not what gui/src builds (%s). Rebuild: %s -p gui" % (", ".join(differ), " ".join(TSC))
    for f, text in have.items():
        for target in re.findall(r'from "(\./[^"]+)"', text):
            assert os.path.exists(os.path.join(dist, target)), "%s imports %s, which is not in gui/dist" % (f, target)
    print("ok: gui/dist はソースから作り直したものと同じ(型検査も通る)。相対 import の相手がある")


def main():
    if "--dist" in sys.argv:
        return check_dist()
    e = Env()
    try:
        check_entry(e)
        check_headers(e)
        check_open(e)
        check_links(e)
        check_search(e)
        check_record(e)
        check_page(e)
        check_cite(e)
        check_errors(e)
        check_readonly(e)
        check_dbs(e)
        check_library(e)
        check_stale(e)
        check_fix(e)
        check_fix_doi(e)
        check_fix_isbn(e)
        check_tidy(e)
        check_replace(e)
        check_same_db(e)
        check_port_busy(e)
        assert e.net == [], "外へ出ようとした: %s" % e.net
        print("ok: 外へは出ていない")
    finally:
        e.close()
    check_types()
    check_parent_watch()
    print("gui_check: ok")


if __name__ == "__main__":
    main()
