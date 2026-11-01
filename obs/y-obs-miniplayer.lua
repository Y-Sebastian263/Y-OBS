--[[
  Y-OBS ミニプレイヤー配信 (OBS スクリプト / Windows 用)

  OBS の映像を、このPC上の小さな配信サーバー(MediaMTX)に送り、
  Cloudflare の無料トンネルで https の URL を作って、スマホ用ページ
  (GitHub Pages)から見られるようにします。

  使い方は README.md を見てください。
  OBS → ツール → スクリプト → 「+」でこのファイルを追加します。
]]

obs = obslua

local DEFAULT_PAGES_URL = "https://y-sebastian263.github.io/Y-OBS/"
local HLS_PORT = 8888
local WEBRTC_PORT = 8889

local cfg = {
  pub_pass    = "",
  tools_dir   = "",
  pages_url   = DEFAULT_PAGES_URL,
  stream_name = "live",
}

local state = {
  polling     = false,
  poll_count  = 0,
  viewer_url  = "",
}

---------------------------------------------------------------------------
-- 小物
---------------------------------------------------------------------------
local function log(msg)
  obs.script_log(obs.LOG_INFO, "[Y-OBS] " .. msg)
end

local function warn(msg)
  obs.script_log(obs.LOG_WARNING, "[Y-OBS] " .. msg)
end

local function winpath(p)
  p = (p or ""):gsub("/", "\\")
  p = p:gsub("\\+$", "")
  return p
end

local function file_exists(p)
  local f = io.open(p, "rb")
  if f then f:close() return true end
  return false
end

local function read_file(p)
  local f = io.open(p, "rb")
  if not f then return nil end
  local s = f:read("*a")
  f:close()
  return s
end

local function write_file(p, content)
  local f = io.open(p, "wb")
  if not f then return false end
  f:write(content)
  f:close()
  return true
end

-- コンソール窓を出さずにバッチを起動
local function run_hidden(bat)
  local cmd = 'powershell -NoProfile -WindowStyle Hidden -Command "Start-Process -WindowStyle Hidden -FilePath \'' .. bat .. '\'"'
  os.execute(cmd)
end

local function kill_tools()
  os.execute('taskkill /F /IM mediamtx.exe /IM mediamtx.exe.exe /IM cloudflared.exe /IM cloudflared.exe.exe ' ..
    '/IM cloudflared-windows-amd64.exe >nul 2>&1')
end

-- 名前の揺れ(.exe.exe や ダウンロード時の名前)も探す
local function find_exe(dir, names)
  for _, n in ipairs(names) do
    if file_exists(dir .. "\\" .. n) then return n end
  end
  return nil
end

local MEDIAMTX_NAMES = { "mediamtx.exe", "mediamtx.exe.exe" }
local CLOUDFLARED_NAMES = { "cloudflared.exe", "cloudflared.exe.exe",
  "cloudflared-windows-amd64.exe", "cloudflared-windows-amd64.exe.exe" }

local function tools_ok()
  local dir = winpath(cfg.tools_dir)
  if dir == "" then
    warn("ツールフォルダが未設定です")
    return nil
  end
  local mtx = find_exe(dir, MEDIAMTX_NAMES)
  local cfd = find_exe(dir, CLOUDFLARED_NAMES)
  local missing = {}
  if not mtx then table.insert(missing, "mediamtx.exe") end
  if not cfd then table.insert(missing, "cloudflared.exe") end
  if #missing > 0 then
    warn("ツールフォルダに " .. table.concat(missing, " と ") .. " がありません: " .. dir)
    return nil
  end
  return dir, mtx, cfd
end

local function make_viewer_url(tunnel, rtc_tunnel)
  local base = cfg.pages_url
  if base == "" then base = DEFAULT_PAGES_URL end
  if not base:match("/$") and not base:match("%.html$") then base = base .. "/" end
  local name = cfg.stream_name ~= "" and cfg.stream_name or "live"
  local url = base .. "?src=" .. tunnel .. "/" .. name .. "/index.m3u8"
  if rtc_tunnel then
    url = url .. "&rtc=" .. rtc_tunnel .. "/" .. name .. "/whep"
  end
  return url
end

