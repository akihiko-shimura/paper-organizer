#!/usr/bin/env python3
"""索引とキャッシュの書き込みの検査。

selftest に入れないのは意図(CLAUDE.md): ここは一時ディレクトリに書くので落ちる理由が多い。
見るのは 3 つ:
  A. 書き込みの途中で例外が出ても、旧索引が 1 バイトも変わらない
  B. 読んだ後に追記された索引は差し替えず、追記を残す(差し替えないときは別名に残す)
  C. 書き込みのために open する箇所が、下の許可リストにあるものだけ(静的検査)
  D. 記入済みで未 merge の unresolved.tsv を review が白紙に戻さない
  E. 記入済みで未 merge の published.tsv を pubver が白紙に戻さない。merge は y だけを書く
  F. review の候補の列は記入ではない。merge は ok=y の候補だけを書き、n は拒否として残す
  G. fix は --apply が無ければ書かない。1 件に絞れない・直せない項目は止まる。書く前に退避する
  H. シンボリックリンク(~/.local/bin/papers)経由でも、リンクの場所ではなく本体の索引を読み書きする
  I. info は、検索の索引が索引より古いときに古いと言い、新しいときは言わない
  J. rename と undo は、動かしたファイルを指す項目(duplicate_of・published_copy)も書き換える
  K. relocate は既定で書かない。--apply で動いたファイルのパスと指す先を直し、消えた重複だけを外す
  L. views は元の PDF へのリンクを作り、作り直しで消すのは自分の作ったリンクだけ(普通のファイルがあれば止まる)
  M. move は既定で動かさない。--apply でファイルと索引を一緒に動かし、置き場の外・名前のぶつかりは止まる。undo で戻る
  N. scan は、手で動かしたファイル(パスの無い記録に当たる新しいファイル)があれば同定せずに止まる
  O. add は --apply を受けない。新しい PDF があれば退避してから足し、ファイルは動かさず、検索の索引を作り直す。
     無ければ同定も退避もしない
  P. scan・add は、前回外部から取れなかった記録(error で fetch_failed あり)だけを同定し直し、同じパスの行を 2 つにしない。
     読み取りのエラー(fetch_failed なし)は取り直さない
C は規約 1 の機械化: 新しい書き込み経路を足すと、ここで名指しで落ちる。
許可リストに足すなら、なぜ _atomic_write を通さなくてよいかをコメントに書くこと。
実行: python3 index_check.py
"""
import ast, glob, json, os, tempfile

import organize as O

# (関数名, open の第 1 引数) → _atomic_write を通さない理由
ALLOWED = {
    ("_atomic_write", "tmp"): "これ自体が原子的書き込みの実装",
    ("scan", "INDEX"): "追記(a)。既存行を消さない",
    ("rename", "trail"): "時刻付きの新規ファイル。mv の前に書く",
    ("rename", "os.path.join(ROOT, 'unresolved.txt')"): "索引からの派生。毎回作り直す",
    ("dedup", "trail"): "時刻付きの新規ファイル。mv の前に書く",
    ("views", "os.path.join(out, VIEW_MARK)"): "ビュー(リンクだけのフォルダ)の印。索引とは無関係で、作り直しのたびに書く",
    ("views", "os.path.join(out, VIEW_HELP)"): "ビューの使い方。作り直しのたびに書き直す",
}


def check_static():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "organize.py")).read()
    tree = ast.parse(src)
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open"):
                continue
            mode = node.args[1] if len(node.args) > 1 else next(
                (k.value for k in node.keywords if k.arg == "mode"), None)
            m = mode.value if isinstance(mode, ast.Constant) else "r"
            if any(c in m for c in "wax+"):
                found.append((fn.name, ast.unparse(node.args[0])))
    bad = [f for f in found if f not in ALLOWED]
    assert not bad, "許可リストに無い書き込み: %s" % bad
    missing = [k for k in ALLOWED if k not in found]
    assert not missing, "許可リストの項目が見つからない(リストが古い): %s" % missing
    print("C ok: 書き込み open %d 箇所、すべて許可リスト内" % len(found))


def write(path, recs):
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def check_crash(d):
    O.INDEX = os.path.join(d, "a.jsonl")
    write(O.INDEX, [{"path": "x%d" % i} for i in range(3)])
    before = open(O.INDEX, "rb").read()
    recs, stamp = O.load_index()
    recs.insert(1, {"path": object()})            # 2 行目で json.dumps が TypeError
    try:
        O.save_index(recs, stamp)
    except TypeError:
        pass
    else:
        raise AssertionError("例外が起きていない(検査が何も壊していない)")
    assert open(O.INDEX, "rb").read() == before, "書き込み途中の例外で旧索引が変わった"
    assert not glob.glob(O.INDEX + ".tmp*"), "一時ファイルが残った"
    print("A ok: 途中の例外で旧索引は不変、一時ファイルも残らない")


