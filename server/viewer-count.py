#!/usr/bin/env python3
"""Y-OBS 同時視聴人数カウンター

GET /count → {"viewers": 合計, "hls": n, "webrtc": n, "live": 配信中か}

- HLS(iPhone など): Caddy のアクセスログから、直近 WINDOW 秒に映像を取りに来た
  視聴者を MediaMTX の session ID で数える
- WebRTC(低遅延): MediaMTX の API から接続中のセッションを数える
"""
import json
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOG = os.environ.get("YOBS_HLS_LOG", "/var/log/caddy/y-obs-hls.log")
API = os.environ.get("YOBS_API", "http://127.0.0.1:9997")
STREAM = os.environ.get("YOBS_STREAM", "live")
PORT = int(os.environ.get("YOBS_COUNT_PORT", "8897"))
WINDOW = 20          # 秒: この間に映像を取りに来た人を「視聴中」とみなす
TAIL_BYTES = 3_000_000
CACHE_SEC = 3

_cache = {"t": 0.0, "data": None}


def api_get(path):
    try:
        with urllib.request.urlopen(API + path, timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def count_hls(now):
    try:
        size = os.path.getsize(LOG)
        with open(LOG, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().splitlines()
    except OSError:
        return 0
    viewers = set()
    for raw in reversed(lines):
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        ts = e.get("ts", 0)
        if now - ts > WINDOW + 60:
            break          # 十分古いところまで来たら終わり(ログは完了順なので多少前後する)
        if now - ts > WINDOW:
            continue
        req = e.get("request", {})
        uri = req.get("uri", "")
        if f"/{STREAM}/" not in uri or e.get("status", 0) >= 400:
            continue
        q = parse_qs(urlsplit(uri).query)
        # MediaMTX は視聴者ごとに session ID を付けるので、それで数える
        # (最初の目次の取得だけは ID が無いので数えない=二重カウント防止)
        sid = (q.get("session") or [""])[0]
        if sid:
            viewers.add(sid)
    return len(viewers)


def count_webrtc():
    data = api_get("/v3/webrtcsessions/list?itemsPerPage=1000") or {}
    n = 0
    for s in data.get("items", []):
        if s.get("path") == STREAM and s.get("state") == "read":
            n += 1
    return n


def is_live():
    data = api_get(f"/v3/paths/get/{STREAM}") or {}
    return bool(data.get("ready"))


def snapshot():
    now = time.time()
    if _cache["data"] and now - _cache["t"] < CACHE_SEC:
        return _cache["data"]
    hls, rtc = count_hls(now), count_webrtc()
    data = {"viewers": hls + rtc, "hls": hls, "webrtc": rtc, "live": is_live()}
    _cache.update(t=now, data=data)
    return data


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if urlsplit(self.path).path.rstrip("/") not in ("/count", ""):
            self.send_error(404)
            return
        body = json.dumps(snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
