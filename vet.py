#!/usr/bin/env python3
"""ページの検証: Claude Haiku 4.5 に 1 ページ目の画像を見せ、主たる論文の表題と第一著者を読ませる。

役割は「指し示す」だけ(利用者の判断)。同定は決めさせない。読んだ表題が PDF に字面で存在し、しかも
解決済みの記録の表題と食い違うときだけ、その記録を人に回す(organize.vet_verdict)。未解決と unverified には
読んだ表題を review のヒントとして残す。測定と根拠は DESIGN.md「ページを見る」「ページの検証」。

有料の API を呼ぶので、引数なしでは見積もりを出すだけで何も投入しない(利用者の同意の決まり、CLAUDE.md)。

    uv run --with anthropic --python 3.12 python vet.py            # 対象の件数と費用の見積もり(課金なし)
    uv run --with anthropic --python 3.12 python vet.py --apply    # Batch API に投入し、終わるまで待って取り込む
    uv run --with anthropic --python 3.12 python vet.py --collect  # 中断した投入の続き(結果の取り込み)
    uv run --with anthropic --python 3.12 python vet.py --status   # 投入と処理の進み具合(課金なし)
    python3 vet.py --selftest                                       # SDK なしで動く。--apply 無しで投入しないこと等

キーは `ANTHROPIC_API_KEY=$(security find-generic-password -s anthropic-api-key -w)` で渡す。
"""
import base64, hashlib, json, os, random, re, sys, tempfile, time

import organize as O

MODEL = "claude-haiku-4-5"
PRICE_IN, PRICE_OUT = 1.0, 5.0          # ドル / 100 万トークン(Claude Haiku 4.5)。Batch API はこの半額
OUT_TOKENS = 68                          # 測定 86 回の出力の平均
BATCH_BYTES = 200 * 1024 * 1024          # 1 バッチ 256 MB の上限に余裕を残す
RESULTS = os.path.join(O.ROOT, "vet-results.jsonl")
STATE = os.path.join(O.ROOT, "vet-batches.json")

# DESIGN.md「ページを見る」で測ったプロンプトそのもの。変えたら測り直す。
PROMPT = """This image is page 1 of a PDF file from a researcher's paper collection.
Identify the MAIN article that this PDF file contains. Beware: page 1 may begin with the tail of a
different, preceding article (its references or acknowledgements), may be a download cover page,
or may carry ads, banners, logos or running headers. None of those are the main article's title.

Reply with JSON only, no other text:
{"page_type": one of "article_first_page", "starts_with_previous_article_tail", "cover_page",
               "supplementary_material", "slides_or_non_article", "other",
 "main_title": the main article's title copied exactly as printed (null if none visible),
 "first_author_surname": surname of the main article's first author as printed (null if none)}"""


def cid(path):
    return hashlib.sha1(path.encode()).hexdigest()[:32]


def page_png(path):
    """測定と同じ条件(1 ページ目、150 dpi、PNG)。"""
    d = tempfile.mkdtemp()
    try:
        O.sh(["pdftoppm", "-f", "1", "-l", "1", "-r", "150", "-singlefile", "-png", path, os.path.join(d, "p")],
             timeout=120)
        f = os.path.join(d, "p.png")
        return base64.standard_b64encode(open(f, "rb").read()).decode() if os.path.exists(f) else None
    finally:
        for x in os.listdir(d):
            os.remove(os.path.join(d, x))
        os.rmdir(d)


def page_text(r):
    """読んだ表題が字面で存在するかを見る本文。テキスト層が無ければ索引の OCR テキスト。"""
    t = O.sh(["pdftotext", "-f", "1", "-l", "1", r["path"], "-"])
    return t if len(t) > 200 else (r.get("text") or "")


def load_results():
    if not os.path.exists(RESULTS):
        return {}
    return {x["custom_id"]: x for x in map(json.loads, open(RESULTS)) if x.get("ok")}


def targets(recs, done):
    """解決済み(手入力を除く)、unverified、unresolved。補足資料とエラーは除く。取得済みは除く(二度課金しない)。
    取得済みは結果のキャッシュ(パスで引く)と、索引に反映済みの印(page_type)の両方で見る。rename でパスが
    変わるとキャッシュでは引けなくなるため(2026-09-23、一括 rename の前に気づいた)。"""
    return [r for r in recs if cid(r["path"]) not in done and "page_type" not in r and os.path.exists(r["path"])
            and ((O.renamable(r) and r.get("rung") != "manual") or r.get("rung") in ("unverified", "unresolved"))]


