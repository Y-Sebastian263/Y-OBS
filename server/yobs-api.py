#!/usr/bin/env python3
"""Y-OBS サーバーAPI(視聴人数・コメント・管理ログイン・アクセス禁止)

Caddy 経由で https://<host>/count と https://<host>/api/... に公開される。

  GET  /count                 同時視聴人数 {"viewers","hls","webrtc","live"}
  POST /api/hello             端末の挨拶(1分ごと)。{"device","name","ua","viewing"} → {"banned"}
  POST /api/comment           コメント送信 {"device","name","text"}
  POST /api/login             管理ログイン {"device","pin"} → {"token"}
  GET  /api/admin/state       管理画面のデータ(Authorization: Bearer <token>)
  POST /api/admin/title        配信タイトルを設定(配信終了で自動リセット)
  POST /api/admin/logout

ブラウザからは MAC アドレスや端末名は取れないため、ページが端末ごとに作る
「端末ID」と、接続元の IP アドレスで端末を見分ける。
暗証番号を 1 時間に 3 回間違えると、その端末IDと IP アドレスを永久に禁止する。
"""
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
FAIL_WINDOW = 3600          # 1時間
FAIL_LIMIT = 3
TOKEN_TTL = 12 * 3600
COMMENT_MAX = 300
NAME_MAX = 30
COMMENT_INTERVAL = 3        # 秒: 同じ端末の連投制限
COMMENTS_KEEP = 2000

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
stream = load_json("stream.json", {"title": ""})   # 配信タイトル(配信終了でリセット)
TITLE_MAX = 60


def append_comment(c):
    comments.append(c)
    del comments[:-COMMENTS_KEEP]
    with open(path("comments.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(c, ensure_ascii=False) + "\n")


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
            "title": stream.get("title", "") if live else ""}
    _count_cache.update(t=now, data=data)
    return data


# ---------------------------------------------------------------- 配信の終了を見張る
def watch_stream():
    """配信中→配信終了になった瞬間に、配信タイトルをリセットする"""
    was_live = False
    while True:
        live = bool((api_get(f"/v3/paths/get/{STREAM}") or {}).get("ready"))
        if was_live and not live:
            with lock:
                stream["title"] = ""
                save_json("stream.json", stream)
                _count_cache["data"] = None
        was_live = live
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
        if p == "/api/admin/state":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                now = time.time()
                # 同じ IP + 同じ端末キー(画面・GPU などブラウザが違っても同じ情報)を 1 台にまとめる
                groups = {}
                for dev, c in clients.items():
                    if now - c["last"] > ONLINE_WINDOW:
                        continue
                    key = (c["ip"], c.get("hw") or dev)
                    g = groups.setdefault(key, {
                        "key": (c.get("hw") or dev)[:8], "ip": c["ip"], "names": [], "os": c["label"].split(" / ")[0],
                        "browsers": [], "devices": [], "last": 0, "viewing": False, "ua": c["ua"],
                    })
                    if c["name"] and c["name"] not in g["names"]:
                        g["names"].append(c["name"])
                    br = c["label"].split(" / ")[-1]
                    if br not in g["browsers"]:
                        g["browsers"].append(br)
                    g["devices"].append(dev[:8])
                    g["last"] = max(g["last"], c["last"])
                    g["viewing"] = g["viewing"] or c["viewing"]
                online = []
                for g in groups.values():
                    g["ago"] = int(now - g["last"])
                    online.append(g)
                online.sort(key=lambda c: c["last"], reverse=True)
                data = {
                    "clients": online,
                    "comments": list(reversed(comments[-300:])),
                    "bans": {"devices": len(bans["devices"]), "ips": len(bans["ips"])},
                    "count": count_snapshot(),
                    "title": stream.get("title", ""),
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
                    name = clean(d.get("name"), NAME_MAX) or prev.get("name", "") or names.get(device, {}).get("name", "")
                    clients[device] = {
                        "name": name, "ip": ip, "ua": ua, "label": device_label(ua),
                        "hw": clean(d.get("hw"), 16),
                        "first": prev.get("first", now), "last": now,
                        "viewing": bool(d.get("viewing")),
                    }
            return self.send_json(200, {"banned": False})

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
                ua = clean(self.headers.get("User-Agent"), 300)
                label = device_label(ua)
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

        if p == "/api/admin/title":
            with lock:
                if not valid_token(self.token()):
                    return self.send_json(401, {"error": "ログインしてください"})
                stream["title"] = clean(d.get("title"), TITLE_MAX)
                save_json("stream.json", stream)
                _count_cache["data"] = None
            return self.send_json(200, {"ok": True, "title": stream["title"]})

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
