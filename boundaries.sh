#!/bin/sh
# design-blanks の表 1 の grep 一式と、どの表が要るかの機械的な判定(「小さい変更だから」と判断しない)。
#
#   sh boundaries.sh               # 境界ごとに、リポジトリ全体(*.py)の該当行を出す(表 1 を埋める材料)
#   sh boundaries.sh --diff [REV]   # 作業中の差分(既定: HEAD との差)だけに当て、要る表を出す
#
# 判定(--diff):
#   境界のパターンに当たる差分の行がある        → 表 1
#   正規表現・定数の一覧を足す/変える行がある    → 表 0
#   検査(assert・selftest・*_check.py・mutate.py)を足す/変える → 表 3
#   同定の関数(_resolve・by_*・from_crossref・from_arxiv・norm・corroborate・clean_text)の中を変える
#                                                → 同定の経路(ladder_check と rescan_check)
cd "$(dirname "$0")" || exit 1

BOUNDARIES='ネットワーク|_fetch(\|urlopen
索引・キャッシュの書き込み|open(INDEX\|save_index\|_atomic_write
ファイル移動|os\.rename\|shutil
失敗の握り潰し|except Exception
段の扱い|renamable\|needs_human\|dedup_key\|enrichable\|TERMINAL
外部の書誌文字列|clean_text\|from_crossref\|from_arxiv\|it\.get("title")\|m\.get("title")
有料の API|anthropic\|messages\.create\|batches'

if [ "$1" != "--diff" ]; then
    echo "$BOUNDARIES" | while IFS='|' read -r name pat; do
        echo "== $name"
        grep -n "$pat" ./*.py | sed 's/^/  /'
    done
    exit 0
fi

REV="${2:-HEAD}"
DIFF="$(git diff -U0 "$REV" -- '*.py')"
if [ -z "$DIFF" ]; then
    echo "差分なし($REV との比較、*.py)"; exit 0
fi
CHANGED="$(printf '%s\n' "$DIFF" | grep -E '^[+-]' | grep -vE '^(\+\+\+|---) ')"

need1=0
echo "$BOUNDARIES" | while IFS='|' read -r name pat; do
    n=$(printf '%s\n' "$CHANGED" | grep -c "$pat")
    [ "$n" -gt 0 ] && echo "  境界 $name: $n 行"
done > /tmp/boundaries.$$
[ -s /tmp/boundaries.$$ ] && need1=1
n0=$(printf '%s\n' "$CHANGED" | grep -cE "re\.compile|^[+-][A-Z_][A-Z0-9_]* *= *[\[({]|^[+-] +r'")
n3=$(printf '%s\n' "$DIFF" | grep -cE "^\+\+\+ b/.*(_check|mutate)\.py|^[+-] +assert |def selftest")
nid=$(printf '%s\n' "$DIFF" | grep -E '^@@' | grep -cE "def (_resolve|resolve|by_[a-z_]+|from_crossref|from_arxiv|_arxiv_record|norm|corroborate|clean_text|title_only_in_citation|_title_in_text)\(")

echo "差分($REV との比較、*.py)に当てた結果:"
[ "$need1" -eq 1 ] && { echo "表 1: 要る"; cat /tmp/boundaries.$$; } || echo "表 1: 境界に触れない(0 件)"
rm -f /tmp/boundaries.$$
[ "$n0" -gt 0 ] && echo "表 0: 要る(正規表現・定数の一覧を変える行 $n0)" || echo "表 0: 該当なし"
[ "$n3" -gt 0 ] && echo "表 3: 要る(検査を変える箇所 $n3)" || echo "表 3: 検査を変えない。足す検査があれば要る"
[ "$nid" -gt 0 ] && echo "同定の経路: 触る($nid か所)→ ladder_check.py と rescan_check.py" || echo "同定の経路: 触らない"
echo "表 2 は、機構(検査・判定・書き込みの経路)を足すなら常に要る"