def check_conflict(d):
    O.INDEX = os.path.join(d, "b.jsonl")
    write(O.INDEX, [{"path": "x"}])
    # 正の対照: 誰も触らなければ差し替わる(差し替えない実装でも B が通ってしまうのを防ぐ)
    recs, stamp = O.load_index()
    recs[0]["k"] = 1
    try:
        O.save_index(recs, stamp)
    except SystemExit:
        raise AssertionError("干渉が無いのに衝突と判定した")
    assert O.load_index()[0] == [{"path": "x", "k": 1}], "干渉なしで差し替わらない"
    # 読んだ後に scan が追記した
    recs, stamp = O.load_index()
    with open(O.INDEX, "a") as f:
        f.write(json.dumps({"path": "appended"}) + "\n")
    recs[0]["k"] = 2
    try:
        O.save_index(recs, stamp)
    except SystemExit:
        pass
    else:
        raise AssertionError("追記を上書きした")
    now = O.load_index()[0]
    assert now == [{"path": "x", "k": 1}, {"path": "appended"}], now
    side = glob.glob(O.INDEX + ".conflict-*")
    assert len(side) == 1 and '"k": 2' in open(side[0]).read(), "このパスの結果が残っていない"
    print("B ok: 読んだ後の追記を残し、こちらの結果は %s に退避" % os.path.basename(side[0]))


def check_review(d):
    """D. 記入済みで未 merge の行がある unresolved.tsv を review が白紙に戻さない。"""
    O.INDEX = os.path.join(d, "r.jsonl")
    O.REVIEW_TSV = os.path.join(d, "unresolved.tsv")
    write(O.INDEX, [{"path": "/a.pdf", "rung": "unresolved"}, {"path": "/b.pdf", "rung": "unresolved"}])
    cols = len(O.REVIEW_COLS)
    head = "\t".join(["reason", "path", "hint"] + O.REVIEW_COLS) + "\n"
    filled = head + "C\t/a.pdf\th\t10.1/x" + "\t" * (cols - 1) + "\n" + "C\t/b.pdf\th" + "\t" * cols + "\n"
    open(O.REVIEW_TSV, "w").write(filled)
    O.sys.argv = ["organize.py", "review"]
    try:
        O.review()
    except SystemExit:
        pass
    else:
        raise AssertionError("記入済みで未 merge の unresolved.tsv を上書きした")
    assert open(O.REVIEW_TSV).read() == filled, "止まったのにファイルが変わった"
    # --force: 既存は退避され、消えない
    O.sys.argv = ["organize.py", "review", "--force"]
    O.review()
    kept = glob.glob(O.REVIEW_TSV[:-4] + "-*.tsv")
    assert len(kept) == 1 and open(kept[0]).read() == filled, "--force で記入が退避されていない"
    assert "10.1/x" not in open(O.REVIEW_TSV).read()
    # merge 済み(索引で manual)なら守る対象ではなく、そのまま作り直せる
    open(O.REVIEW_TSV, "w").write(filled)
    write(O.INDEX, [{"path": "/a.pdf", "rung": "manual", "source": "crossref"}, {"path": "/b.pdf", "rung": "unresolved"}])
    O.sys.argv = ["organize.py", "review"]
    try:
        O.review()
    except SystemExit:
        raise AssertionError("merge 済みの行まで守って review を止めた(記入→merge→review の流れが止まる)")
    assert "/a.pdf" not in open(O.REVIEW_TSV).read() and "/b.pdf" in open(O.REVIEW_TSV).read()
    print("D ok: 未 merge の記入は守り、--force は退避してから作り直し、merge 済みは止めない")


