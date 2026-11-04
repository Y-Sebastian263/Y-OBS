#!/usr/bin/env python3
"""Y-OBS サーバーAPI(視聴人数・コメント・管理ログイン・アクセス禁止)

Caddy 経由で https://<host>/count と https://<host>/api/... に公開される。

  GET  /count                 同時視聴人数 {"viewers","hls","webrtc","live"}
  POST /api/hello             端末の挨拶(1分ごと)。{"device","name","ua","viewing"} → {"banned"}
  POST /api/comment           コメント送信 {"device","name","text"}
  POST /api/login             管理ログイン {"device","pin"} → {"token"}
  GET  /api/admin/state       管理画面のデータ(Authorization: Bearer <token>)
  POST /api/admin/title        配信タイトルを設定(配信終了で自動リセット)
  POST /api/admin/mute         コメントのミュート {"device","on"}(ミュート中の端末のコメントは記録しない)
  POST /api/admin/logout
  POST /api/consent            年齢制限の注意への同意 {"device","version"}(記録が残る)
  POST /api/admin/rating       年齢制限(R18)の設定 {"on","text"}
  GET  /api/admin/visits?from=&to=[&device=]   訪問履歴(端末ごと・期間指定)
  GET  /api/admin/streams                      配信の一覧
  GET  /api/admin/stream?id=                   配信の詳細(1分ごとの視聴者数・コメント数)

ブラウザからは MAC アドレスや端末名は取れないため、ページが端末ごとに作る
「端末ID」と、接続元の IP アドレスで端末を見分ける。
暗証番号を 1 時間に 3 回間違えると、その端末IDと IP アドレスを永久に禁止する。
"""
import bisect
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

DATA = os.environ.get("YOBS_DATA", "/var/lib/y-obs")
LOG = os.environ.get("YOBS_HLS_LOG", "/var/log/caddy/y-obs-hls.log")
API = os.environ.get("YOBS_API", "http://127.0.0.1:9997")
STREAM = os.environ.get("YOBS_STREAM", "live")
PORT = int(os.environ.get("YOBS_API_PORT", "8897"))

COUNT_WINDOW = 8            # 秒: この間に映像を取りに来た人を視聴中とみなす
ONLINE_WINDOW = 90          # 秒: この間に挨拶が来た端末を「アクセス中」とみなす
TAB_WINDOW = 45             # 秒: この間に挨拶が来たタブを「表示中」とみなす(挨拶は30秒ごと)
FAIL_WINDOW = 3600          # 1時間
FAIL_LIMIT = 3
TOKEN_TTL = 12 * 3600
COMMENT_MAX = 300
NAME_MAX = 30
COMMENT_INTERVAL = 3        # 秒: 同じ端末の連投制限
COMMENTS_KEEP = 200000      # 履歴の集計に使うので多めに保持(画面には最新300件)

lock = threading.Lock()
clients = {}                # device -> {name, ip, ua, label, first, last, viewing}
fails = []                  # [(ts, device, ip)]
tokens = {}                 # token -> expire
last_comment = {}           # device -> ts
_count_cache = {"t": 0.0, "data": None}


# ---------------------------------------------------------------- 保存まわり
def path(name):
    return os.path.join(DATA, name)


def load_json(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name, data):
    tmp = path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(name))


bans = load_json("bans.json", {"devices": {}, "ips": {}})
names = load_json("names.json", {})      # device -> {name, ip, ua, label, updated}

# Y++ public text profile. Kept separately from streaming data.
profile_record = load_json("yplus_profile.json", {"revision": 0, "profile": None})
PROFILE_ORIGINS = set(filter(None, (x.strip() for x in os.environ.get(
    "YOBS_PROFILE_ORIGINS", "https://y-tools.github.io"
).split(","))))

def validate_profile_links(value):
    if not isinstance(value, list) or len(value) > 10:
        raise ValueError("プロフィールリンクが不正です")
    out = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("プロフィールリンクが不正です")
        label = item.get("label")
        href = item.get("href")
        if not isinstance(label, str) or not isinstance(href, str):
            raise ValueError("プロフィールリンクが不正です")
        if len(label) > 40 or len(href) > 500 or not href.startswith("https://"):
            raise ValueError("プロフィールリンクが不正です")
        out.append({"label": label, "href": href})
    return out


