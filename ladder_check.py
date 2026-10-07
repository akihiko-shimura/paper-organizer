#!/usr/bin/env python3
"""同定の段の順序の検査。手元の実在の PDF で resolve() を通す(ネットワークと PDF が要るので CI 外)。

検査する PDF は ladder_cases.json(追跡しない。自分の蔵書のファイル名が入るため)に書く。書式は
ladder_cases.example.json: [ファイル名の先頭, 期待する DOI(どこにも着かないのが正解なら null), 期待する段, メモ]。
過去に誤って同定された PDF を足していく。例:
  - 段4(表題の検索)が先だと、同年・同著者の予稿や、同誌・同著者の翌年の別論文に着くもの
  - 同じ表題の後年の書籍章に着いていたもの(corroborate 導入の契機)
  - 表紙のある PDF(表紙の書誌で裏が取れるもの、表紙に他の論文が並ぶもの)
  - 1 ページ目が前の論文の参考文献から始まるもの、arXiv の刻印があるもの
段4 を段6 の前に戻すと最初の型が落ちる(規約 4: 確認済み)。corroborate を緩めても落ちない(段6 が先なので
裏取りに頼らず正しい版に着く。それは selftest が捕まえる)。   実行: python3 ladder_check.py
"""
import glob, json, os, sys
import organize as O

CASES_FILE = os.path.join(O.ROOT, "ladder_cases.json")
if not os.path.exists(CASES_FILE):
    print("ladder_check: 検査していない(%s が無い。書式は ladder_cases.example.json)" % CASES_FILE)
    sys.exit(0)
CASES = [tuple(c[:3]) for c in json.load(open(CASES_FILE))]   # (ファイル名の先頭, 期待する DOI, 期待する段)

paths = {r["filename"]: r["path"] for r in map(json.loads, open(O.INDEX)) if r.get("path")}
# 一括 rename の後も元の名前で引けるように、rename の記録を古い順にたどり、元の名前 → 今のパスを足す
cur = {}                                                   # 今のパス → 元の名前
for t in sorted(glob.glob(os.path.join(O.ROOT, "renames-*.jsonl"))):
    for l in open(t):
        m = json.loads(l)
        cur[m["to"]] = cur.pop(m["from"], os.path.basename(m["from"]))
paths.update({orig: p for p, orig in cur.items()})
bad = 0
for prefix, want, rung in CASES:
    fn = next((k for k in paths if k.startswith(prefix)), None)
    if not fn or not os.path.exists(paths[fn]):
        print("  SKIP %s (索引か Drive に無い)" % prefix)   # 無言で通さない: SKIP も失敗扱い
        bad += 1
        continue
    r = O.resolve(paths[fn])
    ok = (r.get("doi") or "").lower() == (want or "") and r["rung"] == rung
    bad += not ok
    print("  %s %-34s %-12s %s" % ("ok  " if ok else "FAIL", fn[:34], r["rung"], r.get("doi")))
print("ladder_check: %s" % ("ok" if not bad else "%d 件失敗" % bad))
sys.exit(1 if bad else 0)