def check_pubver(d):
    """E. 記入済みで未 merge の published.tsv を pubver が上書きしない。merge は y だけ書き、n は二度と出さない。"""
    O.INDEX = os.path.join(d, "p.jsonl")
    O.PUBVER_TSV = os.path.join(d, "published.tsv")
    base = {"rung": "arxiv_text", "source": "arxiv", "arxiv_id": "1", "first_author": "K", "title": "t", "year": 2000}
    write(O.INDEX, [dict(base, path="/a.pdf"), dict(base, path="/b.pdf"), dict(base, path="/c.pdf")])
    row = lambda p, doi, ok: "\t".join([p, "t", "", doi, "t", "J", "2001", "journal-article", ok]) + "\n"
    filled = "\t".join(O.PUBVER_COLS) + "\n" + row("/a.pdf", "10.1/a", "y") + row("/b.pdf", "10.1/b", "n") + row("/c.pdf", "10.1/c", "?")
    open(O.PUBVER_TSV, "w").write(filled)
    O.sys.argv = ["organize.py", "pubver"]
    try:
        O.pubver()
    except SystemExit:
        pass
    else:
        raise AssertionError("記入済みで未 merge の published.tsv を上書きした")
    assert open(O.PUBVER_TSV).read() == filled, "止まったのにファイルが変わった"
    O.sys.argv = ["organize.py", "pubver", "--merge"]
    O.pubver()
    by = {r["path"]: r for r in map(json.loads, open(O.INDEX))}
    assert by["/a.pdf"].get("published_doi") == "10.1/a", "y を書いていない"
    assert not by["/b.pdf"].get("published_doi") and by["/b.pdf"].get("published_doi_rejected") == ["10.1/b"], "n の扱いが違う"
    assert not by["/c.pdf"].get("published_doi") and not by["/c.pdf"].get("published_doi_rejected"), "y/n 以外を書いた"
    assert [x[0] for x in O._pubver_filled(O.PUBVER_TSV, list(by.values()))] == ["/c.pdf"], "merge 後も守る行が違う"
    print("E ok: 未 merge の記入は守り、merge は y だけ書き、n は拒否として残す")


def check_review_cand(d):
    """F. 候補の列は記入ではない。ok=y の候補だけを書き、n は拒否として残し、雑誌として引けない候補は書かない。"""
    O.INDEX = os.path.join(d, "f.jsonl")
    O.REVIEW_TSV = os.path.join(d, "cand.tsv")
    paths = ["/pre.pdf", "/y.pdf", "/n.pdf", "/thesis.pdf"]
    write(O.INDEX, [{"path": p, "rung": "unresolved"} for p in paths])
    head = ["reason", "path", "hint"] + O.CAND_COLS + O.REVIEW_COLS
    def row(p, cand, ok):
        v = dict(reason="B_", path=p, hint="t", cand_doi=cand, cand_title="t", cand_where="J", cand_year="2000", ok=ok)
        return "\t".join(v.get(h, "") for h in head) + "\n"
    tsv = "\t".join(head) + "\n" + row("/pre.pdf", "10.1/pre", "") + row("/y.pdf", "10.1/y", "y") + \
        row("/n.pdf", "10.1/n", "n") + row("/thesis.pdf", "10.1/thesis", "y")
    open(O.REVIEW_TSV, "w").write(tsv)
    assert [r[1] for r in O._read_review(O.REVIEW_TSV)[1]] == ["/y.pdf", "/n.pdf", "/thesis.pdf"], \
        "review が埋めた候補の列を記入と数えている(記入が 1 件も無くても review が止まる)"
    real = O.from_crossref
    O.from_crossref = lambda doi: None if doi == "10.1/thesis" else {"doi": doi, "year": 2000, "first_author": "A", "journal": "J"}
    try:
        O.merge()
    finally:
        O.from_crossref = real
    by = {r["path"]: r for r in map(json.loads, open(O.INDEX))}
    assert by["/y.pdf"].get("doi") == "10.1/y" and by["/y.pdf"]["rung"] == "manual", "ok=y の候補を書いていない"
    assert by["/n.pdf"]["rung"] == "unresolved" and by["/n.pdf"].get("cand_rejected") == ["10.1/n"], "ok=n の扱いが違う"
    assert by["/pre.pdf"]["rung"] == "unresolved" and not by["/pre.pdf"].get("doi"), "ok の無い候補を書いた"
    assert by["/thesis.pdf"]["rung"] == "unresolved", "雑誌として引けない候補で空の手入力の記録を作った"
    left = [r[1] for r in O._unmerged(O.REVIEW_TSV, list(by.values()))]
    assert left == ["/thesis.pdf"], "merge 後に守る行が違う: %s" % left
    print("F ok: 候補の列は記入でなく、y だけ書き、n は拒否、雑誌として引けない候補は書かずに残す")