def normalize_profile(value):
    """Accept only plain text and a bounded set of editable blocks."""
    if not isinstance(value, dict):
        raise ValueError("プロフィールの形式が不正です")
    def limited(value, length, field):
        if not isinstance(value, str) or len(value) > length:
            raise ValueError(field + "が長すぎるか不正です")
        return value
    blocks = value.get("blocks", [])
    if not isinstance(blocks, list) or len(blocks) > 24:
        raise ValueError("ブロックは最大24個です")
    cleaned = []
    ids = set()
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("ブロックが不正です")
        ident = limited(block.get("id", ""), 80, "識別子")
        if not ident or not all(c.isascii() and (c.isalnum() or c in "-_") for c in ident) or ident in ids:
            raise ValueError("ブロック識別子が不正です")
        ids.add(ident)
        kind = block.get("type")
        if kind not in ("weekly", "basic"):
            raise ValueError("ブロックの種類が不正です")
        cleaned.append({
            "id": ident,
            "type": kind,
            "title": limited(block.get("title", ""), 80, "タイトル"),
            "content": limited(block.get("content", ""), 1800, "本文"),
        })
    return {
        "displayName": limited(value.get("displayName", ""), 70, "表示名"),
        "handle": limited(value.get("handle", ""), 100, "補助表示"),
        "statement": limited(value.get("statement", ""), 160, "キャッチコピー"),
        "description": limited(value.get("description", ""), 1000, "紹介文"),
        "links": validate_profile_links(value.get("links", [])),
        "blocks": cleaned,
    }



