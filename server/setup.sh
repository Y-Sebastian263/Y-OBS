#!/usr/bin/env bash
# Y-OBS 配信サーバー 自動セットアップ (Ubuntu 22.04 / 24.04)
#
#   curl -fsSL https://raw.githubusercontent.com/Y-Sebastian263/Y-OBS/main/server/setup.sh | sudo bash
#
# 入るもの:
#   - MediaMTX : OBS(RTMP)を受けて、HLS(iPhone等)と WebRTC(低遅延)で視聴者に配る
#   - Caddy    : https の入口(証明書は自動。ホスト名は <IP>.sslip.io)
#   - ufw      : すでに有効な場合だけ、必要なポートを追加(有効化はしない)
# 既存のサービス(マイクラ等)や Caddy の設定は消しません。Caddyfile はバックアップ後に追記します。
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
apt-get install -y curl tar gnupg debian-keyring debian-archive-keyring apt-transport-https

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

echo "==> ポートの空きを確認"
systemctl stop y-obs-mediamtx 2>/dev/null || true
BUSY=""
for P in 1935 8189 8888 8889; do
  if ss -Hlntu "( sport = :$P )" | grep -q .; then BUSY="$BUSY $P"; fi
done
if [ -n "$BUSY" ]; then
  echo "ポート${BUSY} がすでに他のソフトで使われています。中止します(何も変更していません)。" >&2
  ss -lntup | grep -E ":(1935|8189|8888|8889)\b" >&2 || true
  exit 1
fi
if ss -Hlnt "( sport = :443 )" | grep -q . && ! pgrep -x caddy >/dev/null; then
  echo "443番を Caddy 以外のソフトが使っています。中止します(何も変更していません)。" >&2
  exit 1
fi

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

# 視聴人数カウンター用(このサーバー内からだけ)
api: yes
apiAddress: 127.0.0.1:9997

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

CADDYFILE=/etc/caddy/Caddyfile
BEGIN_MARK="# >>> Y-OBS >>>"
END_MARK="# <<< Y-OBS <<<"
touch "$CADDYFILE"
BACKUP="$CADDYFILE.bak-yobs-$(date +%Y%m%d-%H%M%S)"
cp -p "$CADDYFILE" "$BACKUP"
echo "    Caddyfile をバックアップ: $BACKUP"
# 前回の Y-OBS 部分だけ取り除いて、新しく追記
sed -i "/^${BEGIN_MARK}\$/,/^${END_MARK}\$/d" "$CADDYFILE"
cat >> "$CADDYFILE" <<EOF

${BEGIN_MARK}
# 視聴(HLS)と低遅延(WebRTC)はアドレスを分けて、パスを書き換えずにそのまま中継する
hls.${HOST} {
	log {
		output file /var/log/caddy/y-obs-hls.log {
			roll_size 10MiB
			roll_keep 2
		}
		format json
	}
	reverse_proxy 127.0.0.1:8888
}
rtc.${HOST} {
	reverse_proxy 127.0.0.1:8889
}
${HOST} {
	handle /count {
		reverse_proxy 127.0.0.1:8897
	}
	handle {
		respond "Y-OBS server OK" 200
	}
}
${END_MARK}
EOF
if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/tmp/y-obs-caddy-validate.log 2>&1; then
  cp -p "$BACKUP" "$CADDYFILE"
  echo "Caddy の設定チェックに失敗したので元に戻しました。中止します。" >&2
  tail -n 20 /tmp/y-obs-caddy-validate.log >&2
  exit 1
fi

echo "==> 同時視聴人数カウンター"
mkdir -p /var/log/caddy /usr/local/lib/y-obs
chown caddy:caddy /var/log/caddy
curl -fsSL "https://raw.githubusercontent.com/Y-Sebastian263/Y-OBS/${YOBS_REF:-main}/server/viewer-count.py" \
  -o /usr/local/lib/y-obs/viewer-count.py
chmod 755 /usr/local/lib/y-obs/viewer-count.py
cat > /etc/systemd/system/y-obs-count.service <<EOF
[Unit]
Description=Y-OBS viewer counter
After=network-online.target

[Service]
User=caddy
ExecStart=/usr/bin/python3 /usr/local/lib/y-obs/viewer-count.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> ファイアウォール"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  for R in 80/tcp 443/tcp 1935/tcp 8189/tcp 8189/udp; do ufw allow "$R" >/dev/null; done
  echo "    ufw に 80,443,1935,8189 を追加しました"
else
  echo "    ufw は無効なので変更しません(さくらのパケットフィルターで許可してください)"
fi

echo "==> 起動"
systemctl daemon-reload
systemctl enable --now y-obs-mediamtx >/dev/null
systemctl restart y-obs-mediamtx
systemctl enable --now y-obs-count >/dev/null
systemctl restart y-obs-count
systemctl enable caddy >/dev/null
if systemctl is-active --quiet caddy; then systemctl reload caddy; else systemctl start caddy; fi

cat > "$CONF_DIR/info.txt" <<EOF
サーバー        : ${HOST}  (${IP})
OBS サーバー    : rtmp://${IP}:1935
OBS 送信パスワード: ${PASS}
視聴 HLS       : https://hls.${HOST}/live/index.m3u8
視聴 WebRTC    : https://rtc.${HOST}/live/whep
視聴人数       : https://${HOST}/count
EOF
chmod 600 "$CONF_DIR/info.txt"

echo
echo "================ セットアップ完了 ================"
echo "サーバーのアドレス : ${IP}"
echo "OBS送信パスワード  : ${PASS}"
echo "(あとで見る時: sudo cat ${CONF_DIR}/info.txt)"
echo "=================================================="
echo "※ パスワードは他人に見せないでください。"
