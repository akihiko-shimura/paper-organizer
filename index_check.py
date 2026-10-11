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
  K. relocate は既定で書かない。--apply で動いたファイルのパスと指す先を直し、消えた重複だけを外す。
     見つからない記録は --forget --apply のときだけ外す(退避してから)
  L. views は元の PDF へのリンクを作り、作り直しで消すのは自分の作ったリンクだけ(普通のファイルがあれば止まる)
  M. move は既定で動かさない。--apply でファイルと索引を一緒に動かし、置き場の外・名前のぶつかりは止まる。undo で戻る
  N. scan は、手で動かしたファイル(パスの無い記録に当たる新しいファイル)があれば同定せずに止まる
  O. add は --apply を受けない。新しい PDF があれば退避してから足し、ファイルは動かさず、全頁の本文を読み、検索の索引を作り直す。
     無ければ同定も退避もしない
  P. scan・add は、前回外部から取れなかった記録(error で fetch_failed あり)だけを同定し直し、同じパスの行を 2 つにしない。
     読み取りのエラー(fetch_failed なし)は取り直さない
  S. ファイルを動かす経路(rename・move・dedup・undo・nonpaper)は、移動先の別のファイルを上書きしない。os.rename を呼ぶのは
     _move_file だけ(静的検査)。大小文字だけが違う名前は同じ名前として扱う。途中で失敗しても動かした分は索引に書く。
     同じ秒に 2 回動かしても、前の記録を上書きしない
  R. nonpaper は既定で動かさない。nonpaper-ok は索引に無いパス・知らない種類があれば書かない。--apply で退避してから
     ファイルと索引を一緒に動かし、undo で戻る。nonpaper-page は頁数を調べて索引に書き、候補だけをページに出す
  T. 全頁の本文(fulltext/)は中身から作る鍵で置き、改名しても読み直さない。読めないファイルは置かない。テキスト層の乏しい
     ファイルは全頁を OCR する。検索の索引は別名で作ってから置き換える。今の表で当たる記録が先に並ぶ。--kind で種類を絞る