def load_comments():
    out = []
    try:
        with open(path("comments.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out[-COMMENTS_KEEP:]


comments = load_comments()
stream = load_json("stream.json", {"title": ""})                # 配信タイトル(配信終了でリセット)
mutes = load_json("mutes.json", {"devices": {}, "keys": {}})   # コメントのミュート
TITLE_MAX = 60
RATING_TEXT = "ホラーゲームなどのショッキングな内容(暴力・流血表現など)を含みます。18歳以上の方のみご視聴ください。"
stream.setdefault("rating", {"on": False, "text": RATING_TEXT, "version": ""})   # 年齢制限(R18)の設定


def rating_public():
    r = stream.get("rating", {})
    return {"on": bool(r.get("on")), "text": r.get("text") or RATING_TEXT, "version": r.get("version", "")}


def append_comment(c):
    comments.append(c)
    del comments[:-COMMENTS_KEEP]
    with open(path("comments.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")
    stream_comment(c["ts"])


def load_jsonl(name):
    out = []
    try:
        with open(path(name), encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def append_jsonl(name, obj):
    with open(path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 端末の見分け
def device_label(ua):
    ua = ua or ""
    if "iPhone" in ua:
        os_ = "iPhone"
    elif "iPad" in ua:
        os_ = "iPad"
    elif "Android" in ua:
        import re
        m = re.search(r"Android ([\d.]+)", ua)
        os_ = "Android " + (m.group(1) if m else "")
    elif "Windows" in ua:
        os_ = "Windows PC"
    elif "Macintosh" in ua:
        os_ = "Mac"
    elif "Linux" in ua:
        os_ = "Linux"
    else:
        os_ = "不明な端末"
    if "Line/" in ua:
        br = "LINE内ブラウザ"
    elif "Instagram" in ua:
        br = "Instagram内ブラウザ"
    elif "Twitter" in ua:
        br = "X内ブラウザ"
    elif "Discord" in ua:
        br = "Discord内ブラウザ"
    elif "EdgiOS" in ua or "Edg/" in ua or "EdgA" in ua:
        br = "Edge"
    elif "CriOS" in ua or "Chrome/" in ua:
        br = "Chrome"
    elif "FxiOS" in ua or "Firefox/" in ua:
        br = "Firefox"
    elif "Safari/" in ua:
        br = "Safari"
    else:
        br = "ブラウザ"
    return f"{os_.strip()} / {br}"


def clean(s, n):
    s = str(s or "").replace("\r", " ").replace("\n", " ").strip()
    return s[:n]


def mute_key(ip, hw):
    return f"{ip}|{hw}" if hw else ""


# ---------------------------------------------------------------- 端末のつながり
# 基本の単位は「ブラウザID」。次のどちらかを満たしたブラウザ同士を同じ端末として結びつけ、
# 一度結びついたらずっと覚えておく(あとで IP や名前が変わっても同じ端末のまま)。
#   1) 端末キーが同じ + 同じ時間に同じ IP から来ている
#   2) 端末キーが同じ + 同じ名前を登録したことがある
# 名前だけ・IP だけでは結びつけない。
seen = load_json("seen.json", {})     # ブラウザID -> {hw, label, ua, first, last, names[], ips[]}
links = load_json("links.json", {})   # ブラウザID -> 親のブラウザID(つながりの木)
_seen_saved = [0.0]
LINK_WINDOW = 60                      # 秒: 「同じ時間」とみなす幅


def find(d):
    root = d
    while links.get(root, root) != root:
        root = links[root]
    while links.get(d, d) != root:     # 経路を短くしておく
        nxt = links[d]
        links[d] = root
        d = nxt
    return root


def union(a, b):
    ra, rb = find(a), find(b)
    if ra == rb:
        return
    # 先に見つかった方を代表にする(端末番号がなるべく変わらないように)
    if seen.get(rb, {}).get("first", 0) < seen.get(ra, {}).get("first", 0):
        ra, rb = rb, ra
    links[rb] = ra
    save_json("links.json", links)


def remember(device, hw, ip, ua, name, now):
    """ブラウザの情報を記録し、条件を満たす他のブラウザと結びつける"""
    if not device:
        return
    s = seen.setdefault(device, {"hw": hw, "first": now, "names": [], "ips": []})
    changed = device not in links and not s.get("label")
    if hw and s.get("hw") != hw:
        s["hw"] = hw
        changed = True
    s["label"], s["ua"], s["last"] = device_label(ua), ua, now
    if ip and ip not in s["ips"]:
        s["ips"] = (s["ips"] + [ip])[-10:]
        changed = True
    if name and name != "名無し" and name not in s["names"]:
        s["names"] = (s["names"] + [name])[-10:]
        changed = True
    if changed or now - _seen_saved[0] > 60:
        save_json("seen.json", seen)
        _seen_saved[0] = now
    if not hw:
        return
    for other, c in clients.items():                       # 1) 同じ時間に同じ IP
        if other != device and c.get("hw") == hw and c.get("ip") == ip and now - c["last"] <= LINK_WINDOW:
            union(device, other)
    if name and name != "名無し":                            # 2) 同じ名前の登録歴
        for other, o in seen.items():
            if other != device and o.get("hw") == hw and name in o.get("names", []):
                union(device, other)


def is_muted(device, ip="", hw=""):
    """その端末(結びついた全ブラウザ)がミュートされているか"""
    if device and device in mutes["devices"]:
        return True
    if device:
        root = find(device)
        if any(find(m) == root for m in mutes["devices"]):
            return True
    return bool(hw) and mute_key(ip, hw) in mutes["keys"]


def is_banned(device, ip):
    return (device and device in bans["devices"]) or (ip and ip in bans["ips"])


# ---------------------------------------------------------------- 視聴人数
def api_get(p):
    try:
        with urllib.request.urlopen(API + p, timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def count_hls(now):
    try:
        size = os.path.getsize(LOG)
        with open(LOG, "rb") as f:
            f.seek(max(0, size - 3_000_000))
            lines = f.read().splitlines()
    except OSError:
        return 0
    seen = set()
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        ts = e.get("ts", 0)
        if now - ts > COUNT_WINDOW + 60:
            break
        if now - ts > COUNT_WINDOW:
            continue
        req = e.get("request", {})
        uri = req.get("uri", "")
        if f"/{STREAM}/" not in uri or e.get("status", 0) >= 400:
            continue
        sid = (parse_qs(urlsplit(uri).query).get("session") or [""])[0]
        if sid:
            seen.add(sid)
    return len(seen)


def count_webrtc():
    data = api_get("/v3/webrtcsessions/list?itemsPerPage=1000") or {}
    return sum(1 for s in data.get("items", [])
               if s.get("path") == STREAM and s.get("state") != "publish")


def count_snapshot():
    now = time.time()
    if _count_cache["data"] and now - _count_cache["t"] < 1:
        return _count_cache["data"]
    hls, rtc = count_hls(now), count_webrtc()
    live = bool((api_get(f"/v3/paths/get/{STREAM}") or {}).get("ready"))
    data = {"viewers": hls + rtc, "hls": hls, "webrtc": rtc, "live": live,
            "title": stream.get("title", "") if live else "", "rating": rating_public()}
    _count_cache.update(t=now, data=data)
    return data


# ---------------------------------------------------------------- 訪問履歴
# ページからの挨拶(30秒ごと)が VISIT_GAP 秒途切れたら「退室」とみなして 1 回の訪問を確定する。
VISIT_GAP = 180
visits = load_jsonl("visits.jsonl")                 # [{device, start, end}]
visits_open = load_json("visits_open.json", {})     # ブラウザID -> {start, last}
consents = load_jsonl("consents.jsonl")              # 年齢制限への同意の記録 [{ts, device, ip, label, version, text}]


def consents_of(device):
    return [{"ts": c["ts"], "version": c["version"], "ip": c.get("ip", "")} for c in consents if c.get("device") == device]


def visit_touch(device, now):
    v = visits_open.get(device)
    if v and now - v["last"] <= VISIT_GAP:
        v["last"] = now
        return
    if v:
        close_visit(device, v)
    visits_open[device] = {"start": now, "last": now}


def close_visit(device, v):
    rec = {"device": device, "start": v["start"], "end": v["last"]}
    visits.append(rec)
    append_jsonl("visits.jsonl", rec)


def close_stale_visits(now):
    for dev, v in list(visits_open.items()):
        if now - v["last"] > VISIT_GAP:
            close_visit(dev, v)
            del visits_open[dev]
    save_json("visits_open.json", visits_open)


def visits_report(t_from, t_to, only=None):
    """期間内の訪問を端末(つながったブラウザのまとまり)ごとに集計する"""
    now = time.time()
    items = [dict(v, open=False) for v in visits]
    items += [{"device": d, "start": v["start"], "end": v["last"], "open": True} for d, v in visits_open.items()]
    groups = {}
    for v in items:
        if v["end"] < t_from or v["start"] >= t_to:
            continue
        root = find(v["device"])
        if only and root != only:
            continue
        g = groups.setdefault(root, {"root": root, "key": root[:8], "visits": [], "total": 0, "comments": 0})
        g["visits"].append(v)
    # 端末の情報とコメント数(端末ごとのコメント時刻を一度だけ並べて数える)
    by_dev = {}
    for c in comments:
        by_dev.setdefault(c.get("device"), []).append(c["ts"])
    for root, g in groups.items():
        members = [d for d in seen if find(d) == root] or [root]
        names, labels = [], []
        for d in members:
            sv = seen.get(d, {})
            for n in sv.get("names", []):
                if n not in names:
                    names.append(n)
            if sv.get("label") and sv["label"] not in labels:
                labels.append(sv["label"])
        g["names"], g["labels"] = names, labels
        # 端末の詳細(今つながっていなくても記録から出す)
        g["members"] = []
        ips = []
        for d in members:
            sv = seen.get(d, {})
            for ip in sv.get("ips", []):
                if ip not in ips:
                    ips.append(ip)
            g["members"].append({
                "device": d, "label": sv.get("label", ""), "browser": sv.get("label", "").split(" / ")[-1],
                "ua": sv.get("ua", ""), "first": sv.get("first", 0), "last": sv.get("last", 0),
                "names_hist": sv.get("names", []), "ips_hist": sv.get("ips", []), "muted": bool(is_muted(d)),
                "consents": consents_of(d),
            })
        g["ips"] = ips
        g["first"] = min((m["first"] for m in g["members"] if m["first"]), default=0)
        mset = set(members)
        g["visits"].sort(key=lambda v: v["start"], reverse=True)
        for v in g["visits"]:
            v["duration"] = int(v["end"] - v["start"])
            v["comments"] = sum(bisect.bisect_right(by_dev.get(d, []), v["end"] + 60) -
                                bisect.bisect_left(by_dev.get(d, []), v["start"] - 5) for d in mset)
            v["browser"] = seen.get(v["device"], {}).get("label", "").split(" / ")[-1]
            g["total"] += v["duration"]
            g["comments"] += v["comments"]
        g["count"] = len(g["visits"])
        g["last"] = max(v["end"] for v in g["visits"])
        g["online"] = any(v["open"] and now - v["end"] <= VISIT_GAP for v in g["visits"])
    out = sorted(groups.values(), key=lambda g: g["last"], reverse=True)
    return out


# ---------------------------------------------------------------- 配信の記録
# 配信開始〜終了を 1 回の配信として記録。終了から STREAM_MERGE 秒以内につけ直したら同じ配信の続き。
# 1 分ごとに視聴者数とコメント数を記録して、盛り上がりのヒートマップに使う。
STREAM_MERGE = 3600
streams = load_json("streams.json", [])
_stream_saved = [0.0]


def active_stream():
    return streams[-1] if streams and streams[-1].get("end") is None else None


def stream_on(now):
    if active_stream():
        return
    last = streams[-1] if streams else None
    if last and now - last["end"] <= STREAM_MERGE:
        last["end"] = None                      # 1 時間以内のつけ直し → 同じ配信の続き
        last["segments"].append([now, None])
    else:
        streams.append({"id": str(int(now)), "start": now, "end": None, "title": "",
                        "segments": [[now, None]], "buckets": {}})
    save_json("streams.json", streams)


def stream_off(now):
    s = active_stream()
    if not s:
        return
    s["end"] = now
    s["segments"][-1][1] = now
    save_json("streams.json", streams)


def bucket(s, ts):
    return s["buckets"].setdefault(str(int((ts - s["start"]) // 60)), {"vmax": 0, "vsum": 0, "n": 0, "c": 0})


def stream_sample(now, viewers):
    s = active_stream()
    if not s:
        return
    b = bucket(s, now)
    b["vmax"] = max(b["vmax"], viewers)
    b["vsum"] += viewers
    b["n"] += 1
    if stream.get("title"):
        s["title"] = stream["title"]
    if now - _stream_saved[0] > 60:
        save_json("streams.json", streams)
        _stream_saved[0] = now


def stream_comment(ts):
    s = active_stream()
    if s:
        bucket(s, ts)["c"] += 1


def stream_summary(s):
    now = time.time()
    dur = sum((e or now) - b for b, e in s["segments"])
    bs = s["buckets"].values()
    return {"id": s["id"], "start": s["start"], "end": s["end"], "title": s.get("title", ""),
            "duration": int(dur), "restarts": len(s["segments"]) - 1,
            "peak": max((b["vmax"] for b in bs), default=0),
            "comments": sum(b["c"] for b in bs), "live": s["end"] is None}


def page_viewers(now):
    """ページを開いて再生中の端末数(サーバーの映像カウントの補助)"""
    roots = set()
    for dev, c in clients.items():
        if c.get("viewing") and now - c["last"] <= 45:
            roots.add(find(dev))
    return len(roots)


# ---------------------------------------------------------------- 見張り役(1秒ごと)
def watch_stream():
    """配信の開始・終了を見張る。終了でタイトルをリセット、配信の記録、訪問の整理"""
    was_live = False
    tick = 0
    while True:
        now = time.time()
        live = bool((api_get(f"/v3/paths/get/{STREAM}") or {}).get("ready"))
        with lock:
            if live and not was_live:
                stream_on(now)
            if was_live and not live:
                stream["title"] = ""
                save_json("stream.json", stream)
                _count_cache["data"] = None
                stream_off(now)
            if live and tick % 10 == 0:
                snap = count_snapshot()
                stream_sample(now, max(snap["viewers"], page_viewers(now)))
            if tick % 30 == 0:
                close_stale_visits(now)
        was_live = live
        tick += 1
        time.sleep(1)


# ---------------------------------------------------------------- 暗証番号
def check_pin(pin):
    conf = load_json("admin_pin.json", None)
    if not conf or not pin or not str(pin).isdigit():
        return False
    h = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), bytes.fromhex(conf["salt"]), conf["iter"]).hex()
    return hmac.compare_digest(h, conf["hash"])


def valid_token(tok):
    now = time.time()
    for t in [t for t, exp in tokens.items() if exp < now]:
        tokens.pop(t, None)
    return bool(tok) and tok in tokens


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Y-OBS"

    def ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0])

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 20000)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, TypeError):
            return {}

    def token(self):
        a = self.headers.get("Authorization", "")
        return a[7:] if a.startswith("Bearer ") else ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def do_GET(self):
        p = urlsplit(self.path).path.rstrip("/")
        if p in ("/count", ""):
            return self.send_json(200, count_snapshot())
        if p == "/api/profile":
            with lock:
                return self.send_json(200, {
                    "profile": profile_record.get("profile"),
                    "revision": profile_record.get("revision", 0)
                })
        if p.startswith("/api/admin/") and p != "/api/admin/state":
            q = parse_qs(urlsplit(self.path).query)
            arg = lambda k, dflt="": (q.get(k) or [dflt])[0]
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                if p == "/api/admin/visits":
                    try:
                        t_from, t_to = float(arg("from", "0")), float(arg("to", str(time.time() + 1)))
                    except ValueError:
                        return self.send_json(400, {"error": "期間が不正です"})
                    only = find(arg("device")) if arg("device") else None
                    return self.send_json(200, {"groups": visits_report(t_from, t_to, only)})
                if p == "/api/admin/streams":
                    return self.send_json(200, {"streams": [stream_summary(s) for s in reversed(streams[-200:])]})
                if p == "/api/admin/stream":
                    s = next((s for s in streams if s["id"] == arg("id")), None)
                    if not s:
                        return self.send_json(404, {"error": "見つかりません"})
                    return self.send_json(200, dict(stream_summary(s), segments=s["segments"], buckets=s["buckets"]))
            return self.send_json(404, {"error": "not found"})
        if p == "/api/admin/state":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                now = time.time()
                # つながっているブラウザ同士を 1 台にまとめる
                groups = {}
                for dev, c in clients.items():
                    if now - c["last"] > ONLINE_WINDOW:
                        continue
                    root = find(dev)
                    g = groups.setdefault(root, {
                        "key": root[:8], "root": root, "ip": c["ip"], "ips": [], "names": [], "os": c["label"].split(" / ")[0],
                        "browsers": [], "devices": [], "last": 0, "viewing": False, "ua": c["ua"],
                        "tabs": 0, "tab_detail": {}, "items": [], "first": seen.get(root, {}).get("first", c.get("first", now)),
                        "muted": bool(is_muted(dev)),
                    })
                    if c["ip"] not in g["ips"]:
                        g["ips"].append(c["ip"])
                    if c["name"] and c["name"] not in g["names"]:
                        g["names"].append(c["name"])
                    br = c["label"].split(" / ")[-1]
                    if br not in g["browsers"]:
                        g["browsers"].append(br)
                    g["devices"].append(dev[:8])
                    g["last"] = max(g["last"], c["last"])
                    g["viewing"] = g["viewing"] or c["viewing"]
                    ent = visits_open.get(dev, {}).get("start", c.get("first", now))
                    g["entered"] = min(g.get("entered", ent), ent)
                    n_tabs = sum(1 for ts in c.get("tabs", {}).values() if now - ts <= TAB_WINDOW)
                    sv = seen.get(dev, {})
                    g["items"].append({
                        "device": dev, "browser": br, "label": c["label"], "name": c["name"], "ua": c["ua"],
                        "ip": c["ip"], "tabs": n_tabs, "first": sv.get("first", c.get("first", now)),
                        "last": c["last"], "viewing": c["viewing"], "muted": bool(is_muted(dev)),
                        "names_hist": sv.get("names", []), "ips_hist": sv.get("ips", []),
                        "consents": consents_of(dev),
                    })
                    if n_tabs:
                        g["tabs"] += n_tabs
                        g["tab_detail"][br] = g["tab_detail"].get(br, 0) + n_tabs
                online = []
                for g in groups.values():
                    g["ago"] = int(now - g["last"])
                    online.append(g)
                online.sort(key=lambda c: (c["entered"], c["key"]))   # 入室順(順番は動かない)
                data = {
                    "clients": online,
                    "comments": [dict(c, muted=bool(is_muted(c.get("device"), c.get("ip"), c.get("hw"))),
                                      gkey=find(c.get("device", ""))[:8])
                                 for c in reversed(comments[-300:])],
                    "mutes": sorted(mutes["devices"].values(), key=lambda m: m["ts"], reverse=True),
                    "bans": {"devices": len(bans["devices"]), "ips": len(bans["ips"])},
                    "count": count_snapshot(),
                    "title": stream.get("title", ""),
                    "rating": rating_public(),
                    "now": now,
                }
            return self.send_json(200, data)
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        p = urlsplit(self.path).path.rstrip("/")
        d = self.body()
        ip = self.ip()
        device = clean(d.get("device"), 64)
        now = time.time()

        if p == "/api/hello":
            with lock:
                if is_banned(device, ip):
                    return self.send_json(200, {"banned": True})
                if device:
                    ua = clean(self.headers.get("User-Agent"), 300)
                    prev = clients.get(device, {})
                    tabs = {t: ts for t, ts in prev.get("tabs", {}).items() if now - ts <= TAB_WINDOW}
                    hw = clean(d.get("hw"), 16)
                    remember(device, hw, ip, ua, clean(d.get("name"), NAME_MAX), now)
                    visit_touch(device, now)
                    tab = clean(d.get("tab"), 32)
                    if tab:
                        tabs[tab] = now
                    name = clean(d.get("name"), NAME_MAX) or prev.get("name", "") or names.get(device, {}).get("name", "")
                    clients[device] = {
                        "name": name, "ip": ip, "ua": ua, "label": device_label(ua),
                        "hw": hw,
                        "first": prev.get("first", now), "last": now,
                        "viewing": bool(d.get("viewing")),
                        "tabs": tabs,
                    }
            return self.send_json(200, {"banned": False})

        if p == "/api/bye":
            # タブを閉じた・移動した時の通知(表示数をすぐ減らす)
            with lock:
                c = clients.get(device)
                if c:
                    c.get("tabs", {}).pop(clean(d.get("tab"), 32), None)
            return self.send_json(200, {"ok": True})

        if p == "/api/comment":
            with lock:
                if is_banned(device, ip):
                    return self.send_json(403, {"error": "アクセスが禁止されています"})
                text = clean(d.get("text"), COMMENT_MAX)
                name = clean(d.get("name"), NAME_MAX) or "名無し"
                if not device or not text:
                    return self.send_json(400, {"error": "コメントを入力してください"})
                if now - last_comment.get(device, 0) < COMMENT_INTERVAL:
                    return self.send_json(429, {"error": "少し待ってから送ってください"})
                last_comment[device] = now
                hw = clean(d.get("hw"), 16)
                if is_muted(device, ip, hw):
                    # ミュート中の端末: 送った側には成功に見せて、記録しない
                    return self.send_json(200, {"ok": True})
                ua = clean(self.headers.get("User-Agent"), 300)
                label = device_label(ua)
                remember(device, hw, ip, ua, name, now)
                visit_touch(device, now)
                append_comment({"ts": now, "name": name, "text": text,
                                "device": device, "ip": ip, "label": label,
                                "hw": clean(d.get("hw"), 16)})
                if names.get(device, {}).get("name") != name:
                    names[device] = {"name": name, "ip": ip, "label": label, "updated": now}
                    save_json("names.json", names)
                if device in clients:
                    clients[device]["name"] = name
            return self.send_json(200, {"ok": True})

        if p == "/api/login":
            with lock:
                if is_banned(device, ip):
                    return self.send_json(403, {"error": "アクセスが禁止されています"})
                if check_pin(d.get("pin")):
                    tok = secrets.token_urlsafe(32)
                    tokens[tok] = now + TOKEN_TTL
                    return self.send_json(200, {"token": tok})
                fails.append((now, device, ip))
                fails[:] = [f for f in fails if now - f[0] <= FAIL_WINDOW]
                n_dev = sum(1 for f in fails if device and f[1] == device)
                n_ip = sum(1 for f in fails if f[2] == ip)
                if max(n_dev, n_ip) >= FAIL_LIMIT:
                    rec = {"ts": now, "ip": ip, "device": device,
                           "label": device_label(self.headers.get("User-Agent", "")),
                           "reason": "暗証番号を1時間に3回間違えた"}
                    if device:
                        bans["devices"][device] = rec
                    bans["ips"][ip] = rec
                    save_json("bans.json", bans)
                    return self.send_json(403, {"error": "3回間違えたため、アクセスを禁止しました"})
                left = FAIL_LIMIT - max(n_dev, n_ip)
                return self.send_json(401, {"error": f"番号が違います(あと{left}回間違えると禁止されます)"})

        if p == "/api/admin/profile":
            # Cross-origin profile writes are limited to the Y++ site.
            # Other applications may still use their original Y-OBS endpoints.
            origin = self.headers.get("Origin", "")
            if origin not in PROFILE_ORIGINS:
                return self.send_json(403, {"error": "許可されていないサイトからの更新です"})
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                if not isinstance(d, dict):
                    return self.send_json(400, {"error": "送信データが不正です"})
                rev = d.get("revision")
                if type(rev) is not int or rev != profile_record.get("revision", 0):
                    return self.send_json(409, {"error": "別の端末で更新されています"})
                try:
                    profile = normalize_profile(d.get("profile"))
                except ValueError as e:
                    return self.send_json(400, {"error": str(e)})
                serialized = json.dumps(profile, ensure_ascii=False)
                if len(serialized.encode("utf-8")) > 17000:
                    return self.send_json(413, {"error": "プロフィールの容量が大きすぎます"})
                next_rev = rev + 1
                next_record = {"revision": next_rev, "profile": profile}
                try:
                    save_json("yplus_profile.json", next_record)
                except OSError:
                    return self.send_json(500, {"error": "保存に失敗しました"})
                profile_record.update(next_record)
            return self.send_json(200, {"ok": True, "revision": next_rev})

        if p == "/api/admin/title":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                stream["title"] = clean(d.get("title"), TITLE_MAX)
                save_json("stream.json", stream)
                _count_cache["data"] = None
            return self.send_json(200, {"ok": True, "title": stream["title"]})

        if p == "/api/consent":
            # 視聴者が年齢制限の注意に同意した記録
            with lock:
                if is_banned(device, ip):
                    return self.send_json(403, {"error": "アクセスが禁止されています"})
                if not device:
                    return self.send_json(400, {"error": "端末が不明です"})
                ua = clean(self.headers.get("User-Agent"), 300)
                r = rating_public()
                rec = {"ts": now, "device": device, "ip": ip, "label": device_label(ua),
                       "version": clean(d.get("version"), 32) or r["version"], "text": r["text"]}
                consents.append(rec)
                append_jsonl("consents.jsonl", rec)
                remember(device, clean(d.get("hw"), 16), ip, ua, "", now)
            return self.send_json(200, {"ok": True})

        if p == "/api/admin/rating":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                on = bool(d.get("on"))
                text = clean(d.get("text"), 200) or RATING_TEXT
                r = stream["rating"]
                if on != r.get("on") or text != r.get("text"):
                    r["version"] = str(int(now)) if on else ""   # 変えたら全員にもう一度同意してもらう
                r["on"], r["text"] = on, text
                save_json("stream.json", stream)
                _count_cache["data"] = None
            return self.send_json(200, {"ok": True, "rating": rating_public()})

        if p == "/api/admin/mute":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                dev = clean(d.get("device"), 64)
                if not dev:
                    return self.send_json(400, {"error": "端末が指定されていません"})
                if d.get("on"):
                    # その端末(ブラウザ)と、同じ IP+端末キー(同じスマホの別ブラウザ)をミュート
                    src = next((c for c in reversed(comments) if c.get("device") == dev), {})
                    key = mute_key(src.get("ip", ""), src.get("hw", ""))
                    rec = {"ts": now, "device": dev, "key": key, "name": src.get("name", ""),
                           "label": src.get("label", ""), "ip": src.get("ip", "")}
                    mutes["devices"][dev] = rec
                    if key:
                        mutes["keys"][key] = rec
                else:
                    rec = mutes["devices"].pop(dev, None) or {}
                    if rec.get("key"):
                        mutes["keys"].pop(rec["key"], None)
                save_json("mutes.json", mutes)
            return self.send_json(200, {"ok": True})

        if p == "/api/admin/logout":
            with lock:
                tokens.pop(self.token(), None)
            return self.send_json(200, {"ok": True})

        self.send_json(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=watch_stream, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
