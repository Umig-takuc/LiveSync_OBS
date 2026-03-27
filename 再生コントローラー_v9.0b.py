from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify
import configparser
import os
import sys
import threading
import time
import datetime
import queue
from obsws_python import ReqClient
from pythonosc.udp_client import SimpleUDPClient
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

# --- 設定 ---
WEB_PORT = 5000

# 【変更点1】ドラムのキメに合わせるため、監視間隔を限界まで短くする
# 0.03秒 = 30ms間隔 (秒間約33回チェック)
POLL_INTERVAL = 0.03

app = Flask(__name__)
app.secret_key = 'obs_secret_key'

# --- パス設定 ---
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(application_path, "config.ini")

# --- グローバル設定 ---
config_data = {
    "servers": [],
    "scenes": [],
    "default_source_name": "メディアソース",
    "scene_source_map": {},
    "songs": [],
    "daslight": {"enable": False, "host": "127.0.0.1", "port": 7000, "listen_port": 8000}
}

# --- フィードバック情報 ---
daslight_feedback = {
    "message": "",
    "timestamp": 0,
    "pause_state": -1,  # -1=不明, 0=無効, 1=一時停止中
    "lock": threading.Lock()
}

# ==========================================
# Daslight OSC送信マネージャー (常駐ワーカー)
# ==========================================
class DaslightOSCSender:
    def __init__(self):
        self.q = queue.Queue()
        self.client = None
        self.current_host = ""
        self.current_port = 0
        # デーモンスレッドとしてワーカーを常駐起動
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def _update_client(self, host, port):
        # ホストやポートが変更された場合のみソケット（クライアント）を再生成する
        if self.current_host != host or self.current_port != port:
            self.client = SimpleUDPClient(host, port)
            self.current_host = host
            self.current_port = port

    def _worker_loop(self):
        while True:
            # キューにデータが入るまでここで待機（ブロック）されるためCPUを消費しません
            osc_path = self.q.get()
            
            dl_conf = config_data.get("daslight", {})
            if dl_conf.get("enable", False):
                try:
                    host = dl_conf.get("host", "127.0.0.1")
                    port = int(dl_conf.get("port", 7000))
                    
                    self._update_client(host, port)
                    # 送信処理（最速）
                    self.client.send_message(osc_path, 1.0)
                    
                    # 高速化のためコメントアウト推奨ですが、デバッグ時は外してください
                    # print(f"📡 OSC Send: {osc_path}") 
                except Exception as e:
                    print(f"❌ OSC Send Error: {e}")
            
            # タスク完了をキューに通知
            self.q.task_done()

    def send(self, osc_path):
        if osc_path:
            # メインループからはキューに入れるだけ（一瞬で終わる）
            self.q.put(osc_path)

# グローバルインスタンスを作成
osc_sender = DaslightOSCSender()

# ==========================================
# Daslight OSC受信サーバー (リスナー)
# ==========================================
class DaslightFeedbackServer:
    def __init__(self):
        self.server = None
        self.thread = None
        self.running = False

    def start(self, port):
        if self.running: return
        self.running = True
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self.handle_osc)
        try:
            self.server = BlockingOSCUDPServer(("0.0.0.0", port), dispatcher)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            print(f"👂 OSC Listener started on port {port}")
        except Exception as e:
            print(f"❌ Failed to start OSC Listener: {e}")
            self.running = False

    def handle_osc(self, address, *args):
        args_str = ", ".join([str(arg) for arg in args]) if args else "値なし"
        
        with daslight_feedback["lock"]:
            # /obs/pause の場合は引数から状態(0 or 1)を取得して保存
            if address == "/obs/pause":
                try:
                    # Daslightからは 1.0 などのfloatで来る場合があるためintに変換
                    daslight_feedback["pause_state"] = int(float(args[0])) if args else -1
                except ValueError:
                    pass

            msg = ""
            if address == "/obs/play": msg = f"再生 (Play) [値: {args_str}]"
            elif address == "/obs/pause": msg = f"一時停止 (Pause) [値: {args_str}]"
            elif address == "/obs/stop": msg = f"停止 (Stop) [値: {args_str}]"
            elif address == "/obs/music1": msg = f"M1 (Restart) [値: {args_str}]"
            else: msg = f"{address} [値: {args_str}]"

            daslight_feedback["message"] = f"照明からの応答: {msg}"
            daslight_feedback["timestamp"] = time.time()

