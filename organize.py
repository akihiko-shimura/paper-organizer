#!/usr/bin/env python3
"""Resolve local paper PDFs to authoritative metadata (Crossref / arXiv) and index them.

    python3 organize.py scan [dir]     # build papers.jsonl (resumable, cached)
    python3 organize.py stats          # ladder coverage from the index

Design: identification is a *lookup*, never a generation.  Every rung below produces a
candidate identifier or title; the authority (Crossref/arXiv) produces the field values.
An unverifiable candidate becomes 'unresolved' rather than a guess.
"""
import collections, difflib, hashlib, html, json, os, re, shutil, sqlite3, subprocess, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

CODE = os.path.dirname(os.path.realpath(__file__))   # ~/.local/bin/papers などのリンク経由でも本体の場所
# DB(索引・検索の索引・全頁の本文・キャッシュ・退避・作業用の表)を置くフォルダ。既定は本体と同じ場所。
# 別の DB は PAPERS_HOME で指す: PAPERS_HOME=<フォルダ> papers add <PDF のフォルダ>
ROOT = os.path.realpath(os.path.expanduser(os.environ["PAPERS_HOME"])) if os.environ.get("PAPERS_HOME") else CODE
if not os.path.isdir(ROOT):
    sys.exit("PAPERS_HOME is not a folder: %s (create it first: mkdir -p <folder>)" % ROOT)


def _config(d):
    """フォルダ d の config.json(追跡しない)。無ければ空で、どれも省ける。
      mailto  Crossref・OpenAlex に名乗る連絡先(「礼儀正しい枠」に入る)。環境変数 PAPERS_MAILTO が優先
      views   papers views の出力先(既定 ~/PaperViews)
      name    画面に出す DB の名前(既定は論文の置き場のフォルダ名。DB のフォルダの config.json だけを見る)
      dbs     papers gui が一緒に開く、ほかの DB の並び。[{"name": 名前, "home": その DB のフォルダ}](DB のフォルダの config.json だけを見る)
      books   true なら書籍の DB(DB のフォルダの config.json だけを見る。DESIGN.md「書籍の DB」)
      exclude add・scan が見ないフォルダの名前の並び(渡したフォルダの直下の名前。DB のフォルダの config.json だけを見る)"""
    try:
        with open(os.path.join(d, "config.json")) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


# 利用者ごとの値は本体の場所の config.json。別の DB(PAPERS_HOME)のフォルダにも config.json があれば、その値が先
CONFIG = dict(_config(CODE), **(_config(ROOT) if ROOT != CODE else {}))
MAILTO = os.environ.get("PAPERS_MAILTO") or CONFIG.get("mailto") or ""
# DB ごとの指定は、その DB のフォルダの config.json だけから読む(本体の場所の値を、別の DB が引き継がない)
BOOKS = bool(_config(ROOT).get("books"))
EXCLUDE = [str(x) for x in _config(ROOT).get("exclude") or []]
INDEX = os.environ.get("INDEX") or os.path.join(ROOT, "papers.jsonl")
CACHE = os.path.join(ROOT, ".cache")
OCR_LANG = os.environ.get("OCR_LANG", "ja-JP,en-US")   # 順序が精度を決める(DESIGN.md「段5: 画像PDFの OCR」)

DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"<>,;\]}]+')   # ')' は除外しない(旧Elsevier PII)
ARX_TEXT = re.compile(r'arXiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?', re.I)  # prefix required: a bare
ARX_NEW = re.compile(r'^(\d{4}\.\d{4,5})(?:v\d+)?', re.I)  # 4.5-digit run is often a page range
ARX_OLD = re.compile(r'(?:arXiv:)?\s*([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', re.I)
# arXiv が PDF の余白に刷る刻印(ID・版・分野・日付)。この PDF が arXiv 版であることの証拠。
# 参考文献中の引用(`arXiv:1234.5678 [cond-mat]`)は版や日付を持たないので当たらない。
# arXiv の固定の書式なので列挙でよい(規約 6: 閉じた集合)。当たり方の測定は DESIGN.md「プレプリント」。
ARX_STAMP = re.compile(r'arXiv:\s?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v\d+\s*\[[a-z.-]+\]\s+'
                       r'\d{1,2}\s+[A-Z][a-z]{2}\s+(?:19|20)\d\d')


def sh(cmd, timeout=40):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def norm(s):
    """照合用。NFKD で合字(ﬁ→fi)と全角を展開し、アクセントを外してから英数字だけ残す。
    PDF の本文はアクセントを分解形(n + ´)で持つことがあり、Crossref は合成形(ń)で返すので、同じ綴りにそろえる。
    和文は全部消える。測定は DESIGN.md「照合用の正規化 `norm()`」。"""
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def title_ratio(a, b):
    """norm 済みの 2 つの表題の一致率。SequenceMatcher は貪欲な照合で、引数の順で値が変わる。どちらの順で見つけた一致も
    実在する共通部分なので、大きい方をとる。titles_agree と段4 by_title が使う。"""
    return max(difflib.SequenceMatcher(None, a, b).ratio(), difflib.SequenceMatcher(None, b, a).ratio())


# Crossref の文字列は HTML のまま来る: タイトルに MathML(<mml:math>…)や <i>、誌名に &amp;、
# `Laser &amp;amp; Photonics Reviews` のような二重エスケープまである。
# タグの形をしたものだけを消す。=Si<O_2 のような本物の不等号は残す。
_TAG = re.compile(r'</?[A-Za-z][\w:.-]*(\s[^<>]*)?/?>')
_MATH = re.compile(r'</?(mml:)?math(\s[^<>]*)?>')


def clean_text(s):
    """外部 API の書誌文字列を人が読む形に戻す。比較(段4・段6)と保存の両方がここを通る。"""
    if not s:
        return s
    for _ in range(3):                         # 二重・三重のエスケープ
        u = html.unescape(s)
        if u == s:
            break
        s = u
    s = _MATH.sub(" ", s)                      # 数式の前後は語の切れ目にする
    s = _TAG.sub("", s)
    return " ".join(s.split())


def _title_in_text(title, text_norm):
    """段6 の採用条件: 整えたタイトルが本文に literal に含まれる。"""
    t = norm(clean_text(title))
    return len(t) > 25 and t in text_norm


# ---------- network: 礼儀とリトライを集約する共有層 ----------
# ネットワークに出る経路は get_json と from_arxiv の 2 つだけで、両方がここを通る。
# 新しい経路を足すときも必ず _fetch を通すこと(片方だけに検査を置かない)。
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY = 5          # OpenAlex の文書が示す値。初回+5回、待ちの合計は 31 秒
PACE_MAX = 5.0         # 429 で減速するときの 1 リクエストあたり上限(秒)
ABORT_AFTER = 3        # 連続でリトライを使い切った件数がこれに達したらパスを止める
_pace = {"mult": 1.0, "exhausted_in_a_row": 0}


class RateLimitAbort(BaseException):
    """連続でリトライを使い切った = 相手が受け付けていない。パス全体を止める。

    Exception ではなく BaseException を継承しているのは意図。from_crossref /
    by_title / by_bibliographic / from_arxiv / enrich はいずれも `except Exception`
    で失敗を握り潰すので、Exception 派生にすると中断が黙って無視される。
    「全ての捕捉箇所に再送出を書き足すのを忘れない」に頼らないための型選択。"""


def _retry_plan(status, attempt, retry_after=None):
    """(リトライするか, 待ち秒数) を返す純関数。ネットワークに触らないので selftest で見る。

    429 は「送りすぎ」、5xx は「相手の都合」。どちらも待てば直る。
    404/403 は待っても変わらないので即座に諦める(リトライしても無駄に叩くだけ)。
    待ちは 2**attempt = 1,2,4,8,16 秒。OpenAlex のエラー文書が示す値そのもの。
    Retry-After が来ていればサーバ自身の指示なので、こちらの推測より優先する。"""
    if status not in RETRY_STATUS or attempt >= MAX_RETRY:
        return False, 0.0
    if retry_after is not None:
        try:
            return True, max(0.0, min(float(retry_after), 300.0))
        except (TypeError, ValueError):
            pass                      # 日付形式などは解釈せず既定のバックオフに落とす
    return True, float(2 ** attempt)


_KEY = {}


def _openalex_key():
    """OpenAlex の API キー。環境変数 OPENALEX_API_KEY、無ければ macOS のキーチェーン(openalex-api-key)。
    無ければ None(キー無しの枠で動く)。一度だけ引く。"""
    if "openalex" not in _KEY:
        k = os.environ.get("OPENALEX_API_KEY")
        if not k:
            try:
                k = subprocess.run(["security", "find-generic-password", "-s", "openalex-api-key", "-w"],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
            except Exception:
                k = None
        _KEY["openalex"] = k or None
    return _KEY["openalex"]


def _auth_headers(url):
    """送り先ごとの認証ヘッダ。キーは URL に入れない(応答のキャッシュのファイル名は URL から作る)。
    OpenAlex のキーは openalex.org のサインインも兼ねるので、api.openalex.org 以外には送らない。"""
    if urllib.parse.urlsplit(url).hostname == "api.openalex.org" and _openalex_key():
        return {"Authorization": "Bearer " + _openalex_key()}
    return {}


def _fetch(url, pace=0.3, timeout=30):
    """本文を bytes で返す。429/5xx はバックオフして再送する。

    429 は個別のリクエストの事故ではなく**クライアント全体に向けた信号**なので、
    その 1 件を叩き直すだけでなく _pace を上げてパス全体を減速させる。
    「送る量を減らせ」と言われた直後に 5 倍叩き返すのを避けるため。
    503 は相手の都合なので再送はするが減速はしない(こちらのペースが原因ではない)。"""
    for attempt in range(MAX_RETRY + 1):
        try:
            req = urllib.request.Request(
                url, headers=dict({"User-Agent": "paper-organizer (mailto:%s)" % MAILTO if MAILTO else "paper-organizer"},
                                  **_auth_headers(url)))
            body = urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            h = getattr(e, "headers", None)
            status = getattr(e, "code", None) or 503   # 接続不能・タイムアウトは一時障害扱い
            go, wait = _retry_plan(status, attempt, h.get("Retry-After") if h else None)
            if status == 429:
                _pace["mult"] = min(_pace["mult"] * 2, PACE_MAX / max(pace, 0.01))
            if not go:
                if status in RETRY_STATUS:             # 待っても駄目だった
                    _pace["exhausted_in_a_row"] += 1
                    if _pace["exhausted_in_a_row"] >= ABORT_AFTER:
                        raise RateLimitAbort(
                            "%d 件連続で応答が得られない (最後: %s)。"
                            "時間をおいて再実行してください。取得済みは .cache/ に残ります"
                            % (ABORT_AFTER, url))
                raise
            time.sleep(wait)
        else:
            _pace["exhausted_in_a_row"] = 0
            _pace["mult"] = max(1.0, _pace["mult"] * 0.9)   # 成功が続けば元のペースへ戻す
            time.sleep(min(pace * _pace["mult"], PACE_MAX))
            return body
    raise RuntimeError("unreachable")   # ループは必ず return か raise で抜ける


def _no_empty_mailto(url):
    """連絡先が未設定なら、URL から空の mailto= を外す(空のまま送らない)。設定してあれば URL は 1 文字も変えない:
    キャッシュのファイル名は URL から作るので、変えると取得済みの応答を全部取り直すことになる。"""
    if MAILTO:
        return url
    url = re.sub(r'([?&])mailto=(&|$)', lambda m: m.group(1) if m.group(2) else "", url)
    return url.rstrip("?&")


def get_json(url):
    """Cached GET. The scan touches ~2000 records; never pay for the same one twice."""
    url = _no_empty_mailto(url)
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, re.sub(r'[^A-Za-z0-9]+', '_', url)[-180:] + ".json")
    if os.path.exists(p):
        return json.load(open(p))
    d = json.loads(_fetch(url).decode())
    _atomic_write(p, [json.dumps(d)])      # 書きかけが残ると以後この URL は毎回例外になる
    return d


def get_text(url, pace=0.3, timeout=30):
    """get_json と同じ(キャッシュつきの GET)。JSON でない応答(国立国会図書館の XML)用。"""
    os.makedirs(CACHE, exist_ok=True)
    p = os.path.join(CACHE, re.sub(r'[^A-Za-z0-9]+', '_', url)[-180:] + ".txt")
    if os.path.exists(p):
        return open(p).read()
    x = _fetch(url, pace=pace, timeout=timeout).decode()
    _atomic_write(p, [x])
    return x


# ---------- authorities ----------
_FAILED = []   # 1 件の resolve の中で、外部から取れなかった問い合わせ。resolve が毎回空にする


def _lookup_failed(e, what):
    """問い合わせの例外を握り潰す前に通す。「記録が無い」(404 など。再実行しても変わらない)は何もしない。
    取れなかった(タイムアウト・5xx・再送の使い切り)ものは残し、resolve が「同定できなかった」と区別する。"""
    if _fetch_outcome(e) == "failed":
        _FAILED.append(what)


def from_crossref(doi):
    try:
        m = get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi) +
                     "?mailto=" + MAILTO)["message"]
    except Exception as e:
        _lookup_failed(e, "crossref:" + doi)
        return None
    if not m.get("container-title"):
        return None  # a DOI that resolves to no journal is not a journal article
    a = (m.get("author") or [{}])[0]
    # issued = Crossref 自身が持つ最も早い既知の公開日。無ければ実在する日付の最小年。
    # created(DOI登録日) は使わない: 後年に電子化された論文では、登録日が出版年より何年も後になる(DESIGN.md「3. 年の決定」)。
    def _y(k):
        v = ((m.get(k) or {}).get("date-parts") or [[None]])[0][0]
        return v if isinstance(v, int) else None
    year = _y("issued") or min([y for y in (_y("published-online"), _y("published-print"),
                                            _y("created")) if y] or [None])
    return {"doi": m.get("DOI"), "title": clean_text((m.get("title") or [""])[0]),
            "year": year,
            "first_author": clean_text(a.get("family") or a.get("name") or ""),
            "authors": [clean_text(((x.get("family") or "") + " " + (x.get("given") or "")).strip())
                        for x in (m.get("author") or [])][:20],
            "journal": clean_text(m.get("container-title", [""])[0]),
            "journal_short": clean_text((m.get("short-container-title") or [""])[0]),
            "volume": m.get("volume"), "issue": m.get("issue"), "pages": m.get("page"),
            # APS・Nat. Commun. などは頁の代わりに論文番号(PRL 110, 244302)。ファイル名だけで使う。
            # pages に混ぜないのは、corroborate の開始頁の手がかりまで変わるから。
            "article_number": m.get("article-number"),
            "abstract": clean_text(re.sub(r'<[^>]+>', ' ', m.get("abstract") or "")) or None,
            "subject": m.get("subject") or [], "source": "crossref"}


def from_arxiv(aid):
    os.makedirs(CACHE, exist_ok=True)
    cp = os.path.join(CACHE, "arxiv_" + re.sub(r'[^A-Za-z0-9]+', '_', aid) + ".xml")
    try:
        if os.path.exists(cp):
            x = open(cp).read()
        else:
            x = _fetch("http://export.arxiv.org/api/query?id_list=" + aid,
                       pace=3, timeout=60).decode()   # arXiv は 3 秒間隔を要求する。応答が遅いことがあるので長く待つ
            _atomic_write(cp, [x])
    except Exception as e:
        _lookup_failed(e, "arxiv:" + aid)
        return None
    return _arxiv_record(x, aid)


def _arxiv_record(x, aid):
    """arXiv API の Atom を記録にする。出版版の DOI は同一性のキーではなく関係(published_doi)として持つ
    (プレプリントは出版版と別の論文: 利用者の判断)。"""
    A, X = "{http://www.w3.org/2005/Atom}", "{http://arxiv.org/schemas/atom}"
    import xml.etree.ElementTree as ET
    e = ET.fromstring(x).find(A + "entry")
    if e is None or e.find(A + "title") is None:
        return None
    names = [n.find(A + "name").text for n in e.findall(A + "author")]
    pub = e.find(X + "doi")
    jref = e.find(X + "journal_ref")
    return {"doi": None, "arxiv_id": aid,
            "published_doi": pub.text.strip().lower() if pub is not None and pub.text else None,
            "journal_ref": " ".join(jref.text.split()) if jref is not None and jref.text else None,
            "title": " ".join(e.find(A + "title").text.split()),
            "abstract": " ".join(e.find(A + "summary").text.split()),
            "year": int(e.find(A + "published").text[:4]),
            "first_author": (names[0].split()[-1] if names else ""),
            "authors": names[:20], "journal": "arXiv",
            "journal_short": "arXiv", "volume": None, "issue": None, "pages": None,
            "subject": [c.attrib["term"] for c in e.findall(A + "category")],
            "source": "arxiv"}


def corroborate(d, text):
    """権威が返したレコードが『この PDF のもの』か本文で裏を取る。

    これが無いと、参考文献欄から拾った他人の DOI や、同一タイトルの別版
    (CLEO予稿 / 本誌 / 書籍章 / Erratum) が静かに採用される(実例は DESIGN.md「全段に本文との裏取りを入れる」)。
    タイトル一致率では区別できない(同一タイトルなので 1.00 になる)。

    呼び出し側は必ず1ページ目のヘッダ領域を渡すこと。引用文献の著者名も
    本文には出てくるので、全文で照合しても裏取りにならない。"""
    t = norm(text)
    if not t or not d:
        return False
    au = norm(d.get("first_author"))
    if len(au) > 2 and au not in t:
        return False
    signals = [str(d.get("year") or ""), norm(d.get("journal_short")), norm(d.get("journal")),
               str(d.get("volume") or ""), str(d.get("pages") or "").split("-")[0]]
    return any(len(x) > 2 and x in t for x in signals if x)


def by_bibliographic(text):
    """本文先頭300字をそのまま Crossref に投げ、返ったタイトルが本文に
    literal に現れるものだけ採る。推論器(LLM)にタイトルを提案させる段は置かない(DESIGN.md「1. 中心となる設計判断」)。"""
    q = " ".join(text.split())[:300]
    if len(q) < 40:
        return None
    try:
        r = get_json("https://api.crossref.org/works?rows=5&mailto=" + MAILTO +
                     "&select=DOI,title&query.bibliographic=" + urllib.parse.quote(q))
    except Exception as e:
        _lookup_failed(e, "crossref の検索(本文の先頭)")
        return None
    full = norm(text)
    for it in r["message"]["items"]:
        t = (it.get("title") or [""])[0]
        if _title_in_text(t, full) and not title_only_in_citation(t, text):
            return from_crossref(it["DOI"])
    return None


def by_title(cand, text):
    """Crossref title search, accepted ONLY on a near-exact normalized title match.
    Relevance score is not separable (measured: correct hit 59, noise floor 23), so the
    string comparison — not the score — is what makes this rung safe."""
    if not cand or len(cand) < 15:
        return None
    try:
        r = get_json("https://api.crossref.org/works?rows=5&mailto=" + MAILTO +
                     "&select=DOI,title&query.title=" + urllib.parse.quote(cand))
    except Exception as e:
        _lookup_failed(e, "crossref の検索(表題)")
        return None
    for it in r["message"]["items"]:
        t = clean_text((it.get("title") or [""])[0])
        if title_ratio(norm(t), norm(cand)) >= 0.92 \
                and not title_only_in_citation(t, text):    # 引用文の中にしか無いタイトルは証拠にしない
            return from_crossref(it["DOI"])
    return None


# ---------- candidate identifiers (the ladder) ----------
def title_candidate(info, text):
    """Heuristic title: embedded Title, else the longest plausible line near the top."""
    m = re.search(r'^Title:\s*(.+)$', info, re.M)
    t = m.group(1).strip() if m else ""
    if len(t) > 15 and not t.lower().endswith((".dvi", ".pdf", ".tex", ".doc")):
        return t
    lines = [" ".join(l.split()) for l in text.split("\n")[:25]]
    lines = [l for l in lines if 25 < len(l) < 250 and not DOI_RE.search(l)
             and sum(c.isdigit() for c in l) < len(l) * 0.2]
    return max(lines, key=len) if lines else ""


def _vocr():
    """vocr.swift を必要になった時だけビルドする（以後は再利用）。"""
    exe, src = os.path.join(CODE, "vocr"), os.path.join(CODE, "vocr.swift")
    if not os.path.exists(exe) or os.path.getmtime(src) > os.path.getmtime(exe):
        subprocess.run(["swiftc", "-O", "-o", exe, src], capture_output=True, timeout=300)
    return exe if os.path.exists(exe) else None


def ocr(path, pages=2, first=1):
    """画像PDF用。macOS の Vision framework を直接呼ぶ(vocr.swift)。
    Vision・fm・tesseract の比較は DESIGN.md「段5: 画像PDFの OCR」。
    言語順が効く: en-US を先に置くと日本語がほとんど読めないので、ja-JP を先に置く。"""
    import tempfile, glob as _g
    d = tempfile.mkdtemp()
    sh(["pdftoppm", "-f", str(first), "-l", str(first + pages - 1), "-r", "200", "-png", path,
        os.path.join(d, "p")], timeout=180)
    out = []
    for png in sorted(_g.glob(os.path.join(d, "p*.png"))):
        exe = _vocr()
        out.append(sh([exe, png, OCR_LANG] if exe else
                      ["tesseract", png, "stdout"], timeout=180))   # swiftc が無ければ退避
        os.remove(png)
    os.rmdir(d)
    return "\n".join(out)


# ダウンロード時に付く表紙。1 ページ目が丸ごと表紙で、論文は 2 ページ目から始まる。
# 表紙だけをヘッダにすると、著者名が表紙に無い(IOP)か一部だけ(ResearchGate)なので裏取りが
# 落ちる。そこで論文側の 1 ページ目をヘッダにする。表紙は 2 種類に分けて扱う:
#   SELF   その論文だけを説明する(ResearchGate・SPIE・T&F・JSTOR・大学リポジトリ)。
#          SPIE の予稿は論文側に年も巻も無く、表紙の書誌で裏が取れていた。ヘッダに足す
#   OTHERS 他の論文も並べる(IOP の「You may also be interested in」)。ヘッダに入れると
#          他人の著者名で通る(DESIGN.md「ダウンロード表紙」)。ヘッダにも DOI の探索対象にも入れない
# 1 ページ目の上端に付く帯(ACS のオープンアクセス告知、RSC の View Article Online、
# Royal Society の Downloaded from)は論文の 1 ページ目そのものなので表紙ではない。
COVER_SELF = re.compile(r'^\W*(See discussions, stats, and author profiles for this publication'
                        r'|PROCEEDINGS OF SPIE SPIEDigitalLibrary\.org'
                        r'|This article was downloaded by:)'
                        r'|Your use of the JSTOR archive indicates|links\.jstor\.org/sici'
                        r'|Citation for published version \(APA\)')
COVER_OTHERS = re.compile(r'^\W*Home Search Collections Journals About Contact us My IOPscience')


def cover_kind(text):
    t = " ".join((text or "").split())[:1500]
    return "others" if COVER_OTHERS.search(t) else "self" if COVER_SELF.search(t) else None


def is_cover(text):
    return cover_kind(text) is not None


# 引用文の中にしか現れない証拠は採らない。
# 1 ページ目が前の論文の末尾(参考文献欄)から始まる PDF では、本文に他人の論文のタイトルが
# 引用として並び、段6 はそれに着いて裏取りも通る。書き出しの形は Nature の「16. 姓, 名の頭文字」、
# 「Received …; doi:…. 1. 姓, 名の頭文字」、JOSA の「"題," J. Opt. Soc. Am. 58, 930 (1968).」と
# 様々で、形を列挙すると規則が増え続けた。代わりに証拠の文脈を見る: 引用なら、タイトルの直後に
# 「巻, 頁 (年)」が続く。本物の 1 ページ目なら、タイトルの直後は著者名。
# 測定は DESIGN.md「引用文の中の証拠は採らない」。
CITE_TAIL = re.compile(r'^[^()]{0,90}?\b\d{1,4}\s*,\s*[A-Z]?\d+(?:\s*[–-]\s*\d+)?\s*\((?:19|20)\d\d\)')


def _norm_map(t):
    """norm() と同じ文字列と、その各文字が元の t のどこにあったか。"""
    out, pos = [], []
    for i, ch in enumerate(t):
        for c in unicodedata.normalize("NFKD", ch):
            c = c.lower()
            if not unicodedata.combining(c) and c.isascii() and c.isalnum():
                out.append(c)
                pos.append(i)
    return "".join(out), pos


def title_only_in_citation(title, text):
    """本文でのタイトルの出現が全て引用文の中なら True。見つからなければ False(判定しない)。
    位置は段6 の一致判定(norm 後の部分文字列)と同じ正規化の上で探す。単語の正規表現で探すと
    「16 μm」と「1.6 μm」のような書き方の差で見つからない。"""
    k = norm(clean_text(title))
    if len(k) <= 25:
        return False
    t = " ".join((text or "").split())
    s, pos = _norm_map(t)
    ends, i = [], s.find(k)
    while i >= 0:
        ends.append(pos[i + len(k) - 1] + 1)
        i = s.find(k, i + 1)
    return bool(ends) and all(CITE_TAIL.search(t[e:e + 130]) for e in ends)


def _read(path, first):
    """first ページ目から 2 ページ分の本文。テキスト層が無ければ OCR する。"""
    text = sh(["pdftotext", "-f", str(first), "-l", str(first + 1), path, "-"])
    if len(text) < 200:                       # 画像PDF: OCR して同じ段をそのまま適用する
        return ocr(path, first=first), True
    return text, False


def _resolve(path):
    info = sh(["pdfinfo", path])
    text, ocred = _read(path, 1)
    first, cover, kind = 1, "", cover_kind(text)
    if kind:                                  # 表紙は読み飛ばす。OCR した本文でも判定する
        first = 2
        if kind == "self":                    # その論文だけを説明する表紙は裏取りに使える
            cover = " ".join(sh(["pdftotext", "-f", "1", "-l", "1", path, "-"]).split())[:1500] \
                or " ".join(text.split())[:1500]
        text, ocred = _read(path, 2)
    base = os.path.basename(path)
    rec = {"path": path, "folder": os.path.dirname(path), "filename": base}

    rec["ocr"] = ocred
    if kind:
        rec["cover"] = kind
    unver = None
    # 自誌の書誌はヘッダにあり、引用文献の著者・DOIは後ろに来る。裏取りはヘッダ限定で行う。
    head = " ".join(sh(["pdftotext", "-f", str(first), "-l", str(first), path, "-"]).split())[:1500] \
        or " ".join(text.split())[:1500]
    head = (head + " " + cover).strip()
    rec["text"] = " ".join(text.split())[:3000]   # 抽出済み本文は捨てない(再OCRは高い)
    # arXiv の刻印があれば、この PDF は arXiv 版(プレプリント)。出版版とは別の論文として扱う(利用者の判断)ので、
    # 本文中にある出版版の DOI などには着かせない。
    st = ARX_STAMP.search(" ".join(text.split()))
    if st:
        d = from_arxiv(st.group(1))
        if d and corroborate(d, head):
            return dict(rec, rung="arxiv_text", **d)
        if d:
            return dict(rec, rung="unverified", **d)
        return dict(rec, rung="unresolved", source=None, ocr=ocred,
                    title_guess=title_candidate(info, text)[:200], no_text=False)
    # SELF の表紙の DOI はその論文自身のもの。探す対象に残し、表紙を先に置く。
    for rung, hay in (("pdfinfo_doi", info), ("text_doi", cover + "\n" + text)):
        m = DOI_RE.search(hay)
        if m:
            d = from_crossref(m.group(0).rstrip('.').rstrip(')'))
            if d and corroborate(d, head + " " + info):
                return dict(rec, rung=rung, **d)
            if d:
                unver = dict(rec, rung="unverified", **d)   # 参考文献欄の他人のDOI等

    for rung, pat, hay in (("arxiv_text", ARX_OLD, text), ("arxiv_text", ARX_TEXT, text),
                           ("arxiv_name", ARX_NEW, base)):
        m = pat.search(hay)
        if m:
            d = from_arxiv(m.group(1))
            if d and corroborate(d, head):
                return dict(rec, rung=rung, **d)
            if d and not unver:
                unver = dict(rec, rung="unverified", **d)

    d = by_bibliographic(text)
    if d and corroborate(d, head):            # 同一タイトルの別論文があるので著者でも裏を取る
        return dict(rec, rung="bib_query", **d)
    if d and not unver:
        unver = dict(rec, rung="unverified", **d)

    # 段4 は段6 の後ろのフォールバック。タイトル一致は同一タイトルの別版を区別できず、corroborate も年か誌名の一致で
    # 通してしまう。段6 は本文の書誌行ごと投げるので正しい版に着く。段4 を残すのは、埋め込み Title からしか引けない
    # 記録があるため(DESIGN.md「段6 を段4 の前に置く」)。
    d = by_title(title_candidate(info, text), text)
    if d and corroborate(d, head):
        return dict(rec, rung="title_search", **d)

    if unver:
        return unver                          # 候補はあるが裏が取れない: 人に回す
    return dict(rec, rung="unresolved", source=None, ocr=ocred,
                title_guess=title_candidate(info, text)[:200],
                no_text=len(text) < 200)


# ---------- 書籍の DB: 著作権の頁の ISBN で引く ----------
# 本は 1 頁目が表紙で、書誌は著作権の頁にある。その頁の ISBN で目録(Crossref・Open Library・国立国会図書館)を引き、返った表題が
# PDF に字面であるものだけ採る。部分(章だけ・前付けだけ)か全体かは決めない(人が種類を付ける)。測定は DESIGN.md「書籍の DB」。
BOOK_HEAD, BOOK_TAIL, BOOK_OCR = 20, 6, 12      # 文字の層を読む先頭・末尾の頁数と、層が乏しいときに OCR する先頭の頁数
BOOK_THIN = 300                                 # 先頭の頁の平均がこの字数未満で、ISBN も見つからなければ OCR する
BOOK_TYPES = ("book", "monograph", "edited-book", "reference-book")     # Crossref の種類のうち、本の全体を指すもの(章・前付けを除く)
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
ISBN_LABEL = re.compile(r'ISBN(?:[- ]?1[03](?!\d))?\s*[:：]?\s*((?:[0-9Xx][- ]?){9,16}[0-9Xx])', re.I)   # 「ISBN-13」の 13 は、数字が続かないときだけ表示の一部
ISBN_BARE = re.compile(r'(?<![\d-])97[89](?:[- ]?\d){10}(?![\d-])')
COPYRIGHT = re.compile(r'©|copyright|all rights reserved|library of congress|printed in|cataloging|cataloguing|published by|発行|印刷', re.I)
_C_YEAR = re.compile(r'(?:©|\(c\)|copyright)\s*(?:by\s+)?\D{0,60}?((?:19|20)\d\d(?:\s*[,/–-]\s*(?:19|20)\d\d)*)', re.I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_EDITION = re.compile(r'\b(%s)\s+(?:\w+\s+)?edition\b|\b(\d{1,2})\s?(?:st|nd|rd|th)\s+ed(?:ition|\.|\b)' % "|".join(_ORDINALS), re.I)


def isbn13(s):
    """検査数字の合う ISBN(10 桁か 13 桁。ハイフン・空白つきでよい)を 13 桁で返す。合わなければ None。"""
    d = re.sub(r'[- ]', '', s or "").upper()
    if re.fullmatch(r'\d{9}[\dX]', d):
        if sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d)) % 11:
            return None
        d = "978" + d[:9]
        return d + str(-sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d)) % 10)
    if re.fullmatch(r'97[89]\d{10}', d) and sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d)) % 10 == 0:
        return d
    return None


def _isbn10(i):
    """978 で始まる 13 桁の ISBN の 10 桁の形(古い記録は 10 桁で登録されている)。979 は None。"""
    if not i.startswith("978"):
        return None
    k = -sum((10 - n) * int(c) for n, c in enumerate(i[3:12])) % 11
    return i[3:12] + ("X" if k == 10 else str(k))


def isbns_in(text, bare=False):
    """本文にある ISBN(13 桁にそろえ、出た順、重複なし)。既定は「ISBN」の表示に続く番号だけ。bare なら、表示の無い 978・979 の
    13 桁も拾う。どちらも検査数字の合うものだけ。"""
    t = unicodedata.normalize("NFKC", text or "").translate(_DASHES)
    out = []
    for m in (ISBN_BARE if bare else ISBN_LABEL).finditer(t):
        s = m.group(0) if bare else m.group(1)
        d = re.sub(r'[- ]', '', s)
        # 表示の後ろに続く数字(刷数など)まで拾ったときは、頭から切って試す。978・979 で始まる並びを 10 桁には切らない
        # (検査数字の合わない 13 桁の頭 10 桁が、別の ISBN として通る)
        tries = [s, s[:17], d[:13]] + ([] if d.startswith(("978", "979")) else [d[:10]])
        v = next((x for x in map(isbn13, tries) if x), None)
        if v and v not in out:
            out.append(v)
    return out


def copyright_isbns(pages):
    """pages は [(頁番号, 本文)]。著作権の語と ISBN の両方がある最初の頁の (頁番号, ISBN の並び, その頁の本文)。無ければ (None, [], "")。
    その頁だけを使うのは、関連書の ISBN を並べた頁が前にある本があるから。「ISBN」の表示のある番号を先に探し、どの頁にも無ければ
    表示の無い 13 桁を探す。"""
    for bare in (False, True):
        for no, t in pages:
            if COPYRIGHT.search(t):
                got = isbns_in(t, bare)
                if got:
                    return no, got, t
    return None, [], ""


def page_year(text):
    """著作権の頁の © の年のうち、いちばん新しいもの(「© 1988, 1992, 2007」と前の版の年が並ぶ)。無ければ None。"""
    text = re.sub(r'Copyright,?\s+Designs\s+and\s+Patents\s+Act,?\s+1988', ' ', text or "", flags=re.I)      # 英国の法律の名前(年ではない)
    ys = [int(y) for m in _C_YEAR.finditer(text) for y in re.findall(r'(?:19|20)\d\d', m.group(1))]
    return max(ys) if ys else None


def page_edition(text):
    """著作権の頁に書いてある版のうち、いちばん大きいもの(「First published … Second edition …」と並ぶ)。無ければ None。"""
    got = [_ORDINALS[a.lower()] if a else int(b) for a, b in _EDITION.findall(text or "")]
    return max(got) if got else None


def _bnorm(s):
    """本の表題の照合用。norm() と違い、和文を残す。"""
    return re.sub(r'[^a-z0-9぀-ヿ一-鿿]', '', unicodedata.normalize("NFKD", s or "").lower())


def _main_title(t):
    """副題・叢書名を除いた主題(「:」「(」の前)。"""
    return " ".join(re.split(r'[:(（]', t or "")[0].split())


def book_title_in(title, text_norm):
    """目録の表題(主題)が、PDF の本文(_bnorm 済み)に字面であるか。"""
    t = _bnorm(_main_title(title))
    return len(t) >= (2 if _CJK.search(t) else 4) and t in text_norm        # 和書の主題は短い(「熱力学」)


def _family_of(name):
    """目録の著者名から姓。「姓, 名」はカンマの前、そうでなければ最後の語。"""
    name = " ".join((name or "").split())
    return name.split(",")[0].strip() if "," in name else (name.split() or [""])[-1]


def _crossref_book(m):
    """Crossref の記録(本の種類)を、書籍の記録の項目に。著者が無く編者だけの本は、編者を著者の欄に入れて role を editor にする。
    著者の並びは論文と同じ「姓 名」。"""
    who = m.get("author") or m.get("editor") or []
    y = lambda k: ((m.get(k) or {}).get("date-parts") or [[None]])[0][0]
    year = y("published") or y("issued")
    return {"doi": m.get("DOI"), "title": clean_text(" ".join(m.get("title") or [])), "subtitle": clean_text(" ".join(m.get("subtitle") or [])) or None,
            "authors": [clean_text(((x.get("family") or x.get("name") or "") + " " + (x.get("given") or "")).strip()) for x in who][:20],
            "families": [clean_text(x.get("family") or x.get("name") or "") for x in who][:20],
            "authors_role": "editor" if who and not m.get("author") else None,
            "year": year if isinstance(year, int) else None, "publisher": clean_text(m.get("publisher") or "") or None, "source": "crossref",
            "isbn_all": [x for x in map(isbn13, m.get("ISBN") or []) if x]}


def _openlibrary_book(v):
    """Open Library の記録(jscmd=data)を、書籍の記録の項目に。著者の並びは目録のまま(「名 姓」が多い)。"""
    names = [" ".join((a.get("name") or "").split()) for a in v.get("authors") or []]
    y = re.search(r'(1[5-9]|20)\d\d', str(v.get("publish_date") or ""))
    return {"doi": None, "title": clean_text(v.get("title") or ""), "subtitle": clean_text(v.get("subtitle") or "") or None,
            "authors": [n for n in names if n][:20], "families": [_family_of(n) for n in names if n][:20], "authors_role": None,
            "year": int(y.group(0)) if y else None,
            "publisher": clean_text(((v.get("publishers") or [{}])[0]).get("name") or "") or None, "source": "openlibrary"}


def _ndl_book(xml):
    """国立国会図書館サーチ(OpenSearch)の応答の先頭の 1 件を、書籍の記録の項目に。無ければ None。著者は「姓, 名」で返るので、
    カンマを外して「姓 名」にする(訳者も著者の欄に入っている)。"""
    it = re.search(r'<item>(.*?)</item>', xml or "", re.S)
    if not it:
        return None
    get = lambda tag: [html.unescape(" ".join(v.split())) for v in re.findall(r'<%s(?: [^>]*)?>(.*?)</%s>' % (tag, tag), it.group(1), re.S)]
    title = (get("dc:title") or [""])[0]
    if not title:
        return None
    who = [re.sub(r',?\s*\d{4}-(\d{4})?$', '', w).strip() for w in get("dc:creator")]
    y = re.search(r'(1[5-9]|20)\d\d', " ".join(get("dcterms:issued") + get("dc:date")))
    return {"doi": None, "title": title, "subtitle": None, "authors": [w.replace(",", " ").replace("  ", " ") for w in who if w][:20],
            "families": [_family_of(w) for w in who if w][:20], "authors_role": None, "year": int(y.group(0)) if y else None,
            "publisher": (get("dc:publisher") or [None])[0], "source": "ndl"}


def isbn_urls(i):
    """books_by_isbn が ISBN(13 桁)1 つのために取りに行きうる URL の全部を、問い合わせる順に: {相手: URL の並び}。
    画面のサーバは、ISBN を引く間、これだけを外へ通す。引く番号は末尾に置く(キャッシュの名前は URL の末尾から作る)。"""
    cr = "https://api.crossref.org/works?rows=10&mailto=%s&filter=%s,isbn:%%s" % (MAILTO, ",".join("type:" + t for t in BOOK_TYPES))
    return dict(([("ndl", ["https://ndlsearch.ndl.go.jp/api/opensearch?isbn=%s" % i])] if i.startswith("9784") else [])
                + [("crossref-isbn", [_no_empty_mailto(cr % x) for x in (i, _isbn10(i)) if x]),
                   ("openlibrary", ["https://openlibrary.org/api/books?bibkeys=ISBN:%s&jscmd=data&format=json" % i])])