def check_fix(d):
    """G. fix は既定で書かない。1 件に絞れない名前・直せない項目では止まる。--apply は退避してから書く。"""
    O.INDEX = os.path.join(d, "g", "g.jsonl")
    os.makedirs(os.path.dirname(O.INDEX))
    base = {"rung": "text_doi", "source": "crossref", "year": 2002, "first_author": "Yamadda", "folder": "/f"}
    write(O.INDEX, [dict(base, path="/f/2006_Yamadda_X.pdf", filename="2006_Yamadda_X.pdf"),
                    dict(base, path="/f/2006_Yamadda_Y.pdf", filename="2006_Yamadda_Y.pdf")])
    before = open(O.INDEX).read()
    def run(*a):
        O.sys.argv = ["organize.py", "fix"] + list(a)
        try:
            O.fix(); return "ok"
        except SystemExit:
            return "exit"
    assert run("Yamadda", "--set", "first_author=Yamada", "--apply") == "exit", "2 件に当たる名前で書いた"
    assert run("_X.pdf", "--set", "rung=manual", "--apply") == "exit", "直せない項目を書いた"
    assert run("_X.pdf", "--set", "first_author=Yamada") == "ok" and open(O.INDEX).read() == before, "--apply 無しで書いた"
    assert run("_X.pdf", "--set", "first_author=Yamada", "--set", "year=2006", "--apply") == "ok"
    by = {r["filename"]: r for r in map(json.loads, open(O.INDEX))}
    x = by["2006_Yamadda_X.pdf"]
    assert x["first_author"] == "Yamada" and x["year"] == 2006 and x["rung"] == "manual", "直した値が違う"
    assert by["2006_Yamadda_Y.pdf"]["first_author"] == "Yamadda", "別の記録まで書き換えた"
    kept = glob.glob(os.path.join(d, "g", "backups", "*-before-fix.jsonl"))
    assert len(kept) == 1 and open(kept[0]).read() == before, "書く前に退避していない"
    print("G ok: 既定は書かない、絞れない・直せない項目は止まる、書く前に退避する")


def check_link(d):
    """H. リンク経由で呼んでも ROOT は本体の場所。リンクの場所を ROOT にすると、そこに索引・キャッシュを作る。"""
    import subprocess, sys
    link = os.path.join(d, "papers")
    os.symlink(os.path.join(os.path.dirname(os.path.abspath(__file__)), "organize.py"), link)
    env = {k: v for k, v in os.environ.items() if k != "INDEX"}
    r = subprocess.run([sys.executable, link, "selftest"], cwd=d, env=env, capture_output=True, text=True)
    root = subprocess.run([sys.executable, "-c", "import runpy; print(runpy.run_path(%r)['ROOT'])" % link],
                          cwd=d, env=env, capture_output=True, text=True).stdout.strip()
    assert r.returncode == 0 and root == os.path.dirname(os.path.abspath(__file__)), \
        "リンク経由で ROOT がリンクの場所(%s)になる" % root
    # 索引がまだ無いとき(初めての利用者)、読むコマンドは例外で落ちずに、最初の手順を出して止まる
    env2 = dict(env, INDEX=os.path.join(d, "no-such-index.jsonl"))
    for cmd in ("info", "search", "stats", "has", "rename", "dedup"):
        q = subprocess.run([sys.executable, link, cmd, "x"], cwd=d, env=env2, capture_output=True, text=True)
        assert q.returncode != 0 and "papers add" in q.stderr and "Traceback" not in q.stderr, \
            "索引が無いときに %s が案内を出さずに落ちる: %s" % (cmd, q.stderr[-200:])
    print("H ok: リンク経由でも ROOT は本体。索引が無ければ最初の手順を出して止まる")


def check_info(d):
    """I. info の db_stale: 索引の方が新しければ真、古ければ(件数が同じなら)偽。一時ディレクトリの小さな索引と
    検索の索引で見る(本物のデータに頼ると、それが無い CI や mutate.py の複製で素通りする)。"""
    import contextlib, io, sqlite3
    O.INDEX, O.DB = os.path.join(d, "info.jsonl"), os.path.join(d, "info.db")
    write(O.INDEX, [{"path": "/f/a.pdf", "folder": "/f", "rung": "unresolved"}])
    c = sqlite3.connect(O.DB); c.execute("CREATE TABLE p (path)"); c.execute("INSERT INTO p VALUES ('/f/a.pdf')")
    c.commit(); c.close()
    def run():
        O.sys.argv = ["organize.py", "info", "--json"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            O.info()
        return json.loads(out.getvalue())
    dm = os.path.getmtime(O.DB)
    os.utime(O.INDEX, (dm + 60, dm + 60))
    assert run()["db_stale"], "索引の方が新しいのに、検索の索引が古いと言わない"
    os.utime(O.INDEX, (dm - 60, dm - 60))
    assert not run()["db_stale"], "検索の索引の方が新しく件数も同じなのに古いと言う"
    print("I ok: 検索の索引が古いときだけ古いと言う")


def check_rename_pointers(d):
    """J. 一時ディレクトリで rename --apply と undo を回し、重複の記録が指す先がファイルと一緒に動くかを見る。"""
    root = os.path.join(d, "j"); lib = os.path.join(root, "lib"); os.makedirs(os.path.join(lib, "_dup"))
    keep, dup = os.path.join(lib, "old.pdf"), os.path.join(lib, "_dup", "old.pdf")
    for f in (keep, dup):
        open(f, "w").write("%PDF")
    O.ROOT, O.INDEX = root, os.path.join(root, "j.jsonl")
    base = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "Miller", "journal": "ACS Nano",
            "volume": "10", "pages": "2803-2818", "doi": "10.1/x"}
    write(O.INDEX, [dict(base, path=keep, folder=lib, filename="old.pdf"),
                    dict(base, path=dup, folder=os.path.dirname(dup), filename="old.pdf", duplicate_of=keep)])
    O.sys.argv = ["organize.py", "rename", "--apply"]
    O.rename()
    by = {r["filename"]: r for r in map(json.loads, open(O.INDEX))}
    new = by["2016_Miller_ACSNano_10_2803.pdf"]["path"]
    assert os.path.exists(new) and by["old.pdf"]["duplicate_of"] == new, "改名したファイルを指す項目が古いパスのまま"
    O.sys.argv = ["organize.py", "undo"]
    O.undo()
    by = {r["path"]: r for r in map(json.loads, open(O.INDEX))}
    assert by[dup]["duplicate_of"] == keep and os.path.exists(keep), "undo で指す先が戻らない"
    print("J ok: rename と undo は、動かしたファイルを指す項目も書き換える")


