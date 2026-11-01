#!/usr/bin/env bash
# Y-OBS 配信サーバー 自動セットアップ (Ubuntu 22.04 / 24.04)
#
#   curl -fsSL https://raw.githubusercontent.com/Y-Sebastian263/Y-OBS/main/server/setup.sh | sudo bash
#
# 入るもの:
#   - MediaMTX : OBS(RTMP)を受けて、HLS(iPhone等)と WebRTC(低遅延)で視聴者に配る
#   - Caddy    : https の入口(証明書は自動。ホスト名は <IP>.sslip.io)
#   - ufw      : 必要なポートだけ開ける
# 何度実行しても大丈夫です(送信パスワードは最初の1回だけ作られます)。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "sudo を付けて実行してください" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
CONF_DIR=/etc/y-obs
mkdir -p "$CONF_DIR"

echo "==> パッケージを準備"
apt-get update -y
apt-get install -y curl tar gnupg debian-keyring debian-archive-keyring apt-transport-https ufw

echo "==> このサーバーの公開IPを確認"
IP="$(curl -4 -fsS https://api.ipify.org || curl -4 -fsS https://ifconfig.me)"
HOST="${IP//./-}.sslip.io"
echo "    IP: $IP / ホスト名: $HOST"

PASS_FILE="$CONF_DIR/publish_pass"
if [ ! -s "$PASS_FILE" ]; then
  tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 20 > "$PASS_FILE"
  chmod 600 "$PASS_FILE"
fi
PASS="$(cat "$PASS_FILE")"

echo "==> MediaMTX をインストール"
case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "未対応のCPUです: $(uname -m)" >&2; exit 1 ;;
esac
VER="$(curl -fsSL -o /dev/null -w '%{url_effective}' https://github.com/bluenviron/mediamtx/releases/latest | sed 's#.*/tag/##')"
echo "    バージョン: $VER"
TMP="$(mktemp -d)"
curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${VER}/mediamtx_${VER}_linux_${ARCH}.tar.gz" | tar xz -C "$TMP"
install -m 755 "$TMP/mediamtx" /usr/local/bin/mediamtx
rm -rf "$TMP"

cat > "$CONF_DIR/mediamtx.yml" <<EOF
logLevel: info

# OBS からの受け口(パスワード必須)
rtmp: yes
rtmpAddress: :1935
rtsp: no
srt: no

# 視聴: HLS(iPhone など)。https は Caddy 経由
hls: yes
hlsAddress: 127.0.0.1:8888

# 視聴: WebRTC(低遅延)。合図は Caddy 経由、映像は 8189 番で直接
webrtc: yes
webrtcAddress: 127.0.0.1:8889
webrtcLocalUDPAddress: :8189
webrtcLocalTCPAddress: :8189
webrtcAdditionalHosts: [${IP}]

authInternalUsers:
  # 視聴は誰でもOK
  - user: any
    ips: []
    permissions:
      - action: read
      - action: playback
  # 送信は OBS(パスワード付き)だけ
  - user: obs
    pass: ${PASS}
    ips: []
    permissions:
      - action: publish
  - user: any
    ips: ['127.0.0.1', '::1']
    permissions:
      - action: api
      - action: metrics
      - action: pprof

paths:
  all_others:
EOF
chmod 600 "$CONF_DIR/mediamtx.yml"

cat > /etc/systemd/system/y-obs-mediamtx.service <<EOF
[Unit]
Description=Y-OBS MediaMTX
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/mediamtx $CONF_DIR/mediamtx.yml
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> Caddy(https)をインストール"
if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

cat > /etc/caddy/Caddyfile <<EOF
${HOST} {
	handle_path /hls/* {
		reverse_proxy 127.0.0.1:8888
	}
	handle_path /rtc/* {
		reverse_proxy 127.0.0.1:8889
	}
	handle {
		respond "Y-OBS server OK" 200
	}
}
EOF

echo "==> ファイアウォール"
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw allow 1935/tcp >/dev/null
ufw allow 8189/tcp >/dev/null
ufw allow 8189/udp >/dev/null
ufw --force enable >/dev/null

echo "==> 起動"
systemctl daemon-reload
systemctl enable --now y-obs-mediamtx >/dev/null
systemctl restart y-obs-mediamtx
systemctl enable caddy >/dev/null
systemctl restart caddy

cat > "$CONF_DIR/info.txt" <<EOF
サーバー        : ${HOST}  (${IP})
OBS サーバー    : rtmp://${IP}:1935
OBS 送信パスワード: ${PASS}
視聴 HLS       : https://${HOST}/hls/live/index.m3u8
視聴 WebRTC    : https://${HOST}/rtc/live/whep
EOF
chmod 600 "$CONF_DIR/info.txt"

echo
echo "================ セットアップ完了 ================"
echo "サーバーのアドレス : ${IP}"
echo "OBS送信パスワード  : ${PASS}"
echo "(あとで見る時: sudo cat ${CONF_DIR}/info.txt)"
echo "=================================================="
echo "※ パスワードは他人に見せないでください。"
