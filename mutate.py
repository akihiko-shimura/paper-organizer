#!/usr/bin/env python3
"""壊して、検査が落ちるかを見る(規約 4)。作業用のコピーで行い、本物のファイルと索引には触れない。

    python3 mutate.py FILE 'OLD' 'NEW' -- CMD ...
    python3 mutate.py organize.py @old.txt @new.txt -- python3 organize.py selftest

OLD・NEW は文字列そのもの、または @ファイル(複数行の置換向け)。FILE の中で OLD が**ちょうど 1 件**
当たらなければ何もせず止まる(置換が当たっていないのに「通った」と読む罠を防ぐ)。
コピーには *.py・vocr・vec・索引の複製を置き、.cache は共有する(通信を増やさない)。CMD はコピーの中で
INDEX=コピーの索引 として走る。

終了コード: 0 = 検査が落ちた(壊れを捕まえた)/ 1 = 検査が通った(壊しても落ちない = 発見。検査を足す)/
2 = 置換が 1 件でない。

selftest に届かない壊し方(_resolve の中など、PDF やネットワークが要る箇所)は、次のどれかで落ちるようにする:
- 判定を純粋な関数に切り出して selftest で見る(例: title_only_in_citation、vet_verdict)
- 通信を差し替えて呼び出しを確かめる(例: Crossref の応答を差し替えて by_bibliographic を呼ぶ)
- 引数を必須にして、渡し忘れを実行時の例外にする(例: by_title(cand, text))
- それでも届かなければ ladder_check.py に実物の例を足す(Drive が要るので CI の外)
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def arg(s):
    return open(s[1:]).read() if s.startswith("@") else s


def main(argv):
    if "--" not in argv or argv.index("--") != 4:
        sys.exit(__doc__)
    target, old, new, cmd = argv[1], arg(argv[2]), arg(argv[3]), argv[5:]
    src = open(os.path.join(ROOT, target)).read()
    n = src.count(old)
    if n != 1:
        print("置換対象が %d 件(1 件であるべき)。何もしない" % n)
        return 2
    d = tempfile.mkdtemp(prefix="mutate-")
    for f in os.listdir(ROOT):
        if f.endswith((".py", ".swift")) or f in ("vocr", "vec"):
            shutil.copy2(os.path.join(ROOT, f), d)
    if os.path.exists(os.path.join(ROOT, "papers.jsonl")):
        shutil.copy2(os.path.join(ROOT, "papers.jsonl"), d)       # 本物の索引は書き換えさせない
    if os.path.isdir(os.path.join(ROOT, ".cache")):
        os.symlink(os.path.join(ROOT, ".cache"), os.path.join(d, ".cache"))
    open(os.path.join(d, target), "w").write(src.replace(old, new))
    diff = subprocess.run(["diff", os.path.join(ROOT, target), os.path.join(d, target)],
                          capture_output=True, text=True).stdout
    print("差分 %d 行(コピー: %s)" % (sum(1 for l in diff.splitlines() if l[:1] in "<>"), d))
    env = dict(os.environ, INDEX=os.path.join(d, "papers.jsonl"), PAPER_REPO=ROOT)
    r = subprocess.run(cmd, cwd=d, env=env, capture_output=True, text=True)
    tail = r.stdout.strip().splitlines()[-10:] + r.stderr.strip().splitlines()[-3:]
    print("\n".join("  " + l for l in tail))
    if r.returncode != 0:
        print("落ちた(exit %d): 検査は壊れを捕まえている" % r.returncode)
        return 0
    print("通った: 壊しても落ちない。この検査は対象を見ていない(規約 4 の「発見」)")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
