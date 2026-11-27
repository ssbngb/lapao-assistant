"""步道乐跑控制台 —— 摇杆 / 路线 / 工具

设计要点：
    摇杆和路线页共用一个 Runner。离开摇杆页或窗口失焦时停止手动移动。

    摇杆      手动控制方向
    路线      用画笔绘制路线、选择起点、沿路线跑
    工具      环境自检、轨迹预演和配置

用法：
    .venv\\Scripts\\python.exe app.py
或双击「控制台.bat」
"""
import os, sys, math, json, time, threading, subprocess, urllib.request
import random
import webbrowser
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, font as tkfont

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

sys.stdout.reconfigure(encoding="utf-8", errors="replace") if sys.stdout else None
FROZEN = getattr(sys, "frozen", False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
os.chdir(HERE)
if not FROZEN:
    sys.path.insert(0, HERE)

from PIL import Image, ImageTk
try:
    _BILINEAR = Image.Resampling.BILINEAR      # Pillow 10+
except AttributeError:
    _BILINEAR = Image.BILINEAR
from lepao.mumu import MuMuLocationClient
from lepao.geo import (offset_meters, haversine,
                       gcj02_to_wgs84, wgs84_to_gcj02)
from lepao.sensors import StepFeeder, cadence_for_speed
from lepao.track import Track, smooth_stroke
from lepao.navigation import plan_return
from screen_state import ScreenReader, read_screen_state

CONFIG_PATH = os.path.join(HERE, "run_config.json")
LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "logo.ico")
ROUTE_PATH = os.path.join(HERE, "route.json")
TILE_DIR = os.path.join(HERE, "_tiles")
TILE_SIZE = 256
MAX_TILE_Z = 18          # 高德瓦片实测上限：z=19/20 无数据
MIN_TILE_Z = 3           # z=3 即可看全国轮廓
MAX_MAP_LAT = 85.05112878
TILE_WORKERS = 6
TILE_CACHE_SIZE = 128
TILE_RETRY_S = 15
BRUSH_SAMPLE_PX = 4
BRUSH_MIN_LENGTH_PX = 8
MAX_ROUTE_LAPS = 200
SPEED_HOLD_S = 30
VARY_SPEED_LABEL = f"随机变速（约每 {SPEED_HOLD_S} 秒，±10%）"
START_FIX_TOLERANCE_M = 2.5
START_FIX_TIMEOUT_S = 6
PYTHON = (os.path.join(os.path.dirname(sys.executable), "python.exe")
          if os.path.basename(sys.executable).lower() == "pythonw.exe"
          else sys.executable)

DEFAULTS = {
    "km": 2.05,
    # 起始坐标为占位值，首次使用请在「路线」页搜索学校或直接点地图设起点
    "lat": 0.0, "lon": 0.0,
    "vm_index": 0, "v_max": 6.0,
    "push_interval": 1.0, "read_interval": 0.5,
}

BACKGROUND = "#eef3f6"
SURFACE = "#ffffff"
INK = "#142638"
MUTED = "#516779"
BORDER = "#d8e3e9"
HEADER = "#142a39"
ACCENT = "#087862"
ACCENT_DARK = "#06604e"
ACCENT_LIGHT = "#def2eb"
DANGER = "#a92d39"
UI_FONT = "Microsoft YaHei UI"
MONO_FONT = "Consolas"


def _style_console(root):
    root.configure(background=BACKGROUND)
    for name in ("TkDefaultFont", "TkTextFont"):
        tkfont.nametofont(name, root=root).configure(family=UI_FONT, size=9)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BACKGROUND, foreground=INK, bordercolor=BORDER)
    style.configure("TFrame", background=BACKGROUND)
    style.configure("Card.TFrame", background=SURFACE, borderwidth=1, relief="solid")
    style.configure("Surface.TFrame", background=SURFACE)
    style.configure("Header.TFrame", background=HEADER)
    style.configure("Title.TLabel", background=HEADER, foreground=SURFACE,
                    font=(UI_FONT, 15, "bold"))
    style.configure("Connection.TLabel", background=HEADER, foreground="#d9e8ee")
    style.configure("Card.TLabel", background=SURFACE)
    style.configure("Section.TLabel", background=SURFACE, font=(UI_FONT, 10, "bold"))
    style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=(UI_FONT, 8))
    style.configure("Metric.TLabel", background=SURFACE, font=(MONO_FONT, 20, "bold"))
    style.configure("Accent.TLabel", background=SURFACE, foreground=ACCENT_DARK,
                    font=(MONO_FONT, 11, "bold"))
    style.configure("Distance.TLabel", background=SURFACE, foreground=ACCENT_DARK,
                    font=(MONO_FONT, 18, "bold"))
    style.configure("Footer.TLabel", foreground=MUTED, font=(UI_FONT, 8))
    style.configure("Details.TButton", font=(UI_FONT, 8), padding=(8, 0))

    style.configure("TNotebook", background=BACKGROUND, borderwidth=0, tabmargins=(0, 0, 0, 3))
    style.configure("TNotebook.Tab", background=BACKGROUND, foreground=MUTED,
                    padding=(16, 6), borderwidth=0, focuscolor=ACCENT)
    style.map("TNotebook.Tab", background=[("selected", SURFACE), ("active", "#e1eaf0")],
              foreground=[("selected", ACCENT_DARK)], padding=[])
    style.configure("TButton", background="#f2f6f8", foreground=INK, padding=(10, 4),
                    width=0, relief="flat", borderwidth=1, focuscolor=ACCENT)
    style.map("TButton", background=[("disabled", "#e9eff2"), ("pressed", "#d9e7ee"),
                                     ("active", "#e6eff4")],
              foreground=[("disabled", "#6d8190")])
    for name, color, hover in (("Primary", ACCENT, ACCENT_DARK), ("Stop", DANGER, "#84202b")):
        style.configure(f"{name}.TButton", background=color, foreground=SURFACE)
        style.map(f"{name}.TButton", foreground=[("disabled", "#6d8190")],
                  background=[("disabled", "#e9eff2"), ("pressed", hover), ("active", hover)])
    style.layout("Tool.TRadiobutton", style.layout("TButton"))
    style.configure("Tool.TRadiobutton", background="#f2f6f8", padding=(10, 4),
                    relief="flat", anchor="center", focuscolor=ACCENT)
    style.map("Tool.TRadiobutton", background=[("selected", ACCENT_LIGHT), ("active", "#e6eff4")],
              foreground=[("selected", ACCENT_DARK)])
    style.configure("TEntry", fieldbackground=SURFACE, padding=4)
    style.configure("TCombobox", fieldbackground=SURFACE, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
    style.configure("Horizontal.TScale", background=ACCENT, troughcolor="#e3edf0", sliderlength=18)
    style.map("Horizontal.TScale", background=[("active", ACCENT_DARK)])


def _card(parent, title=None, *, padding=12):
    card = ttk.Frame(parent, style="Card.TFrame", padding=padding)
    if title:
        ttk.Label(card, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 8))
    return card


def _field(parent, label, variable, width=14):
    field = ttk.Frame(parent, style="Surface.TFrame")
    ttk.Label(field, text=label, style="Muted.TLabel").pack(anchor="w", pady=(0, 3))
    ttk.Entry(field, textvariable=variable, width=width).pack(fill="x")
    return field


class LogView(tk.Text):
    def __init__(self, parent, *, height=6):
        frame = ttk.Frame(parent, style="Surface.TFrame")
        frame.pack(fill="both", expand=True)
        super().__init__(frame, bg="#f4f7fa", fg=INK, font=(MONO_FONT, 9), wrap="word",
                         height=height, width=1, padx=8, pady=6, relief="flat", borderwidth=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT, state="disabled")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.yview)
        self.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.pack(side="left", fill="both", expand=True)

    def append(self, text):
        at_end = self.yview()[1] >= 0.999
        self.configure(state="normal")
        self.insert("end", text)
        self.configure(state="disabled")
        if at_end:
            self.see("end")

    def clear(self):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")


def _number(value, label, minimum, maximum=None):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是数字")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}必须是数字") from exc
    if not math.isfinite(number) or number < minimum or (
            maximum is not None and number > maximum):
        raise ValueError(f"{label}超出范围")
    return number


def _coordinates(lat, lon):
    return _number(lat, "纬度", -90, 90), _number(lon, "经度", -180, 180)


def _route_laps(value):
    text = str(value).strip()
    if not text.isascii() or not text.isdecimal() or len(text) > 3 or not 1 <= int(text) <= MAX_ROUTE_LAPS:
        raise ValueError(f"圈数须为 1～{MAX_ROUTE_LAPS} 的整数")
    return int(text)


def _pace_text(seconds_per_km):
    minutes, seconds = divmod(round(seconds_per_km), 60)
    return f"{minutes}'{seconds:02d}\""


def _selected_speed(variable):
    # ttk.Scale 给出连续值；让显示、配速换算与实际采用的速度共用 0.1 m/s 精度。
    speed = round(variable.get(), 1)
    variable.set(speed)
    return speed


def _random_speeds(base_speed):
    """每档速度保持约 30 秒，到期后在下一个定位点切换。"""
    low = max(1.0, base_speed * 0.9)
    high = min(6.0, base_speed * 1.1)
    while True:
        speed = random.uniform(low, high)
        change_at = time.monotonic() + SPEED_HOLD_S
        while time.monotonic() < change_at:
            yield speed


def _route_distances(track):
    """单圈按不超过 2.5 米的间距取样，并保留每段的终点。"""
    for start, end in zip(track.cum, track.cum[1:]):
        steps = math.ceil((end - start) / 2.5)
        for i in range(1, steps):
            yield start + (end - start) * (i / steps)
        yield end


def _interval(value, label):
    seconds = _number(value, label, 0)
    if seconds == 0 or seconds > min(threading.TIMEOUT_MAX, 2_147_483):
        raise ValueError(f"{label}必须大于零且在系统支持范围内")
    return seconds