def books_by_isbn(i):
    """ISBN(13 桁)で目録を引いた記録の並び。和書(9784)は国立国会図書館が先。取れなかった問い合わせは _lookup_failed に残す
    (resolve が error にして、次の add が取り直す)。"""
    out = []
    def ask(what, fn):
        try:
            out.extend(x for x in fn() if x and x.get("title"))
        except Exception as e:
            _lookup_failed(e, "%s:%s" % (what, i))
    urls = isbn_urls(i)
    def crossref():
        for u in urls["crossref-isbn"]:                 # 13 桁、次に 10 桁(古い記録は 10 桁で登録されている)
            its = get_json(u)["message"]["items"]
            if its:
                return [_crossref_book(m) for m in its]
        return []
    read = {"ndl": lambda: [_ndl_book(get_text(urls["ndl"][0], pace=1.0))], "crossref-isbn": crossref,
            "openlibrary": lambda: [_openlibrary_book(v) for v in get_json(urls["openlibrary"][0]).values()]}
    for what in urls:
        ask(what, read[what])
    return [dict(x, isbn=i) for x in out]


def book_pick(cands, text_norm):
    """目録が返した記録から 1 冊を選ぶ。(記録, None) か (None, 採らない理由)。
    表題が PDF に字面であるものだけ残す。残った表題どうしが食い違えば採らない(片方がもう片方を含めば同じ本とみる)。
    項目の出どころは、国立国会図書館 → 著者のある Crossref → 著者のあるもの → 先頭。DOI は、選んだ記録と同じ ISBN を持つ Crossref の
    記録にあれば付ける(表題が似ているだけの別の本の DOI を付けない)。"""
    ok = [c for c in cands if book_title_in(c.get("title"), text_norm)]
    if not ok:
        return None, "the catalogue title is not in the PDF" if cands else "ISBN not found in the catalogues"
    keys = [_bnorm(_main_title(c["title"])) for c in ok]
    if any(a not in b and b not in a for a in keys for b in keys):
        return None, "the ISBNs on the copyright page point to different books"
    best = (next((c for c in ok if c["source"] == "ndl"), None)
            or next((c for c in ok if c["source"] == "crossref" and c.get("families")), None)
            or next((c for c in ok if c.get("families")), None) or ok[0])
    same = lambda c: c.get("isbn") == best.get("isbn") or best.get("isbn") in (c.get("isbn_all") or [])
    doi = best.get("doi") or next((c["doi"] for c in ok if c.get("doi") and same(c)), None)
    return dict({k: v for k, v in best.items() if k != "isbn_all"}, doi=doi), None


def _book_pages(path, n):
    """[(頁番号, 本文)]: 先頭 BOOK_HEAD 頁と、その後ろにあれば末尾 BOOK_TAIL 頁(和書の奥付は末尾にある)。文字の層だけ。"""
    split = lambda t: t.split("\f")[:-1] if t.endswith("\f") else t.split("\f")
    head = split(sh(["pdftotext", "-l", str(BOOK_HEAD), path, "-"], timeout=60))
    pages = list(enumerate(head, 1))
    if n and n > BOOK_HEAD:
        first = max(BOOK_HEAD + 1, n - BOOK_TAIL + 1)
        pages += list(enumerate(split(sh(["pdftotext", "-f", str(first), path, "-"], timeout=60)), first))
    return pages


def _resolve_book(path):
    """書籍の DB の同定。arXiv の刻印・番号があればプレプリント(種類は書籍(部分))、無ければ著作権の頁の ISBN で目録を引く。
    採れなければ unresolved(why_not に理由)。表題の検索では決めない(候補を出すのは papers review)。"""
    base = os.path.basename(path)
    n = pdf_pages(path)
    pages = _book_pages(path, n)
    head = [t for no, t in pages if no <= BOOK_HEAD]
    no, ids, ctext = copyright_isbns(pages)
    ocred = False
    if not ids and sum(len(t.strip()) for t in head) < BOOK_THIN * max(1, len(head)):       # 層が乏しい: 先頭を OCR して探し直す
        got = _ocr_pages(path, 1, min(BOOK_OCR, n or BOOK_OCR))
        if got:
            pages, ocred = sorted(got.items()), True
            head = [t for _, t in pages]
            no, ids, ctext = copyright_isbns(pages)
    text = " ".join(" ".join(head).split())
    rec = {"path": path, "folder": os.path.dirname(path), "filename": base, "ocr": ocred, "text": text[:3000]}
    first2 = " ".join(" ".join(head[:2]).split())
    in_text = ARX_STAMP.search(first2) or ARX_TEXT.search(first2)
    m = in_text or ARX_NEW.search(base)
    if m:
        d = from_arxiv(m.group(1))
        if d and corroborate(d, first2[:3000]):
            return dict(rec, rung="arxiv_text" if in_text else "arxiv_name", kind="bookpart", **d)
    unres = dict(rec, rung="unresolved", source=None, title_guess=os.path.splitext(base)[0][:200], no_text=len(text) < 200)
    if not ids:
        return dict(unres, why_not="no ISBN on a copyright page")
    cands = [c for i in ids[:6] for c in books_by_isbn(i)]
    d, why = book_pick(cands, _bnorm(" ".join(t for _, t in pages)))
    if not d:
        return dict(unres, why_not=why, isbn_seen=ids)
    d["year"] = d.get("year") or page_year(ctext)          # 目録に年が無ければ、著作権の頁の年
    return dict(rec, rung="isbn", first_author=(d["families"] or [""])[0], isbn_page=no, isbns=ids,
                page_year=page_year(ctext), page_edition=page_edition(ctext), **d)


def resolve(path):
    """外部から取れなかった問い合わせが 1 つでもあれば、結果を採らずに error にする(次の scan・add が取り直す)。
    解決していても採らない: 前の段が取れずに後ろの段で着くと、段も、ときには同定も変わる
    (arXiv が取れずに本文の検索で出版版に着く、など)。"""
    del _FAILED[:]
    r = _resolve_book(path) if BOOKS else _resolve(path)
    if _FAILED:
        what = sorted(set(_FAILED))
        return {"path": path, "folder": os.path.dirname(path), "filename": os.path.basename(path), "rung": "error",
                "error": ("外部から取れなかった: " + ", ".join(what))[:200], "fetch_failed": what,
                "text": r.get("text"), "ocr": r.get("ocr")}
    return r if BOOKS else mark_supplement(r)     # 補足資料の判定は論文の 1 頁目の書き出しを見る。本には当てない


def retryable(r):
    """外部から取れなかっただけの記録。scan・add はこれを索引に無いものとして扱い、同定し直す。"""
    return r.get("rung") == "error" and bool(r.get("fetch_failed"))


# ---------- 補足資料(SI)と、段ごとの扱い ----------
# SI は 1 ページ目に本文のタイトルを再掲するので、本文の DOI に着く(裏取りも通りうる)。
# 同じ DOI でも論文そのものではないので、rename も dedup もしない。人にも回さない。
# 判定は 1 ページ目の先頭 300 字だけ。ファイル名は見ない(誤検出する)。タイトルしか無い SI は拾えない
# (DESIGN.md「補足資料（SI）と、段ごとの扱い」)。
_LEAD = r'^\W*(?:(?:doi:?\s*\S+|www\.\S+|https?://\S+)\s+)?'   # 先頭の DOI/URL は読み飛ばす
SI_HEAD = re.compile(_LEAD + r'(supporting\s+(online\s+)?(information|material)|supplementa(ry|l)\s+'
                     r'(information|materials?|notes?|data|methods|figures?|text)|electronic\s+supplementary)', re.I)
SI_CAPS = re.compile(r'\b(SUPPLEMENTARY|SUPPORTING)\s+(INFORMATION|MATERIALS?)\b')   # 全大文字の見出し
SI_PHRASE = re.compile(r'Supplementary\s+Figure\s+S?1\b|Supporting\s+Online\s+Material|'
                       r'Supplementary\s+Materials\s+for\b')
# ACS の論文は本文に "Supporting Information" と書くが、先頭ではなく大小混在なので当たらない。


def is_supplement(text):
    t = " ".join((text or "").split())[:300]
    return bool(SI_HEAD.search(t) or SI_CAPS.search(t) or SI_PHRASE.search(t))


def mark_supplement(r):
    """scan と既存索引の移行の両方がこれを通す。text の先頭だけで決まる。"""
    if r.get("rung") in ("error", "supplement", "manual") or not is_supplement(r.get("text")):
        return r
    return dict(r, rung="supplement", supplement_of_rung=r.get("rung"))


# rename / dedup が触れない段。stats・rename・review・dedup はすべてここを見る
# (1 か所に置く: 段を見ずに unverified の DOI でファイルを動かさないため)。
TERMINAL = ("unresolved", "error", "unverified", "supplement")


def renamable(r):
    return bool(r.get("source") and r.get("year") and r.get("first_author")) \
        and r.get("rung") not in TERMINAL


POINTERS = ("duplicate_of", "published_copy")   # 別の記録のパスを指す項目(dedup が書く)


def repoint(recs, moved):
    """ファイルを動かしたら、そのパスを指す項目も書き換える。moved は {旧パス: 新パス}。"""
    n = 0
    for r in recs:
        for k in POINTERS:
            if r.get(k) in moved:
                r[k], n = moved[r[k]], n + 1
    return n


def moved_aside(r):
    """脇へ移した記録: dedup が _重複文献/・_プレプリント(出版版あり)/ へ移したものと、利用者が DB から外したもの
    (removed。_索引から外した/ にある)。利用者が消すのを待つもので、検索に出さず、もう組にも改名にもかけない
    (かけると dedup の再実行が移したものを移し直す)。"""
    return bool(r.get("duplicate_of") or r.get("published_copy") or r.get("removed"))


def needs_human(r):
    """人が見る記録: 改名できないもの(補足資料と、人が決めた manual を除く)。同定できていない段(unresolved・error・unverified)と、
    段の判定は通ったが出どころ・年・第一著者のどれかが欠けた記録。後者は Crossref の記録に著者が無いとき
    (誌の前付け・編集記事・書籍の章)に起き、裏取りは著者が空だと著者の照合を飛ばすので、論文でない記録に着きうる。
    manual は項目が欠けていても回さない: 人が一部だけ埋めた記録を戻すと、review が「未 merge の記入」とみて止まり続ける。
    人が「論文ではない」とした記録(not_paper: 本の抜粋・講義ノート・資料)も回さない。同定する相手が無い。
    利用者が DB から外した記録(removed)も回さない。"""
    # 書籍の DB で人が「本」と付けた記録は、同定の相手(目録)があるので回し続ける
    not_paper = r.get("not_paper") and not (BOOKS and r.get("kind") == "book")
    return r.get("rung") not in ("supplement", "manual") and not not_paper and not r.get("removed") and not renamable(r)


def enrichable(r):
    """外部の値(抄録・分野)を付けてよい記録。unverified の DOI は他人のものかもしれない。
    SI は本文の DOI を持つので、本文の抄録で検索に引っかかるのは望ましい。"""
    return bool(r.get("doi")) and (renamable(r) or r.get("rung") == "supplement")


def _same_person(a, b):
    """第一著者の姓が同じか。複合姓(García López と López のような組)は arXiv が最後の語を
    姓とするので、一方がもう一方に含まれれば同じとみなす。"""
    a, b = norm(a), norm(b)
    return len(min(a, b, key=len)) > 2 and (a in b or b in a)


def preprint_pairs(recs):
    """(プレプリント, 出版版) の組。両方とも解決済みで、プレプリントの published_doi が出版版の DOI と一致し、
    第一著者が同じもの。未確定の記録(候補を持つだけのもの)は組にしない。"""
    pub = {(r.get("doi") or "").lower(): r for r in recs if renamable(r) and r.get("doi") and not moved_aside(r)}
    return [(r, pub[r["published_doi"]]) for r in recs
            if renamable(r) and not moved_aside(r) and r.get("arxiv_id") and r.get("published_doi") in pub
            and _same_person(r.get("first_author"), pub[r["published_doi"]].get("first_author"))]


# ---------- ページの検証(vet.py が Claude Haiku で読んだ表題を使う) ----------
# Haiku は「主たる論文の表題と第一著者を指し示す」役に限る(利用者の判断)。同定は決めさせず、
# 読んだ表題が PDF に字面で存在し、しかも記録の表題と食い違うときだけ、解決済みを人に回す(拒否権のみ)。
# 測定は DESIGN.md「ページを見る」。
_CJK = re.compile(r'[぀-ヿ一-鿿]')


def titles_agree(a, b):
    """表題の一致。測定と同じ基準(norm 後の一致率 0.8 以上か、20 字を超える包含)。一致率は両方の順の大きい方。"""
    a, b = norm(clean_text(a or "")), norm(clean_text(b or ""))
    return title_ratio(a, b) >= 0.8 or \
        (len(a) > 20 and a in b) or (len(b) > 20 and b in a)


def vet_verdict(r):
    """"hold"(人に回す)か None。解決済みで、読んだ表題が PDF に字面であり、記録の表題と食い違うときだけ hold。
    手入力(manual)と、人が確かめて戻した記録(vet_cleared)は人が決めた値なので対象外。和文の表題は Crossref の英題と
    食い違って見えるので判定しない(未測定)。"""
    t = r.get("page_title")
    if not renamable(r) or r.get("rung") == "manual" or r.get("vet_cleared") or not t or not r.get("page_title_literal"):
        return None
    if _CJK.search(t):
        return None
    return None if titles_agree(t, r.get("title")) else "hold"


def dedup_key(r):
    """同じ論文のファイルを束ねる鍵。裏の取れていない DOI や SI の親 DOI では束ねない。
    人に回す記録(段は通ったが著者・年が無い)の DOI でも束ねない: 改名できない記録の DOI でファイルを動かさない。"""
    if not renamable(r):
        return None
    return (r.get("doi") or "").lower() or (r.get("arxiv_id") or "") or None


# ---------- ファイルを動かすときの守り(rename・move・dedup・nonpaper・undo が同じものを通す) ----------
# os.rename は、移動先にファイルがあると黙って上書きする。置き場のボリューム(macOS・Google Drive)は大文字と小文字、
# 合成形と分解形を区別しないので、文字列が違っても同じファイルを指すことがある(Thesis.pdf と thesis.pdf)。
def _same_name(p):
    """パスを、置き場のボリュームが同じファイルとみなす形にそろえる(予定の中の同名を見つける鍵)。"""
    return unicodedata.normalize("NFC", p).casefold()


def _occupied(dst, src):
    """dst に、src 以外の何かがあるか。壊れたリンクも「ある」に数える(lexists)。src と同じパスなら空いているとみなす。
    大小文字だけが違う名前は、同じファイルを指していても「ある」とする: その場で名前の書き方だけを変える改名はしない
    (Google Drive の上での動きを確かめていない。同じ中身かをリンクをたどって見ると、リンクを実体の名前へ動かして実体を壊す)。"""
    return os.path.lexists(dst) and os.path.abspath(dst) != os.path.abspath(src)


def _rename_excl(src, dst):
    """「移動先に何かあれば失敗する改名」を、OS に 1 回の呼び出しで頼む(macOS の renamex_np、Linux の renameat2)。
    確かめてから動かすまでの間に、同期などが移動先へファイルを作っても上書きしない。
    True = 動かした。False = この OS・ボリュームではできない(呼び出し側が、確かめてから os.rename する)。移動先があれば FileExistsError。"""
    import ctypes, errno
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        s, d = os.fsencode(src), os.fsencode(dst)
        if b"\0" in s or b"\0" in d:           # C の文字列は NUL で切れる。os.rename に断らせる
            return False
        if sys.platform == "darwin":
            fn = libc.renamex_np
            fn.argtypes, fn.restype = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
            rc = fn(s, d, 0x4)                                   # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            fn = libc.renameat2
            fn.argtypes, fn.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint], ctypes.c_int
            rc = fn(-100, s, -100, d, 0x1)                       # AT_FDCWD, RENAME_NOREPLACE
        else:
            return False
    except (OSError, AttributeError, ValueError, TypeError):    # libc が開けない・関数が無い
        return False
    if rc == 0:
        return True
    if ctypes.get_errno() == errno.EEXIST:
        raise FileExistsError("移動先にもうファイルがある(上書きしない): %s" % dst)
    # EEXIST 以外の失敗(対応していないボリューム・src が無い・権限・別のボリューム)は、動かさずに False。このあとの os.rename が
    # 同じ理由で止まる(手元の 5 種のボリュームで確かめた)。一時的な失敗のときは、確かめたうえでの os.rename = 前と同じ動きになる
    return False


def _move_file(src, dst):
    """src を dst へ移す。dst に何かあれば動かさずに止める。src と dst が同じパスなら何もしない。"""
    if os.path.abspath(src) == os.path.abspath(dst):
        return
    if os.path.lexists(dst):
        raise FileExistsError("移動先にもうファイルがある(上書きしない): %s" % dst)
    if not _rename_excl(src, dst):             # OS が断る改名を使えないときだけ、上の確認を頼りに動かす
        os.rename(src, dst)


def _stopped(what, n):
    """動かす途中で例外が出たときに、finally の中から 1 行出す(例外そのものは、このあと上へ伝わる)。"""
    if sys.exc_info()[0] is not None:
        print("途中で止まった(%s): %d 件は動かして索引に書いた。残りは動かしていない" % (what, n), file=sys.stderr)


def _backup(tag):
    """索引を書き換える前に、今の索引を backups/ へ写す(papers-<時刻>-before-<tag>.jsonl)。同じ秒に同じ tag の退避がもうあれば、
    時刻に _2, _3 を付けて前の退避を上書きしない。写した先のパスを返す。"""
    bk = os.path.join(os.path.dirname(INDEX), "backups")
    os.makedirs(bk, exist_ok=True)
    ts, i = time.strftime("%Y%m%d-%H%M%S"), 1
    path = os.path.join(bk, "papers-%s-before-%s.jsonl" % (ts, tag))
    while os.path.exists(path):
        i += 1
        path = os.path.join(bk, "papers-%s_%d-before-%s.jsonl" % (ts, i, tag))
    tmp = path + ".part"                       # 写し終えてから名前を付ける(途中で失敗しても、書きかけが退避に見えない)
    try:
        shutil.copy2(INDEX, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def _trail(prefix):
    """動かす予定を書き残すファイルの名前(<prefix>-<時刻>.jsonl)。同じ秒にもう 1 つあれば _2, _3 を付けて、前の記録を
    上書きしない。'.' より '_' が後ろなので、同じ秒の 9 個目までは名前の順が作った順になる(undo は名前の順で最後の 1 つを読む。
    10 個目からは _10 が _2 の前に並ぶ)。"""
    base = os.path.join(ROOT, "%s-%s" % (prefix, time.strftime("%Y%m%d-%H%M%S")))
    p, i = base + ".jsonl", 2
    while os.path.exists(p):
        p, i = "%s_%d.jsonl" % (base, i), i + 1
    return p


# ---------- 索引とキャッシュの書き込み ----------
# 直接 open(path, "w") で上書きすると、書き込み中に落ちたとき旧版も新版も失う。
# 一時ファイルに書き切って fsync してから os.replace で差し替える。
def _atomic_write(path, lines):
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            for l in lines:
                f.write(l)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def load_index():
    """索引と、書き戻し時の照合に使う印 (size, mtime_ns) を返す。
    stat を読む前に取るので、間に追記されても照合は「変わった」側に倒れる。"""
    st = os.stat(INDEX)
    with open(INDEX) as f:
        recs = [json.loads(l) for l in f if l.strip()]
    return recs, (st.st_size, st.st_mtime_ns)


def save_index(recs, stamp):
    """読んだ後に索引が変わっていたら(scan の追記・別パスの書き戻し)差し替えない。
    差し替えると相手の書いた分が消え、scan は消えた旧ファイルに書き続ける。"""
    lines = (json.dumps(r, ensure_ascii=False) + "\n" for r in recs)
    st = os.stat(INDEX)
    if (st.st_size, st.st_mtime_ns) != stamp:
        side = INDEX + ".conflict-" + time.strftime("%Y%m%d-%H%M%S")
        _atomic_write(side, lines)
        sys.exit("索引が読み込み後に変更されていたので差し替えなかった"
                 "(scan など別のパスが同時に動いていないか)。\nこのパスの結果: " + side)
    _atomic_write(INDEX, lines)


# ---------- passes ----------
def _excluded(path, root, names):
    """path が、root の直下の names のどれかのフォルダの中にあるか。名前は合成形にそろえて比べる(置き場のボリュームは分解形で返すことがある)。"""
    rel = os.path.relpath(path, root).split(os.sep)
    nfc = lambda x: unicodedata.normalize("NFC", x)
    return len(rel) > 1 and nfc(rel[0]) in {nfc(n) for n in names}


def _new_pdfs(root, done):
    """root(フォルダ、または 1 行 1 パスの .txt)の PDF のうち、索引に無いもの。scan と add が同じ一覧を見る。
    フォルダを渡したときは、DB の config.json の exclude にある直下のフォルダを見ない。"""
    src = ([l.strip() for l in open(root)] if os.path.isfile(root)   # a .txt list of paths
           else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs
                 if not _excluded(os.path.join(d, f), root, EXCLUDE)])
    return sorted(p for p in src if p.lower().endswith(".pdf") and p not in done)


def scan(root=None):
    root = root or sys.argv[2]
    done, again = set(), set()
    if os.path.exists(INDEX):
        for r in (json.loads(l) for l in open(INDEX) if l.strip()):
            (again if retryable(r) else done).add(r["path"])
    files = _new_pdfs(root, done)
    again &= set(files)
    print("%d new files (%d already indexed)%s" % (len(files) - len(again), len(done),
          "、取り直す %d 件(前回は外部から取れなかった)" % len(again) if again else ""), file=sys.stderr)
    # 手で動かしたファイルを新しいファイルとして同定し直すと、手で直した書誌・判定が古い記録に取り残される。
    # パスにファイルが無い記録があり、新しいファイルがそれに当たるなら、先に relocate させる(--force で続ける)
    if files and os.path.exists(INDEX) and "--force" not in sys.argv:
        recs = load_index()[0]
        missing = [r for r in recs if not os.path.exists(r["path"]) and not moved_aside(r)]
        if missing:
            memo = {}
            got, amb, _ = relocate_plan(missing, files, lambda q: memo.setdefault(q, sh(["pdftotext", "-f", "1", "-l", "3", q, "-"])))
            if got or amb:
                sys.exit("新しいファイルのうち %d 件は、索引にある記録のファイルを手で動かしたもの(例: %s)。\n"
                         "先に papers relocate --apply で索引を直してから scan する(同定し直すと手で直した値が失われる)。\n"
                         "動かしたのではないなら papers scan ... --force" % (len(got) + len(amb), (got or amb)[0][0]["filename"]))
    if again:                                   # 取り直す記録は先に外す(同じパスの行を 2 つにしない)
        recs, stamp = load_index()
        _backup("retry")
        save_index([r for r in recs if r["path"] not in again], stamp)
    with open(INDEX, "a") as out:
        for i, p in enumerate(files):
            try:
                rec = resolve(p)
            except Exception as e:                      # one bad file must not kill the pass
                rec = {"path": p, "folder": os.path.dirname(p), "filename": os.path.basename(p),
                       "rung": "error", "error": str(e)[:200]}
            rec["scanned_at"] = time.strftime("%Y-%m-%d")      # papers recent が使う
            n = pdf_pages(p)                                    # 論文以外の候補(papers nonpaper)が使う
            if n is not None:                                   # 読めなかったら書かない(nonpaper-page が調べ直す)
                rec["n_pages"] = n
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()                                 # resumable: a Drive stall costs nothing
            if i % 25 == 0:
                print("  %d/%d %s" % (i, len(files), rec["rung"]), file=sys.stderr)


def add_lines(new):
    """add の報告。記録の表題と本文の冒頭を並べる。同定が合っているかは読む側が見る(ここでは判定しない)。"""
    out = []
    for r in new:
        out.append("%-12s %s" % (r["rung"], r["filename"]))
        if r.get("title"):
            out.append("   記録      %s %s %s | %s" % (r.get("first_author") or "-", r.get("year") or "-",
                       r.get("journal_short") or r.get("journal") or "-", r["title"][:90]))
        text = " ".join((r.get("text") or "").split())
        out.append("   本文の冒頭 %s" % (text[:110] or "(本文なし)"))
        if retryable(r):
            out.append("   取得失敗   %s。次の papers add で取り直す" % ", ".join(r["fetch_failed"]))
        elif needs_human(r):
            out.append("   人手行き   %s" % why(r))
    return out


def add():
    """papers add [<フォルダ>] [--force]。新しい PDF を索引に足して検索に出るようにし、動かす操作の予定まで出す。
    scan → enrich → 新しい記録の報告 → dedup と rename の dry run → 検索の索引。フォルダを省くと論文の置き場の全体。
    ファイルは動かさない。--apply は受けない(dedup・rename は同じ sys.argv を読むので、通すと承認なしに動く)。"""
    if "--apply" in sys.argv:
        sys.exit("add はファイルを動かさない。予定を見てから papers dedup --apply ・ papers rename --apply")
    recs = load_index()[0] if os.path.exists(INDEX) else []
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    root = args[0] if args else (os.path.commonpath([r["folder"] for r in recs]) if recs else None)
    if not root:
        sys.exit("索引が空。最初は papers add <論文の置き場>")
    before = {r["path"] for r in recs if not retryable(r)}     # 前回取れなかった記録は、新しい PDF と同じに扱う
    if _new_pdfs(root, before):
        if recs:
            _backup("add")
        scan(root)
        try:
            enrich()
        except SystemExit:                       # 抄録は後からでも取れる。ここで止めると報告と予定が出ない
            print("抄録の取得は中断した。あとで papers enrich")
        new = [r for r in load_index()[0] if r["path"] not in before]
        print("\n=== 新しい記録 %d 件: 段の判定を通った %d ・ 人手行き %d ・ 取得失敗 %d(同定が正しいかは、下の 2 行を見比べる)"
              % (len(new), sum(1 for r in new if renamable(r)),
                 sum(1 for r in new if needs_human(r) and not retryable(r)), sum(1 for r in new if retryable(r))))
        print("\n".join(add_lines(new)))
    else:
        print("新しい PDF は無い(%s)。最近入った記録は papers recent" % root)
    if BOOKS:                                    # 書籍の DB: 重複の集約と論文以外の移動はしない。同定できなかった本を知らせる
        un = [r for r in load_index()[0] if needs_human(r) and not retryable(r) and not moved_aside(r)]
        print("\n=== Not identified: %d record(s). Candidates and how to settle them: papers review" % len(un))
        for r in sorted(un, key=lambda r: r["filename"].lower()):
            print("  %5s p  %-60s %s" % (r.get("n_pages") or "?", r["filename"][:60], r.get("why_not") or why(r)))
    else:
        print("\n=== 重複(dry run)")
        dedup()
    print("\n=== 改名(dry run。まだ改名していない記録の全部)")
    try:
        rename()
    except SystemExit as e:                      # 書籍の DB: 読めない rename.tsv で、全頁の本文と検索の索引まで止めない
        if not BOOKS:
            raise
        print("rename plan skipped: %s" % e)
    if not BOOKS:
        print("\n=== 論文以外(dry run。本・学位論文・講義ノートなど)")
        nonpaper()
    print("\n=== 全頁の本文(検索用。スキャンは全頁を OCR する)")
    got, failed = fulltext_fill(load_index()[0])
    fulltext_report(got, failed)
    if got["read"] or got["ocr"] or not _db_format_ok() or os.path.getmtime(DB) < os.path.getmtime(INDEX):
        print("\n=== 検索の索引")
        build_db()
    if BOOKS:
        print("\nNothing was moved. Next: papers review (books not identified) / papers nonpaper-page (label chapters and notes) / "
              "papers rename (check rename.tsv, then --apply)")
        return
    print("\n動かすのは利用者の承認のあと:\n"
          "  papers dedup --apply                                   # 重複の予定があるとき\n"
          "  papers rename --apply && papers index && papers views   # 改名の予定があるとき\n"
          "  papers nonpaper-page → papers nonpaper-ok <書き出したファイル> → papers nonpaper --apply   # 論文以外の候補があるとき\n"
          "ページの検証(任意・有料)は、まだ検証していない記録だけが対象。見積もりと手順は MANUAL.md の §4")


def stats():
    recs = [json.loads(l) for l in open(INDEX) if l.strip()]
    by = {}
    for r in recs:
        by[r["rung"]] = by.get(r["rung"], 0) + 1
    n = len(recs)
    print("n=%d" % n)
    for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
        print("  %-14s %4d  %5.1f%%" % (k, v, 100.0 * v / n))
    res = sum(v for k, v in by.items() if k not in TERMINAL)
    print("  %-14s %4d  %5.1f%%  (rename 対象)" % ("RESOLVED", res, 100.0 * res / n))
    hand = sum(1 for r in recs if needs_human(r))
    np_ = sum(1 for r in recs if r.get("not_paper"))
    print("  %-14s %4d  %5.1f%%  (review で人へ)" % ("要人手", hand, 100.0 * hand / n))
    if np_:
        print("  %-14s %4d  %5.1f%%  (人が「論文ではない」とした。これを除く解決率 %.1f%%)"
              % ("論文でない", np_, 100.0 * np_ / n, 100.0 * res / max(n - np_, 1)))
    supp = by.get("supplement", 0)
    print("  %-14s %4d  %5.1f%%  (補足資料。触れない)" % ("SI", supp, 100.0 * supp / n))
    print("\n--- unresolved sample ---")
    for r in [x for x in recs if x["rung"] == "unresolved"][:20]:
        print("  %-45s notext=%-5s %s" % (r["filename"][:45], r.get("no_text"),
                                          (r.get("title_guess") or "")[:60]))




# ---------- rename pass ----------
# Reads the index only; never opens a PDF.  Dry-run unless --apply.
BAD = re.compile(r'[/:\x00-\x1f]')


def safe(s, limit=180):
    s = BAD.sub("-", " ".join(str(s).split())).strip(" .")
    while len(s.encode("utf-8")) > limit:          # HFS caps at 255 *bytes*; kanji cost 3
        s = s[:-1]
    return s


def tight(s):
    """'Opt. Commun.' -> 'OptCommun'。完全な誌名は papers.jsonl 側にあるので、
    ファイル名は人が目視・入力しやすい形を優先する。"""
    return re.sub(r'[.\s]+', '', re.sub(r'^The\s+', '', str(s), flags=re.I))


JOURNAL_MAX = 40


def journal_tag(r):
    """ファイル名の略誌名。Crossref の略誌名(無ければ誌名)を詰めたもの。40 字を超えるときだけ、「:」の前
    (Applied Physics B)、誌名の中の括弧の略称(CLEO-PR・BBA)、大文字で始まる語の頭文字(NIM A → NIMPRSA)の順に試す。
    Elsevier は略誌名に正式名が入り、会議録は略誌名が無いので長くなる。"""
    s = r.get("journal_short") or r["journal"]
    t = tight(s)
    if len(t) <= JOURNAL_MAX:
        return t
    head = tight(s.split(":")[0])
    if len(head) <= JOURNAL_MAX:
        return head
    m = re.search(r'\(([A-Za-z][A-Za-z0-9-]{1,15})\)', s)
    if m:
        return m.group(1)
    words = re.findall(r"[A-Za-z][\w'-]*|\d+", s.split(":")[0])
    if words and re.fullmatch(r"(19|20)\d\d", words[0]):
        words = words[1:]                                   # 会議の年はファイル名の先頭にある
    roman = lambda w: re.fullmatch(r"[IVXLC]{2,}", w)
    ini = "".join(w if w.isdigit() or roman(w) else w[0] for w in words if w[0].isupper() or w.isdigit())
    return (ini or t)[:JOURNAL_MAX]