def check_relocate(d):
    """K. 一時ディレクトリで、ファイルを手で動かし・重複を消した状態を作り、relocate を回す。"""
    root = os.path.join(d, "k"); os.makedirs(os.path.join(root, "old")); os.makedirs(os.path.join(root, "new"))
    a_old, a_new = os.path.join(root, "old", "a.pdf"), os.path.join(root, "new", "a.pdf")
    open(a_new, "w").write("%PDF")                                  # a は old から new へ手で動かした
    O.INDEX = os.path.join(root, "k.jsonl")
    base = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "A", "doi": "10.1/a", "text": ""}
    write(O.INDEX, [dict(base, path=a_old, folder=os.path.dirname(a_old), filename="a.pdf"),
                    dict(base, path=os.path.join(root, "_dup", "a.pdf"), folder=os.path.join(root, "_dup"), filename="a.pdf",
                         duplicate_of=a_old),                       # 利用者が消した重複
                    dict(base, path=os.path.join(root, "_pre", "x.pdf"), folder=os.path.join(root, "_pre"), filename="x.pdf",
                         doi=None, rung="arxiv_text", arxiv_id="1501.01234", published_copy=a_old),   # 消したプレプリント
                    dict(base, path=os.path.join(root, "old", "lost.pdf"), folder=os.path.join(root, "old"),
                         filename="lost.pdf", doi="10.1/l")])        # どこにも無い(消した?)
    before = open(O.INDEX).read()
    O.sys.argv = ["organize.py", "relocate", "--root", root]
    O.relocate()
    assert open(O.INDEX).read() == before, "--apply 無しで書いた"
    O.sys.argv = ["organize.py", "relocate", "--root", root, "--apply"]
    O.relocate()
    by = {r["filename"] + (r.get("doi") or ""): r for r in map(json.loads, open(O.INDEX))}
    assert by["a.pdf10.1/a"]["path"] == a_new and by["a.pdf10.1/a"]["folder"] == os.path.dirname(a_new), "動いたパスを直していない"
    assert not [r for r in by.values() if r.get("duplicate_of") or r.get("published_copy")], "消えた重複を索引に残している"
    assert "lost.pdf10.1/l" in by, "重複でない記録を、ファイルが見つからないだけで外した"
    assert by["a.pdf10.1/a"].get("preprint_arxiv_id") == "1501.01234", "消したプレプリントの arXiv ID を出版版に移していない"
    assert glob.glob(os.path.join(root, "backups", "*-before-relocate.jsonl")), "書く前に退避していない"
    print("K ok: 既定は書かない、動いたパスを直す、消えた重複だけを外す、書く前に退避する")