def _validated_config(data):
    if not isinstance(data, dict):
        raise ValueError("配置必须是 JSON 对象")
    config = {**DEFAULTS, **data}
    config["lat"], config["lon"] = _coordinates(config["lat"], config["lon"])
    config["push_interval"] = _interval(config["push_interval"], "定位下发间隔")
    config["read_interval"] = _interval(config["read_interval"], "读屏间隔")
    return config


def _atomic_write(path, data):
    filename = None
    try:
        with NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(path)),
                                 suffix=".tmp", delete=False) as f:
            filename = f.name
            f.write(data)
        os.replace(filename, path)
        filename = None
    finally:
        if filename is not None:
            os.unlink(filename)


def _save_json(path, data):
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2,
                                   allow_nan=False).encode("utf-8"))


def _terminate_process(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            proc.terminate()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _explain(ex):
    """把 MuMu 的原始异常压成一句人话。"""
    t = str(ex)
    low = t.lower()
    if "player not running" in low:
        return "MuMu 模拟器没启动"
    if "10061" in t or "积极拒绝" in t or "refused" in low:
        return "连不上 MuMu 网关（模拟器没启动，或端口不是 24576）"
    if "timed out" in low or "超时" in t:
        return "MuMu 网关超时"
    first = next((s.strip() for s in t.splitlines() if s.strip()), "未知原因")
    return first[:100]


# ============ 共享定位引擎 ============
class Runner:
    """所有页签共用的定位 + 步频引擎。"""

    def __init__(self):
        self.client = MuMuLocationClient()
        self.config_error = ""
        self.cfg = self._load_cfg()
        self.trail = deque(maxlen=20000)
        self.motion_lock = threading.Lock()
        self.bearing = None          # None = 停止
        self.speed = 2.8
        self._manual_speeds = None
        self.manual_speed = None     # 最近一次手动移动使用的速度
        self.push_interval = self.cfg["push_interval"]
        self.lock = threading.Lock()
        self._stopped = threading.Event()
        self.total_m = 0.0
        self.last_error = ""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return _validated_config(json.load(f))
        except FileNotFoundError:
            return dict(DEFAULTS)
        except (OSError, ValueError) as exc:
            self.config_error = f"配置加载失败，已使用默认值：{exc}"
            return dict(DEFAULTS)

    def save_cfg(self, cfg):
        config = _validated_config({**self.cfg, **cfg})
        _save_json(CONFIG_PATH, config)
        with self.lock:
            self.cfg = config
            self.push_interval = config["push_interval"]
        self.config_error = ""

    def go(self, bearing):
        with self.lock:
            self.bearing = bearing

    @property
    def home(self):
        point = (self.cfg["lat"], self.cfg["lon"])
        if self.cfg.get("home_set", point != (0.0, 0.0)):
            return point
        return None

    def navigation_snapshot(self, cancel):
        # Finish any in-flight push before reading GPS. A cancelled GPS read
        # must neither hold up the next movement nor replace its position.
        with self.motion_lock:
            if cancel.is_set():
                return None, ()
        current = self._read_position()
        with self.motion_lock:
            if cancel.is_set():
                return None, ()
            if self.trail and haversine(*self.trail[-1], *current) > 2.5:
                self.trail.clear()  # 模拟器已从外部移动，两段轨迹不相连
            if not self.trail or self.trail[-1] != current:
                self.trail.append(current)
            return current, list(self.trail)

    def _read_position(self):
        gps = self.client.gps_status()
        if not gps["enabled"] or gps["location"] is None:
            raise ValueError("尚未取得模拟器 GPS 位置，请开启定位后重试")
        return _coordinates(*gps["location"])

    def sync_start(self, home, cancel, waiting_for_gps):
        """初始化模拟器定位，回读确认后从起点重新记录轨迹。"""
        with self.motion_lock:
            if cancel.is_set() or self._stopped.is_set():
                return False
            self.trail.clear()
            self.client.set_wgs84(*home)
        deadline = None
        waiting = False
        while not cancel.is_set() and not self._stopped.is_set():
            gps = self.client.gps_status()
            if not gps["enabled"]:
                raise ValueError("模拟器定位未开启，请开启后重试")
            if gps["active"] is False:
                if not waiting:
                    waiting_for_gps()
                    waiting = True
                deadline = None
                cancel.wait(1)
                continue
            if deadline is None:
                deadline = time.monotonic() + START_FIX_TIMEOUT_S
            fix = gps["location"]
            gap = haversine(*home, *_coordinates(*fix)) if fix is not None else None
            if gap is not None and gap <= START_FIX_TOLERANCE_M:
                with self.motion_lock:
                    if cancel.is_set() or self._stopped.is_set():
                        return False
                    self.trail.append(home)
                    self.last_error = ""
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                detail = "未读到 GPS 位置" if gap is None else f"GPS 仍距起点 {gap:.1f} 米"
                raise ValueError(f"已发送起点，{detail}，请检查模拟器定位后重试")
            cancel.wait(min(0.4, remaining))
        return False

    def _position(self):
        if not self.trail:
            self.trail.append(self._read_position())
        return self.trail[-1]

    def halt(self):
        with self.lock:
            self.bearing = None
            self._manual_speeds = None
            self.manual_speed = None

    def set_speed(self, v, vary_speed=False):
        base = max(1.0 if vary_speed else 0.5, min(6.0, v))
        with self.lock:
            if base == self.speed and vary_speed == (self._manual_speeds is not None):
                return
            self.speed = base
            self._manual_speeds = _random_speeds(base) if vary_speed else None

    def _push(self, lat, lon):
        """调用方持有 motion_lock。只记录网关确认的位移。"""
        try:
            self.client.set_wgs84(lat, lon)
        except Exception as e:
            self.last_error = _explain(e)
            self.trail.clear()  # 下次移动前重新读取 GPS
            raise
        point = (lat, lon)
        if self.trail:
            self.total_m += haversine(*self.trail[-1], *point)
        if point == self.home:
            self.trail.clear()
        self.trail.append(point)
        self.last_error = ""

    def move_to(self, lat, lon, cancel):
        with self.motion_lock:
            if cancel.is_set() or self._stopped.is_set():
                return False
            self._push(lat, lon)
            return True

    def _loop(self):
        while not self._stopped.is_set():
            with self.lock:
                b = self.bearing
                iv = self.push_interval
            if b is None:
                self._stopped.wait(0.06)
                continue
            try:
                with self.motion_lock:
                    with self.lock:
                        if self.bearing != b or self._stopped.is_set():
                            continue
                    current = self._position()
                    with self.lock:
                        if self.bearing != b or self._stopped.is_set():
                            continue
                        v = next(self._manual_speeds) if self._manual_speeds is not None else self.speed
                        self.manual_speed = v
                    step = v * iv
                    bb = (b + (time.time() % 7 - 3.5) * 0.6) % 360.0
                    dn = math.cos(math.radians(bb)) * step
                    de = math.sin(math.radians(bb)) * step
                    self._push(*offset_meters(*current, dn, de))
            except Exception as exc:
                self.last_error = _explain(exc)
                self.halt()
                self.feeder.pause()
            self._stopped.wait(iv)

    def shutdown(self):
        self._stopped.set()
        self.halt()

    def is_alive(self):
        return self._thread.is_alive()


# ============ 瓦片地图 ============
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def lonlat_to_tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(max(-MAX_MAP_LAT, min(MAX_MAP_LAT, lat)))
    y = (1.0 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2.0 * n
    return x, y


def tile_to_lonlat(tx, ty, z):
    n = 2 ** z
    lon = tx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    return lat, lon


def fetch_tile(z, x, y):
    path = os.path.join(TILE_DIR, str(z), str(x), f"{y}.png")
    if os.path.exists(path):
        try:
            with Image.open(path) as source:
                if source.size == (TILE_SIZE, TILE_SIZE):
                    return source.convert("RGB")
        except (OSError, ValueError):
            pass
    url = (f"https://webrd0{1 + (x + y) % 4}.is.autonavi.com/appmaptile"
           f"?x={x}&y={y}&z={z}&lang=zh_cn&size=1&scl=1&style=8")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _opener.open(req, timeout=10) as r:
            data = r.read(1_048_577)
        if len(data) > 1_048_576:
            return None
        with Image.open(BytesIO(data)) as source:
            if source.size != (TILE_SIZE, TILE_SIZE):
                return None
            image = source.convert("RGB")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _atomic_write(path, data)
        except OSError:
            pass
        return image
    except (OSError, ValueError):
        return None


class TileCache(OrderedDict):
    capacity = TILE_CACHE_SIZE

    def resize(self, capacity):
        self.capacity = capacity
        while len(self) > capacity:
            self.popitem(last=False)

    def get(self, key, default=None):
        if key not in self:
            return default
        self.move_to_end(key)
        return self[key]

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self.capacity:
            self.popitem(last=False)


class MapView(tk.Canvas):
    def __init__(self, master, on_click=None, on_stroke=None, center=None, **kw):
        super().__init__(master, bg="#e8e8e4", highlightthickness=0, **kw)
        self.z = 17
        base = center or (DEFAULTS["lat"], DEFAULTS["lon"])
        glon, glat = wgs84_to_gcj02(base[1], base[0])
        self.lat, self.lon = glat, glon
        self.on_click = on_click
        self.on_stroke = on_stroke
        self.tool = "start"
        self._cache = TileCache()
        self._pil_cache = TileCache()
        self._placeholder = TileCache()
        self._failed = TileCache()
        self._pending = set()
        self._completed = SimpleQueue()
        self._pool = ThreadPoolExecutor(max_workers=TILE_WORKERS, thread_name_prefix="tiles")
        self._next_retry = math.inf
        self._visible_images = []
        self._closed = False
        self._drag = None
        self._moved = False
        self._stroke = None
        self._stroke_item = None
        self.route_start = None
        self.route_points = []
        self.robot = None
        self.preview_point = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._dragm)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<ButtonPress-3>", self._pan_press)
        self.bind("<B3-Motion>", lambda e: self._pan_motion(e, 3))
        self.bind("<ButtonRelease-3>", self._pan_release)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Escape>", self._cancel_gesture)
        self.bind("<Unmap>", self._cancel_gesture)
        self.bind("<Configure>", self._resize)
        # Tk 的 after() 不能在下载线程里调，改由主线程轮询补画
        self._poll_id = self.after(200, self._poll)

    def _poll(self):
        changed = False
        while True:
            try:
                key, future = self._completed.get_nowait()
            except Empty:
                break
            self._pending.discard(key)
            try:
                image = future.result()
            except Exception:
                image = None
            if image is None:
                self._failed[key] = time.monotonic() + TILE_RETRY_S
            else:
                self._failed.pop(key, None)
                self._pil_cache[key] = image
            changed = True
        if changed or time.monotonic() >= self._next_retry:
            self.redraw()
        self._poll_id = self.after(200, self._poll)

    def close(self):
        self._closed = True
        self.after_cancel(self._poll_id)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def screen_to_latlon(self, sx, sy):
        cx, cy = lonlat_to_tile(self.lat, self.lon, self.z)
        w, h = self.winfo_width(), self.winfo_height()
        return tile_to_lonlat(cx + (sx - w / 2) / TILE_SIZE,
                              cy + (sy - h / 2) / TILE_SIZE, self.z)

    def latlon_to_screen(self, lat, lon):
        cx, cy = lonlat_to_tile(self.lat, self.lon, self.z)
        tx, ty = lonlat_to_tile(lat, lon, self.z)
        w, h = self.winfo_width(), self.winfo_height()
        return (w / 2 + (tx - cx) * TILE_SIZE, h / 2 + (ty - cy) * TILE_SIZE)

    def _press(self, e):
        if self._drag is not None:
            return
        if self.tool == "brush" and self.on_stroke is not None:
            self.focus_set()
            self._stroke = [(e.x, e.y)]
        else:
            self._pan_press(e)

    def _dragm(self, e):
        if self._stroke is None:
            self._pan_motion(e, 1)
            return
        point = self._brush_point(e)
        if math.dist(self._stroke[-1], point) >= BRUSH_SAMPLE_PX:
            self._stroke.append(point)
            self._draw_stroke()

    def _release(self, e):
        if self._stroke is None:
            if self._pan_release(e) and self.tool == "start" and not self._moved and self.on_click:
                self.on_click(*self.screen_to_latlon(e.x, e.y))
            return
        points = self._stroke
        endpoint = self._brush_point(e)
        if endpoint != points[-1]:
            points.append(endpoint)
        self.cancel_stroke()
        if sum(map(math.dist, points, points[1:])) < BRUSH_MIN_LENGTH_PX:
            return
        self.on_stroke([self.screen_to_latlon(*point) for point in smooth_stroke(points)])

    def _brush_point(self, e):
        return (max(0, min(e.x, self.winfo_width() - 1)),
                max(0, min(e.y, self.winfo_height() - 1)))

    def _draw_stroke(self):
        if self._stroke is None or len(self._stroke) < 2:
            return
        coords = [coordinate for point in smooth_stroke(self._stroke) for coordinate in point]
        if self._stroke_item is None:
            self._stroke_item = self.create_line(
                coords, fill="#1264db", width=3, capstyle="round", joinstyle="round")
        else:
            self.coords(self._stroke_item, *coords)

    def set_tool(self, tool):
        self._cancel_gesture()
        self.tool = tool
        self.configure(cursor="" if tool == "pan" else "crosshair")

    def cancel_stroke(self):
        self._stroke = None
        if self._stroke_item is not None:
            self.delete(self._stroke_item)
            self._stroke_item = None

    def _cancel_gesture(self, _e=None):
        self._drag = None
        self.cancel_stroke()

    def _pan_press(self, e):
        if self._stroke is None and self._drag is None:
            self._drag = (e.num, e.x, e.y, self.lat, self.lon)
            self._moved = False

    def _pan_motion(self, e, button):
        if self._drag is None or self._drag[0] != button:
            return
        _, x0, y0, la0, lo0 = self._drag
        dx, dy = e.x - x0, e.y - y0
        self._moved = self._moved or abs(dx) + abs(dy) > 4
        if not self._moved:
            return
        cx, cy = lonlat_to_tile(la0, lo0, self.z)
        self.lat, self.lon = tile_to_lonlat(cx - dx / TILE_SIZE, cy - dy / TILE_SIZE, self.z)
        self.redraw()

    def _pan_release(self, e):
        if self._drag is None or self._drag[0] != e.num:
            return False
        self._drag = None
        return True

    def _resize(self, _e):
        self._cancel_gesture()
        self.redraw()

    def _wheel(self, e):
        self.zoom(1 if e.delta > 0 else -1, e.x, e.y)

    def zoom(self, step, x=None, y=None):
        if self._stroke is not None or self._drag is not None:
            return
        z = max(MIN_TILE_Z, min(MAX_TILE_Z, self.z + step))
        if z == self.z:
            return
        center_x, center_y = self.winfo_width() / 2, self.winfo_height() / 2
        x = center_x if x is None else x
        y = center_y if y is None else y
        lat, lon = self.screen_to_latlon(x, y)
        tx, ty = lonlat_to_tile(lat, lon, z)
        self.z = z
        self.lat, self.lon = tile_to_lonlat(tx - (x - center_x) / TILE_SIZE,
                                          ty - (y - center_y) / TILE_SIZE, z)
        self.redraw()

    def jump_to(self, lat_wgs, lon_wgs, z=17):
        """跳转到指定 WGS-84 坐标。"""
        self._cancel_gesture()
        glon, glat = wgs84_to_gcj02(lon_wgs, lat_wgs)
        self.lat, self.lon = glat, glon
        self.z = max(MIN_TILE_Z, min(MAX_TILE_Z, z))
        self.redraw()

    def redraw(self):
        """重绘。关键：缺瓦片时用「上一层级放大」做过渡底图，绝不白屏。"""
        if self._closed:
            return
        self._next_retry = math.inf
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        self.delete("all")
        self._stroke_item = None
        self._visible_images.clear()
        cx, cy = lonlat_to_tile(self.lat, self.lon, self.z)
        n = 2 ** self.z
        x0, x1 = int(cx - w / (2*TILE_SIZE)) - 1, int(cx + w / (2*TILE_SIZE)) + 1
        y0, y1 = int(cy - h / (2*TILE_SIZE)) - 1, int(cy + h / (2*TILE_SIZE)) + 1
        visible_tiles = (x1 - x0 + 1) * (y1 - y0 + 1)
        for cache in (self._cache, self._pil_cache, self._placeholder, self._failed):
            cache.resize(max(TILE_CACHE_SIZE, visible_tiles * 2))

        missing = set()
        parents = set()
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                if not (0 <= ty < n):
                    continue
                k = (self.z, tx % n, ty)
                sx = w/2 + (tx - cx) * TILE_SIZE
                sy = h/2 + (ty - cy) * TILE_SIZE
                img = self._img(k)
                if img is not None:
                    self._visible_images.append(img)
                    self.create_image(sx, sy, image=img, anchor="nw")
                else:
                    missing.add(k)
                    ph = self._placeholder_tile(*k, parents)
                    if ph is not None:
                        self._visible_images.append(ph)
                        self.create_image(sx, sy, image=ph, anchor="nw")

        now = time.monotonic()
        failed = 0
        for keys in (missing, parents):
            for key in sorted(keys):
                retry_at = self._failed.get(key, 0)
                if retry_at > now:
                    self._next_retry = min(self._next_retry, retry_at)
                    if key in missing:
                        failed += 1
                elif key not in self._pending and len(self._pending) < TILE_WORKERS:
                    self._pending.add(key)
                    future = self._pool.submit(fetch_tile, *key)
                    future.add_done_callback(lambda result, k=key: self._completed.put((k, result)))
        self._markers()
        self._draw_stroke()

        if missing:
            tip = f"地图加载中… 剩 {len(missing) - failed} 块" if len(missing) > failed else ""
            if failed:
                tip = f"{tip}  {failed} 块加载失败，稍后重试".strip()
            label = self.create_text(w - 18, h - 14, text=tip, anchor="se",
                                     fill=INK, font=(UI_FONT, 9))
            x1, y1, x2, y2 = self.bbox(label)
            backdrop = self.create_rectangle(x1-6, y1-3, x2+6, y2+3,
                                             fill=SURFACE, outline=BORDER)
            self.tag_lower(backdrop, label)

    def _img(self, k):
        """取 Tk 可用的瓦片图。缺 PhotoImage 时在主线程补建（Tk 不允许子线程创建）。"""
        ph = self._cache.get(k)
        if ph is not None:
            return ph
        src = self._pil_cache.get(k)
        if src is None:
            return None
        try:
            ph = ImageTk.PhotoImage(src)
        except Exception:
            return None
        self._cache[k] = ph
        return ph

    def _placeholder_tile(self, z, x, y, missing):
        """取父层级瓦片并放大 2 倍，作为当前层级的临时底图。"""
        if z <= 10:
            return None
        pz = z - 1
        px, py = x // 2, y // 2
        pk = (pz, px, py)
        parent = self._pil_cache.get(pk)
        if parent is None:
            missing.add(pk)
            return None
        # 按象限裁剪再放大：x/y 的奇偶决定取父图的哪个 128x128 角
        half = TILE_SIZE // 2
        ox = (x % 2) * half
        oy = (y % 2) * half
        key = (pz, px, py, x % 2, y % 2)
        cached = self._placeholder.get(key)
        if cached is not None:
            return cached
        try:
            crop = parent.crop((ox, oy, ox + half, oy + half)).resize(
                (TILE_SIZE, TILE_SIZE), _BILINEAR)
            ph = ImageTk.PhotoImage(crop)
            self._placeholder[key] = ph
            return ph
        except Exception:
            return None

    def _markers(self):
        if self.route_start is not None:
            start = self.latlon_to_screen(*self.route_start)
            pts = [start] + [self.latlon_to_screen(*point) for point in self.route_points]
            if len(pts) >= 2:
                self.create_line([c for p in pts for c in p], fill="#3a7afe", width=3,
                                 capstyle="round", joinstyle="round")
            if len(pts) >= 3:
                self.create_line(*pts[-1], *start, fill="#3a7afe", width=2, dash=(4, 4))
            x, y = start
            self.create_oval(x-7, y-7, x+7, y+7, fill="#e04040", outline="white", width=2)
            self.create_text(x, y-17, text="起点", fill="#c02020",
                             font=("Microsoft YaHei UI", 9, "bold"))
        if self.robot:
            x, y = self.latlon_to_screen(*self.robot)
            self.create_oval(x-8, y-8, x+8, y+8, fill="#22cc66", outline="white", width=2)
            self.create_text(x, y+18, text="你", fill="#108040",
                             font=("Microsoft YaHei UI", 9, "bold"))
        self.draw_preview_point()

    def draw_preview_point(self):
        self.delete("preview_point")
        if self.preview_point is not None:
            x, y = self.latlon_to_screen(*self.preview_point)
            self.create_oval(x-6, y-6, x+6, y+6, fill="#723fc1", outline="white", width=2,
                             tags="preview_point")


# ============ 地名搜索（OSM Nominatim） ============
def search_place(query, limit=6):
    """地理编码：返回 [(显示名, lat_wgs, lon_wgs), ...]。

    用 OSM Nominatim —— 它返回的就是 WGS-84，跟定位层同一坐标系，不用转换。
    （高德的地理编码接口需要 user key，无 key 时返回 INVALID_USER_KEY）
    """
    import urllib.parse
    url = ("https://nominatim.openstreetmap.org/search?"
           + urllib.parse.urlencode({"q": query, "format": "json",
                                     "limit": limit, "accept-language": "zh-CN"}))
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "lepao-console/1.0 (campus run tool)"})
        with _opener.open(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        out = []
        for it in data:
            name = it.get("display_name", "")
            try:
                la = float(it["lat"]); lo = float(it["lon"])
            except Exception:
                continue
            out.append((name, la, lo))
        return out
    except Exception as ex:
        raise RuntimeError(f"搜索失败：{_explain(ex)}") from ex


# ============ Tab ① 摇杆 ============
class JoystickTab(ttk.Frame):
    def __init__(self, master, runner, navigation):
        super().__init__(master, padding=8)
        self.r = runner
        self.navigation = navigation
        self.direction_buttons = []
        self.keys = set()
        self.dirs = []
        self.dragging = False
        self.stick_offset = (0.0, 0.0)   # 相对摇杆半径，重绘时保留正在按住的方向
        self.active = False
        self._build()
        navigation.status.trace_add("write", self._sync_controls)
        navigation.stop_manual = self.stop
        self._sync_controls()

    def _build(self):
        info = _card(self)
        info.pack(fill="x")
        metrics = ttk.Frame(info, style="Surface.TFrame")
        metrics.pack(fill="x")
        metrics.columnconfigure(tuple(range(4)), weight=1, uniform="metric")
        self.metrics = {}
        for column, (key, text) in enumerate((("distance", "里程"), ("time", "用时"),
                                               ("pace", "乐跑平均配速"), ("count", "已打卡"))):
            item = ttk.Frame(metrics, style="Surface.TFrame")
            item.grid(row=0, column=column, sticky="ew", padx=(0, 12))
            ttk.Label(item, text=text, style="Muted.TLabel").pack(anchor="w")
            value = ttk.Label(item, text="—", style="Metric.TLabel")
            value.pack(anchor="w")
            self.metrics[key] = value
        self.lb_cp = ttk.Label(info, text="等待进入跑步页…", style="Muted.TLabel")
        self.lb_cp.pack(anchor="w", pady=(8, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(8, 0))

        left = _card(body, "方向摇杆")
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, justify="center", style="Muted.TLabel",
                  text="拖动摇杆或按 WASD / 方向键 · 空格急停").pack(side="bottom", pady=(4, 0))
        self.cv = tk.Canvas(left, width=300, height=255, bg=SURFACE,
                            highlightthickness=0, cursor="hand2")
        self.cv.pack(fill="both", expand=True)
        self.cv.bind("<Configure>", self._draw)
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        pad = _card(right, "快捷方向", padding=10)
        pad.pack(fill="x")
        g = ttk.Frame(pad, style="Surface.TFrame")
        g.pack(fill="x")
        g.columnconfigure((0, 1, 2), weight=1, uniform="direction")
        layout = [(0, 0, "↖ 西北", 315), (0, 1, "↑ 正北", 0), (0, 2, "↗ 东北", 45),
                  (1, 0, "← 正西", 270), (1, 2, "→ 正东", 90),
                  (2, 0, "↙ 西南", 225), (2, 1, "↓ 正南", 180), (2, 2, "↘ 东南", 135)]
        for r_, c_, txt, b in layout:
            btn = ttk.Button(g, text=txt, width=8, takefocus=False)
            self.direction_buttons.append(btn)
            btn.grid(row=r_, column=c_, padx=3, pady=2, sticky="ew")
            btn.bind("<ButtonPress-1>", lambda e, bb=b: self._press_dir(bb))
            btn.bind("<ButtonRelease-1>", lambda e, bb=b: self._release_dir(bb))

        sp = _card(right, "基准速度", padding=10)
        sp.pack(fill="x", pady=(10, 0))
        self.spd = tk.DoubleVar(value=2.8)
        row = ttk.Frame(sp, style="Surface.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="1.0", style="Muted.TLabel").pack(side="left")
        self.speed_scale = ttk.Scale(row, from_=1.0, to=6.0, variable=self.spd, orient="horizontal",
                                     command=self._speed)
        self.speed_scale.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(row, text="6.0", style="Muted.TLabel").pack(side="left")
        self.vary_speed = tk.BooleanVar(value=True)
        self.speed_toggle = ttk.Checkbutton(sp, text=VARY_SPEED_LABEL, variable=self.vary_speed,
                                            command=self._speed)
        self.speed_toggle.pack(anchor="w", pady=(4, 0))
        self.lb_pace = ttk.Label(sp, style="Accent.TLabel")
        self.lb_pace.pack(anchor="w", pady=(3, 0))
        self.show_speed()

        act = ttk.Frame(right); act.pack(fill="x", pady=(8, 0))
        ttk.Button(act, text="立即停下", style="Stop.TButton", command=self.stop_all).pack(side="left")
        self.reset_btn = ttk.Button(act, text="复位到起点", command=self.reset)
        self.reset_btn.bind("<KeyPress-space>", self._stop_from_reset)
        self.reset_btn.pack(side="left", padx=(6, 0))
        ttk.Label(right, textvariable=self.navigation.status, wraplength=300).pack(anchor="w", pady=4)

    def _sync_controls(self, *_):
        busy = self.navigation.running
        for control in [self.speed_scale, self.speed_toggle, *self.direction_buttons]:
            control.state(["disabled"] if busy else ["!disabled"])
        self.reset_btn.state(["disabled"] if busy or self.r.home is None else ["!disabled"])
        self.cv.configure(cursor="arrow" if busy else "hand2")

    def _joystick_geometry(self):
        cx = self.cv.winfo_width() / 2
        cy = self.cv.winfo_height() / 2
        return cx, cy, max(1, min(120, cx - 28, cy - 28))

    def _draw(self, _event=None):
        c = self.cv
        cx, cy, R = self._joystick_geometry()
        dx, dy = self.stick_offset
        knob_x, knob_y = cx + dx * R, cy + dy * R
        c.delete("all")
        c.create_oval(cx-R, cy-R, cx+R, cy+R, outline=BORDER, width=2, fill="#f3f8f7")
        for ring in (R * 0.6, R * 0.32):
            c.create_oval(cx-ring, cy-ring, cx+ring, cy+ring, outline="#dcebe6", width=1)
        c.create_line(cx-R+30, cy, cx+R-30, cy, fill="#d8e8e3")
        c.create_line(cx, cy-R+30, cx, cy+R-30, fill="#d8e8e3")
        for ang, nm in ((0, "北"), (90, "东"), (180, "南"), (270, "西")):
            a = math.radians(ang)
            c.create_text(cx+(R-18)*math.sin(a), cy-(R-18)*math.cos(a),
                          text=nm, fill=MUTED, font=(UI_FONT, 9))
        c.create_oval(knob_x-26, knob_y-26, knob_x+26, knob_y+26,
                      fill=ACCENT, outline=ACCENT_DARK, width=2)
        c.create_oval(knob_x-10, knob_y-10, knob_x+10, knob_y+10,
                      fill=ACCENT, outline="#9cddce", width=1)

    def _press(self, e):
        if self.navigation.running:
            return
        self.cv.focus_set()
        self.dragging = True
        self._fromxy(e.x, e.y)

    def _drag(self, e):
        if self.dragging:
            self._fromxy(e.x, e.y)

    def _release(self, e):
        self.dragging = False
        self.stick_offset = (0.0, 0.0)
        self._sync()
        self._draw()

    def _fromxy(self, x, y):
        cx, cy, radius = self._joystick_geometry()
        dx, dy = x-cx, y-cy
        r = math.hypot(dx, dy)
        if r < 12:
            self.stick_offset = (0.0, 0.0)
        else:
            scale = max(radius, r)
            self.stick_offset = (dx / scale, dy / scale)
        self._sync()
        self._draw()

    def _go(self, b):
        if self.navigation.running:
            return
        if not self.navigation.origin_ready:
            self.navigation.return_home(self.spd.get(), on_arrival=self._sync)
            return
        self.active = True
        self.r.set_speed(self.spd.get(), self.vary_speed.get())
        self.r.go(b)
        self.r.feeder.resume()

    def _press_dir(self, b):
        if self.navigation.running:
            return
        self.dirs.append(b)
        self._sync()

    def _release_dir(self, b):
        if b in self.dirs:
            self.dirs.remove(b)
        self._sync()

    KM = {"w": 0, "up": 0, "s": 180, "down": 180, "a": 270, "left": 270,
          "d": 90, "right": 90, "q": 315, "e": 45, "z": 225, "c": 135}

    def key_down(self, k):
        if k == "space":
            self.stop_all(); return
        if k in self.KM and not self.navigation.running:
            self.keys.add(k); self._sync()

    def key_up(self, k):
        if k in self.keys:
            self.keys.remove(k)
            self._sync()

    def _sync(self):
        if self.dirs:
            self._go(self.dirs[-1])
            return
        dx, dy = self.stick_offset
        if dx or dy:
            self._go(math.degrees(math.atan2(dx, -dy)) % 360.0)
            return
        dn = de = 0.0
        for k in self.keys:
            b = math.radians(self.KM[k]); dn += math.cos(b); de += math.sin(b)
        if abs(dn) + abs(de) < 1e-6:
            self._halt()
        else:
            self._go(math.degrees(math.atan2(de, dn)) % 360.0)

    def _speed(self, _=None):
        v = _selected_speed(self.spd)
        self.r.set_speed(v, self.vary_speed.get())
        self.r.feeder.set_cadence(cadence_for_speed(v*3.6))
        self.show_speed()

    def show_speed(self):
        base = self.spd.get()
        speed = self.r.manual_speed
        if speed is not None and self.vary_speed.get():
            text = f"基准 {base:.1f} · 当前 {speed:.2f} m/s · {_pace_text(1000 / speed)}/km"
        else:
            text = f"基准 {base:.1f} m/s · {_pace_text(1000 / base)}/km"
        self.lb_pace.config(text=text)

    def _halt(self):
        if not self.active:
            return
        self.active = False
        self.r.halt()
        self.r.feeder.pause()
        self.show_speed()

    def stop(self):
        self.dirs.clear()
        self.keys.clear()
        self.dragging = False
        self.stick_offset = (0.0, 0.0)
        self._halt()
        self._draw()

    def reset(self):
        self.navigation.return_home(self.spd.get())

    def stop_all(self):
        self.stop()
        self.navigation.stop()

    def _stop_from_reset(self, _event):
        self.stop_all()
        return "break"

    def update_state(self, st):
        if st is None:
            for label in self.metrics.values():
                label.config(text="—")
            self.lb_cp.config(text="等待进入跑步页…")
            return
        p = st.avg_pace_s
        ps = _pace_text(p) if p else "--"
        self.metrics["distance"].config(text=f"{st.distance_m/1000:.2f} km")
        self.metrics["time"].config(text=f"{st.elapsed_s//60:02d}:{st.elapsed_s%60:02d}")
        self.metrics["pace"].config(text=ps)
        self.metrics["count"].config(text=str(st.punched))
        if st.cp_name:
            text = st.cp_name
            if st.cp_dist_m is not None:
                text += f"   ·   距 {st.cp_dist_m:g} 米"
        else:
            text = "待分配"
        self.lb_cp.config(text=f"打卡点   {text}")


# ============ Tab ② 路线 ============
class RouteTab(ttk.Frame):
    def __init__(self, master, runner, post):
        super().__init__(master, padding=8)
        self.r = runner
        self.post = post
        self.points_gcj = []
        self.track = None
        self.laps = tk.StringVar(value="1")
        self.preview_timer = None
        self.status = tk.StringVar(value="")
        self.origin_ready = False
        self.planning = False
        self.stop_manual = lambda: None
        self.running = False
        self.cancel = threading.Event()
        self.closing = False
        self._search_id = 0
        self._build()
        self._auto_load()

    @property
    def start_wgs(self):
        return self.r.home

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.columnconfigure(1, minsize=270)
        self.rowconfigure(0, weight=1)
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")

        search = _card(left, padding=8)
        search.pack(fill="x", pady=(0, 8))
        search.columnconfigure(1, weight=1)
        ttk.Label(search, text="搜索地点", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.q = tk.StringVar()
        place = ttk.Entry(search, textvariable=self.q, width=1)
        place.grid(row=0, column=1, sticky="ew", padx=8)
        place.bind("<Return>", lambda e: self._search())
        ttk.Button(search, text="搜索", command=self._search).grid(row=0, column=2, sticky="ew")
        ttk.Label(search, text="搜索结果", style="Card.TLabel").grid(row=1, column=0, sticky="w")
        self.res_box = ttk.Combobox(search, state="readonly", height=6, width=1)
        self.res_box.set("搜索后在此选择地点")
        self.res_box.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=5)
        self.res_box.bind("<<ComboboxSelected>>", self._on_result)
        self._results = []

        ttk.Label(search, text="按坐标跳转", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        coords = ttk.Frame(search, style="Surface.TFrame")
        coords.grid(row=2, column=1, sticky="ew", padx=8)
        self.q_lat = tk.StringVar()
        self.q_lon = tk.StringVar()
        coords.columnconfigure((1, 3), weight=1, uniform="coordinate")
        for i, (text, variable) in enumerate((("纬度", self.q_lat), ("经度", self.q_lon))):
            ttk.Label(coords, text=text, style="Muted.TLabel").grid(
                row=0, column=2 * i, sticky="w", padx=(0, 4))
            entry = ttk.Entry(coords, textvariable=variable, width=10)
            entry.grid(row=0, column=2 * i + 1, sticky="ew", padx=(0, 8) if i == 0 else 0)
            entry.bind("<Return>", lambda e: self._jump_coord())
        coord_actions = ttk.Frame(search, style="Surface.TFrame")
        coord_actions.grid(row=2, column=2)
        ttk.Button(coord_actions, text="跳转", command=self._jump_coord).pack(side="left")
        self.coord_home_btn = ttk.Button(coord_actions, text="设为起点", command=self._home_from_fields)
        self.coord_home_btn.pack(side="left", padx=(4, 0))

        map_card = _card(left, padding=0)
        map_card.pack(fill="both", expand=True)
        toolbar = ttk.Frame(map_card, style="Surface.TFrame", padding=(8, 5))
        toolbar.pack(fill="x")
        self.mode_v = tk.StringVar(value="brush")
        self.edit_controls = [self.coord_home_btn]
        for label, mode in (("画笔", "brush"), ("设置起点", "start")):
            button = ttk.Radiobutton(toolbar, text=label, value=mode, variable=self.mode_v,
                                     style="Tool.TRadiobutton", command=self._mode)
            button.pack(side="left", padx=(0, 4))
            self.edit_controls.append(button)
        for label, step in (("放大 ＋", 1), ("缩小 －", -1)):
            ttk.Button(toolbar, text=label, command=lambda step=step: self.map.zoom(step)
                       ).pack(side="right", padx=(4, 0))
        self.map = MapView(map_card, on_click=self._select_start, on_stroke=self._finish_stroke,
                           center=self.start_wgs, width=1, height=1)
        self.map.pack(fill="both", expand=True)
        self.mode_hint = ttk.Label(map_card, style="Muted.TLabel", justify="left", padding=(8, 6))
        self.mode_hint.pack(fill="x")
        self.mode_hint.bind("<Configure>",
                            lambda e: self.mode_hint.configure(wraplength=max(1, e.width - 16)))
        self.routing_credit = ttk.Label(map_card, text="步行路网 © OpenStreetMap 贡献者 · 反馈地图问题",
                                        style="Muted.TLabel", cursor="hand2", padding=(8, 2))
        self.routing_credit.bind("<Button-1>", lambda _: webbrowser.open("https://www.openstreetmap.org/fixthemap"))
        self._mode()

        overview = _card(right, padding=10)
        overview.pack(fill="x")
        distance = ttk.Frame(overview, style="Surface.TFrame")
        distance.pack(fill="x", pady=(0, 4))
        ttk.Label(distance, text="计划总距离\n含返回起点", style="Muted.TLabel").pack(side="left")
        self.lb_l = ttk.Label(distance, text="—", style="Distance.TLabel")
        self.lb_l.pack(side="right")
        self.lb_s = ttk.Label(overview, text="起点：未选", style="Muted.TLabel")
        self.lb_s.pack(anchor="w")
        repeat = ttk.Frame(overview, style="Surface.TFrame")
        repeat.pack(fill="x", pady=(2, 0))
        ttk.Label(repeat, text="重复圈数", style="Card.TLabel").pack(side="left")
        self.lap_input = ttk.Spinbox(repeat, from_=1, to=MAX_ROUTE_LAPS, textvariable=self.laps, width=4)
        self.lap_input.pack(side="left", padx=5)
        self.preview_btn = ttk.Button(repeat, text="预览", command=self.preview)
        self.preview_btn.pack(side="right")

        sp = _card(right, padding=10)
        sp.pack(fill="x", pady=(8, 0))
        pace = ttk.Frame(sp, style="Surface.TFrame")
        pace.pack(fill="x", pady=(0, 4))
        ttk.Label(pace, text="基准速度", style="Muted.TLabel").pack(side="left")
        self.lb_p = ttk.Label(pace, text="2.8 m/s · 5'57\"/km", style="Accent.TLabel")
        self.lb_p.pack(side="right")
        self.spd = tk.DoubleVar(value=2.8)
        row = ttk.Frame(sp, style="Surface.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="1.0", style="Muted.TLabel").pack(side="left")
        self.speed_scale = ttk.Scale(row, from_=1.0, to=6.0, variable=self.spd, orient="horizontal",
                                     command=self._speed)
        self.speed_scale.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(row, text="6.0", style="Muted.TLabel").pack(side="left")

        self.vary_speed = tk.BooleanVar(value=True)
        self.speed_toggle = ttk.Checkbutton(sp, text=VARY_SPEED_LABEL,
                                            variable=self.vary_speed)
        self.speed_toggle.pack(anchor="w", pady=(4, 0))

        act = _card(right, padding=10)
        act.pack(fill="x", pady=(8, 0))
        act.columnconfigure((0, 1), weight=1, uniform="action")
        self.run_btn = ttk.Button(act, text="沿路线跑", style="Primary.TButton", command=self.run)
        self.stop_btn = ttk.Button(act, text="停止", style="Stop.TButton", command=self.stop)
        self.run_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        for i, (text, command) in enumerate((("保存路线", self.save), ("加载路线", self.load),
                                             ("清空路线", self.clear), ("查看起点", self.goto))):
            column = i % 2
            button = ttk.Button(act, text=text, command=command)
            button.grid(row=1 + i // 2, column=column, sticky="ew", pady=(4, 0),
                        padx=(4, 0) if column else (0, 4))
            if text in ("加载路线", "清空路线"):
                self.edit_controls.append(button)
        self.home_btn = ttk.Button(act, text="回到起点", command=lambda: self.return_home(self.spd.get()))
        self.home_btn.bind("<KeyPress-space>", self._stop_from_home)
        self.home_btn.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._sync_route_controls()
        self.laps.trace_add("write", self._laps_changed)

        activity = _card(right, "路线日志", padding=8)
        activity.pack(fill="both", expand=True, pady=(8, 0))
        self.log = LogView(activity, height=3)
        if self.start_wgs is not None:
            self._refresh_route()

    def _mode(self):
        mode = self.mode_v.get()
        self.map.set_tool(mode)
        hints = {"brush": "左键拖动画线，松开定稿。右键平移，滚轮缩放，Esc 取消。",
                 "start": "点击地图设置摇杆和路线的共同起点，或输入 WGS-84 经纬度后点「设为起点」。"}
        self.mode_hint.config(text=hints[mode])

    def _sync_route_controls(self):
        ready = self.track is not None and not self.running
        distance = "—"
        try:
            laps = _route_laps(self.laps.get())
            if self.track is not None:
                total = self.track.length * laps
                distance = f"{total:.0f} m" if total < 1000 else f"{total/1000:.2f} km"
        except ValueError:
            distance = "圈数无效"
            ready = False
        self.lb_l.config(text=distance)
        self.run_btn.config(state="normal" if ready and len(self.points_gcj) >= 2
                            and self.preview_timer is None else "disabled")
        self.stop_btn.config(state="normal" if self.running and not self.cancel.is_set() else "disabled")
        self.preview_btn.config(state="normal" if ready else "disabled")
        self.home_btn.state(["disabled"] if self.running or self.start_wgs is None else ["!disabled"])
        for control in (self.lap_input, self.speed_scale, self.speed_toggle, *self.edit_controls):
            control.state(["disabled"] if self.running else ["!disabled"])

    # ---- 搜索 ----
    def _search(self):
        text = self.q.get().strip()
        if not text:
            return
        self._log(f"搜索「{text}」…")
        self._results = []
        self.res_box["values"] = ()
        self.res_box.set("搜索中…")
        self._search_id += 1
        request = self._search_id

        def work():
            error = ""
            try:
                res = search_place(text)
            except RuntimeError as exc:
                res = []
                error = str(exc)

            def put():
                if request != self._search_id:
                    return
                self._results = res
                names = [r[0][:80] for r in res]
                self.res_box["values"] = names
                if names:
                    self.res_box.set(f"找到 {len(names)} 个结果，选一个跳转")
                    self._log(f"找到 {len(names)} 个结果")
                elif error:
                    self.res_box.set("搜索失败，请重试")
                    self._log(error)
                else:
                    self.res_box.set("无结果")
                    self._log("没有找到匹配地点")
            self.post(put)
        threading.Thread(target=work, daemon=True).start()

    def _on_result(self, _e=None):
        idx = self.res_box.current()
        if idx < 0 or idx >= len(self._results):
            return
        name, la, lo = self._results[idx]
        self.map.jump_to(la, lo, z=17)
        self._log(f"跳转到 {name[:50]}  ({la:.6f}, {lo:.6f})")
        # 搜索结果同时作为候选起点，方便直接用
        self.q_lat.set(f"{la:.6f}"); self.q_lon.set(f"{lo:.6f}")

    def _jump_coord(self):
        try:
            la = float(self.q_lat.get()); lo = float(self.q_lon.get())
        except Exception:
            messagebox.showinfo("提示", "经纬度格式不对，例如纬度 39.904200，经度 116.407400")
            return
        try:
            la, lo = _coordinates(la, lo)
        except ValueError:
            messagebox.showinfo("提示", "经纬度超出范围")
            return
        self.map.jump_to(la, lo, z=17)
        self._log(f"跳转到坐标 ({la:.6f}, {lo:.6f})")

    def _home_from_fields(self):
        try:
            home = _coordinates(self.q_lat.get(), self.q_lon.get())
        except ValueError as exc:
            messagebox.showinfo("设置起点", str(exc))
            return
        if self.set_home(home):
            self.goto()

    def set_home(self, home):
        if self.running or self.closing:
            return False
        try:
            self.r.save_cfg({"lat": home[0], "lon": home[1], "home_set": True})
        except (OSError, ValueError) as exc:
            messagebox.showerror("设置起点失败", str(exc))
            return False
        self.origin_ready = False
        self.mode_v.set("brush")
        self._mode()
        self._refresh_route()
        self.status.set(f"共同起点已保存 · {home[0]:.6f}, {home[1]:.6f} · 首次出发时同步定位")
        return True

    def _select_start(self, glat, glon):
        self.set_home(gcj02_to_wgs84(glon, glat)[::-1])

    def _finish_stroke(self, points):
        if self.closing or self.running:
            return
        if self.start_wgs is None:
            glat, glon = points[0]
            if not self.set_home(gcj02_to_wgs84(glon, glat)[::-1]):
                return
        self.points_gcj = points
        self._refresh_route()

    def _refresh_route(self):
        self._stop_preview()
        if self.start_wgs is None:
            self.track = None
            self.map.route_start = None
            self.map.route_points = []
            self.lb_s.config(text="共同起点：未设置")
            self._laps_changed()
            self.map.redraw()
            return
        lat, lon = self.start_wgs
        glon, glat = wgs84_to_gcj02(lon, lat)
        self.map.route_start = (glat, glon)
        self.map.route_points = self.points_gcj
        self.lb_s.config(text=f"共同起点：{lat:.6f}, {lon:.6f}")
        self.track = None
        if self.points_gcj:
            coords = [self.start_wgs] + [gcj02_to_wgs84(lo, la)[::-1] for la, lo in self.points_gcj]
            if any(haversine(*self.start_wgs, *point) > 0.001 for point in coords[1:]):
                self.track = Track(coords, closed=True, corner_radius_m=0)
        self._laps_changed()
        self.map.redraw()

    def _laps_changed(self, *_):
        self._stop_preview()
        self._sync_route_controls()

    def preview(self):
        if self.preview_timer is not None:
            self._stop_preview()
            self._sync_route_controls()
            return
        if self.track is None or self.running:
            return
        laps = _route_laps(self.laps.get())
        started = time.monotonic()
        self.preview_btn.config(text="停止预览")
        self._log(f"地图预览：{laps} 圈，单圈 {self.track.length:.0f} m")

        def frame():
            progress = (time.monotonic() - started) / 5.0  # five seconds per preview lap
            if progress >= laps:
                self._stop_preview()
                self._sync_route_controls()
                self._log(f"地图预览完成：{laps} 圈")
                return
            fraction = progress % 1
            lat, lon = self.track.position_at(self.track.length * fraction)
            self.map.preview_point = wgs84_to_gcj02(lon, lat)[::-1]
            self.map.draw_preview_point()
            self.mode_hint.config(text=f"地图预览 · 第 {int(progress)+1}/{laps} 圈")
            self.preview_timer = self.after(40, frame)

        frame()
        self._sync_route_controls()

    def _stop_preview(self):
        if self.preview_timer is None:
            return
        self.after_cancel(self.preview_timer)
        self.preview_timer = None
        self.map.preview_point = None
        self.map.draw_preview_point()
        self.preview_btn.config(text="预览")
        self._mode()

    def _speed(self, _=None):
        v = _selected_speed(self.spd)
        self.r.set_speed(v)
        self.r.feeder.set_cadence(cadence_for_speed(v*3.6))
        self.lb_p.config(text=f"{v:.1f} m/s · {_pace_text(1000 / v)}/km")

    def clear(self):
        if self.running:
            return
        self.map.set_tool(self.mode_v.get())
        self.points_gcj = []
        self.laps.set("1")
        self._refresh_route()

    def goto(self):
        if self.start_wgs is not None:
            self.map.jump_to(*self.start_wgs, z=self.map.z)

    def save(self):
        if self.start_wgs is None:
            messagebox.showinfo("保存路线", "请先设置共同起点")
            return
        try:
            laps = _route_laps(self.laps.get())
        except ValueError as exc:
            messagebox.showinfo("圈数", str(exc))
            return
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="route.json",
                                         initialdir=HERE, filetypes=[("JSON", "*.json")])
        if not p: return
        try:
            _save_json(p, {"start_wgs": self.start_wgs, "points_gcj": self.points_gcj, "laps": laps})
        except (OSError, ValueError) as exc:
            self._log(f"保存失败：{exc}")
            return
        self._log(f"已保存 {os.path.basename(p)}")

    def load(self):
        p = filedialog.askopenfilename(initialdir=HERE, filetypes=[("JSON", "*.json")])
        if p: self._load_from(p)

    def _load_from(self, p):
        if self.running:
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or "start_wgs" not in d:
                raise ValueError("路线缺少起点")
            start = d["start_wgs"]
            points = d.get("points_gcj", [])
            if not isinstance(start, list) or not isinstance(points, list) or any(
                    not isinstance(point, list) or len(point) != 2 for point in [start, *points]):
                raise ValueError("起点和路线点必须包含两个坐标")
            start_wgs = _coordinates(*start)
            points_gcj = [_coordinates(*point) for point in points]
            laps = _route_laps(d.get("laps", 1))
        except (OSError, ValueError) as ex:
            self._log(f"加载失败：{ex}")
            return
        if self.start_wgs is None and not self.set_home(start_wgs):
            return
        self.points_gcj = points_gcj
        self.laps.set(str(laps))
        self.mode_v.set("brush")
        self._mode()
        self._refresh_route()
        self.goto()
        self._log(f"已加载 {os.path.basename(p)}：{len(self.points_gcj)} 点")

    def _auto_load(self):
        if os.path.exists(ROUTE_PATH):
            self._load_from(ROUTE_PATH)

    def run(self):
        if self.running or self.closing or self.preview_timer is not None:
            return
        if self.track is None or len(self.points_gcj) < 2:
            messagebox.showinfo("提示", "请先用画笔画出路线")
            return
        try:
            laps = _route_laps(self.laps.get())
        except ValueError as exc:
            messagebox.showinfo("圈数", str(exc))
            return
        self._start_motion(self.spd.get(), laps, self.vary_speed.get())

    def return_home(self, speed, on_arrival=None):
        self._start_motion(speed, 0, False, on_arrival)

    def _start_motion(self, speed, laps, vary_speed, on_arrival=None):
        if self.running or self.closing:
            return
        home = self.start_wgs
        if home is None:
            self.status.set("请先点击窗口顶部的「设置起点」")
            return
        if on_arrival is None:
            self.stop_manual()
        self.r.halt()
        self._stop_preview()
        course = self.track
        cancel = self.cancel = threading.Event()
        self.running = True
        self.planning = True
        self._sync_route_controls()
        initialize = not self.origin_ready and (laps > 0 or on_arrival is not None)
        self._show_progress("正在同步模拟器定位到共同起点…" if initialize else "正在规划到共同起点的路线…")

        def waiting_for_gps():
            if self.cancel is cancel and not cancel.is_set() and not self.closing:
                self._show_progress("已发送起点，等待 GPS 更新。请在模拟器内打开显示当前位置的地图页，或点「停止」。")

        def plan():
            try:
                if initialize:
                    if not self.r.sync_start(home, cancel, lambda: self.post(waiting_for_gps)):
                        return
                    approach, source = None, "共同起点"
                else:
                    current, trail = self.r.navigation_snapshot(cancel)
                    if cancel.is_set():
                        return
                    approach, source = plan_return(current, home, course, trail, cancel)
            except Exception as exc:
                action = "同步起点失败" if initialize else ("前往起点失败" if laps else "返程规划失败")
                msg = f"{action} · {str(exc) if isinstance(exc, ValueError) else _explain(exc)}"
                self.post(lambda: self._run_finished(cancel, msg))
                return
            def begin():
                if cancel.is_set() or self.closing:
                    return
                self.planning = False
                self.origin_ready = True
                if initialize:
                    self.map.robot = wgs84_to_gcj02(home[1], home[0])[::-1]
                    self.map.redraw()
                    self._log("模拟器 GPS 已确认在共同起点")
                if approach is None and not laps:
                    self._run_finished(cancel, "已在共同起点")
                    if on_arrival:
                        on_arrival()
                    return
                if approach is not None:
                    if source == "步行路网":
                        self.routing_credit.pack(anchor="w")
                    self._log(f"返回共同起点 · {source} · {approach.length:.0f} m")
                    self._show_progress(f"正在回到起点 · 剩余 {approach.length:.0f} m")
                self.r.feeder.set_cadence(cadence_for_speed(speed * 3.6))
                self.r.feeder.resume()
                args = (cancel, approach, course, laps, speed, vary_speed, on_arrival)
                threading.Thread(target=self._worker, args=args, daemon=True).start()
            self.post(begin)
        threading.Thread(target=plan, daemon=True).start()

    def _walk(self, cancel, track, base_speed, speeds, lap, laps):
        distance = 0.0
        sent_at = time.monotonic()
        for next_distance in _route_distances(track):
            speed = next(speeds) if speeds is not None else base_speed
            due = sent_at + (next_distance - distance) / speed
            if cancel.wait(max(0.0, due - time.monotonic())):
                break
            point = track.position_at(next_distance)
            sent_at = time.monotonic()
            if not self.r.move_to(*point, cancel):
                break
            distance = next_distance
            progress = (f"沿路线跑 · 第 {lap}/{laps} 圈" if lap else
                        f"正在回到起点 · 剩余 {track.length - distance:.0f} m")
            self._robot(point, f"{progress} · {speed:.2f} m/s")
        return distance

    def _worker(self, cancel, approach, track, laps, base_speed, vary_speed, on_arrival):
        result = "已回到共同起点"
        try:
            if approach is not None:
                self._walk(cancel, approach, base_speed, None, 0, 0)
            if laps and not cancel.is_set():
                mode = "随机变速" if vary_speed else "匀速"
                self._log(f"沿路线跑：{laps} 圈，单圈 {track.length:.0f} m，{mode}，基准 {base_speed:.1f} m/s")
            speeds = _random_speeds(base_speed) if vary_speed else None
            total = 0.0
            for lap in range(1, laps + 1):
                if cancel.is_set():
                    break
                started = time.monotonic()
                distance = self._walk(cancel, track, base_speed, speeds, lap, laps)
                elapsed = time.monotonic() - started
                total += distance
                if cancel.is_set():
                    break
                if distance < track.length:
                    result = f"路线已停止，共 {total:.0f} m"
                    return
                pace = _pace_text(elapsed * 1000 / distance)
                self._log(f"已完成第 {lap}/{laps} 圈，累计 {total:.0f} m。"
                          f"本圈 {distance:.0f} m，用时 {elapsed:.1f} 秒，路线配速 {pace}/km")
            if laps:
                result = f"路线结束 · {laps} 圈，共 {total:.0f} m"
        except Exception as ex:
            result = f"跑动失败 · {_explain(ex)}"
            on_arrival = None
        finally:
            self.r.feeder.pause()
            def finish():
                self._run_finished(cancel, result)
                if on_arrival and not cancel.is_set() and not self.closing:
                    on_arrival()
            self.post(finish)

    def _run_finished(self, cancel, message):
        if not self.running or cancel is not self.cancel:
            return
        if cancel.is_set():
            message = "已停止，可从当前位置继续" if self.origin_ready else "已停止，尚未确认共同起点"
        self.planning = False
        self.running = False
        self._sync_route_controls()
        self._mode()
        self.status.set(message)
        self._log(message)

    def _show_progress(self, message):
        self.status.set(message)
        self.mode_hint.config(text=message)

    def _robot(self, point, message):
        la, lo = point
        glon, glat = wgs84_to_gcj02(lo, la)
        def put():
            self.map.robot = (glat, glon)
            self._show_progress(message)
            self.map.redraw()
        self.post(put)

    def stop(self):
        if self.running:
            self.cancel.set()
            self.r.feeder.pause()
            if self.planning:
                self._run_finished(self.cancel, "")
            else:
                self._sync_route_controls()

    def _stop_from_home(self, _event):
        self.stop()
        return "break"

    def _log(self, m):
        self.post(lambda: self.log.append(time.strftime("[%H:%M:%S] ") + m + "\n"))


# ============ Tab ③ 工具 ============
class ToolsTab(ttk.Frame):
    def __init__(self, master, runner, post):
        super().__init__(master, padding=8)
        self.r = runner
        self.post = post
        self.proc = None
        self.closing = False
        self._build()
        if self.r.config_error:
            self._log(self.r.config_error)

    def _build(self):
        cfg = _card(self, "控制台配置")
        cfg.pack(fill="x")
        self.vars = {}
        fields = ttk.Frame(cfg, style="Surface.TFrame")
        fields.pack(fill="x")
        items = (("定位下发间隔（秒）", "push_interval"), ("读屏间隔（秒）", "read_interval"))
        for label, key in items:
            variable = tk.StringVar(value=str(self.r.cfg.get(key, DEFAULTS[key])))
            self.vars[key] = variable
            _field(fields, label, variable).pack(side="left", padx=(0, 14))
        ttk.Button(cfg, text="保存配置", style="Primary.TButton", command=self._save
                   ).pack(anchor="w", pady=(12, 0))

        activity = _card(self)
        activity.pack(fill="both", expand=True, pady=(8, 0))
        actions = ttk.Frame(activity, style="Surface.TFrame")
        actions.pack(fill="x", pady=(0, 8))
        ttk.Label(actions, text="工具日志", style="Section.TLabel").pack(side="left", padx=(0, 12))
        self.diagnose_btn = ttk.Button(actions, text="环境自检", command=self._diagnose)
        self.diagnose_btn.pack(side="left")
        self.preview_btn = ttk.Button(actions, text="轨迹预演", command=self._preview)
        self.preview_btn.pack(side="left", padx=(6, 0))
        self.log = LogView(activity)
        ttk.Button(actions, text="清空日志", command=self.log.clear).pack(side="right")

    def _save(self):
        try:
            self.r.save_cfg({k: v.get() for k, v in self.vars.items()})
            self._log("配置已保存")
        except (OSError, ValueError) as ex:
            messagebox.showerror("保存配置失败", str(ex))

    def _diagnose(self):
        self._run("check_env.py", "--vm-index", str(self.r.client.vm_index))

    def _preview(self):
        km = float(self.r.cfg.get("km", 2.05))
        self._run("check_track.py", str(int(km*1000)))

    def _run(self, *args):
        if self.closing or self.proc:
            return
        try:
            command = [sys.executable, "--tool"] if FROZEN else [PYTHON, "-u"]
            proc = subprocess.Popen([*command, *args], cwd=HERE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._log(f"启动失败：{exc}")
            return
        self.proc = proc
        self.diagnose_btn.config(state="disabled")
        self.preview_btn.config(state="disabled")
        self._log(f"正在运行 {args[0]}…")

        def work():
            try:
                output, _ = proc.communicate(timeout=240)
                for line in output.splitlines():
                    if line.strip():
                        self._log(line.rstrip())
                if proc.returncode:
                    self._log(f"[退出码 {proc.returncode}]")
            except subprocess.TimeoutExpired:
                self._log("工具运行超时，正在停止")
                self._stop(proc)
            except Exception as ex:
                self._log(f"[出错] {ex}")
            finally:
                if proc.poll() is not None:
                    self.post(self._finished)
        threading.Thread(target=work, daemon=True).start()

    def _finished(self):
        self.proc = None
        self.diagnose_btn.config(state="normal")
        self.preview_btn.config(state="normal")

    def stop(self):
        self.closing = True
        if self.proc and self.proc.poll() is None:
            threading.Thread(target=self._stop, args=(self.proc,), daemon=True).start()

    def _stop(self, proc):
        try:
            _terminate_process(proc)
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(f"停止工具失败：{exc}")

    def _log(self, m):
        self.post(lambda: self.log.append(m + "\n"))


# ============ 主窗口 ============
class App:
    def __init__(self, root):
        self.root = root
        self._messages = SimpleQueue()
        self._closing = False
        root.title("步道乐跑 一体化控制台")
        _style_console(root)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1180, int(sw*0.90)); h = min(860, int(sh*0.92))
        root.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//3)}")
        root.minsize(920, 680)

        self.r = Runner()
        self.r.feeder = StepFeeder(self.r.client,
                                   cadence=cadence_for_speed(2.8*3.6),
                                   logger=lambda *_: None)
        self.r.feeder.pause()
        self.r.feeder.start()

        self.reader = None
        self._reading = False
        self._retry_delay = 0
        self._read_timer = None
        self._map_timer = None
        self._tick_timer = None
        self._reset_reader = False
        self._read_error = ""

        header = ttk.Frame(root, style="Header.TFrame", padding=(16, 8))
        header.pack(fill="x")
        logo_size = max(16, round(root.winfo_fpixels("24p")))
        with Image.open(LOGO_PATH) as logo:
            self.logo = ImageTk.PhotoImage(logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS),
                                           master=root)
        if sys.platform == "win32":
            root.iconbitmap(default=LOGO_PATH)
        else:
            root.iconphoto(True, self.logo)
        ttk.Label(header, image=self.logo, style="Title.TLabel").pack(side="left", padx=(0, 8))
        ttk.Label(header, text="步道乐跑控制台", style="Title.TLabel").pack(side="left")
        self.reconnect_btn = ttk.Button(header, text="重新连接", command=self._reconnect)
        self.reconnect_btn.pack(side="right")
        self.lb = ttk.Label(header, style="Connection.TLabel")
        self.lb.pack(side="right", padx=(8, 12))

        footer = ttk.Frame(root, padding=(16, 0, 16, 7))
        footer.pack(side="bottom", fill="x")
        self.clock = ttk.Label(footer, style="Footer.TLabel")
        self.clock.pack(side="right", anchor="n", padx=(12, 0))
        self.connection_detail = ttk.Label(footer, style="Footer.TLabel", foreground=DANGER)
        self.connection_detail.pack(side="left")
        self.connection_errors = ""
        self.error_details_btn = ttk.Button(footer, text="查看详情", style="Details.TButton",
                                            command=self._show_errors)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(7, 0))

        self.t_route = RouteTab(nb, self.r, self._messages.put)
        self.t_joy = JoystickTab(nb, self.r, self.t_route)
        self.t_tools = ToolsTab(nb, self.r, self._messages.put)

        nb.add(self.t_joy, text="摇杆")
        nb.add(self.t_route, text="路线")
        nb.add(self.t_tools, text="工具")
        self.nb = nb
        self.set_home_btn = ttk.Button(header, text="设置起点", command=self._choose_home)
        self.set_home_btn.pack(side="left", padx=12)
        self.t_route.status.trace_add("write", lambda *_: self.set_home_btn.state(
            ["disabled"] if self.t_route.running else ["!disabled"]))
        # 页签切换时强制重绘地图：MapView 创建时所在页签未显示，拿不到真实尺寸
        nb.bind("<<NotebookTabChanged>>", self._on_tab)

        root.bind("<KeyPress>", self._kd)
        root.bind("<KeyRelease>", self._ku)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self._tick()
        self._poll()
        self._drain()

    def _choose_home(self):
        if self._closing or self.t_route.running:
            return
        self.nb.select(self.t_route)
        self.t_joy.stop()
        self.t_route._stop_preview()
        self.t_route._sync_route_controls()
        self.t_route.mode_v.set("start")
        self.t_route._mode()

    def _on_tab(self, _e=None):
        """切到路线页时，等布局完成再重绘地图。"""
        if self._closing:
            return
        current = self.nb.select()
        if current != str(self.t_joy):
            self.t_joy.stop()
        if self._map_timer is not None:
            self.root.after_cancel(self._map_timer)
            self._map_timer = None
        if current == str(self.t_route):
            self._map_timer = self.root.after_idle(self._redraw_map)

    def _redraw_map(self):
        self._map_timer = None
        self.t_route.map.redraw()

    def _kd(self, e):
        if self._closing:
            return
        if e.keysym == "space" and self.t_route.running:
            self.t_route.stop()
            return "break"
        if self.nb.select() == str(self.t_joy):
            self.t_joy.key_down(e.keysym.lower())

    def _ku(self, e):
        if self.nb.select() == str(self.t_joy):
            self.t_joy.key_up(e.keysym.lower())

    def _reconnect(self):
        if self._closing:
            return
        self._reset_reader = True
        self._retry_delay = 0
        self.reader = None
        self._read_error = ""
        self._show_connection()
        if self._read_timer is not None:
            self.root.after_cancel(self._read_timer)
            self._read_timer = None
        if not self._reading:
            self._poll()

    def _tick(self):
        self._tick_timer = None
        if self._closing:
            return
        self._show_connection()
        if self.t_joy.active:
            self.t_joy.show_speed()
        in_window = 6 <= time.localtime().tm_hour < 23
        self.clock.config(text=time.strftime("%H:%M:%S") + ("  有效时段内" if in_window else "  已超时段"),
                          foreground=MUTED if in_window else DANGER)
        self._tick_timer = self.root.after(1000, self._tick)

    def _show_connection(self):
        connecting = self._reset_reader or (self.reader is None and not self._read_error)
        location_error = self.r.last_error
        if connecting:
            self.lb.config(text="正在连接 ADB…", foreground="#d9e8ee")
        elif self._read_error:
            self.lb.config(text="● ADB 读屏未连接", foreground="#ffb4ba")
        else:
            self.lb.config(text="● ADB 读屏已连接", foreground="#9be8c6")
        errors = []
        labels = []
        if self._read_error and not connecting:
            errors.append(self._read_error)
            labels.append("读屏失败")
        if location_error:
            errors.append(f"定位下发失败：{location_error}")
            labels.append("定位下发失败")
        self.connection_errors = "\n".join(errors)
        self.connection_detail.config(text=" / ".join(labels))
        if errors and not self.error_details_btn.winfo_manager():
            self.error_details_btn.pack(side="left", padx=(8, 0))
        elif not errors:
            self.error_details_btn.pack_forget()

    def _show_errors(self):
        if self.connection_errors:
            messagebox.showerror("连接详情", self.connection_errors, parent=self.root)

    def _poll(self):
        self._read_timer = None
        if self._closing or self._reading:
            return
        if self._reset_reader:
            self.reader = None
            self._reset_reader = False
        reader = self.reader
        self._reading = True

        def work():
            nonlocal reader
            error = ""
            try:
                if reader is None:
                    client = self.r.client
                    if not client.adb_path:
                        raise RuntimeError("未找到 adb")
                    reader = ScreenReader(adb_path=client.adb_path, serial=client.adb_serial(refresh=True))
                st = read_screen_state(reader, fresh_only=True)
            except Exception as exc:
                reader = None
                st = None
                error = f"读屏失败：{_explain(exc)}"
            self._messages.put(lambda: self._read_done(reader, st, error))

        threading.Thread(target=work, daemon=True).start()

    def _read_done(self, reader, state, error):
        self._reading = False
        if self._closing:
            return
        if self._reset_reader:
            self._read_timer = self.root.after_idle(self._poll)
            return
        self.reader = reader
        self._read_error = error
        self._show_connection()
        self.t_joy.update_state(state)
        self._retry_delay = min(15, max(2, self._retry_delay * 2)) if error else 0
        delay = max(1, int(1000 * max(self.r.cfg["read_interval"], self._retry_delay)))
        self._read_timer = self.root.after(delay, self._poll)

    def _drain(self):
        joy = self.t_joy
        if (joy.active or joy.keys or joy.dirs or joy.dragging) and self.root.focus_displayof() is None:
            joy.stop()
        for _ in range(128):
            try:
                callback = self._messages.get_nowait()
            except Empty:
                break
            try:
                callback()
            except Exception:
                self.root.report_callback_exception(*sys.exc_info())
        tools_proc = self.t_tools.proc
        if (self._closing and not self.t_route.running and not self.r.is_alive()
                and not self.r.feeder.is_alive()
                and (tools_proc is None or tools_proc.poll() is not None)):
            self.root.destroy()
            return
        self.root.after(25, self._drain)

    def _close(self):
        if not self._closing:
            self._closing = True
            self.lb.config(text="正在停止后台任务…", foreground="#d9e8ee")
            self.reconnect_btn.config(state="disabled")
            for timer in (self._read_timer, self._map_timer, self._tick_timer):
                if timer is not None:
                    self.root.after_cancel(timer)
            self._read_timer = self._map_timer = self._tick_timer = None
            self.t_route.closing = True
            self.t_route._stop_preview()
            self.t_joy.stop()
            self.t_route.stop()
            self.t_route.map.close()
            self.r.shutdown()
            self.r.feeder.stop()
        self.t_tools.stop()


def main():
    # pythonw.exe 下 sys.stdout 为 None，任何 print 都会抛异常导致启动即崩，
    # 所以这里改成安全的日志方式，并全程兜住启动异常。
    root = tk.Tk()
    try:
        App(root)
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        try:
            with open(os.path.join(HERE, "app_error.log"), "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        messagebox.showerror("启动失败", f"{ex}\n\n详细信息已写入 app_error.log")
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--tool"]:
        import runpy
        tools = {"check_env.py": "check_env", "check_track.py": "check_track"}
        if len(sys.argv) < 3 or sys.argv[2] not in tools:
            sys.exit("未知工具")
        sys.argv = sys.argv[2:]
        runpy.run_module(tools[sys.argv[0]], run_name="__main__")
    else:
        main()
