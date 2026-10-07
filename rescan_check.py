#!/usr/bin/env python3
"""同定の経路を変えたときの確認(design-blanks の必須手順)。索引には書かない。

    python3 rescan_check.py                 # 全件 3248 件で 7〜10 分(全件の解き直し 4 回の実測: 437・438・446・580 秒)
    python3 rescan_check.py --limit 30      # 無作為 30 件(道具自体の確認用)
    python3 rescan_check.py --base HEAD~1   # 対照にする版(既定 HEAD)

1. 今のコードで resolve() し直し、索引と (段, DOI か arXiv ID) を比べる
2. 変わった記録を、対照の版の organize.py でも解き直す。対照でも同じに変わるなら、今回の変更ではなく
   揺れ(キャッシュの取りこぼし・別の過去の変更)なので分けて出す
3. 今回の変更による変化を「新しく解決 / 外れた / 解決済みのまま同定が変わった / 未解決のまま」に分けて並べる

誤りはどの群にも隠れる(規約 3)。**新しく解決・外れた・同定が変わった、は 1 件ずつ中身を見る。**
過去の実例: 新しく解決の中に参考文献の論文への誤同定、外れた中に古い誤りの訂正、
同定が変わった中に同じ著者の別論文(arXiv 版が出版された別の論文に着いた)。
結果の JSON は --out(既定: 一時ディレクトリ)に書く。
"""
import json, os, random, shutil, subprocess, sys, tempfile

import organize as O

RESOLVED = lambda rung: rung not in ("unverified", "unresolved", "supplement", "error", None)
WORKER = """
import json, sys
import organize as O
for p in json.load(sys.stdin):
    try:
        r = O.resolve(p); out = {"rung": r["rung"], "id": (r.get("doi") or r.get("arxiv_id") or "").lower()}
    except BaseException as e:
        out = {"rung": "EXC", "id": repr(e)[:100]}
    print(json.dumps({"path": p, **out}, ensure_ascii=False), flush=True)
"""


def resolve_with(code_dir, paths):
    """code_dir の organize.py で paths を解く(別プロセス)。"""
    r = subprocess.run([sys.executable, "-c", WORKER], cwd=code_dir, input=json.dumps(paths),
                       capture_output=True, text=True)
    return {x["path"]: x for x in map(json.loads, r.stdout.splitlines())}


def base_copy(rev):
    """対照の版の organize.py を一時ディレクトリに取り出す(.cache は共有、vocr 等は今のもの)。"""
    d = tempfile.mkdtemp(prefix="rescan-base-")
    repo = os.environ.get("PAPER_REPO", O.ROOT)          # mutate.py のコピーの中では元のリポジトリを使う
    src = subprocess.run(["git", "show", "%s:organize.py" % rev], cwd=repo, capture_output=True, text=True)
    if src.returncode != 0:
        sys.exit("対照の版 %s の organize.py を取り出せない: %s" % (rev, src.stderr.strip()))
    open(os.path.join(d, "organize.py"), "w").write(src.stdout)
    for f in ("vocr", "vocr.swift", "vec", "vec.swift"):
        if os.path.exists(os.path.join(O.ROOT, f)):
            shutil.copy2(os.path.join(O.ROOT, f), d)
    os.symlink(O.CACHE, os.path.join(d, ".cache"))
    return d


def main(argv):
    limit = int(next((a.split("=")[1] for a in argv if a.startswith("--limit=")), 0) or
                (argv[argv.index("--limit") + 1] if "--limit" in argv else 0))
    rev = argv[argv.index("--base") + 1] if "--base" in argv else "HEAD"
    out = argv[argv.index("--out") + 1] if "--out" in argv else os.path.join(tempfile.mkdtemp(), "rescan.json")
    recs = {r["path"]: r for r in O.load_index()[0]}
    paths = sorted(recs)
    if limit:
        paths = random.Random(20260923).sample(paths, min(limit, len(paths)))
    print("今のコードで %d 件を解き直す" % len(paths), file=sys.stderr)
    new = resolve_with(O.ROOT, paths)
    key = lambda x: (x.get("rung"), (x.get("doi") or x.get("arxiv_id") or x.get("id") or "").lower())
    changed = [p for p in paths if key(recs[p]) != key(new.get(p, {}))]
    print("索引と違う %d 件を、対照の版(%s)でも解き直す" % (len(changed), rev), file=sys.stderr)
    base = resolve_with(base_copy(rev), changed) if changed else {}
    groups = {"新しく解決": [], "外れた": [], "解決済みのまま同定が変わった": [], "未解決のまま変化": [], "揺れ(対照でも同じ)": [], "例外": []}
    for p in changed:
        o, n, b = recs[p], new.get(p, {"rung": "EXC"}), base.get(p, {})
        row = {"file": o["filename"], "path": p, "old": list(key(o)), "new": list(key(n)), "base": list(key(b))}
        if n["rung"] == "EXC":
            g = "例外"
        elif key(b) == key(n):
            g = "揺れ(対照でも同じ)"
        elif not RESOLVED(o["rung"]) and RESOLVED(n["rung"]):
            g = "新しく解決"
        elif RESOLVED(o["rung"]) and not RESOLVED(n["rung"]):
            g = "外れた"
        elif RESOLVED(o["rung"]):
            g = "解決済みのまま同定が変わった"
        else:
            g = "未解決のまま変化"
        groups[g].append(row)
    json.dump(groups, open(out, "w"), ensure_ascii=False, indent=1)
    print("n=%d / 索引と違う %d" % (len(paths), len(changed)))
    for g, rows in groups.items():
        print("  %-22s %d" % (g, len(rows)))
        for r in rows[:50]:
            print("    %-40s %s → %s" % (r["file"][:40], "/".join(r["old"]), "/".join(r["new"])))
    print("詳細: %s。新しく解決・外れた・同定が変わった、は 1 件ずつ中身を見ること" % out)


if __name__ == "__main__":
    main(sys.argv)