feedback_server = DaslightFeedbackServer()

# ==========================================
# OBS接続・監視マネージャー
# ==========================================
class OBSConnectionManager:
    def __init__(self):
        self.clients = {} 
        self.statuses = {} 
        # 【変更点2】前回の再生位置を記憶する辞書（自動トリガー用）
        self.prev_cursors = {}
        self.lock = threading.Lock()
        self.running = True
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

    def update_servers(self, server_list):
        with self.lock:
            current_keys = set(self.clients.keys())
            new_keys = set([f"{s['host']}:{s['port']}" for s in server_list])
            for key in current_keys - new_keys:
                del self.clients[key]
                if key in self.statuses: del self.statuses[key]
                if key in self.prev_cursors: del self.prev_cursors[key]
            
            for server in server_list:
                key = f"{server['host']}:{server['port']}"
                if key not in self.clients:
                    self.clients[key] = None
                    self.statuses[key] = {
                        "alive": False, "scene": "-", "target_source": "-", "media_state": "NONE", "in_scene": False, "ping": 0, "error": "Initializing..."
                    }
                    self.prev_cursors[key] = -1
                    self.connect_client(key, server)

    def connect_client(self, key, server_conf):
        try:
            client = ReqClient(host=server_conf["host"], port=server_conf["port"], password=server_conf["password"], timeout=2)
            self.clients[key] = client
            return True
        except Exception:
            self.clients[key] = None
            return False

    def get_server_conf(self, key):
        host, port = key.split(':')
        for s in config_data["servers"]:
            if s["host"] == host and str(s["port"]) == str(port): return s
        return None

    def determine_target_source(self, scene_name):
        return config_data["scene_source_map"].get(scene_name, config_data["default_source_name"])

    def monitor_loop(self):
        while self.running:
            with self.lock: target_keys = list(self.clients.keys())
            
            primary_key = None
            if config_data["servers"] and len(config_data["servers"]) > 0:
                s = config_data["servers"][0]
                primary_key = f"{s['host']}:{s['port']}"

            for key in target_keys:
                client = self.clients.get(key)
                
                # 未接続なら再接続試行
                if client is None:
                    conf = self.get_server_conf(key)
                    if conf and self.connect_client(key, conf): client = self.clients[key]
                
                status_data = {"alive": False, "scene": "-", "target_source": "-", "media_state": "UNKNOWN", "in_scene": False, "ping": 0, "error": ""}
                
                if client:
                    try:
                        start_ts = time.time()
                        res = client.get_current_program_scene()
                        current_scene = res.current_program_scene_name
                        target_source = self.determine_target_source(current_scene)
                        
                        end_ts = time.time()
                        status_data["ping"] = int((end_ts - start_ts) * 1000)
                        
                        status_data["alive"] = True
                        status_data["scene"] = current_scene
                        status_data["target_source"] = target_source

                        try:
                            media_res = client.get_media_input_status(target_source)
                            status_data["media_state"] = media_res.media_state
                            current_cursor = media_res.media_cursor

                            # --- 自動トリガーロジック ---
                            if media_res.media_state == "OBS_MEDIA_STATE_PLAYING":
                                last_cursor = self.prev_cursors.get(key, -1)
                                
                                # 【改善点】シーク検知ロジック
                                # 前回位置との差分を計算
                                delta = current_cursor - last_cursor

                                # 条件1: 前回正常に取得できている (last_cursor != -1)
                                # 条件2: 時間が進んでいる (current_cursor > last_cursor)
                                if last_cursor != -1 and delta > 0:
                                    
                                    # 条件3: 【重要】経過時間が 2000ms (2秒) 未満であること
                                    # 2秒以上飛んでいる場合は「シーク操作」とみなし、OSCを送信しない
                                    if delta < 2000:
                                        if key == primary_key:
                                            for song in config_data["songs"]:
                                                trigger_ms = song["ms"]
                                                osc_addr = song["osc"]
                                                
                                                # 通過判定
                                                if last_cursor < trigger_ms <= current_cursor:
                                                    print(f"⚡ HIT! [Main] {song['name']} ({trigger_ms}ms) -> OSC")
                                                    trigger_daslight_async(osc_addr)
                                    else:
                                        # シークされた場合、ログだけ出してトリガーは引かない
                                        if delta > 2000 and key == primary_key:
                                            print(f"⏭ Seek detected ({delta}ms jump). Skipping triggers.")

                                self.prev_cursors[key] = current_cursor
                            else:
                                # 停止中は現在位置を更新し続ける
                                self.prev_cursors[key] = current_cursor

                            status_data["in_scene"] = True

                        except Exception:
                            status_data["media_state"] = "NOT_FOUND"
                            status_data["in_scene"] = False
                            self.prev_cursors[key] = -1

                    except Exception as e:
                        # エラーが多発するとログが汚れるので、接続切れなどの重要エラー以外は抑制しても良い
                        status_data["error"] = str(e)
                        with self.lock: self.clients[key] = None
                        self.prev_cursors[key] = -1
                else: 
                    status_data["error"] = "Disconnected"
                    self.prev_cursors[key] = -1

                with self.lock: self.statuses[key] = status_data
            
            time.sleep(POLL_INTERVAL)

    def get_status_snapshot(self):
        with self.lock: return self.statuses.copy()

    def send_smart_play_all(self):
        any_restarted = False
        with self.lock: target_items = list(self.clients.items())
        def _worker(key, client, results_list):
            is_restart = False
            if client is None:
                conf = self.get_server_conf(key)
                if conf and self.connect_client(key, conf): client = self.clients[key]
                else: return
            try:
                scene_res = client.get_current_program_scene()
                target_source = self.determine_target_source(scene_res.current_program_scene_name)
                should_restart = False
                try:
                    status = client.get_media_input_status(target_source)
                    if status.media_state in ['OBS_MEDIA_STATE_STOPPED', 'OBS_MEDIA_STATE_ENDED']: should_restart = True
                except: pass
                if should_restart:
                    client.trigger_media_input_action(name=target_source, action="OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART")
                    is_restart = True
                else:
                    client.trigger_media_input_action(name=target_source, action="OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY")
            except Exception as e:
                print(f"❌ SmartPlay Error [{key}]: {e}")
                with self.lock: self.clients[key] = None
            results_list.append(is_restart)
        threads = []
        results = []
        for key, client in target_items:
            t = threading.Thread(target=_worker, args=(key, client, results))
            t.start()
            threads.append(t)
        for t in threads: t.join()
        return any(results)

    def send_command_all(self, command_type, value):
        with self.lock: target_items = list(self.clients.items())
        def _worker(key, client):
            if client is None:
                conf = self.get_server_conf(key)
                if conf and self.connect_client(key, conf): client = self.clients[key]
                else: return
            try:
                if command_type == 'media':
                    scene_res = client.get_current_program_scene()
                    target = self.determine_target_source(scene_res.current_program_scene_name)
                    client.trigger_media_input_action(name=target, action=value)
                elif command_type == 'scene': client.set_current_program_scene(value)
                elif command_type == 'seek':
                    scene_res = client.get_current_program_scene()
                    target = self.determine_target_source(scene_res.current_program_scene_name)
                    client.set_media_input_cursor(name=target, cursor=value)
                    self.prev_cursors[key] = value 
            except Exception as e:
                print(f"❌ Command Error [{key}]: {e}")
                with self.lock: self.clients[key] = None
        threads = []
        for key, client in target_items:
            t = threading.Thread(target=_worker, args=(key, client))
            t.start()
            threads.append(t)
        for t in threads: t.join()

