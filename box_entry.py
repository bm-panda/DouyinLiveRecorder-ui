# -*- coding: utf-8 -*-
"""
不忙脚本盒子 · 直播录制管理器

把 DouyinLiveRecorder（纯命令行）接入盒子：
  盒子触发 → box_entry.py 读 argv[1] 的 JSON
  → 起本地 HTTP 服务 + webview-cli 控制面板（管理直播间 / 全局设置 / 实时日志）
  → 写入 config/URL_config.ini 与 config/config.ini
  → 后台启动上游 main.py 持续值守录制，关窗即停。

不修改上游任何源码；FFmpeg / Node / webview-cli 由盒子的 binaries 提供，
其绝对路径从 JSON 的 environment.binaries 取出并在进程内前插 PATH。
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(SCRIPT_DIR, "app")
MAIN_PY = os.path.join(APP_DIR, "main.py")
CONFIG_DIR = os.path.join(APP_DIR, "config")
CONFIG_INI = os.path.join(CONFIG_DIR, "config.ini")
URL_INI = os.path.join(CONFIG_DIR, "URL_config.ini")
UI_HTML = os.path.join(SCRIPT_DIR, "box_ui.html")
TEXT_ENC = "utf-8-sig"

QUALITIES = ["原画", "蓝光", "超清", "高清", "标清", "流畅"]
FORMATS = ["ts", "mkv", "flv", "mp4", "mp3音频", "m4a音频"]

URL_RE = re.compile(r"(https?://)?(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+(:\d+)?(/.*)?")

# UI 键 -> (ini 段, ini 选项, 类型)  类型: str / bool / int
SETTINGS_MAP = {
    "save_path": ("录制设置", "直播保存路径(不填则默认)", "str"),
    "save_format": ("录制设置", "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频", "str"),
    "default_quality": ("录制设置", "原画|超清|高清|标清|流畅", "str"),
    "folder_by_author": ("录制设置", "保存文件夹是否以作者区分", "bool"),
    "split_enabled": ("录制设置", "分段录制是否开启", "bool"),
    "split_time": ("录制设置", "视频分段时间(秒)", "int"),
    "convert_mp4": ("录制设置", "录制完成后自动转为mp4格式", "bool"),
    "loop_interval": ("录制设置", "循环时间(秒)", "int"),
    "max_threads": ("录制设置", "同一时间访问网络的线程数", "int"),
    "use_proxy": ("录制设置", "是否使用代理ip(是/否)", "bool"),
    "proxy_addr": ("录制设置", "代理地址", "str"),
    "douyin_cookie": ("Cookie", "抖音cookie", "str"),
    "kuaishou_cookie": ("Cookie", "快手cookie", "str"),
    "bilibili_cookie": ("Cookie", "b站cookie", "str"),
    "xhs_cookie": ("Cookie", "小红书cookie", "str"),
    "tiktok_cookie": ("Cookie", "tiktok_cookie", "str"),
}

SETTINGS_DEFAULT = {
    "save_path": "",
    "save_format": "ts",
    "default_quality": "原画",
    "folder_by_author": True,
    "split_enabled": True,
    "split_time": 1800,
    "convert_mp4": True,
    "loop_interval": 300,
    "max_threads": 3,
    "use_proxy": False,
    "proxy_addr": "",
    "douyin_cookie": "",
    "kuaishou_cookie": "",
    "bilibili_cookie": "",
    "xhs_cookie": "",
    "tiktok_cookie": "",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

BINS = {}
STATE = {
    "proc": None,
    "started_at": None,
    "monitoring": 0,
    "stopping": False,
}
LOGS = deque(maxlen=3000)
LOG_SEQ = [0]
LOG_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 参数与环境
# --------------------------------------------------------------------------
def load_payload():
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def setup_binaries(env):
    """从 environment.binaries 取出工具绝对路径，并把目录前插到 PATH，
    使上游 ffmpeg/node 及 execjs 能按命令名命中。"""
    bins = env.get("binaries") or {}
    for tool, mapping in bins.items():
        if not isinstance(mapping, dict):
            continue
        for cmd, path in mapping.items():
            if not path:
                continue
            BINS.setdefault(cmd, path)
            BINS.setdefault(tool, path)
            folder = os.path.dirname(path)
            if folder and folder not in os.environ.get("PATH", ""):
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    for name in ("ffmpeg", "node", "webview-cli"):
        if name not in BINS:
            found = shutil.which(name)
            if found:
                BINS[name] = found


# --------------------------------------------------------------------------
# 直播间列表 config/URL_config.ini
# --------------------------------------------------------------------------
def _parse_line(raw):
    line = raw.strip()
    if len(line) < 8:
        return None
    enabled = not line.startswith("#")
    if not enabled:
        line = line.lstrip("#").strip()
    if not line:
        return None
    parts = re.split(r"[，,]", line) if re.search(r"[，,]", line) else [line, ""]
    if len(parts) == 1:
        quality, url, name = "", parts[0], ""
    elif len(parts) == 2:
        if URL_RE.search(parts[0]):
            quality, url, name = "", parts[0], parts[1]
        else:
            quality, url, name = parts[0], parts[1], ""
    else:
        quality, url, name = parts[0], parts[1], parts[2]
    quality = quality.strip()
    if quality not in QUALITIES:
        quality = ""
    name = name.strip()
    if name.startswith("主播:"):
        name = name[len("主播:"):].strip()
    url = url.strip()
    if not url:
        return None
    return {"url": url, "quality": quality, "name": name, "enabled": enabled}


def _read_lines():
    if not os.path.isfile(URL_INI):
        return []
    with open(URL_INI, "r", encoding=TEXT_ENC, errors="ignore") as f:
        return f.read().splitlines()


def _write_lines(lines):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(URL_INI, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def _normalize(url):
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return url.rstrip("/")


def _build_line(url, quality, name, enabled=True):
    quality = quality if quality in QUALITIES else ""
    line = ("%s,%s" % (quality, url)) if quality else url
    name = (name or "").strip()
    if name:
        line += ",主播: %s" % name
    if not enabled:
        line = "#" + line
    return line


def read_rooms():
    rooms = []
    for raw in _read_lines():
        info = _parse_line(raw)
        if info:
            rooms.append(info)
    return rooms


def write_rooms(rooms):
    out = []
    for room in rooms:
        url = str(room.get("url", "")).strip()
        if not url:
            continue
        out.append(_build_line(url, room.get("quality") or "", room.get("name") or "",
                               room.get("enabled", True)))
    _write_lines(out)


def _find_room_line(lines, url):
    target = _normalize(url)
    if not target:
        return None
    for i, raw in enumerate(lines):
        info = _parse_line(raw)
        if info and _normalize(info["url"]) == target:
            return i
    return None


def room_add(url, quality, name, enabled=True):
    url = (url or "").strip()
    if not url:
        return False
    lines = _read_lines()
    if _find_room_line(lines, url) is not None:
        return room_update(url, url, quality, name)
    lines.append(_build_line(url, quality, name, enabled))
    _write_lines(lines)
    return True


def room_update(old_url, url, quality, name, enabled=None):
    url = (url or "").strip() or (old_url or "").strip()
    lines = _read_lines()
    idx = _find_room_line(lines, old_url)
    if idx is None:
        idx = _find_room_line(lines, url)
    info = _parse_line(lines[idx]) if idx is not None else None
    if info is None:
        return room_add(url, quality, name) if url else False
    if enabled is None:
        enabled = info["enabled"]
    lines[idx] = _build_line(url, quality, name, enabled)
    _write_lines(lines)
    return True


def room_toggle(url, enabled):
    lines = _read_lines()
    idx = _find_room_line(lines, url)
    if idx is None:
        return False
    info = _parse_line(lines[idx])
    lines[idx] = _build_line(info["url"], info["quality"], info["name"], bool(enabled))
    _write_lines(lines)
    return True


def room_delete(url):
    lines = _read_lines()
    idx = _find_room_line(lines, url)
    if idx is None:
        return False
    del lines[idx]
    _write_lines(lines)
    return True


# --------------------------------------------------------------------------
# 全局设置 config/config.ini（行级打补丁，保留注释与键名大小写）
# --------------------------------------------------------------------------
def read_settings():
    import configparser
    result = dict(SETTINGS_DEFAULT)
    if not os.path.isfile(CONFIG_INI):
        return result
    with open(CONFIG_INI, "r", encoding=TEXT_ENC, errors="ignore") as f:
        lines = _dedupe_ini(f.read().splitlines())
    parser = configparser.RawConfigParser()
    try:
        parser.read_string("\n".join(lines))
    except Exception:
        return result
    for key, (section, option, kind) in SETTINGS_MAP.items():
        try:
            raw = parser.get(section, option)
        except Exception:
            continue
        raw = raw.strip()
        if kind == "bool":
            result[key] = raw == "是"
        elif kind == "int":
            try:
                result[key] = int(float(raw))
            except ValueError:
                pass
        else:
            result[key] = raw
    return result


def _dedupe_ini(lines):
    """按段内、忽略大小写去重重复的 `键 = 值` 行（保留最后一次的值，占首次出现位置）。
    上游 configparser 读取不区分大小写，借此修复历史写入造成的重复键。"""
    out = []
    seen = {}
    section = None
    opt_re = re.compile(r"^\s*([^#;=\s][^=:]*?)\s*[=:]")
    for ln in lines:
        head = re.match(r"^\s*\[(.+?)\]\s*$", ln)
        if head:
            section = head.group(1).strip().lower()
            seen = {}
            out.append(ln)
            continue
        mo = opt_re.match(ln)
        if mo and section is not None:
            name = mo.group(1).strip().lower()
            if name in seen:
                out[seen[name]] = ln
                continue
            seen[name] = len(out)
        out.append(ln)
    return out


def _ini_set(lines, section, option, value):
    sec_start = None
    sec_end = len(lines)
    for i, ln in enumerate(lines):
        m = re.match(r"^\s*\[(.+?)\]\s*$", ln)
        if m and m.group(1).strip() == section:
            sec_start = i
            sec_end = len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*\[.*?\]\s*$", lines[j]):
                    sec_end = j
                    break
            break
    if sec_start is None:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("[%s]" % section)
        lines.append("%s = %s" % (option, value))
        return
    pat = re.compile(r"^\s*" + re.escape(option) + r"\s*[=:]", re.IGNORECASE)
    for k in range(sec_start + 1, sec_end):
        if pat.match(lines[k]):
            lines[k] = "%s = %s" % (option, value)
            return
    lines.insert(sec_end, "%s = %s" % (option, value))


def write_settings(settings):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.isfile(CONFIG_INI):
        with open(CONFIG_INI, "r", encoding=TEXT_ENC, errors="ignore") as f:
            lines = f.read().splitlines()
    else:
        lines = []
    lines = _dedupe_ini(lines)
    for key, (section, option, kind) in SETTINGS_MAP.items():
        if key not in settings:
            continue
        value = settings[key]
        if kind == "bool":
            text = "是" if value else "否"
        else:
            text = str(value)
        _ini_set(lines, section, option, text)
    with open(CONFIG_INI, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    save_path = str(settings.get("save_path") or "").strip()
    if save_path:
        try:
            os.makedirs(save_path, exist_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 录制子进程
# --------------------------------------------------------------------------
def _log(text):
    with LOG_LOCK:
        LOG_SEQ[0] += 1
        LOGS.append({"seq": LOG_SEQ[0], "text": text})


def _reader(proc):
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            m = re.search(r"共监测(\d+)个直播中", line)
            if m:
                STATE["monitoring"] = int(m.group(1))
            if line:
                _log(line)
    except Exception:
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass


def combined_logs(after):
    if after >= LOG_SEQ[0]:
        return []
    with LOG_LOCK:
        return [item for item in LOGS if item["seq"] > after]


def is_running():
    proc = STATE["proc"]
    return proc is not None and proc.poll() is None


def stop_recorder():
    proc = STATE["proc"]
    STATE["proc"] = None
    STATE["monitoring"] = 0
    STATE["started_at"] = None
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                creationflags=CREATE_NO_WINDOW, capture_output=True,
            )
        else:
            proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def start_recorder(rooms=None, settings=None):
    stop_recorder()
    if rooms is not None:
        write_rooms(rooms)
    if settings is not None:
        write_settings(settings)
    STATE["monitoring"] = 0
    _log("[管理器] 启动录制进程...")
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", MAIN_PY],
        cwd=APP_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    STATE["proc"] = proc
    STATE["started_at"] = time.time()
    threading.Thread(target=_reader, args=(proc,), daemon=True).start()
    return proc


atexit.register(stop_recorder)


def pick_folder():
    """弹出 Windows 原生文件夹选择框，返回所选路径。"""
    try:
        ps = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description='选择直播保存目录'; "
            "$d.ShowNewFolderButton=$true; "
            "if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){[Console]::Out.Write($d.SelectedPath)}"
        )
        proc = subprocess.run(
            [ps, "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW, timeout=300,
        )
        path = (proc.stdout or "").strip()
        if path:
            return {"ok": True, "path": path}
        return {"ok": False, "msg": "已取消选择", "path": ""}
    except Exception as exc:
        return {"ok": False, "msg": "无法打开文件夹选择框: %s" % exc, "path": ""}


# --------------------------------------------------------------------------
# 本地 HTTP 服务
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False))

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", self.server.page_bytes)
        elif path == "/api/state":
            self._json({
                "rooms": read_rooms(),
                "settings": read_settings(),
                "qualities": QUALITIES,
                "formats": FORMATS,
                "suggested_save_dir": os.path.join(os.path.expanduser("~"), "Videos", "直播录制"),
            })
        elif path == "/api/status":
            self._json({
                "running": is_running(),
                "monitoring": STATE["monitoring"],
                "elapsed": int(time.time() - STATE["started_at"]) if STATE["started_at"] else 0,
            })
        elif path == "/api/logs":
            qs = parse_qs(parsed.query)
            try:
                after = int(qs.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json({"lines": combined_logs(after), "next": LOG_SEQ[0]})
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/settings":
            data = self._body()
            write_settings(data.get("settings", data))
            self._json({"ok": True, "msg": "设置已保存"})
        elif path == "/api/room/add":
            d = self._body()
            ok = room_add(d.get("url", ""), d.get("quality", ""), d.get("name", ""),
                          d.get("enabled", True))
            self._json({"ok": ok, "msg": "已添加" if ok else "无有效地址"},
                       200 if ok else 400)
        elif path == "/api/room/update":
            d = self._body()
            ok = room_update(d.get("old_url", ""), d.get("url", ""),
                             d.get("quality", ""), d.get("name", ""), d.get("enabled"))
            self._json({"ok": ok, "msg": "已更新" if ok else "未找到该直播间"},
                       200 if ok else 404)
        elif path == "/api/room/toggle":
            d = self._body()
            ok = room_toggle(d.get("url", ""), d.get("enabled", True))
            self._json({"ok": ok, "msg": "已更新" if ok else "未找到该直播间"},
                       200 if ok else 404)
        elif path == "/api/room/delete":
            d = self._body()
            ok = room_delete(d.get("url", ""))
            self._json({"ok": ok, "msg": "已删除" if ok else "未找到该直播间"},
                       200 if ok else 404)
        elif path == "/api/pick-folder":
            self._json(pick_folder())
        elif path == "/api/open-folder":
            sp = str(read_settings().get("save_path") or "").strip()
            if not sp:
                self._json({"ok": False, "msg": "请先设置保存目录"}, 400)
                return
            try:
                os.makedirs(sp, exist_ok=True)
                opener = getattr(os, "startfile", None)
                if opener:
                    opener(sp)
                else:
                    subprocess.Popen(["xdg-open", sp])
            except Exception as exc:
                self._json({"ok": False, "msg": "无法打开目录: %s" % exc}, 500)
                return
            self._json({"ok": True, "msg": "已打开保存目录"})
        elif path == "/api/start":
            data = self._body()
            settings = data.get("settings")
            if data.get("rooms") is not None:
                write_rooms(data.get("rooms"))
            if settings is not None:
                write_settings(settings)
            enabled = [r for r in read_rooms() if r.get("enabled")]
            if not enabled:
                self._json({"ok": False, "msg": "请至少添加并启用一个直播间"}, 400)
                return
            if not str(read_settings().get("save_path") or "").strip():
                self._json({"ok": False, "msg": "请先设置保存目录"}, 400)
                return
            try:
                start_recorder()
            except Exception as exc:
                self._json({"ok": False, "msg": "启动失败: %s" % exc}, 500)
                return
            self._json({"ok": True, "msg": "已开始录制"})
        elif path == "/api/stop":
            stop_recorder()
            _log("[管理器] 已停止录制")
            self._json({"ok": True, "msg": "已停止"})
        else:
            self._send(404, "text/plain; charset=utf-8", "not found")


class LocalServer(object):
    def __init__(self):
        with open(UI_HTML, "rb") as f:
            self.page_bytes = f.read()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.page_bytes = self.page_bytes
        self.port = self.httpd.server_address[1]
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return "http://127.0.0.1:%d/" % self.port

    def shutdown(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------
def run_window(url):
    exe = BINS.get("webview-cli") or shutil.which("webview-cli") or "webview-cli"
    cmd = [
        exe,
        "--title", "直播录制管理器",
        "--width", "1040",
        "--height", "780",
        url,
    ]
    return subprocess.run(
        cmd, input="", text=True, encoding="utf-8", errors="replace",
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )


# --------------------------------------------------------------------------
def main():
    payload = load_payload()
    env = payload.get("environment") or {}
    setup_binaries(env)

    invoke_mode = env.get("invoke_mode", "manual")

    if invoke_mode != "manual":
        # 无人值守：直接用现有配置运行上游
        rc = subprocess.call(
            [sys.executable, "-u", MAIN_PY],
            cwd=APP_DIR,
        )
        sys.exit(rc)

    server = LocalServer()
    url = server.start()
    try:
        run_window(url)
    finally:
        stop_recorder()
        server.shutdown()


if __name__ == "__main__":
    main()