---------------------------------------------------------------------------
-- QR 表示ページ(PCのブラウザで開く)
---------------------------------------------------------------------------
local function open_qr_page()
  local dir = winpath(cfg.tools_dir)
  if state.viewer_url == "" or dir == "" then
    warn("まだURLができていません。先に「まとめて開始」を押してください")
    return
  end
  local html = [[<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><title>Y-OBS スマホ用QR</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#14111c;color:#f3eefc;font-family:"Hiragino Sans","Noto Sans JP","Yu Gothic",sans-serif}
.card{background:#1f1a2b;border:1px solid #352d47;border-radius:18px;padding:28px;text-align:center;max-width:520px}
#qr{background:#fff;padding:14px;border-radius:12px;display:inline-block}
#qr svg{width:300px;height:300px;display:block}
.url{word-break:break-all;font-size:12px;color:#a99cc4;margin:14px 0}
button{background:linear-gradient(135deg,#ff6fae,#7c5cff);border:0;color:#fff;font-weight:700;
padding:12px 20px;border-radius:12px;font-size:15px;cursor:pointer}
</style></head><body><div class="card">
<h2>スマホでこのQRを読み取ってください</h2>
<div id="qr"></div>
<div class="url" id="u"></div>
<button onclick="navigator.clipboard.writeText(U).then(()=>this.textContent='コピーしました')">URLをコピー</button>
<p style="font-size:12px;color:#a99cc4">iPhoneはSafari、AndroidはChromeで開いてください。<br>
このURLはサーバーを起動するたびに変わります。</p>
</div>
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js"></script>
<script>
var U = "]] .. state.viewer_url .. [[";
document.getElementById('u').textContent = U;
var q = qrcode(0, 'M'); q.addData(U); q.make();
document.getElementById('qr').innerHTML = q.createSvgTag({cellSize: 6, margin: 0, scalable: true});
</script></body></html>]]
  local path = dir .. "\\y-obs-qr.html"
  if write_file(path, html) then
    os.execute('start "" "' .. path .. '"')
  else
    warn("QRページを書き込めませんでした: " .. path)
  end
end

---------------------------------------------------------------------------
-- トンネルURLの読み取り(1秒ごと)
---------------------------------------------------------------------------
local function poll_tunnel()
  state.poll_count = state.poll_count + 1
  local dir = winpath(cfg.tools_dir)
  local pat = "https://[%w%-]+%.trycloudflare%.com"
  local url = (read_file(dir .. "\\tunnel.log") or ""):match(pat)
  local rtc = (read_file(dir .. "\\tunnel-rtc.log") or ""):match(pat)
  -- 両方そろうのを待つ(低遅延用が遅いときは15秒で諦めて通常のみ)
  if url and (rtc or state.poll_count >= 15) then
    obs.remove_current_callback()
    state.polling = false
    if not rtc then warn("低遅延用のトンネルが作れませんでした(通常モードのみで続行)") end
    state.viewer_url = make_viewer_url(url, rtc)
    log("スマホ用URL: " .. state.viewer_url)
    open_qr_page()
    return
  end
  if state.poll_count >= 45 then
    obs.remove_current_callback()
    state.polling = false
    warn("トンネルのURLを取得できませんでした。tunnel.log を確認してください: " .. dir .. "\\tunnel.log")
  end
end

local function start_stream_later()
  obs.remove_current_callback()
  if not obs.obs_frontend_streaming_active() then
    log("OBSの配信を開始します")
    obs.obs_frontend_streaming_start()
  end
end

---------------------------------------------------------------------------
-- ボタンの処理
---------------------------------------------------------------------------
local function on_setup(props, prop)
  local s = obs.obs_data_create()
  obs.obs_data_set_string(s, "server", "rtmp://127.0.0.1:1935")
  obs.obs_data_set_string(s, "key", (cfg.stream_name ~= "" and cfg.stream_name or "live") ..
    "?user=obs&pass=" .. cfg.pub_pass)
  local svc = obs.obs_service_create("rtmp_custom", "y-obs-miniplayer", s, nil)
  obs.obs_frontend_set_streaming_service(svc)
  obs.obs_frontend_save_streaming_service()
  obs.obs_service_release(svc)
  obs.obs_data_release(s)
  log("配信先を rtmp://127.0.0.1:1935 (キー: " .. (cfg.stream_name ~= "" and cfg.stream_name or "live") .. ") に設定しました")
  return false
end

local function start_servers()
  local dir, mtx, cfd = tools_ok()
  if not dir then return false end

  kill_tools()
  os.remove(dir .. "\\tunnel.log")
  os.remove(dir .. "\\tunnel-rtc.log")

  -- 視聴(read)は誰でもOK、送信(publish)はパスワード付きの OBS だけ
  write_file(dir .. "\\y-obs-mediamtx.yml", table.concat({
    "authInternalUsers:",
    "  - user: any",
    "    ips: []",
    "    permissions:",
    "      - action: read",
    "      - action: playback",
    "  - user: obs",
    "    pass: " .. cfg.pub_pass,
    "    ips: []",
    "    permissions:",
    "      - action: publish",
    "  - user: any",
    "    ips: ['127.0.0.1', '::1']",
    "    permissions:",
    "      - action: api",
    "      - action: metrics",
    "      - action: pprof",
    "paths:",
    "  all_others:",
    "",
  }, "\r\n"))
  write_file(dir .. "\\y-obs-mediamtx.bat",
    "@echo off\r\ncd /d \"%~dp0\"\r\n\"" .. mtx .. "\" y-obs-mediamtx.yml > mediamtx.log 2>&1\r\n")
  write_file(dir .. "\\y-obs-tunnel.bat",
    "@echo off\r\ncd /d \"%~dp0\"\r\n\"" .. cfd .. "\" tunnel --no-autoupdate --url http://127.0.0.1:" ..
    HLS_PORT .. " > tunnel.log 2>&1\r\n")

  run_hidden(dir .. "\\y-obs-mediamtx.bat")
  write_file(dir .. "\\y-obs-tunnel-rtc.bat",
    "@echo off\r\ncd /d \"%~dp0\"\r\n\"" .. cfd .. "\" tunnel --no-autoupdate --url http://127.0.0.1:" ..
    WEBRTC_PORT .. " > tunnel-rtc.log 2>&1\r\n")
  run_hidden(dir .. "\\y-obs-tunnel.bat")
  run_hidden(dir .. "\\y-obs-tunnel-rtc.bat")
  log("サーバーとトンネルを起動しました。URLを待っています…")

  state.viewer_url = ""
  state.poll_count = 0
  if state.polling then obs.timer_remove(poll_tunnel) end
  state.polling = true
  obs.timer_add(poll_tunnel, 1000)
  return true
end

local function on_start_all(props, prop)
  if start_servers() then
    obs.timer_add(start_stream_later, 3000)
  end
  return false
end

local function on_start_servers_only(props, prop)
  start_servers()
  return false
end

local function on_qr(props, prop)
  open_qr_page()
  return false
end

local function on_stop(props, prop)
  if obs.obs_frontend_streaming_active() then
    obs.obs_frontend_streaming_stop()
  end
  if state.polling then
    obs.timer_remove(poll_tunnel)
    state.polling = false
  end
  kill_tools()
  state.viewer_url = ""
  log("配信とサーバーを停止しました")
  return false
end

---------------------------------------------------------------------------
-- OBS スクリプト API
---------------------------------------------------------------------------
function script_description()
  return [[<h3>Y-OBS ミニプレイヤー配信</h3>
OBSの映像をスマホのミニプレイヤー用ページに届けます。<br>
<b>初回だけ:</b> ツールフォルダを選んで「① 配信先を設定」(更新後はもう一度押してください)<br>
<b>毎回:</b> 「② まとめて開始」→ PCに出るQRをスマホで読む<br>
<small>※①はOBSの「配信」設定を上書きします。</small>]]
end

function script_properties()
  local p = obs.obs_properties_create()
  obs.obs_properties_add_path(p, "tools_dir", "ツールフォルダ", obs.OBS_PATH_DIRECTORY, nil, nil)
  obs.obs_properties_add_text(p, "pages_url", "ページのURL", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_text(p, "stream_name", "配信名", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_button(p, "btn_setup", "① OBSの配信先をこのPCに設定(初回だけ)", on_setup)
  obs.obs_properties_add_button(p, "btn_all", "② まとめて開始(サーバー起動 → 配信開始)", on_start_all)
  obs.obs_properties_add_button(p, "btn_servers", "サーバーだけ起動", on_start_servers_only)
  obs.obs_properties_add_button(p, "btn_qr", "スマホ用QRをもう一度表示", on_qr)
  obs.obs_properties_add_button(p, "btn_stop", "停止(配信とサーバー)", on_stop)
  return p
end

function script_defaults(settings)
  obs.obs_data_set_default_string(settings, "pages_url", DEFAULT_PAGES_URL)
  obs.obs_data_set_default_string(settings, "stream_name", "live")
end

local function random_pass()
  local chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
  math.randomseed(os.time() + math.floor(os.clock() * 1000000))
  local t = {}
  for i = 1, 20 do
    local n = math.random(1, #chars)
    t[i] = chars:sub(n, n)
  end
  return table.concat(t)
end

function script_load(settings)
  -- OBS 送信用パスワード(初回に自動作成して OBS の設定内にだけ保存)
  if obs.obs_data_get_string(settings, "pub_pass") == "" then
    obs.obs_data_set_string(settings, "pub_pass", random_pass())
  end
  cfg.pub_pass = obs.obs_data_get_string(settings, "pub_pass")
end

function script_update(settings)
  cfg.tools_dir   = obs.obs_data_get_string(settings, "tools_dir")
  local pp = obs.obs_data_get_string(settings, "pub_pass")
  if pp ~= "" then cfg.pub_pass = pp end
  cfg.pages_url   = obs.obs_data_get_string(settings, "pages_url")
  cfg.stream_name = obs.obs_data_get_string(settings, "stream_name"):gsub("[^%w%-_]", "")
end

function script_unload()
  kill_tools()
end