obs_manager = OBSConnectionManager()

# --- Config ---
def parse_time_str(time_str):
    try:
        if '.' in time_str: t_part, ms_part = time_str.split('.'); ms = int(ms_part.ljust(3, '0')[:3])
        else: t_part = time_str; ms = 0
        parts = list(map(int, t_part.split(':')))
        if len(parts) == 3: h, m, s = parts
        elif len(parts) == 2: h, m, s = 0, parts[0], parts[1]
        else: return 0
        return (h * 3600000) + (m * 60000) + (s * 1000) + ms
    except: return 0

def load_config():
    global config_data
    config = configparser.ConfigParser()
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write("""[servers]
list = 
    127.0.0.1, 4455, password
[settings]
default_source = メディアソース
scene1 = 待機画面
scene2 = 本番画面
[scene_media_map]
待機画面 = メディアソース1
本番画面 = メディアソース2
[songs]
# 曲名 = 開始時間(HH:MM:SS.sss), OSCアドレス
# 0.03秒間隔でスキャンするのでかなり正確に反応します
01_Start = 00:00:01.000, /obs/scene1
02_DrumHit = 00:02:15.500, /obs/flash
[daslight]
enable = false
host = 127.0.0.1
port = 7000
listen_port = 8000
""")
    config.read(CONFIG_FILE, encoding='utf-8')
    
    servers = []
    if config.has_section('servers'):
        for line in config['servers'].get('list', '').strip().split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 3: servers.append({"host": parts[0], "port": int(parts[1]), "password": parts[2]})
                except ValueError: pass
    config_data["servers"] = servers
    obs_manager.update_servers(servers)

    scenes = []
    if config.has_section('settings'):
        scenes = sorted([(k, v) for k, v in config['settings'].items() if k.startswith('scene')])
        config_data["default_source_name"] = config['settings'].get('default_source', 'メディアソース')
    config_data["scenes"] = scenes

    scene_source_map = {}
    if config.has_section('scene_media_map'):
        for k, v in config['scene_media_map'].items(): scene_source_map[k] = v
    config_data["scene_source_map"] = {k.lower(): v for k, v in scene_source_map.items()}

    songs = []
    if config.has_section('songs'):
        for key, val in config['songs'].items():
            try:
                parts = [p.strip() for p in val.split(',')]
                if len(parts) >= 2:
                    t_str, osc_addr = parts[0], parts[1]
                    songs.append({"name": key.upper(), "time_str": t_str, "ms": parse_time_str(t_str), "osc": osc_addr})
            except: pass
    config_data["songs"] = songs

    if config.has_section('daslight'):
        dl = config['daslight']
        config_data["daslight"]["enable"] = dl.getboolean('enable', False)
        config_data["daslight"]["host"] = dl.get('host', '127.0.0.1')
        config_data["daslight"]["port"] = dl.getint('port', 7000)
        l_port = dl.getint('listen_port', 8000)
        if config_data["daslight"]["enable"]:
            feedback_server.start(l_port)
    return True

