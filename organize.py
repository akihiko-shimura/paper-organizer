#!/usr/bin/env python3
"""Resolve local paper PDFs to authoritative metadata (Crossref / arXiv) and index them.

    python3 organize.py scan [dir]     # build papers.jsonl (resumable, cached)
    python3 organize.py stats          # ladder coverage from the index

Design: identification is a *lookup*, never a generation.  Every rung below produces a
candidate identifier or title; the authority (Crossref/arXiv) produces the field values.
An unverifiable candidate becomes 'unresolved' rather than a guess.
"""
import collections, difflib, html, json, os, re, shutil, sqlite3, subprocess, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.realpath(__file__))   # ~/.local/bin/papers などのリンク経由でも本体の場所


def _config():
    """利用者ごとの値。ROOT/config.json(追跡しない)。無ければ空で、どれも省ける。
      mailto  Crossref・OpenAlex に名乗る連絡先(「礼儀正しい枠」に入る)。環境変数 PAPERS_MAILTO が優先
      views   papers views の出力先(既定 ~/PaperViews)"""
    try:
        with open(os.path.join(ROOT, "config.json")) as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


CONFIG = _config()
MAILTO = os.environ.get("PAPERS_MAILTO") or CONFIG.get("mailto") or ""
INDEX = os.environ.get("INDEX") or os.path.join(ROOT, "papers.jsonl")
CACHE = os.path.join(ROOT, ".cache")
OCR_LANG = os.environ.get("OCR_LANG", "ja-JP,en-US")   # 順序が精度を決める。§5参照

DOI_RE = re.compile(r'10\.\d{4,9}/[^\s"<>,;\]}]+')   # ')' は除外しない(旧Elsevier PII)
ARX_TEXT = re.compile(r'arXiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?', re.I)  # prefix required: a bare
ARX_NEW = re.compile(r'^(\d{4}\.\d{4,5})(?:v\d+)?', re.I)  # 4.5-digit run is often a page range
ARX_OLD = re.compile(r'(?:arXiv:)?\s*([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?', re.I)
# arXiv が PDF の余白に刷る刻印(ID・版・分野・日付)。この PDF が arXiv 版であることの証拠。
# 参考文献中の引用(`arXiv:1234.5678 [cond-mat]`)は版や日付を持たないので当たらない。
# arXiv の固定の書式なので列挙でよい(規約 6: 閉じた集合)。実測: 出版版として解決済み 2282 件で
# 当たったのはプレプリントだった 4 件のみ、arXiv として解決済み 79 件のうち 71 件に刻印がある。
ARX_STAMP = re.compile(r'arXiv:\s?(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})v\d+\s*\[[a-z.-]+\]\s+'
                       r'\d{1,2}\s+[A-Z][a-z]{2}\s+(?:19|20)\d\d')


def sh(cmd, timeout=40):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def norm(s):
    """照合用。NFKD で合字(ﬁ→fi)と全角を展開し、アクセントを外してから英数字だけ残す。
    PDF の本文はアクセントを分解形(n + ´)で持つことがあり、Crossref は合成形(ń)で返す。
    以前は ń ごと消していたので、ń を含む姓が 2 通りの綴りに割れ、裏取りで落ちた。
    全件の解き直し(n=3248)で +27 件が解決(27/27 正しい)、-1 件(AIP の PDF が − を À で
    持ち、以前は À ごと消えて偶然一致していた)。和文は従来どおり全部消える。"""
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def title_ratio(a, b):
    """norm 済みの 2 つの表題の一致率。SequenceMatcher は貪欲な照合なので引数の順で変わる(実例: 0.800 と
    0.782)。どちらの順で見つけた一致も実在する共通部分なので、大きい方をとる。titles_agree と段4 by_title が使う。"""
    return max(difflib.SequenceMatcher(None, a, b).ratio(), difflib.SequenceMatcher(None, b, a).ratio())


# Crossref の文字列は HTML のまま来る: タイトルに MathML(<mml:math>…)や <i>、誌名に &amp;、
# `Laser &amp;amp; Photonics Reviews` のような二重エスケープまである(n=3248 で 150 件)。
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


# ---------- authorities ----------
_FAILED = []   # 1 件の resolve の中で、外部から取れなかった問い合わせ。resolve が毎回空にする


def _lookup_failed(e, what):
    """問い合わせの例外を握り潰す前に通す。「記録が無い」(404 など。再実行しても変わらない)は何もしない。
    取れなかった(タイムアウト・5xx・再送の使い切り)ものは残し、resolve が「同定できなかった」と区別する。
    以前はどちらも None で、arXiv の遅延 1 回で刻印のある PDF が unresolved のまま索引に残った(2026-10-04)。"""
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
    # created(DOI登録日) は使わない: 後年に電子化された論文で実測 99/189 件が3年以上ずれる
    # (1962年の論文が created=2002)。
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
                       pace=3, timeout=60).decode()   # arXiv は 3 秒間隔を要求する。応答に 39.7 秒かかった例がある(n=1)
            _atomic_write(cp, [x])
    except Exception as e:
        _lookup_failed(e, "arxiv:" + aid)
        return None
    return _arxiv_record(x, aid)