def check_views(d):
    """L. 一時ディレクトリにビューを作り、作り直し・普通のファイルがあるとき・印の無いフォルダを見る。"""
    root = os.path.join(d, "l"); os.makedirs(root)
    pdf = os.path.join(root, "2016_Miller_ACSNano.pdf"); open(pdf, "w").write("%PDF")
    O.INDEX = os.path.join(root, "l.jsonl")
    write(O.INDEX, [{"path": pdf, "folder": root, "filename": os.path.basename(pdf), "rung": "text_doi", "source": "crossref",
                     "year": 2016, "first_author": "Miller", "journal": "ACS Nano"}])
    out = os.path.join(root, "view")
    def run():
        O.sys.argv = ["organize.py", "views", "--out", out]
        try:
            O.views(); return "ok"
        except SystemExit:
            return "exit"
    assert run() == "ok"
    link = os.path.join(out, "誌名", "ACS Nano", os.path.basename(pdf))
    assert os.path.islink(link) and os.path.realpath(link) == os.path.realpath(pdf), "元の PDF へのリンクになっていない"
    assert run() == "ok" and os.path.exists(pdf), "作り直しで元の PDF を消した(または使い方.txt で止まった)"
    assert open(os.path.join(out, O.VIEW_HELP)).read() == O.VIEW_HELP_TEXT, "使い方.txt が無い"
    sub = os.path.join(out, "年", O.VIEW_HELP); open(sub, "w").write("下の階層の同じ名前")
    assert run() == "exit" and os.path.exists(sub), "下の階層の普通のファイル(使い方.txt という名前)を消した"
    os.remove(sub)
    for f in (os.path.join(out, ".DS_Store"), os.path.join(out, "年", ".DS_Store")):
        open(f, "w").write("Finder")
    assert run() == "ok", "Finder が作った .DS_Store で作り直しが止まる"
    mine = os.path.join(out, "年", "memo.txt"); open(mine, "w").write("利用者のメモ")
    assert run() == "exit" and os.path.exists(mine), "ビューの中の普通のファイルを消した"
    os.remove(mine)
    os.remove(os.path.join(out, O.VIEW_MARK))
    assert run() == "exit" and os.path.exists(link), "papers views が作ったのでないフォルダを消した"
    print("L ok: 元の PDF へのリンク、作り直しで消すのは自分のリンクだけ、普通のファイル・印の無いフォルダでは止まる")


def check_move(d):
    """M. 一時ディレクトリの置き場で move を回す。"""
    root = os.path.join(d, "m"); lib = os.path.join(root, "lib")
    for sub in ("a", "b", "c"):
        os.makedirs(os.path.join(lib, sub))
    x = os.path.join(lib, "a", "x.pdf"); open(x, "w").write("%PDF")
    open(os.path.join(lib, "c", "x.pdf"), "w").write("%PDF other")
    O.ROOT, O.INDEX = root, os.path.join(root, "m.jsonl")
    base = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "A", "doi": "10.1/x"}
    write(O.INDEX, [dict(base, path=x, folder=os.path.dirname(x), filename="x.pdf"),
                    dict(base, path=os.path.join(lib, "c", "x.pdf"), folder=os.path.join(lib, "c"), filename="x.pdf",
                         doi="10.1/y", duplicate_of=x)])
    def run(*a):
        O.sys.argv = ["organize.py", "move"] + list(a)
        try:
            O.move(); return "ok"
        except SystemExit:
            return "exit"
    before = open(O.INDEX).read()
    assert run(x, "--to", "b") == "ok" and os.path.exists(x) and open(O.INDEX).read() == before, "--apply 無しで動かした"
    os.makedirs(os.path.join(d, "outside"), exist_ok=True)       # 実在するフォルダで(無いフォルダは別の判定で止まる)
    assert run(x, "--to", os.path.join(d, "outside"), "--apply") == "exit" and os.path.exists(x), "置き場の外へ動かした"
    assert run(x, "--to", "c", "--apply") == "exit" and os.path.exists(x), "同じ名前のファイルのある所へ動かした"
    nd = os.path.join(lib, "d")
    assert run(x, "--to", "d") == "ok" and not os.path.exists(nd), "dry run でフォルダを作った(または無いフォルダで予定を出さない)"
    assert run(x, "--to", "d", "--apply") == "exit" and not os.path.exists(nd) and os.path.exists(x), "--mkdir 無しでフォルダを作った"
    assert run(x, "--to", "b", "--apply") == "ok"
    new = os.path.join(lib, "b", "x.pdf")
    by = {r["doi"]: r for r in map(json.loads, open(O.INDEX))}
    assert os.path.exists(new) and not os.path.exists(x), "ファイルが動いていない"
    assert by["10.1/x"]["path"] == new and by["10.1/x"]["folder"] == os.path.dirname(new), "索引を直していない"
    assert by["10.1/y"]["duplicate_of"] == new, "動かしたファイルを指す項目を直していない"
    O.sys.argv = ["organize.py", "undo"]; O.undo()
    assert os.path.exists(x) and json.loads(open(O.INDEX).readline())["path"] == x, "undo で戻らない"
    print("M ok: 既定は動かさない、置き場の外・名前のぶつかりは止まる、ファイルと索引と指す先を一緒に動かす、undo で戻る")


