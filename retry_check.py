#!/usr/bin/env python3
"""_fetch の結合検査。偽サーバ相手に 429/503/404 を返させて振る舞いを見る。

selftest に入れないのは意図(CLAUDE.md): ここはソケットを開くので落ちる理由が多い。
純関数 _retry_plan の検査は selftest 側にある。こちらは _fetch の実挙動
—— 再送回数・全体の減速・連続失敗での中断 —— を見る。

本物の Crossref / OpenAlex に 429 を出させるのは、まさに避けたい行為そのものなので
相手は localhost に限る。   実行: python3 retry_check.py
"""
import http.server, threading, time, urllib.error

import organize as O

_plan, _hits = [], []


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        _hits.append(time.time())
        code = _plan.pop(0) if _plan else 200
        body = b'{"ok": 1}'
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if code == 429 and getattr(self.server, "retry_after", None):
            self.send_header("Retry-After", self.server.retry_after)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def setup(plan, retry_after=None):
    """サーバを立て直し、返す status の列を仕込む。sleep は記録するだけにする
    (本当に 1+2+4+8+16 秒待つと検査が 31 秒かかり、誰も回さなくなる)。"""
    del _plan[:], _hits[:]
    _plan.extend(plan)
    O._pace.update({"mult": 1.0, "exhausted_in_a_row": 0})
    srv.retry_after = retry_after
    waits = []
    O.time.sleep = lambda s: waits.append(round(s, 3))
    return waits


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/x" % srv.server_address[1]
_real_sleep = time.sleep
ok = 0


def check(name, cond, detail=""):
    global ok
    if cond:
        ok += 1
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        raise SystemExit(1)


try:
    # 1. 429 を 5 回浴びてから成功する。再送し、待ちは 1,2,4,8,16 秒
    w = setup([429] * 5)
    body = O._fetch(URL)
    backoff = [x for x in w if x in (1.0, 2.0, 4.0, 8.0, 16.0)]
    check("429×5 のあと成功する", body == b'{"ok": 1}')
    check("リクエストは 6 回(初回+5 再送)", len(_hits) == 6, "実際 %d 回" % len(_hits))
    check("待ちが 1,2,4,8,16 秒", backoff == [1.0, 2.0, 4.0, 8.0, 16.0], str(w))

    # 2. 429 が止まらない → 諦める。相手を無限に叩かない
    w = setup([429] * 20)
    try:
        O._fetch(URL)
        check("使い切ったら例外", False, "例外が出ない")
    except urllib.error.HTTPError:
        check("使い切ったら例外", True)
    check("再送は 5 回で打ち切る", len(_hits) == 6, "実際 %d 回" % len(_hits))

    # 3. 429 はパス全体を減速させる。1 件を叩き直すだけでは「送る量を減らせ」に反する
    check("429 で全体のペースが落ちる", O._pace["mult"] > 1.0, str(O._pace))

    # 4. 連続 ABORT_AFTER 件でパスを止める(18 時間グラインドさせない)
    w = setup([429] * 200)
    aborted = False
    for _ in range(O.ABORT_AFTER):
        try:
            O._fetch(URL)
        except O.RateLimitAbort:
            aborted = True
            break
        except urllib.error.HTTPError:
            pass
    check("連続 %d 件で中断する" % O.ABORT_AFTER, aborted)

    # 5. 404 は待っても変わらない。1 回で諦める
    w = setup([404])
    try:
        O._fetch(URL)
        check("404 で例外", False, "例外が出ない")
    except urllib.error.HTTPError:
        check("404 で例外", True)
    check("404 をリトライしない", len(_hits) == 1, "実際 %d 回" % len(_hits))

    # 6. 503 も再送する(相手の都合。こちらのペースは原因ではない)
    w = setup([503, 503])
    check("503×2 のあと成功する", O._fetch(URL) == b'{"ok": 1}')
    check("503 を再送する", len(_hits) == 3, "実際 %d 回" % len(_hits))

    # 8. 成功したら失敗カウンタが戻る。戻らないと、離れた時点の失敗が累積して
    #    「連続」でないものを連続と誤認し、正常なパスを途中で止めてしまう。
    w = setup([429] * 6)
    try:
        O._fetch(URL)
    except urllib.error.HTTPError:
        pass
    check("使い切りで失敗カウンタが上がる", O._pace["exhausted_in_a_row"] == 1, str(O._pace))
    O._fetch(URL)
    check("成功したら失敗カウンタが戻る", O._pace["exhausted_in_a_row"] == 0, str(O._pace))

    # 7. Retry-After が来たら自前のバックオフより優先する
    w = setup([429], retry_after="7")
    O._fetch(URL)
    check("Retry-After に従う", 7.0 in w, str(w))

    # 9. 同定の 4 つの問い合わせ: 取れなかった(503 を使い切る)ものは O._FAILED に残り、「記録が無い」(404)は残らない。
    #    混ぜると、API の遅延 1 回で PDF が unresolved のまま索引に残る(scan は索引にあるパスを飛ばす)。
    import tempfile
    real_fetch, real_cache = O._fetch, O.CACHE
    O._fetch = lambda url, **k: real_fetch(URL, **{x: v for x, v in k.items() if x != "timeout"})   # どの URL も偽サーバへ
    try:
        for name, call in (("from_crossref", lambda: O.from_crossref("10.1/x")), ("from_arxiv", lambda: O.from_arxiv("0000.00000")),
                           ("by_bibliographic", lambda: O.by_bibliographic("t " * 40)),
                           ("by_title", lambda: O.by_title("A title long enough to search", ""))):
            for plan, failed in (([503] * 6, True), ([404], False)):
                with tempfile.TemporaryDirectory() as d:          # キャッシュは毎回空(前の応答を読まない)
                    O.CACHE = d
                    setup(plan)
                    del O._FAILED[:]
                    got = call()
                    check("%s: %d は%s" % (name, plan[0], "取れなかったと数える" if failed else "記録が無い(数えない)"),
                          got is None and bool(O._FAILED) == failed, "返り値 %r、_FAILED %r" % (got, O._FAILED))
    finally:
        O._fetch, O.CACHE = real_fetch, real_cache
finally:
    O.time.sleep = _real_sleep
    srv.shutdown()

print("retry_check: ok (%d 項目)" % ok)