def request(r, img):
    return {"custom_id": cid(r["path"]),
            "params": {"model": MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img}},
                {"type": "text", "text": PROMPT}]}]}}


def estimate(client, ts, render, n=10):
    """課金なし。count_tokens の平均と、画像の大きさから分割するバッチ数を見積もる。"""
    sample = random.Random(20260923).sample(ts, min(n, len(ts)))
    toks, sizes = [], []
    for r in sample:
        img = render(r["path"])
        if not img:
            continue
        sizes.append(len(img))
        toks.append(client.messages.count_tokens(model=MODEL, messages=request(r, img)["params"]["messages"]).input_tokens)
    avg_in = sum(toks) / max(len(toks), 1)
    cost = len(ts) * (avg_in * PRICE_IN + OUT_TOKENS * PRICE_OUT) / 1e6
    nbatch = int(len(ts) * (sum(sizes) / max(len(sizes), 1)) // BATCH_BYTES) + 1
    return avg_in, cost, cost / 2, nbatch


def submit(client, ts, render):
    """PNG を描きながら合計 200 MB 以下ずつに分けて投入し、バッチ ID を状態ファイルに残す。"""
    ids, chunk, size, mapping = [], [], 0, {}
    def flush():
        if chunk:
            b = client.messages.batches.create(requests=list(chunk))
            ids.append(b.id)
            print("  投入: %s (%d 件)" % (b.id, len(chunk)), file=sys.stderr)
            O._atomic_write(STATE, [json.dumps({"batches": ids, "paths": mapping}, ensure_ascii=False)])
            chunk.clear()
    for r in ts:
        img = render(r["path"])
        if not img:
            continue
        req = request(r, img)
        if size + len(img) > BATCH_BYTES:
            flush(); size = 0
        chunk.append(req); size += len(img); mapping[req["custom_id"]] = r["path"]
    flush()
    return ids


def parse(text):
    t = text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(t)
    except Exception:
        pass
    # 表題の中の " がエスケープされずに返ることがある(実例 1/3200: 表題の中に "thick" のような引用符)。キーが 1 行ずつなら行で読む
    out = {}
    for k in ("page_type", "main_title", "first_author_surname"):
        m = re.search(r'^\s*"%s"\s*:\s*(?:null|"(.*)")\s*,?\s*$' % k, t, re.M)
        if m:
            out[k] = m.group(1)
    return out if out.get("main_title") else None


def collect(client, wait=True):
    """状態ファイルのバッチが終わるまで待ち、結果をキャッシュに足して索引に反映する。"""
    st = json.load(open(STATE))
    done = load_results()
    for bid in st["batches"]:
        while wait:
            b = client.messages.batches.retrieve(bid)
            if b.processing_status == "ended":
                break
            print("  待機: %s %s" % (bid, b.request_counts), file=sys.stderr)
            time.sleep(60)
        for res in client.messages.batches.results(bid):
            if res.result.type != "succeeded":
                continue
            msg = res.result.message
            j = parse("".join(x.text for x in msg.content if x.type == "text")) or {}
            done[res.custom_id] = {"custom_id": res.custom_id, "path": st["paths"].get(res.custom_id), "ok": bool(j),
                                   "page_type": j.get("page_type"), "main_title": j.get("main_title"),
                                   "first_author_surname": j.get("first_author_surname"),
                                   "usage": [msg.usage.input_tokens, msg.usage.output_tokens]}
    O._atomic_write(RESULTS, [json.dumps(x, ensure_ascii=False) + "\n" for x in done.values()])
    return apply_to_index(done)


def apply_to_index(done):
    """読んだ表題を索引に書き、vet_verdict が hold なら解決済みを unverified に戻す(元の段は vet_prev_rung)。"""
    recs, stamp = O.load_index()
    held = []
    for r in recs:
        x = done.get(cid(r["path"]))
        if not x or not x.get("ok"):
            continue
        r["page_title"] = x.get("main_title")
        r["page_author"] = x.get("first_author_surname")
        r["page_type"] = x.get("page_type")
        t = O.norm(O.clean_text(r["page_title"] or ""))
        r["page_title_literal"] = len(t) > 15 and t in O.norm(page_text(r))
        if O.vet_verdict(r) == "hold":
            r["vet_prev_rung"], r["rung"], r["held_by"] = r["rung"], "unverified", "vet"
            held.append(r)
    O.save_index(recs, stamp)
    print("反映 %d 件。人に回した解決済み %d 件" % (sum(1 for r in recs if cid(r["path"]) in done), len(held)))
    for r in held:
        print("  hold: %-40s | 記録: %s | ページ: %s" % (r["filename"][:40], (r.get("title") or "")[:50],
                                                       (r.get("page_title") or "")[:50]))
    return held


def bar(n, total, width=30):
    f = int(width * n / max(total, 1))
    return "[%s%s] %d/%d (%.0f%%)" % ("#" * f, "-" * (width - f), n, total, 100.0 * n / max(total, 1))


def status(client, recs):
    """課金なし。投入済みの件数(状態ファイル)と、バッチごとの処理の進み具合(Batch API の状態)。"""
    total = len(targets(recs, load_results()))
    if not os.path.exists(STATE):
        print("投入 " + bar(0, total)); return
    st = json.load(open(STATE))
    print("投入 " + bar(len(st["paths"]), total))
    done = err = sent = 0
    for bid in st["batches"]:
        c = client.messages.batches.retrieve(bid).request_counts
        n = c.processing + c.succeeded + c.errored + c.canceled + c.expired
        done += c.succeeded; err += c.errored + c.canceled + c.expired; sent += n
        print("  %s %s" % (bid[-8:], bar(c.succeeded, n, 20)))
    print("処理 " + bar(done, sent) + ("  失敗 %d" % err if err else ""))


def main(argv, client, recs, render):
    if "--status" in argv:
        return status(client, recs)
    if "--collect" in argv:
        return collect(client)
    ts = targets(recs, load_results())
    avg_in, cost, cost_batch, nbatch = estimate(client, ts, render)
    print("対象 %d 件。1 件あたり入力 約 %.0f トークン。費用の見積もり: 通常 %.2f ドル / Batch API %.2f ドル。"
          "バッチ %d 個に分けて投入" % (len(ts), avg_in, cost, cost_batch, nbatch))
    if "--apply" not in argv:
        print("見積もりのみ(課金なし)。実行するには利用者の同意を得てから --apply")
        return None
    submit(client, ts, render)
    return collect(client)


def selftest():
    """SDK もネットワークも使わない。索引・状態・結果のファイルは一時ディレクトリに向け、本物には触れない。
    --apply が無ければ投入しないこと、あれば投入すること、二度課金しないことを確かめる。"""
    global STATE, RESULTS
    raw = ('```json\n{\n  "page_type": "article_first_page",\n  "main_title": "Example conversion of short pulses in '
           '"thick" model crystals",\n  "first_author_surname": "Sample"\n}\n```')
    assert parse(raw) == {"page_type": "article_first_page", "first_author_surname": "Sample",
                          "main_title": 'Example conversion of short pulses in "thick" model crystals'}, \
        "表題の中の引用符で答えを捨てている(表題に引用符のある型)"
    assert parse('{"page_type": "slide", "main_title": null}') == {"page_type": "slide", "main_title": None}
    d = tempfile.mkdtemp()
    O.INDEX, STATE, RESULTS = os.path.join(d, "i.jsonl"), os.path.join(d, "state.json"), os.path.join(d, "r.jsonl")
    recs = [{"path": __file__, "rung": "unresolved", "filename": "x.pdf"}]
    O._atomic_write(O.INDEX, [json.dumps(r) + "\n" for r in recs])

    class Fake:
        created = 0
        class messages:
            @staticmethod
            def count_tokens(**kw):
                return type("T", (), {"input_tokens": 1700})()
            class batches:
                @staticmethod
                def create(requests):
                    Fake.created += len(requests)
                    return type("B", (), {"id": "b1"})()
                @staticmethod
                def retrieve(bid):
                    return type("B", (), {"processing_status": "ended"})()
                @staticmethod
                def results(bid):
                    return []
    render = lambda p: "QUJD"
    main(["vet.py"], Fake, recs, render)
    assert Fake.created == 0, "--apply が無いのに投入した(利用者の同意の前に課金する)"
    main(["vet.py", "--apply"], Fake, recs, render)
    assert Fake.created == 1, "--apply で投入できていない(検査が何も見ていない)"
    assert not targets([dict(recs[0], rung="supplement")], {}), "補足資料を対象にしている"
    assert not targets(recs, {cid(__file__): {}}), "取得済みをもう一度課金しようとしている"
    assert not targets([dict(recs[0], page_type="article_first_page", page_title=None)], {}), \
        "索引に反映済みの記録を、パスが変わった(rename)だけでもう一度課金しようとしている"
    print("vet selftest: ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        import anthropic
        main(sys.argv, anthropic.Anthropic(), O.load_index()[0], page_png)