def _arxiv_record(x, aid):
    """arXiv API の Atom を記録にする。出版版の DOI は同一性のキーではなく関係(published_doi)として持つ
    (プレプリントは出版版と別の論文: 利用者の判断)。実測で arXiv の記録 99 件中 74 件に出版版の DOI がある。"""
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
    (CLEO予稿 / 本誌 / 書籍章 / Erratum) が静かに採用される。実測で
    1998 年の Optics Letters の論文が、同じ表題の 2023 年の書籍章として改名されていた。
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
    literal に現れるものだけ採る。実測(n=20): 旧 LLM 段が拾った 20 件のうち 17 件を
    再現し、誤答 0。推論器(LLM)にタイトルを提案させる段は不要。"""
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
    """vocr.swift を必要になった時だけビルドする（約8秒、以後は再利用）。"""
    exe, src = os.path.join(ROOT, "vocr"), os.path.join(ROOT, "vocr.swift")
    if not os.path.exists(exe) or os.path.getmtime(src) > os.path.getmtime(exe):
        subprocess.run(["swiftc", "-O", "-o", exe, src], capture_output=True, timeout=300)
    return exe if os.path.exists(exe) else None


def ocr(path, pages=2, first=1):
    """画像PDF用。macOS の Vision framework を直接呼ぶ(vocr.swift)。
    実測(テキスト層つきPDFの真値と比較): 英語 語recall 91.7%/1.3秒、日本語 文字recall 98%。
    macOS の fm --tool ocr は 45.6%/70秒(4件中2件が空)。tesseract は英語92.4%だが日本語データ未導入。
    言語順が効く: OCR_LANG=\'en-US,ja-JP\' だと日本語が 2% まで落ちるので ja-JP を先に置く。"""
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
#          他人の著者名で通る。実例: ある論文が、表紙に並んだ同じ巻の別の論文に
#          着いていた。ヘッダにも DOI の探索対象にも入れない
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
# 実測: 段6・段4 の解決済み 590 件で、引用の中にしか無いのは 5 件で全て止めるべきもの
# (誤り 2 件と、論文を紹介したジャーナルクラブの資料 3 件)。既知の段6 の誤り 5/5 を捕まえる。
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
    「16 μm」と「1.6 μm」のような書き方の差で見つからず、段6 の解決済みの 9%(53/578)で判定が働かなかった。"""
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
    # 本文中にある出版版の DOI などには着かせない。以前は 4 件が text_doi で出版版として解決されていた。
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

    # 段4 は段6 の後ろのフォールバック。段4 が先だった時期、段4 の解決 118 件に段6 を
    # 掛け直すと 2 件で段4 が誤っていた(CLEO 予稿 / 同誌同著者の翌年の別論文)。
    # タイトル一致は同一タイトルの別版を区別できず、corroborate も年か誌名の一致で
    # 通してしまう。段6 は本文の書誌行ごと投げるので正しい版に着いた。
    # 段4 を残すのは、埋め込み Title からしか引けない 15 件(n=118)があるため。
    d = by_title(title_candidate(info, text), text)
    if d and corroborate(d, head):
        return dict(rec, rung="title_search", **d)

    if unver:
        return unver                          # 候補はあるが裏が取れない: 人に回す
    return dict(rec, rung="unresolved", source=None, ocr=ocred,
                title_guess=title_candidate(info, text)[:200],
                no_text=len(text) < 200)


def resolve(path):
    """外部から取れなかった問い合わせが 1 つでもあれば、結果を採らずに error にする(次の scan・add が取り直す)。
    解決していても採らない: 前の段が取れずに後ろの段で着くと、段も、ときには同定も変わる
    (arXiv が取れずに本文の検索で出版版に着く、など)。"""
    del _FAILED[:]
    r = _resolve(path)
    if _FAILED:
        what = sorted(set(_FAILED))
        return {"path": path, "folder": os.path.dirname(path), "filename": os.path.basename(path), "rung": "error",
                "error": ("外部から取れなかった: " + ", ".join(what))[:200], "fetch_failed": what,
                "text": r.get("text"), "ocr": r.get("ocr")}
    return mark_supplement(r)


def retryable(r):
    """外部から取れなかっただけの記録。scan・add はこれを索引に無いものとして扱い、同定し直す。"""
    return r.get("rung") == "error" and bool(r.get("fetch_failed"))


# ---------- 補足資料(SI)と、段ごとの扱い ----------
# SI は 1 ページ目に本文のタイトルを再掲するので、本文の DOI に着く(裏取りも通りうる)。
# 同じ DOI でも論文そのものではないので、rename も dedup もしない。人にも回さない。
# 判定は 1 ページ目の先頭 300 字だけ。ファイル名は見ない(suppression で誤検出した)。
# 実測 n=3248: 当たる 43 件はすべて SI(目視)。タイトルしか無い SI 3 件は拾えない。
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
# (以前は 3 箇所に別々に書かれ、dedup は段を見ずに unverified の DOI でファイルを動かした)。
TERMINAL = ("unresolved", "error", "unverified", "supplement")


def renamable(r):
    return bool(r.get("source") and r.get("year") and r.get("first_author")) \
        and r.get("rung") not in TERMINAL


POINTERS = ("duplicate_of", "published_copy")   # 別の記録のパスを指す項目(dedup が書く)


def repoint(recs, moved):
    """ファイルを動かしたら、そのパスを指す項目も書き換える。moved は {旧パス: 新パス}。
    rename がこれをしなかったので、一括 rename の後に移した 129 件すべてが古いパスを指していた(2026-09-23)。"""
    n = 0
    for r in recs:
        for k in POINTERS:
            if r.get(k) in moved:
                r[k], n = moved[r[k]], n + 1
    return n


def moved_aside(r):
    """dedup が _重複文献/・_プレプリント(出版版あり)/ へ移した記録。利用者が消すのを待つもので、
    もう組にも改名にもかけない(かけると dedup の再実行が移したものを移し直す)。"""
    return bool(r.get("duplicate_of") or r.get("published_copy"))


def needs_human(r):
    """人が見る記録: 改名できないもの(補足資料と、人が決めた manual を除く)。同定できていない段(unresolved・error・unverified)と、
    段の判定は通ったが出どころ・年・第一著者のどれかが欠けた記録。後者は Crossref の記録に著者が無いとき
    (誌の前付け・編集記事・書籍の章)に起き、裏取りは著者が空だと著者の照合を飛ばすので、論文でない記録に着きうる。
    以前は段だけを見ていたので、この記録は改名もされず、人手行きの一覧にも件数にも出なかった(2026-10-06、3 件)。
    manual は項目が欠けていても回さない: 人が一部だけ埋めた記録を戻すと、review が「未 merge の記入」とみて止まり続ける。"""
    return r.get("rung") not in ("supplement", "manual") and not renamable(r)


def enrichable(r):
    """外部の値(抄録・分野)を付けてよい記録。unverified の DOI は他人のものかもしれない。
    SI は本文の DOI を持つので、本文の抄録で検索に引っかかるのは望ましい。"""
    return bool(r.get("doi")) and (renamable(r) or r.get("rung") == "supplement")


def _same_person(a, b):
    """第一著者の姓が同じか。複合姓(García López と López のような組)は arXiv が最後の語を
    姓とするので、一方がもう一方に含まれれば同じとみなす(実測: 版の間の不一致 3/74 は全てこれ)。"""
    a, b = norm(a), norm(b)
    return len(min(a, b, key=len)) > 2 and (a in b or b in a)


def preprint_pairs(recs):
    """(プレプリント, 出版版) の組。両方とも解決済みで、プレプリントの published_doi が出版版の DOI と一致し、
    第一著者が同じもの。未確定の記録は組にしない(実例: 研究室のポスターが arXiv の候補を持つだけだった)。"""
    pub = {(r.get("doi") or "").lower(): r for r in recs if renamable(r) and r.get("doi") and not moved_aside(r)}
    return [(r, pub[r["published_doi"]]) for r in recs
            if renamable(r) and not moved_aside(r) and r.get("arxiv_id") and r.get("published_doi") in pub
            and _same_person(r.get("first_author"), pub[r["published_doi"]].get("first_author"))]


# ---------- ページの検証(vet.py が Claude Haiku で読んだ表題を使う) ----------
# Haiku は「主たる論文の表題と第一著者を指し示す」役に限る(利用者の判断)。同定は決めさせず、
# 読んだ表題が PDF に字面で存在し、しかも記録の表題と食い違うときだけ、解決済みを人に回す(拒否権のみ)。
# 実測(DESIGN.md「ページを見る」): 既知の誤り 7/7 で食い違い、正しい 30/30 で一致。
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
def _new_pdfs(root, done):
    """root(フォルダ、または 1 行 1 パスの .txt)の PDF のうち、索引に無いもの。scan と add が同じ一覧を見る。"""
    src = ([l.strip() for l in open(root)] if os.path.isfile(root)   # a .txt list of paths
           else [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs])
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
        bk = os.path.join(os.path.dirname(INDEX), "backups")
        os.makedirs(bk, exist_ok=True)
        shutil.copy2(INDEX, os.path.join(bk, "papers-%s-before-retry.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
        save_index([r for r in recs if r["path"] not in again], stamp)
    with open(INDEX, "a") as out:
        for i, p in enumerate(files):
            try:
                rec = resolve(p)
            except Exception as e:                      # one bad file must not kill the pass
                rec = {"path": p, "folder": os.path.dirname(p), "filename": os.path.basename(p),
                       "rung": "error", "error": str(e)[:200]}
            rec["scanned_at"] = time.strftime("%Y-%m-%d")      # papers recent が使う
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
            bk = os.path.join(os.path.dirname(INDEX), "backups")
            os.makedirs(bk, exist_ok=True)
            shutil.copy2(INDEX, os.path.join(bk, "papers-%s-before-add.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
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
    print("\n=== 重複(dry run)")
    dedup()
    print("\n=== 改名(dry run。まだ改名していない記録の全部)")
    rename()
    if not os.path.exists(DB) or os.path.getmtime(DB) < os.path.getmtime(INDEX):
        print("\n=== 検索の索引")
        build_db()
    print("\n動かすのは利用者の承認のあと:\n"
          "  papers dedup --apply                                   # 重複の予定があるとき\n"
          "  papers rename --apply && papers index && papers views   # 改名の予定があるとき\n"
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
    print("  %-14s %4d  %5.1f%%  (review で人へ)" % ("要人手", hand, 100.0 * hand / n))
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
    ok = [r for r in recs if renamable(r) and not moved_aside(r)]
    skipped = [r for r in recs if r not in ok]


    plan, seen = [], set()
    for r in ok:
        target = safe(filename_for(r)) + ".pdf"
        dst = os.path.join(r["folder"], target)
        if target == r["filename"]:
            continue                               # idempotent: already correct
        i = 2
        while dst in seen or (os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(r["path"])):
            dst = os.path.join(r["folder"], safe(filename_for(r)) + "_%d.pdf" % i)
            i += 1
        seen.add(dst)
        plan.append({"from": r["path"], "to": dst, "doi": r.get("doi"), "rung": r["rung"]})

    with open(os.path.join(ROOT, "unresolved.txt"), "w") as f:
        for r in skipped:
            f.write("%s\t%s\n" % (r.get("rung"), r["path"]))
    if not apply:
        for p in plan[:40]:
            print("  %s\n->%s" % (os.path.basename(p["from"]), os.path.basename(p["to"])))
        print("\n%d rename / %d 変更不要 / %d 触れない\nDRY RUN. 実行するには --apply"
              % (len(plan), len(ok) - len(plan), len(skipped)))
        return
    trail = os.path.join(ROOT, "renames-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with open(trail, "w") as f:                    # --apply のときだけ。written BEFORE any mv
        for p in plan:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(os.path.join(ROOT, "unresolved.txt"), "w") as f:
        for r in skipped:
            f.write("%s\t%s\n" % (r.get("rung"), r["path"]))

    for p in plan[:40]:
        print("  %s\n->%s" % (os.path.basename(p["from"]), os.path.basename(p["to"])))
    print("\n%d rename / %d 変更不要 / %d 触れない(unresolved.txt)" %
          (len(plan), len(ok) - len(plan), len(skipped)))
    moved = {}
    for p in plan:
        if not os.path.exists(p["from"]):          # already moved by an earlier run
            continue
        os.rename(p["from"], p["to"])
        moved[p["from"]] = p["to"]
    if moved:                                      # keep the index truthful, or the next
        recs2 = []                                 # rename run plans from dead paths
        for r in recs:
            if r["path"] in moved:
                r["path"] = moved[r["path"]]
                r["filename"] = os.path.basename(r["path"])
            recs2.append(r)
        repoint(recs2, moved)
        save_index(recs2, stamp)
    print("done (%d moved). 復元: python3 organize.py undo" % len(moved))


def undo():
    import glob
    trails = sorted(glob.glob(os.path.join(ROOT, "renames-*.jsonl")))
    if not trails:
        print("no rename trail"); return
    n = 0
    for l in open(trails[-1]):                     # most recent run only
        p = json.loads(l)
        if os.path.exists(p["to"]):
            os.rename(p["to"], p["from"])
            n += 1
    recs, stamp = load_index()
    back = {json.loads(l)["to"]: json.loads(l)["from"] for l in open(trails[-1])}
    for r in recs:
        if r["path"] in back:
            r["path"] = back[r["path"]]
            r["filename"] = os.path.basename(r["path"])
    repoint(recs, back)
    save_index(recs, stamp)
    print("reverted %d 件 (%s)" % (n, os.path.basename(trails[-1])))




# ---------- human handoff ----------
# 未解決は自動で埋めない。人が入力できる形に分類して渡し、書き戻す。
REVIEW_COLS = ["doi", "year", "first_author", "journal", "journal_short",
               "volume", "issue", "pages", "title"]


def why(r):
    if r.get("rung") not in TERMINAL:
        return "F_記録に著者か年が無い(論文でない記録に着いた疑い)"   # 段は通ったが改名できない
    if retryable(r):
        return "E_外部から取れなかった(次の scan・add で取り直す)"
    if r.get("rung") == "error":
        return "E_スキャン時にエラー"          # 読み取り自体が失敗。再スキャンで直ることもある
    if r.get("rung") == "unverified" and r.get("held_by") == "vet":
        return "G_ページの表題と記録の表題が食い違う"      # 解決済みを vet が人に回した
    if r.get("rung") == "unverified":
        return "D_候補はあるが本文と合わない"
    if r.get("no_text"):
        return "A_本文なし" if not r.get("ocr") else "A_OCRしても読めず"
    if len((r.get("title_guess") or "")) < 15:
        return "C_タイトル候補が取れず"
    # 以前の名前は「Crossrefに無い」だったが、Crossref にある論文も多い(候補が出た 146/617)。自動の問い合わせが
    # 外れただけ: 本文の先頭 300 字が論文の書き出しでない 108、表題が本文に字面で無い 31、その他 7(2026-10-07)
    return "B_タイトルは読めたが自動の検索で見つからない"   # 学位論文・書籍・予稿と、問い合わせが外れた論文


REVIEW_TSV = os.path.join(ROOT, "unresolved.tsv")
# 候補の列(review が埋める)と、利用者が埋める列。候補は ok に y を付けたときだけ merge が書く
CAND_COLS = ["cand_doi", "cand_title", "cand_where", "cand_year", "ok"]
USER_COLS = ["ok"] + REVIEW_COLS


def review_accept(r, it):
    """Crossref の検索結果 it を、人手行きの r の候補として review の表に出すか。Haiku が読んだ表題
    (page_title、PDF に字面で確認済み)と第一著者で照合する。実測(DESIGN.md「Haiku が読んだ表題で候補を出す」):
    人手側の候補 302/501、標本 18/22 が正しい。誤りは同名の別の版・同じ著者の別の本なので、人が y / n で決める。"""
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
    記入済みで未 merge の行があれば上書きしない(以前は再実行で記入が白紙に戻った)。"""
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
    """unresolved.tsv の人手入力を索引に書き戻す。doi があれば Crossref を正とする。"""
    head, rows = _read_review(REVIEW_TSV)
    by_path, rejected, not_journal = {}, {}, []
    for r in rows:
        d = {k: v.strip() for k, v in zip(head, r)}
        ok = d.get("ok", "").lower()
        took = ok == "y" and d.get("cand_doi") and not d.get("doi")
        if took:
            d["doi"] = d["cand_doi"]                        # 候補を採る
        elif ok == "n" and d.get("cand_doi"):
            rejected[d["path"]] = d["cand_doi"]             # 次から候補に出さない
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
        if r["path"] in by_path:
            r.update(by_path[r["path"]])
            hit += 1
    save_index(recs, stamp)
    print("%d 件を索引に反映、候補を違うとした %d 件" % (hit, len(rejected)))
    if not_journal:
        print("y を付けたが Crossref で雑誌の論文として引けない %d 件は書いていない(学位論文・書籍など)。"
              "year/first_author/journal などの列を埋めて再度 merge:" % len(not_journal))
        for q in not_journal[:20]:
            print("  " + q)