C は規約 1 の機械化: 新しい書き込み経路を足すと、ここで名指しで落ちる。
許可リストに足すなら、なぜ _atomic_write を通さなくてよいかをコメントに書くこと。
実行: python3 index_check.py
"""
import ast, glob, json, os, tempfile, time

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
    # S の静的な半分: os.rename は黙って上書きするので、呼んでよいのは移動先を確かめる _move_file だけ
    raw = sorted({fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) for node in ast.walk(fn)
                  if isinstance(node, ast.Call) and ast.unparse(node.func) in ("os.rename", "shutil.move", "os.renames")})
    assert raw == ["_move_file"], "移動先を確かめずにファイルを動かす箇所がある(_move_file を通す): %s" % raw


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
    kept = glob.glob(os.path.join(os.path.dirname(O.INDEX), "backups", "*-before-pubver.jsonl"))
    assert kept and "published_doi" not in open(kept[0]).read(), "pubver --merge が、書く前の索引を退避していない"
    assert not by["/b.pdf"].get("published_doi") and by["/b.pdf"].get("published_doi_rejected") == ["10.1/b"], "n の扱いが違う"
    assert not by["/c.pdf"].get("published_doi") and not by["/c.pdf"].get("published_doi_rejected"), "y/n 以外を書いた"
    assert [x[0] for x in O._pubver_filled(O.PUBVER_TSV, list(by.values()))] == ["/c.pdf"], "merge 後も守る行が違う"
    print("E ok: 未 merge の記入は守り、merge は y だけ書き、n は拒否として残す")


def check_review_cand(d):
    """F. 候補の列は記入ではない。ok=y の候補だけを書き、n は拒否として残し、雑誌として引けない候補は書かない。"""
    O.INDEX = os.path.join(d, "f.jsonl")
    O.REVIEW_TSV = os.path.join(d, "cand.tsv")
    paths = ["/pre.pdf", "/y.pdf", "/n.pdf", "/thesis.pdf", "/x.pdf", "/m.pdf", "/x0.pdf"]
    write(O.INDEX, [{"path": p, "rung": "unresolved"} for p in paths])
    head = ["reason", "path", "hint"] + O.CAND_COLS + O.REVIEW_COLS
    def row(p, cand, ok):
        v = dict(reason="B_", path=p, hint="t", cand_doi=cand, cand_title="t", cand_where="J", cand_year="2000", ok=ok)
        return "\t".join(v.get(h, "") for h in head) + "\n"
    tsv = "\t".join(head) + "\n" + row("/pre.pdf", "10.1/pre", "") + row("/y.pdf", "10.1/y", "y") + \
        row("/n.pdf", "10.1/n", "n") + row("/thesis.pdf", "10.1/thesis", "y") + \
        row("/x.pdf", "10.1/x", "x") + row("/m.pdf", "10.1/m", "m") + row("/x0.pdf", "", "x")
    open(O.REVIEW_TSV, "w").write(tsv)
    assert [r[1] for r in O._read_review(O.REVIEW_TSV)[1]] == ["/y.pdf", "/n.pdf", "/thesis.pdf", "/x.pdf", "/m.pdf", "/x0.pdf"], \
        "review が埋めた候補の列を記入と数えている(記入が 1 件も無くても review が止まる)"
    real = O.from_crossref
    O.from_crossref = lambda doi: None if doi == "10.1/thesis" else {"doi": doi, "year": 2000, "first_author": "A", "journal": "J"}
    try:
        O.merge()
    finally:
        O.from_crossref = real
    by = {r["path"]: r for r in map(json.loads, open(O.INDEX))}
    assert by["/y.pdf"].get("doi") == "10.1/y" and by["/y.pdf"]["rung"] == "manual", "ok=y の候補を書いていない"
    kept = glob.glob(os.path.join(os.path.dirname(O.INDEX), "backups", "*-before-merge.jsonl"))
    assert kept and '"manual"' not in open(kept[0]).read(), "merge が、書く前の索引を退避していない"
    assert by["/n.pdf"]["rung"] == "unresolved" and by["/n.pdf"].get("cand_rejected") == ["10.1/n"], "ok=n の扱いが違う"
    assert by["/pre.pdf"]["rung"] == "unresolved" and not by["/pre.pdf"].get("doi"), "ok の無い候補を書いた"
    assert by["/thesis.pdf"]["rung"] == "unresolved", "雑誌として引けない候補で空の手入力の記録を作った"
    # x: 論文ではない。同定せず、人手行きから外し、候補は二度と出さない。候補の無い行でも付けられる
    for p in ("/x.pdf", "/x0.pdf"):
        assert by[p]["rung"] == "unresolved" and by[p].get("not_paper") == "user" and not by[p].get("doi") \
            and not O.needs_human(by[p]), "ok=x の扱いが違う: %s" % by[p]
    assert by["/x.pdf"].get("cand_rejected") == ["10.1/x"], "論文ではないとした記録の候補を、拒否として残していない"
    # m: 候補は正しいが、手元の PDF は出版前の原稿。y と同じに書き、原稿の印を付ける
    assert by["/m.pdf"].get("doi") == "10.1/m" and by["/m.pdf"]["rung"] == "manual" \
        and by["/m.pdf"].get("version_note") == "manuscript", "ok=m の扱いが違う: %s" % by["/m.pdf"]
    assert not by["/y.pdf"].get("version_note") and not by["/y.pdf"].get("not_paper"), "y の記録に原稿・論文でないの印を付けた"
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


def stub_db(path, fmt=None):
    """検索の索引の代わり(Swift も FTS5 も使わない)。版の印だけ本物と同じ形で持つ。fmt="" なら印の無い古い索引。"""
    import sqlite3
    if os.path.exists(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE p (path)"); c.execute("INSERT INTO p VALUES ('/f/a.pdf')")
    c.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO meta VALUES ('mtime','9e99')")     # 索引より新しい。作り直す理由を版だけにする
    if fmt != "":
        c.execute("INSERT INTO meta VALUES ('format',?)", (O.DB_FORMAT if fmt is None else fmt,))
    c.commit(); c.close()


def check_info(d):
    """I. info の db_stale: 索引の方が新しければ真、古ければ(件数が同じなら)偽。一時ディレクトリの小さな索引と
    検索の索引で見る(本物のデータに頼ると、それが無い CI や mutate.py の複製で素通りする)。"""
    import contextlib, io
    O.INDEX, O.DB = os.path.join(d, "info.jsonl"), os.path.join(d, "info.db")
    write(O.INDEX, [{"path": "/f/a.pdf", "folder": "/f", "rung": "unresolved"}])
    stub_db(O.DB)
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
    for old in ("1", ""):                                   # 作り方の版が違う・版の印が無い(この変更より前の索引)
        stub_db(O.DB, old)
        os.utime(O.INDEX, (dm - 60, dm - 60))
        assert run()["db_stale"], "作り方の古い検索の索引(版 %r)を古いと言わない" % old
    print("I ok: 検索の索引が古いとき・作り方が違うときだけ古いと言う")


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
    before, n_bk = open(O.INDEX).read(), len(glob.glob(os.path.join(root, "backups", "*")))
    O.sys.argv = ["organize.py", "relocate", "--root", root, "--forget"]
    O.relocate()
    assert open(O.INDEX).read() == before, "--forget だけで(--apply 無しで)書いた"
    O.sys.argv = ["organize.py", "relocate", "--root", root, "--forget", "--apply"]
    O.relocate()
    left = [r["filename"] for r in map(json.loads, open(O.INDEX))]
    assert left == ["a.pdf"], "--forget --apply が、見つからない記録を外していない、またはファイルのある記録まで外した: %s" % left
    assert len(glob.glob(os.path.join(root, "backups", "*"))) == n_bk + 1, "--forget で外す前に退避していない"
    # 移した重複が指している記録は、ファイルが無くても外さない(外すと、ファイルのある重複が検索に出ないまま残る)
    kept = os.path.join(root, "old", "m.pdf")
    os.makedirs(os.path.join(root, "_dup"), exist_ok=True); open(os.path.join(root, "_dup", "m.pdf"), "w").write("%PDF m")
    write(O.INDEX, [dict(base, path=a_new, folder=os.path.dirname(a_new), filename="a.pdf"),
                    dict(base, path=kept, folder=os.path.dirname(kept), filename="m.pdf", doi="10.1/m"),
                    dict(base, path=os.path.join(root, "_dup", "m.pdf"), folder=os.path.join(root, "_dup"), filename="m.pdf",
                         doi="10.1/m", duplicate_of=kept)])
    O.relocate()
    left = sorted(r["path"] for r in map(json.loads, open(O.INDEX)))
    assert kept in left and len(left) == 3, "移した重複が指している記録を --forget で外した: %s" % left
    # 全部の記録のファイルが無い(置き場が見えない)ときは、--forget を止める
    os.remove(os.path.join(root, "_dup", "m.pdf")); os.remove(a_new)   # 置き場に PDF が 1 つも無い状態にする(対応付く先を残さない)
    write(O.INDEX, [dict(base, path=kept, folder=os.path.dirname(kept), filename="m.pdf", doi="10.1/m")])
    before = open(O.INDEX).read()
    try:
        O.relocate(); stopped = False
    except SystemExit:
        stopped = True
    assert stopped and open(O.INDEX).read() == before, "全部の記録のファイルが無いのに、--forget で索引を空にした"
    print("K ok: 既定は書かない、動いたパスを直す、消えた重複だけを外す、見つからない記録は --forget のときだけ外す、書く前に退避する")


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
    back = json.loads(open(O.INDEX).readline())
    assert os.path.exists(x) and back["path"] == x and back["folder"] == os.path.dirname(x), "undo で戻らない(ファイル・パス・フォルダ)"
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
    real, calls, real_pages, real_fill = (O.resolve, O.enrich, O.build_db), [], O.pdf_pages, O.fulltext_fill
    read, fills = set(), []                                  # 全頁の本文は差し替える: まだ読んでいないパスの数を「読んだ」と返す
    ocr_next = []                                            # 次の呼び出しで「OCR した」と返す件数
    def fill(recs, do_ocr=True):
        new = {r["path"] for r in recs} - read
        read.update(new); fills.append((len(new), do_ocr))
        return {"read": len(new), "ocr": ocr_next.pop() if ocr_next else 0, "ocr_pages": 0, "pending": 0}, []
    O.fulltext_fill = fill
    O.resolve = lambda p: calls.append("resolve") or dict(base, path=p, folder=lib, filename=os.path.basename(p), year=2020,
                                                          doi="10.1/n", title="New", text="New paper")
    O.enrich = lambda: calls.append("enrich")
    O.build_db = lambda: calls.append("index") or stub_db(O.DB)
    O.pdf_pages = lambda p: 7
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
        assert recs[1].get("n_pages") == 7 and "=== 論文以外" in out, "新しい記録の頁数を記録していない、または論文以外の予定を出していない"
        assert calls == ["resolve", "enrich", "index"], "同定・抄録・検索の索引の順に進んでいない: %s" % calls
        assert fills == [(2, True)] and "=== 全頁の本文" in out, "足したあとに全頁の本文を読んでいない(OCR つきで): %s" % fills
        assert len(backups()) == 1 and len(open(backups()[0]).readlines()) == 1, "足す前の索引を退避していない"
        report = out.split("=== 重複")[0]
        assert "new.pdf" in report and "New paper" in report and "Miller_ACSNano" not in report, "報告が新しい記録だけでない"
        assert "2020_Miller_ACSNano_10_2803.pdf" in out, "改名の予定を出していない"
        out = run(lib)                                       # 2 回目: 新しい PDF は無い
        assert "新しい PDF は無い" in out and calls == ["resolve", "enrich", "index"], "新しい PDF が無いのに同定・索引をやり直した"
        assert len(backups()) == 1, "新しい PDF が無いのに退避した"
        stub_db(O.DB, "1")                                   # 作り方の古い検索の索引は、新しい PDF が無くても作り直す
        run(lib)
        assert calls == ["resolve", "enrich", "index", "index"], "作り方の古い検索の索引を作り直さない: %s" % calls
        read.clear()                                         # 新しい PDF は無いが、全頁の本文を新しく読んだ → 検索の索引を作り直す
        run(lib)
        assert calls == ["resolve", "enrich", "index", "index", "index"], "全頁の本文を読んだのに、検索の索引を作り直さない: %s" % calls
        ocr_next.append(1)                                   # 新しく読んだものは無いが、待ちだった OCR が進んだ
        run(lib)
        assert calls == ["resolve", "enrich"] + ["index"] * 4, "OCR が進んだのに、検索の索引を作り直さない: %s" % calls
        del calls[-1]
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
        O.pdf_pages, O.fulltext_fill = real_pages, real_fill
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


def check_nonpaper(d):
    """R. 一時ディレクトリの置き場で、論文以外の候補のページ・判定の取り込み・移動を回す。"""
    import contextlib, io, shutil
    root = os.path.join(d, "r"); lib = os.path.join(root, "lib")
    for sub in ("a", "b"):
        os.makedirs(os.path.join(lib, sub))
    shutil.copy2(os.path.join(os.path.dirname(os.path.abspath(__file__)), "nonpaper_page.html"), root)
    O.ROOT, O.INDEX = root, os.path.join(root, "r.jsonl")
    def rec(sub, name, **kw):
        p = os.path.join(lib, sub, name); open(p, "w").write("%PDF " + sub)
        return dict({"path": p, "folder": os.path.dirname(p), "filename": name, "rung": "unresolved"}, **kw)
    recs = [rec("a", "thesis.pdf"), rec("b", "thesis.pdf"), rec("a", "short.pdf"),
            rec("a", "paper.pdf", rung="text_doi", source="crossref", year=2016, first_author="A", doi="10.1/x"), rec("a", "broken.pdf")]
    recs[3]["duplicate_of"] = recs[0]["path"]                    # 動かすファイルを指す項目(repoint が直す)
    recs[0]["text"] = "see __KINDS__ and __PAGES__ </script>"   # 本文は外から来る。ページの型の印と同じ文字列があっても壊さない
    write(O.INDEX, recs)
    real, real_rename, real_excl, asked = O.pdf_pages, os.rename, O._rename_excl, []
    O.pdf_pages = lambda p: asked.append(p) or (None if p.endswith("broken.pdf") else 20 if p.endswith("short.pdf") else 120)
    def run(fn, *a):
        O.sys.argv = ["organize.py", "x"] + list(a)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                fn()
            return "ok"
        except SystemExit:
            return "exit"
    load = lambda: {os.path.relpath(r["path"], lib): r for r in map(json.loads, open(O.INDEX))}
    try:
        assert run(O.nonpaper_page) == "ok"
        by, page = load(), open(os.path.join(root, "nonpaper.html")).read()
        assert by["a/thesis.pdf"]["n_pages"] == 120 and by["a/short.pdf"]["n_pages"] == 20, "頁数を調べて索引に書いていない"
        assert "n_pages" not in by["a/paper.pdf"], "同定済みの記録の頁数まで調べている(全件を読むことになる)"
        assert "n_pages" not in by["a/broken.pdf"], "読めなかった頁数を書いている(次から調べ直さなくなる)"
        del asked[:]
        assert run(O.nonpaper_page) == "ok" and asked == [recs[4]["path"]], "頁数の読めなかった記録だけを調べ直していない: %s" % asked
        assert glob.glob(os.path.join(root, "backups", "*before-nonpaper-page*")), "頁数を書く前に索引を退避していない"
        assert page.count("thesis.pdf") >= 2 and "short.pdf" not in page and "paper.pdf" not in page and "__DATA__" not in page, \
            "ページに候補(未同定で頁数の多い記録)だけを出していない、または型の印が残っている"
        data = json.loads(page.split('<script id="data" type="application/json">')[1].split("</script>")[0])
        assert "const KINDS = [[" in page and any("__KINDS__ and __PAGES__" in r["head"] for r in data) and "超える" in page and "__PAGES__ 頁" not in page, \
            "型の印を記録より後に置き換えている(本文に同じ文字列があるとページが壊れる)、または置き換えていない"
        ok = os.path.join(root, "nonpaper-ok.tsv")
        before = open(O.INDEX).read()
        assert run(O.nonpaper_ok, ok) == "exit" and open(O.INDEX).read() == before, "渡されたファイルが無いのに止まらない"
        for body, why in (("path\tkind\n", "判定の行が無い"), ("%s t\n" % recs[0]["path"], "タブで割れない行"),
                          ("%s\tt\n%s\tb\n" % (recs[0]["path"], recs[0]["path"]), "同じパスが 2 行")):
            open(ok, "w").write(body)
            assert run(O.nonpaper_ok, ok) == "exit" and open(O.INDEX).read() == before and not glob.glob(os.path.join(root, "backups", "*before-nonpaper-ok*")), \
                "書かずに止まるべき: " + why
        open(ok, "w").write("path\tkind\n%s\tt\n%s\tb\n" % (recs[0]["path"], os.path.join(lib, "a", "none.pdf")))
        assert run(O.nonpaper_ok, ok) == "exit" and open(O.INDEX).read() == before, "索引に無いパスがあるのに書いた"
        open(ok, "w").write("path\tkind\n%s\tt\n%s\tz\n" % (recs[0]["path"], recs[1]["path"]))
        assert run(O.nonpaper_ok, ok) == "exit" and open(O.INDEX).read() == before, "知らない種類があるのに書いた"
        open(ok, "w").write("path\tkind\n%s\tt\n%s\tt\n" % (recs[0]["path"], recs[1]["path"]))
        assert run(O.nonpaper_ok, ok) == "ok" and load()["a/thesis.pdf"]["kind"] == "thesis" and os.path.exists(recs[0]["path"]), \
            "判定を索引に書いていない、または取り込みでファイルを動かした"
        assert glob.glob(os.path.join(root, "backups", "*before-nonpaper-ok*")), "判定を書く前に索引を退避していない"
        before = open(O.INDEX).read()
        dest = os.path.join(lib, O.NONPAPER_DIR, O.KINDS["thesis"][1])
        assert run(O.nonpaper) == "ok" and os.path.exists(recs[0]["path"]) and not os.path.exists(dest) and open(O.INDEX).read() == before, \
            "--apply 無しで動かした(またはフォルダを作った)"
        assert run(O.nonpaper, "--apply") == "ok"
        by = load()
        moved = sorted(os.listdir(dest))
        assert moved == ["thesis.pdf", "thesis_2.pdf"] and not os.path.exists(recs[0]["path"]) and not os.path.exists(recs[1]["path"]), \
            "ファイルを種類のフォルダへ動かしていない、または同じ名前を上書きした: %s" % moved
        assert sorted(r["folder"] for r in by.values() if r.get("kind")) == [dest, dest] and all(os.path.exists(r["path"]) for r in by.values()), \
            "索引のパスを直していない"
        assert glob.glob(os.path.join(root, "backups", "*before-nonpaper.jsonl")) and glob.glob(os.path.join(root, "renames-*.jsonl")), \
            "動かす前に索引を退避していない、または記録を残していない"
        assert by["a/paper.pdf"]["duplicate_of"] == os.path.join(dest, "thesis.pdf"), "動かしたファイルを指す項目を直していない"
        far = os.path.join(d, "r-outside"); os.makedirs(far, exist_ok=True)      # 置き場の外の記録が入ると、根が変わる
        open(os.path.join(far, "o.pdf"), "w").write("%PDF")
        with open(O.INDEX, "a") as f:
            f.write(json.dumps({"path": os.path.join(far, "o.pdf"), "folder": far, "filename": "o.pdf", "rung": "unresolved"}) + "\n")
        assert run(O.nonpaper, "--apply") == "exit" and sorted(os.listdir(dest)) == moved, "置き場の根が変わったのに、移し済みのファイルを動かした"
        write(O.INDEX, [r for r in map(json.loads, open(O.INDEX)) if r["folder"] != far])
        assert run(O.nonpaper, "--apply") == "ok" and sorted(os.listdir(dest)) == moved, "もう種類のフォルダにある記録を動かし直した"
        assert run(O.undo) == "ok" and os.path.exists(recs[0]["path"]) and os.path.exists(recs[1]["path"]) and not os.listdir(dest), "undo で戻らない"
        assert all(os.path.exists(r["path"]) and r["folder"] == os.path.dirname(r["path"]) for r in load().values()), \
            "undo のあと索引のパスかフォルダが戻っていない"
        n = []                                                   # 2 件目の移動で失敗させる: 動かした 1 件は索引に書く
        def flaky(a, b):
            n.append(1)
            if len(n) == 2:
                raise OSError("EXDEV")
            return real_excl(a, b)
        O._rename_excl = flaky
        try:
            O.sys.argv = ["organize.py", "nonpaper", "--apply"]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                O.nonpaper()
            failed = False
        except OSError:
            failed = True
        O._rename_excl = real_excl
        by = load()
        assert failed and len(os.listdir(dest)) == 1 and all(os.path.exists(r["path"]) and r["folder"] == os.path.dirname(r["path"]) for r in by.values()), \
            "移動の途中で失敗したとき、動かした分を索引に書いていない(索引が無いパスを指す)"
        left = [r for r in by.values() if r.get("kind") and r["folder"] != dest][0]
        there = os.path.join(dest, "thesis.pdf")               # 予定が見分けられなかった同名(予定のあとに現れた等)は上書きしない
        keep, real_plan = open(there).read(), O.nonpaper_plan
        O.nonpaper_plan = lambda recs, root: ([(r, there) for r in recs if r["path"] == left["path"]], [])
        try:
            O.sys.argv = ["organize.py", "nonpaper", "--apply"]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                O.nonpaper()
            stopped = False
        except FileExistsError:
            stopped = True
        O.nonpaper_plan = real_plan
        assert stopped and open(there).read() == keep and os.path.exists(left["path"]), "移動先にあるファイルを上書きした"
        assert run(O.nonpaper, "--apply") == "ok" and sorted(os.listdir(dest)) == ["thesis.pdf", "thesis_2.pdf"], "失敗のあとの再実行で残りを動かさない"
    finally:
        O.pdf_pages, os.rename, O._rename_excl = real, real_rename, real_excl
    print("R ok: 頁数を調べて候補だけをページに出す、判定は索引に無いパス・知らない種類があれば書かない、既定は動かさない、"
          "退避してからファイルと索引を一緒に動かす、undo で戻る")


def check_overwrite(d):
    """S. 一時ディレクトリの置き場で、動かす経路が別のファイルを上書きしないことを見る。"""
    import contextlib, io, time
    root = os.path.join(d, "s"); lib = os.path.join(root, "lib")
    for sub in ("a", "b", "c"):
        os.makedirs(os.path.join(lib, sub))
    O.ROOT, O.INDEX = root, os.path.join(root, "s.jsonl")
    real_rename, real_excl = os.rename, O._rename_excl
    insensitive = os.path.exists(os.path.join(lib, "A"))         # このボリュームが大小文字を区別しないか(macOS は既定でしない)
    def put(sub, name, body):
        p = os.path.join(lib, sub, name); open(p, "w").write(body); return p
    def run(fn, *a):
        O.sys.argv = ["organize.py", "x"] + list(a)
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                fn()
            return "ok"
        except SystemExit:
            return "exit"
        except OSError as e:
            return type(e).__name__
    load = lambda: [json.loads(l) for l in open(O.INDEX)]
    sound = lambda: all(os.path.exists(r["path"]) and r["folder"] == os.path.dirname(r["path"]) and r["filename"] == os.path.basename(r["path"])
                        for r in load())
    bodies = lambda folder: sorted(open(os.path.join(folder, f)).read() for f in os.listdir(folder))
    def flaky_on(k):                                             # k 回目の移動で失敗させる(_move_file が最初に呼ぶところで)
        n = []
        def f(a, b):
            n.append(1)
            if len(n) == k:
                raise OSError("EXDEV")
            return real_excl(a, b)
        return f
    base = {"rung": "text_doi", "source": "crossref", "year": 2016, "journal_short": "J", "volume": "1", "pages": "2"}
    try:
        # --- _move_file そのもの
        x, y = put("a", "x.pdf", "X"), put("a", "y.pdf", "Y")
        try:
            O._move_file(x, y); hit = False
        except FileExistsError:
            hit = True
        assert hit and open(x).read() == "X" and open(y).read() == "Y", "移動先の別のファイルを上書きした"
        O._move_file(x, os.path.join(lib, "a", "z.pdf"))
        assert not os.path.exists(x) and open(os.path.join(lib, "a", "z.pdf")).read() == "X", "空いている移動先へ動かさない"
        def refused(src, dst):
            try:
                O._move_file(src, dst); return False
            except FileExistsError:
                return True
        if insensitive:                                          # 大小文字だけを変える移動はしない(その場での書き換えは確かめていない)
            assert refused(y, os.path.join(lib, "a", "Y.pdf")) and os.listdir(os.path.join(lib, "a")).count("y.pdf") == 1, \
                "大小文字だけが違う名前へ、その場で動かした"
        link, dead = os.path.join(lib, "a", "link.pdf"), os.path.join(lib, "a", "dead.pdf")
        os.symlink(y, link); os.symlink(os.path.join(lib, "a", "nowhere"), dead)
        assert refused(link, y) and open(y).read() == "Y" and not os.path.islink(y), "リンクを実体の名前へ動かして、実体を壊した"
        assert refused(y, dead) and os.path.islink(dead) and open(y).read() == "Y", "壊れたリンクを、無いものとして上書きした"
        O._move_file(y, y)
        assert open(y).read() == "Y", "同じパスへの移動で止まった・壊した"
        # OS が断る改名: 確かめた後に移動先へファイルが現れても(ここでは確認を黙らせて再現)、上書きしない
        z = os.path.join(lib, "a", "z.pdf")
        probe = os.path.join(lib, "a", "probe.pdf"); open(probe, "w").write("P")
        excl = O._rename_excl(probe, os.path.join(lib, "a", "probe2.pdf"))        # この OS・ボリュームで使えるか
        os.remove(os.path.join(lib, "a", "probe2.pdf")) if excl else os.remove(probe)
        import sys as _sys
        assert excl or not _sys.platform.startswith(("darwin", "linux")), "この OS では「移動先があれば断る改名」が使えるはずなのに、使えていない"
        assert O._rename_excl(os.path.join(lib, "a", "no-such.pdf"), os.path.join(lib, "a", "free.pdf")) is False \
            and not os.path.exists(os.path.join(lib, "a", "free.pdf")), "失敗した改名を「動かした」と返した"
        O._rename_excl = lambda s, t: False                      # 使えない OS・ボリュームの枝: 確かめてから os.rename
        try:
            fb = os.path.join(lib, "a", "fb.pdf"); open(fb, "w").write("FB")
            O._move_file(fb, os.path.join(lib, "a", "fb2.pdf"))
            assert open(os.path.join(lib, "a", "fb2.pdf")).read() == "FB" and not os.path.exists(fb) and refused(os.path.join(lib, "a", "fb2.pdf"), y), \
                "断る改名が使えないとき、確かめてからの移動をしない(または確かめない)"
            os.remove(os.path.join(lib, "a", "fb2.pdf"))
        finally:
            O._rename_excl = real_excl
        if excl:
            real_lexists = os.path.lexists
            os.path.lexists = lambda p: False
            try:
                late = refused(z, y)
            finally:
                os.path.lexists = real_lexists
            assert late and open(y).read() == "Y" and open(z).read() == "X", "確認のあとに現れたファイルを上書きした(OS が断る改名を使っていない)"
        assert O._occupied(dead, y) and O._occupied(link, y) and not O._occupied(y, y) and not O._occupied(os.path.join(lib, "a", "free.pdf"), y), \
            "移動先が塞がっているかの判定: 壊れたリンク・リンクは「ある」、自分自身と何も無い場所は「無い」"
        for f in os.listdir(os.path.join(lib, "a")):
            os.remove(os.path.join(lib, "a", f))
        # --- rename: 大小文字だけが違う改名先が 2 件。途中で失敗しても索引は合う
        p1, p2 = put("a", "one.pdf", "ONE"), put("a", "two.pdf", "TWO")
        rec = lambda p, **kw: dict(base, path=p, folder=os.path.dirname(p), filename=os.path.basename(p), **kw)
        write(O.INDEX, [rec(p1, first_author="delong", doi="10.1/1"), rec(p2, first_author="DeLong", doi="10.1/2")])
        names = lambda folder: sorted(os.listdir(folder))
        assert run(O.rename, "--apply") == "ok" and sound() and bodies(os.path.join(lib, "a")) == ["ONE", "TWO"], \
            "rename が、大小文字だけが違う名前へ 2 件を改名して 1 件を上書きした(または止まった): %s" % os.listdir(os.path.join(lib, "a"))
        got = names(os.path.join(lib, "a"))
        kept_b = lambda tag: glob.glob(os.path.join(root, "backups", "*-before-%s.jsonl" % tag))
        assert len(kept_b("rename")) == 1 and open(kept_b("rename")[0]).read().count("one.pdf") == 2, "rename --apply が、動かす前の索引を退避していない"
        assert len({O._same_name(f) for f in got}) == 2, "rename が、大小文字だけが違う 2 つの名前を付けた(区別しないボリュームでは上書きになる): %s" % got
        n_trails = len(glob.glob(os.path.join(root, "renames-*.jsonl")))
        time.sleep(1.1)
        assert run(O.rename, "--apply") == "ok" and names(os.path.join(lib, "a")) == got and sound(), "もう _2 付きの名前になっている記録を、もう一度動かそうとした"
        assert len(glob.glob(os.path.join(root, "renames-*.jsonl"))) == n_trails, "動かすものが無いのに記録を書いた(直前の move を undo で戻せなくなる)"
        q1, q2 = put("b", "q1.pdf", "Q1"), put("b", "q2.pdf", "Q2")
        write(O.INDEX, [rec(q1, first_author="Able", doi="10.1/1b"), rec(q2, first_author="Baker", doi="10.1/2b")])
        O._rename_excl = flaky_on(2)
        time.sleep(1.1)
        assert run(O.rename, "--apply") == "OSError" and sound() and len(os.listdir(os.path.join(lib, "b"))) == 2, \
            "rename が途中で失敗したとき、動かした分を索引に書いていない"
        O._rename_excl = real_excl
        for f in os.listdir(os.path.join(lib, "b")):
            os.remove(os.path.join(lib, "b", f))
        # --- ファイルの無い記録の名前へ、別の記録を改名しない(続く改名が「その名前にあるファイル」を動かし、PDF と書誌が入れ替わる)
        g1 = put("b", "g.pdf", "G")
        ghost = os.path.join(lib, "b", "2016_Able_J_1_2.pdf")   # 記録だけあって、ファイルは無い
        write(O.INDEX, [rec(g1, first_author="Able", doi="10.1/g1"), dict(rec(ghost, doi="10.1/u"), rung="unresolved")])
        said = io.StringIO()
        O.sys.argv = ["organize.py", "rename"]
        with contextlib.redirect_stdout(said):
            O.rename()
        assert "2016_Able_J_1_2_2.pdf" in said.getvalue(), "同定できていない記録の(ファイルの無い)パスへ、別の記録を改名する予定にした"
        write(O.INDEX, [rec(g1, first_author="Able", doi="10.1/g1"), rec(ghost, first_author="Baker", doi="10.1/g2")])
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            O.rename()
        assert "ファイルが無いので改名しない 1" in said.getvalue() and "\n1 rename" in said.getvalue(), "予定(dry run)が、ファイルの無い記録を報告しない"
        time.sleep(1.1)
        said = io.StringIO()
        O.sys.argv = ["organize.py", "rename", "--apply"]
        with contextlib.redirect_stdout(said), contextlib.redirect_stderr(io.StringIO()):
            O.rename()
        by = {r["doi"]: r["path"] for r in load()}
        assert names(os.path.join(lib, "b")) == ["2016_Able_J_1_2_2.pdf"] and open(by["10.1/g1"]).read() == "G" and by["10.1/g2"] == ghost, \
            "ファイルの無い記録の名前へ別の記録を改名した(または、その PDF を無い記録の名前に付け替えた): %s %s" % (names(os.path.join(lib, "b")), by)
        assert "ファイルが無いので改名しない 1" in said.getvalue() and "\n1 rename" in said.getvalue(), \
            "ファイルの無い記録を、改名の予定に数えている(または報告しない): %s" % said.getvalue()[-120:]
        assert run(O.undo) == "ok" and open(g1).read() == "G"
        os.remove(g1)
        # --- undo は後に動かしたものから戻す。同じファイルを 2 度動かした記録(A→B、B→C)でも、ファイルも索引も A へ戻る
        # 直す前の rename が書いた記録の形: 記録 X が c1→c2、ファイルの無い記録 Y が c2→c3(X の PDF が Y の名前になった)
        c1, c2, c3 = [os.path.join(lib, "b", n) for n in ("c1.pdf", "c2.pdf", "c3.pdf")]
        open(c3, "w").write("CH")
        write(O.INDEX, [rec(c2, first_author="A", doi="10.1/X"), rec(c3, first_author="B", doi="10.1/Y")])
        time.sleep(1.1)
        with open(O._trail("renames"), "w") as f:
            f.write(json.dumps({"from": c1, "to": c2}) + "\n" + json.dumps({"from": c2, "to": c3}) + "\n")
        assert run(O.undo) == "ok" and names(os.path.join(lib, "b")) == ["c1.pdf"] and open(c1).read() == "CH", \
            "undo が、2 度動かされたファイルを最初の場所まで戻さない: %s" % names(os.path.join(lib, "b"))
        by = {r["doi"]: r["path"] for r in load()}
        assert by == {"10.1/X": c1, "10.1/Y": c2}, "undo が、索引を 1 段ずつ戻していない(2 件の記録が同じ PDF を指す): %s" % by
        os.remove(c1)
        # --- 同じ秒の 2 つ目の記録は前の記録を上書きしない。undo は後のほうを読む
        b1, b2 = O._backup("x"), O._backup("x")                 # 同じ秒の 2 つ目の退避は、前の退避を上書きしない
        assert b1 != b2 and os.path.exists(b1) and os.path.exists(b2) and sorted([b1, b2]) == [b1, b2] and b2.endswith("-before-x.jsonl"), \
            "同じ秒の退避の名前が重なる(前の退避を上書きする)、または名前の形が変わった: %s" % os.path.basename(b2)
        for prefix in ("renames", "dedup"):
            t1 = O._trail(prefix); open(t1, "w").close()
            t2 = O._trail(prefix)
            assert t1 != t2 and sorted([t1, t2]) == [t1, t2] and os.path.basename(t1).startswith(prefix + "-"), \
                "同じ秒の記録の名前が重なる、または名前の順が作った順でない(%s)" % prefix
            os.remove(t1)
        # --- undo: 元の場所に別のファイルが置かれていたら上書きしない。元のフォルダが消されていても戻す
        for f in os.listdir(root):
            if f.startswith("renames-"):
                os.remove(os.path.join(root, f))
        for f in os.listdir(os.path.join(lib, "a")):
            os.remove(os.path.join(lib, "a", f))
        m1, m2 = put("a", "m1.pdf", "M1"), put("b", "m2.pdf", "M2")
        write(O.INDEX, [rec(m1, first_author="A", doi="10.1/3"), rec(m2, first_author="B", doi="10.1/4")])
        assert run(O.move, m1, m2, "--to", "c", "--apply") == "ok" and sound()
        put("a", "m1.pdf", "INTRUDER")                           # 元の場所に、あとから別のファイル
        os.rmdir(os.path.join(lib, "b"))                         # 元のフォルダが消された
        said = io.StringIO()
        O.sys.argv = ["organize.py", "undo"]
        with contextlib.redirect_stdout(said):
            O.undo()
        assert "戻さなかった 1 件" in said.getvalue() and "m1.pdf" in said.getvalue(), "undo が、戻さなかったファイルを報告しない"
        assert kept_b("undo") and all(os.path.join(lib, "c") in l for l in open(sorted(kept_b("undo"))[-1])), "undo が、戻す前の索引を退避していない"
        assert open(m1).read() == "INTRUDER" and open(os.path.join(lib, "c", "m1.pdf")).read() == "M1", "undo が、元の場所に置かれた別のファイルを上書きした"
        by = {r["doi"]: r["path"] for r in load()}
        assert open(m2).read() == "M2" and sound() and by["10.1/4"] == m2, "undo が、消された元のフォルダへ戻さない"
        assert by["10.1/3"] == os.path.join(lib, "c", "m1.pdf"), "undo が、戻せなかった記録の索引まで元のパスに戻した(別のファイルを指す)"
        w1, w2 = put("a", "w1.pdf", "W1"), put("b", "w2.pdf", "W2")
        write(O.INDEX, [rec(w1, first_author="A", doi="10.1/w1"), rec(w2, first_author="B", doi="10.1/w2")])
        time.sleep(1.1)
        assert run(O.move, w1, w2, "--to", "c", "--apply") == "ok" and sound()
        O._rename_excl = flaky_on(2)
        assert run(O.undo) == "OSError" and sound() and sorted(os.path.basename(r["folder"]) for r in load()) in (["a", "c"], ["b", "c"]), \
            "undo が途中で失敗したとき、戻した分を索引に書いていない(または戻していない分まで書いた)"
        O._rename_excl = real_excl
        assert run(O.undo) == "ok" and sound() and sorted(os.path.basename(r["folder"]) for r in load()) == ["a", "b"], "undo のやり直しで残りを戻さない"
        n_undo = len(kept_b("undo"))
        assert run(O.undo) == "ok" and len(kept_b("undo")) == n_undo, "戻すものが無いのに、undo が索引を退避した(打つたびに退避が増える)"
        # 手で元に戻してから undo: 索引だけを戻す。置き場が見えないとき(どちらの場所にも無い)は、索引を戻さない
        time.sleep(1.1)
        assert run(O.move, w1, w2, "--to", "c", "--apply") == "ok" and sound()
        real_rename(os.path.join(lib, "c", "w1.pdf"), w1)
        hidden = lib + ".hidden"
        real_rename(lib, hidden)                                 # 置き場が見えない(Drive が止まっている)
        before = open(O.INDEX).read()
        assert run(O.undo) == "ok" and open(O.INDEX).read() == before, "置き場が見えないのに、undo が索引だけを元のパスに戻した"
        real_rename(hidden, lib)
        assert run(O.undo) == "ok" and sound() and sorted(os.path.basename(r["folder"]) for r in load()) == ["a", "b"], \
            "手で元に戻したファイルの索引を、undo が戻さない"
        # 動かすものの無い rename --apply のあとでも、直前の move を undo で戻せる
        time.sleep(1.1)
        write(O.INDEX, [dict(r, filename=os.path.basename(r["path"])) for r in load()])
        cur = {r["doi"]: r["path"] for r in load()}
        assert run(O.move, cur["10.1/w1"], "--to", "c", "--apply") == "ok"
        O.filename_for, real_ff = (lambda r: os.path.splitext(r["filename"])[0]), O.filename_for   # どの記録も今の名前のまま(予定が空)
        try:
            assert run(O.rename, "--apply") == "ok"
        finally:
            O.filename_for = real_ff
        assert run(O.undo) == "ok" and {r["doi"]: r["path"] for r in load()} == cur and sound(), "予定が空の rename のあと、直前の move を undo で戻せない"
        # --- move: 大小文字だけが違う 2 件を同じフォルダへ。途中の失敗でも索引は合う
        for sub in ("a", "b", "c"):
            os.makedirs(os.path.join(lib, sub), exist_ok=True)
            for f in os.listdir(os.path.join(lib, sub)):
                os.remove(os.path.join(lib, sub, f))
        v1, v2, v3 = put("a", "Paper.pdf", "P1"), put("b", "paper.pdf", "P2"), put("b", "other.pdf", "P3")
        write(O.INDEX, [rec(v1, first_author="A", doi="10.1/5"), rec(v2, first_author="B", doi="10.1/6"), rec(v3, first_author="C", doi="10.1/7")])
        assert run(O.move, v1, v2, "--to", "c", "--apply") == "exit" and os.path.exists(v1) and os.path.exists(v2) and not os.listdir(os.path.join(lib, "c")), \
            "move が、大小文字だけが違う 2 件を同じフォルダへ動かそうとした"
        O._rename_excl = flaky_on(2)
        time.sleep(1.1)
        assert run(O.move, v1, v3, "--to", "c", "--apply") == "OSError" and sound(), "move が途中で失敗したとき、動かした分を索引に書いていない"
        O._rename_excl = real_excl
        # --- dedup: 同じ DOI の 3 件。移す 2 件の名前が大小文字だけ違う
        for sub in ("a", "b", "c"):
            for f in os.listdir(os.path.join(lib, sub)):
                os.remove(os.path.join(lib, sub, f))
        d1, d2, d3 = put("a", "keep.pdf", "KEEP-largest"), put("b", "Dup.pdf", "D2"), put("c", "dup.pdf", "D3")
        write(O.INDEX, [rec(d, first_author="A", doi="10.1/8") for d in (d1, d2, d3)])
        n_ren = len(glob.glob(os.path.join(root, "renames-*.jsonl")))
        assert run(O.dedup, "--apply") == "ok" and sound(), "dedup のあと、索引のパス・フォルダ・ファイル名が合わない(_2 を付けた名前など)"
        aside = [os.path.join(dp, f) for dp, _, fs in os.walk(lib) for f in fs if os.sep + "_" in dp]
        assert sorted(open(p).read() for p in aside) == ["D2", "D3"] and open(d1).read() == "KEEP-largest", \
            "dedup が、大小文字だけが違う 2 件を移して 1 件を上書きした(または止まった): %s" % aside
        assert len({O._same_name(os.path.basename(p)) for p in aside}) == 2, "dedup が、大小文字だけが違う 2 つの名前で移した: %s" % aside
        assert kept_b("dedup") and "duplicate_of" not in open(sorted(kept_b("dedup"))[0]).read(), "dedup --apply が、動かす前の索引を退避していない"
        assert glob.glob(os.path.join(root, "dedup-*.jsonl")) and len(glob.glob(os.path.join(root, "renames-*.jsonl"))) == n_ren, \
            "dedup の記録を renames-* に書いた(undo が dedup の記録を読んで、重複の移動を巻き戻す)"
        e1, e2, e3 = put("a", "e-keep.pdf", "E-KEEP-largest"), put("b", "e2.pdf", "E2"), put("c", "e3.pdf", "E3")
        write(O.INDEX, [rec(e, first_author="A", doi="10.1/9") for e in (e1, e2, e3)])
        O._rename_excl = flaky_on(2)
        time.sleep(1.1)
        assert run(O.dedup, "--apply") == "OSError" and sound() and sum(1 for r in load() if r.get("duplicate_of")) == 1, \
            "dedup が途中で失敗したとき、動かした分を索引に書いていない"
        O._rename_excl = real_excl
        # --- 同じファイルを指す記録が 2 行あっても重複ではない(重複とみなすと、唯一の実体を移してしまう)
        import shutil
        for sub in os.listdir(lib):                              # 前の場合の残り(移した重複のフォルダも)を片付ける
            shutil.rmtree(os.path.join(lib, sub))
        for sub in ("a", "b", "c"):
            os.makedirs(os.path.join(lib, sub))
        only = put("a", "only.pdf", "ONLY")
        alias = os.path.join(lib, "b", "alias.pdf"); os.symlink(only, alias)
        twin = os.path.join(lib, "a", "ONLY.pdf") if insensitive else only
        write(O.INDEX, [rec(only, first_author="A", doi="10.1/s"), rec(only, first_author="A", doi="10.1/s"),
                        rec(alias, first_author="A", doi="10.1/s"), rec(twin, first_author="A", doi="10.1/s")])
        before = open(O.INDEX).read()
        said = io.StringIO()
        O.sys.argv = ["organize.py", "dedup", "--apply"]
        try:
            with contextlib.redirect_stdout(said), contextlib.redirect_stderr(io.StringIO()):
                O.dedup()
        except OSError:                                          # 同じファイルを 2 度動かそうとして止まった場合も、下の assert で言う
            pass
        assert os.path.exists(only) and open(only).read() == "ONLY" and os.path.islink(alias) and open(O.INDEX).read() == before, \
            "dedup が、同じファイルを指す記録(同じパス・リンク・大小文字違い)を重複とみなして、実体かリンクを動かした"
        assert "同じファイルを指す記録" in said.getvalue() and "3 行" in said.getvalue(), "同じファイルを指す行があることを報告しない: %s" % said.getvalue()
        real_dup = put("c", "copy.pdf", "COPY-of-the-same-paper")
        write(O.INDEX, [rec(alias, first_author="A", doi="10.1/s"), rec(only, first_author="A", doi="10.1/s"), rec(only, first_author="A", doi="10.1/s"),
                        rec(twin, first_author="A", doi="10.1/s"), rec(real_dup, first_author="A", doi="10.1/s")])
        time.sleep(1.1)
        assert run(O.dedup, "--apply") == "ok"
        assert os.path.islink(alias) and len({r["path"] for r in load()}) == 2, \
            "同じファイルを別の書き方(リンク・大小文字)で指す行が、移した後に別々のパス(無いパス)を指す。またはリンクのほうを動かした: %s" % sorted({r["path"] for r in load()})
        left = sorted(open(os.path.join(dp, f)).read() for dp, _, fs in os.walk(lib) for f in fs if not os.path.islink(os.path.join(dp, f)))
        aside = [f for dp, _, fs in os.walk(lib) for f in fs if os.sep + "_" in dp]
        assert left == ["COPY-of-the-same-paper", "ONLY"] and len(aside) == 1 and sound(), \
            "同じファイルを指す 2 行と、本当の重複 1 つ: 動かすファイルは 1 つだけで、索引は合う: %s %s" % (left, aside)
        if not insensitive:                                      # 大小文字を区別するボリュームでは、Paper.pdf と paper.pdf は別のファイル = 重複
            for sub in os.listdir(lib):
                shutil.rmtree(os.path.join(lib, sub))
            os.makedirs(os.path.join(lib, "a"))
            k1, k2 = put("a", "Paper.pdf", "K1-largest"), put("a", "paper.pdf", "K2")
            write(O.INDEX, [rec(k1, first_author="A", doi="10.1/k"), rec(k2, first_author="A", doi="10.1/k")])
            time.sleep(1.1)
            assert run(O.dedup, "--apply") == "ok" and sum(1 for r in load() if r.get("duplicate_of")) == 1 and sound(), \
                "大小文字を区別するボリュームで、名前の大小文字だけが違う別々のファイルを「同じファイル」とみなした"
    finally:
        O._rename_excl = real_excl
    print("S ok: 動かす経路は別のファイルを上書きしない(大小文字だけが違う名前も)、途中で失敗しても索引は合う、"
          "同じ秒の記録を上書きしない、undo は元の場所の別のファイルを残す" + ("" if insensitive else "(このボリュームは大小文字を区別する)")
          + ("" if excl else "(この OS・ボリュームは、移動先があれば断る改名を使えない)"))


def check_search_db(d):
    """Q. 検索の索引を実際に作って引く(ベクトルは差し替える。Swift が要らない)。索引に入れる文字列と検索語に同じ正規化が
    かかっていること、作り方の古い索引を作り直すこと、トークナイザが使えない SQLite でも索引が作れることを見る。"""
    import contextlib, io, sqlite3
    O.ROOT, O.INDEX, O.DB = d, os.path.join(d, "s.jsonl"), os.path.join(d, "s.db")
    rec = lambda name, **kw: dict({"path": "/f/%s.pdf" % name, "folder": "/f", "rung": "unresolved"}, **kw)
    write(O.INDEX, [rec("accent", authors=["Jürgen Müller"]), rec("plain", authors=["J Muller"]),
                    rec("nfd", text="Mu\u0308ller et al."), rec("lig", text="electric \ufb01eld"),
                    rec("wide", text="\uff33\uff28\uff27\u7d20\u5b50"), rec("kana", text="\uff7a\uff8b\uff70\uff9a\uff9d\uff84\u5236\u5fa1"),
                    rec("short", text="\uff33\uff49 \u57fa\u677f"),
                    rec("li", first_author="Li", authors=["Li Wei", "Zhang San"]), rec("lin", first_author="Lin", authors=["Lin Bo", "Oliver Kohli"]),
                    rec("li2", first_author="Zhang", authors=["Zhang San", "\uff2c\uff49"]),
                    rec("abs", abstract="\u5e72\u6e09 \u6e2c\u5b9a\u306e\u5831\u544a"), rec("ttl", title="\u504f\u5149 \u89e3\u6790\u306e\u624b\u6cd5"),
                    rec("wrap", text="\u30b9\u30da\u30af \u30c8\u30eb\u89e3\u6790"), rec("loose", text="A. Garci\u00b4a and B. Pe\u00b4rez")])
    real = (O._embed, O.TOKENIZERS)
    O._embed = lambda texts: [None] * len(texts)
    quiet = contextlib.redirect_stderr(io.StringIO())
    find = lambda c, q: sorted(os.path.basename(p)[:-4] for p in O._fts(c, q, 10))
    try:
        with quiet:
            c = O._db()
        assert O._db_format_ok(), "作った検索の索引に、作り方の版の印が無い(検索のたびに作り直すことになる)"
        try:                                                 # この SQLite の trigram がアクセントを畳めるか(古い版は畳めない)
            sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram remove_diacritics 1')")
            folds = True
        except sqlite3.OperationalError:
            folds = False
        if folds:
            assert find(c, "Müller") == find(c, "Muller") == ["accent", "nfd", "plain"], \
                "アクセントの有無で別の語になっている: %s / %s" % (find(c, "Müller"), find(c, "Muller"))
        else:
            assert find(c, "Müller") == ["accent", "nfd"], "合成形と分解形が別の語になっている: %s" % find(c, "Müller")
        assert find(c, "field") == ["lig"], "合字(ﬁ)で載っている語に当たらない"
        assert find(c, "SHG") == find(c, "\uff33\uff28\uff27") == ["wide"], "全角の英数字で載っている語に当たらない"
        assert find(c, "コヒーレント") == ["kana"], "半角の片仮名で載っている語に当たらない"
        assert find(c, "Si") == find(c, "\uff33\uff49") == ["short"], "3 文字以上の語が無い入力が、全角で載っている語に当たらない"
        assert find(c, "基板 制御") == ["kana", "short"], "3 文字以上の語が無い入力を語ごとに探していない"
        assert find(c, "author:Li") == ["li", "li2"], "2 文字の姓を著者の列で語として引けない(Lin・Oliver・Kohli に当たる、欄の端の Li を落とす): %s" % find(c, "author:Li")
        assert find(c, "first_author:Li") == ["li"] and find(c, "author:Lin") == ["lin"], "第一著者の列・3 文字以上の姓"
        assert find(c, "スペクトル解析") == ["wrap"], "行の折り返しで空白が入った語に当たらない"
        assert find(c, "干渉測定") == ["abs"] and find(c, "偏光解析") == ["ttl"], "抄録・表題の仮名・漢字の間の空白を詰めていない"
        assert find(c, '"スペク トル解析"') == ["wrap"] and find(c, 'text:"スペク トル解析"') == ["wrap"], \
            "空白入りの引用符つきの句が、空白を詰めた索引に当たらない"
        assert find(c, "Garcia") == find(c, "Perez") == ["loose"], "字の間に離れて載るアクセント記号で語が切れている"
        c.close()
        for old in ("1", ""):                                # 作り方の古い索引・版の印が無い索引は、索引より新しくても作り直す
            stub_db(O.DB, old)
            with quiet:
                c = O._db()
            c.close()
            assert O._db_format_ok(), "作り方の古い検索の索引(版 %r)を作り直さない" % old
        O.TOKENIZERS = ["no_such_tokenizer", "trigram"]      # 最初の候補を知らない SQLite
        os.remove(O.DB)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                c = O._db()
            got = find(c, "field"); c.close()
        except sqlite3.OperationalError as e:
            got = e
        assert got == ["lig"], "トークナイザが使えないとき、次の候補で作らない: %s" % got
        assert "no_such_tokenizer" in err.getvalue(), "トークナイザを黙って替えている(アクセントを畳まない索引になったことが分からない)"
        O.TOKENIZERS = ["no_such_tokenizer"]
        os.remove(O.DB)
        try:
            with quiet:
                O._db()
            raised = False
        except sqlite3.OperationalError:
            raised = True
        assert raised, "どのトークナイザも使えないのに、検索の索引ができたことにしている"
    finally:
        O._embed, O.TOKENIZERS = real
    print("Q ok: 検索の索引と検索語に同じ正規化がかかる、作り方の古い索引は作り直す、トークナイザが無ければ次の候補で作る"
          + ("" if folds else "(この SQLite はアクセントを畳めない)"))


def check_fulltext(d):
    """T. 全頁の本文と、それを入れた検索の索引。PDF の読み取り(pdftotext・PDFKit・OCR)とベクトルは差し替える。
    鍵は中身から作る(改名で変わらない)、読んだ本文は読み直さない、読めないファイルは置かない、テキスト層の乏しい
    ファイルは全頁を OCR する、検索の索引は別名で作ってから置き換える、順位は「今の表で当たる記録が先」、種類で絞れる。"""
    import contextlib, io
    root = os.path.join(d, "t"); lib = os.path.join(root, "lib"); os.makedirs(lib)
    O.ROOT, O.INDEX, O.DB, O.FULLTEXT = root, os.path.join(root, "t.jsonl"), os.path.join(root, "t.db"), os.path.join(root, "fulltext")
    f = lambda name: os.path.join(lib, name + ".pdf")
    long = "ordinary sentence about optics. " * 40
    # ファイルの中身(偽物) → pdftotext が返すもの。slow は時間切れ、bad は失敗、scan と blank はテキスト層が乏しい
    cover = "this first page has a longer text layer than its OCR"
    layer = {"good": (long + "\f" + long + " zyxwvut gadget gadget gadget QZ \u30b9\u30da\u30af \u30c8\u30eb\u89e3\u6790 \uff3a\uff25\uff22\uff32\uff21\f", None),
             "slow": ("", "timed out after 30 s"),
             "bad": ("", "exit status 1"), "scan": (cover + "\f\f\f", None), "blank": ("\f\f", None), "ocrfail": ("\f\f", None),
             "long": (("filler words without the term. " * 3000) + "\f", None), "book": (long + " gadget\f", None)}
    kit = {"slow": (long + " viakit\f", None), "bad": ("", "exit status 1")}
    for name in layer:
        open(f(name), "w").write("%PDF " + name)
    rec = lambda name, **kw: dict({"path": f(name), "folder": lib, "filename": name + ".pdf", "rung": "unresolved", "text": ""}, **kw)
    recs = [rec("good"), rec("slow"), rec("bad"), rec("scan"), rec("blank"), rec("ocrfail"), rec("long", text="one gadget here"),
            rec("book", not_paper="user", kind="book", title="Handbook QZ"),
            dict(rec("dup", text="uniqdupword"), path=os.path.join(root, "_dup", "dup.pdf"), duplicate_of=f("good"))]   # 移した重複(ファイルは無い)
    write(O.INDEX, recs)
    real = (O._run_text, O._ptext, O._ocr_pages, O._vocr, O._embed, O.OCR_CHUNK)
    ran, ocred = [], []
    name_of = lambda path: open(path).read().split()[-1]      # 中身から(パスは改名で変わる)
    def run_text(cmd, timeout):
        tool = "pdftotext" if cmd[0] == "pdftotext" else "pdfkit"
        ran.append((tool, name_of(cmd[1])))
        return (layer if tool == "pdftotext" else kit)[name_of(cmd[1])]
    def ocr_pages(path, first, last):                         # scan: 1 頁目だけテキスト層より短い。blank: 画像にはできたが字が無い。ocrfail: 画像にできない
        ocred.append((name_of(path), first, last))
        return {"scan": {i: "short" if i == 1 else "scanned page %d wombat" % i for i in range(first, last + 1)},
                "blank": {i: "" for i in range(first, last + 1)}}.get(name_of(path), {})
    O._run_text, O._ptext, O._ocr_pages, O._vocr, O.OCR_CHUNK = run_text, lambda: "ptext", ocr_pages, lambda: None, 2
    O._embed = lambda texts: [None] * len(texts)
    quiet = contextlib.redirect_stderr(io.StringIO())
    entry = lambda name: O._fulltext_load(O._file_key(f(name)))
    try:
        # 鍵: 中身から作る
        k = O._file_key(f("good"))
        os.rename(f("good"), f("renamed"))
        assert O._file_key(f("renamed")) == k, "全頁の本文の鍵が改名で変わる(改名のたびに読み直すことになる)"
        os.rename(f("renamed"), f("good"))
        assert O._file_key(f("slow")) != k and O._file_key(f("nofile")) is None, "中身の違うファイルが同じ鍵になる、または無いファイルに鍵が付く"
        big = [os.path.join(root, "big%d.bin" % i) for i in (1, 2)]
        for path, end in zip(big, "AB"):
            open(path, "w").write("x" * 200000 + end)
        assert O._file_key(big[0]) != O._file_key(big[1]), "先頭の 64 KB と大きさが同じで、末尾だけが違うファイルが同じ鍵になる"
        # 読む(OCR なし)。コマンドとして呼ぶ: papers fulltext --no-ocr
        before = open(O.INDEX).read()
        O.sys.argv = ["organize.py", "fulltext", "--no-ocr"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            O.fulltext()
        assert open(O.INDEX).read() == before, "全頁の本文を読んで papers.jsonl を書き換えた"
        assert "新しく読んだ 7 件" in out.getvalue() and "OCR 待ち 3 件" in out.getvalue() and "bad.pdf" in err.getvalue(), \
            "papers fulltext --no-ocr の報告(読めた数・OCR 待ち・読めなかったファイル)が違う: %s %s" % (out.getvalue(), err.getvalue())
        assert O._db_format_ok(), "papers fulltext が検索の索引を作り直していない"
        assert len(entry("good")["pages"]) == 2 and entry("good")["how"] == "pdftotext", "頁に分けて置いていない"
        assert entry("slow")["how"] == "pdfkit" and "viakit" in entry("slow")["pages"][0], "pdftotext が時間切れのとき、PDFKit で読んでいない"
        assert entry("bad") is None, "読めなかったファイルを、空の本文として置いた(読めなかったことが分からなくなる)"
        assert entry("scan")["thin"] and len(entry("scan")["pages"]) == 3 and not ocred, "--no-ocr で OCR した、または OCR 待ちの印が無い"
        # もう 1 度: 置いてある本文は読み直さない。読めなかったファイルだけをまた読む
        del ran[:]
        with quiet:
            got, failed = O.fulltext_fill(recs, do_ocr=False)
        assert sorted(ran) == [("pdfkit", "bad"), ("pdftotext", "bad")] and got["read"] == 0, "置いてある本文を読み直した: %s" % ran
        # OCR: テキスト層の乏しいファイルだけ、全頁を、区切って
        with quiet:
            got, failed = O.fulltext_fill(recs)
        assert sorted(ocred) == [("blank", 1, 2), ("ocrfail", 1, 2), ("scan", 1, 2), ("scan", 3, 3)], \
            "乏しいファイルの全頁を、区切って OCR していない: %s" % ocred
        assert entry("scan")["how"] == "ocr" and entry("scan")["pages"][2] == "scanned page 3 wombat" and not entry("scan")["thin"], \
            "OCR の結果を頁ごとに置いていない"
        assert entry("scan")["pages"][0] == cover, "テキスト層のほうが字の多い頁を、OCR の結果で置き換えた"
        assert got["ocr"] == 1 and got["ocr_pages"] == 2, "OCR した件数・頁数の報告が違う: %s" % got
        assert entry("blank")["how"] == "pdftotext" and not entry("blank")["thin"], "OCR しても増えないファイルの、待ちの印を外していない"
        assert entry("ocrfail")["thin"] and got["pending"] == 1 and sorted(os.path.basename(p) for p, _ in failed) == ["bad.pdf", "ocrfail.pdf"], \
            "頁を画像にできなかったファイルを、OCR 済みにした(二度と OCR されない)、または報告していない: %s %s" % (got, failed)
        del ocred[:]
        with quiet:
            O.fulltext_fill(recs)
        assert ocred == [("ocrfail", 1, 2)], "OCR 済みのファイルをまた OCR した、または失敗したファイルを OCR し直していない: %s" % ocred
        # 置き方の版が違う本文は使わず、読み直す
        kb = O._file_key(f("book"))
        write(O._fulltext_path(kb), [{"v": O.FULLTEXT_V - 1, "how": "pdftotext", "pages": ["old layout"], "thin": False}])
        assert O._fulltext_load(kb) is None, "置き方の版が違う本文を使っている"
        with quiet:
            O.fulltext_fill(recs)
        assert "gadget" in entry("book")["pages"][0], "置き方の版が違う本文を読み直していない"
        # 検索の索引: 全頁の本文が入り、今の表で当たる記録が先に並ぶ
        find = lambda c, q, n=10: [os.path.basename(p)[:-4] for p in O._fts(c, q, n)]
        with quiet:
            c = O.build_db()                                 # 上の fulltext_fill は直に呼んだので、作り直しも直に(コマンドなら自分で作り直す)
        assert find(c, "zyxwvut") == ["good"] and find(c, "wombat") == ["scan"] and find(c, "viakit") == ["slow"], \
            "全頁の本文(テキスト層・OCR・PDFKit)の語で引けない"
        # long は先頭の本文に 1 度だけ gadget があり、全頁の本文がとても長い。1 つの表の bm25 なら good・book より下になる
        assert find(c, "gadget") == ["long", "good", "book"], "今の表で当たる記録を先に、全頁の本文でだけ当たる記録を後ろに並べていない: %s" % find(c, "gadget")
        assert find(c, "gadget", 1) == ["long"] and find(c, "gadget", 2) == ["long", "good"], "件数を、今の表で当たる記録から数えて切っていない"
        assert find(c, "handbook gadget", 3) == ["book", "long", "good"], \
            "全頁の本文の側を、今の表で当たった分だけ多めに引いていない(上位が重なると足りなくなる): %s" % find(c, "handbook gadget", 3)
        assert find(c, "スペクトル解析") == find(c, "ZEBRA") == ["good"] and find(c, 'fulltext:"スペク トル解析"') == ["good"], \
            "全頁の本文に、索引と同じ正規化(仮名・漢字の間の空白・全角)がかかっていない"
        assert find(c, "fulltext:zyxwvut") == ["good"] and find(c, "fulltext:gadget AND title:Handbook") == ["book"], \
            "fulltext: の列を名指しした式で引けない"
        assert find(c, "QZ") == ["book", "good"], "3 文字以上の語が無い入力が全頁の本文を見ていない、または表題で当たる記録を先に並べていない: %s" % find(c, "QZ")
        assert c.execute("SELECT v FROM meta WHERE k='fulltext'").fetchone()[0] == "7", "全頁の本文のある件数を記録していない"
        c.close()
        # 種類で絞る。全頁の本文でだけ当たった記録の前に、区切りの 1 行を出す
        def search(*args):
            O.sys.argv = ["organize.py", "search"] + list(args)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), quiet:
                O.search()
            return out.getvalue()
        names = lambda out: [l.split("/")[-1][:-4] for l in out.splitlines() if l.startswith("    /")]      # パスの行
        assert names(search("gadget")) == ["long", "good", "book"]
        assert names(search("gadget", "--kind=book")) == ["book"] and names(search("gadget", "--kind=paper")) == ["long", "good"], \
            "--kind で種類を絞っていない"
        assert names(search("gadget", "--kind=book,paper", "--n=2")) == ["long", "good"], "--kind の複数指定・件数"
        assert names(search("gadget", "--kind=book", "--n=1")) == ["book"], "--kind で外す分を見込んで多めに引いていない(小さい --n で 0 件になる)"
        assert names(search("uniqdupword")) == [] and names(search("uniqdupword", "--all")) == ["dup"], "移した重複を出している、または --all で出さない"
        MARK = "match only when the full text is searched"
        out = search("gadget")
        assert out.count(MARK) == 1 and out.index(MARK) > out.index("long.pdf") and out.index(MARK) < out.index("good.pdf"), \
            "全頁の本文でだけ当たった記録の前に、区切りを 1 度だけ出していない"
        marks = lambda *args: [(r["filename"], r.get("matched")) for r in map(json.loads, search(*args).splitlines())]
        assert marks("gadget", "--json") == [("long.pdf", None), ("good.pdf", "fulltext"), ("book.pdf", "fulltext")], \
            "--json で、全頁の本文でだけ当たった記録に印を付けていない: %s" % marks("gadget", "--json")
        out = search("gadget", "--full")
        assert out.count("matches only when the full text is searched") == 2 and out.index("matches only") > out.index("good.pdf"), \
            "--full で、全頁の本文でだけ当たった記録に印を付けていない"
        # 印は「全頁の本文を入れて初めて当たる」記録だけ: 表題でも当たる記録・fulltext: を名指しした式・fts 以外のモードには付けない
        assert marks("QZ", "--json") == [("book.pdf", None), ("good.pdf", "fulltext")] and search("QZ").count(MARK) == 1, \
            "3 文字以上の語が無い入力で、全頁の本文でだけ当たった記録に印を付けていない: %s" % marks("QZ", "--json")
        assert marks("title:Handbook OR fulltext:nosuchword", "--json") == [("book.pdf", None)] \
            and MARK not in search("fulltext:gadget"), "fulltext: を名指しした式で、表題で当たった記録にも「全頁の本文でだけ」の印を付けた"
        real_vec = O._vec
        O._vec = lambda c, q, n: [f("slow"), f("good")]       # ベクトルで来た記録(slow は語を含まない、good は全頁の本文にだけ含む)
        try:
            assert marks("gadget", "--mode=vec", "--json") == [("slow.pdf", None), ("good.pdf", None)] and MARK not in search("gadget", "--mode=vec"), \
                "--mode=vec で来た記録に「全頁の本文でだけ」の印を付けた"
            O._vec = lambda c, q, n: []
            assert names(search("gadget", "--mode=hybrid")) == ["long"], "--mode=hybrid の語の側が、全頁の本文でだけ当たる記録を足している(今までと順位が変わる)"
        finally:
            O._vec = real_vec
        try:
            search("gadget", "--kind=nope"); stopped = False
        except SystemExit:
            stopped = True
        assert stopped, "知らない --kind を黙って受けた"
        O.sys.argv = ["organize.py", "info", "--json"]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            O.info()
        assert json.loads(out.getvalue())["db_fulltext"] == 7, "info が全頁の本文のある件数を出していない"
        O.sys.argv = ["organize.py", "info"]                 # 読めていない記録(bad)がある → 注意を出す
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            O.info()
        assert "papers fulltext" in out.getvalue(), "全頁の本文の無い記録があるのに、info が注意を出していない"
        # 作り直しは別名で作ってから置き換える: 途中で止まっても前の索引が残り、作りかけは残らない
        good_db = open(O.DB, "rb").read()
        stale, busy = O.DB + ".1.part", O.DB + ".2.part"
        for part, age in ((stale, 7200), (busy, 0)):
            open(part, "w").write("x"); os.utime(part, (time.time() - age,) * 2)
        def boom(texts):
            raise RuntimeError("embed failed")
        O._embed = boom
        try:
            with quiet:
                O.build_db(); raised = False
        except RuntimeError:
            raised = True
        assert raised and open(O.DB, "rb").read() == good_db, "作り直しが途中で止まったとき、前の検索の索引が残っていない"
        assert sorted(glob.glob(O.DB + ".*")) == [busy], "作りかけを残した、古い作りかけを掃除していない、または作っている最中かもしれない作りかけを消した: %s" % glob.glob(O.DB + ".*")
        os.remove(busy)
        O._embed = lambda texts: [None] * len(texts)
        # 改名しても、全頁の本文は読み直さず、検索の索引に入る
        os.rename(f("good"), f("renamed"))
        recs[0].update(path=f("renamed"), filename="renamed.pdf")
        write(O.INDEX, recs)
        del ran[:]
        with quiet:
            O.fulltext_fill(recs)
            c = O._db()
        assert ("pdftotext", "good") not in ran and find(c, "zyxwvut") == ["renamed"], "改名したファイルの全頁の本文を読み直した、または索引に入らない"
        c.close()
        # ファイルが開けない(置き場が見えない・同期待ち)記録の全頁の本文は、前の検索の索引から引き継ぐ
        os.remove(f("slow"))
        write(O.INDEX, recs)
        with quiet:
            c = O._db()
        assert find(c, "viakit") == ["slow"], "ファイルが開けない記録の全頁の本文が、作り直しで消えた"
        c.close()
    finally:
        O._run_text, O._ptext, O._ocr_pages, O._vocr, O._embed, O.OCR_CHUNK = real
    print("T ok: 全頁の本文は中身の鍵で置く(改名で読み直さない)、読めないファイルは置かない、乏しいファイルは全頁を OCR、"
          "検索の索引は別名で作って置き換える、今の表で当たる記録が先、種類で絞れる")


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
        check_nonpaper(d)
        check_overwrite(d)
        check_search_db(d)
        check_fulltext(d)
    print("index_check: ok")