def start_page(pages):
    """頁の範囲の開始頁。「C4-35-C4-55」(頁に前置きの付く会議録)は C4-35。「3 pp.」は頁数なので None。"""
    p = str(pages or "").strip()
    if not p or re.search(r"\bpp?\.?$", p):
        return None
    parts = p.split("-")
    return "-".join(parts[:len(parts) // 2]) if len(parts) % 2 == 0 else parts[0]


def filename_for(r):
    """公開年_第一著者_略誌名_巻_号_開始頁。巻号頁は重複の区別のためだけに入れる。"""
    parts = [str(r["year"]), tight(re.sub(r"[*†‡]", "", r["first_author"])), journal_tag(r)]
    if r.get("arxiv_id"):
        return "_".join(parts + [r["arxiv_id"]])   # プレプリントに巻号頁は無い
    parts += [str(v) for v in (r.get("volume"), r.get("issue")) if v]
    if start_page(r.get("pages")):
        parts.append(start_page(r["pages"]))
    elif r.get("article_number"):
        parts.append(str(r["article_number"]))
    return "_".join(parts)



def rename():
    apply = "--apply" in sys.argv
    recs, stamp = load_index()
    # 誌名が無いと名前を作れない(人が誌名を入れずに直した学位論文・本)。その記録は、利用者の付けた名前のまま残す
    ok = [r for r in recs if renamable(r) and (r.get("journal_short") or r.get("journal")) and not moved_aside(r)]
    name_of, book_vals = filename_for, {}
    if BOOKS:                                      # 書籍の DB: 名前は「著者 - 表題 (版, 年)」。版と年は rename.tsv で人が直してから適用する
        if apply and not os.path.exists(RENAME_TSV):
            sys.exit("rename.tsv does not exist yet. Run papers rename first, check the edition and year columns, then --apply.")
        ok = [r for r in recs if book_renamable(r)]
        book_vals = book_rename_table(ok, apply)
        no_row = [r for r in ok if r["path"] not in book_vals]      # 適用のときだけ: 表に行の無い記録は、そのまま残す
        ok = [r for r in ok if r["path"] in book_vals]
        name_of = lambda r: book_filename(r, *book_vals[r["path"]])
    skipped = [r for r in recs if r not in ok]
    # ファイルの無い記録は改名しない。その記録のパスは「使われている」に入れる: 空いて見えるので、別の記録がその名前へ改名されると、
    # 続けてこの記録の改名が「その名前にあるファイル」(別の記録の PDF)を動かし、PDF と書誌が入れ替わる
    gone = [r for r in ok if not os.path.exists(r["path"])]
    ok = [r for r in ok if os.path.exists(r["path"])]

    plan, case_only, seen = [], 0, {_same_name(r["path"]) for r in recs if not os.path.exists(r["path"])}
    for r in ok:                                   # もう正しい名前の記録の名前は、先に「使われている」に入れる。入れないと、
        if safe(name_of(r)) + ".pdf" == r["filename"]:      # 大小文字を区別するボリュームで、_2 付きの記録がその名前へ戻ろうとする
            seen.add(_same_name(r["path"]))
    for r in ok:
        target = safe(name_of(r)) + ".pdf"
        dst = os.path.join(r["folder"], target)
        if target == r["filename"]:
            continue                               # idempotent: already correct
        if BOOKS and _same_name(dst) == _same_name(r["path"]):
            case_only += 1                         # 大小文字・合成/分解だけの違い: その場では変えない(_occupied)。_2 も付けない
            continue
        i = 2
        while _same_name(dst) in seen or _occupied(dst, r["path"]):
            dst = os.path.join(r["folder"], safe(name_of(r)) + "_%d.pdf" % i)
            i += 1
        seen.add(_same_name(dst))
        if dst == r["path"]:
            continue                               # もう _2 付きの名前になっている(動かすものは無い)
        plan.append({"from": r["path"], "to": dst, "doi": r.get("doi"), "rung": r["rung"]})

    with open(os.path.join(ROOT, "unresolved.txt"), "w") as f:
        for r in skipped:
            f.write("%s\t%s\n" % (r.get("rung"), r["path"]))
    if not apply:
        for p in plan[:40]:
            print("  %s\n->%s" % (os.path.basename(p["from"]), os.path.basename(p["to"])))
        print("\n%d rename / %d 変更不要 / %d 触れない%s\nDRY RUN. 実行するには --apply"
              % (len(plan), len(ok) - len(plan), len(skipped),
                 " / ファイルが無いので改名しない %d(papers relocate)" % len(gone) if gone else ""))
        if BOOKS and case_only:
            print("%d name(s) differ from the current name only in letter case and are left as they are." % case_only)
        if BOOKS:
            print("Author and title come from the catalogue (some catalogues list only the first author); edition and year come from the file name,\n"
                  "the copyright page or the catalogue. Any of them can be wrong. Check and edit the %s columns of %s\n"
                  "(rows are matched by the path column; the remaining columns are not read), run papers rename again to see the names, then --apply.\n"
                  "--apply renames only the rows that are in the table: delete the rows you do not want renamed." % (" / ".join(RENAME_EDIT), RENAME_TSV))
        return
    # 書籍の DB: 表で確かめた版と年を記録に書く(名前が変わらない記録にも)
    named = [r for r in ok if BOOKS and (r.get("name_edition"), r.get("name_year")) + book_name_parts(r) + (bool(r.get("named_at")),)
             != book_vals[r["path"]] + (True,)]
    if BOOKS and no_row:
        print("%d record(s) have no row in rename.tsv and are left as they are." % len(no_row))
    if named and not plan:
        _backup("rename")
    for r in named:                                # 目録の項目(year・title・authors)は変えない。名前に使う値だけを別に持つ
        r["name_edition"], r["name_year"], r["name_author"], r["name_title"] = book_vals[r["path"]]
        r["named_at"] = time.strftime("%Y-%m-%d")
    if plan:                                       # 空の記録は書かない(書くと、直前の move・nonpaper を undo で戻せなくなる)
        _backup("rename")
        trail = _trail("renames")
        with open(trail, "w") as f:                # --apply のときだけ。written BEFORE any mv
            for p in plan:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(os.path.join(ROOT, "unresolved.txt"), "w") as f:
        for r in skipped:
            f.write("%s\t%s\n" % (r.get("rung"), r["path"]))

    for p in plan[:40]:
        print("  %s\n->%s" % (os.path.basename(p["from"]), os.path.basename(p["to"])))
    print("\n%d rename / %d 変更不要 / %d 触れない(unresolved.txt)%s" %
          (len(plan), len(ok) - len(plan), len(skipped),
           " / ファイルが無いので改名しない %d(papers relocate)" % len(gone) if gone else ""))
    if BOOKS and case_only:
        print("%d name(s) differ from the current name only in letter case and are left as they are." % case_only)
    moved = {}
    try:
        for p in plan:
            if not os.path.exists(p["from"]):      # already moved by an earlier run
                continue
            _move_file(p["from"], p["to"])
            moved[p["from"]] = p["to"]
    finally:                                       # 途中で止まっても、動かした分は索引に書く
        if moved or named:                         # keep the index truthful, or the next
            recs2 = []                             # rename run plans from dead paths
            for r in recs:
                if r["path"] in moved:
                    r["path"] = moved[r["path"]]
                    r["filename"] = os.path.basename(r["path"])
                recs2.append(r)
            repoint(recs2, moved)
            save_index(recs2, stamp)
        _stopped("rename", len(moved))
    if BOOKS and not plan:                         # undo は「最後に動かした組」を戻す。動かしていないのに勧めると、前の改名が戻る
        print("No file was moved. %d record(s) updated with the table's values." % len(named))
        return
    print("done (%d moved). 復元: python3 organize.py undo" % len(moved))


def undo():
    import glob
    trails = sorted(glob.glob(os.path.join(ROOT, "renames-*.jsonl")))
    if not trails:
        print("no rename trail"); return
    lines = [json.loads(l) for l in open(trails[-1]) if l.strip()]      # most recent run only
    if any(os.path.exists(p["to"]) for p in lines):      # 戻すものがあるときだけ(何度打っても退避が増えない)
        _backup("undo")
    done, kept = set(), []
    try:
        for p in reversed(lines):                  # 後に動かしたものから戻す(同じファイルを 2 度動かした記録でも元へ戻る)
            if not os.path.exists(p["to"]):
                continue
            if _occupied(p["from"], p["to"]):      # 元の場所に、あとから別のファイルが置かれた。上書きしない
                kept.append(p)
                continue
            os.makedirs(os.path.dirname(p["from"]), exist_ok=True)       # 元のフォルダが消されていても戻す
            _move_file(p["to"], p["from"])
            done.add(p["to"])
    finally:                                       # 途中で止まっても、戻した分は索引に書く
        recs, stamp = load_index()
        # 索引を戻すのは、ファイルがもう移動先に無いものだけ(今戻した・前に戻した)。戻せなかったものは今の場所を指したままにする
        # 前に戻したものは、移動先に無く、元の場所にある。どちらにも無い(置き場が見えない・消された)ものは、索引を戻さない
        back = {p["to"]: p["from"] for p in lines
                if p["to"] in done or (not os.path.exists(p["to"]) and os.path.exists(p["from"]))}
        _undo_index(recs, stamp, back, {p["to"] for p in lines if p.get("removed")})
        _stopped("undo", len(done))
    if kept:
        print("戻さなかった %d 件(元の場所に別のファイルがある。今の場所に残した):" % len(kept))
    for p in kept[:10]:
        print("  %s" % p["to"])
    print("reverted %d 件 (%s)" % (len(done), os.path.basename(trails[-1])))


def _undo_index(recs, stamp, back, unremove=()):
    """記録のパスを戻す。unremove は、DB から外す操作(remove)で移したパス: 戻したら、外した印も消す(残すと、戻したのに検索に出ない)。"""
    for r in recs:
        if r["path"] in back:
            if r["path"] in unremove:
                r.pop("removed", None)
            r["path"] = back[r["path"]]             # 1 段だけ。記録の A→B と B→C は、別々の記録の移動(たどると 2 件が同じパスになる)
            r["filename"] = os.path.basename(r["path"])
            r["folder"] = os.path.dirname(r["path"])      # move・nonpaper はフォルダも変える。戻さないと記録の中で食い違う
    repoint(recs, back)
    save_index(recs, stamp)




# ---------- human handoff ----------
# 未解決は自動で埋めない。人が入力できる形に分類して渡し、書き戻す。
REVIEW_COLS = ["doi", "year", "first_author", "journal", "journal_short",
               "volume", "issue", "pages", "title"]


def why(r):
    if BOOKS and r.get("rung") not in TERMINAL:
        return "H_identified, but the catalogue gives no author or no year (set them: papers fix <file> --set year=... --set first_author=...)"
    if r.get("rung") not in TERMINAL:
        return "F_記録に著者か年が無い(論文でない記録に着いた疑い)"   # 段は通ったが改名できない
    if retryable(r):
        return "E_外部から取れなかった(次の scan・add で取り直す)"
    if r.get("rung") == "error":
        return "E_スキャン時にエラー"          # 読み取り自体が失敗。再スキャンで直ることもある
    if r.get("why_not"):
        return "H_not identified as a book: %s" % r["why_not"]
    if r.get("rung") == "unverified" and r.get("held_by") == "vet":
        return "G_ページの表題と記録の表題が食い違う"      # 解決済みを vet が人に回した
    if r.get("rung") == "unverified":
        return "D_候補はあるが本文と合わない"
    if r.get("no_text"):
        return "A_本文なし" if not r.get("ocr") else "A_OCRしても読めず"
    if len((r.get("title_guess") or "")) < 15:
        return "C_タイトル候補が取れず"
    # Crossref に無い文献だけではない: 自動の問い合わせが外れただけの論文も入る(DESIGN.md「8. 未解決の人手引き渡し」)
    return "B_タイトルは読めたが自動の検索で見つからない"   # 学位論文・書籍・予稿と、問い合わせが外れた論文


REVIEW_TSV = os.path.join(ROOT, "unresolved.tsv")
# 候補の列(review が埋める)と、利用者が埋める列。候補は ok に y を付けたときだけ merge が書く
CAND_COLS = ["cand_doi", "cand_title", "cand_where", "cand_year", "ok"]
# ok 列の値。y 正しい / m 正しいが手元の PDF は出版前の原稿 / n 候補が違う / x 論文ではない(本の抜粋・講義ノート・資料)
OK_VALUES = ("y", "m", "n", "x")
USER_COLS = ["ok"] + REVIEW_COLS


def review_accept(r, it):
    """Crossref の検索結果 it を、人手行きの r の候補として review の表に出すか。Haiku が読んだ表題
    (page_title、PDF に字面で確認済み)と第一著者で照合する。誤りは同名の別の版・同じ著者の別の本なので、人が y / n で決める
    (測定は DESIGN.md「Haiku が読んだ表題で候補を出す」)。"""
    doi = (it.get("DOI") or "").lower()
    if not doi or it.get("type") in ("component", "peer-review", "dataset") or doi in (r.get("cand_rejected") or []):
        return False
    a = _surname((it.get("author") or [{}])[0].get("family"))
    if not a or (r.get("page_author") and a != _surname(r["page_author"])):
        return False
    return titles_agree((it.get("title") or [""])[0], r.get("page_title"))


def review_candidate(r):
    """r の候補(Crossref の検索結果 1 件)か None。表題が字面で確認できない・和文なら引かない。"""
    t = r.get("page_title")
    if not t or not r.get("page_title_literal") or _CJK.search(t):
        return None
    q = "%s %s" % (t, r.get("page_author") or "")
    try:
        its = get_json("https://api.crossref.org/works?rows=5&mailto=%s&select=DOI,title,author,issued,type,container-title"
                       "&query.bibliographic=%s" % (MAILTO, urllib.parse.quote(q)))["message"]["items"]
    except Exception:
        return None
    return next((it for it in its if review_accept(r, it)), None)


def _read_review(p):
    """unresolved.tsv を merge と同じ読み方で読む。(見出し, 人が記入した行) を返す。
    記入とみなすのは利用者の列(ok と書誌の列)だけ。review が埋めた候補の列は記入ではない。"""
    rows = [l.rstrip("\n").split("\t") for l in open(p)]
    cols = [i for i, h in enumerate(rows[0]) if h in USER_COLS]
    return rows[0], [r for r in rows[1:] if any(i < len(r) and r[i].strip() for i in cols)]


def _unmerged(p, recs):
    """記入済みで、まだ merge していない行。索引でまだ人手行きのもの、または索引に無いもの。
    review の再実行で消えてはいけないのはこれだけ(merge 済みの行は索引に入っている)。"""
    if not os.path.exists(p):
        return []
    head, rows = _read_review(p)
    by = {r["path"]: r for r in recs}
    col = {h: i for i, h in enumerate(head)}
    def only_rejected(r):                                  # n だけで、merge がもう「違う」と記録した行
        get = lambda k: r[col[k]].strip() if k in col and col[k] < len(r) else ""
        return (get("ok").lower() == "n" and not any(get(k) for k in REVIEW_COLS)
                and get("cand_doi") in (by[r[1]].get("cand_rejected") or []))
    return [r for r in rows if r[1] not in by or (needs_human(by[r[1]]) and not only_rejected(r))]


def review():
    """unresolved.tsv を書き出す。doi 列だけ埋めれば Crossref から全項目を取る。
    Crossref に無い文献(学位論文・書籍・予稿)は他の列を直接埋める。
    記入済みで未 merge の行があれば上書きしない(記入を失わないため)。書籍の DB では book_review。"""
    if BOOKS:
        return book_review()
    recs = [json.loads(l) for l in open(INDEX) if l.strip()]
    un = [r for r in recs if needs_human(r)]
    p = REVIEW_TSV
    pending = _unmerged(p, recs)
    if pending and "--force" not in sys.argv:
        sys.exit("%s に記入済みで未 merge の行が %d 件ある。上書きしない。\n"
                 "先に `python3 organize.py merge` を実行するか、"
                 "--force で既存のファイルを退避してから作り直す。" % (p, len(pending)))
    if pending:
        kept = p[:-4] + "-" + time.strftime("%Y%m%d-%H%M%S") + ".tsv"
        os.replace(p, kept)
        print("記入済みの %d 行を含む既存ファイルを %s に退避した" % (len(pending), kept))
    lines = ["\t".join(["reason", "path", "hint"] + CAND_COLS + REVIEW_COLS) + "\n"]
    tsv = lambda v: " ".join(str(v or "").split())                # 外部の文字列のタブ・改行が列を壊さないように
    ncand = 0
    cands = {r["path"]: review_candidate(r) for r in un}
    for r in sorted(un, key=lambda r: (cands[r["path"]] is None, why(r))):   # 候補のある行を上に(y / n を付けやすく)
        hint = (r.get("page_title") or r.get("title") or r.get("title_guess") or "")[:120]   # Haiku が読んだ表題を優先
        c = cands[r["path"]]
        cand = ["", "", "", ""] if not c else [c["DOI"].lower(), clean_text((c.get("title") or [""])[0])[:150],
                                             clean_text((c.get("container-title") or [""])[0])[:60],
                                             ((c.get("issued") or {}).get("date-parts") or [[None]])[0][0]]
        ncand += bool(c)
        lines.append("\t".join([why(r), r["path"], tsv(hint)] + [tsv(v) for v in cand] +
                               [""] * (1 + len(REVIEW_COLS))) + "\n")
    _atomic_write(p, lines)
    n = collections.Counter(why(r) for r in un)
    print("%s に %d 件（解決済み %d 件）" % (p, len(un), len(recs) - len(un)))
    for k, v in sorted(n.items()):
        print("  %-28s %d" % (k, v))
    print("候補あり %d 件: 正しければ ok 列に y、違えば n。" % ncand)
    print("\n候補が無い・違うものは doi 列を埋めて `python3 organize.py merge`。"
          "Crossref に無いものは year/first_author/journal/volume/issue/pages を直接。")


def merge():
    """unresolved.tsv の人手入力を索引に書き戻す。doi があれば Crossref を正とする。書籍の DB では book_merge。"""
    if BOOKS:
        return book_merge()
    head, rows = _read_review(REVIEW_TSV)
    by_path, rejected, not_journal, not_paper, manuscript = {}, {}, [], set(), set()
    for r in rows:
        d = {k: v.strip() for k, v in zip(head, r)}
        ok = d.get("ok", "").lower()
        if ok == "x":                                      # 論文ではない: 人手行きから外す。同定はしない
            not_paper.add(d["path"])
            if d.get("cand_doi"):
                rejected[d["path"]] = d["cand_doi"]
            continue
        took = ok in ("y", "m") and d.get("cand_doi") and not d.get("doi")
        if took:
            d["doi"] = d["cand_doi"]                        # 候補を採る
        elif ok == "n" and d.get("cand_doi"):
            rejected[d["path"]] = d["cand_doi"]             # 次から候補に出さない
        if ok == "m":
            manuscript.add(d["path"])                       # 論文は合っているが、手元の PDF は出版前の原稿
        if not any(d.get(k) for k in REVIEW_COLS):
            continue                                       # n だけの行: 同定はまだ
        rec = from_crossref(d["doi"]) if d.get("doi") else None
        if rec is None and took and not any(d.get(k) for k in REVIEW_COLS if k != "doi"):
            not_journal.append(d["path"])                   # 学位論文・書籍など。書誌の列を埋めてもらう
            continue
        if rec is None:
            rec = {k: (d.get(k) or None) for k in REVIEW_COLS}
            rec["year"] = int(rec["year"]) if (rec.get("year") or "").isdigit() else None
            rec["source"] = "manual"
        else:
            rec["source"] = "crossref"
        rec["rung"] = "manual"
        by_path[d["path"]] = rec
    recs, stamp = load_index()
    hit = 0
    for r in recs:
        if r["path"] in rejected:
            r["cand_rejected"] = (r.get("cand_rejected") or []) + [rejected[r["path"]]]
        if r["path"] in not_paper:
            r["not_paper"] = "user"
        if r["path"] in by_path:
            r.update(by_path[r["path"]])
            if r["path"] in manuscript:
                r["version_note"] = "manuscript"
            hit += 1
    _backup("merge")
    save_index(recs, stamp)
    print("%d 件を索引に反映(うち原稿 %d)、候補を違うとした %d 件、論文ではないとした %d 件"
          % (hit, sum(1 for r in recs if r["path"] in manuscript and r["path"] in by_path), len(rejected) - len(
              [p for p in rejected if p in not_paper]), len(not_paper)))
    if not_journal:
        print("y を付けたが Crossref で雑誌の論文として引けない %d 件は書いていない(学位論文・書籍など)。"
              "year/first_author/journal などの列を埋めて再度 merge:" % len(not_journal))
        for q in not_journal[:20]:
            print("  " + q)


# ---------- 出版版の候補(arXiv 版に published_doi が無いもの。人が確かめてから書く) ----------
# 候補は自動では書かず、published.tsv で人が y/n を付ける(見つけた候補に誤りが混ざる。測定は DESIGN.md「出版版の候補」)。
PUBVER_TSV = os.path.join(ROOT, "published.tsv")
PUBVER_COLS = ["path", "arxiv_title", "page_title", "cand_doi", "cand_title", "cand_where", "cand_year", "cand_type", "ok"]
_NOT_PUBLISHED = ("posted-content", "component", "peer-review", "dataset")


def _surname(s):
    w = (s or "").replace(",", " ").split()
    return norm(w[-1]) if w else ""


def pubver_target(r):
    """出版版を探す対象: arXiv 版として解決済みで、arXiv に出版版 DOI も掲載誌(journal_ref)も無いもの。
    journal_ref があれば掲載先は arXiv が知っているので、探さない(探すと同名の別の文献に着きうる)。"""
    return (renamable(r) and bool(r.get("arxiv_id")) and not r.get("doi")
            and not r.get("published_doi") and not r.get("journal_ref"))


def pubver_accept(r, it):
    """Crossref の検索結果 it を r の出版版の候補として採るか。著者が無い候補は採らない
    (著者の照合ができず、他人の文献に着きうる)。"""
    doi = (it.get("DOI") or "").lower()
    if not doi or doi.startswith("10.48550") or it.get("type") in _NOT_PUBLISHED:
        return False
    if doi in (r.get("published_doi_rejected") or []):
        return False
    a = _surname((it.get("author") or [{}])[0].get("family"))
    if not a or a != _surname(r.get("first_author")):
        return False
    if not any(titles_agree((it.get("title") or [""])[0], t) for t in (r.get("title"), r.get("page_title")) if t):
        return False
    y = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
    return not (isinstance(y, int) and r.get("year") and y < int(r["year"]) - 1)


def _pubver_filled(p, recs):
    """ok 列が記入済みで、まだ書き戻していない行(索引でまだ対象のもの)。"""
    if not os.path.exists(p):
        return []
    rows = [l.rstrip("\n").split("\t") for l in open(p)][1:]
    by = {r["path"]: r for r in recs}
    return [x for x in rows if len(x) == len(PUBVER_COLS) and x[-1].strip()
            and x[0] in by and pubver_target(by[x[0]]) and x[3] not in (by[x[0]].get("published_doi_rejected") or [])]


def pubver():
    """published.tsv に候補を書き出す(索引は変えない)。ok 列に y / n を付けて `pubver --merge`。"""
    if "--merge" in sys.argv:
        return pubver_merge()
    recs, _ = load_index()
    pending = _pubver_filled(PUBVER_TSV, recs)
    if pending:
        sys.exit("%s に記入済みで未 merge の行が %d 件ある。上書きしない。先に `python3 organize.py pubver --merge`"
                 % (PUBVER_TSV, len(pending)))
    lines, n = ["\t".join(PUBVER_COLS) + "\n"], 0
    tsv = lambda v: " ".join(str(v or "").split())                # 外部の文字列のタブ・改行が列を壊さないように
    for r in (r for r in recs if pubver_target(r)):
        n += 1
        titles = [r.get("title")] + [r["page_title"]] if r.get("page_title") and not titles_agree(r["page_title"], r.get("title")) else [r.get("title")]
        hit = None
        for t in titles:
            q = "%s %s" % (t, r.get("first_author") or "")
            try:
                its = get_json("https://api.crossref.org/works?rows=5&mailto=" + MAILTO +
                               "&select=DOI,title,author,issued,type,container-title&query.bibliographic=" +
                               urllib.parse.quote(q))["message"]["items"]
            except Exception as e:
                print("  取得できない: %s (%s)" % (r["filename"], e)); break
            hit = next((it for it in its if pubver_accept(r, it)), None)
            if hit:
                break
        if hit:
            lines.append("\t".join(tsv(v) for v in [
                r["path"], r.get("title"), r.get("page_title"), hit["DOI"].lower(), clean_text((hit.get("title") or [""])[0]),
                clean_text((hit.get("container-title") or [""])[0]),
                ((hit.get("issued") or {}).get("date-parts") or [[None]])[0][0], hit.get("type"), ""]) + "\n")
    _atomic_write(PUBVER_TSV, lines)
    print("対象 %d 件、候補 %d 件を %s に。ok 列に y(出版版)/ n(違う)を付けて `python3 organize.py pubver --merge`"
          % (n, len(lines) - 1, PUBVER_TSV))


def compact(r, with_text=False):
    """エージェント向けの JSON。1 頁目の本文(text)は既定で外す(出力の大半を占める)。"""
    return r if with_text else {k: v for k, v in r.items() if k != "text"}


def norm_id(s):
    """DOI・arXiv ID の書き方の揺れを揃える。("doi" | "arxiv" | None, 値)。"""
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s.strip(), flags=re.I)
    m = re.match(r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)(.+?)(?:v\d+)?(?:\.pdf)?$", s, re.I)
    if m:
        return "arxiv", m.group(1).lower()
    if re.fullmatch(r"\d{4}\.\d{4,5}(v\d+)?|[a-z-]+(\.[a-z]{2})?/\d{7}(v\d+)?", s, re.I):
        return "arxiv", re.sub(r"v\d+$", "", s).lower()
    if s.startswith("10."):
        return "doi", s.lower()
    return None, s


def has_lookup(recs, ids):
    """識別子ごとに、持っているかを完全一致で答える。状態の強い順:
    有り(同定済み) > プレプリントのみ(arXiv 版の published_doi が一致) > 候補(unverified の DOI) > 補足資料のみ > 無し。
    dedup が移した重複に当たったら、残した方のパスを返す。"""
    by_path = {r["path"]: r for r in recs}
    out = []
    for raw in ids:
        kind, v = norm_id(raw)
        best, via_pub = (9, None), False
        for r in recs:
            if r.get("removed"):
                continue                                          # 利用者が DB から外した記録は、持っているものに数えない
            if kind == "doi" and (r.get("doi") or "").lower() == v:
                rank = 0 if renamable(r) else 2 if r.get("rung") == "unverified" else 3 if r.get("rung") == "supplement" else 9
            elif kind == "doi" and (r.get("published_doi") or "").lower() == v and r.get("arxiv_id"):
                rank = 1
            elif kind == "arxiv" and (r.get("arxiv_id") or "").lower() == v:
                rank = 0 if renamable(r) else 9
            elif kind == "arxiv" and (r.get("preprint_arxiv_id") or "").lower() == v and renamable(r):
                best, via_pub = (0, r), True                      # arXiv 版は利用者が消した。出版版を持っている
                continue
            else:
                continue
            pub = False
            if moved_aside(r) and rank == 0:
                keep = by_path.get(r.get("duplicate_of") or r.get("published_copy"))
                pub = bool(keep and r.get("published_copy"))
                r = keep or r
            if rank < best[0] or (rank == best[0] and moved_aside(best[1] or {}) and not moved_aside(r)):
                best, via_pub = (rank, r), pub
        st = ["有り", "プレプリントのみ", "候補(未検証)", "補足資料のみ"][best[0]] if best[0] < 4 else "無し"
        if via_pub:
            st = "有り(出版版)"                              # arXiv 版は _プレプリント(出版版あり)/ に移してある
        r = best[1] or {}
        out.append({"id": raw, "status": st, "path": r.get("path"), "rung": r.get("rung"), "title": r.get("title")})
    return out


def has():
    """papers has <DOI や arXiv ID…> [--file 一覧] [--json]。持っているかを完全一致で答える(検索ではない)。"""
    ids = [a for a in sys.argv[2:] if not a.startswith("--") and sys.argv[sys.argv.index(a) - 1] != "--file"]
    if "--file" in sys.argv:
        ids += [l.strip() for l in open(sys.argv[sys.argv.index("--file") + 1]) if l.strip()]
    if not ids:
        sys.exit(has.__doc__)
    res = has_lookup(load_index()[0], ids)
    for x in res:
        if "--json" in sys.argv:
            print(json.dumps(x, ensure_ascii=False))
        else:
            print("%-34s %-14s %s" % (x["id"][:34], x["status"], x["path"] or ""))
    if "--json" not in sys.argv:
        print("有り %d / %d" % (sum(x["status"].startswith("有り") for x in res), len(res)))


def _bib_authors(r):
    """BibTeX の著者。Crossref の応答(キャッシュ)か記録の families に姓・名の区別があれば「姓, 名」。無ければ索引の値のまま
    (arXiv は「名 姓」で BibTeX がそのまま読める。Crossref 由来の「姓 名」は区切りが失われている)。"""
    if r.get("doi") and r.get("source") == "crossref":
        try:
            m = get_json("https://api.crossref.org/works/" + urllib.parse.quote(r["doi"]) + "?mailto=" + MAILTO)["message"]
            a = [(x.get("family") or "") + (", " + x["given"] if x.get("given") else "") for x in m.get("author") or []]
            if a:
                return a
        except Exception:
            pass
    names, fams = r.get("authors") or [], r.get("families") or []
    if names and len(fams) == len(names):            # 目録・登録機関が姓を分けて返した記録(本、Crossref 以外の DOI)
        return [_family_first(n, f) for n, f in zip(names, fams)]
    return names or [r.get("first_author") or ""]


def _family_first(name, family):
    """「姓, 名」。名前の端に姓が無ければ、名前のまま(姓だけの名前・団体名・直した後の食い違い)。"""
    if family and name.startswith(family + " "):
        return family + ", " + name[len(family) + 1:]
    if family and name.endswith(" " + family):
        return family + ", " + name[:-len(family) - 1]
    return name


def bibtex(r, authors):
    """索引の記録から BibTeX を 1 件作る。未検証の記録には注意書きを付ける。"""
    fold = lambda x: unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode()
    word = next((w for w in re.findall(r"[A-Za-z]{4,}", fold(r.get("title"))) if w.lower() not in ("with", "from", "this", "that")), "")
    key = re.sub(r"[^A-Za-z0-9]", "", fold(r.get("first_author"))) + str(r.get("year") or "") + word
    arx = r.get("arxiv_id") and not r.get("doi")
    f = [("author", " and ".join(authors)), ("title", "{%s}" % r["title"] if r.get("title") else None)]
    if arx:
        f += [("year", r.get("year")), ("eprint", r["arxiv_id"]), ("archivePrefix", "arXiv")]
        if r.get("published_doi"):
            f.append(("note", "Published: https://doi.org/%s" % r["published_doi"]))
    else:
        pages = r.get("pages") or r.get("article_number")
        f += [("journal", r.get("journal")), ("year", r.get("year")), ("volume", r.get("volume")),
              ("number", r.get("issue")), ("pages", str(pages).replace("-", "--") if pages else None), ("doi", r.get("doi"))]
    book = bool(r.get("isbn")) and not arx and not r.get("journal")      # 目録で同定した本
    if book:
        f = [("editor" if r.get("authors_role") == "editor" else "author", " and ".join(authors)), f[1], ("publisher", r.get("publisher")),
             ("year", r.get("name_year") or r.get("year")), ("edition", r.get("name_edition")), ("isbn", r["isbn"]), ("doi", r.get("doi"))]
    head = "% 注意: 未検証の記録(DOI は 1 頁目で裏が取れていない)\n" if r.get("rung") == "unverified" else ""
    body = ",\n".join("  %s = {%s}" % (k, v) for k, v in f if v)
    kind = "book" if book else "article" if r.get("journal") and not arx else "misc"     # 誌名の無い記録(人が直した学位論文)は article にしない
    return "%s@%s{%s,\n%s\n}" % (head, kind, key, body)


def cite():
    """papers cite <DOI・arXiv ID・パス・ファイル名の一部>…。手元の論文の BibTeX(索引から作る)。"""
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if not args:
        sys.exit(cite.__doc__)
    recs = load_index()[0]
    for a in args:
        kind, v = norm_id(a)
        hit = [r for r in recs if (kind == "doi" and (r.get("doi") or "").lower() == v) or
               (kind == "arxiv" and (r.get("arxiv_id") or "").lower() == v)] if kind else \
            ([r for r in recs if r["path"] == a] or [r for r in recs if a in r["filename"]])
        hit = sorted(hit, key=lambda r: (moved_aside(r), not renamable(r)))
        if kind is None and len(hit) > 1 and len({(r.get("doi"), r.get("arxiv_id")) for r in hit}) > 1:
            print("%% %s: %d 件に当たる。1 件に絞れる名前か DOI で:\n%s" % (a, len(hit), "\n".join("%%   " + r["filename"] for r in hit[:10])))
            continue
        if not hit or not (renamable(hit[0]) or hit[0].get("rung") == "unverified"):
            print("%% %s: 書誌のある記録が無い(索引に無いか、同定できていない)" % a)
            continue
        print(bibtex(hit[0], _bib_authors(hit[0])) + "\n")


def recent():
    """papers recent [--n=20] [--json]。索引に最後に入った記録(scan の順)。古い記録には scanned_at が無い。"""
    n = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--n=")), "20"))
    for r in load_index()[0][-n:][::-1]:
        if "--json" in sys.argv:
            print(json.dumps(compact(r), ensure_ascii=False))
        else:
            print("%-10s %-13s %-4s %-16s %s\n           %s" % (r.get("scanned_at") or "-", r.get("rung"), r.get("year") or "-",
                  (r.get("first_author") or "-")[:16], (r.get("title") or r["filename"])[:70], r["path"]))


def _fingerprint(r):
    """記録の本文(1〜2 頁目の先頭 3,000 字)から、別の場所に移ったファイルを見分ける 300 字。短い・和文だけなら None。"""
    t = norm(r.get("text") or "")
    return t[40:340] if len(t) >= 340 else None


def relocate_plan(missing, new_files, text_of):
    """パスにファイルが無くなった記録(missing)を、索引に無いファイル(new_files)に対応付ける。text_of(path) は
    そのファイルの 1〜3 頁目の本文。返り値 (対応 [(記録, 新パス, どう)], 決まらない [(記録, 候補)], 見つからない [記録])。
    どう: 名前と本文 / 本文(名前も変わった) / 名前だけ(本文が無い・スキャン)。1 つのファイルは 1 つの記録にしか当てない。"""
    by_name = collections.defaultdict(list)
    for p in new_files:
        by_name[os.path.basename(p)].append(p)
    got, amb, lost = [], [], []
    for r in missing:
        fp, cands = _fingerprint(r), by_name.get(r["filename"], [])
        if fp:
            ok = [p for p in cands if fp in norm(text_of(p))]
            how = "名前と本文"
            if not ok and not cands:
                ok, how = [p for p in new_files if fp in norm(text_of(p))], "本文(名前も変わった)"
        else:
            ok, how = cands, "名前だけ(本文が無い・スキャン)"
        if len(ok) == 1:
            got.append((r, ok[0], how))
        elif ok or cands:
            amb.append((r, ok or cands))
        else:
            lost.append(r)
    used = collections.Counter(p for _, p, _ in got)
    amb += [(r, [p]) for r, p, _ in got if used[p] > 1]
    return [g for g in got if used[g[1]] == 1], amb, lost


def carry_preprint_ids(recs, gone):
    """消えたプレプリント(published_copy のある記録)の arXiv ID を、残した出版版の記録に preprint_arxiv_id として移す。
    移さないと、記録を外したあと has が arXiv ID で引けなくなる(消したプレプリントの ID が「無し」になった)。"""
    by = {r["path"]: r for r in recs}
    for g in gone:
        pub = by.get(g.get("published_copy"))
        if pub is not None and g.get("arxiv_id"):
            pub["preprint_arxiv_id"] = g["arxiv_id"]


def relocate():
    """papers relocate [--root 論文の置き場] [--forget] [--apply]。手で動かした(名前も変えた)ファイルを索引の記録に対応付け直す。
    既定は対応を見せるだけ。--apply で索引を退避してから書く。dedup が移した重複でファイルが消えたもの(利用者が
    消した)は索引から外す。--forget を付けると、どのファイルにも当たらない「見つからない」記録も索引から外す
    (自分で消したファイルの記録。同期が済んでいないだけのファイルも「見つからない」に出るので、一覧を見てから)。そのあと papers index。"""
    import glob
    recs, stamp = load_index()
    root = sys.argv[sys.argv.index("--root") + 1] if "--root" in sys.argv else os.path.commonpath([r["folder"] for r in recs])
    known = {r["path"] for r in recs}
    missing = [r for r in recs if not os.path.exists(r["path"])]
    new_files = sorted(p for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
                       if p.lower().endswith(".pdf") and p not in known)
    memo = {}
    text_of = lambda p: memo.setdefault(p, sh(["pdftotext", "-f", "1", "-l", "3", p, "-"]))
    # ふつうの記録を先に当て、dedup が移した重複には残ったファイルだけを当てる(重複は本体と同じ名前なので、
    # 本体を手で動かすと、消えた重複と動いた本体が 1 つのファイルを取り合う)
    got, amb, lost = relocate_plan([r for r in missing if not moved_aside(r)], new_files, text_of)
    taken = {p for _, p, _ in got}
    g2, a2, l2 = relocate_plan([r for r in missing if moved_aside(r)], [p for p in new_files if p not in taken], text_of)
    got, amb, lost = got + g2, amb + a2, lost + l2
    gone = [r for r in lost if moved_aside(r)]                      # 利用者が消した重複
    lost = [r for r in lost if not moved_aside(r)]
    rel = lambda p: os.path.relpath(p, root)
    print("索引 %d 件のうち、パスにファイルが無い %d 件。置き場の下の索引に無い PDF %d 件" % (len(recs), len(missing), len(new_files)))
    for r, p, how in got:
        print("  移動  %s\n     → %s  (%s)" % (rel(r["path"]), rel(p), how))
    for r, ps in amb:
        print("  決まらない  %s\n     候補: %s" % (rel(r["path"]), " / ".join(rel(p) for p in ps[:4])))
    # 見つからない記録を外す(--forget)。ほかの記録(移した重複・プレプリント)が指している記録は外さない: 外すと、指す先の無い
    # 「移した重複」が残り、ファイルがあるのに検索に出なくなる
    pointed = {r.get(k) for r in recs for k in ("duplicate_of", "published_copy") if id(r) not in {id(g) for g in gone}} - {None}
    forget = [r for r in lost if r["path"] not in pointed] if "--forget" in sys.argv else []
    if forget and (not os.path.isdir(root) or len(forget) == sum(1 for r in recs if not moved_aside(r))):
        sys.exit("stopped: the library folder is missing or no record's file was found (%s). "
                 "It may not be mounted or synced; --forget would empty the index" % root)
    for r in lost:
        print("  %s  %s" % ("見つからない(索引から外す)" if any(r is x for x in forget) else "見つからない", rel(r["path"])))
    print("対応 %d ・ 決まらない %d ・ 見つからない %d%s ・ 消えた重複(索引から外す) %d"
          % (len(got), len(amb), len(lost), "(うち %d 件を索引から外す)" % len(forget) if "--forget" in sys.argv else "", len(gone)))
    if "--forget" in sys.argv and len(forget) < len(lost):
        print("note: %d missing record(s) are kept because a moved duplicate or preprint still points to them"
              % (len(lost) - len(forget)), file=sys.stderr)
    if lost and "--forget" not in sys.argv:
        print("note: if you deleted these files yourself, drop their records with: papers relocate --forget", file=sys.stderr)
    left = len(new_files) - len(got)
    if left:
        print("どの記録にも当たらない PDF %d 件は新しいファイル: papers scan で足す" % left)
    if "--apply" not in sys.argv:
        print("DRY RUN. 書くには --apply"); return
    if not got and not gone and not forget:
        return
    _backup("relocate")
    moved = {}
    for r, p, _ in got:
        moved[r["path"]] = p
        r.update(path=p, folder=os.path.dirname(p), filename=os.path.basename(p))
    repoint(recs, moved)
    carry_preprint_ids(recs, gone)
    drop = {id(r) for r in gone + forget}
    save_index([r for r in recs if id(r) not in drop], stamp)
    print("書いた。検索に反映: papers index")


# 別の DB(PAPERS_HOME)は、その DB の config.json に views が無ければ、いつもの出力先に DB のフォルダ名を足した所にする:
# 同じ出力先だと、片方の views がもう片方のビューを消して作り直す
VIEWS = os.path.expanduser(CONFIG.get("views") or "~/PaperViews") if ROOT == CODE or _config(ROOT).get("views") else \
    os.path.expanduser(_config(CODE).get("views") or "~/PaperViews") + "-" + os.path.basename(ROOT)
VIEW_MARK = ".papers-views"                       # papers views が作ったフォルダの印(これが無いフォルダは消さない)
VIEW_HELP = "使い方.txt"                          # papers views が毎回書く。作り直しの確認の対象外
VIEW_HELP_TEXT = """papers views — 使い方

これは何
  論文 PDF を、誌名・年・分野・第一著者ごとに並べたフォルダ。
  中身はすべて元の PDF への「リンク」(アイコンに小さな矢印)で、PDF 本体は論文の置き場の元の場所にある。
    誌名/Optics Letters/   年/2019/   分野/(OpenAlex のトピック)/   第一著者/Smith/

使い方
  - ダブルクリック: プレビューで開く
  - スペース: クイックルック
  - 右クリック →「元のファイルを表示」: 元の場所を開く

してよいこと(元の PDF にも索引にも影響しない)
  - リンクを消す・動かす・名前を変える
  ただし、次に作り直すと元に戻る(自分で並べた分は消える)

しないこと
  - このフォルダにメモや PDF を保存しない(作り直しが止まる。消さずに止まるので失われはしない)
  - リンクを論文の置き場のフォルダへドラッグしない(複製ができると、同じ論文が二重に入る)

作り直す(ファイルの改名・移動・人手の入力のあと。古いリンクは切れる)
  ターミナルで:  papers views

入っていないもの
  - 同定できていない論文(誌名・年が分からないもの)。探すときは:  papers search "語"
  - 分野は OpenAlex にトピックのあるものだけ

テーマごとに自分で並べたいときは、このフォルダの外に自分のフォルダを作る。
詳しくは MANUAL.md の「Finder で辿る」(場所は papers info の「リポジトリ」)。
"""


def view_links(recs):
    """ビューに置くリンク [(ビューの中の相対パス, 元のファイル)]。解決済みで、移した重複でないものだけ。
    切り口: 誌名・年・分野(OpenAlex のトピックの先頭)・第一著者。同じフォルダで名前がぶつかれば _2 を付ける。"""
    out, seen = [], set()
    for r in sorted(recs, key=lambda r: r["path"]):
        if not renamable(r) or moved_aside(r):
            continue
        subj = r.get("subject")
        subj = subj[0] if isinstance(subj, list) and subj else subj if isinstance(subj, str) else None
        for facet, v in (("誌名", r.get("journal")), ("年", r.get("year")), ("分野", subj), ("第一著者", r.get("first_author"))):
            if not v:
                continue
            d, base = os.path.join(facet, safe(v, 120) or "-"), r["filename"]
            link, i = os.path.join(d, base), 2
            while link in seen:
                stem, ext = os.path.splitext(base)
                link, i = os.path.join(d, "%s_%d%s" % (stem, i, ext)), i + 1
            seen.add(link)
            out.append((link, r["path"]))
    return out


def views():
    """papers views [--out フォルダ]。Finder 用に、誌名・年・分野・第一著者ごとのフォルダへ元の PDF へのリンクを並べる
    (既定 ~/PaperViews、config.json の views で変えられる。同期フォルダの外に置く)。ファイル本体は動かさない。作り直すたびに古いビューは消す(リンクだけ)。"""
    out = os.path.expanduser(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else VIEWS
    if os.path.exists(out):
        if not os.path.exists(os.path.join(out, VIEW_MARK)):
            sys.exit("%s は papers views が作ったフォルダではない(%s が無い)。消さない。--out で別の場所を" % (out, VIEW_MARK))
        own = [os.path.join(d, f) for d, _, fs in os.walk(out) for f in fs
               if not (d == out and f in (VIEW_MARK, VIEW_HELP))
               and f != ".DS_Store" and not f.startswith("._")]       # Finder が開くたびに作る表示設定
        kept = [f for f in own if not os.path.islink(f)]
        if kept:
            sys.exit("%s にリンクでないファイルが %d 件ある(例: %s)。消さないので、動かしてから作り直す"
                     % (out, len(kept), kept[0]))
        shutil.rmtree(out)                        # リンクだけ。リンク先には触れない
    links = view_links(load_index()[0])
    for rel, target in links:
        dst = os.path.join(out, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(target, dst)
    open(os.path.join(out, VIEW_MARK), "w").write("papers views が作ったリンクのフォルダ。作り直すと中身は消える\n")
    open(os.path.join(out, VIEW_HELP), "w").write(VIEW_HELP_TEXT)
    n = collections.Counter(rel.split(os.sep)[0] for rel, _ in links)
    print("%s に %d 本のリンク(%s)。Finder で開く: open %s"
          % (out, len(links), "・".join("%s %d" % kv for kv in n.items()), out))


def info_lines():
    """索引・検索の索引・論文の置き場の場所と更新日時、件数を (辞書, 人が読む行) で。読むだけ。
    info と GUI の「DB の情報」が同じものを出す。"""
    import glob
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None
    recs = load_index()[0]
    im = os.path.getmtime(INDEX)
    dm = os.path.getmtime(DB) if os.path.exists(DB) else None
    dn = df = None
    if dm:
        c = sqlite3.connect(DB)
        dn = c.execute("SELECT count(*) FROM p").fetchone()[0]
        try:
            df = int(c.execute("SELECT v FROM meta WHERE k='fulltext'").fetchone()[0])
        except (sqlite3.Error, TypeError, ValueError):       # 全頁の本文を入れる前の作り方の索引
            pass
        c.close()
    newest = lambda pat: max(glob.glob(os.path.join(ROOT, pat)), key=os.path.getmtime, default=None)
    ren, bk = newest("renames-*.jsonl"), newest("backups/papers-*.jsonl")
    d = {"repo": CODE, "home": ROOT, "library": os.path.commonpath([r["folder"] for r in recs]) if recs else None,
         "index": INDEX, "index_updated": fmt(im), "index_bytes": os.path.getsize(INDEX), "records": len(recs),
         "resolved": sum(1 for r in recs if renamable(r) and not moved_aside(r)),
         "needs_human": sum(1 for r in recs if needs_human(r)), "moved_aside": sum(1 for r in recs if moved_aside(r)),
         "db": DB if dm else None, "db_updated": fmt(dm), "db_bytes": os.path.getsize(DB) if dm else None, "db_records": dn, "db_fulltext": df,
         "db_stale": bool(dm is None or dm < im or dn != len(recs) or not _db_format_ok()),
         "missing_files": sum(1 for r in recs if not os.path.exists(r["path"])),
         "last_rename": fmt(os.path.getmtime(ren)) if ren else None, "last_backup": bk}
    out = ["リポジトリ    %s" % d["repo"]] + (["DB folder     %s" % d["home"]] if d["home"] != d["repo"] else []) + ["論文の置き場  %s" % d["library"]]
    mb = lambda b: "%.1f MB" % (b / 1e6) if b else "-"
    out.append("索引          %s\n              更新 %s ・ %s ・ %d 件（解決済み %d ・ 人手行き %d ・ 移した重複と外した記録 %d）"
               % (d["index"], d["index_updated"], mb(d["index_bytes"]), d["records"], d["resolved"], d["needs_human"], d["moved_aside"]))
    out.append("検索の索引    %s\n              更新 %s ・ %s ・ %s 件(全頁の本文あり %s 件)"
               % (d["db"] or "(無い)", d["db_updated"] or "-", mb(d["db_bytes"]), d["db_records"] or 0, d["db_fulltext"] or 0))
    if d["db_records"] and (d["db_fulltext"] or 0) < d["db_records"] - d["missing_files"]:
        out.append("              note: some records have no full text yet (they are searchable by their first 3,000 characters). Read it with: papers fulltext")
    if d["db_stale"]:
        out.append("              ※ 索引より古い(または件数・作り方が違う)。検索に反映するには: papers index")
    if d["missing_files"]:
        out.append("              ※ パスにファイルが無い記録 %d 件(手で動かした・消した)。papers relocate" % d["missing_files"])
    out += ["最後の改名    %s" % (d["last_rename"] or "-"), "最新の退避    %s" % (d["last_backup"] or "-")]
    return d, out


def info():
    """索引・検索の索引・論文の置き場の場所と更新日時、件数。読むだけ。--json で機械向け。"""
    d, lines = info_lines()
    print(json.dumps(d, ensure_ascii=False) if "--json" in sys.argv else "\n".join(lines))


FIX_KEYS = REVIEW_COLS + ["article_number", "published_doi", "authors"]


def _one_record(recs, target):
    """フルパスか、ファイル名の一部で 1 件に絞る。絞れなければ止まる(fix・move が使う)。"""
    hit = [r for r in recs if r["path"] == target] or [r for r in recs if target in r["filename"]]
    if len(hit) != 1:
        sys.exit("「%s」に当たる記録が %d 件。1 件に絞れるパスか名前にする:\n%s"
                 % (target, len(hit), "\n".join("  " + r["path"] for r in hit[:10])))
    return hit[0]


def move():
    """papers move <パスかファイル名の一部>… --to <フォルダ> [--apply [--mkdir]]。ファイルを動かし、同時に索引も直す。
    フォルダは論文の置き場の下(相対パスなら置き場からの相対)。既定は予定を出すだけ(フォルダが無くても出す)。
    --apply で索引を退避してから動かす。無いフォルダは --mkdir のときだけ作る。papers undo で戻せる。"""
    args = sys.argv[2:]
    if "--to" not in args:
        sys.exit(move.__doc__)
    to = args[args.index("--to") + 1]
    names = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--to")]
    recs, stamp = load_index()
    root = os.path.commonpath([r["folder"] for r in recs])
    dest = os.path.normpath(to if os.path.isabs(to) else os.path.join(root, to))
    if os.path.commonpath([dest, root]) != root:
        sys.exit("%s は論文の置き場(%s)の外。置き場の下のフォルダにする" % (dest, root))
    apply, mk = "--apply" in args, "--mkdir" in args
    if not os.path.isdir(dest) and apply and not mk:
        sys.exit("フォルダが無い: %s(作るなら --mkdir)" % dest)
    plan = []
    for n in names:
        r = _one_record(recs, n)
        new = os.path.join(dest, r["filename"])
        if os.path.exists(new) or _same_name(new) in {_same_name(q) for _, q in plan}:
            sys.exit("移動先に同じ名前のファイルがある: %s" % new)
        plan.append((r, new))
    for r, new in plan:
        print("  %s\n→ %s" % (os.path.relpath(r["path"], root), os.path.relpath(new, root)))
    if not apply:
        print("DRY RUN. 動かすには --apply%s" % ("" if os.path.isdir(dest) else "(フォルダが無いので --mkdir も)")); return
    _backup("move")                            # フォルダを作るより先(退避に失敗したら、何も作らずに止まる)
    if not os.path.isdir(dest):
        os.makedirs(dest)
    trail = _trail("renames")
    _atomic_write(trail, [json.dumps({"from": r["path"], "to": new}, ensure_ascii=False) + "\n" for r, new in plan])
    moved = {}
    try:
        for r, new in plan:
            _move_file(r["path"], new)
            moved[r["path"]] = new
            r.update(path=new, folder=dest, filename=os.path.basename(new))
    finally:                                       # 途中で止まっても、動かした分は索引に書く
        if moved:
            repoint(recs, moved)
            save_index(recs, stamp)
        _stopped("move", len(moved))
    print("%d 件を動かした。戻すには papers undo。検索とビューに反映: papers index && papers views" % len(moved))


# ---------- 論文以外(本・学位論文・講義ノート・資料・特許)を避ける ----------
# 同定の相手(Crossref・arXiv)が無いファイルは、いつまでも人手行きに残る。未同定で頁数の多いものを候補として人に見せ、
# 人が種類を付けたものだけを置き場の下の _論文以外/<種類>/ へ移す。機械は候補を出すだけで、論文かどうかは決めない。
# 候補の線(50 頁)の根拠は DESIGN.md「論文以外を避ける」。50 頁以下は候補に出さない。
NONPAPER_DIR = "_論文以外"
NONPAPER_PAGES = 50
# 種類 → (確認用ページのキー, 移す先のフォルダ)。paper は「論文として残す」: 移さず、候補に出さなくなるだけ
KINDS = {"book": ("b", "本・ハンドブック"), "thesis": ("t", "学位論文"), "lecture": ("l", "講義ノート・スライド"),
         "document": ("d", "報告書・マニュアル・資料"), "patent": ("a", "特許"), "bookpart": ("c", "書籍(部分)"), "paper": ("p", None)}


KIND_GROUPS = ("paper", "book", "other")


def kind_group(r):
    """検索の種類の絞り込み(--kind)での分け方: 論文以外の印が無ければ paper、本・ハンドブックと本の一部は book、ほかの論文以外は other。
    書籍の DB では、印の無い記録は book(人が「論文」と付けたものだけ paper)。"""
    k = r.get("kind")
    if not r.get("not_paper") and not (BOOKS and k != "paper"):
        return "paper"
    return "book" if k in ("book", "bookpart") or not r.get("not_paper") else "other"


def _pages_in(info):
    """pdfinfo の出力から頁数。無ければ None。"""
    m = re.search(r'^Pages:\s+(\d+)', info or "", re.M)
    return int(m.group(1)) if m else None


def pdf_pages(path):
    """PDF の頁数。読めなければ None。"""
    return _pages_in(sh(["pdfinfo", path]))


def nonpaper_candidates(recs):
    """論文以外の候補: 人手行きで、まだ人が種類を決めておらず、頁数が線を超える記録。頁数を調べていない記録は入らない。
    外部から取れなかっただけの記録(次の add が取り直す)も入れない。
    書籍の DB では、種類の決まっていない記録の全部(同定済みも。章だけの PDF も本の ISBN で同定される)。"""
    if BOOKS:
        return [r for r in recs if not r.get("kind") and not moved_aside(r)]
    return [r for r in recs if needs_human(r) and not retryable(r) and not r.get("kind")
            and (r.get("n_pages") or 0) > NONPAPER_PAGES]


def nonpaper_marks(recs, marks, resolved_too=False):
    """確認用ページの判定 {パス: キー} を記録に書く。(論文以外にした数, 論文として残す数, 同定済みで飛ばした数, 索引に無いパス, 知らないキー)。
    論文以外は not_paper と kind を、論文として残すものは kind=paper だけを書く(人手行きに残り、候補には出なくなる)。
    同定済みの記録には書かない(ページは候補を作った後に同定が進んだ記録を含みうる)。resolved_too は、画面から 1 件ずつ付けるとき:
    現物を見て決めた種類なので、同定済みにも書く。"""
    key = {v[0]: k for k, v in KINDS.items()}
    nfc = lambda s: unicodedata.normalize("NFC", s)
    by = {nfc(r["path"]): r for r in recs}
    n = keep = skipped = 0
    unknown, bad = [], []
    for p, k in marks.items():
        r = by.get(nfc(p))
        if r is None:
            unknown.append(p)
        elif k not in key:
            bad.append(k)
        elif renamable(r) and not resolved_too:
            skipped += 1
        elif key[k] == "paper":
            r["kind"] = "paper"
            r.pop("not_paper", None)
            keep += 1
        else:
            r["kind"], r["not_paper"] = key[k], "user"
            n += 1
    return n, keep, skipped, unknown, bad


def nonpaper_read(text):
    """nonpaper-ok に渡すファイルの中身を ({パス: キー}, 読めない行) にする。見出しの行(path …)と空行は読み飛ばす。
    タブで 2 欄に割れない行・同じパスの 2 行目は読めない行にする(黙って飛ばすと、付けたつもりの印が付かない)。"""
    marks, broken = {}, []
    for l in text.splitlines():
        f = [x.strip() for x in l.split("\t")]
        if not l.strip() or f[0] == "path":
            continue
        if len(f) < 2 or not f[0] or not f[1] or f[0] in marks:
            broken.append(l)
        else:
            marks[f[0]] = f[1]
    return marks, broken


def nonpaper_plan(recs, root, exists=os.path.exists):
    """種類の付いた論文以外の記録のうち、種類のフォルダにまだ無いものの移動の予定 [(記録, 移動先)] と、ファイルが無くて
    飛ばした記録。同じ名前があれば _2, _3 を付ける(中身が同じでも消さない)。"""
    plan, gone, taken = [], [], set()
    for r in recs:
        folder = KINDS.get(r.get("kind"), (None, None))[1]
        if not r.get("not_paper") or not folder or moved_aside(r):
            continue
        dest = os.path.join(root, NONPAPER_DIR, folder)
        if r["folder"] == dest:
            continue
        if not exists(r["path"]):
            gone.append(r)
            continue
        stem, ext = os.path.splitext(r["filename"])
        new, i = os.path.join(dest, r["filename"]), 2
        while _same_name(new) in taken or exists(new):          # 大小文字・合成/分解だけが違う名前も同じ名前
            new, i = os.path.join(dest, "%s_%d%s" % (stem, i, ext)), i + 1
        taken.add(_same_name(new))
        plan.append((r, new))
    return plan, gone


def nonpaper_strays(plan):
    """予定のうち、すでにどこかの _論文以外/<同じ種類>/ にあるファイルを別の場所の同じフォルダへ動かすもの。
    置き場の根(全記録の共通の親)が変わったときに起きる(置き場の外のフォルダを add した等)。移し済みの全件が動くので止める。"""
    return [(r, new) for r, new in plan if os.path.basename(os.path.dirname(r["folder"])) == NONPAPER_DIR
            and os.path.basename(r["folder"]) == os.path.basename(os.path.dirname(new))]


def nonpaper_page():
    """papers nonpaper-page。論文以外の候補(未同定で 50 頁超)に種類を付けるページ nonpaper.html を作る(手元で開く)。
    頁数をまだ調べていない人手行きの記録は、ここで調べて索引に書く(退避してから)。"""
    recs, stamp = load_index()
    todo = [r for r in recs if needs_human(r) and not r.get("kind") and r.get("n_pages") is None and os.path.exists(r["path"])]
    got = 0
    for i, r in enumerate(todo):
        n = pdf_pages(r["path"])
        if n is not None:                      # 読めなかった記録には書かない(次も調べ直す)
            r["n_pages"], got = n, got + 1
        if i % 100 == 0:
            print("  頁数 %d/%d" % (i, len(todo)), file=sys.stderr)
    if got:
        _backup("nonpaper-page")
        save_index(recs, stamp)
    root = os.path.commonpath([r["folder"] for r in recs])
    rows = [{"path": r["path"], "file": r["filename"], "folder": os.path.relpath(r["folder"], root), "pages": r.get("n_pages"),
             "rung": r.get("rung") or "", "ocr": bool(r.get("ocr")), "guess": " ".join((r.get("title_guess") or r.get("title") or "").split())[:120],
             "head": " ".join((r.get("text") or "").split())[:200], "stamp": "1"}
            for r in sorted(nonpaper_candidates(recs), key=lambda r: (r.get("n_pages") or 0) * (1 if BOOKS else -1))]   # 書籍の DB は短いものから(部分の候補)
    kinds = json.dumps([[v[0], v[1] or "論文として残す"] for v in KINDS.values()], ensure_ascii=False)
    # 型の印は、記録を埋める前に置き換える(表題の推定や本文の冒頭に同じ文字列があっても壊れない)
    tpl = open(os.path.join(CODE, "nonpaper_page.html")).read().replace("__KINDS__", kinds).replace("__PAGES__", str(NONPAPER_PAGES))
    page = os.path.join(ROOT, "nonpaper.html")
    _atomic_write(page, [review_page_html(rows, tpl)])
    print("%d 件の候補を %s に。開く: open %s" % (len(rows), page, page))


def nonpaper_ok():
    """papers nonpaper-ok <nonpaper-ok.tsv>。ページの「書き出す」の結果(パスと種類のキー)を索引に書く(退避してから)。
    索引に無いパス・知らないキーが 1 つでもあれば書かない。ファイルは動かさない(動かすのは papers nonpaper --apply)。"""
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit(nonpaper_ok.__doc__)
    src = os.path.expanduser(args[0])
    if not os.path.exists(src):
        sys.exit("ファイルが無い: %s(ページの「書き出す」で保存したファイルを渡す。候補が 0 件なら書き出すものは無い)" % src)
    marks, broken = nonpaper_read(open(src, encoding="utf-8-sig").read())
    if broken:
        sys.exit("書かなかった。読めない行が %d 行(1 行は パス・タブ・キー。同じパスは 1 行だけ)。例: %r" % (len(broken), broken[0][:80]))
    if not marks:
        sys.exit("書くものが無い(%s に判定の行が無い)" % src)
    recs, stamp = load_index()
    n, keep, skipped, unknown, bad = nonpaper_marks(recs, marks, resolved_too=BOOKS)      # 書籍の DB では、同定済みの記録にも種類を付ける
    if unknown or bad:
        sys.exit("書かなかった。索引に無いパス %d 件%s、知らない種類 %s(使えるのは %s)"
                 % (len(unknown), "(例: %s)" % unknown[0] if unknown else "", sorted(set(bad)), " ".join(v[0] for v in KINDS.values())))
    _backup("nonpaper-ok")
    save_index(recs, stamp)
    print("論文以外 %d 件・論文として残す %d 件を書いた%s。移動の予定: papers nonpaper"
          % (n, keep, "(同定済みの %d 件には書かなかった)" % skipped if skipped else ""))


def nonpaper():
    """papers nonpaper [--apply]。種類の付いた論文以外を、置き場の下の _論文以外/<種類>/ へ移す。既定は予定を出すだけ。
    --apply で索引を退避してから動かし、索引も直す。papers undo で戻せる。消さない(同じ名前には _2 を付ける)。"""
    if BOOKS:
        sys.exit("papers nonpaper is not available in a book DB: kinds are labels there and files are not moved "
                 "(label them with papers nonpaper-page and papers nonpaper-ok).")
    apply = "--apply" in sys.argv
    recs, stamp = load_index()
    root = os.path.commonpath([r["folder"] for r in recs])
    unseen = sum(1 for r in recs if needs_human(r) and not r.get("kind") and r.get("n_pages") is None)
    print("論文以外の候補(未同定で %d 頁超・種類が未定) %d 件%s。種類を付けるページ: papers nonpaper-page"
          % (NONPAPER_PAGES, len(nonpaper_candidates(recs)),
             "、頁数の分からない人手行き %d 件(まだ調べていない・読めない PDF・ファイルが無い)" % unseen if unseen else ""))
    plan, gone = nonpaper_plan(recs, root)
    if nonpaper_strays(plan):
        r, new = nonpaper_strays(plan)[0]
        sys.exit("止めた: すでに %s の下にあるファイルを、別の場所の同じフォルダへ動かす予定になった(%d 件。例: %s → %s)。\n"
                 "論文の置き場(%s)の外の記録が索引に入っていないかを確かめる" % (NONPAPER_DIR, len(nonpaper_strays(plan)), r["path"], new, root))
    for r, new in plan[:40]:
        print("  %s\n→ %s" % (os.path.relpath(r["path"], root), os.path.relpath(new, root)))
    for r in gone[:10]:
        print("  ファイルが無いので飛ばす: %s" % os.path.relpath(r["path"], root))
    if not plan:
        print("移すものは無い(種類の付いた論文以外は %d 件)" % sum(1 for r in recs if r.get("not_paper") and r.get("kind")))
        return
    if not apply:
        print("%d 件を %s の下へ。DRY RUN. 動かすには --apply" % (len(plan), os.path.join(root, NONPAPER_DIR)))
        return
    _backup("nonpaper")
    trail = _trail("renames")
    _atomic_write(trail, [json.dumps({"from": r["path"], "to": new}, ensure_ascii=False) + "\n" for r, new in plan])   # 動かす前に
    moved = {}
    try:
        for r, new in plan:
            os.makedirs(os.path.dirname(new), exist_ok=True)
            _move_file(r["path"], new)         # 予定のあとに現れた・予定が見分けられなかった同名は、上書きせずに止まる
            moved[r["path"]] = new
            r.update(path=new, folder=os.path.dirname(new), filename=os.path.basename(new))
    finally:                                   # 途中で止まっても、動かした分は索引に書く(書かないと、索引が無いパスを指す)
        if moved:
            repoint(recs, moved)
            save_index(recs, stamp)
        if len(moved) < len(plan):
            print("途中で止まった: %d 件中 %d 件を動かし、索引に書いた。残りは動かしていない。戻すには papers undo"
                  % (len(plan), len(moved)), file=sys.stderr)
    print("%d 件を動かした。戻すには papers undo。検索に反映: papers index" % len(moved))


# ---------- 書籍の DB: 同定できなかった本を人が決める(review・merge)、名前を付ける(rename) ----------
# 表題の検索は「作品」までしか絞れず、別の版・同名の別の本を通すので、候補として見せるだけにする。決めるのは人で、ISBN か DOI を書く
# (DESIGN.md「書籍の DB」)。
BOOK_REVIEW_COLS = ["isbn", "doi", "file", "pages", "why", "candidates", "path"]
RENAME_TSV = os.path.join(ROOT, "rename.tsv")
RENAME_COLS = ["author", "title", "edition", "year", "new_name", "current_name", "catalog_year", "page_year", "page_edition", "path"]
RENAME_EDIT = RENAME_COLS[:4]                   # 利用者が直す列。ほかの列は読まない


def _book_text_norm(r):
    """記録の PDF の先頭 BOOK_HEAD 頁の本文(_bnorm 済み)。全頁の本文を読んであればそれ(OCR 済みのスキャンも入る)、無ければ文字の層。"""
    e = _fulltext_load(_file_key(r["path"]) or "")
    pages = e["pages"][:BOOK_HEAD] if e else [t for _, t in _book_pages(r["path"], None)]
    return _bnorm(" ".join(pages))


def _book_query(filename):
    """表題の検索に投げる語: ファイル名から記号と版の語を除いたもの。"""
    s = re.sub(r'[_=,()\[\]&.\-]+', ' ', os.path.splitext(filename)[0])
    return " ".join(re.sub(r'\b(\d(st|nd|rd|th)|ed|edition|vol|pdf|book|web)\b', ' ', s, flags=re.I).split())


def book_candidates(r, text_norm):
    """同定できなかった本の候補(表題の検索)。表題と著者の姓の両方が PDF に字面であるものだけ。採らない: 人が見て ISBN か DOI を書く。
    検索の語は URL の末尾に置く(キャッシュの名前は URL の末尾から作るので、前に置くと長い名前どうしが同じ名前になる)。
    取れなかった問い合わせは、候補なしとして扱う(次の review で取り直す。応答はキャッシュしない)。"""
    q, out = urllib.parse.quote(_book_query(r["filename"])), []
    try:
        its = get_json("https://api.crossref.org/works?rows=5&mailto=%s&filter=%s&query.bibliographic=%s"
                       % (MAILTO, ",".join("type:" + t for t in BOOK_TYPES), q))["message"]["items"]
        out += [_crossref_book(m) for m in its]
    except Exception:
        pass
    try:
        docs = get_json("https://openlibrary.org/search.json?limit=5&fields=title,author_name,first_publish_year&q=%s" % q).get("docs") or []
        out += [{"doi": None, "title": clean_text(d.get("title") or ""), "families": [_family_of(a) for a in d.get("author_name") or []],
                 "year": d.get("first_publish_year"), "source": "openlibrary"} for d in docs]
    except Exception:
        pass
    seen, keep = set(), []
    for c in out:
        k = (c["source"], _bnorm(c["title"]), c.get("year"))
        if k in seen or not book_title_in(c["title"], text_norm) \
                or not any(len(_bnorm(a)) >= 3 and _bnorm(a) in text_norm for a in c.get("families") or []):
            continue
        seen.add(k)
        keep.append(c)
    return keep


def _cand_cell(c):
    return " / ".join(str(x) for x in (c["title"][:70], ", ".join((c.get("families") or [])[:3]), c.get("year") or "-",
                                       "doi:" + c["doi"] if c.get("doi") else c["source"]))


def _tsv_rows(p, need=()):
    """TSV(1 行目が見出し)を、行ごとの辞書に。無ければ空。表計算で保存し直した表を読む: 引用符で囲んだ欄、BOM、UTF-16、CRLF。
    need の列が見出しに無ければ止まる(カンマ区切りで保存した・列を消した表を、何も書いていない表として読まない)。"""
    import csv, io
    if not os.path.exists(p):
        return []
    raw = open(p, "rb").read()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        sys.exit("%s: cannot read the file (save it as tab-separated text in UTF-8)" % p)
    rows = list(csv.reader(io.StringIO(text, newline=""), dialect="excel-tab"))
    head = [h.strip() for h in rows[0]] if rows else []
    if [c for c in need if c not in head]:
        sys.exit("%s: column(s) %s not found in the first line (%s). Save the table tab-separated with its header line, or delete the file "
                 "to start over." % (p, ", ".join(c for c in need if c not in head), " | ".join(head)[:80]))
    return [dict(zip(head, (c.strip() for c in r))) for r in rows[1:] if any(c.strip() for c in r)]


def _tsv_text(rows):
    """行の並び(1 行目が見出し)を TSV の文字列に。欄の中の改行・タブは空白にし、引用符の要る欄は囲む(_tsv_rows が読める形)。"""
    import csv, io
    out = io.StringIO()
    csv.writer(out, dialect="excel-tab", lineterminator="\n").writerows(
        [" ".join(str(v if v is not None else "").split()) for v in row] for row in rows)
    return out.getvalue()


def book_review():
    """papers review(書籍の DB)。同定できなかった記録を unresolved.tsv に書く。候補(表題の検索)は見るだけの欄。
    正しい本の ISBN か DOI を isbn・doi の列に書いて papers merge。前に書いた isbn・doi の列は残す。"""
    recs = load_index()[0]
    un = [r for r in recs if needs_human(r) and not retryable(r) and not moved_aside(r)]
    mine = {d.get("path"): d for d in _tsv_rows(REVIEW_TSV, need=("isbn", "doi", "path"))}
    lines, ncand = [BOOK_REVIEW_COLS], 0
    for i, r in enumerate(sorted(un, key=lambda r: r["filename"].lower())):
        cands = book_candidates(r, _book_text_norm(r)) if os.path.exists(r["path"]) else []
        ncand += bool(cands)
        old = mine.get(r["path"], {})
        seen = ["ISBN printed in the PDF: " + ", ".join(r["isbn_seen"])] if r.get("isbn_seen") else []      # 表題の照合で採らなかった番号
        lines.append([old.get("isbn"), old.get("doi"), r["filename"], r.get("n_pages"), why(r),
                      " | ".join(seen + [_cand_cell(c) for c in cands[:4]]), r["path"]])
        if i % 10 == 0:
            print("  candidates %d/%d" % (i, len(un)), file=sys.stderr)
    _atomic_write(REVIEW_TSV, [_tsv_text(lines)])
    print("%d record(s) not identified (%d with candidates) -> %s" % (len(un), ncand, REVIEW_TSV))
    for r in sorted(un, key=lambda r: r["filename"].lower()):
        print("  %5s p  %s" % (r.get("n_pages") or "?", r["filename"]))
    print("Candidates are hints only (a title search cannot tell editions apart). Write the ISBN or the DOI of the right book in the\n"
          "isbn / doi column, then: papers merge. Chapters and notes that are not a whole book: mark their kind (papers nonpaper-page).")


def _book_by_hand(isbn, doi):
    """人が書いた ISBN か DOI で目録を引いた記録。(記録, None) か (None, 理由)。人が決めたので、表題が PDF にあるかは問わない。"""
    if isbn:
        i = isbn13(isbn)
        if not i:
            return None, "not a valid ISBN (check digit): %s" % isbn
        cands = books_by_isbn(i)
        if not cands:
            return None, "ISBN %s is not in the catalogues" % i
        best = (next((c for c in cands if c["source"] == "ndl"), None) or next((c for c in cands if c["source"] == "crossref" and c.get("families")), None)
                or next((c for c in cands if c.get("families")), None) or cands[0])
        same = lambda c: c.get("isbn") == best.get("isbn") or best.get("isbn") in (c.get("isbn_all") or [])
        return dict({k: v for k, v in best.items() if k != "isbn_all"},
                    doi=best.get("doi") or next((c["doi"] for c in cands if c.get("doi") and same(c)), None)), None
    try:
        m = get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi) + "?mailto=" + MAILTO)["message"]
    except Exception as e:
        _lookup_failed(e, "crossref:" + doi)
        return None, "DOI %s was not found at Crossref" % doi
    if m.get("type") not in BOOK_TYPES:
        return None, "DOI %s is a %s, not a whole book" % (doi, m.get("type"))
    d = _crossref_book(m)
    return dict({k: v for k, v in d.items() if k != "isbn_all"}, isbn=(d["isbn_all"] or [None])[0]), None


def book_merge():
    """papers merge(書籍の DB)。unresolved.tsv の isbn・doi の列に書いた番号で目録を引き、記録に書く(退避してから)。
    引けなかった行は書かずに理由を出す。"""
    recs, stamp = load_index()
    by = {r["path"]: r for r in recs}
    done, bad = [], []
    for d in _tsv_rows(REVIEW_TSV, need=("isbn", "doi", "path")):
        isbn, doi = (d.get("isbn") or "").strip(), (d.get("doi") or "").strip()
        if not (isbn or doi):
            continue
        r = by.get(d.get("path"))
        if r is None:
            bad.append((d.get("file"), "path is not in the index"))
            continue
        if r.get("rung") == "manual" and ((isbn and isbn13(isbn) == r.get("isbn")) or (doi and doi.lower() == (r.get("doi") or "").lower())):
            continue                                   # もう書いた行(表は merge の後も残る)
        del _FAILED[:]
        new, why_ = _book_by_hand(isbn, doi)
        if _FAILED:
            why_, new = "could not reach the catalogue (%s); run papers merge again" % ", ".join(sorted(set(_FAILED))), None
        if not new:
            bad.append((r["filename"], why_))
            continue
        for k in ("why_not", "isbn_seen"):
            r.pop(k, None)
        r.update(new, first_author=(new.get("families") or [""])[0], rung="manual", fixed_by="user", fixed_at=time.strftime("%Y-%m-%d"))
        done.append(r)
    if done:
        _backup("merge")
        save_index(recs, stamp)
    for r in done:
        print("  %s\n   -> %s %s | %s%s" % (r["filename"], r.get("first_author") or "-", r.get("year") or "-", r["title"][:80],
                                          "" if renamable(r) else "   (author or year is missing: still listed by papers review)"))
    for name, why_ in bad:
        print("  NOT WRITTEN  %s: %s" % (name, why_), file=sys.stderr)
    print("%d record(s) written, %d not written. Update the search: papers index" % (len(done), len(bad)))


def _ordinal(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def name_hints(filename):
    """いまのファイル名に利用者が書いた (版, 年)。無ければ None。改名の表の初期値にだけ使う(人が表で直す)。"""
    s = os.path.splitext(filename)[0]
    e = re.search(r'(?<![\d.])(\d{1,2})\s?(?:st|nd|rd|th)\b|(?<![\d.])(\d{1,2})\s?ed\b', s, re.I)
    y = re.findall(r'(?<!\d)((?:19|20)\d\d)(?!\d)', s)
    return (int(e.group(1) or e.group(2)) if e else None), (int(y[-1]) if y else None)


def book_name_parts(r):
    """名前の (著者の部分, 表題の部分)。著者は姓(2 人は &、3 人以上は et al、編者は (ed.))、表題は主題だけ。
    利用者が改名の表で直した値(name_author・name_title)があれば、それ。"""
    fam = [f for f in r.get("families") or ([r["first_author"]] if r.get("first_author") else []) if f]
    who = fam[0] if len(fam) == 1 else " & ".join(fam) if len(fam) == 2 else fam[0] + " et al" if fam else ""
    if who and r.get("authors_role") == "editor":
        who += " (ed.)"
    return r.get("name_author") or who, r.get("name_title") or _main_title(r.get("title"))[:120]


def book_filename(r, edition=None, year=None, author=None, title=None):
    """「著者 - 表題 (版, 年)」。版は 2 版以上のときだけ。author・title を渡さなければ book_name_parts の値。"""
    who, what = book_name_parts(r)
    who, what = author or who, title or what
    tail = ", ".join(x for x in (_ordinal(edition) + " ed" if edition and edition > 1 else "", str(year) if year else "") if x)
    return (who + " - " if who else "") + what + (" (%s)" % tail if tail else "")


def book_renamable(r):
    """書籍の DB で名前を付け替える記録: 目録で同定できた本の全体。部分・プレプリント・本でない種類は、利用者の付けた名前のまま残す。
    脇へ移した記録(外した記録)も付け替えない。"""
    return renamable(r) and bool(r.get("title")) and not r.get("arxiv_id") and r.get("kind") in (None, "book") and not moved_aside(r)


def book_rename_table(ok, apply):
    """改名の表(rename.tsv)。{パス: (版, 年, 著者の部分, 表題の部分)} を返す。
    予定(apply でない)では、ok の全部の記録の行を作って表を書き直す。版と年の初期値は、前に適用した値 → いまのファイル名に書いてある値 →
    著作権の頁(年は無ければ目録)。著者と表題の初期値は目録から作る(book_name_parts)。表がもうあれば、その author・title・edition・year
    の列(利用者が直した値)を使う。author・title が空なら初期値、edition・year が空なら名前に入れない。
    適用(apply)では、表に行のある記録だけを返す(行を消した記録・表を作った後に増えた記録は、確かめていない初期値で改名しない)。
    見出しに列が足りない・同じパスが 2 行ある・数字でない版・4 桁でない年があれば止まる。"""
    rows = _tsv_rows(RENAME_TSV, need=RENAME_EDIT + ["path"] if apply else ["path"])
    mine = {d.get("path"): d for d in rows}
    if len(mine) != len(rows):
        sys.exit("rename.tsv: the same path appears in more than one row")
    vals, lines = {}, [RENAME_COLS]
    for r in sorted(ok, key=lambda r: r["filename"].lower()):
        hint_e, hint_y = name_hints(r["filename"])
        ed = r.get("name_edition") if r.get("named_at") else hint_e or r.get("page_edition")
        yr = r.get("name_year") if r.get("named_at") else hint_y or r.get("page_year") or r.get("year")
        who, what = book_name_parts(r)
        d = mine.get(r["path"])
        if d is None and apply:
            continue
        if d is not None:
            e, y = d.get("edition", str(ed or "")), d.get("year", str(yr or ""))        # 列が無い(前の形の表)ときは初期値
            if (e and not e.isdigit()) or (y and not re.fullmatch(r'(1[5-9]|20)\d\d', y)):
                sys.exit("rename.tsv: edition must be a number and year a 4-digit year (%s: edition=%r year=%r)" % (r["filename"], e, y))
            ed, yr = (int(e) if e else None), (int(y) if y else None)
            who, what = " ".join((d.get("author") or "").split()) or who, " ".join((d.get("title") or "").split()) or what
        vals[r["path"]] = (ed, yr, who, what)
        lines.append([who, what, ed, yr, safe(book_filename(r, ed, yr, who, what)) + ".pdf", r["filename"], r.get("year"), r.get("page_year"),
                      r.get("page_edition"), r["path"]])
    if not apply:
        _atomic_write(RENAME_TSV, [_tsv_text(lines)])
    return vals


# ---------- DB から外す・別の DB へ送る ----------
# 論文でないもの・要らないものを見つけたときの 2 つの出口。どちらも既定は予定を出すだけで、動かす前に索引を退避し、移動先のファイルを
# 上書きしない(DESIGN.md「DB から外す・別の DB へ送る」)。
REMOVED_DIR = "_索引から外した"


def _dest_name(dest, name, taken, exists):
    """dest の下の、まだ無い名前(同じ名前があれば _2, _3。大小文字・合成/分解だけが違う名前も同じ名前)。決めた名前は taken に足す。"""
    stem, ext = os.path.splitext(name)
    new, i = os.path.join(dest, name), 2
    while _same_name(new) in taken or exists(new):
        new, i = os.path.join(dest, "%s_%d%s" % (stem, i, ext)), i + 1
    taken.add(_same_name(new))
    return new


def _immovable(recs, targets):
    """外せない・送れない記録があれば、その理由(無ければ None)。脇へ移した記録は、もう利用者が消すのを待っている。
    ほかの記録(移した重複・プレプリント)が指している記録を動かすと、指す先の無い記録が残る。"""
    pointed = {r.get(k) for r in recs for k in POINTERS} - {None}
    for r in targets:
        if moved_aside(r):
            return "already moved aside (a duplicate, a preprint kept with its published copy, or a removed record): %s" % r["path"]
        if r["path"] in pointed:
            return "a moved duplicate or preprint points to this record (papers relocate drops those once you delete them): %s" % r["path"]
    return None


def remove_plan(recs, targets, exists=os.path.exists):
    """DB から外す予定 [(記録, 移動先)]。移動先は置き場の下の _索引から外した/。ファイルの無い記録は移動先 None(印だけ付ける)。
    外せない記録があれば ValueError。"""
    bad = _immovable(recs, targets)
    if bad:
        raise ValueError(bad)
    dest, taken = os.path.join(os.path.commonpath([r["folder"] for r in recs]), REMOVED_DIR), set()
    return [(r, _dest_name(dest, r["filename"], taken, exists) if exists(r["path"]) else None) for r in targets]


def remove_apply(recs, stamp, plan):
    """予定どおりに外す: 索引を退避 → 動かす予定を書き残す → ファイルを移す → 印(removed)を付けて索引を書く。途中で止まっても、
    済んだ分は索引に書く。papers undo で戻る(ファイルを戻し、印を消す)。"""
    _backup("remove")
    todo = [json.dumps({"from": r["path"], "to": new, "removed": True}, ensure_ascii=False) + "\n" for r, new in plan if new]
    if todo:                                   # 空の記録は書かない(書くと、直前の移動を undo で戻せなくなる)
        _atomic_write(_trail("renames"), todo)
    moved, n = {}, 0
    try:
        for r, new in plan:
            if new:
                os.makedirs(os.path.dirname(new), exist_ok=True)
                _move_file(r["path"], new)
                moved[r["path"]] = new
                r.update(path=new, folder=os.path.dirname(new), filename=os.path.basename(new))
            r["removed"], n = "user", n + 1
    finally:
        if n:
            repoint(recs, moved)
            save_index(recs, stamp)
        _stopped("remove", n)


def remove():
    """papers remove <パスかファイル名の一部>… [--apply]。DB から外す: 記録に印を付けて検索と一覧から隠し、ファイルを置き場の下の
    _索引から外した/ へ移す。既定は予定を出すだけ。--apply で索引を退避してから動かす。papers undo で戻せる。
    ファイルを消すのは利用者。消した後の記録は papers relocate --apply が索引から外す。"""
    names = [a for a in sys.argv[2:] if not a.startswith("--")]
    if not names:
        sys.exit(remove.__doc__)
    recs, stamp = load_index()
    try:
        plan = remove_plan(recs, list({id(r): r for r in (_one_record(recs, n) for n in names)}.values()))
    except ValueError as e:
        sys.exit(str(e))
    root = os.path.commonpath([r["folder"] for r in recs])
    for r, new in plan:
        print("  %s\n→ %s" % (os.path.relpath(r["path"], root), os.path.relpath(new, root) if new else "(the file is missing: only the record is marked)"))
    if "--apply" not in sys.argv:
        print("DRY RUN. To remove them from the DB: --apply"); return
    remove_apply(recs, stamp, plan)
    print("%d record(s) removed from the DB. Undo: papers undo. Update the search: papers index" % len(plan))


def other_db(home):
    """organize.py をもう 1 つ読み込み、別の DB(home のフォルダ)を見るモジュールを返す。モジュールの値(索引の場所・キャッシュ)は
    読み込みごとに別なので、DB どうしで混ざらない。home が無い・フォルダでなければ SystemExit。
    環境変数 INDEX(この過程の DB の索引の場所を変える)は引き継がない: 引き継ぐと、2 つの DB が同じ索引のファイルを指す。"""
    import importlib.util
    keep = {k: os.environ.pop(k, None) for k in ("PAPERS_HOME", "INDEX")}
    os.environ["PAPERS_HOME"] = home
    try:
        spec = importlib.util.spec_from_file_location("organize_db", os.path.join(CODE, "organize.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        del os.environ["PAPERS_HOME"]
        os.environ.update({k: v for k, v in keep.items() if v is not None})
    return m


def library_of(recs):
    """この DB の論文の置き場: この DB の config.json の library、無ければ全記録のフォルダの共通の親。決まらなければ None
    (記録がまだ無い DB)。ほかの DB から送られてくるファイルの行き先になる。"""
    own = _config(ROOT).get("library")
    if own:
        return os.path.abspath(os.path.expanduser(str(own)))
    folders = [r["folder"] for r in recs if r.get("folder")]
    return os.path.commonpath(folders) if folders else None


def send_plan(recs, targets, B, exists=os.path.exists):
    """別の DB(B: other_db が返すモジュール)へ送る予定 [(記録, 移動先)]。移動先は相手の論文の置き場(種類の付いた論文以外は、
    その下の種類のフォルダ)。同じ名前があれば _2, _3。送れなければ ValueError: 相手がこの DB・相手の置き場が決まらない・
    片方の置き場がもう片方の中(中にあると、外側の DB の次の add が、送ったファイルをまた拾う)・ファイルが無い・動かせない記録。"""
    bad = _immovable(recs, targets)
    if bad:
        raise ValueError(bad)
    if B.ROOT == ROOT or os.path.realpath(B.INDEX) == os.path.realpath(INDEX):
        raise ValueError("the other DB is this DB (%s)" % INDEX)
    dest, here = B.library_of(B.load_index()[0] if exists(B.INDEX) else []), library_of(recs)
    if not dest:
        raise ValueError('the other DB has no folder for its PDFs yet. Put "library": "<folder>" in %s' % os.path.join(B.ROOT, "config.json"))
    inside = lambda a, b: os.path.commonpath([os.path.realpath(a), os.path.realpath(b)]) == os.path.realpath(b)
    if here and (inside(dest, here) or inside(here, dest)):
        raise ValueError("one DB keeps its PDFs inside the other's folder (%s and %s): the next papers add in the outer one would index "
                         "the sent files again. Give the other DB a separate folder" % (here, dest))
    taken, plan = set(), []
    for r in targets:
        if not exists(r["path"]):
            raise ValueError("the file is missing: %s" % r["path"])
        folder = KINDS.get(r.get("kind"), (None, None))[1] if r.get("not_paper") else None
        plan.append((r, _dest_name(os.path.join(dest, folder) if folder else dest, r["filename"], taken, exists)))
    return plan


def send_apply(recs, stamp, plan, B):
    """予定どおりに送る: 両方の索引を退避 → 動かす予定を書き残す → 全頁の本文を相手に写してからファイルを移す → 相手の索引に足す →
    こちらの索引から外す。途中で止まっても、動かした分は両方の索引に書く。相手に書くのが先(逆だと、こちらを外した後に止まったとき、
    記録がどちらにも無くなる)。"""
    _backup("send")
    b_recs, b_stamp = B.load_index() if os.path.exists(B.INDEX) else ([], None)
    if b_stamp:
        B._backup("receive")
    _atomic_write(_trail("sends"), [json.dumps({"from": r["path"], "to": new, "db": B.ROOT}, ensure_ascii=False) + "\n" for r, new in plan])
    sent = []
    try:
        for r, new in plan:
            key = _file_key(r["path"])
            text = _fulltext_load(key) if key else None
            if text:                               # 動かす前に写す(写せなければ、この記録は動かさない)
                B._fulltext_save(key, text.get("how"), text["pages"], text.get("thin"))
            os.makedirs(os.path.dirname(new), exist_ok=True)
            _move_file(r["path"], new)
            sent.append((r, dict(r, path=new, folder=os.path.dirname(new), filename=os.path.basename(new))))
    finally:
        if sent:
            arrived = b_recs + [x for _, x in sent]
            if b_stamp:
                B.save_index(arrived, b_stamp)
            else:
                B._atomic_write(B.INDEX, [json.dumps(x, ensure_ascii=False) + "\n" for x in arrived])
            gone = {id(r) for r, _ in sent}
            save_index([r for r in recs if id(r) not in gone], stamp)
        _stopped("send", len(sent))


def send():
    """papers send <パスかファイル名の一部>… --to <別の DB のフォルダ> [--nonpaper] [--apply]。記録とファイル(と全頁の本文)を別の DB へ移す。
    ファイルは相手の DB の論文の置き場へ(種類の付いた論文以外は、その下の種類のフォルダへ)。--nonpaper は、論文以外の印の付いた記録の
    全部。既定は予定を出すだけ。--apply で両方の索引を退避してから動かす。戻すには、相手の DB から送り返す。"""
    args = sys.argv[2:]
    if "--to" not in args:
        sys.exit(send.__doc__)
    home = args[args.index("--to") + 1]
    names = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--to")]
    recs, stamp = load_index()
    targets = [_one_record(recs, n) for n in names] + ([r for r in recs if r.get("not_paper") and not moved_aside(r)] if "--nonpaper" in args else [])
    targets = list({id(r): r for r in targets}.values())
    if not targets:
        sys.exit(send.__doc__)
    B = other_db(home)
    try:
        plan = send_plan(recs, targets, B)
    except ValueError as e:
        sys.exit(str(e))
    for r, new in plan[:40]:
        print("  %s\n→ %s" % (r["path"], new))
    if "--apply" not in args:
        print("%d record(s) would move to the DB in %s. DRY RUN. To move them: --apply" % (len(plan), B.ROOT)); return
    send_apply(recs, stamp, plan, B)
    print("%d record(s) sent. Update both searches: papers index && PAPERS_HOME=%s papers index" % (len(plan), B.ROOT))


def _fix_bad(sets):
    """直せない項目(FIX_KEYS に無い)があれば、その文面。無ければ None。"""
    bad = [k for k in sets if k not in FIX_KEYS]
    return "直せない項目: %s(直せるのは %s)" % (", ".join(bad), ", ".join(FIX_KEYS)) if bad else None


DOI_RA = "https://doi.org/ra/"
ARXIV_DOI = re.compile(r'10\.48550/arxiv\.(\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?', re.I)


def doi_urls(doi):
    """by_doi がその DOI のために取りに行きうる URL の全部(「?」より前)。画面のサーバは、DOI を引く間、これだけを外へ通す。"""
    ax = ARXIV_DOI.fullmatch(doi)
    if ax:
        return ["http://export.arxiv.org/api/query?id_list=" + ax.group(1)]
    q = urllib.parse.quote(doi)
    return ["https://api.crossref.org/works/" + q, DOI_RA + q] + [base + q for _, base, _ in DOI_AGENCIES.values()]


def doi_agency(doi):
    """DOI の登録機関の名前(doi.org が答える。Crossref・DataCite・JaLC など)。その DOI が無ければ None。
    届かない・答えがその DOI のものでなければ例外。キャッシュしない(「無い」という答えは、後で登録されると変わる)。"""
    d = json.loads(_fetch(DOI_RA + urllib.parse.quote(doi)).decode())
    d = d[0] if isinstance(d, list) and d and isinstance(d[0], dict) else {}
    if (d.get("DOI") or "").lower() != doi.lower():
        raise ValueError("doi.org answered about another DOI")
    return d.get("RA")


def _csl_record(m, doi, source):
    """登録機関(DataCite)が返した CSL JSON を記録にする。その DOI の答えでない・表題が無いものは None。
    著者は Crossref と同じ「姓 名」で、姓の並びを families に持つ(和文の名前は、位置では姓を決められない)。"""
    if not isinstance(m, dict) or (m.get("DOI") or "").lower() != doi.lower():
        return None
    one = lambda v: clean_text(str((v[0] if v else "") if isinstance(v, list) else v or ""))
    who = [x for x in m.get("author") or [] if isinstance(x, dict)]
    names = [clean_text(" ".join(str(v) for v in (x.get("family"), x.get("given")) if v) or str(x.get("literal") or x.get("name") or "")) for x in who]
    fams = [clean_text(str(x.get("family") or "")) or n for x, n in zip(who, names)]
    keep = [i for i, n in enumerate(names) if n][:20]
    y = str(((m.get("issued") or {}).get("date-parts") or [[None]])[0][0])
    if not one(m.get("title")):
        return None
    return {"doi": doi, "title": one(m.get("title")), "year": int(y) if y.isdigit() else None,
            "first_author": fams[keep[0]] if keep else "", "authors": [names[i] for i in keep], "families": [fams[i] for i in keep],
            "journal": one(m.get("container-title")) or None, "journal_short": None,
            "volume": one(m.get("volume")) or None, "issue": one(m.get("issue") or m.get("number")) or None, "pages": one(m.get("page")) or None,
            "abstract": clean_text(re.sub(r'<[^>]+>', ' ', str(m.get("abstract") or ""))) or None, "source": source}


def _jalc_record(m, doi, source):
    """JaLC の API の答えを記録にする。その DOI の答えでない・表題が無いものは None。表題・著者・誌名は、英語の欄があれば英語を採る
    (検索を英字に揃える。HANDOFF.md「決定済み」)。著者は「姓 名」で、姓の並びを families に持つ。"""
    x = m.get("data") if isinstance(m, dict) else None
    if not isinstance(x, dict) or (x.get("doi") or "").lower() != doi.lower():
        return None
    rows = lambda v: [i for i in v if isinstance(i, dict)] if isinstance(v, list) else []
    text = lambda v: clean_text(str(v or ""))
    en = lambda i: i.get("lang") != "en"                         # 並べ替えの鍵: 英語の項目を先に(sorted は元の順を保つ)
    title = next((text(i["title"]) for i in sorted(rows(x.get("title_list")), key=en) if i.get("title")), "")
    if not title:
        return None
    names, fams = [], []
    for c in sorted(rows(x.get("creator_list")), key=lambda c: int(c["sequence"]) if str(c.get("sequence") or "").isdigit() else 10 ** 6):
        n = next(iter(sorted(rows(c.get("names")), key=en)), {})
        name = text(" ".join(str(v) for v in (n.get("last_name"), n.get("first_name")) if v))
        if name:
            names.append(name)
            fams.append(text(n.get("last_name")) or name)
    journal = next((text(i["journal_title_name"]) for i in sorted(rows(x.get("journal_title_name_list")), key=lambda i: (en(i), i.get("type") != "full"))
                    if i.get("journal_title_name")), "")
    when = x.get("publication_date") if isinstance(x.get("publication_date"), dict) else {}
    y, first, last = str(when.get("publication_year") or ""), text(x.get("first_page")), text(x.get("last_page"))
    first = first if re.search(r'\w', first) else ""              # 頁の無い記録は「-」を入れてくる
    return {"doi": doi, "title": title, "year": int(y) if y.isdigit() else None, "first_author": fams[0] if fams else "",
            "authors": names[:20], "families": fams[:20], "journal": journal or None, "journal_short": None,
            "volume": text(x.get("volume")) or None, "issue": text(x.get("issue")) or None,
            "pages": (first + ("-" + last if last and last != first else "")) or None, "abstract": None, "source": source}


# 人が書いた DOI を引く先(fix・画面の「直す」)。DOI の登録機関のうち、決まった宛先が書誌を返すもの:
# {doi.org が答える機関名: (source の値, 宛先, 答えを記録にする関数)}。Crossref は from_crossref、arXiv の DOI は from_arxiv。
# doi.org に書誌を頼む形(転送)は使わない。理由は DESIGN.md「人が直した記録の扱い」
DOI_AGENCIES = {"DataCite": ("datacite", "https://data.crosscite.org/application/vnd.citationstyles.csl+json/", _csl_record),
                "JaLC": ("jalc", "https://api.japanlinkcenter.org/dois/", _jalc_record)}


ARTICLE_KEYS = ("journal", "journal_short", "volume", "issue", "pages", "article_number")


def _as_book(b):
    """目録で引いた本の記録を、記録の上に書く形に。第一著者の姓を入れ、論文の項目(ARTICLE_KEYS)を空にする
    (論文と誤って同定されていた本を直すとき、前の誌名・巻・頁を残さない)。"""
    return dict(dict.fromkeys(ARTICLE_KEYS), **dict(b, first_author=(b.get("families") or [""])[0]))


def by_isbn(isbn):
    """人が書いた ISBN 1 つから本の書誌を引いて返す(目録は books_by_isbn と同じ)。引けなければ LookupError(理由を英語の 1 文で)。
    届かなかった問い合わせは _FAILED に残る。"""
    b, why = _book_by_hand(isbn, None)
    if b:
        return _as_book(b)
    if _FAILED:
        raise LookupError("could not reach the book catalogues (%s). Try again later" % ", ".join(sorted({f.split(":")[0] for f in _FAILED})))
    raise LookupError(why + ". Check the number, or fill in the fields by hand")


def by_doi(doi):
    """人が書いた DOI 1 つから書誌を引いて返す。引けなければ LookupError(理由を英語の 1 文で)。届かなかった問い合わせは _FAILED に残る。
    arXiv の DOI は arXiv。ほかは、書籍の DB なら本として Crossref、次に雑誌の論文として Crossref、無ければ doi.org に
    登録機関を聞いて DOI_AGENCIES の宛先。「.」「..」だけの区切りを含む文字列は引かない(宛先のサーバの、別の道になる)。"""
    if not DOI_RE.fullmatch(doi) or {".", ".."} & set(doi.split("/")):
        raise LookupError("this is not a DOI: %s" % doi)
    ax = ARXIV_DOI.fullmatch(doi)
    if ax:
        m = from_arxiv(ax.group(1))
    else:
        b = _book_by_hand(None, doi)[0] if BOOKS else None
        m = _as_book(b) if b else from_crossref(doi)
    if m:
        return m
    if _FAILED:
        raise LookupError("could not reach %s (network or server trouble). Try again later" % ("arXiv" if ax else "Crossref"))
    if ax:
        raise LookupError("arXiv has no paper %s. Check the DOI" % ax.group(1))
    try:
        ra = doi_agency(doi)
    except Exception as e:
        _lookup_failed(e, "doi.org:" + doi)
        raise LookupError("could not ask doi.org who registered DOI %s. Try again later" % doi)
    if ra is None:
        raise LookupError("DOI %s does not exist (doi.org does not know it). Check the DOI" % doi)
    if ra == "Crossref":
        raise LookupError("DOI %s is at Crossref, but it is not a journal article%s. Fill in the fields by hand"
                          % (doi, " or a whole book" if BOOKS else ""))
    if ra not in DOI_AGENCIES:
        raise LookupError("DOI %s is registered with %s, which papers cannot ask. Fill in the fields by hand" % (doi, ra))
    source, base, read = DOI_AGENCIES[ra]
    try:
        m = read(get_json(base + urllib.parse.quote(doi)), doi, source)
    except Exception as e:
        _lookup_failed(e, source + ":" + doi)
        raise LookupError("could not get the record of DOI %s from %s (%s). Fill in the fields by hand, or try again later"
                          % (doi, ra, "no answer" if _FAILED else "it answered %s" % getattr(e, "code", type(e).__name__)))
    if m is None:
        raise LookupError("%s has no usable record (no title) for DOI %s. Fill in the fields by hand" % (ra, doi))
    return m


def fix_record(r, doi, sets, isbn=None):
    """直した記録(新しい辞書)を返す。r は変えない。doi があれば by_doi、isbn があれば by_isbn で引き直し、sets の項目を上に書く
    (doi と isbn の両方は ValueError)。
    authors は「;」で区切った名前の並び(1 人ぶんは「名 姓」の順)。first_author(第一著者の姓)が空なら、先頭の名前の
    最後の語を入れる(arXiv の記録と同じ決め方)。出どころの無い記録(未同定)は source=manual にする(改名・引用の対象に入る)。
    著者を引き直すか、著者・第一著者を手で変えたら、前の姓の並び(families)は捨てる(本の名前と引用の姓は、それから作られる)。
    直せない項目は ValueError、引けない DOI・ISBN は LookupError。fix と GUI が使う(直し方を 1 か所にする)。"""
    if _fix_bad(sets):
        raise ValueError(_fix_bad(sets))
    if doi and isbn:
        raise ValueError("give either a DOI or an ISBN, not both")
    new = dict(r)
    if doi or isbn:
        new.pop("families", None)                  # 引き直す前の著者に付いていた姓
        new.update(by_doi(doi) if doi else by_isbn(isbn))
    for k, v in sets.items():
        new[k] = int(v) if k == "year" and v.isdigit() else (v or None)
    if sets.get("authors"):
        new["authors"] = [a for a in (" ".join(x.split()) for x in sets["authors"].split(";")) if a] or None
        if new["authors"] and not new.get("first_author"):
            new["first_author"] = new["authors"][0].split()[-1]
    if "authors" in sets or "first_author" in sets:
        new.pop("families", None)                  # 直す前の著者に付いていた姓。残すと book_filename と引用が古い姓を使う
    if not new.get("source"):                      # 未同定の記録は source を None で持つ(項目はある)
        new["source"] = "manual"
    new.update(rung="manual", fixed_by="user", fixed_at=time.strftime("%Y-%m-%d"))
    return new


def fix_diff(r, new):
    """直す前と後で違う項目を、1 項目 1 行で。並び(著者)は人数を先に出す(何人と読んだかが、切り詰めても見える)。"""
    show = lambda v: ("%d: %s" % (len(v), "; ".join(map(str, v))) if isinstance(v, list) else str(v))[:60]
    return ["  %-14s %s → %s" % (k, show(r.get(k)), show(new.get(k)))
            for k in sorted(set(r) | set(new)) if r.get(k) != new.get(k) and k not in ("fixed_at",)]


def fix_apply(recs, stamp, r, new):
    """recs の中の記録 r を new に置き換えて索引を書く。書く前に backups/ へ退避する。"""
    _backup("fix")
    r.clear(); r.update(new)
    save_index(recs, stamp)


def fix():
    """1 件の記録を直す。既定は変更前と変更後を出すだけ。--apply で索引を書く(書く前に backups/ へ退避)。

        python3 organize.py fix <パスかファイル名の一部> --doi 10.1234/example.2020.001    DOI から引き直す(相手は by_doi)
        python3 organize.py fix <パスかファイル名の一部> --isbn 978-4-00-000000-0          本を ISBN から引き直す(相手は by_isbn)
        python3 organize.py fix <パスかファイル名の一部> --set year=1993 --set first_author=Yamada

    直した記録は rung=manual(人が決めた値。vet も上書きしない)。ファイル名と検索に反映するには、続けて
    `rename --apply` と `index`。直せる項目は FIX_KEYS だけ(path・filename・rung は直さない)。"""
    args = sys.argv[2:]
    target = next((a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--doi", "--isbn", "--set"))), None)
    doi, isbn = (args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else None for k in ("--doi", "--isbn"))
    sets = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--set" and "=" in args[i + 1])
    if not target or not (doi or isbn or sets):
        sys.exit(fix.__doc__)
    if _fix_bad(sets):
        sys.exit(_fix_bad(sets))
    recs, stamp = load_index()
    r = _one_record(recs, target)
    try:
        new = fix_record(r, doi, sets, isbn)
    except (ValueError, LookupError) as e:
        sys.exit(str(e))
    print(r["path"])
    for line in fix_diff(r, new):
        print(line)
    if "--apply" not in args:
        print("DRY RUN. 書くには --apply"); return
    fix_apply(recs, stamp, r, new)
    print("書いた。ファイル名と検索に反映: python3 organize.py rename --apply && python3 organize.py index")


def gui():
    """papers gui [--port=N] [--no-open]。検索の画面のサーバ(gui.py)に替わる。127.0.0.1 で待ち、ブラウザで開く。止めるのは Ctrl-C。
    使い方は MANUAL「画面で検索する」、設計は GUI_DESIGN.md。"""
    os.execv(sys.executable, [sys.executable, os.path.join(CODE, "gui.py")] + sys.argv[2:])


REVIEW_PAGE = os.path.join(ROOT, "review.html")


def review_page_html(rows, template):
    """候補の行を JSON にしてページに埋める。< > & を \\u 形式にし、表題に </script> があってもページを壊さない
    (ページ側は textContent で描く)。"""
    data = json.dumps(rows, ensure_ascii=False)
    for a, b in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        data = data.replace(a, b)
    return template.replace("__DATA__", data)


def review_page():
    """unresolved.tsv の候補のある行から、y / n を押して確かめるページ review.html を作る(手元で開く)。"""
    rows = [l.rstrip("\n").split("\t") for l in open(REVIEW_TSV)]
    head, out = rows[0], []
    for r in rows[1:]:
        d = dict(zip(head, r))
        if d.get("cand_doi"):
            out.append({k: d.get(k, "") for k in ("reason", "path", "hint", "cand_doi", "cand_title", "cand_where", "cand_year")}
                       | {"file": os.path.basename(d["path"]), "folder": os.path.basename(os.path.dirname(d["path"])),
                          "stamp": "1"})
    tpl = open(os.path.join(CODE, "review_page.html")).read()
    _atomic_write(REVIEW_PAGE, [review_page_html(out, tpl)])
    print("%d 件の候補を %s に。開く: open %s" % (len(out), REVIEW_PAGE, REVIEW_PAGE))


def apply_ok(lines, ok):
    """unresolved.tsv の行に、ページで付けた判定 ok {(path, cand_doi): OK_VALUES のどれか} を入れる。候補が変わった行
    (review をやり直した)と表に無い行は入れない。(新しい行, 入れた数, 入れなかった数) を返す。"""
    head = lines[0].rstrip("\n").split("\t")
    ip, ic, io = head.index("path"), head.index("cand_doi"), head.index("ok")
    out, n, seen = [lines[0]], 0, set()
    for l in lines[1:]:
        r = l.rstrip("\n").split("\t")
        v = ok.get((r[ip], r[ic])) if r[ic] else None
        if v in OK_VALUES:
            r[io], n = v, n + 1
            seen.add((r[ip], r[ic]))
        out.append("\t".join(r) + "\n")
    return out, n, sum(1 for k, v in ok.items() if v in OK_VALUES and k not in seen)


def review_ok():
    """ページが書き出した review-ok.tsv(path, cand_doi, ok)を unresolved.tsv の ok 列に入れる。"""
    ok = {}
    for l in list(open(sys.argv[2]))[1:]:
        r = l.rstrip("\n").split("\t")
        if len(r) == 3:
            ok[(r[0], r[1])] = r[2].strip().lower()
    out, n, stale = apply_ok(list(open(REVIEW_TSV)), ok)
    _atomic_write(REVIEW_TSV, out)
    print("%d 件の判定を %s に入れた。候補が変わった・表に無いので入れなかった %d 件。次: python3 organize.py merge"
          % (n, REVIEW_TSV, stale))


def pubver_merge():
    """ok=y は published_doi に書き、n は published_doi_rejected に足す(次から候補に出さない)。他の値は残す。"""
    recs, stamp = load_index()
    rows = _pubver_filled(PUBVER_TSV, recs)
    by = {r["path"]: r for r in recs}
    y = n = other = 0
    for x in rows:
        v, r = x[-1].strip().lower(), by[x[0]]
        if v == "y":
            r["published_doi"], r["published_doi_by"] = x[3], "user"; y += 1
        elif v == "n":
            r["published_doi_rejected"] = (r.get("published_doi_rejected") or []) + [x[3]]; n += 1
        else:
            other += 1
    _backup("pubver")
    save_index(recs, stamp)
    print("出版版 %d 件、違う %d 件を索引に。y/n 以外の %d 行は書いていない" % (y, n, other))




# ---------- enrich: 抄録は PDF から切らず、書誌 API から取る ----------
def _inverted(inv):
    pos = {}
    for w, ixs in (inv or {}).items():
        for i in ixs:
            pos[i] = w
    return " ".join(pos[k] for k in sorted(pos)) or None


def _fetch_outcome(e):
    """取得の例外を「記録が無い」(確定。再実行しても変わらない)と「取れなかった」(一時的)に分ける。
    404 は「その記録が無い」という確定した答え。分け方は _retry_plan と同じ: 再送する状態(RETRY_STATUS)と、応答の無い失敗(接続断・
    時間切れ)だけが再実行で取り直せる。それ以外の HTTP の状態(400・403・404 など)は待っても変わらない
    (arXiv は ID の形でない文字列に 400 を返す。DESIGN.md「取れなかった問い合わせ」)。"""
    code = getattr(e, "code", None)
    return "failed" if code is None or code in RETRY_STATUS else "absent"


def enrich():
    """DOI から OpenAlex の抄録を取る。索引のみを読む再実行可能なパス。
    Crossref の抄録より多く取れる。PDF のレイアウト解析(GROBID)は使わない(どちらも HANDOFF.md「決定済み」)。"""
    recs, stamp = load_index()
    got = failed = absent = no_abs = 0
    aborted = None
    try:
        for i, r in enumerate(recs):
            if r.get("abstract") or not enrichable(r):
                continue
            try:
                m = get_json("https://api.openalex.org/works/doi:" +
                             urllib.parse.quote(r["doi"]) + "?mailto=" + MAILTO)
            except Exception as e:
                if _fetch_outcome(e) == "absent":
                    absent += 1           # OpenAlex に記録が無い。再実行しても変わらない
                else:
                    failed += 1           # 「抄録が無い」と混ぜない。再実行で取り直せる集合
                continue
            a = _inverted(m.get("abstract_inverted_index"))
            if a and len(a) > 80:
                r["abstract"] = a
                r["abstract_source"] = "openalex"
                got += 1
            else:
                no_abs += 1
            for k, v in (("subject", [c["display_name"] for c in (m.get("topics") or [])[:3]]),):
                if v and not r.get(k):
                    r[k] = v               # OpenAlex の topics は Crossref の subject より埋まる
            if i % 50 == 0:
                print("  %d/%d" % (i, len(recs)), file=sys.stderr)
    except RateLimitAbort as e:
        aborted = e                        # 中断しても、ここまでの取得は捨てない
    save_index(recs, stamp)
    tot = sum(1 for r in recs if r.get("abstract"))
    print("抄録 +%d 件 → %d/%d (%.0f%%)" % (got, tot, len(recs), 100.0 * tot / max(len(recs), 1)))
    # 誤りが隠れる集合を分けて出す: 「抄録が無かった」と「取れなかった」は別物で、
    # 後者だけが再実行で回収できる。合算すると再実行の要否が数字から読めなくなる。
    # 404 は以前「取れなかった」に数えていたが、再実行しても取れないので「無い」側に分けた。
    print("OpenAlex に記録はあるが抄録なし %d 件 / 記録そのものが無い %d 件（どちらも再実行しても変わらない）"
          % (no_abs, absent))
    if failed:
        print("取得失敗 %d 件（一時的な失敗。再実行で取り直せます）" % failed)
    if aborted:
        print("中断: %s" % aborted, file=sys.stderr)
        sys.exit(1)


# ---------- dedup: 同一 DOI を _重複文献/ に集める ----------
DEDUP_KEEP = os.path.join(ROOT, "dedup-keep.txt")


def dedup():
    """各 DOI につき1本だけ元の場所に残し、残りを _重複文献/ へ移す。削除はしない。
    残すのは最大のファイル。ただし dedup-keep.txt(1 行 1 パス)にあるものを優先する(利用者の指定。
    最大だと著者原稿・ResearchGate の表紙付きが残り出版社版が移る組がある: DESIGN.md「重複の集約」)。"""
    if BOOKS:
        sys.exit("papers dedup is not available in a book DB: a chapter and the whole book share one ISBN / DOI and are not duplicates.")
    apply = "--apply" in sys.argv
    recs, stamp = load_index()
    keep = {l.strip() for l in open(DEDUP_KEEP) if l.strip()} if os.path.exists(DEDUP_KEEP) else set()
    # 同じファイルを指す記録が 2 行あっても、ファイルは 1 つ。重複とみなすと、唯一の実体を _重複文献/ へ移してしまう
    # 同じファイルかは、リンクをたどった先の (デバイス, inode) で見る: 同じパス・リンク・大小文字だけ違うパスは同じになり、
    # 大小文字を区別するボリュームの Paper.pdf と paper.pdf は別になる
    def same_file(r):
        st = os.stat(r["path"])
        return (st.st_dev, st.st_ino)
    groups, twice, alias = collections.defaultdict(dict), 0, collections.defaultdict(list)
    for r in recs:
        key = dedup_key(r)
        if key and os.path.exists(r["path"]) and not moved_aside(r):
            f = same_file(r)
            alias[f].append(r)
            if f not in groups[key] or (os.path.islink(groups[key][f]["path"]) and not os.path.islink(r["path"])):
                twice += f in groups[key]
                groups[key][f] = r                 # 代表は実体の行(リンクの行を代表にすると、リンクだけを移す)
            else:
                twice += 1
    dups = {k: list(v.values()) for k, v in groups.items() if len(v) > 1}
    pairs = [(a, b) for a, b in preprint_pairs(recs) if os.path.exists(a["path"]) and os.path.exists(b["path"])
             and same_file(a) != same_file(b)]
    if twice:
        print("同じファイルを指す記録が、ほかに %d 行ある(索引の行の重なり。1 つのファイルとして数える)" % twice)
    if not dups and not pairs:
        print("重複なし"); return
    root = os.path.commonpath([r["folder"] for r in recs])
    dest = os.path.join(root, "_重複文献")
    plan = []
    for k, v in sorted(dups.items()):
        v.sort(key=lambda r: (r["path"] not in keep, -os.path.getsize(r["path"])))   # 指定、次に最大を原位置に残す
        for r in v[1:]:
            to = os.path.join(dest, os.path.basename(r["path"]))
            i = 2
            while _same_name(to) in {_same_name(p["to"]) for p in plan} or os.path.exists(to):
                b, e = os.path.splitext(os.path.basename(r["path"]))
                to = os.path.join(dest, "%s_%d%s" % (b, i, e)); i += 1
            plan.append({"from": r["path"], "to": to, "doi": k, "keep": v[0]["path"]})
    # 出版版も持っているプレプリントは別フォルダへ(削除は利用者が行う。パイプラインは削除しない)
    pdest = os.path.join(root, "_プレプリント(出版版あり)")
    for a, b in pairs:
        if a["path"] in [p["from"] for p in plan]:
            continue                                   # 同じプレプリントの重複としてすでに移す
        to = os.path.join(pdest, os.path.basename(a["path"]))
        i = 2
        while _same_name(to) in {_same_name(p["to"]) for p in plan} or os.path.exists(to):
            base, ext = os.path.splitext(os.path.basename(a["path"]))
            to = os.path.join(pdest, "%s_%d%s" % (base, i, ext)); i += 1
        plan.append({"from": a["path"], "to": to, "doi": a["published_doi"], "keep": b["path"], "kind": "preprint"})
    unknown = keep - {r["path"] for v in dups.values() for r in v}
    if unknown:
        sys.exit("dedup-keep.txt に重複の組に無いパスがある(打ち間違いか、もう移した): %s" % sorted(unknown))
    for k, v in sorted(dups.items()):
        if v[0]["path"] in keep:
            print("  残す指定: %s" % v[0]["path"].replace(root + os.sep, ""))
    for a, b in pairs[:10]:
        print("  プレプリント %s → 出版版 %s" % (os.path.basename(a["path"]), os.path.basename(b["path"])))
    for k, v in list(sorted(dups.items()))[:10]:
        print("  %s" % k)
        print("    残す: %s (%.1fMB)" % (os.path.basename(v[0]["path"]),
                                        os.path.getsize(v[0]["path"]) / 1e6))
        for r in v[1:]:
            print("    移動: %s" % r["path"].replace(root + os.sep, ""))
    npre = sum(1 for p in plan if p.get("kind") == "preprint")
    print("\n%d 種の重複、%d ファイルを %s へ。出版版もあるプレプリント %d ファイルを %s へ"
          % (len(dups), len(plan) - npre, dest, npre, pdest))
    if not apply:
        print("DRY RUN. 実行するには --apply"); return
    _backup("dedup")                           # フォルダを作るより先
    for d in {os.path.dirname(p["to"]) for p in plan}:
        os.makedirs(d, exist_ok=True)
    trail = _trail("dedup")
    with open(trail, "w") as f:
        for p in plan:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    moved = {}
    try:
        for p in plan:
            if os.path.exists(p["from"]):
                _move_file(p["from"], p["to"])
                moved[p["from"]] = p["to"]
    finally:                                       # 途中で止まっても、動かした分は索引に書く
        for old in list(moved):                    # 同じファイルを別の書き方(大小文字・リンク)で指す行も、一緒に移動先を指させる
            rep = [r for rs in alias.values() for r in rs if r["path"] == old]
            for rs in alias.values():
                if rep and rep[0] in rs:
                    for r in rs:
                        if r["path"] != old:
                            moved[r["path"]] = moved[old]
        for r in recs:
            if r["path"] in moved:
                r["path"] = moved[r["path"]]
                r["folder"], r["filename"] = os.path.dirname(r["path"]), os.path.basename(r["path"])   # _2 が付けば名前も変わる
                p = [p for p in plan if p["to"] == r["path"]][0]
                r["published_copy" if p.get("kind") == "preprint" else "duplicate_of"] = p["keep"]
        save_index(recs, stamp)
        _stopped("dedup", len(moved))
    print("%d 件を移動。戻すには %s を使う" % (len(moved), os.path.basename(trail)))



# ---------- search: FTS5(trigram) + 文ベクトル ----------
# ---- 全頁の本文(検索専用) ----
# 同定に使う text(先頭 3,000 字)は papers.jsonl にある。全頁の本文は大きいので索引には入れず、ここに置く(DESIGN.md「全文の索引」)。
# 鍵はファイルの中身から作る(パスを鍵にすると、改名・移動のたびに読み直しになる)。消してもよい: papers fulltext が作り直す。
FULLTEXT = os.path.join(ROOT, "fulltext")
FULLTEXT_V = 1
THIN_CHARS = 200            # 1 頁あたりの平均がこれ未満のファイルは、テキスト層が乏しいとみなして全頁を OCR する
PDFTOTEXT_TIMEOUT = 30      # 全頁の読み取りの時間切れ(秒)。超えたら PDFKit(ptext.swift)で読む
OCR_CHUNK = 8               # 一度に画像にする頁数(全頁を一度に画像にすると、数百頁の本で一時ファイルが大きくなる)


def _file_key(path):
    """全頁の本文の鍵。ファイルの大きさと、先頭・末尾の 64 KB から作る。読めなければ None。
    中身が同じファイル(別のフォルダの写し)は同じ鍵になり、本文を共有する。"""
    try:
        n = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(65536)
            f.seek(max(0, n - 65536))
            tail = f.read(65536)
    except OSError:
        return None
    return hashlib.sha1(b"%d\0" % n + head + tail).hexdigest()


def _fulltext_path(key):
    return os.path.join(FULLTEXT, key[:2], key + ".json")


def _fulltext_load(key):
    """鍵の本文 {"how": 読み方, "pages": [頁の本文…], "thin": OCR 待ち}。無い・読めない・版が違えば None(読み直す)。"""
    try:
        e = json.load(open(_fulltext_path(key)))
    except (OSError, ValueError):
        return None
    return e if isinstance(e, dict) and e.get("v") == FULLTEXT_V and isinstance(e.get("pages"), list) else None


def _fulltext_save(key, how, pages, thin):
    os.makedirs(os.path.dirname(_fulltext_path(key)), exist_ok=True)
    _atomic_write(_fulltext_path(key), [json.dumps({"v": FULLTEXT_V, "how": how, "pages": pages, "thin": thin}, ensure_ascii=False)])


def _split_pages(t):
    """pdftotext の出力を頁に分ける(改頁が頁の終わり。最後の空の片は頁ではない)。頁ごとに空白の並びを 1 つにする。"""
    pages = t.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return [" ".join(p.split()) for p in pages]


def _thin(pages):
    """テキスト層が乏しい(スキャン)。1 頁あたりの平均で見る: 図だけの頁・白紙が混ざる PDF を OCR に回さない。"""
    return sum(map(len, pages)) < THIN_CHARS * len(pages)


def _ptext():
    """ptext.swift(PDFKit で全頁の本文を読む)を必要になった時だけビルドする。swiftc が無ければ None。"""
    exe, src = os.path.join(CODE, "ptext"), os.path.join(CODE, "ptext.swift")
    try:
        if not os.path.exists(exe) or os.path.getmtime(src) > os.path.getmtime(exe):
            subprocess.run(["swiftc", "-O", "-o", exe, src], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return exe if os.path.exists(exe) else None


def _run_text(cmd, timeout):
    """(標準出力, 失敗の理由)。成功なら理由は None。sh() と違い、時間切れ・失敗を空の本文と区別する。"""
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "", "timed out after %d s" % timeout
    except OSError as e:
        return "", str(e)
    return p.stdout.decode("utf-8", "replace"), None if p.returncode == 0 else "exit status %d" % p.returncode


def _text_layer(path):
    """全頁のテキスト層を (頁の並び, 読み方) で返す。pdftotext が時間切れ・失敗なら PDFKit で読む。
    どちらも読めなければ ([], 理由)。頁が 1 つも無い出力は読めなかった扱い(空の本文として置かない)。"""
    out, why = _run_text(["pdftotext", path, "-"], PDFTOTEXT_TIMEOUT)
    if why is None and _split_pages(out):
        return _split_pages(out), "pdftotext"
    why = "pdftotext: %s" % (why or "no pages")
    exe = _ptext()
    if exe:
        out, why2 = _run_text([exe, path], 120)
        if why2 is None and _split_pages(out):
            return _split_pages(out), "pdfkit"
        why += "; pdfkit: %s" % (why2 or "no pages")
    return [], why


def _ocr_pages(path, first, last):
    """first〜last 頁を OCR して {頁番号: 本文}。画像にできなかった頁は入らない。"""
    import tempfile, glob as _g
    d = tempfile.mkdtemp()
    sh(["pdftoppm", "-f", str(first), "-l", str(last), "-r", "200", "-png", path, os.path.join(d, "p")], timeout=300)
    out = {}
    for png in sorted(_g.glob(os.path.join(d, "p*.png"))):
        exe = _vocr()
        out[int(re.search(r'(\d+)\.png$', png).group(1))] = " ".join(
            sh([exe, png, OCR_LANG] if exe else ["tesseract", png, "stdout"], timeout=180).split())
        os.remove(png)
    os.rmdir(d)
    return out


def _ocr_all(path, n):
    """n 頁のファイルの全頁を OCR して、(頁の並び(長さ n), 画像にできた頁の数)で返す。画像にできなかった頁は空。"""
    got = {}
    for first in range(1, n + 1, OCR_CHUNK):
        got.update(_ocr_pages(path, first, min(n, first + OCR_CHUNK - 1)))
    return [got.get(i, "") for i in range(1, n + 1)], sum(1 for i in range(1, n + 1) if i in got)


def fulltext_fill(recs, do_ocr=True):
    """全頁の本文がまだ無いファイルを読んで fulltext/ に置く。papers.jsonl は書かない。
    テキスト層が乏しいファイルは、do_ocr なら全頁を OCR する(しなければ OCR 待ちの印を付けて、次の機会に回す)。
    読めなかったファイルは置かない(次に呼ばれたときにまた読む)。返り値は件数の辞書と、読めなかった [(パス, 理由)]。"""
    from concurrent.futures import ThreadPoolExecutor
    todo, wait = {}, {}
    for r in recs:
        k = _file_key(r["path"])
        if not k:                                  # ファイルが無い・開けない(papers info が件数を出す)
            continue
        e = _fulltext_load(k)
        if e is None:
            todo.setdefault(k, r["path"])
        elif e.get("thin"):
            wait.setdefault(k, (r["path"], e))
    got = {"read": 0, "ocr": 0, "ocr_pages": 0, "pending": 0}
    failed = []
    if todo:
        _ptext()                                   # 並列に呼ぶ前に 1 度だけビルドする
    with ThreadPoolExecutor(8) as ex:
        for i, (k, (pages, how)) in enumerate(zip(todo, ex.map(_text_layer, todo.values()))):
            if i % 200 == 0 and len(todo) > 200:
                print("  全頁の本文 %d/%d" % (i, len(todo)), file=sys.stderr)
            if not pages:
                failed.append((todo[k], how))
                continue
            _fulltext_save(k, how, pages, _thin(pages))
            got["read"] += 1
            if _thin(pages):
                wait[k] = (todo[k], {"how": how, "pages": pages})
    if not do_ocr:
        got["pending"] = len(wait)
        return got, failed
    if wait:
        _vocr()                                    # 並列に呼ぶ前に 1 度だけビルドする
        print("  OCR: %d 件・%d 頁(1 頁 1 秒ほど)" % (len(wait), sum(len(e["pages"]) for _, e in wait.values())), file=sys.stderr)

    def one(item):
        path, e = item
        return _ocr_all(path, len(e["pages"]))
    with ThreadPoolExecutor(4) as ex:
        for i, (k, (pages, done)) in enumerate(zip(wait, ex.map(one, wait.values()))):
            path, e = wait[k]
            if done < len(pages):                  # 画像にできなかった頁がある(pdftoppm の失敗・時間切れ)。待ちの印を残し、次にまた OCR する
                failed.append((path, "ocr: rendered %d of %d pages" % (done, len(pages))))
                got["pending"] += 1
                continue
            # 頁ごとに、字の多いほうを採る(テキスト層のある頁が混ざるファイルで、その頁の本文を OCR で減らさない)
            best = [o if len(o) > len(t) else t for o, t in zip(pages, e["pages"])]
            used = sum(1 for o, t in zip(pages, e["pages"]) if len(o) > len(t))
            _fulltext_save(k, "ocr" if used else e["how"], best, False)   # 1 頁も増えない(白紙・図だけ)なら、待ちの印だけ外す
            if used:
                got["ocr"], got["ocr_pages"] = got["ocr"] + 1, got["ocr_pages"] + used
            if i % 10 == 0:
                print("  OCR %d/%d" % (i, len(wait)), file=sys.stderr)
    return got, failed


def fulltext_report(got, failed):
    print("全頁の本文: 新しく読んだ %d 件%s%s" % (got["read"],
          "、うち OCR %d 件・%d 頁" % (got["ocr"], got["ocr_pages"]) if got["ocr"] else "",
          "、OCR 待ち %d 件(papers fulltext)" % got["pending"] if got["pending"] else ""))
    if failed:
        print("note: could not read or OCR the full text of %d file(s); they stay searchable by what was read so far"
              % len(failed), file=sys.stderr)
        for path, why in failed[:10]:
            print("  %s (%s)" % (path, why), file=sys.stderr)


def fulltext():
    """papers fulltext [--no-ocr]。全頁の本文を読んで fulltext/ に置き、検索の索引を作り直す(検索専用。papers.jsonl は書かない)。
    まだ読んでいないファイルだけを読む。テキスト層の乏しい PDF(スキャン)は全頁を OCR する(1 頁 1 秒ほど)。
    --no-ocr なら OCR を後回しにする(次に --no-ocr なしで呼んだときに OCR する)。"""
    got, failed = fulltext_fill(load_index()[0], "--no-ocr" not in sys.argv)
    fulltext_report(got, failed)
    build_db().close()


def fulltext_of(recs, prev=lambda: {}):
    """記録ごとの、検索の索引に入れる全頁の本文(頁ごとに索引と同じ正規化をかけ、頁の間に改頁)。まだ読んでいなければ空。
    ファイルが開けない記録(置き場が見えない・同期待ち)は鍵が作れないので、prev() = 前の検索の索引の {パス: 本文} から引き継ぐ
    (引き継がないと、置き場が見えないときの作り直しで、全頁の本文が黙って消える)。"""
    out, old = [], None
    for r in recs:
        k = _file_key(r["path"])
        if k is None:
            old = prev() if old is None else old
            out.append(old.get(r["path"], ""))
            continue
        e = _fulltext_load(k)
        out.append("\f".join(_norm_text(p) for p in e["pages"]) if e else "")
    return out


def _prev_fulltext():
    """今の検索の索引の {パス: 全頁の本文}。無い・作り方が違う・読めないなら空。"""
    if not _db_format_ok():
        return {}
    try:
        c = sqlite3.connect(DB)
        out = dict(c.execute("SELECT path, fulltext FROM f WHERE fulltext != ''"))
        c.close()
        return out
    except sqlite3.Error:
        return {}


# papers.jsonl からの純粋な派生。古ければ search が黙って作り直す。
DB = os.path.join(ROOT, "papers.db")
COLS = ["path", "year", "doi", "first_author", "title", "authors",
        "journal", "folder", "abstract", "text"]
# bm25 は全列に重みが要る(UNINDEXED 列の分も)。text はページ1なのでタイトルを再掲する → 低く。
W = [0, 0, 8, 6, 10, 6, 4, 2, 3, 1]
# 全頁の本文は別の表 f に入れる(p の列 + fulltext)。順位は「p で当たる記録を p の順位で先に、f でだけ当たる記録をその後ろに」。
# 1 つの表にすると、文書の長さが bm25 に入って、全頁の本文を入れる前の順位が変わる(DESIGN.md「全文の索引」)。
FCOLS = COLS + ["fulltext"]
WF = W + [0.3]              # f でだけ当たる記録どうしの並びに使う
# 検索の索引の作り方の版。build_db の作る中身(トークナイザ・正規化・列)を変えたら上げる。古い版の papers.db は作り直す。
# 2: 文字列を NFKC にし、アクセントを畳む(remove_diacritics)
# 3: 字の間に離れて載るアクセント記号を落とす。本文・抄録・表題の仮名・漢字の間の空白を詰める。著者の欄の両端に空白を足す
# 4: 全頁の本文の表 f を足す
DB_FORMAT = "4"
# アクセントを畳む trigram を知らない古い SQLite では、畳まない trigram で作る(Müller と Muller は別の語のまま)
TOKENIZERS = ["trigram remove_diacritics 1", "trigram"]


def _norm(s):
    """索引に入れる文字列と検索語の両方にかける正規化。全角の英数字・半角の片仮名・合字(ﬁ)・分解形(u + ¨)を
    1 つの形にそろえる。片側だけにかけると、もう片側の形で載っている記録に当たらなくなる。
    字の間に離れて載るアクセント記号(古い LaTeX の PDF の Garci´a)は落とす。アクセントは索引が畳むので、無い形にそろえばよい。"""
    return unicodedata.normalize("NFKC", _LOOSE_ACCENT.sub("", s or ""))


def _norm_text(s):
    """索引に入れる本文・抄録・表題にだけかける。PDF の行の折り返しは空白になって語の途中に入る(コヒー レント)ので、
    仮名・漢字の間の空白を詰める。検索語にはかけない(検索語の空白は語の区切り)。"""
    return _CJK_GAP.sub("", _norm(s))


def _pad(s):
    """著者の欄の両端に空白を足す。2 文字以下の姓を " li " の形(空白込みで trigram が引ける)で探せるようにする。"""
    return " %s " % " ".join(_norm(s).split())


# 語は、英数字(アクセント付きのラテン文字を含む。× ÷ は除く)の連続か、仮名・漢字(長音「ー」・踊り字「々」を含む)の連続。
# 3 文字以上を索引(trigram)で引く。2 つの文字種の境目では切る(繋げて 1 語にすると、索引に無い並びは 0 件になる。DESIGN.md「語の切り方と、3 文字以上の語が無い入力」)。
_ALNUM, _KANA = r'A-Za-z0-9À-ÖØ-öø-ɏ', r'ぁ-んァ-ヶー々一-龥'
TERM = re.compile(r'[%s]{3,}|[%s]{3,}' % (_ALNUM, _KANA))
_RUN = re.compile(r'[%s]+|[%s]+' % (_ALNUM, _KANA))
# 字の間に 1 文字として載るアクセント記号。Unicode の離れたアクセントから 8 字(¨ ´ ` ˆ ˜ ¸ ˇ ˚)を選んだ(文字の集合は閉じているので
# 列挙でよい: 規約 6)。¯ ˘ ˙ ˛ ˝ は入れていない: 索引の字の間に ˘ ˙ ˛ ˝ は 0 か所、¯ は fl の合字の化け(in¯uenced)で、落とすと誤り
_LOOSE_ACCENT = re.compile(r'(?<=[^\W\d_])[\u00a8\u00b4\u0060\u02c6\u02dc\u00b8\u02c7\u02da](?=[^\W\d_])')
_CJK_GAP = re.compile(r'(?<=[%s]) +(?=[%s])' % (_KANA, _KANA))
# 著者の列に、2 文字以下の語をそのまま書いた条件(author:Li)。両端に空白を足した句に書き換える
_SHORT_AUTHOR = re.compile(r'\b(authors?|first_author):([^\s"():*]{1,2})(?=[\s)]|$)')
_QUOTED = re.compile(r'(\b[A-Za-z_]+:)?"([^"]*)"')


def _squash_quoted(q):
    """式の中の引用符つきの句を、索引と同じ形にする。索引は本文・抄録・表題の仮名・漢字の間の空白を詰めてあるので、
    空白入りの句('"山田 太郎"')はそのままだと当たらない。詰めていない列(著者・誌名・フォルダ・DOI)を指す句は触らない。
    列を指さない句は全部の列を見るので、打ったままの形と詰めた形の両方で探す。"""
    def one(m):
        col, body = m.group(1) or "", m.group(2)
        sq = _CJK_GAP.sub("", body)
        if sq == body or (col and col not in ("title:", "abstract:", "text:", "fulltext:")):
            return m.group(0)
        return '%s"%s"' % (col, sq) if col else '("%s" OR "%s")' % (body, sq)
    return _QUOTED.sub(one, q)


def _embed(texts):
    """vec.swift に 1 プロセスで流し込む。"""
    exe, src = os.path.join(CODE, "vec"), os.path.join(CODE, "vec.swift")
    if not os.path.exists(exe) or os.path.getmtime(src) > os.path.getmtime(exe):
        subprocess.run(["swiftc", "-O", "-o", exe, src], capture_output=True, timeout=300)
    if not os.path.exists(exe):
        return [None] * len(texts)
    inp = "\n".join(" ".join((t or "-").split())[:300] for t in texts)
    out = subprocess.run([exe], input=inp, capture_output=True, text=True,
                         timeout=1800).stdout.split("\n")
    vs = []
    for i in range(len(texts)):
        p = out[i].split("\t") if i < len(out) else []
        vs.append([float(x) for x in p] if len(p) > 8 else None)
    return vs


def _fts_table(c, name, cols):
    """FTS5 の表を作る。この SQLite が知らないトークナイザなら次の候補で作る(最後の候補でも駄目ならそのまま落とす)。"""
    for i, tok in enumerate(TOKENIZERS):
        try:
            c.execute("CREATE VIRTUAL TABLE %s USING fts5(%s, tokenize='%s')" %
                      (name, ", ".join(x + " UNINDEXED" if x in ("path", "year") else x for x in cols), tok))
            return
        except sqlite3.OperationalError:
            if i == len(TOKENIZERS) - 1:
                raise
            print("note: this SQLite (%s) does not support tokenizer '%s'; using '%s'"
                  % (sqlite3.sqlite_version, tok, TOKENIZERS[i + 1]), file=sys.stderr)


def build_db():
    """検索の索引を作り直す。別の名前で作り、できあがってから置き換える(途中で止まっても、前の索引が残る)。"""
    import array, glob
    recs = [json.loads(l) for l in open(INDEX) if l.strip()]
    for old in glob.glob(glob.escape(DB) + ".*.part*"):      # 前に途中で止まった作りかけ(1 時間より古いものだけ。今作っている最中かもしれない)
        try:
            if time.time() - os.path.getmtime(old) > 3600:
                os.remove(old)
        except OSError:                                      # 見ている間に、作っていた側が片付けた
            pass
    tmp = "%s.%d.part" % (DB, os.getpid())
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        c = sqlite3.connect(tmp)
        _fts_table(c, "p", COLS)
        _fts_table(c, "f", FCOLS)
        # path と year は照合に使わないのでそのまま(path は索引の記録を引く鍵)。ほかは検索語と同じ正規化をかける
        rows = [[r.get("path") or "", str(r.get("year") or ""), _norm(r.get("doi")), _pad(r.get("first_author")),
                 _norm_text(r.get("title") or (r.get("title_guess") if BOOKS else None)),      # 書籍の DB: 未同定の本はファイル名が表題
                 _pad(" ".join(r.get("authors") or [])), _norm(r.get("journal") or r.get("publisher")),
                 _norm(r.get("folder")), _norm_text(r.get("abstract")), _norm_text(r.get("text"))] for r in recs]
        full = fulltext_of(recs, _prev_fulltext)
        c.executemany("INSERT INTO p VALUES (%s)" % ",".join("?" * len(COLS)), rows)
        c.executemany("INSERT INTO f VALUES (%s)" % ",".join("?" * len(FCOLS)), [row + [t] for row, t in zip(rows, full)])
        # ベクトルはタイトルのみ。抄録を足すと平均で薄まり精度が落ちる(DESIGN.md「10.2 埋め込み」)。
        c.execute("CREATE TABLE v (path TEXT PRIMARY KEY, vec BLOB)")
        titles = [r.get("title") or (r.get("title_guess") or "")[:120] for r in recs]
        for r, v in zip(recs, _embed(titles)):
            if v:
                c.execute("INSERT OR REPLACE INTO v VALUES (?,?)",
                          (r["path"], array.array("f", v).tobytes()))
        c.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        c.execute("INSERT INTO meta VALUES ('mtime',?)", (str(os.path.getmtime(INDEX)),))
        c.execute("INSERT INTO meta VALUES ('format',?)", (DB_FORMAT,))
        c.execute("INSERT INTO meta VALUES ('fulltext',?)", (str(sum(1 for t in full if t)),))
        c.commit()
        nv = c.execute("SELECT count(*) FROM v").fetchone()[0]
        c.close()
        os.replace(tmp, DB)
    finally:
        for left in glob.glob(glob.escape(tmp) + "*"):       # 途中で止まった作りかけと、その journal
            os.remove(left)
    print("papers.db: %d 件 (ベクトル %d、全頁の本文 %d)" % (len(recs), nv, sum(1 for t in full if t)), file=sys.stderr)
    return sqlite3.connect(DB)


def _db_format_ok():
    """papers.db が今の作り方(DB_FORMAT)で作られているか。検索語は今の正規化で作るので、古い作り方の索引に当てると
    全角や合字で載っている記録を取りこぼす。無い・読めない・版の印が無いは偽。"""
    if not os.path.exists(DB):
        return False
    try:
        c = sqlite3.connect(DB)
        m = c.execute("SELECT v FROM meta WHERE k='format'").fetchone()
        c.close()
    except sqlite3.Error:
        return False
    return bool(m) and m[0] == DB_FORMAT


def _db():
    if not _db_format_ok():
        return build_db()                      # 無い、または作り方が古い
    c = sqlite3.connect(DB)
    try:
        m = c.execute("SELECT v FROM meta WHERE k='mtime'").fetchone()
    except sqlite3.Error:
        m = None
    if not m or float(m[0]) < os.path.getmtime(INDEX):
        c.close()
        return build_db()                      # 索引が更新されていれば黙って作り直す
    return c


SEARCH_COLS = ("title", "first_author", "authors", "journal", "doi", "abstract", "folder", "text", "fulltext")
_FULL_COL = re.compile(r'\bfulltext\s*:')        # 表 p には無い列。この列を名指しした式は f だけで引く
_OPS = re.compile(r'\b(AND|OR|NOT)\b|["()]|\b(author|%s):' % "|".join(SEARCH_COLS))


def _match(q):
    """検索語を FTS5 の MATCH 式にする。
    演算子(大文字の AND・OR・NOT、引用符、括弧、列名:)があれば、利用者の書いた式をそのまま使う(author: は authors:)。
    無ければ語に分けて引用し OR で繋ぐ。生の文字列は MATCH に渡せず('second-harmonic' は構文エラー)、
    FTS5 の既定は AND なので概念クエリが 0 件になるため。"""
    q = _norm(q)                               # 索引と同じ形に。Finder から貼った名前は分解形(u + ¨)で、そのままだと語が切れる
    if _OPS.search(q):
        return re.sub(r'\bauthor:', "authors:", _squash_quoted(_SHORT_AUTHOR.sub(r'\1:" \2 "', q)))
    return " OR ".join('"%s"' % t.replace('"', '') for t in TERM.findall(q.lower()))


def _short_terms(q):
    """演算子の式の中の 2 文字以下の語。trigram の索引では何にも当たらない(黙って 0 件になる)。"""
    q = _SHORT_AUTHOR.sub(r'\1:" \2 "', q)       # 著者の列の短い語は _match が句に書き換えるので引ける
    words = re.sub(r'\b(AND|OR|NOT)\b|\b\w+:|[()]', " ", re.sub(r'"[^"]*"', " ", q)).split()
    quoted = [w for w in re.findall(r'"([^"]*)"', q)]
    return [w for w in words + quoted if len(w) < 3]


def _unused_terms(q):
    """演算子の無い入力で、3 文字以上の語と一緒に書かれて使われなかった 2 文字以下の語('レーザー 発振' の 発振)。
    3 文字以上の語が 1 つも無ければ _like が入力の全部を使うので、空。"""
    if _OPS.search(q) or not _match(q):
        return []
    return [t for t in _RUN.findall(_norm(q)) if len(t) < 3]


def _like_hits(q):
    """3 文字以上の語が無い入力の、部分一致の数え方。(表題・先頭の本文で当たる語の数の式, 全頁の本文も入れた式, 式 1 つ分の引数の組)。
    % と _ は文字として探す。"""
    pats = ["%" + re.sub(r'([\\%_])', r'\\\1', t) + "%" for t in _norm(q).split()]
    one = lambda cols: " + ".join(["(" + " OR ".join("%s LIKE ? ESCAPE '\\'" % x for x in cols) + ")"] * len(pats)) or "0"
    return one(("title", "text")), one(("title", "text", "fulltext")), (lambda k: [p for p in pats for _ in range(k)])


def _like(q, full=True):
    """3 文字以上の語が無い入力(trigram が索引できない)を、表題と本文(先頭と全頁)の部分一致で探す SQL と引数。
    空白で分けた語のどれかを含む記録を返す。表題・先頭の本文で当たる記録が、含む語の多い順に先(全頁の本文を入れる前と同じ並び)。
    全頁の本文でだけ当たる記録はその後ろに、含む語の多い順。full=False なら表題・先頭の本文だけを見る。"""
    near, anywhere, args = _like_hits(q)
    if not full:
        return "SELECT path FROM f WHERE %s > 0 ORDER BY %s DESC, rowid LIMIT ?" % (near, near), args(2) * 2
    return ("SELECT path FROM f WHERE %s > 0 ORDER BY (%s) > 0 DESC, CASE WHEN (%s) > 0 THEN %s ELSE %s END DESC, rowid LIMIT ?"
            % (anywhere, near, near, near, anywhere), args(3) + args(2) * 3 + args(3))


def _visible(paths, hidden, n):
    """検索結果から hidden(dedup が移した重複 = 利用者が消す予定のもの、--kind に合わない種類)を外して n 件に。"""
    return [p for p in paths if p not in hidden][:n]


def _only_full(c, q, path):
    """この記録が、全頁の本文を入れて初めて式に当たるか(表題・著者・抄録・先頭の本文だけでは当たらない)。
    fulltext: を名指しした式は表 p で評価できないので、印を付けない(どの記録も偽)。"""
    m = _match(q)
    if not m:
        near, anywhere, args = _like_hits(q)
        r = c.execute("SELECT (%s) > 0, (%s) > 0 FROM f WHERE path=?" % (near, anywhere), args(2) + args(3) + [path]).fetchone()
        return bool(r) and not r[0] and bool(r[1])
    if _FULL_COL.search(m):
        return False
    return (not c.execute("SELECT 1 FROM p WHERE p MATCH ? AND path=?", (m, path)).fetchone()
            and bool(c.execute("SELECT 1 FROM f WHERE f MATCH ? AND path=?", (m, path)).fetchone()))


def _fts(c, q, n, full=True):
    """順位つきの検索。表 p(表題・著者・抄録・先頭の本文など)で当たる記録を p の順位で先に並べ、n 件に届かなければ、
    全頁の本文を入れた表 f でだけ当たる記録を後ろに足す。full=False なら全頁の本文を見ない(hybrid。入れる前と同じ並び。
    fulltext: を名指しした式は 0 件になる)。"""
    m = _match(q)
    if not m:                                  # 2文字以下(trigram が索引できない)
        sql, args = _like(q, full)
        return [r[0] for r in c.execute(sql, args + [n]).fetchall()]
    ranked = lambda t, w, k: [r[0] for r in c.execute("SELECT path FROM %s WHERE %s MATCH ? ORDER BY bm25(%s,%s) LIMIT ?"
                                                    % (t, t, t, ",".join(str(x) for x in w)), (m, k))]   # bm25 は昇順(小さいほど良い)
    first = [] if _FULL_COL.search(m) else ranked("p", W, n)
    if len(first) >= n or not full:
        return first
    # p で当たる記録は first に全部ある(n 件に届いていない)。全頁の本文でだけ当たる記録を、その後ろに並べる
    seen = set(first)
    return first + [p for p in ranked("f", WF, n + len(first)) if p not in seen][:n - len(first)]


def _vec(c, q, n):
    import array, math
    qv = _embed([q])[0]
    if not qv:
        return []
    qn = math.sqrt(sum(x * x for x in qv)) or 1
    out = []
    for path, blob in c.execute("SELECT path, vec FROM v"):
        v = array.array("f"); v.frombytes(blob)
        d = sum(a * b for a, b in zip(qv, v))
        vn = math.sqrt(sum(b * b for b in v)) or 1
        out.append((d / (qn * vn), path))
    out.sort(reverse=True)
    return [p for _, p in out[:n]]


def _full_entry(i, r):
    """--full の 1 件分。通常表示は 1 行に収めるため著者・誌名・タイトルを切るが、ここは切らない。"""
    ref = " ".join(str(v) for v in (r.get("journal"), r.get("volume"),
                                     r.get("pages") or r.get("article_number")) if v) \
        or " ".join(str(v) for v in (r.get("publisher"), "ISBN " + r["isbn"] if r.get("isbn") else None) if v)
    head = "%2d  %s  %s  %s" % (i, r.get("year") or "-", r.get("first_author") or "-", ref or "-")
    title = r.get("title") or "(%s) %s" % (r.get("rung"), r.get("filename"))
    return "\n".join([head, "    " + title, "    DOI: %s" % (r.get("doi") or "-"),
                      "    " + r["path"]] + (["    Kind: %s" % r["kind"]] if r.get("kind") else []) +
                     (["    Published: %s%s" % (r["published_doi"], " (%s)" % r["journal_ref"] if r.get("journal_ref") else "")]
                      if r.get("published_doi") else []) +
                     ["    Abstract: %s" % (r.get("abstract") or "-"), ""])


def _listing(i, row, p):
    """検索結果の 1 行。row は検索の索引の (年, 第一著者, 誌名, 表題)。第一著者の欄は両端に空白を足してあるので取る。"""
    return "%2d  %-4s %-16s %-26s %s" % (i, row[0] or "-", ((row[1] or "").strip() or "-")[:16],
                                         (row[2] or "-")[:26], (row[3] or os.path.basename(p))[:60])


def search():
    """papers search <語> [--n=10] [--kind=paper|book|other] [--full | --json [--with-text]] [--all]
    語は OR・部分一致。AND・OR・NOT(大文字)、"語句"、括弧、列名:(title・author・first_author・journal・doi・abstract・folder・text・fulltext)
    を書けば、その式で絞る。--all で dedup が移した重複も出す。
    - 表題・著者・誌名・抄録・本文の先頭(text)で当たる記録が先に並び、全頁の本文(fulltext)を入れて初めて当たる記録はその後ろに並ぶ
      (fulltext: を名指しした式は、全部が全頁の本文を入れた順位で並び、境目は出ない)
    - --kind で種類を絞る: paper(論文)・book(本・ハンドブック)・other(学位論文・講義ノート・資料・特許)。コンマで複数。既定は全部
    - 記号でつながる語は引用符で囲む(そのままだと記号で切れる): papers search '"X-ray"'   '"FT-IR" OR "FTIR"'
    - 2 文字以下の語を語として引くには、前後の空白や記号ごと囲む: papers search '" Si "'   '" Si-"'   '"(Si)"'
      (囲まない Si は basis のような語の一部にも当たる)
    - 2 文字以下の姓は著者の列で引ける: papers search 'author:Li'   'first_author:Wu AND title:laser'"""
    q = _norm(" ".join(a for a in sys.argv[2:] if not a.startswith("--")))   # 注意の判定も、式を作るのと同じ形で見る(全角のコロンなど)
    n = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--n=")), "10"))
    mode = next((a.split("=")[1] for a in sys.argv if a.startswith("--mode=")), "fts")
    if not q:
        sys.exit(search.__doc__)
    if _OPS.search(q) and _short_terms(q):
        print("note: terms shorter than 3 characters cannot be matched by the index, so their conditions match nothing: %s"
              % ", ".join(_short_terms(q)), file=sys.stderr)
    if _unused_terms(q):
        print("note: terms shorter than 3 characters were not used: %s" % ", ".join(_unused_terms(q)), file=sys.stderr)
    kinds = next((a.split("=")[1].split(",") for a in sys.argv if a.startswith("--kind=")), None)
    if kinds and not set(kinds) <= set(KIND_GROUPS):
        sys.exit("unknown --kind value: %s (use %s; separate several with commas)"
                 % (", ".join(sorted(set(kinds) - set(KIND_GROUPS))), ", ".join(KIND_GROUPS)))
    # 結果から外す記録: dedup が移した重複(--all なら外さない)と、--kind に合わない種類
    hidden = {r["path"] for r in map(json.loads, open(INDEX))
              if (moved_aside(r) and "--all" not in sys.argv) or (kinds and kind_group(r) not in kinds)}
    k = n + len(hidden)                                        # 外す分を見込んで多めに引く
    c = _db()
    try:
        if mode == "vec":
            paths = _vec(c, q, k)
        elif mode == "hybrid":
            rank = collections.defaultdict(float)
            for lst in (_fts(c, q, k * 3, full=False), _vec(c, q, k * 3)):
                for i, p in enumerate(lst):
                    rank[p] += 1.0 / (60 + i)      # RRF
            paths = [p for p, _ in sorted(rank.items(), key=lambda kv: -kv[1])[:k]]
        else:
            paths = _fts(c, q, k)
    except sqlite3.OperationalError as e:
        sys.exit("invalid search expression (%s).\n"
                 "- write AND, OR, NOT in capitals; NOT needs a term before it (laser NOT review)\n"
                 "- quote terms that contain hyphens or symbols (\"second-harmonic\")\n"
                 "- columns: title, author, first_author, journal, doi, abstract, folder, text, fulltext" % e)
    paths = _visible(paths, hidden, n)
    if "--json" in sys.argv or "--full" in sys.argv:
        idx = {r["path"]: r for r in map(json.loads, open(INDEX)) if r.get("path")}
        for i, p in enumerate((p for p in paths if p in idx), 1):
            only = mode == "fts" and _only_full(c, q, p)
            if "--json" in sys.argv:
                print(json.dumps(dict(compact(idx[p], "--with-text" in sys.argv), **({"matched": "fulltext"} if only else {})),
                                 ensure_ascii=False))
            else:
                print(_full_entry(i, idx[p]) + ("    (matches only when the full text is searched)\n" if only else ""))
        return
    m = _match(q)
    marked = False
    for i, p in enumerate(paths, 1):
        row = c.execute("SELECT year, first_author, journal, title FROM p WHERE path=?",
                        (p,)).fetchone() or ("", "", "", "")
        only = mode == "fts" and _only_full(c, q, p)
        sn = ""
        if m:
            t, cols = ("f", FCOLS) if only or _FULL_COL.search(m) else ("p", COLS)      # 抜粋は、当たった側の本文から
            r = c.execute("SELECT snippet(%s,%d,'[',']','…',10) FROM %s WHERE %s MATCH ? AND path=?"
                          % (t, len(cols) - 1, t, t), (m, p)).fetchone()
            sn = (r[0] if r else "").replace("\n", " ").replace("\f", " ")[:110]
        if only and not marked:                    # ここから後ろは全部そう(_fts・_like の並べ方)
            print("    -- the records below match only when the full text is searched "
                  "(not by title, authors, abstract and the first 3,000 characters alone) --")
            marked = True
        print(_listing(i, row, p))
        print("    %s" % p)
        if sn:
            print("    … %s" % sn)



def selftest():
    """ネットワーク不要の自己検査。`python3 organize.py selftest`

    corroborate() は一度コミットから丸ごと抜け落ちたことがある(呼び出し4箇所が残り、
    scan 実行時に NameError)。構文検査も search も通ってしまい気づけなかったので、
    落ちたら壊れる最小の検査をここに置く。

    ただし未定義名の検出は pyflakes の仕事。ここは同定まわりのロジックだけを見る。"""
    head = "OPTICS LETTERS / Vol. 31, No. 4 / February 15, 2006  Taro Yamada"
    real = {"first_author": "Yamada", "year": 2006, "journal": "Optics Letters",
            "volume": "31", "pages": "1234"}
    other = {"first_author": "Suzuki", "year": 2021,
             "journal": "Handbook of Example Lasers and Related Phenomena"}
    assert corroborate(real, head), "自誌のレコードが裏取りで落ちている"
    assert not corroborate(other, head), "同一タイトルの別版が通っている"
    assert not corroborate(dict(real, first_author="Suzuki"), head), "著者が違うのに、年と誌名が合うだけで通している"
    assert not corroborate(real, ""), "本文が空なら通してはいけない"

    assert DOI_RE.search("see 10.1016/0000-0000(80)90000-3 x").group(0) == \
        "10.1016/0000-0000(80)90000-3", "旧Elsevier DOI の括弧が切れている"
    assert not ARX_TEXT.search("pages 2609.22016"), "arXiv: 接頭辞なしで誤検出している"

    assert safe("A/B: C.") == "A-B- C", safe("A/B: C.")
    assert tight("Opt. Commun.") == "OptCommun"
    assert tight("The Journal of Chemical Physics") == "JournalofChemicalPhysics"
    r = {"year": 1999, "first_author": "Tanaka", "journal_short": "Opt. Lett.",
         "journal": "Optics Letters", "volume": "24", "issue": "7", "pages": "512-514"}
    assert filename_for(r) == "1999_Tanaka_OptLett_24_7_512", filename_for(r)
    assert filename_for({"year": 2020, "first_author": "Sato", "journal": "arXiv",
                         "journal_short": "arXiv", "arxiv_id": "2001.01234"}) == \
        "2020_Sato_arXiv_2001.01234"

    assert _match("second-harmonic generation") == '"second" OR "harmonic" OR "generation"'
    assert _match("整合") == "", "2文字は trigram に渡してはいけない"

    assert why({"rung": "error"}).startswith("E_"), "error が別バケットに落ちている"
    assert why({"rung": "unverified"}).startswith("D_")
    assert why({"rung": "unresolved", "no_text": True}).startswith("A_")

    # --- 照合用の正規化。PDF は分解形・合字で持ち、Crossref は合成形で返す ---
    assert norm("Nowa\u0144ski") == norm("Nowan\u0301ski") == "nowanski", "分解形と合成形が割れる"
    assert norm("Tracer di\ufb00usion") == "tracerdiffusion", "合字が展開されない"
    assert norm("Opt. Lett. 31, 1234") == "optlett311234", "ASCII の結果が変わった"
    assert corroborate({"first_author": "Nowa\u0144ski", "year": 2018},
                       "OPTICS EXPRESS 12345 J AN N OWA N\u0301SKI 2018 Nowan\u0301ski"), \
        "分解形のヘッダで著者の裏が取れない"

    # --- ダウンロード表紙。実物の書き出しから ---
    for t in ("See discussions, stats, and author profiles for this publication at: https://www.researchgate.net/",
              "Home Search Collections Journals About Contact us My IOPscience Large Example",
              "PROCEEDINGS OF SPIE SPIEDigitalLibrary.org/conference-proceedings-of-spie What is",
              "This article was downloaded by: [Example University] On: 01 January 2010",
              "Example Title 229 STOR Science Your use of the JSTOR archive indicates your",
              "Example pulse trains Published in: Physical Review Letters Citation for published version (APA):"):
        assert is_cover(t), "表紙を見逃している: " + t[:40]
    for t in ("This is an open access article published under an ACS AuthorChoice License Article pubs.acs.org",
              "View Article Online / Journal Homepage / Table of Contents for this issue PAPER www.rsc.org/pccp",
              "Downloaded from http://rsta.royalsocietypublishing.org/ on January 1, 2010 Phil. Trans. R. Soc. A"):
        assert not is_cover(t), "論文の 1 ページ目を表紙とみなしている: " + t[:40]
    # IOP の表紙は他の論文の著者を並べるので、裏取りには使わない(表紙に並ぶ別の著者に着く)
    assert cover_kind("Home Search Collections Journals About Contact us My IOPscience x") == "others", \
        "IOP の表紙を裏取りに使ってしまう"
    assert cover_kind("PROCEEDINGS OF SPIE SPIEDigitalLibrary.org/conference x") == "self"

    # --- Crossref の HTML。実物の値から ---
    assert clean_text("Laser &amp;amp; Photonics Reviews") == "Laser & Photonics Reviews", "二重エスケープが残る"
    assert clean_text("&lt;title&gt;Origin of defects in example nonlinear crystals&lt;/title&gt;") == \
        "Origin of defects in example nonlinear crystals", "エスケープされたタグが残る"
    assert clean_text("The two-level model =Si&lt;O_2:∙Xx absorption center") == \
        "The two-level model =Si<O_2:∙Xx absorption center", "本物の不等号を消している"
    # 抄録の本物の不等号。広い <[^>]+> だと < から > までの本文が消える
    ineq = ("seen in the short-wavelength (&lt;500 nm) region, while such an effect is "
            "still absent in the long-wavelength (&gt;500 nm) region")
    assert clean_text(ineq) == html.unescape(ineq), "不等号にはさまれた本文を消している"
    mml = ('Strain effects on the<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML">'
           '<mml:msub><mml:mi>E</mml:mi><mml:mn>11</mml:mn></mml:msub></mml:math>and E22 optical transitions')
    assert clean_text(mml) == "Strain effects on the E11 and E22 optical transitions", clean_text(mml)
    assert _title_in_text(mml, norm("Strain effects on the E11 and E22 optical transitions in "
                                    "example low-dimensional semiconductors")), \
        "MathML 入りのタイトルが本文と一致しない(段6 が取りこぼす)"

    # --- 引用文の中にしか現れないタイトルは証拠にしない。実物の文面から ---
    cited = ("letters to nature 24. Adams, B. C. et al. Ripples in a cold atomic gas. "
             "Phys. Rev. Lett. 83, 2498 (1999). 25. Baker, D. E., Adams, B. C., Clark, F. G. "
             "Dynamics of mode separation in a binary mixture of cold atomic gases. "
             "Phys. Rev. Lett. 81, 1539–1542 (1998). Acknowledgements We thank H. I. Jones")
    assert title_only_in_citation("Dynamics of Mode Separation in a Binary Mixture of "
                                  "Cold Atomic Gases", cited), "参考文献中のタイトルを証拠にしている"
    unnum = ('"Stark effect and intensity anomalies in Xei," J. Opt. Soc. Am. 58, 930-836 (1968). '
             "5 A. Example and B. V. Sample")
    assert title_only_in_citation("Stark Effect and Intensity Anomalies in XeI*", unnum), \
        "番号の無い形の引用を見逃している"
    prl = ("PRL 110, 123456 (2013) PHYSICAL REVIEW LETTERS week ending 14 JUNE 2013 Large Optical "
           "Enhancement by Resonant Transmission in Layered Example Media Alice B. Author, "
           "C. D. Writer, Eve F. Scholar (Received 9 October 2012; published 13 June 2013)")
    assert not title_only_in_citation("Large Optical Enhancement by Resonant Transmission "
                                      "in Layered Example Media", prl), "論文自身の見出しを引用とみなしている"
    # 段6・段4 がこの判定を実際に呼んでいるか。Crossref の応答を差し替え、通信せずに確かめる
    ctitle = "Dynamics of Mode Separation in a Binary Mixture of Cold Atomic Gases"
    saved = get_json, from_crossref
    try:
        globals()["get_json"] = lambda url: {"message": {"items": [{"DOI": "10.1103/x", "title": [ctitle]}]}}
        globals()["from_crossref"] = lambda doi: {"doi": doi, "title": ctitle}
        assert by_bibliographic(cited) is None, "段6 が引用の中のタイトルに着く(判定を呼んでいない)"
        assert by_title(ctitle, cited) is None, "段4 が引用の中のタイトルに着く(判定を呼んでいない)"
        assert by_title(ctitle, "") is not None, "段4 の差し替えが効いていない(検査が何も見ていない)"
        # 段4 の一致率が引数の順で変わらないか。この組は一方の順で 0.927、逆で 0.78(0.92 をまたぐ)
        pa, pb = "Photon Correlations in Germanium and Germanene", "Photon Correlations in and Germanium Germanene"
        globals()["get_json"] = lambda url: {"message": {"items": [{"DOI": "10.1103/x", "title": [pb]}]}}
        assert by_title(pa, "") is not None, "段4 の一致率が引数の順で変わる(両方の順の大きい方をとっていない)"
        globals()["get_json"] = lambda url: {"message": {"items": [{"DOI": "10.1103/x", "title": [pa]}]}}
        assert by_title(pb, "") is not None, "段4 の一致率が引数の順で変わる(逆の順だけをとっている)"
    finally:
        globals()["get_json"], globals()["from_crossref"] = saved

    # --- enrich の失敗の数え方。404 は再実行しても変わらないので「取れなかった」に入れない ---
    class _E(Exception):
        def __init__(self, code): self.code = code
    assert _fetch_outcome(_E(404)) == "absent", "404 を「再実行で取り直せる」に数えている"
    assert _fetch_outcome(_E(503)) == "failed", "一時的な失敗を「無い」に数えている"
    assert _fetch_outcome(OSError("timed out")) == "failed", "接続断を「無い」に数えている"
    assert _fetch_outcome(_E(400)) == "absent", "400(待っても変わらない)を「再実行で取り直せる」に数えている"
    assert _fetch_outcome(_E(429)) == "failed", "429(送りすぎ。待てば直る)を「無い」に数えている"
    assert all(_retry_plan(c, 0)[0] == (_fetch_outcome(_E(c)) == "failed") for c in (400, 401, 403, 404, 410, 429, 500, 503)), \
        "再送するかどうかと、取り直せるかどうかの分け方が食い違う"

    # --- search --full は切らずに出す ---
    long_t = "Phase-stable, few-cycle example pulses tunable from the visible to the near infrared region"
    e = _full_entry(1, {"year": 2009, "first_author": "Author", "journal": "Journal of the Optical "
                        "Society of America B", "volume": "25", "pages": "B62", "title": long_t,
                        "doi": "10.1234/example.25.000b62", "path": "/x/a.pdf", "abstract": "A" * 500})
    assert long_t in e and "Journal of the Optical Society of America B 25 B62" in e, "--full が切っている"
    assert "Abstract: " + "A" * 500 in e, "--full が抄録を切っている"

    # --- プレプリント。arXiv の刻印は版・分野・日付を持つ。参考文献中の引用は持たない ---
    assert ARX_STAMP.search("arXiv:1234.5678v1 [physics.optics] 23 May 2014").group(1) == "1234.5678"
    assert ARX_STAMP.search("arXiv:cond-mat/0001234v2 [cond-mat.mtrl-sci] 4 Jun 2004").group(1) == "cond-mat/0001234"
    for t in ("[12] A. B. Smith, arXiv:1512.01234 [quant-ph] (2015).", "see arXiv:1234.5678v2 for details"):
        assert not ARX_STAMP.search(t), "参考文献中の arXiv の引用を刻印とみなしている: " + t
    xml = ('<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>'
           '<title>Example coupling in a model system</title><summary>s</summary><published>2016-07-06T00:00:00Z</published>'
           '<author><name>Ana García López</name></author><arxiv:doi>10.1234/ExampleB.95.155402</arxiv:doi>'
           '<arxiv:journal_ref>Phys. Rev. B 95, 155402 (2017)</arxiv:journal_ref></entry></feed>')
    ar = _arxiv_record(xml, "1601.01234")
    assert ar["published_doi"] == "10.1234/exampleb.95.155402" and ar["doi"] is None, \
        "出版版の DOI を関係として持っていない(または同一性の DOI にしている)"
    pre = dict(ar, rung="arxiv_text", source="arxiv", year=2016, path="/p.pdf")
    pubr = {"rung": "text_doi", "source": "crossref", "year": 2017, "first_author": "García López",
            "doi": "10.1234/ExampleB.95.155402", "path": "/q.pdf"}
    assert len(preprint_pairs([pre, pubr])) == 1, "複合姓のプレプリントと出版版を組にしない"
    assert not preprint_pairs([dict(pre, rung="unverified"), pubr]), "未確定の記録を組にしている"
    assert not preprint_pairs([pre, dict(pubr, first_author="Suzuki")]), "著者の違う論文を組にしている"
    assert dedup_key(pre) == "1601.01234", "プレプリントの同一性のキーが arXiv の ID でない"

    # --- ページの検証。Haiku は拒否権だけを持つ ---
    base = {"rung": "text_doi", "source": "crossref", "year": 2004, "first_author": "Brown",
            "title": "Cold gas beyond the visible edge of a model spiral galaxy"}
    other_paper = dict(base, page_title="Electrical excitation and damping of vibrations in example nanowires",
                 page_title_literal=True)
    assert vet_verdict(other_paper) == "hold", "ページの表題と記録が食い違うのに人に回さない(別の論文のページを含む PDF の型)"
    assert vet_verdict(dict(other_paper, page_title_literal=False)) is None, "字面で確かめていない表題で人に回している"
    assert vet_verdict(dict(other_paper, page_title=base["title"].upper())) is None, "一致しているのに人に回している"
    assert vet_verdict(dict(other_paper, rung="manual")) is None, "人が決めた手入力を覆している"
    assert vet_verdict(dict(other_paper, vet_cleared="user")) is None, "人が確かめて戻した記録を再び止めている"
    assert vet_verdict(dict(other_paper, rung="unresolved", source=None)) is None, "未解決を判定している"
    assert vet_verdict(dict(other_paper, page_title="酸化物・窒化物半導体における点欠陥")) is None, "和文の表題で判定している"
    assert why(dict(other_paper, rung="unverified", held_by="vet")).startswith("G_")

    # --- dedup が移した記録は、もう組にしない ---
    pre = {"rung": "arxiv_text", "source": "arxiv", "arxiv_id": "1", "published_doi": "10.1/p", "first_author": "Kim", "year": 2020}
    pubr = {"rung": "text_doi", "source": "crossref", "doi": "10.1/p", "first_author": "Kim", "year": 2021}
    assert len(preprint_pairs([pre, pubr])) == 1
    assert not preprint_pairs([dict(pre, published_copy="/x.pdf"), pubr]), "移したプレプリントをまた組にしている"
    assert not preprint_pairs([pre, dict(pubr, duplicate_of="/y.pdf")]), "移した重複を出版版として組にしている"

    # --- OpenAlex のキーは api.openalex.org にだけ、ヘッダで送る ---
    saved = dict(_KEY); _KEY["openalex"] = "k123"
    assert _auth_headers("https://api.openalex.org/works?search=x") == {"Authorization": "Bearer k123"}
    for u in ("https://api.crossref.org/works", "http://export.arxiv.org/api/query", "https://api.openalex.org.evil.example/",
              "http://127.0.0.1:8000/"):
        assert _auth_headers(u) == {}, "OpenAlex のキーを別の送り先に送っている: " + u
    _KEY.clear(); _KEY.update(saved)

    # --- review の候補。Haiku が読んだ表題と第一著者で照合し、人が y / n で決める ---
    hr = {"rung": "unresolved", "page_title": "Thermal squeezed states", "page_author": "Mayer", "page_title_literal": True}
    hit = {"DOI": "10.1016/0000-0000(89)90000-X", "type": "journal-article", "author": [{"family": "Mayer"}],
           "title": ["Thermal squeezed states"]}
    assert review_accept(hr, hit), "正しい候補を出さない"
    assert not review_accept(hr, dict(hit, author=[])), "著者の無い候補を出している"
    assert not review_accept(hr, dict(hit, author=[{"family": "Rosen"}])), "第一著者の違う候補を出している"
    assert not review_accept(dict(hr, cand_rejected=[hit["DOI"].lower()]), hit), "人が違うとした候補をまた出している"
    assert not review_accept(hr, dict(hit, title=["Thermal states of light"])), "表題の違う候補を出している"

    # --- ファイル名の略誌名。長い誌名だけを短くする ---
    jt = lambda j, js=None: journal_tag({"journal": j, "journal_short": js})
    assert jt("Optics Letters", "Opt. Lett.") == "OptLett", "短い略誌名を変えている"
    assert jt("Applied Physics B: Lasers and Optics and Other Things") == "AppliedPhysicsB"
    assert jt("2017 Conference on Lasers and Electro-Optics Pacific Rim (CLEO-PR)") == "CLEO-PR"
    assert jt("Nuclear Instruments and Methods in Physics Research Section A: Accelerators, Spectrometers") == "NIMPRSA"
    assert jt("High-Power, High-Energy, and High-Intensity Laser Technology and Applications Volume XXIV") .endswith("XXIV")
    assert all(len(jt(j)) <= JOURNAL_MAX for j in ("A " * 60, "a" * 90, "Proceedings of the 40th ACM SIGPLAN Conference on X"))

    assert [start_page(x) for x in ("87-96", "342", "C4-35-C4-55", "3 pp.", "A954", "", None)] == \
        ["87", "342", "C4-35", None, "A954", None, None], "開始頁の取り方が違う"
    assert filename_for({"year": 2006, "first_author": "Berger *", "journal_short": "J. Mod. Opt.", "pages": "87-96"}) \
        == "2006_Berger_JModOpt_87", "著者名の記号が名前に残る"

    # --- ファイルを動かしたら、そのパスを指す項目も ---
    pr = [{"path": "/new.pdf"}, {"path": "/_dup/x.pdf", "duplicate_of": "/old.pdf"}, {"path": "/p", "published_copy": "/old.pdf"}]
    assert repoint(pr, {"/old.pdf": "/new.pdf"}) == 2 and pr[1]["duplicate_of"] == pr[2]["published_copy"] == "/new.pdf", \
        "改名したファイルを指す項目が古いパスのまま"

    # --- views: Finder 用のリンクの切り口 ---
    vr = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "Miller", "journal": "ACS Nano",
          "subject": ["Example research"], "filename": "a.pdf"}
    vl = view_links([dict(vr, path="/x/a.pdf"), dict(vr, path="/y/a.pdf"), dict(vr, path="/d/a.pdf", duplicate_of="/x/a.pdf"),
                     {"path": "/u.pdf", "rung": "unresolved", "filename": "u.pdf"}])
    assert (os.path.join("誌名", "ACS Nano", "a.pdf"), "/x/a.pdf") in vl and (os.path.join("誌名", "ACS Nano", "a_2.pdf"), "/y/a.pdf") in vl, \
        "同じ名前のリンクがぶつかる"
    assert {t for _, t in vl} == {"/x/a.pdf", "/y/a.pdf"}, "移した重複や未同定をビューに入れている"
    assert {rel.split(os.sep)[0] for rel, _ in vl} == {"誌名", "年", "分野", "第一著者"}

    # --- relocate: 手で動かしたファイルを、名前と本文で記録に対応付け直す ---
    body = "Theory of Example Light Scattering " * 20
    T = {"/n/a.pdf": body, "/n/x/b.pdf": "other paper " * 50, "/n/renamed.pdf": "Example measured quantity " * 30,
         "/n/s1/si.pdf": "si one " * 80, "/n/s2/si.pdf": "si two " * 80, "/n/scan.pdf": ""}
    M = [{"path": "/o/a.pdf", "filename": "a.pdf", "text": body},
         {"path": "/o/k.pdf", "filename": "k.pdf", "text": "Example measured quantity " * 30},
         {"path": "/o/si.pdf", "filename": "si.pdf", "text": "si two " * 80},
         {"path": "/o/scan.pdf", "filename": "scan.pdf", "text": ""},
         {"path": "/o/gone.pdf", "filename": "gone.pdf", "text": "gone " * 100},
         {"path": "/o/b.pdf", "filename": "b.pdf", "text": "not the same file " * 40}]
    kept = {"path": "/pub.pdf", "doi": "10.1/p"}
    carry_preprint_ids([kept], [{"path": "/_pre/x.pdf", "arxiv_id": "1501.01234", "published_copy": "/pub.pdf"}])
    assert kept.get("preprint_arxiv_id") == "1501.01234", "消えたプレプリントの arXiv ID を出版版に移していない"
    hk = has_lookup([dict(kept, rung="text_doi", source="crossref", year=2016, first_author="H")], ["arXiv:1501.01234"])[0]
    assert (hk["status"], hk["path"]) == ("有り(出版版)", "/pub.pdf"), hk
    got, amb, lost = relocate_plan(M, sorted(T), T.get)
    g = {r["filename"]: (p, how) for r, p, how in got}
    assert g == {"a.pdf": ("/n/a.pdf", "名前と本文"), "k.pdf": ("/n/renamed.pdf", "本文(名前も変わった)"),
                 "si.pdf": ("/n/s2/si.pdf", "名前と本文"), "scan.pdf": ("/n/scan.pdf", "名前だけ(本文が無い・スキャン)")}, g
    assert [r["filename"] for r, _ in amb] == ["b.pdf"], "名前は同じだが本文の違うファイルを当てている"
    assert [r["filename"] for r in lost] == ["gone.pdf"]
    two = [{"path": "/o/1/a.pdf", "filename": "a.pdf", "text": body}, {"path": "/o/2/a.pdf", "filename": "a.pdf", "text": body}]
    g2, a2, _ = relocate_plan(two, ["/n/a.pdf"], T.get)
    assert g2 == [] and len(a2) == 2, "1 つのファイルを 2 つの記録に当てている(当てずに、決まらないに出す)"

    # --- 検索式: 演算子が無ければ OR、あれば利用者の式。移した重複は既定で外す ---
    assert _match("second-harmonic generation") == '"second" OR "harmonic" OR "generation"'
    assert _match("laser and fiber").count(" OR ") == 2, "小文字の and を演算子として扱っている"
    assert _match("title:laser AND fiber NOT review") == "title:laser AND fiber NOT review"
    assert _match('author:Smith "second harmonic"') == 'authors:Smith "second harmonic"'
    assert _short_terms("title:laser AND CW") == ["CW"] and _short_terms('"CEP" OR title:laser') == []
    # 語の切り方: 長音・踊り字・アクセント付きのラテン文字は語の中。文字種の境目と記号では切る
    assert _match("レーザー 発振") == '"レーザー"' and _match("コヒーレント") == '"コヒーレント"', "長音「ー」で語を切っている"
    assert _match("様々な") == '"様々な"', "踊り字「々」で語を切っている"
    assert _match("Müller Schrödinger") == '"müller" OR "schrödinger"', "アクセント付きの文字で語を切っている"
    assert _match("Mu\u0308ller") == '"müller"' and _match("author:Mu\u0308ller") == "authors:Müller", \
        "分解形(u + ¨)の入力を合成形にしていない"
    assert _match("3×3×3") == "", "× を文字として数えている"
    assert _match("ＳＨＧ ﬁber ｺﾋｰﾚﾝﾄ") == '"shg" OR "fiber" OR "コヒーレント"', "全角・合字・半角の片仮名を索引と同じ形にしていない"
    assert _match("SHG素子") == '"shg"' and _match("X-ray") == '"ray"', "文字種の境目・記号で切っていない"
    assert _unused_terms("レーザー 発振") == ["発振"] and _unused_terms("SHG素子") == ["素子"] and _unused_terms("X-ray") == ["X"]
    assert _unused_terms("レーザー カ\u3099ス") == ["ガス"], "分解形の入力を合成形にしていない"
    assert _unused_terms("Si Ge") == [] and _unused_terms("title:laser AND CW") == [], "LIKE の経路・演算子の式で注意を出している"
    # 3 文字以上の語が無い入力: % と _ は文字として、語ごとに探し、含む語の多い順
    lk = sqlite3.connect(":memory:")
    lk.execute("CREATE TABLE f (path, title, text, fulltext)")
    lk.executemany("INSERT INTO f VALUES (?,?,?,?)", [("a", "T_c of films", "", ""), ("b", "", "Tc and T1c", ""), ("c", "", "50% yield", ""),
                                                      ("d", "", "500 samples", ""), ("e", "", "制御のみ", ""), ("f", "発振", "発振と制御", ""),
                                                      ("g", "", "ガス", ""), ("h", "Si", "", ""), ("i", "", "", "奥の頁に整合"),
                                                      ("j", "整合の条件", "", "")])
    like = lambda q: [r[0] for r in lk.execute(_like(q)[0], _like(q)[1] + [10])]
    assert like("T_c") == ["a"] and like("50%") == ["c"], "LIKE の % と _ がワイルドカードのまま"
    assert like("発振 制御") == ["f", "e"], "語ごとに分けていない、または含む語の多い順でない"
    assert like("制御") == ["e", "f"] and like("%") == ["c"]
    assert like("カ\u3099ス") == ["g"] and like("Ｓｉ") == ["h"], "分解形・全角の入力を索引と同じ形にしていない"
    assert like("整合") == ["j", "i"], "3 文字以上の語が無い入力が全頁の本文を見ていない、または表題・先頭で当たる記録を先に並べていない"
    assert like("整合 奥") == ["j", "i"], "全頁の本文でだけ当たる記録を、含む語が多いからと、表題で当たる記録より前に並べた"
    assert _match('fulltext:"第二 高調波"') == 'fulltext:"第二高調波"', "fulltext: の句の、仮名・漢字の間の空白を詰めていない"
    # 全頁の本文: 頁の分け方、テキスト層が乏しいかの判定、検索の種類の分け方
    assert _split_pages("a  b\n c\f\fd\f") == ["a b c", "", "d"] and _split_pages("") == [] and _split_pages("x") == ["x"], \
        "改頁で頁に分けていない・最後の空の片を頁に数えている・途中の空の頁を落としている"
    assert _thin(["", "x" * (THIN_CHARS - 1)]) and not _thin(["", "x" * THIN_CHARS * 2]) and not _thin([]), \
        "テキスト層が乏しいかを、頁あたりの平均で見ていない"
    assert [kind_group(r) for r in ({}, {"kind": "paper"}, {"not_paper": "user", "kind": "book"}, {"not_paper": "user", "kind": "thesis"},
                                    {"not_paper": "user"})] == ["paper", "paper", "book", "other", "other"], "検索の種類の分け方"
    assert _FULL_COL.search(_match("fulltext:laser AND title:fiber")) and not _FULL_COL.search(_match("text:laser")), \
        "fulltext: を名指しした式を見分けていない"
    # 索引に入れる本文: 仮名・漢字の間の空白(行の折り返しの跡)を詰める。英字の間・英字と和文の間は詰めない。検索語にはかけない
    assert _norm_text("スペク トル解析 と 干渉  測定") == "スペクトル解析と干渉測定", "仮名・漢字の間の空白を詰めていない"
    assert _norm_text("\uff7d\uff8d\uff9f\uff78 \uff84\uff99") == "スペクトル", "空白を詰めるのが NFKC より先になっている(半角の片仮名の間が詰まらない)"
    assert _norm_text("fiber laser と SHG 素子") == "fiber laser と SHG 素子", "英字の間・英字と和文の間の空白まで詰めている"
    assert _norm("レーザー 発振") == "レーザー 発振", "検索語の空白(語の区切り)まで詰めている"
    # 字の間に離れて載るアクセント記号は落とす(索引と検索語の両方)。字の間でなければ触らない
    assert _norm("Garci\u00b4a M\u00a8uller fa\u02c6t") == "Garcia Muller fat", "字の間のアクセント記号を落としていない"
    assert _norm("a\u02dcb a\u00b8b a\u02c7b a\u02dab a\u0060b") == "ab ab ab ab ab", "8 字のどれかを落としていない"
    assert _norm("a \u0060b\u0060 c") == "a \u0060b\u0060 c" and _match("Pe\u00b4rez") == '"perez"', "字の間でない記号まで落としている"
    # 著者の列の 2 文字以下の語は、両端に空白を足した句にする(索引の側も両端に空白を足す)。3 文字以上は部分一致のまま
    assert _pad("Li  Wei") == " Li Wei " and _pad(None) == "  "
    assert _listing(1, ("2020", _pad("Li"), "Optica", "T"), "/f/a.pdf").split()[:3] == ["1", "2020", "Li"] \
        and _listing(1, ("", _pad(None), "", ""), "/f/a.pdf").split() == ["1", "-", "-", "-", "a.pdf"], "検索結果の行に、著者の欄に足した空白が出ている"
    assert _match("author:Li") == 'authors:" Li "' and _match("first_author:Wu AND title:laser") == 'first_author:" Wu " AND title:laser'
    assert _match("(author:Li OR authors:Xu) NOT review") == '(authors:" Li " OR authors:" Xu ") NOT review'
    assert _match("author:Lin") == "authors:Lin" and _match('author:"Li"') == 'authors:"Li"', "3 文字以上・引用符つきの著者まで書き換えている"
    assert _match("author:Li*") == "authors:Li*" and _match("author:a:b") == "authors:a:b", "* や : の付いた語まで句にしている"
    # 引用符つきの句: 索引が空白を詰めた列(本文・抄録・表題)に合わせる。列を指さない句は両方の形で、詰めていない列の句はそのまま
    assert _match('"干渉 測定"') == '("干渉 測定" OR "干渉測定")' and _match('text:"干渉 測定" NOT "fiber laser"') == 'text:"干渉測定" NOT "fiber laser"'
    assert _match('author:"山田 太郎" OR folder:"干渉 測定"') == 'authors:"山田 太郎" OR folder:"干渉 測定"', "空白を詰めていない列の句まで詰めている"
    assert _short_terms("author:Li AND title:laser") == [] and _short_terms("title:Si AND author:Li") == ["Si"], \
        "引けるようになった著者の短い語に「当たらない」の注意を出している、または他の列の短い語の注意を消している"
    assert _visible(["/a", "/_dup/b", "/c"], {"/_dup/b"}, 2) == ["/a", "/c"], "移した重複を検索結果に出している"

    # --- エージェント向け: has / cite / --json ---
    assert norm_id("https://doi.org/10.1103/PhysRevLett.100.123456") == ("doi", "10.1103/physrevlett.100.123456")
    assert norm_id("arXiv:1501.01234v3") == ("arxiv", "1501.01234") and norm_id("1501.01234") == ("arxiv", "1501.01234")
    assert norm_id("10.48550/arXiv.2201.01234") == ("arxiv", "2201.01234"), "arXiv の DOI を arXiv ID にしていない"
    assert norm_id("cond-mat/0001234v1") == ("arxiv", "cond-mat/0001234")
    ok = {"rung": "text_doi", "source": "crossref", "year": 2016, "first_author": "Miller"}
    R = [dict(ok, path="/keep.pdf", doi="10.1/x"), dict(ok, path="/_dup/x.pdf", doi="10.1/x", duplicate_of="/keep.pdf"),
         dict(ok, path="/arx.pdf", rung="arxiv_text", arxiv_id="1501.01234", published_doi="10.1/y"),
         {"path": "/u.pdf", "rung": "unverified", "doi": "10.1/z"}]
    got = {x["id"]: (x["status"], x["path"]) for x in has_lookup(R, ["10.1/X", "10.1/y", "10.1/z", "10.1/w", "arXiv:1501.01234"])}
    assert got == {"10.1/X": ("有り", "/keep.pdf"), "10.1/y": ("プレプリントのみ", "/arx.pdf"), "10.1/z": ("候補(未検証)", "/u.pdf"),
                   "10.1/w": ("無し", None), "arXiv:1501.01234": ("有り", "/arx.pdf")}, got
    assert has_lookup([R[1], R[0]], ["10.1/x"])[0]["path"] == "/keep.pdf", "移した重複のパスを返している"
    moved_pre = [dict(ok, path="/_pre/arx.pdf", rung="arxiv_text", arxiv_id="1401.01234", published_copy="/pub.pdf"),
                 dict(ok, path="/pub.pdf", doi="10.1/pub")]
    assert [has_lookup(moved_pre, ["1401.01234"])[0][k] for k in ("path", "status")] == ["/pub.pdf", "有り(出版版)"], \
        "移したプレプリントのパスを返している(出版版を返し、出版版だと言う)"
    b = bibtex(dict(ok, doi="10.1234/example.5b01234", title="Theory of Example Light Scattering", journal="ACS Nano",
                    volume="10", issue="2", pages="2803-2818"), ["Miller, Ann J.", "Young, Bo"])
    assert b.startswith("@article{Miller2016Theory,") and "pages = {2803--2818}" in b and "Miller, Ann J. and Young, Bo" in b, b
    assert bibtex(dict(ok, rung="unverified", title="T", journal="J"), ["A"]).startswith("% 注意"), "未検証に注意書きが無い"
    b = bibtex(dict(ok, source="manual", rung="manual", title=None), ["Ann Miller"])
    assert b.startswith("@misc{Miller2016,") and "None" not in b, "誌名・表題の無い記録(人が直した学位論文・本)を article にしている、または None を書いている: " + b

    # --- fix: 人が直した記録は改名・引用の対象に入る。著者は並びで持つ ---
    un_ = {"path": "/L/t.pdf", "rung": "unresolved", "source": None, "title_guess": "g"}       # 未同定の記録は source を None で持つ
    fx = fix_record(un_, None, {"title": "T", "year": "2017", "authors": " Ann  B. Tester ;Bo Young; "})
    assert (fx["source"], fx["rung"], fx["year"], fx["authors"], fx["first_author"]) == ("manual", "manual", 2017, ["Ann B. Tester", "Bo Young"], "Tester"), fx
    assert renamable(fx) and un_["source"] is None, "直した未同定の記録が改名・引用の対象に入らない(source が None のまま)、または元の記録を変えた"
    assert fix_record(dict(un_, first_author="de Souza"), None, {"authors": "Ann Tester"})["first_author"] == "de Souza", "入っている第一著者の姓を、著者の並びで上書きしている"
    assert fix_record(dict(fx, source="crossref"), None, {"year": "2018"})["source"] == "crossref", "出どころのある記録の source を書き換えている"
    assert fix_record(fx, None, {"authors": ""})["authors"] is None, "空にした著者が消えない"
    bk_ = dict(fx, families=["Tester", "Young"], first_author="Tester", title="Optics")
    assert [fix_record(bk_, None, s).get("families") for s in ({"authors": "Carl Other"}, {"first_author": "Other"}, {"authors": ""}, {"year": "2001"})] \
        == [None, None, None, ["Tester", "Young"]], "著者を直しても古い姓の並びが残る・著者に触れない直しで姓の並びが消える"
    assert book_filename(fix_record(bk_, None, {"first_author": "Other"})).startswith("Other - "), "直した第一著者が本の名前に出ない"
    assert any("authors" in l and "2: Ann B. Tester; Bo Young" in l for l in fix_diff(un_, fx)), "著者の人数が違いの行に出ない: %s" % fix_diff(un_, fx)

    # --- 人が書いた DOI を引く: 取りに行く先は閉じた集合。登録機関の答えは、その DOI のものだけを採る ---
    assert doi_urls("10.5555/a<b") == ["https://api.crossref.org/works/10.5555/a%3Cb", "https://doi.org/ra/10.5555/a%3Cb",
                                    "https://data.crosscite.org/application/vnd.citationstyles.csl+json/10.5555/a%3Cb",
                                    "https://api.japanlinkcenter.org/dois/10.5555/a%3Cb"], doi_urls("10.5555/a<b")
    assert doi_urls("10.48550/arXiv.2101.00002v4") == ["http://export.arxiv.org/api/query?id_list=2101.00002"] \
        and doi_urls("10.48550/ARXIV.math.CT/0701234") == ["http://export.arxiv.org/api/query?id_list=math.CT/0701234"]
    assert not any(ARXIV_DOI.fullmatch(s) for s in ("10.48550/arXiv.2101.00002&x=1", "10.5281/zenodo.123", "10.48550/arXiv.2012", "x10.48550/arXiv.2101.00002")), \
        "arXiv の番号の形でないものを arXiv へ渡す"
    d_csl = {"DOI": "10.5555/JA.1", "title": "和文の表題", "author": [{"family": "試験", "given": "太郎"}, {"literal": "Test Consortium"}, {"given": "Solo"}, {}],
           "issued": {"date-parts": [["2012", 3]]}, "container-title": ["試験の雑誌", "x"], "volume": 55, "number": "1", "page": "42-46", "abstract": "<p>要旨</p>"}
    d_rec = _csl_record(d_csl, "10.5555/ja.1", "datacite")
    assert d_rec == {"doi": "10.5555/ja.1", "title": "和文の表題", "year": 2012, "first_author": "試験", "authors": ["試験 太郎", "Test Consortium", "Solo"],
                  "families": ["試験", "Test Consortium", "Solo"], "journal": "試験の雑誌", "journal_short": None, "volume": "55", "issue": "1",
                  "pages": "42-46", "abstract": "要旨", "source": "datacite"}, d_rec
    assert renamable(d_rec) and [_csl_record(m, "10.5555/ja.1", "datacite") for m in (dict(d_csl, DOI="10.5555/other"), dict(d_csl, title=""), [d_csl], None)] == [None] * 4, \
        "別の DOI の答え・表題の無い答え・形の違う答えを採る"
    # JaLC の答え: 英語の欄があれば英語、無ければあるもの。著者は順番の番号で並べる。頁の無い記録の「-」は頁にしない
    d_ja = {"doi": "10.5555/JA.1", "title_list": [{"lang": "ja", "title": "和文の表題"}, {"lang": "en", "title": "An English Title"}],
            "creator_list": [{"sequence": "2", "names": [{"lang": "ja", "last_name": "検査", "first_name": "花子"}]},
                             {"sequence": "1", "names": [{"lang": "ja", "last_name": "試験", "first_name": "太郎"}, {"lang": "en", "last_name": "SHIKEN", "first_name": "Taro"}]},
                             {"sequence": "3", "names": [{"first_name": "Test Office"}]}, {"sequence": "4", "names": []}],
            "journal_title_name_list": [{"journal_title_name": "shiken", "type": "abbreviation", "lang": "en"}, {"journal_title_name": "試験の雑誌", "type": "full", "lang": "ja"},
                                        {"journal_title_name": "Journal of Tests", "type": "full", "lang": "en"}],
            "volume": "55", "issue": "1", "first_page": "42", "last_page": "46", "publication_date": {"publication_year": "2012"}}
    d_jalc = {"status": "OK", "data": d_ja}
    d_jrec = _jalc_record(d_jalc, "10.5555/ja.1", "jalc")
    assert d_jrec == {"doi": "10.5555/ja.1", "title": "An English Title", "year": 2012, "first_author": "SHIKEN", "authors": ["SHIKEN Taro", "検査 花子", "Test Office"],
                      "families": ["SHIKEN", "検査", "Test Office"], "journal": "Journal of Tests", "journal_short": None, "volume": "55", "issue": "1",
                      "pages": "42-46", "abstract": None, "source": "jalc"}, d_jrec
    d_only = _jalc_record({"data": dict(d_ja, title_list=d_ja["title_list"][:1], journal_title_name_list=d_ja["journal_title_name_list"][:2],
                                        first_page="-", last_page=None, publication_date="2012")}, "10.5555/ja.1", "jalc")
    assert (d_only["title"], d_only["journal"], d_only["pages"], d_only["year"]) == ("和文の表題", "shiken", None, None), "英語の表題が無い答え・英語は略称だけの誌名・頁と年の無い答え: %s" % d_only
    assert _jalc_record({"data": dict(d_ja, last_page="42")}, "10.5555/ja.1", "jalc")["pages"] == "42" and renamable(d_jrec) \
        and [_jalc_record(m, "10.5555/ja.1", "jalc") for m in ({"data": dict(d_ja, doi="10.5555/other")}, {"data": dict(d_ja, title_list=[])}, {"data": [d_ja]}, d_ja, [d_jalc], None)] \
        == [None] * 6, "JaLC: 別の DOI の答え・表題の無い答え・形の違う答えを採る"
    assert _csl_record({"DOI": "10.5555/ja.1", "title": ["T"]}, "10.5555/ja.1", "datacite")["authors"] == [] \
        and not renamable(_csl_record({"DOI": "10.5555/ja.1", "title": ["T"]}, "10.5555/ja.1", "datacite")), "年も著者も無い答えを同定済みにする"
    assert _bib_authors({"authors": ["試験 太郎", "Ann B. Tester", "Solo", "Bo Young"], "families": ["試験", "Tester", "Solo", "Other"]}) \
        == ["試験, 太郎", "Tester, Ann B.", "Solo", "Bo Young"] and _bib_authors({"authors": ["試験 太郎"], "families": []}) == ["試験 太郎"] \
        and _bib_authors({"authors": ["a b", "c d"], "families": ["a"]}) == ["a b", "c d"], "引用の著者: 姓の並びから「姓, 名」を作らない・合わない並びを使う"
    # ISBN のために取りに行く先。URL はキャッシュの名前になるので、字面を固定する(変えると、取得済みの目録の答えを全部取り直す)
    d_mail = MAILTO
    try:
        for d_m, d_q in (("", "rows=10"), ("me@example.org", "rows=10&mailto=me@example.org")):
            globals()["MAILTO"] = d_m
            d_cr = "https://api.crossref.org/works?" + d_q + "&filter=type:book,type:monograph,type:edited-book,type:reference-book,isbn:%s"
            d_ol = "https://openlibrary.org/api/books?bibkeys=ISBN:%s&jscmd=data&format=json"
            assert list(isbn_urls("9784000000000").items()) == [
                ("ndl", ["https://ndlsearch.ndl.go.jp/api/opensearch?isbn=9784000000000"]),
                ("crossref-isbn", [d_cr % "9784000000000", d_cr % "4000000004"]), ("openlibrary", [d_ol % "9784000000000"])], isbn_urls("9784000000000")
            assert isbn_urls("9790000000001") == {"crossref-isbn": [d_cr % "9790000000001"], "openlibrary": [d_ol % "9790000000001"]}, "979 の ISBN・和書でない ISBN の宛先"
    finally:
        globals()["MAILTO"] = d_mail
    d_saved = {k: globals()[k] for k in ("_fetch", "get_json", "from_crossref", "from_arxiv", "_book_by_hand", "BOOKS")}
    d_asked = []
    def d_http(code):
        e = RuntimeError("no answer")
        if code:
            e.code = code
        return e
    def d_why(doi):
        del _FAILED[:], d_asked[:]
        try:
            return by_doi(doi)
        except LookupError as e:
            return str(e)
    def d_ra(name, doi="10.5555/ja.1"):
        d_ans = {"DOI": doi, "RA": name} if name else {"DOI": doi, "status": "DOI does not exist"}
        globals()["_fetch"] = lambda url, *a, **k: d_asked.append(url) or json.dumps([d_ans]).encode()
    try:
        globals()["BOOKS"] = False
        globals()["_book_by_hand"] = lambda isbn, doi: d_asked.append("book") or ({"title": "B", "families": ["Writer", "Other"], "source": "crossref"}, None)
        globals()["from_crossref"] = lambda doi: d_asked.append("crossref") or {"doi": doi, "source": "crossref"}
        globals()["from_arxiv"] = lambda aid: d_asked.append("arxiv:" + aid) or {"arxiv_id": aid, "source": "arxiv"}
        globals()["get_json"] = lambda url: d_asked.append(url) or (d_jalc if "japanlinkcenter" in url else d_csl)
        d_ra("JaLC")
        for d_bad in ("10.5555/../../10.5555/ja.1", "10.5555/./ja.1", "10.5555/ja.1/..", "ja.1", "10.5555/ja 1", "10.5/ja.1"):
            assert d_why(d_bad) == "this is not a DOI: " + d_bad and d_asked == [], "DOI の形でない・別の道になる文字列を引く: %s %s" % (d_bad, d_asked)
        assert d_why("10.5555/ja.1") == {"doi": "10.5555/ja.1", "source": "crossref"} and d_asked == ["crossref"], "Crossref にある論文で、ほかへ問い合わせる: %s" % d_asked
        assert d_why("10.48550/arXiv.2101.00002v2") == {"arxiv_id": "2101.00002", "source": "arxiv"} and d_asked == ["arxiv:2101.00002"], d_asked
        globals()["BOOKS"] = True
        assert d_why("10.5555/ja.1")["first_author"] == "Writer" and d_asked == ["book"], "書籍の DB で、本の DOI を本として引かない: %s" % d_asked
        # ISBN で引く: 本の記録は、第一著者の姓を入れ、前の記録の論文の項目を空にして上に書く。DB の種類は問わない
        globals()["BOOKS"] = False
        d_old = dict(bk_, journal="J. Wrong", journal_short="JW", volume="3", issue="1", pages="5", article_number="e1", doi="10.5555/wrong", families=["Old"])
        del d_asked[:]
        d_bk = fix_record(d_old, None, {"year": "1999"}, isbn="9780000000002")
        assert d_asked == ["book"] and [d_bk[k] for k in ARTICLE_KEYS] == [None] * 6 and (d_bk["first_author"], d_bk["families"], d_bk["title"], d_bk["year"]) \
            == ("Writer", ["Writer", "Other"], "B", 1999) and d_bk["rung"] == "manual", "ISBN で引いた本に、前の論文の項目が残る・姓が入らない: %s" % d_bk
        try:
            fix_record(d_old, "10.5555/ja.1", {}, isbn="9780000000002")
            raise AssertionError("DOI と ISBN の両方を受けた")
        except ValueError:
            pass
        globals()["BOOKS"] = True
        globals()["_book_by_hand"] = lambda isbn, doi: d_asked.append("book") or (None, "not a whole book")
        assert d_why("10.5555/ja.1")["source"] == "crossref" and d_asked == ["book", "crossref"], "本でない DOI を、書籍の DB で論文として引かない"
        globals()["BOOKS"] = False
        globals()["from_arxiv"] = lambda aid: None
        assert d_why("10.48550/arXiv.2101.00002") == "arXiv has no paper 2101.00002. Check the DOI" and d_asked == [], "arXiv に無い番号を、ほかへ問い合わせる"
        def d_isbn(isbn):
            del _FAILED[:]
            try:
                return by_isbn(isbn)
            except LookupError as e:
                return str(e)
        assert d_isbn("9780000000002") == "not a whole book. Check the number, or fill in the fields by hand"
        globals()["_book_by_hand"] = lambda isbn, doi: (_FAILED.extend(["ndl:1", "openlibrary:1", "ndl:2"]), (None, "x"))[1]
        assert d_isbn("9780000000002") == "could not reach the book catalogues (ndl, openlibrary). Try again later", "目録に届かないのに、無い本として答える"
        globals()["_book_by_hand"] = d_saved["_book_by_hand"]
        assert d_isbn("978-0-00-000000-3").startswith("not a valid ISBN") and not _FAILED, "検査数字の合わない番号を引く"
        globals()["from_crossref"] = lambda doi: _FAILED.append("crossref:" + doi)
        assert d_why("10.5555/ja.1").startswith("could not reach Crossref") and d_asked == [], "Crossref に届かないのに、記録が無いものとして先へ進む"
        globals()["from_crossref"] = lambda doi: None
        d_got = d_why("10.5555/ja.1")
        assert d_got == d_jrec and d_asked == ["https://doi.org/ra/10.5555/ja.1", "https://api.japanlinkcenter.org/dois/10.5555/ja.1"], (d_got, d_asked)
        d_ra("DataCite")
        assert d_why("10.5555/ja.1")["source"] == "datacite" and d_asked[1] == "https://data.crosscite.org/application/vnd.citationstyles.csl+json/10.5555/ja.1", d_asked
        assert set(d_asked) <= set(doi_urls("10.5555/ja.1")), "doi_urls に無い宛先へ取りに行く: %s" % d_asked
        for name, want in ((None, "does not exist"), ("Crossref", "not a journal article. Fill"), ("ISTIC", "registered with ISTIC")):
            d_ra(name)
            assert want in d_why("10.5555/ja.1") and len(d_asked) == 1 and not _FAILED, "登録機関 %s: %s %s" % (name, d_why("10.5555/ja.1"), d_asked)
        d_ra("JaLC", doi="10.5555/another")
        assert d_why("10.5555/ja.1").startswith("could not ask doi.org") and len(d_asked) == 1, "別の DOI についての答えで先へ進む"
        globals()["_fetch"] = lambda url, *a, **k: (_ for _ in ()).throw(d_http(None))
        assert d_why("10.5555/ja.1").startswith("could not ask doi.org") and _FAILED == ["doi.org:10.5555/ja.1"], "doi.org に届かなかったことが残らない: %s" % _FAILED
        d_ra("DataCite")
        globals()["get_json"] = lambda url: dict(d_csl, title=None)
        assert "no usable record" in d_why("10.5555/ja.1") and not _FAILED
        d_ra("JaLC")
        globals()["get_json"] = lambda url: d_csl
        assert "no usable record" in d_why("10.5555/ja.1"), "JaLC の答えを、別の機関の読み方で読む"
        for code, failed in ((404, []), (None, ["jalc:10.5555/ja.1"])):
            globals()["get_json"] = lambda url, code=code: (_ for _ in ()).throw(d_http(code))
            assert d_why("10.5555/ja.1").startswith("could not get the record") and _FAILED == failed, "登録機関に届かない・記録が無い、を分けて残さない: %s %s" % (code, _FAILED)
        del _FAILED[:]
        # fix_record: 引き直すと前の姓の並びは捨て、引いた記録の姓の並びは残す。書き換えた項目はその上に入る
        globals()["from_crossref"], globals()["get_json"] = (lambda doi: None), (lambda url: d_jalc)
        d_fix = fix_record(dict(bk_, families=["Old"]), "10.5555/ja.1", {"year": "2013"})
        assert (d_fix["families"], d_fix["year"], d_fix["source"], d_fix["rung"], d_fix["title"]) == (d_jrec["families"], 2013, "jalc", "manual", "An English Title"), d_fix
        globals()["from_crossref"] = lambda doi: {"doi": doi, "title": "C", "authors": ["Finder Fay"], "source": "crossref"}
        assert "families" not in fix_record(dict(bk_, families=["Old"]), "10.5555/ja.1", {}), "引き直す前の著者の姓の並びが残る"
    finally:
        globals().update(d_saved)
        del _FAILED[:]
    assert "text" not in compact({"text": "x" * 9, "doi": "d"}) and "text" in compact({"text": "x"}, True)

    # --- 確認用ページ: 外部の表題でページを壊さない。判定は候補が同じ行にだけ入れる ---
    page = review_page_html([{"cand_title": "a </script><img src=x onerror=alert(1)> & b"}], "<script>__DATA__</script>")
    assert "</script><img" not in page and page.count("</script>") == 1, "表題の </script> でページが壊れる"
    L = ["path\thint\tcand_doi\tok\n", "/a\th\t10.1/a\t\n", "/b\th\t10.1/b\t\n", "/c\th\t\t\n"]
    for v in ("y", "m", "n", "x"):                          # どれも入る(定数を使うと、定数を削っても通ってしまう)
        assert apply_ok(L, {("/a", "10.1/a"): v})[0][1].endswith("\t%s\n" % v), "判定 %s を入れない" % v
    out, n, stale = apply_ok(L, {("/a", "10.1/a"): "y", ("/b", "10.1/OLD"): "n", ("/c", ""): "y", ("/z", "10.1/z"): "n",
                                 ("/b", "10.1/b"): "maybe"})
    assert n == 1 and out[1].endswith("\ty\n") and out[2].endswith("\t\n") and out[3].endswith("\t\n"), \
        "候補が変わった行・候補の無い行・y/n 以外に判定を入れている"
    assert stale == 3, "入れなかった判定を数えていない"

    # --- 出版版の候補。人が y を付けるまで書かない ---
    kuz = {"rung": "arxiv_text", "source": "arxiv", "arxiv_id": "cond-mat/0001234", "first_author": "Kowalska", "year": 2007,
           "title": "Signatures of Many-Body Correlations in Two-Dimensional Spectra of Example Semiconductor Structures"}
    it = {"DOI": "10.1234/j.example.2007.02.010", "type": "journal-article", "author": [{"family": "Kowalska"}],
          "title": [kuz["title"].lower()], "issued": {"date-parts": [[2007]]}}
    assert pubver_target(kuz) and pubver_accept(kuz, it), "正しい出版版を候補にしない"
    assert not pubver_target(dict(kuz, journal_ref="Encyclopedia of Applied Physics 14 (1996)")), "掲載誌が分かっているのに探している"
    assert not pubver_target(dict(kuz, published_doi="10.1/x")), "出版版 DOI があるのに探している"
    assert not pubver_accept(kuz, dict(it, author=[])), "著者の無い候補を採っている(著者の無い記録に着く型)"
    assert not pubver_accept(kuz, dict(it, author=[{"family": "Sharma"}])), "第一著者の違う候補を採っている"
    assert not pubver_accept(kuz, dict(it, DOI="10.48550/arXiv.cond-mat/0001234")), "arXiv 自身を出版版にしている"
    assert not pubver_accept(kuz, dict(it, type="posted-content")), "プレプリントを出版版にしている"
    assert not pubver_accept(dict(kuz, published_doi_rejected=[it["DOI"]]), it), "人が違うとした候補をまた出している"
    tak_a = "Ballistic transport in two-dimensional lattices with a single vacancy"   # arXiv 版
    tak_p = "Edge currents in two-dimensional lattices with a single vacancy"         # 出版版(改題)。一致率は 0.754 と 0.807
    assert titles_agree(tak_a, tak_p) == titles_agree(tak_p, tak_a), "表題の一致が引数の順で変わる(出版時に改題した型)"
    assert titles_agree(tak_p, tak_a), "両方の順のうち大きい一致率をとっていない(改題した出版版が候補から落ちる)"

    # --- 論文番号。PRL などは頁の代わりに番号を持つ。頁があれば頁を使う ---
    prl = {"year": 2013, "first_author": "Porter", "journal_short": "Phys. Rev. Lett.",
           "journal": "Physical Review Letters", "volume": "110", "issue": "24", "pages": None,
           "article_number": "123456"}
    assert filename_for(prl) == "2013_Porter_PhysRevLett_110_24_123456", filename_for(prl)
    assert filename_for(dict(prl, pages="100-105")).endswith("_24_100"), "頁より論文番号を優先している"

    # --- 補足資料。実物の先頭から。本文の DOI に着いても論文そのものではない ---
    for t in ("Supporting Information for: 5 nm Particle Size in Example (1,2) Samples",
              "doi: 10.1234/example06506 SUPPLEMENTARY INFORMATION 1. Theoretical",
              "www.sciencemag.org/cgi/content/full/000/0000/0000/DC1 Supplementary Materials for Example",
              "Optical response of a single example nanostructure SUPPORTING INFORMATION M"):
        assert is_supplement(t), "SI を見逃している: " + t[:40]
    # ACS の論文は本文に SI の案内を書く。先頭でも全大文字でもないので論文のまま。
    assert not is_supplement("Letter pubs.acs.org/JPCL Example Environment Probed by "
                             "Shaped Laser Pulses *S Supporting Information ABSTRACT"), \
        "論文を SI とみなしている"
    assert not is_supplement("Pump Power Dependence of Example Loss suppression")
    si = mark_supplement({"rung": "bib_query", "source": "crossref", "doi": "10.1/x", "year": 2016,
                          "first_author": "Mayer", "text": "Supporting Information for: 5 nm"})
    assert si["rung"] == "supplement" and si["supplement_of_rung"] == "bib_query"
    paper = dict(si, rung="text_doi", text="PRL 110, 123456 (2013) PHYSICAL REVIEW LETTERS")
    assert mark_supplement(paper) is paper
    # 段ごとの扱い。dedup は unverified の DOI でファイルを動かさない。
    unver = dict(paper, rung="unverified")
    assert renamable(paper), "解決済みがリネーム対象に入らない"
    assert not renamable(si), "SI に本文の名前を付けてしまう"
    assert not renamable(unver), "裏の取れていない候補でリネームする"
    assert dedup_key(paper) == "10.1/x", "解決済みが dedup で束ねられない"
    assert dedup_key(si) is None, "SI が本文と同じ DOI で dedup に入る(本文が移される)"
    assert dedup_key(unver) is None, "裏の取れていない DOI で dedup がファイルを動かす"
    assert needs_human(unver), "unverified が人に回らない"
    assert not needs_human(si), "SI を人に回している(同定の必要が無い)"
    assert not needs_human(paper), "解決済みを人に回している"
    # 段は通ったが著者の無い記録(誌の前付けなど)。どの一覧にも出ず、DOI では束ねられ、抄録も付いていた
    orphan = dict(paper, first_author="")
    assert not renamable(orphan) and needs_human(orphan), "改名できない解決済みの記録が人に回らない(どの一覧にも出ない)"
    assert why(orphan).startswith("F_") and why(dict(orphan, year=None, first_author="A")).startswith("F_")
    assert dedup_key(orphan) is None, "人に回す記録の DOI で dedup がファイルを動かす"
    assert not enrichable(orphan), "人に回す記録の DOI で抄録を付ける"
    assert all(needs_human(dict(paper, rung=x)) for x in ("unresolved", "error", "unverified")), "同定できていない段が人に回らない"
    assert not needs_human(dict(orphan, rung="manual")), "人が一部だけ埋めた記録(manual)を人に戻している(review が止まり続ける)"
    assert not needs_human(dict(unver, not_paper="user")) and not needs_human(dict(paper, rung="unresolved", not_paper="user")), \
        "人が「論文ではない」とした記録を、また人に回している"
    assert not renamable(dict(unver, not_paper="user")), "論文ではない記録を改名の対象にしている"
    assert enrichable(paper), "解決済みに抄録を付けない"
    assert enrichable(si), "SI に本文の抄録を付けない(検索で本文と一緒に引けなくなる)"
    assert not enrichable(unver), "裏の取れていない DOI の抄録を付けてしまう"

    # --- リトライ判断。純関数なのでネットワークに触らずに見られる ---
    # 待ちの列は OpenAlex の文書が示す 1,2,4,8,16 秒。
    assert [_retry_plan(429, a)[1] for a in range(MAX_RETRY)] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert [_retry_plan(503, a)[1] for a in range(MAX_RETRY)] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert _retry_plan(429, MAX_RETRY) == (False, 0.0), "使い切っても諦めていない"
    # 待っても変わらないものを叩き直すと、無駄に相手を叩くだけになる。
    assert _retry_plan(404, 0) == (False, 0.0), "404 をリトライしている"
    assert _retry_plan(403, 0) == (False, 0.0), "403 をリトライしている"
    assert _retry_plan(200, 0) == (False, 0.0), "成功をリトライしている"
    # サーバ自身の指示はこちらの推測より優先する。解釈できない値なら既定に落とす。
    assert _retry_plan(429, 0, "7") == (True, 7.0), "Retry-After を無視している"
    assert _retry_plan(429, 3, "2") == (True, 2.0), "Retry-After より自前の待ちを優先している"
    assert _retry_plan(429, 0, "Wed, 21 Oct 2026 07:28:00 GMT") == (True, 1.0)
    assert _retry_plan(429, 0, "99999")[1] == 300.0, "Retry-After に上限が無い"
    # 中断は Exception 派生だと呼び出し側の except Exception に飲まれる。
    assert not issubclass(RateLimitAbort, Exception), "RateLimitAbort が握り潰される"

    # --- 外部から取れなかった問い合わせを「記録が無い」と混ぜない ---
    class _H(Exception):
        def __init__(self, code=None):
            self.code = code
    for exc, failed in ((_H(404), False), (_H(403), False), (_H(400), False), (_H(503), True), (_H(429), True),
                        (TimeoutError(), True)):
        del _FAILED[:]
        _lookup_failed(exc, "x")
        assert bool(_FAILED) == failed, "%r を%s" % (exc.code if isinstance(exc, _H) else exc,
                                                      "取れなかったと数えない" if failed else "取れなかったと数えた")
    saved = globals()["_resolve"]          # 4 つの問い合わせがこれを通すかは retry_check.py(偽サーバ相手に _fetch を通す)
    try:
        unres = {"path": "/f/a.pdf", "rung": "unresolved", "text": "T", "ocr": False}
        globals()["_resolve"] = lambda p: _FAILED.append("arxiv:1") or dict(unres)
        r = resolve("/f/a.pdf")
        assert r["rung"] == "error" and retryable(r) and r["text"] == "T", "取れなかった問い合わせがあるのに unresolved のまま"
        globals()["_resolve"] = lambda p: _FAILED.append("crossref:10.1/x") or dict(unres, rung="bib_query", doi="10.1/y")
        assert retryable(resolve("/f/a.pdf")), "前の段が取れずに後ろの段で着いた結果を採った"
        globals()["_resolve"] = lambda p: dict(unres)
        assert resolve("/f/a.pdf")["rung"] == "unresolved", "前の resolve の失敗が残って、取れた記録を error にした"
    finally:
        globals()["_resolve"] = saved
    lost = {"rung": "error", "fetch_failed": ["arxiv:1901.01234"], "filename": "a.pdf", "text": ""}
    assert why(lost).startswith("E_") and why(lost) != why({"rung": "error"}), "取得失敗が読み取りのエラーと同じ理由になる"
    assert not retryable({"rung": "error", "error": "broken pdf"}), "読み取りのエラーまで取り直しの対象にした"
    lines = add_lines([lost])
    assert any("取得失敗" in l for l in lines) and not any("人手行き" in l for l in lines) and "(本文なし)" in "".join(lines)
    assert any("人手行き" in l for l in add_lines([{"rung": "unresolved", "filename": "a.pdf", "title_guess": "T" * 20}]))

    # 連絡先が未設定のとき、空の mailto= を送らない。設定してあれば URL を変えない(キャッシュの名前が変わる)
    saved_m = globals()["MAILTO"]
    try:
        globals()["MAILTO"] = ""
        assert _no_empty_mailto("https://a/works/10.1/x?mailto=") == "https://a/works/10.1/x"
        assert _no_empty_mailto("https://a/works?rows=5&mailto=&select=DOI") == "https://a/works?rows=5&select=DOI"
        assert _no_empty_mailto("https://a/works?mailto=&q=1") == "https://a/works?q=1"
        globals()["MAILTO"] = "me@example.org"
        u = "https://a/works?rows=5&mailto=me@example.org&select=DOI"
        assert _no_empty_mailto(u) == u and _no_empty_mailto("https://a/x?mailto=") == "https://a/x?mailto=", \
            "連絡先があるのに URL を変えた(キャッシュの名前が変わり、全部取り直しになる)"
    finally:
        globals()["MAILTO"] = saved_m

    # --- 論文以外: 候補・判定の取り込み・移動の予定(どれも純粋な関数。ファイルを動かす側は index_check の R) ---
    un = {"path": "/L/a/x.pdf", "folder": "/L/a", "filename": "x.pdf", "rung": "unresolved", "n_pages": NONPAPER_PAGES + 70}
    assert nonpaper_candidates([un]) == [un], "未同定で頁数の多い記録を候補にしない"
    for r, reason in ((dict(un, n_pages=NONPAPER_PAGES), "線ちょうどの頁数"), (dict(un, n_pages=None), "頁数が読めない"),
                   ({k: v for k, v in un.items() if k != "n_pages"}, "頁数を調べていない"), (dict(un, kind="paper"), "人が論文として残した"),
                   (dict(un, not_paper="user", kind="book"), "人が論文以外とした"), (dict(paper, n_pages=300), "同定済み"),
                   (dict(un, rung="supplement"), "補足資料"), (dict(un, rung="error", fetch_failed=["x"]), "取得失敗(次の add が取り直す)")):
        assert not nonpaper_candidates([r]), "候補に入れてはいけない: " + reason
    R = [dict(un, path="/L/a/1.pdf"), dict(un, path="/L/a/2.pdf"), dict(paper, path="/L/a/3.pdf"), dict(un, path="/L/a/\u30ab\u3099.pdf")]
    assert nonpaper_marks(R, {"/L/a/1.pdf": "t", "/L/a/2.pdf": "p", "/L/a/3.pdf": "b", "/L/a/\u30ac.pdf": "a"}) == (2, 1, 1, [], [])
    assert (R[0]["kind"], R[0]["not_paper"]) == ("thesis", "user") and R[3]["kind"] == "patent", "論文以外に not_paper と種類を書いていない"
    assert R[1]["kind"] == "paper" and "not_paper" not in R[1] and needs_human(R[1]) and not nonpaper_candidates([R[1]]), \
        "論文として残す記録に not_paper を付けた、または候補に出し続ける"
    assert "kind" not in R[2] and "not_paper" not in R[2], "同定済みの記録に種類を書いている"
    assert nonpaper_marks(R, {"/L/a/none.pdf": "b", "/L/a/2.pdf": "z"}) == (0, 0, 0, ["/L/a/none.pdf"], ["z"]) and R[1]["kind"] == "paper", \
        "索引に無いパス・知らないキーを報告しない、または知らないキーで書き換えた"
    assert nonpaper_marks(R, {"/L/a/1.pdf": "p"})[:2] == (0, 1) and "not_paper" not in R[0], "論文に戻した記録に not_paper が残る"
    D = os.path.join("/L", NONPAPER_DIR, KINDS["thesis"][1])
    np_ = dict(un, not_paper="user", kind="thesis")
    P = [np_, dict(np_, path="/L/b/x.pdf", folder="/L/b"), dict(np_, path=D + "/y.pdf", folder=D, filename="y.pdf"),
         dict(np_, path="/L/a/gone.pdf", filename="gone.pdf"), dict(un, path="/L/a/k.pdf", filename="k.pdf", kind="paper"),
         dict(un, path="/L/a/o.pdf", filename="o.pdf", not_paper="user"),
         dict(np_, path="/L/a/d.pdf", filename="d.pdf", duplicate_of="/L/a/z.pdf"), dict(np_, path="/L/a/y.pdf", filename="y.pdf"),
         dict(un, path="/L/a/h.pdf", filename="h.pdf", kind="thesis")]           # 種類だけあって not_paper の無い記録(手で直した索引)
    disk = {r["path"] for r in P} - {"/L/a/gone.pdf"}
    plan, gone = nonpaper_plan(P, "/L", exists=lambda p: p in disk)
    assert [(r["path"], new) for r, new in plan] == [("/L/a/x.pdf", D + "/x.pdf"), ("/L/b/x.pdf", D + "/x_2.pdf"), ("/L/a/y.pdf", D + "/y_2.pdf")], \
        "移動の予定: 種類のフォルダへ・もうそこにある記録は外す・同じ名前には _2・論文として残す記録と種類の無い記録と移した重複は動かさない: %s" % plan
    assert gone == [P[3]], "ファイルの無い記録を予定に入れた、または報告しない"
    assert all(v[1] for k, v in KINDS.items() if k != "paper") and len({v[0] for v in KINDS.values()}) == len(KINDS), "種類のキーかフォルダが重なる・欠ける"
    assert not {v[0] for v in KINDS.values()} & set("ujko"), "種類のキーが、ページの操作のキー(u 取り消し・j/k 次と前・o 開く)と重なる"
    assert _pages_in("Title:  x\nPages:          12\nEncrypted: no\n") == 12 and _pages_in("") is None and _pages_in(None) is None, "pdfinfo の出力から頁数を読めない"
    assert _same_name("/L/a/Thesis.pdf") == _same_name("/L/a/thesis.pdf") and _same_name("/L/\u30ac.pdf") == _same_name("/L/\u30ab\u3099.pdf") \
        and _same_name("/L/a.pdf") != _same_name("/L/b.pdf"), "大小文字・合成/分解だけが違う名前を、同じ名前とみなしていない"
    # 大小文字・合成/分解だけが違う名前は、置き場のボリュームでは同じファイル。_2 を付けないと 2 つ目が 1 つ目を上書きする
    C = [dict(np_, path="/L/a/Thesis.pdf", filename="Thesis.pdf"), dict(np_, path="/L/b/thesis.pdf", folder="/L/b", filename="thesis.pdf"),
         dict(np_, path="/L/a/\u30ac.pdf", filename="\u30ac.pdf"), dict(np_, path="/L/b/\u30ab\u3099.pdf", folder="/L/b", filename="\u30ab\u3099.pdf")]
    assert [os.path.basename(new) for _, new in nonpaper_plan(C, "/L", exists=lambda p: p in {r["path"] for r in C})[0]] == \
        ["Thesis.pdf", "thesis_2.pdf", "\u30ac.pdf", "\u30ab\u3099_2.pdf"], "大小文字・合成/分解だけが違う名前を同じ移動先にしている(上書きで 1 件消える)"
    # 置き場の根が変わると、移し済みのものを別の場所の同じフォルダへ動かす予定になる。種類の付け直し(別の種類のフォルダへ)は止めない
    far = dict(np_, path="/X/L/" + NONPAPER_DIR + "/" + KINDS["thesis"][1] + "/x.pdf", folder="/X/L/" + NONPAPER_DIR + "/" + KINDS["thesis"][1])
    relabel = dict(np_, path="/L/" + NONPAPER_DIR + "/" + KINDS["book"][1] + "/z.pdf", folder="/L/" + NONPAPER_DIR + "/" + KINDS["book"][1], filename="z.pdf")
    pl = nonpaper_plan([far, relabel, np_], "/L", exists=lambda p: p in (far["path"], relabel["path"], np_["path"]))[0]
    assert len(pl) == 3 and [r["path"] for r, _ in nonpaper_strays(pl)] == [far["path"]], "置き場の根が変わったときの移動を見分けない、または種類の付け直しまで止める"
    # nonpaper-ok に渡すファイル: 見出しと空行は飛ばし、形の崩れた行・同じパスの 2 行目は読めない行として返す(黙って飛ばさない)
    assert nonpaper_read("path\tkind\r\n/a/x.pdf\tt\r\n\n/a/y.pdf\t b \n") == ({"/a/x.pdf": "t", "/a/y.pdf": "b"}, [])
    assert nonpaper_read("/a/x.pdf t\n/a/y.pdf\t\n/a/z.pdf\tt\n/a/z.pdf\tb\n") == ({"/a/z.pdf": "t"}, ["/a/x.pdf t", "/a/y.pdf\t", "/a/z.pdf\tb"]), \
        "タブで割れない行・キーの無い行・同じパスの 2 行目を黙って読み飛ばしている"
    N = [dict(un, path="/L/a/\u30ac.pdf")]
    assert nonpaper_marks(N, {"/L/a/\u30ab\u3099.pdf": "t"})[0] == 1, "渡されたパスが分解形のとき、合成形の索引に当たらない"
    # 画面から 1 件ずつ付ける種類は、同定済みの記録にも書く(上の既定は書かない)
    Rm = [dict(paper, path="/L/a/3.pdf")]
    assert nonpaper_marks(Rm, {"/L/a/3.pdf": "b"}, resolved_too=True)[:3] == (1, 0, 0) and (Rm[0]["kind"], Rm[0]["not_paper"]) == ("book", "user"), \
        "画面から付けた種類を、同定済みの記録に書かない"

    # --- DB から外す・別の DB へ送る ---
    out_ = dict(un, removed="user")
    assert moved_aside(out_) and not needs_human(out_) and needs_human(un), "外した記録を、脇へ移したものとして扱わない・人に回す"
    held = dict(paper, path="/L/a/h.pdf", doi="10.1/rm")
    assert renamable(held) and has_lookup([held], ["10.1/rm"])[0]["status"] == "有り" \
        and has_lookup([dict(held, removed="user")], ["10.1/rm"])[0]["status"] == "無し", "外した記録を、持っているものに数えている"
    S = [dict(un), dict(un, path="/L/b/x.pdf", folder="/L/b"), dict(un, path="/L/a/lost.pdf", filename="lost.pdf"),
         dict(paper, path="/L/a/keep.pdf", folder="/L/a", filename="keep.pdf"),
         dict(paper, path="/L/_dup/keep.pdf", folder="/L/_dup", filename="keep.pdf", duplicate_of="/L/a/keep.pdf")]
    away = "/L/%s/" % REMOVED_DIR
    on_disk = ({r["path"] for r in S} - {"/L/a/lost.pdf"}) | {away + "x.pdf"}       # 外した先に、もう同じ名前がある
    pl = remove_plan(S, S[:3], exists=lambda p: p in on_disk)
    assert [new for _, new in pl] == [away + "x_2.pdf", away + "x_3.pdf", None], \
        "外す先: 置き場の下の決まったフォルダ。同じ名前には _2, _3。ファイルの無い記録は印だけ: %s" % [new for _, new in pl]
    def refused(fn):
        try:
            fn()
        except ValueError:
            return True
        return False
    seen = lambda p: p in on_disk                          # 何でも「ある」と答える見方は使わない(検査が外れたときに、名前探しが終わらなくなる)
    assert refused(lambda: remove_plan(S, [S[3]], exists=seen)), "ほかの記録(移した重複)が指している記録を外す予定を作った"
    assert refused(lambda: remove_plan(S, [S[4]], exists=seen)) and refused(lambda: remove_plan(S, [out_], exists=seen)), \
        "脇へ移した記録(重複・外した記録)を、もう一度外す予定を作った"
    import types
    other = lambda library, root="/DB2": types.SimpleNamespace(ROOT=root, INDEX="/DB2/papers.jsonl", load_index=lambda: ([], None),
                                                               library_of=lambda recs: library)
    th = dict(un, path="/L/a/t.pdf", filename="t.pdf", not_paper="user", kind="thesis")
    here = lambda p: p in ("/L/a/x.pdf", "/L/a/t.pdf", "/Books/x.pdf")
    pl = send_plan([S[0], th], [S[0], th], other("/Books"), exists=here)
    assert [new for _, new in pl] == ["/Books/x_2.pdf", "/Books/%s/t.pdf" % KINDS["thesis"][1]], \
        "送り先: 相手の置き場(種類の付いた論文以外は種類のフォルダ)。同じ名前には _2: %s" % [new for _, new in pl]
    assert refused(lambda: send_plan([S[0]], [S[0]], other("/Books", ROOT), exists=here)), "自分自身の DB へ送る予定を作った"
    twin = types.SimpleNamespace(ROOT="/DB2", INDEX=INDEX, load_index=lambda: ([], None), library_of=lambda recs: "/Books")
    assert refused(lambda: send_plan([S[0]], [S[0]], twin, exists=here)), "同じ索引のファイルを指す DB へ送る予定を作った(フォルダが違っても、索引は 1 つ)"
    assert refused(lambda: send_plan([S[0]], [S[0]], other(None), exists=here)), "論文の置き場が決まっていない DB へ送る予定を作った"
    for nested in ("/L/a/inner", "/L"):                    # 相手の置き場がこちらの中・こちらの置き場が相手の中
        assert refused(lambda: send_plan([S[0]], [S[0]], other(nested), exists=here)), \
            "片方の置き場がもう片方の中にあるのに送る予定を作った(外側の DB の次の add が、送ったファイルをまた拾う): %s" % nested
    assert refused(lambda: send_plan([S[0]], [S[0]], other("/Books"), exists=lambda p: False)), "ファイルの無い記録を送る予定を作った"
    assert refused(lambda: send_plan(S, [S[3]], other("/Books"), exists=seen)), "ほかの記録が指している記録を送る予定を作った"

    # ---------- 書籍の DB ----------
    W13, V13 = "9780000000019", "9783000000010"
    assert isbn13("978-0-00-000001-9") == W13 and isbn13("0-00-000001-9") == W13 and isbn13("000000006X") == "9780000000064", \
        "ISBN を 13 桁にそろえていない(ハイフンつき・10 桁・検査数字が X)"
    assert isbn13("978-0-00-000001-8") is None and isbn13("0-00-000001-8") is None and isbn13("12345") is None and isbn13(None) is None, \
        "検査数字の合わない ISBN を通している"
    assert _isbn10(W13) == "0000000019" and _isbn10("9791234567896") is None, "13 桁から 10 桁の形を作れていない(979 には 10 桁の形が無い)"
    assert isbns_in("ISBN 978-0-00-000001-9 (cloth)") == [W13] and isbns_in("ISBN-13: 978‐0‐00‐000001‐9") == [W13] \
        and isbns_in("ISBN 0-00-000001-9 10 9 8 7 6") == [W13] and isbns_in("ISBN %s ISBN %s isbn:%s" % (W13, V13, W13)) == [W13, V13], \
        "「ISBN」の表示に続く番号を拾えていない(別の形のハイフン・後ろに刷数が続く・重複)"
    assert isbns_in("ISBN 978-0-00-000001-8") == [] and isbns_in("tel 978-0-00-000001-9") == [], "検査数字の合わない番号・表示の無い番号を拾っている"
    assert isbns_in("ISBN 1300000007 10 9 8 7 6 5 4 3 2 1") == ["9781300000006"], "「ISBN」に続く 13 で始まる番号の頭を、表示(ISBN-13)の一部として読んだ"
    assert isbns_in("ISBN 9780000003000") == [] and isbns_in("no. 9780000003000", bare=True) == [], \
        "検査数字の合わない 13 桁の頭 10 桁を、別の ISBN として通した"
    assert isbns_in("tel 978-0-00-000001-9", bare=True) == [W13] and isbns_in("1978000000001912", bare=True) == [], \
        "表示の無い 13 桁: 拾えていない、または長い数字の途中を拾っている"
    cp = "Copyright © 2009 by John Wiley. All rights reserved. ISBN 978-0-00-000001-9"
    assert copyright_isbns([(1, "cover"), (2, "Related titles ISBN 978-3-00-000001-0"), (3, cp)])[:2] == (3, [W13]), \
        "著作権の語の無い頁(関連書の一覧)の ISBN を使っている"
    assert copyright_isbns([(1, "Related titles ISBN " + V13), (2, "no numbers here")]) == (None, [], ""), "著作権の頁が無いのに ISBN を返した"
    assert copyright_isbns([(1, "© 2001 order no. " + V13), (2, cp)])[:2] == (2, [W13]), "表示のある ISBN より先に、表示の無い 13 桁を使っている"
    assert copyright_isbns([(1, "x"), (4, "© 2020 Printed in the USA " + V13)])[:2] == (4, [V13]), "表示の無い 13 桁を、著作権の頁から拾えていない"
    assert page_year("© Springer-Verlag Berlin Heidelberg 2005") == 2005, "© と年の間が長い出版社名のとき、年を読めていない"
    assert page_year(cp) == 2009 and page_year("no year") is None and page_year("Copyright © 1988, 1992, 2007 Cambridge. Printed 2011") == 2007 \
        and page_year("© 1990 A. Copyright © 1994 by B") == 1994 \
        and page_year("in accordance with the Copyright, Designs and Patents Act 1988. First published 2010") is None, \
        "著作権の頁の © の年(並んだ年のいちばん新しいもの。© に続かない年は見ない。英国の法律の名前の 1988 は年でない)"
    assert page_edition("First published 1986 Second edition 1997 Third edition 2000") == 3 and page_edition("QC446.2 — 2nd ed.") == 2 \
        and page_edition(cp) is None, "著作権の頁の版(いちばん大きいもの)"
    head = _bnorm("WILEY IMAGINARY OPTICS Anna M. Writer / 架空太郎のC++入門 架空出版")
    assert book_title_in("Imaginary Optics (Wiley Series in Pure and Applied Optics)", head) and book_title_in("Imaginary optics: an introduction", head) \
        and book_title_in("架空太郎のC++入門", head), "目録の表題(主題)が PDF にあるのに無いとした(叢書名・副題・和文)"
    assert not book_title_in("Nonlinear Optics", head) and not book_title_in("Opt", head) and not book_title_in(None, head), \
        "PDF に無い表題・短すぎる表題を通している"
    ja = _bnorm("熱力学 入門 講義")
    assert book_title_in("熱力学 : 入門", ja) and book_title_in("力学", ja) and not book_title_in("学", ja) and not book_title_in("光学", ja), \
        "和書の短い主題(2 字以上)を通していない、または 1 字・PDF に無い主題を通している"
    cr = _crossref_book({"DOI": "10.1002/x", "title": ["Imaginary Optics"], "author": [{"family": "Writer", "given": "Anna M."}],
                         "published": {"date-parts": [[2008, 10, 28]]}, "publisher": "Wiley"})
    assert (cr["families"], cr["authors"], cr["year"], cr["authors_role"], cr["publisher"]) == (["Writer"], ["Writer Anna M."], 2008, None, "Wiley"), cr
    ed = _crossref_book({"title": ["Imaginary Laser Pulses"], "subtitle": ["Principles"], "editor": [{"family": "Éditeur", "given": "C."}]})
    assert ed["authors_role"] == "editor" and ed["families"] == ["Éditeur"] and ed["subtitle"] == "Principles" and ed["year"] is None, \
        "編者だけの本: 編者を著者の欄に入れて role を editor にしていない"
    ol = _openlibrary_book({"title": "Imaginary optics", "authors": [{"name": "Anna Writer"}, {"name": "Beta, Dana"}],
                            "publish_date": "March 28, 2008", "publishers": [{"name": "Wiley"}]})
    assert (ol["families"], ol["year"], ol["publisher"], ol["doi"]) == (["Writer", "Beta"], 2008, "Wiley", None), ol
    nd = _ndl_book("<rss><channel><item><title>x</title><dc:title>架空太郎のC++入門</dc:title><dc:creator>架空, 太郎</dc:creator>"
                   "<dc:publisher>架空出版</dc:publisher><dcterms:issued xsi:type=\"dcterms:W3CDTF\">2019.9</dcterms:issued></item></channel></rss>")
    assert (nd["title"], nd["families"], nd["authors"], nd["year"], nd["source"]) == ("架空太郎のC++入門", ["架空"], ["架空 太郎"], 2019, "ndl") \
        and _ndl_book("<rss><channel></channel></rss>") is None, "国立国会図書館の応答の読み方"
    noau = dict(cr, families=[], authors=[])
    pick = lambda cs: book_pick(cs, head)
    assert pick([])[0] is None and "not found" in pick([])[1] and pick([dict(cr, title="Nonlinear Optics")])[0] is None, \
        "目録に無い・表題が PDF に無いのに採った"
    assert pick([noau, ol])[0]["source"] == "openlibrary" and pick([noau, ol])[0]["doi"] == "10.1002/x", \
        "Crossref に著者が無いとき: 著者のある引き先の項目を使い、DOI は Crossref のものを付ける"
    assert pick([dict(noau, isbn=V13), dict(ol, isbn=W13)])[0]["doi"] is None \
        and pick([dict(noau, isbn=V13, isbn_all=[V13, W13]), dict(ol, isbn=W13)])[0]["doi"] == "10.1002/x" and "isbn_all" not in pick([cr])[0], \
        "DOI: 選んだ記録と違う ISBN の Crossref の記録から付けた、または同じ本の別の ISBN の記録から付けていない"
    assert pick([ol, cr])[0]["source"] == "crossref" and pick([cr, dict(nd, title="Imaginary optics")])[0]["source"] == "ndl" and pick([noau])[0]["source"] == "crossref", \
        "項目の出どころの順(国立国会図書館 → 著者のある Crossref → 著者のあるもの → 先頭)"
    assert pick([cr, nd])[0] is None and "different books" in pick([cr, nd])[1] and pick([cr, dict(ol, title="Imaginary Optics, Volume I")])[0]["source"] == "crossref" \
        and book_pick([cr, dict(ol, title="Imaginary Optics, Volume I")], _bnorm("imaginary optics volume i"))[0]["source"] == "crossref", \
        "著作権の頁の ISBN が別々の本を指しているのに採った、または表題の片方がもう片方を含む組(同じ本の書き方の違い)を別の本とした"
    B = lambda **kw: dict({"families": ["Writer"], "title": "Imaginary Optics", "first_author": "Writer"}, **kw)
    assert book_filename(B(), None, 2009) == "Writer - Imaginary Optics (2009)" and book_filename(B(), 1, None) == "Writer - Imaginary Optics", \
        "書籍の名前: 1 版は書かない、年が無ければ括弧も無い"
    assert book_filename(B(families=["Alpha", "Beta"]), 4, 2004) == "Alpha & Beta - Imaginary Optics (4th ed, 2004)" \
        and book_filename(B(families=["Gamma", "Delta", "Epsilon"]), 3, 2007) == "Gamma et al - Imaginary Optics (3rd ed, 2007)" \
        and book_filename(B(families=["Éditeur"], authors_role="editor", title="Imaginary Laser Pulses: Principles"), 2, 2005) \
        == "Éditeur (ed.) - Imaginary Laser Pulses (2nd ed, 2005)" and book_filename(B(families=[], first_author=None), 2, None) \
        == "Imaginary Optics (2nd ed)", "書籍の名前: 2 人は &、3 人以上は et al、編者は (ed.)、副題は付けない、著者が無ければ表題から"
    assert book_filename(B(name_author="Alpha & Beta", name_title="Imaginary Thermal Physics"), 2, 1980) == "Alpha & Beta - Imaginary Thermal Physics (2nd ed, 1980)" \
        and book_filename(B(), None, 2009, "A & B", "T") == "A & B - T (2009)" and book_name_parts(B(name_author="X")) == ("X", "Imaginary Optics"), \
        "書籍の名前: 利用者が表で直した著者・表題を使っていない"
    assert [_ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)] == ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"]
    assert [name_hints(n) for n in ("Writer - Imaginary Fiber Optics (5th, 2013).pdf", "Scribe - Imaginary Quantum Mechanics, 2ed, Example-Press, 2011.pdf",
                                    "Scribe - Imaginary Mechanics(3rd).pdf", "Writer - Imaginary Optics 2009.pdf", "18.pdf",
                                    "10.9999_9780000000019_previewpdf.pdf")] \
        == [(5, 2013), (2, 2011), (3, None), (None, 2009), (None, None), (None, None)], "いまのファイル名に書いてある版と年の読み方"
    assert _book_query("Scribe - Imaginary Optics 3rd ed 2007.pdf") == "Scribe Imaginary Optics 2007", "表題の検索に投げる語"
    assert _excluded("/L/その他/a/b.pdf", "/L", ["その他", "errata"]) and not _excluded("/L/b.pdf", "/L", ["その他"]) \
        and not _excluded("/L/x/その他/b.pdf", "/L", ["その他"]) and not _excluded("/L/その他.pdf", "/L", ["その他"]) \
        and _excluded("/L/%s/b.pdf" % unicodedata.normalize("NFD", "ガイド"), "/L", ["ガイド"]) and not _excluded("/L/a/b.pdf", "/L", []), \
        "見ないフォルダ: 直下の名前だけを、合成形にそろえて比べる"
    assert why({"rung": "unresolved", "why_not": "no ISBN on a copyright page"}).startswith("H_"), "本として同定できなかった理由を出していない"
    bk = dict(paper, path="/B/w.pdf", filename="w.pdf", rung="isbn", journal=None, journal_short=None, isbn=W13, publisher="Wiley", name_edition=2,
              title="Imaginary Optics")
    assert bibtex(bk, ["Writer, Anna"]).startswith("@book{") and all(x in bibtex(bk, ["A"]) for x in ("publisher = {Wiley}", "isbn = {%s}" % W13, "edition = {2}")) \
        and "editor = {A}" in bibtex(dict(bk, authors_role="editor"), ["A"]) and bibtex(dict(paper, journal="Phys. Rev. Lett."), ["A"]).startswith("@article{") \
        and "year = {1999}" in bibtex(dict(bk, year=2016, name_year=1999), ["A"]) and "year = {2016}" in bibtex(bk, ["A"]) \
        and bibtex(dict(bk, journal="Phys. Rev. Lett."), ["A"]).startswith("@article{"), \
        "目録で同定した本の BibTeX が @book でない、または論文の BibTeX を変えた"
    unb = {"rung": "unresolved", "source": None, "path": "/B/u.pdf", "filename": "u.pdf"}
    g = globals()
    try:
        g["BOOKS"] = True
        assert [kind_group(r) for r in ({}, {"kind": "paper"}, {"kind": "bookpart"}, {"not_paper": "user", "kind": "lecture"},
                                        {"not_paper": "user", "kind": "bookpart"}, {"not_paper": "user", "kind": "book"})] \
            == ["book", "paper", "book", "other", "book", "book"], "書籍の DB での検索の種類の分け方"
        assert needs_human(dict(unb, not_paper="user", kind="book")) and not needs_human(dict(unb, not_paper="user", kind="bookpart")) \
            and needs_human(unb), "書籍の DB: 人が「本」と付けた未同定の記録を人手行きから外した、または部分と付けた記録を回し続ける"
        assert [r["path"] for r in nonpaper_candidates([bk, unb, dict(unb, path="/B/k.pdf", kind="bookpart"),
                                                        dict(unb, path="/B/m.pdf", removed="user")])] == [bk["path"], "/B/u.pdf"], \
            "書籍の DB の種類の候補: 種類の決まっていない記録の全部(同定済みも)。脇へ移した記録は出さない"
        assert why(dict(bk, year=None)).startswith("H_identified") and why(dict(unb, why_not="x")).startswith("H_not identified"), \
            "書籍の DB: 同定できたが年か著者の無い記録の理由"
        assert book_renamable(bk) and not book_renamable(dict(bk, kind="bookpart")) and not book_renamable(dict(bk, arxiv_id="2101.00002")) \
            and not book_renamable(unb) and book_renamable(dict(bk, kind="book")) and not book_renamable(dict(bk, kind="lecture")) \
            and not book_renamable(dict(bk, removed="user")), \
            "書籍の DB で名前を付け替えるのは、同定できた本の全体だけ"
    finally:
        g["BOOKS"] = False
    assert kind_group({"not_paper": "user", "kind": "bookpart"}) == "book" and kind_group({}) == "paper" and kind_group({"kind": "bookpart"}) == "paper" \
        and not needs_human(dict(unb, not_paper="user", kind="book")), "論文の DB の種類の分け方・人手行きを変えた"

    print("selftest: ok")


CMDS = {"scan": scan, "add": add, "stats": stats, "rename": rename, "undo": undo,
        "review": review, "merge": merge, "pubver": pubver, "review-page": review_page, "review-ok": review_ok, "fix": fix, "info": info, "has": has, "cite": cite, "recent": recent, "relocate": relocate, "views": views, "move": move,
        "enrich": enrich, "dedup": dedup, "search": search, "gui": gui, "index": build_db, "fulltext": fulltext,
        "nonpaper": nonpaper, "nonpaper-page": nonpaper_page, "nonpaper-ok": nonpaper_ok, "remove": remove, "send": send,
        "selftest": selftest}

NO_INDEX_OK = ("scan", "add", "selftest")     # 索引が無くても動くコマンド(作る側と、何も読まない検査)

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit("papers <コマンド> …  コマンド: %s\n手順は %s" % (" ".join(CMDS), os.path.join(CODE, "MANUAL.md")))
    if sys.argv[1] not in NO_INDEX_OK and not os.path.exists(INDEX):
        sys.exit("索引がまだ無い(%s)。最初は: papers add <論文 PDF のフォルダ>" % INDEX)
    CMDS[sys.argv[1]]()