# ---------- 出版版の候補(arXiv 版に published_doi が無いもの。人が確かめてから書く) ----------
# 実測(DESIGN.md「出版版の候補」): arXiv が出版版 DOI を持つ 63 件で 59/63 を再現し違う DOI 1(同じ研究の学会要旨)。
# 欄が空の 19 件では、見つけた 10 件のうち確かに正しいのは 6。だから自動では書かず、published.tsv で人が y/n を付ける。
PUBVER_TSV = os.path.join(ROOT, "published.tsv")
PUBVER_COLS = ["path", "arxiv_title", "page_title", "cand_doi", "cand_title", "cand_where", "cand_year", "cand_type", "ok"]
_NOT_PUBLISHED = ("posted-content", "component", "peer-review", "dataset")


def _surname(s):
    w = (s or "").replace(",", " ").split()
    return norm(w[-1]) if w else ""


def pubver_target(r):
    """出版版を探す対象: arXiv 版として解決済みで、arXiv に出版版 DOI も掲載誌(journal_ref)も無いもの。
    journal_ref があれば掲載先は arXiv が知っている(実例: 百科事典の項目のプレプリントが、同名の別の予稿に着いた)。"""
    return (renamable(r) and bool(r.get("arxiv_id")) and not r.get("doi")
            and not r.get("published_doi") and not r.get("journal_ref"))