def get_mapped_source(scene_name):
    return config_data["scene_source_map"].get(scene_name.lower(), config_data["default_source_name"])
OBSConnectionManager.determine_target_source = lambda self, scene_name: get_mapped_source(scene_name)

last_triggered_times = {}

def trigger_daslight_async(osc_path):
    """
    キューに送信先アドレスを放り込む軽量関数（二重トリガー防止付き）
    """
    current_time = time.time()
    
    # 同じOSCパスが直近3秒以内に送信されていたら無視する（二重発火防止）
    if osc_path in last_triggered_times:
        if (current_time - last_triggered_times[osc_path]) < 3.0:
            print(f"⏭ Double trigger prevented for {osc_path}")
            return
            
    last_triggered_times[osc_path] = current_time

    dl_conf = config_data.get("daslight", {})
    if dl_conf.get("enable", False):
        osc_sender.send(osc_path)


# --- HTML ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OBS Commander (Hi-Speed)</title>
    <style>
        body { font-family: sans-serif; background-color: #222; color: #fff; margin: 0; padding: 10px; text-align: center; }
        h1 { font-size: 1.2rem; color: #ddd; }
        .container { max-width: 600px; margin: 0 auto; }
        .btn { display: block; width: 100%; padding: 15px; margin: 8px 0; border: none; border-radius: 8px; font-size: 1.2rem; font-weight: bold; cursor: pointer; color: white; box-sizing: border-box; }
        .btn-play { background-color: #28a745; }
        .btn-pause { background-color: #ffc107; color: #333; }
        .btn-stop { background-color: #dc3545; }
        .btn-restart { background-color: #17a2b8; }
        .btn-scene { background-color: #6610f2; }
        .btn-song { background-color: #00bcd4; color: #fff; text-align: left; padding: 10px 15px; font-size: 1rem; display: flex; justify-content: space-between; }
        .song-time { font-size: 0.8rem; opacity: 0.8; font-weight: normal; align-self: center; }
        .btn-settings { background-color: #6c757d; margin-top:20px; font-size:1rem; padding:10px; }
        .btn-save { background-color: #007bff; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
        
        .status-panel { background: #333; padding: 10px; border-radius: 8px; margin-bottom: 15px; text-align: left; font-size: 0.85rem; }
        .status-row { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #444; padding: 8px 0; }
        .badge { padding: 3px 6px; border-radius: 4px; font-weight: bold; font-size: 0.8rem; margin-left: 5px; }
        .badge-ok { background-color: #2e7d32; color: #fff; }
        .badge-ng { background-color: #c62828; color: #fff; }
        .media-status { font-weight: bold; margin-top: 4px; display: inline-block; padding: 2px 8px; border-radius: 4px; background: #444; color: #aaa; }
        .media-playing { color: #4caf50; background: #1b5e20; }
        .media-paused  { color: #ffeb3b; background: #f57f17; color: black; }
        .media-stopped { color: #f44336; background: #b71c1c; }
        .ping-val { font-size: 0.75rem; color: #888; margin-left: 5px; }
        .feedback-msg { background: #004d40; border-left: 4px solid #00acc1; padding: 8px; margin-bottom: 10px; font-size: 0.9rem; color: #e0f7fa; display: none; }
        textarea { width: 100%; height: 200px; background: #111; color: #0f0; border: 1px solid #555; }

        /* ここから追加：ロック機能用スタイル */
        .btn-lock { background-color: #e91e63; margin-bottom: 15px; border: 2px solid #ff4081; }
        .btn-lock.is-locked { background-color: #555; border-color: #333; color: #aaa; }
        /* disabled属性がついたボタンとリンクを暗くしてクリック不可にする */
        .btn:disabled, a.btn.disabled {
            opacity: 0.4;
            cursor: not-allowed;
            pointer-events: none;
        }
        /* ここまで追加 */
    </style>
</head>
<body>
    <div class="container">
        {% if page == 'home' %}
            <h1>OBS Commander (⚡Hi-Speed Mode)</h1>
            <div id="feedback-container" class="feedback-msg"></div>
            <div id="status-container" class="status-panel"><div style="text-align:center; color:#aaa;">Connecting...</div></div>

            <div class="status-panel" style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                <div style="font-size: 0.95rem;">
                    💡 照明一時停止: <span id="daslight-pause-status" style="font-weight: bold; color: #888; margin-left: 5px;">取得中...</span>
                </div>
                <form action="/action/daslight_pause" method="post" style="margin:0;">
                    <button class="btn" style="background-color: #ff9800; padding: 10px 15px; font-size: 0.9rem; margin: 0; width: auto; color: white;">
                        照明のみ一時停止
                    </button>
                </form>
            </div>

            <button id="btn-toggle-lock" class="btn btn-lock" onclick="toggleLock()">
                🔓 ロック解除中 (クリックで全操作をロック)
            </button>

            <div class="grid">
                <form action="/action/play" method="post" style="margin:0;"><button id="btn-play" class="btn btn-play">▶ 再生</button></form>
                <form action="/action/pause" method="post" style="margin:0;"><button class="btn btn-pause">⏸ 一時停止</button></form>
            </div>
            <div class="grid">
                <form action="/action/stop" method="post" style="margin:0;" onsubmit="return confirm('⚠️【警告】本当に「停止」しますか？\\n映像や照明が完全にストップします。');">
                    <button class="btn btn-stop">⏹ 停止</button>
                </form>
                <form action="/action/restart" method="post" style="margin:0;" onsubmit="return confirm('⚠️【警告】本当に「最初から」やり直しますか？\\n現在の再生位置がリセットされます。');">
                    <button class="btn btn-restart">⏮ 最初から</button>
                </form>
            </div>
            
            <hr style="border-color: #444;">
            <div style="text-align: left; color: #ffc107; font-size: 0.8rem; margin-bottom: 5px;">
                ⚡自動同期モード有効 (30ms間隔)
            </div>
            
            {% for song in songs %}
                <form action="/track_play" method="post" style="margin:0;">
                    <input type="hidden" name="osc_addr" value="{{ song.osc }}">
                    <input type="hidden" name="seek_ms" value="{{ song.ms }}">
                    <button class="btn btn-song">
                        <span>🎵 {{ song.name }}</span>
                        <span class="song-time">{{ song.time_str }}</span>
                    </button>
                </form>
            {% endfor %}
            
            <hr style="border-color: #444;">
            {% for key, name in scenes %}
                <form action="/scene" method="post">
                    <input type="hidden" name="scene_name" value="{{ name }}">
                    <button class="btn btn-scene">{{ name }}</button>
                </form>
            {% endfor %}
            <a href="/settings" class="btn btn-settings">⚙️ 設定</a>

        {% elif page == 'settings' %}
            <h1>設定</h1>
            <form action="/settings/save" method="post">
                <textarea name="config_text">{{ config_text }}</textarea>
                <button class="btn btn-save">保存</button>
            </form>
            <a href="/" class="btn btn-settings" style="background:#444;">🔙 戻る</a>
        {% endif %}
    </div>

    <script>
        {% if page == 'home' %}
        // --- ここから追加：ロック制御ロジック ---
        let isUiLocked = false;
        function toggleLock() {
            isUiLocked = !isUiLocked;
            const lockBtn = document.getElementById('btn-toggle-lock');
            
            // ロックボタン自身を除く、すべての.btn要素を取得
            const buttons = document.querySelectorAll('.btn:not(#btn-toggle-lock)');

            if (isUiLocked) {
                // ロック状態にする
                lockBtn.innerHTML = "🔒 <b>ロック中</b> (クリックで解除)";
                lockBtn.classList.add("is-locked");
                buttons.forEach(btn => {
                    btn.disabled = true;
                    // aタグ（設定ボタン）向けへの対策
                    if(btn.tagName.toLowerCase() === 'a') btn.classList.add('disabled');
                });
            } else {
                // ロック解除する
                lockBtn.innerHTML = "🔓 ロック解除中 (クリックで全操作をロック)";
                lockBtn.classList.remove("is-locked");
                buttons.forEach(btn => {
                    btn.disabled = false;
                    if(btn.tagName.toLowerCase() === 'a') btn.classList.remove('disabled');
                });
            }
        }
        // --- ここまで追加 ---
        function fetchStatus() {
            fetch('/api/status')
            .then(response => response.json())
            .then(data => {
                const container = document.getElementById('status-container');
                const btnPlay = document.getElementById('btn-play');
                const feedbackDiv = document.getElementById('feedback-container');
                
                if (data.daslight_msg) {
                    feedbackDiv.innerText = data.daslight_msg;
                    feedbackDiv.style.display = 'block';
                } else {
                    feedbackDiv.style.display = 'none';
                }

                // ▼▼ ここから追加：照明のポーズ状態を反映 ▼▼
                if (data.daslight_pause_state !== undefined) {
                    const dlPauseStatus = document.getElementById('daslight-pause-status');
                    if (data.daslight_pause_state === 1) {
                        dlPauseStatus.innerText = "一時停止中";
                        dlPauseStatus.style.color = "#f44336"; // 赤色
                    } else if (data.daslight_pause_state === 0) {
                        dlPauseStatus.innerText = "一時停止無効";
                        dlPauseStatus.style.color = "#aaa"; // グレー
                    } else {
                        dlPauseStatus.innerText = "不明";
                        dlPauseStatus.style.color = "#888"; // 初期状態
                    }
                }
                // ▲▲ ここまで追加 ▲▲

                let obsData = data.obs || data; 
                if(!data.obs && data.alive === undefined && !data.daslight_msg) obsData = data;
                let cleanObsData = {};
                for(const [k, v] of Object.entries(obsData)) {
                    // daslight_msg と daslight_pause_state をOBSリストの描画対象から除外する
                    if(k !== 'daslight_msg' && k !== 'daslight_pause_state') {
                        cleanObsData[k] = v;
                    }
                }
                obsData = cleanObsData;

                let html = '';
                if (Object.keys(obsData).length === 0) {
                    container.innerHTML = '<div style="text-align:center;">No Servers</div>';
                } else {
                    let anyPaused = false;
                    let anyPlaying = false;
                    for (const val of Object.values(obsData)) {
                        if (val && val.alive) {
                            if (val.media_state === 'OBS_MEDIA_STATE_PAUSED') anyPaused = true;
                            if (val.media_state === 'OBS_MEDIA_STATE_PLAYING') anyPlaying = true;
                        }
                    }
                    if (anyPlaying) btnPlay.innerText = "▶ 再生中"; 
                    else if (anyPaused) btnPlay.innerText = "▶ 再開";
                    else btnPlay.innerText = "▶ 再生";

                    for (const [key, val] of Object.entries(obsData)) {
                        let connBadge = val.alive ? '<span class="badge badge-ok">ON</span>' : '<span class="badge badge-ng">OFF</span>';
                        let mediaLabel = '---'; let mediaClass = '';
                        if (val.alive) {
                            switch (val.media_state) {
                                case 'OBS_MEDIA_STATE_PLAYING': mediaLabel = '▶Play'; mediaClass = 'media-playing'; break;
                                case 'OBS_MEDIA_STATE_PAUSED': mediaLabel = '⏸Pause'; mediaClass = 'media-paused'; break;
                                case 'OBS_MEDIA_STATE_STOPPED': case 'OBS_MEDIA_STATE_ENDED': mediaLabel = '⏹Stop'; mediaClass = 'media-stopped'; break;
                                default: mediaLabel = val.media_state;
                            }
                        }
                        html += `
                        <div class="status-row">
                            <div style="flex:1;">
                                <div class="status-host">${key} ${connBadge} <span class="ping-val">📶${val.ping}ms</span></div>
                                <div style="font-size:0.75rem; color:#888;">${val.target_source}</div>
                            </div>
                            <div style="text-align:right;"><div class="media-status ${mediaClass}">${mediaLabel}</div></div>
                        </div>`;
                    }
                    container.innerHTML = html;
                }
            })
            .catch(err => console.error(err));
        }
        setInterval(fetchStatus, 500);
        fetchStatus();
        {% endif %}
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, page='home', scenes=config_data["scenes"], songs=config_data["songs"])

@app.route('/settings')
def settings():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: txt = f.read()
    except: txt = ""
    return render_template_string(HTML_TEMPLATE, page='settings', config_text=txt)

@app.route('/settings/save', methods=['POST'])
def save_config():
    new_text = request.form.get('config_text')
    if new_text:
        new_text = new_text.replace('\r\n', '\n')
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f: f.write(new_text)
        load_config()
    return redirect(url_for('settings'))

@app.route('/api/status')
def api_status():
    response_data = obs_manager.get_status_snapshot()
    msg = None
    with daslight_feedback["lock"]:
        if daslight_feedback["message"] and (time.time() - daslight_feedback["timestamp"] < 5):
            msg = daslight_feedback["message"]
        # APIレスポンスにポーズ状態を追加
        response_data["daslight_pause_state"] = daslight_feedback["pause_state"]
        
    response_data["daslight_msg"] = msg
    return jsonify(response_data)

# --- 新規追加：照明のみ一時停止 ---
@app.route('/action/daslight_pause', methods=['POST'])
def action_daslight_pause():
    trigger_daslight_async('/obs/pause')
    return redirect(url_for('index'))

@app.route('/track_play', methods=['POST'])
def track_play():
    osc_addr = request.form.get('osc_addr')
    seek_ms = request.form.get('seek_ms')
    trigger_daslight_async(osc_addr)
    if seek_ms:
        ms = int(seek_ms)
        obs_manager.send_command_all('seek', ms)
        obs_manager.send_command_all('media', 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY')
    return redirect(url_for('index'))

@app.route('/action/<action_type>', methods=['POST'])
def media_action(action_type):
    if action_type == 'play':
        is_restart = obs_manager.send_smart_play_all()
        if is_restart: trigger_daslight_async('/obs/music1')
        else: trigger_daslight_async('/obs/pause')
    elif action_type == 'restart':
        trigger_daslight_async('/obs/music1')
        obs_manager.send_command_all('media', 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART')
    elif action_type == 'stop':
        trigger_daslight_async('/obs/stop')
        obs_manager.send_command_all('media', 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP')
    elif action_type == 'pause':
        trigger_daslight_async('/obs/pause')
        obs_manager.send_command_all('media', 'OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE')
    return redirect(url_for('index'))

@app.route('/scene', methods=['POST'])
def scene_action():
    s = request.form.get('scene_name')
    if s: obs_manager.send_command_all('scene', s)
    return redirect(url_for('index'))

if __name__ == '__main__':
    try:
        load_config()
        # ポート番号やIPアドレスは必要に応じて変更
        print(f"★ Webサーバー起動: http://localhost:{WEB_PORT}")
        app.run(host='0.0.0.0', port=WEB_PORT, debug=False)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\nクリティカルエラーが発生しました。")
        input("Enterキーを押すと終了します...")