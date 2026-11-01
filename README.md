# Y-OBS ミニプレイヤー

OBSの映像をスマホ(iPhone / Android)のブラウザで受けて、**ミニプレイヤー(ピクチャ・イン・ピクチャ)で再生しながらREALITYに戻る**ためのセットです。

```
OBS ──(RTMP)──▶ MediaMTX(PC内のサーバー) ──▶ Cloudflareトンネル(https) ──▶ スマホのページ(GitHub Pages)
       ↑ OBSスクリプトがまとめて起動・設定
```

- スマホ用ページ: `index.html`(GitHub Pagesで公開)。設定は `config.js` にまとめてあり、ページ上には表示されません
- OBSプラグイン: `obs/y-obs-miniplayer.lua`(OBSのスクリプト機能で読み込む。Pythonなどのインストールは不要)
- 遅延: 1〜3秒くらい
- 音声: あり(最初だけミュートで始まり、ボタンをタップした時点で音が出ます。音量はOBS側で調整)

> **なぜHLS方式なのか:** iPhoneのSafariでは、低遅延方式(WebRTC)の映像をミニプレイヤーにできない仕様があります。iPhoneでも確実にミニプレイヤーにできるHLS方式にしています。

---

## 1. GitHub Pagesを有効にする(初回だけ)

1. https://github.com/Y-Sebastian263/Y-OBS を開く
2. **Settings → Pages**
3. Source: **Deploy from a branch** / Branch: **main**、フォルダ **/(root)** → Save
4. 1〜2分後、`https://y-sebastian263.github.io/Y-OBS/` が開けばOK

## 2. PCにツールを2つ置く(初回だけ)

日本語を含まない場所にフォルダを作ります。例: `C:\Y-OBS-tools`

| ツール | ダウンロード先 | 置き方 |
|---|---|---|
| MediaMTX(配信サーバー) | https://github.com/bluenviron/mediamtx/releases の `mediamtx_v◯◯_windows_amd64.zip` | 解凍して `mediamtx.exe` と `mediamtx.yml` をフォルダへ |
| cloudflared(httpsのURLを作る) | https://github.com/cloudflare/cloudflared/releases の `cloudflared-windows-amd64.exe` | フォルダへ入れて名前を `cloudflared.exe` に変更 |

初回起動時にWindowsのファイアウォール確認が出たら「許可」してください。

## 3. OBSにスクリプトを入れる(初回だけ)

1. このリポジトリの `obs/y-obs-miniplayer.lua` をPCに保存(上のツールフォルダに入れてもOK)
2. OBS → **ツール → スクリプト → 「+」** で `y-obs-miniplayer.lua` を選ぶ
3. 右側の設定で
   - **ツールフォルダ**: `C:\Y-OBS-tools`
   - **ページのURL**: `https://y-sebastian263.github.io/Y-OBS/`(最初から入っています)
4. **「① OBSの配信先をこのPCに設定」** を1回押す
   - ⚠ OBSの「設定 → 配信」が `rtmp://127.0.0.1:1935` に書き換わります。YouTubeなど別の所にOBSで配信する時は戻してください
5. おすすめの出力設定(OBS → 設定 → 出力 → 出力モード「詳細」→ 配信)
   - キーフレーム間隔: **1秒**(遅延が小さくなります)
   - ビットレート: 2500 Kbps 前後、解像度 1280×720 / 30fps くらい

## 4. 毎回の使い方

1. OBSのスクリプト画面で **「② まとめて開始」**
   - サーバーとトンネルが起動 → 3秒後にOBSの配信が自動で始まります
   - PCのブラウザに **QRコード** が出ます(URLは起動するたびに変わります)
2. スマホでQRを読み取り、**iPhoneはSafari / AndroidはChrome** で開く
3. 映像が映ったら **「ミニプレイヤーにしてREALITYへ戻る」** をタップ
4. 終わったら **「停止(配信とサーバー)」**

QRを閉じてしまったら「スマホ用QRをもう一度表示」を押してください。
一度開いたスマホでは接続先がそのスマホの中だけに保存され、アドレスバーからは消えます。同じURLのうちは再読み込みだけで見られます。

## スマホ側の注意

- **iPhone**: ホーム画面に追加したアプリ版や、LINE・Xなどの中のブラウザではミニプレイヤーが使えません。必ずSafariで開いてください。設定 → 一般 → ピクチャ・イン・ピクチャ がオンになっているかも確認。
- **Android**: 設定 → アプリ → Chrome → 「ピクチャー イン ピクチャー」を許可。
- **REALITYに戻ってミニプレイヤーが止まる場合(主にiPhone)**: REALITYの配信がマイクを使い始めると、音の出ている動画が止められることがあります。ページの 🔊 ボタンで **🔇 音なし** にすると止まりにくくなります。
- **戻るボタンでREALITYが開かない場合**: 戻り先は `config.js` の `returnUrl` で決まります(ページ上には設定欄はありません)。REALITYでプロフィールをシェアした時のURL(`https://reality.app/...`)に変えると開くことがあります。うまくいかなくても、ミニプレイヤーが出ていればアプリ切り替えでREALITYに戻ればそのまま見られます。

## 困ったとき

| 症状 | 確認すること |
|---|---|
| QRが出ない | ツールフォルダに `mediamtx.exe` と `cloudflared.exe` があるか。OBSのスクリプトログ(スクリプト画面の「スクリプトログ」)と、ツールフォルダの `tunnel.log` |
| スマホが「配信待ち」のまま | OBSの配信が始まっているか(右下が緑)。① の配信先設定をしたか。ツールフォルダの `mediamtx.log` |
| 遅延が大きい | キーフレーム間隔を1秒に。ページの「再接続」で最新位置に追いつきます |

## 補足

- URL(`xxxx.trycloudflare.com`)を知っている人は誰でも見られます。URLは起動ごとにランダムに変わります。
- このスクリプトはWindows用です。