def pubver_accept(r, it):
    """Crossref の検索結果 it を r の出版版の候補として採るか。著者が無い候補は採らない
    (実例: 講義録のプレプリントが、著者の無い他人の本の章に着いた)。"""
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
    """エージェント向けの JSON。1 頁目の本文(text)は既定で外す(検索 20 件で出力の 65%、約 6 万字だった)。"""
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
    """BibTeX の著者。Crossref の応答(キャッシュ)に姓・名の区別があれば「姓, 名」。無ければ索引の値のまま
    (arXiv は「名 姓」で BibTeX がそのまま読める。Crossref 由来の「姓 名」は区切りが失われている)。"""
    if r.get("doi") and r.get("source") == "crossref":
        try:
            m = get_json("https://api.crossref.org/works/" + urllib.parse.quote(r["doi"]) + "?mailto=" + MAILTO)["message"]
            a = [(x.get("family") or "") + (", " + x["given"] if x.get("given") else "") for x in m.get("author") or []]
            if a:
                return a
        except Exception:
            pass
    return r.get("authors") or [r.get("first_author") or ""]


def bibtex(r, authors):
    """索引の記録から BibTeX を 1 件作る。未検証の記録には注意書きを付ける。"""
    fold = lambda x: unicodedata.normalize("NFKD", str(x or "")).encode("ascii", "ignore").decode()
    word = next((w for w in re.findall(r"[A-Za-z]{4,}", fold(r.get("title"))) if w.lower() not in ("with", "from", "this", "that")), "")
    key = re.sub(r"[^A-Za-z0-9]", "", fold(r.get("first_author"))) + str(r.get("year") or "") + word
    arx = r.get("arxiv_id") and not r.get("doi")
    f = [("author", " and ".join(authors)), ("title", "{%s}" % r.get("title", ""))]
    if arx:
        f += [("year", r.get("year")), ("eprint", r["arxiv_id"]), ("archivePrefix", "arXiv")]
        if r.get("published_doi"):
            f.append(("note", "Published: https://doi.org/%s" % r["published_doi"]))
    else:
        pages = r.get("pages") or r.get("article_number")
        f += [("journal", r.get("journal")), ("year", r.get("year")), ("volume", r.get("volume")),
              ("number", r.get("issue")), ("pages", str(pages).replace("-", "--") if pages else None), ("doi", r.get("doi"))]
    head = "% 注意: 未検証の記録(DOI は 1 頁目で裏が取れていない)\n" if r.get("rung") == "unverified" else ""
    body = ",\n".join("  %s = {%s}" % (k, v) for k, v in f if v)
    return "%s@%s{%s,\n%s\n}" % (head, "misc" if arx else "article", key, body)


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
    """papers recent [--n=20] [--json]。索引に最後に入った記録(scan の順)。scanned_at は 2026-09-24 以降の scan から。"""
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
    """papers relocate [--root 論文の置き場] [--apply]。手で動かした(名前も変えた)ファイルを索引の記録に対応付け直す。
    既定は対応を見せるだけ。--apply で索引を退避してから書く。dedup が移した重複でファイルが消えたもの(利用者が
    消した)は索引から外す。そのあと papers index。"""
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
    for r in lost:
        print("  見つからない  %s" % rel(r["path"]))
    print("対応 %d ・ 決まらない %d ・ 見つからない %d ・ 消えた重複(索引から外す) %d" % (len(got), len(amb), len(lost), len(gone)))
    left = len(new_files) - len(got)
    if left:
        print("どの記録にも当たらない PDF %d 件は新しいファイル: papers scan で足す" % left)
    if "--apply" not in sys.argv:
        print("DRY RUN. 書くには --apply"); return
    if not got and not gone:
        return
    bk = os.path.join(os.path.dirname(INDEX), "backups")
    os.makedirs(bk, exist_ok=True)
    shutil.copy2(INDEX, os.path.join(bk, "papers-%s-before-relocate.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
    moved = {}
    for r, p, _ in got:
        moved[r["path"]] = p
        r.update(path=p, folder=os.path.dirname(p), filename=os.path.basename(p))
    repoint(recs, moved)
    carry_preprint_ids(recs, gone)
    drop = {id(r) for r in gone}
    save_index([r for r in recs if id(r) not in drop], stamp)
    print("書いた。検索に反映: papers index")


VIEWS = os.path.expanduser(CONFIG.get("views") or "~/PaperViews")
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


def info():
    """索引・検索の索引・論文の置き場の場所と更新日時、件数。読むだけ。--json で機械向け。"""
    import glob
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else None
    recs = load_index()[0]
    im = os.path.getmtime(INDEX)
    dm = os.path.getmtime(DB) if os.path.exists(DB) else None
    dn = None
    if dm:
        c = sqlite3.connect(DB)
        dn = c.execute("SELECT count(*) FROM p").fetchone()[0]
        c.close()
    newest = lambda pat: max(glob.glob(os.path.join(ROOT, pat)), key=os.path.getmtime, default=None)
    ren, bk = newest("renames-*.jsonl"), newest("backups/papers-*.jsonl")
    d = {"repo": ROOT, "library": os.path.commonpath([r["folder"] for r in recs]) if recs else None,
         "index": INDEX, "index_updated": fmt(im), "index_bytes": os.path.getsize(INDEX), "records": len(recs),
         "resolved": sum(1 for r in recs if renamable(r) and not moved_aside(r)),
         "needs_human": sum(1 for r in recs if needs_human(r)), "moved_aside": sum(1 for r in recs if moved_aside(r)),
         "db": DB if dm else None, "db_updated": fmt(dm), "db_bytes": os.path.getsize(DB) if dm else None, "db_records": dn,
         "db_stale": bool(dm is None or dm < im or dn != len(recs)),
         "missing_files": sum(1 for r in recs if not os.path.exists(r["path"])),
         "last_rename": fmt(os.path.getmtime(ren)) if ren else None, "last_backup": bk}
    if "--json" in sys.argv:
        print(json.dumps(d, ensure_ascii=False)); return
    print("リポジトリ    %s" % d["repo"])
    print("論文の置き場  %s" % d["library"])
    mb = lambda b: "%.1f MB" % (b / 1e6) if b else "-"
    print("索引          %s\n              更新 %s ・ %s ・ %d 件（解決済み %d ・ 人手行き %d ・ 重複として移した %d）"
          % (d["index"], d["index_updated"], mb(d["index_bytes"]), d["records"], d["resolved"], d["needs_human"], d["moved_aside"]))
    print("検索の索引    %s\n              更新 %s ・ %s ・ %s 件" % (d["db"] or "(無い)", d["db_updated"] or "-", mb(d["db_bytes"]),
                                                   d["db_records"] or 0))
    if d["db_stale"]:
        print("              ※ 索引より古い(または件数が違う)。検索に反映するには: papers index")
    if d["missing_files"]:
        print("              ※ パスにファイルが無い記録 %d 件(手で動かした・消した)。papers relocate" % d["missing_files"])
    print("最後の改名    %s" % (d["last_rename"] or "-"))
    print("最新の退避    %s" % (d["last_backup"] or "-"))


FIX_KEYS = REVIEW_COLS + ["article_number", "published_doi"]


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
        if os.path.exists(new) or new in [q for _, q in plan]:
            sys.exit("移動先に同じ名前のファイルがある: %s" % new)
        plan.append((r, new))
    for r, new in plan:
        print("  %s\n→ %s" % (os.path.relpath(r["path"], root), os.path.relpath(new, root)))
    if not apply:
        print("DRY RUN. 動かすには --apply%s" % ("" if os.path.isdir(dest) else "(フォルダが無いので --mkdir も)")); return
    if not os.path.isdir(dest):
        os.makedirs(dest)
    bk = os.path.join(os.path.dirname(INDEX), "backups")
    os.makedirs(bk, exist_ok=True)
    shutil.copy2(INDEX, os.path.join(bk, "papers-%s-before-move.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
    trail = os.path.join(ROOT, "renames-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    _atomic_write(trail, [json.dumps({"from": r["path"], "to": new}, ensure_ascii=False) + "\n" for r, new in plan])
    moved = {}
    for r, new in plan:
        os.rename(r["path"], new)
        moved[r["path"]] = new
        r.update(path=new, folder=dest, filename=os.path.basename(new))
    repoint(recs, moved)
    save_index(recs, stamp)
    print("%d 件を動かした。戻すには papers undo。検索とビューに反映: papers index && papers views" % len(moved))


def fix():
    """1 件の記録を直す。既定は変更前と変更後を出すだけ。--apply で索引を書く(書く前に backups/ へ退避)。

        python3 organize.py fix <パスかファイル名の一部> --doi 10.1234/example.2020.001    Crossref から引き直す
        python3 organize.py fix <パスかファイル名の一部> --set year=1993 --set first_author=Yamada

    直した記録は rung=manual(人が決めた値。vet も上書きしない)。ファイル名と検索に反映するには、続けて
    `rename --apply` と `index`。直せる項目は FIX_KEYS だけ(path・filename・rung は直さない)。"""
    args = sys.argv[2:]
    target = next((a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--doi", "--set"))), None)
    doi = args[args.index("--doi") + 1] if "--doi" in args else None
    sets = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--set" and "=" in args[i + 1])
    if not target or not (doi or sets):
        sys.exit(fix.__doc__)
    bad = [k for k in sets if k not in FIX_KEYS]
    if bad:
        sys.exit("直せない項目: %s(直せるのは %s)" % (", ".join(bad), ", ".join(FIX_KEYS)))
    recs, stamp = load_index()
    r = _one_record(recs, target)
    new = dict(r)
    if doi:
        m = from_crossref(doi)
        if m is None:
            sys.exit("%s は Crossref で雑誌の論文として引けない。--set で書誌を直接入れる" % doi)
        new.update(m, source="crossref")
    for k, v in sets.items():
        new[k] = int(v) if k == "year" and v.isdigit() else (v or None)
    new.setdefault("source", "manual")
    new.update(rung="manual", fixed_by="user", fixed_at=time.strftime("%Y-%m-%d"))
    print(r["path"])
    for k in sorted(set(r) | set(new)):
        if r.get(k) != new.get(k) and k not in ("fixed_at",):
            print("  %-14s %s → %s" % (k, str(r.get(k))[:60], str(new.get(k))[:60]))
    if "--apply" not in args:
        print("DRY RUN. 書くには --apply"); return
    bk = os.path.join(os.path.dirname(INDEX), "backups")
    os.makedirs(bk, exist_ok=True)
    shutil.copy2(INDEX, os.path.join(bk, "papers-%s-before-fix.jsonl" % time.strftime("%Y%m%d-%H%M%S")))
    r.clear(); r.update(new)
    save_index(recs, stamp)
    print("書いた。ファイル名と検索に反映: python3 organize.py rename --apply && python3 organize.py index")


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
    tpl = open(os.path.join(ROOT, "review_page.html")).read()
    _atomic_write(REVIEW_PAGE, [review_page_html(out, tpl)])
    print("%d 件の候補を %s に。開く: open %s" % (len(out), REVIEW_PAGE, REVIEW_PAGE))


def apply_ok(lines, ok):
    """unresolved.tsv の行に、ページで付けた判定 ok {(path, cand_doi): "y"|"n"} を入れる。候補が変わった行
    (review をやり直した)と表に無い行は入れない。(新しい行, 入れた数, 入れなかった数) を返す。"""
    head = lines[0].rstrip("\n").split("\t")
    ip, ic, io = head.index("path"), head.index("cand_doi"), head.index("ok")
    out, n, seen = [lines[0]], 0, set()
    for l in lines[1:]:
        r = l.rstrip("\n").split("\t")
        v = ok.get((r[ip], r[ic])) if r[ic] else None
        if v in ("y", "n"):
            r[io], n = v, n + 1
            seen.add((r[ip], r[ic]))
        out.append("\t".join(r) + "\n")
    return out, n, sum(1 for k, v in ok.items() if v in ("y", "n") and k not in seen)


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
    404 は OpenAlex にその DOI の記録が無いという確定した答え(実例: ある PRL の論文は、
    同じ号の 68 件ごと OpenAlex に無い)。分け方は _retry_plan と同じ: 再送する状態(RETRY_STATUS)と、応答の無い失敗(接続断・
    時間切れ)だけが再実行で取り直せる。それ以外の HTTP の状態(400・403・404 など)は待っても変わらない。
    以前は 403・404・410 だけを「無い」としていた。arXiv は ID の形でない文字列(URL の一部を ARX_OLD が拾ったもの)に
    400 を返すので、同定の 4 件が毎回「取れなかった」になるところだった(2026-10-06、rescan_check で見つけた)。"""
    code = getattr(e, "code", None)
    return "failed" if code is None or code in RETRY_STATUS else "absent"


def enrich():
    """DOI から OpenAlex の抄録を取る。索引のみを読む再実行可能なパス。
    PDF レイアウト解析(GROBID)より強い: 実測で Crossref 17% に対し OpenAlex 92%。
    GROBID の抄録は公式ベンチで soft-F1 0.82-0.89・完全一致16%、かつ Docker/Java が要る。"""
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
    apply = "--apply" in sys.argv
    recs, stamp = load_index()
    keep = {l.strip() for l in open(DEDUP_KEEP) if l.strip()} if os.path.exists(DEDUP_KEEP) else set()
    groups = collections.defaultdict(list)
    for r in recs:
        key = dedup_key(r)
        if key and os.path.exists(r["path"]) and not moved_aside(r):
            groups[key].append(r)
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    pairs = [(a, b) for a, b in preprint_pairs(recs) if os.path.exists(a["path"]) and os.path.exists(b["path"])]
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
            while to in [p["to"] for p in plan] or os.path.exists(to):
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
        while to in [p["to"] for p in plan] or os.path.exists(to):
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
    for d in {os.path.dirname(p["to"]) for p in plan}:
        os.makedirs(d, exist_ok=True)
    trail = os.path.join(ROOT, "dedup-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
    with open(trail, "w") as f:
        for p in plan:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    moved = {}
    for p in plan:
        if os.path.exists(p["from"]):
            os.rename(p["from"], p["to"])
            moved[p["from"]] = p["to"]
    for r in recs:
        if r["path"] in moved:
            r["path"] = moved[r["path"]]
            r["folder"] = os.path.dirname(r["path"])
            p = [p for p in plan if p["to"] == r["path"]][0]
            r["published_copy" if p.get("kind") == "preprint" else "duplicate_of"] = p["keep"]
    save_index(recs, stamp)
    print("%d 件を移動。戻すには %s を使う" % (len(moved), os.path.basename(trail)))



# ---------- search: FTS5(trigram) + 文ベクトル ----------
# papers.jsonl からの純粋な派生。古ければ search が黙って作り直す。
DB = os.path.join(ROOT, "papers.db")
COLS = ["path", "year", "doi", "first_author", "title", "authors",
        "journal", "folder", "abstract", "text"]
# bm25 は全列に重みが要る(UNINDEXED 列の分も)。text はページ1なのでタイトルを再掲する → 低く。
W = [0, 0, 8, 6, 10, 6, 4, 2, 3, 1]
TERM = re.compile(r'[A-Za-z0-9]{3,}|[ぁ-んァ-ヶ一-龥]{3,}')


def _embed(texts):
    """vec.swift に1プロセスで流し込む。3245件でタイトルのみ約20秒。"""
    exe, src = os.path.join(ROOT, "vec"), os.path.join(ROOT, "vec.swift")
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


def build_db():
    import array
    recs = [json.loads(l) for l in open(INDEX) if l.strip()]
    if os.path.exists(DB):
        os.remove(DB)
    c = sqlite3.connect(DB)
    c.execute("CREATE VIRTUAL TABLE p USING fts5(%s, tokenize='trigram')" %
              ", ".join(x + " UNINDEXED" if x in ("path", "year") else x for x in COLS))
    c.executemany("INSERT INTO p VALUES (%s)" % ",".join("?" * len(COLS)),
                  [[r.get("path") or "", str(r.get("year") or ""), r.get("doi") or "",
                    r.get("first_author") or "", r.get("title") or "",
                    " ".join(r.get("authors") or []), r.get("journal") or "",
                    r.get("folder") or "", r.get("abstract") or "",
                    r.get("text") or ""] for r in recs])
    # ベクトルはタイトルのみ。抄録を足すと平均で薄まり精度が落ちる(§10 の測定)。
    c.execute("CREATE TABLE v (path TEXT PRIMARY KEY, vec BLOB)")
    titles = [r.get("title") or (r.get("title_guess") or "")[:120] for r in recs]
    for r, v in zip(recs, _embed(titles)):
        if v:
            c.execute("INSERT OR REPLACE INTO v VALUES (?,?)",
                      (r["path"], array.array("f", v).tobytes()))
    c.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO meta VALUES ('mtime',?)", (str(os.path.getmtime(INDEX)),))
    c.commit()
    print("papers.db: %d 件 (ベクトル %d)" %
          (len(recs), c.execute("SELECT count(*) FROM v").fetchone()[0]), file=sys.stderr)
    return c


def _db():
    if not os.path.exists(DB):
        return build_db()
    c = sqlite3.connect(DB)
    try:
        m = c.execute("SELECT v FROM meta WHERE k='mtime'").fetchone()
    except sqlite3.Error:
        m = None
    if not m or float(m[0]) < os.path.getmtime(INDEX):
        c.close()
        return build_db()                      # 索引が更新されていれば黙って作り直す
    return c


SEARCH_COLS = ("title", "first_author", "authors", "journal", "doi", "abstract", "folder", "text")
_OPS = re.compile(r'\b(AND|OR|NOT)\b|["()]|\b(author|%s):' % "|".join(SEARCH_COLS))


def _match(q):
    """検索語を FTS5 の MATCH 式にする。
    演算子(大文字の AND・OR・NOT、引用符、括弧、列名:)があれば、利用者の書いた式をそのまま使う(author: は authors:)。
    無ければ語に分けて引用し OR で繋ぐ。生の文字列は MATCH に渡せず('second-harmonic' は構文エラー)、
    FTS5 の既定は AND なので概念クエリが 0 件になるため。"""
    if _OPS.search(q):
        return re.sub(r'\bauthor:', "authors:", q)
    return " OR ".join('"%s"' % t.replace('"', '') for t in TERM.findall(q.lower()))


def _short_terms(q):
    """演算子の式の中の 2 文字以下の語。trigram の索引では何にも当たらない(黙って 0 件になる)。"""
    words = re.sub(r'\b(AND|OR|NOT)\b|\b\w+:|[()]', " ", re.sub(r'"[^"]*"', " ", q)).split()
    quoted = [w for w in re.findall(r'"([^"]*)"', q)]
    return [w for w in words + quoted if len(w) < 3]


def _visible(paths, hidden, n, show_all=False):
    """検索結果から、dedup が移した重複(利用者が消す予定のもの)を外して n 件に。--all なら外さない。"""
    return (paths if show_all else [p for p in paths if p not in hidden])[:n]


def _fts(c, q, n):
    m = _match(q)
    if not m:                                  # 2文字以下(trigram が索引できない)
        rows = c.execute("SELECT path FROM p WHERE title LIKE ? OR text LIKE ? LIMIT ?",
                         ("%" + q + "%", "%" + q + "%", n)).fetchall()
        return [r[0] for r in rows]
    w = ",".join(str(x) for x in W)
    rows = c.execute("SELECT path FROM p WHERE p MATCH ? ORDER BY bm25(p,%s) LIMIT ?" % w,
                     (m, n)).fetchall()         # bm25 は昇順(小さいほど良い)
    return [r[0] for r in rows]


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
                                     r.get("pages") or r.get("article_number")) if v)
    head = "%2d  %s  %s  %s" % (i, r.get("year") or "-", r.get("first_author") or "-", ref or "-")
    title = r.get("title") or "(%s) %s" % (r.get("rung"), r.get("filename"))
    return "\n".join([head, "    " + title, "    DOI: %s" % (r.get("doi") or "-"),
                      "    " + r["path"]] +
                     (["    Published: %s%s" % (r["published_doi"], " (%s)" % r["journal_ref"] if r.get("journal_ref") else "")]
                      if r.get("published_doi") else []) +
                     ["    Abstract: %s" % (r.get("abstract") or "-"), ""])


def search():
    """papers search <語> [--n=10] [--full | --json [--with-text]] [--all]
    語は OR・部分一致。AND・OR・NOT(大文字)、"語句"、括弧、列名:(title・author・journal・doi・abstract・folder・text)
    を書けば、その式で絞る。--all で dedup が移した重複も出す。"""
    q = " ".join(a for a in sys.argv[2:] if not a.startswith("--"))
    n = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--n=")), "10"))
    mode = next((a.split("=")[1] for a in sys.argv if a.startswith("--mode=")), "fts")
    if not q:
        sys.exit(search.__doc__)
    if _OPS.search(q) and _short_terms(q):
        print("注意: 2 文字以下の語は索引で引けないので、その語の条件は 0 件になる: %s" % ", ".join(_short_terms(q)),
              file=sys.stderr)
    hidden = {r["path"] for r in map(json.loads, open(INDEX)) if moved_aside(r)}
    k = n if "--all" in sys.argv else n + len(hidden)          # 外す分を見込んで多めに引く
    c = _db()
    try:
        if mode == "vec":
            paths = _vec(c, q, k)
        elif mode == "hybrid":
            rank = collections.defaultdict(float)
            for lst in (_fts(c, q, k * 3), _vec(c, q, k * 3)):
                for i, p in enumerate(lst):
                    rank[p] += 1.0 / (60 + i)      # RRF
            paths = [p for p, _ in sorted(rank.items(), key=lambda kv: -kv[1])[:k]]
        else:
            paths = _fts(c, q, k)
    except sqlite3.OperationalError as e:
        sys.exit("検索式の書き方が違う(%s)。\n"
                 "- AND・OR・NOT は大文字で、NOT の前には語が要る(laser NOT review)\n"
                 "- ハイフンや記号を含む語は引用符で囲む(\"second-harmonic\")\n"
                 "- 列名は title・author・journal・doi・abstract・folder・text" % e)
    paths = _visible(paths, hidden, n, "--all" in sys.argv)
    if "--json" in sys.argv or "--full" in sys.argv:
        idx = {r["path"]: r for r in map(json.loads, open(INDEX)) if r.get("path")}
        for i, p in enumerate((p for p in paths if p in idx), 1):
            print(json.dumps(compact(idx[p], "--with-text" in sys.argv), ensure_ascii=False) if "--json" in sys.argv
                  else _full_entry(i, idx[p]))
        return
    m = _match(q)
    for i, p in enumerate(paths, 1):
        row = c.execute("SELECT year, first_author, journal, title FROM p WHERE path=?",
                        (p,)).fetchone() or ("", "", "", "")
        sn = ""
        if m:
            r = c.execute("SELECT snippet(p,%d,'[',']','…',10) FROM p " % COLS.index("text") +
                          "WHERE p MATCH ? AND path=?", (m, p)).fetchone()
            sn = (r[0] if r else "").replace("\n", " ")[:110]
        print("%2d  %-4s %-16s %-26s %s" % (i, row[0] or "-", (row[1] or "-")[:16],
                                            (row[2] or "-")[:26], (row[3] or os.path.basename(p))[:60]))
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
    # IOP の表紙は他の論文の著者を並べるので、裏取りには使わない(表紙に並ぶ別の著者に着いた実例がある)
    assert cover_kind("Home Search Collections Journals About Contact us My IOPscience x") == "others", \
        "IOP の表紙を裏取りに使ってしまう"
    assert cover_kind("PROCEEDINGS OF SPIE SPIEDigitalLibrary.org/conference x") == "self"

    # --- Crossref の HTML。実物の値から ---
    assert clean_text("Laser &amp;amp; Photonics Reviews") == "Laser & Photonics Reviews", "二重エスケープが残る"
    assert clean_text("&lt;title&gt;Origin of defects in example nonlinear crystals&lt;/title&gt;") == \
        "Origin of defects in example nonlinear crystals", "エスケープされたタグが残る"
    assert clean_text("The two-level model =Si&lt;O_2:∙Xx absorption center") == \
        "The two-level model =Si<O_2:∙Xx absorption center", "本物の不等号を消している"
    # 抄録の本物の不等号。広い <[^>]+> だと < から > までの本文が消える(実データで 6 件)
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
    assert _visible(["/a", "/_dup/b", "/c"], {"/_dup/b"}, 2) == ["/a", "/c"], "移した重複を検索結果に出している"
    assert _visible(["/a", "/_dup/b"], {"/_dup/b"}, 5, show_all=True) == ["/a", "/_dup/b"]

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
    assert "text" not in compact({"text": "x" * 9, "doi": "d"}) and "text" in compact({"text": "x"}, True)

    # --- 確認用ページ: 外部の表題でページを壊さない。判定は候補が同じ行にだけ入れる ---
    page = review_page_html([{"cand_title": "a </script><img src=x onerror=alert(1)> & b"}], "<script>__DATA__</script>")
    assert "</script><img" not in page and page.count("</script>") == 1, "表題の </script> でページが壊れる"
    L = ["path\thint\tcand_doi\tok\n", "/a\th\t10.1/a\t\n", "/b\th\t10.1/b\t\n", "/c\th\t\t\n"]
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
    # 段ごとの扱い。dedup が段を見ずに unverified の DOI でファイルを動かしていた。
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

    print("selftest: ok")


CMDS = {"scan": scan, "add": add, "stats": stats, "rename": rename, "undo": undo,
        "review": review, "merge": merge, "pubver": pubver, "review-page": review_page, "review-ok": review_ok, "fix": fix, "info": info, "has": has, "cite": cite, "recent": recent, "relocate": relocate, "views": views, "move": move,
        "enrich": enrich, "dedup": dedup, "search": search, "index": build_db,
        "selftest": selftest}

NO_INDEX_OK = ("scan", "add", "selftest")     # 索引が無くても動くコマンド(作る側と、何も読まない検査)

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        sys.exit("papers <コマンド> …  コマンド: %s\n手順は %s" % (" ".join(CMDS), os.path.join(ROOT, "MANUAL.md")))
    if sys.argv[1] not in NO_INDEX_OK and not os.path.exists(INDEX):
        sys.exit("索引がまだ無い(%s)。最初は: papers add <論文 PDF のフォルダ>" % INDEX)
    CMDS[sys.argv[1]]()
