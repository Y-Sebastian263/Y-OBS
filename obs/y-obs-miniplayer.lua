--[[
  Y-OBS ミニプレイヤー配信 (OBS スクリプト)

  OBS の映像を Y-OBS 配信サーバー(VPS)へ送る設定をワンボタンで行います。
  視聴者は https://y-sebastian263.github.io/Y-OBS/ を開くだけで見られます。

  OBS → ツール → スクリプト → 「+」でこのファイルを追加してください。
]]

obs = obslua

local VIEWER_PAGE = "https://y-sebastian263.github.io/Y-OBS/"

local cfg = {
  server   = "",
  password = "",
  stream   = "live",
}

local function log(msg)  obs.script_log(obs.LOG_INFO, "[Y-OBS] " .. msg) end
local function warn(msg) obs.script_log(obs.LOG_WARNING, "[Y-OBS] " .. msg) end

local function trim(s) return (s or ""):gsub("^%s+", ""):gsub("%s+$", "") end

-- "rtmp://1.2.3.4:1935" / "1-2-3-4.sslip.io" / "1.2.3.4" のどれでも受け付ける
local function server_host()
  local h = trim(cfg.server)
  h = h:gsub("^%a+://", ""):gsub("[:/].*$", "")
  local a, b, c, d = h:match("^(%d+)%-(%d+)%-(%d+)%-(%d+)%.sslip%.io$")
  if a then h = a .. "." .. b .. "." .. c .. "." .. d end
  return h
end

local function apply_service()
  local host = server_host()
  if host == "" then warn("サーバーのアドレスが未入力です") return false end
  if trim(cfg.password) == "" then warn("送信パスワードが未入力です") return false end

  local s = obs.obs_data_create()
  obs.obs_data_set_string(s, "server", "rtmp://" .. host .. ":1935")
  obs.obs_data_set_string(s, "key", cfg.stream .. "?user=obs&pass=" .. trim(cfg.password))
  local svc = obs.obs_service_create("rtmp_custom", "y-obs", s, nil)
  obs.obs_frontend_set_streaming_service(svc)
  obs.obs_frontend_save_streaming_service()
  obs.obs_service_release(svc)
  obs.obs_data_release(s)
  log("配信先を rtmp://" .. host .. ":1935 に設定しました")
  return true
end

local function on_setup(props, prop)
  apply_service()
  return false
end

local function on_start(props, prop)
  if apply_service() and not obs.obs_frontend_streaming_active() then
    obs.obs_frontend_streaming_start()
    log("配信を開始しました。視聴ページ: " .. VIEWER_PAGE)
  end
  return false
end

local function on_stop(props, prop)
  if obs.obs_frontend_streaming_active() then
    obs.obs_frontend_streaming_stop()
    log("配信を停止しました")
  end
  return false
end

local function on_open_page(props, prop)
  local cmd
  if package.config:sub(1, 1) == "\\" then
    cmd = 'start "" "' .. VIEWER_PAGE .. '"'
  elseif os.getenv("HOME") and io.open("/usr/bin/open", "r") then
    cmd = 'open "' .. VIEWER_PAGE .. '"'
  else
    cmd = 'xdg-open "' .. VIEWER_PAGE .. '" &'
  end
  os.execute(cmd)
  return false
end

function script_description()
  return [[<h3>Y-OBS ミニプレイヤー配信</h3>
OBSの映像を Y-OBS 配信サーバーへ送り、視聴者がミニプレイヤーで見られるようにします。<br>
<b>初回だけ:</b> サーバーのアドレスと送信パスワードを入力<br>
<b>毎回:</b> 「配信開始」を押す(OBSの通常の配信開始ボタンでもOK)<br>
<small>※OBSの「設定 → 配信」をY-OBSサーバー向けに書き換えます。</small>]]
end

function script_properties()
  local p = obs.obs_properties_create()
  obs.obs_properties_add_text(p, "server", "サーバーのアドレス", obs.OBS_TEXT_DEFAULT)
  obs.obs_properties_add_text(p, "password", "送信パスワード", obs.OBS_TEXT_PASSWORD)
  obs.obs_properties_add_button(p, "btn_start", "配信開始", on_start)
  obs.obs_properties_add_button(p, "btn_stop", "配信停止", on_stop)
  obs.obs_properties_add_button(p, "btn_setup", "配信先の設定だけする", on_setup)
  obs.obs_properties_add_button(p, "btn_page", "視聴ページを開く", on_open_page)
  return p
end

function script_update(settings)
  cfg.server   = obs.obs_data_get_string(settings, "server")
  cfg.password = obs.obs_data_get_string(settings, "password")
end