def check_scan_guard(d):
    """N. 記録のファイルを手で動かした状態で scan すると、同定(ネットワーク)に進まずに止まる。"""
    root = os.path.join(d, "n"); os.makedirs(os.path.join(root, "new"))
    moved = os.path.join(root, "new", "a.pdf"); open(moved, "w").write("%PDF")
    O.INDEX = os.path.join(root, "n.jsonl")
    write(O.INDEX, [{"path": os.path.join(root, "old", "a.pdf"), "folder": os.path.join(root, "old"), "filename": "a.pdf",
                     "rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "A", "text": ""}])
    real, called = O.resolve, []
    O.resolve = lambda p: called.append(p) or {"path": p, "rung": "unresolved", "folder": root, "filename": "a.pdf"}
    try:
        O.sys.argv = ["organize.py", "scan", root]
        try:
            O.scan(root); stopped = False
        except SystemExit:
            stopped = True
    finally:
        O.resolve = real
    assert stopped and not called and len(open(O.INDEX).readlines()) == 1, "手で動かしたファイルを新しいファイルとして同定した"
    print("N ok: 手で動かしたファイルがあると scan は同定せずに止まる")


def check_add(d):
    """O. 一時ディレクトリで add を回す。同定・抄録・検索の索引(ネットワークと Swift)は差し替え、add 自身の段取りを見る。"""
    import contextlib, io
    root = os.path.join(d, "o"); lib = os.path.join(root, "lib"); os.makedirs(lib)
    old, new = os.path.join(lib, "2016_Miller_ACSNano_10_2803.pdf"), os.path.join(lib, "new.pdf")
    for f in (old, new):
        open(f, "w").write("%PDF")
    O.ROOT, O.INDEX, O.DB = root, os.path.join(root, "o.jsonl"), os.path.join(root, "o.db")
    base = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "Miller", "journal": "ACS Nano",
            "volume": "10", "pages": "2803-2818", "doi": "10.1/x", "title": "Old", "text": "Old paper"}
    write(O.INDEX, [dict(base, path=old, folder=lib, filename=os.path.basename(old))])
    real, calls = (O.resolve, O.enrich, O.build_db), []
    O.resolve = lambda p: calls.append("resolve") or dict(base, path=p, folder=lib, filename=os.path.basename(p), year=2020,
                                                          doi="10.1/n", title="New", text="New paper")
    O.enrich = lambda: calls.append("enrich")
    O.build_db = lambda: calls.append("index") or open(O.DB, "w").close()
    def run(*args):
        O.sys.argv = ["organize.py", "add"] + list(args)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            O.add()
        return out.getvalue()
    backups = lambda: glob.glob(os.path.join(root, "backups", "*-before-add.jsonl"))
    try:
        try:
            run(lib, "--apply"); stopped = False
        except SystemExit:
            stopped = True
        assert stopped and not calls and len(open(O.INDEX).readlines()) == 1, "--apply を受けた(dedup・rename が承認なしに動く)"
        out = run(lib)
        recs = [json.loads(l) for l in open(O.INDEX)]
        assert [r["filename"] for r in recs] == [os.path.basename(old), "new.pdf"], "新しい PDF を索引に足していない"
        assert os.path.exists(new) and os.path.exists(old), "add がファイルを動かした"
        assert calls == ["resolve", "enrich", "index"], "同定・抄録・検索の索引の順に進んでいない: %s" % calls
        assert len(backups()) == 1 and len(open(backups()[0]).readlines()) == 1, "足す前の索引を退避していない"
        report = out.split("=== 重複")[0]
        assert "new.pdf" in report and "New paper" in report and "Miller_ACSNano" not in report, "報告が新しい記録だけでない"
        assert "2020_Miller_ACSNano_10_2803.pdf" in out, "改名の予定を出していない"
        out = run(lib)                                       # 2 回目: 新しい PDF は無い
        assert "新しい PDF は無い" in out and calls == ["resolve", "enrich", "index"], "新しい PDF が無いのに同定・索引をやり直した"
        assert len(backups()) == 1, "新しい PDF が無いのに退避した"
        open(os.path.join(lib, "new2.pdf"), "w").write("%PDF")   # 3 回目: 抄録の取得が中断しても、報告と予定は出す
        def abort():
            raise SystemExit(1)
        O.enrich = abort
        out = run(lib)
        assert "new2.pdf" in out.split("=== 重複")[0] and "=== 改名" in out, "抄録の取得の中断で、報告と予定が出なくなった"
        plan = out.split("=== 重複")[1].split("=== 改名")[0]             # new2 は new と同じ DOI(差し替えた同定が同じ値を返す)
        assert "10.1/n" in plan and os.path.exists(os.path.join(lib, "new2.pdf")) and os.path.exists(new), \
            "重複の予定を出していない、または dry run で動かした"
    finally:
        O.resolve, O.enrich, O.build_db = real
    print("O ok: add は --apply を受けない、退避してから足す、ファイルは動かさない、無ければ何もしない")


def check_retry(d):
    """P. 取れなかった記録・読み取りのエラー・解決済みの 3 件がある索引で scan と add を回す。同定は差し替える。"""
    import contextlib, io
    root = os.path.join(d, "p"); lib = os.path.join(root, "lib"); os.makedirs(lib)
    paths = {n: os.path.join(lib, n + ".pdf") for n in ("lost", "broken", "good")}
    for f in paths.values():
        open(f, "w").write("%PDF")
    O.ROOT, O.INDEX, O.DB = root, os.path.join(root, "p.jsonl"), os.path.join(root, "p.db")
    rec = lambda n, **k: dict({"path": paths[n], "folder": lib, "filename": n + ".pdf"}, **k)
    good = rec("good", rung="text_doi", source="crossref", year=2016, first_author="G", journal="J", doi="10.1/g", title="Good")
    other = os.path.join(root, "other"); os.makedirs(other)           # 今回見ないフォルダにある、取れなかった記録
    far = {"path": os.path.join(other, "far.pdf"), "folder": other, "filename": "far.pdf", "rung": "error",
           "error": "外部から取れなかった: arxiv:2", "fetch_failed": ["arxiv:2"]}
    open(far["path"], "w").write("%PDF")
    start = [rec("lost", rung="error", error="外部から取れなかった: arxiv:1", fetch_failed=["arxiv:1"], text="Lost paper"),
             rec("broken", rung="error", error="broken pdf"), good, far]
    real, calls, answer = (O.resolve, O.enrich, O.build_db), [], {}
    O.resolve = lambda p: calls.append(os.path.basename(p)) or dict(answer, path=p, folder=lib, filename=os.path.basename(p))
    O.enrich = lambda: None
    O.build_db = lambda: open(O.DB, "w").close()
    load = lambda: [json.loads(l) for l in open(O.INDEX)]
    try:
        write(O.INDEX, start)                                # 1. まだ取れない: error のまま、行は増えない
        answer.update(rung="error", error="外部から取れなかった: arxiv:1", fetch_failed=["arxiv:1"])
        with contextlib.redirect_stderr(io.StringIO()):
            O.scan(lib)
        recs = load()
        assert calls == ["lost.pdf"], "取り直す対象が違う(読み取りのエラー・解決済みを同定し直した、または取り直していない): %s" % calls
        assert sorted(r["filename"] for r in recs) == ["broken.pdf", "far.pdf", "good.pdf", "lost.pdf"], "同じパスの行が増えた・消えた"
        assert far in recs, "見ていないフォルダの取れなかった記録を、同定し直さずに索引から外した"
        assert [r for r in recs if r["filename"] == "good.pdf"] == [good], "取り直しで他の記録が変わった"
        assert glob.glob(os.path.join(root, "backups", "*-before-retry.jsonl")), "取り直す記録を外す前に退避していない"
        answer.clear()                                       # 2. 取れた: add が新しい記録として報告する
        answer.update(rung="arxiv_text", source="arxiv", year=2019, first_author="L", journal="arXiv", arxiv_id="1", title="Lost")
        O.sys.argv = ["organize.py", "add", lib]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            O.add()
        recs = load()
        assert calls == ["lost.pdf", "lost.pdf"] and len(recs) == 4, "add が取れなかった記録を取り直していない: %s" % calls
        assert [r["rung"] for r in recs if r["filename"] == "lost.pdf"] == ["arxiv_text"], "取り直した結果が索引に入っていない"
        report = out.getvalue().split("=== 重複")[0]
        assert "lost.pdf" in report and "good.pdf" not in report, "取り直した記録を新しい記録として報告していない"
        O.sys.argv = ["organize.py", "add", lib]             # 3. もう取り直すものは無い
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            O.add()
        assert calls == ["lost.pdf", "lost.pdf"], "取れた記録・読み取りのエラーをもう一度同定した"
    finally:
        O.resolve, O.enrich, O.build_db = real
    print("P ok: 取れなかった記録だけを取り直す、行を増やさない、外す前に退避する、取れたら新しい記録として報告する")


if __name__ == "__main__":
    check_static()
    with tempfile.TemporaryDirectory() as d:
        check_crash(d)
        check_conflict(d)
        check_review(d)
        check_pubver(d)
        check_review_cand(d)
        check_fix(d)
        check_link(d)
        check_info(d)
        check_rename_pointers(d)
        check_relocate(d)
        check_views(d)
        check_move(d)
        check_scan_guard(d)
        check_add(d)
        check_retry(d)
    print("index_check: ok")
