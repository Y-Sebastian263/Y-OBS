#!/usr/bin/env bash
# Y-OBS 更新コマンド(y-obs-update)を入れて、そのコマンドだけ sudo のパスワードなしで実行できるようにする。
# 最初に 1 回だけ、本人がパスワードを入れて実行する:
#   curl -fsSL https://raw.githubusercontent.com/Y-Sebastian263/Y-OBS/main/server/install-updater.sh | sudo bash
#
# パスワードなしで実行できるのは「GitHub の Y-OBS の、指定したコミット(40桁)の setup.sh を実行する」ことだけ。
# 他の管理者操作やマイクラには影響しない。やめる時: sudo rm /etc/sudoers.d/y-obs-update
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "sudo を付けて実行してください" >&2
  exit 1
fi

USER_NAME="${SUDO_USER:-ubuntu}"

cat > /usr/local/sbin/y-obs-update <<'EOF'
#!/usr/bin/env bash
# Y-OBS の配信サーバーを、GitHub の指定したコミットの内容で更新する
#   sudo y-obs-update <コミットID(40桁)>
set -euo pipefail
REF="${1:-}"
if ! [[ "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "使い方: sudo y-obs-update <コミットID(40桁の英数字)>" >&2
  exit 1
fi
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
curl -fsSL "https://raw.githubusercontent.com/Y-Sebastian263/Y-OBS/${REF}/server/setup.sh" -o "$TMP"
YOBS_REF="$REF" YOBS_HIDE_PASS=1 bash "$TMP"
EOF
chown root:root /usr/local/sbin/y-obs-update
chmod 755 /usr/local/sbin/y-obs-update

RULE="${USER_NAME} ALL=(root) NOPASSWD: /usr/local/sbin/y-obs-update"
TMP_RULE="$(mktemp)"
echo "$RULE" > "$TMP_RULE"
if visudo -cf "$TMP_RULE" >/dev/null; then
  install -m 0440 -o root -g root "$TMP_RULE" /etc/sudoers.d/y-obs-update
  rm -f "$TMP_RULE"
else
  rm -f "$TMP_RULE"
  echo "sudo の設定チェックに失敗したので中止しました(何も変更していません)" >&2
  exit 1
fi

echo
echo "================ 設定完了 ================"
echo "これで次のコマンドだけ、パスワードなしで実行できます:"
echo "  sudo y-obs-update <コミットID>"
echo "やめる時: sudo rm /etc/sudoers.d/y-obs-update"
echo "=========================================="
