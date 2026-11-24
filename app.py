"""步道乐跑 一体化控制台 —— 摇杆 / 路线 / 全自动 / 工具 四合一

设计要点：
    摇杆和路线页共用一个 Runner。离开摇杆页或窗口失焦时停止手动移动。

四个页签：
    ① 摇杆      手动推方向，人眼判断最可靠
    ② 路线      地图选点 + 画路线 + 沿路线自动跑
    ③ 全自动    调 campus_run.py 全自动寻路（依赖 App 距离刷新，目前不稳定）
    ④ 工具      环境自检 / 轨迹预演 / 配置

用法：
    .venv\\Scripts\\python.exe app.py
或双击「控制台.bat」
"""
import os, sys, math, json, time, threading, subprocess, urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

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
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
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
from screen_state import ScreenReader, read_screen_state

CONFIG_PATH = os.path.join(HERE, "run_config.json")
ROUTE_PATH = os.path.join(HERE, "route.json")
TILE_DIR = os.path.join(HERE, "_tiles")
TILE_SIZE = 256
MAX_TILE_Z = 18          # 高德瓦片实测上限：z=19/20 无数据
MIN_TILE_Z = 3           # z=3 即可看全国轮廓
MAX_MAP_LAT = 85.05112878
TILE_WORKERS = 6
TILE_CACHE_SIZE = 128
TILE_RETRY_S = 15
PYTHON = (os.path.join(os.path.dirname(sys.executable), "python.exe")
          if os.path.basename(sys.executable).lower() == "pythonw.exe"
          else sys.executable)

DEFAULTS = {
    "km": 2.05, "pace": 6.0, "cps": 2,
    # 起始坐标为占位值，首次使用请在「② 路线」页搜索学校或直接点地图设起点
    "lat": 0.0, "lon": 0.0,
    "vm_index": 0, "v_max": 6.0,
    "push_interval": 1.0, "read_interval": 0.5,
}


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
        self.lat, self.lon = self.cfg["lat"], self.cfg["lon"]
        self.bearing = None          # None = 停止
        self.speed = 2.8
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

    def halt(self):
        with self.lock:
            self.bearing = None

    def set_speed(self, v):
        with self.lock:
            self.speed = max(0.5, min(6.0, v))

    def move_to(self, lat, lon):
        with self.lock:
            self.lat, self.lon = lat, lon
        self._push(lat, lon)

    def _push(self, lat, lon):
        """下发一次坐标。模拟器没开也不算致命错误，只记状态。"""
        if self._stopped.is_set():
            return
        try:
            self.client.set_wgs84(lat, lon)
            self.last_error = ""
        except Exception as e:
            self.last_error = _explain(e)

    def _loop(self):
        while not self._stopped.is_set():
            with self.lock:
                b = self.bearing
                v = self.speed
                iv = self.push_interval
            if b is None:
                self._stopped.wait(0.06)
                continue
            step = v * iv
            bb = (b + (time.time() % 7 - 3.5) * 0.6) % 360.0
            dn = math.cos(math.radians(bb)) * step
            de = math.sin(math.radians(bb)) * step
            with self.lock:
                if self.bearing != b or self._stopped.is_set():
                    continue
                self.lat, self.lon = offset_meters(self.lat, self.lon, dn, de)
                la, lo = self.lat, self.lon
            self._push(la, lo)
            self.total_m += step
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
    def __init__(self, master, on_click=None, center=None, **kw):
        super().__init__(master, bg="#e8e8e4", highlightthickness=0, **kw)
        self.z = 17
        base = center or (DEFAULTS["lat"], DEFAULTS["lon"])
        glon, glat = wgs84_to_gcj02(base[1], base[0])
        self.lat, self.lon = glat, glon
        self.on_click = on_click
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
        self.markers = []
        self.robot = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._dragm)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Configure>", lambda e: self.redraw())
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
        self._drag = (e.x, e.y, self.lat, self.lon)
        self._moved = False

    def _dragm(self, e):
        if not self._drag:
            return
        x0, y0, la0, lo0 = self._drag
        dx, dy = e.x - x0, e.y - y0
        if abs(dx) + abs(dy) > 4:
            self._moved = True
        cx, cy = lonlat_to_tile(la0, lo0, self.z)
        self.lat, self.lon = tile_to_lonlat(cx - dx / TILE_SIZE, cy - dy / TILE_SIZE, self.z)
        self.redraw()

    def _release(self, e):
        if not self._moved and self.on_click:
            self.on_click(*self.screen_to_latlon(e.x, e.y))
        self._drag = None

    def _wheel(self, e):
        # 高德瓦片实测可用范围 z=3~18；超出上限只能靠父层放大会一直糊
        nz = max(MIN_TILE_Z, min(MAX_TILE_Z, self.z + (1 if e.delta > 0 else -1)))
        if nz != self.z:
            la, lo = self.screen_to_latlon(e.x, e.y)
            self.z, self.lat, self.lon = nz, la, lo
            self.redraw()

    def jump_to(self, lat_wgs, lon_wgs, z=17):
        """跳转到指定 WGS-84 坐标。"""
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

        if missing:
            tip = f"地图加载中… 剩 {len(missing) - failed} 块" if len(missing) > failed else ""
            if failed:
                tip = f"{tip}  {failed} 块加载失败，稍后重试".strip()
            self.create_text(w - 12, h - 10, text=tip, anchor="se",
                             fill="#7a7a76",
                             font=("Microsoft YaHei UI", 9))

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
        pts = []
        for i, (lat, lon, kind, label) in enumerate(self.markers):
            x, y = self.latlon_to_screen(lat, lon)
            if kind == "start":
                self.create_oval(x-7, y-7, x+7, y+7, fill="#e04040", outline="white", width=2)
                self.create_text(x, y-17, text=label or "起点", fill="#c02020",
                                 font=("Microsoft YaHei UI", 9, "bold"))
            else:
                pts.append((x, y))
                self.create_oval(x-4, y-4, x+4, y+4, fill="#3a7afe", outline="white")
        if len(pts) >= 2:
            self.create_line([c for p in pts for c in p], fill="#3a7afe", width=3)
        if len(pts) >= 3:
            self.create_line(pts[-1][0], pts[-1][1], pts[0][0], pts[0][1],
                             fill="#3a7afe", width=2, dash=(4, 4))
        if self.robot:
            x, y = self.latlon_to_screen(*self.robot)
            self.create_oval(x-8, y-8, x+8, y+8, fill="#22cc66", outline="white", width=2)
            self.create_text(x, y+18, text="你", fill="#108040",
                             font=("Microsoft YaHei UI", 9, "bold"))


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
        return [("搜索失败: %s" % ex, 0.0, 0.0)]


# ============ Tab ① 摇杆 ============
class JoystickTab(ttk.Frame):
    def __init__(self, master, runner):
        super().__init__(master, padding=8)
        self.r = runner
        self.keys = set()
        self.dirs = []
        self.dragging = False
        self.drag_bearing = None
        self.active = False
        self._build()

    def _build(self):
        info = ttk.LabelFrame(self, text=" 实时状态 ", padding=8)
        info.pack(fill="x")
        self.lb_state = ttk.Label(info, text="等待进入跑步页…", font=("Consolas", 11))
        self.lb_state.pack(anchor="w")
        self.lb_cp = ttk.Label(info, text="打卡点：--", font=("Microsoft YaHei UI", 10))
        self.lb_cp.pack(anchor="w", pady=(3, 0))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.LabelFrame(body, text=" 方向摇杆 ", padding=8)
        left.pack(side="left", fill="y")
        self.cv = tk.Canvas(left, width=300, height=300, bg="#f5f5f3", highlightthickness=0)
        self.cv.pack()
        self.cv.bind("<Button-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self._draw(0, 0)
        ttk.Label(left, justify="center", foreground="#666",
                  text="拖动摇杆或按 W/A/S/D、方向键\n松手/松键即停，空格急停").pack(pady=(6, 0))

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        pad = ttk.LabelFrame(right, text=" 快捷方向 ", padding=8)
        pad.pack(fill="x")
        g = ttk.Frame(pad); g.pack()
        layout = [(0, 0, "↖ 西北", 315), (0, 1, "↑ 正北", 0), (0, 2, "↗ 东北", 45),
                  (1, 0, "← 正西", 270), (1, 2, "→ 正东", 90),
                  (2, 0, "↙ 西南", 225), (2, 1, "↓ 正南", 180), (2, 2, "↘ 东南", 135)]
        for r_, c_, txt, b in layout:
            btn = ttk.Button(g, text=txt, width=10)
            btn.grid(row=r_, column=c_, padx=2, pady=2)
            btn.bind("<ButtonPress-1>", lambda e, bb=b: self._press_dir(bb))
            btn.bind("<ButtonRelease-1>", lambda e, bb=b: self._release_dir(bb))

        sp = ttk.LabelFrame(right, text=" 速度 ", padding=8)
        sp.pack(fill="x", pady=(6, 0))
        self.spd = tk.DoubleVar(value=2.8)
        row = ttk.Frame(sp); row.pack(fill="x")
        ttk.Label(row, text="1.0").pack(side="left")
        ttk.Scale(row, from_=1.0, to=6.0, variable=self.spd, orient="horizontal",
                  command=self._speed).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(row, text="6.0").pack(side="left")
        self.lb_pace = ttk.Label(sp, text="约 5'57\"/km", foreground="#007acc")
        self.lb_pace.pack(anchor="w", pady=(3, 0))

        act = ttk.Frame(right); act.pack(fill="x", pady=(8, 0))
        ttk.Button(act, text="■ 立即停下", command=self.stop).pack(side="left")
        ttk.Button(act, text="复位到起点", command=self.reset).pack(side="left", padx=(6, 0))

    def _draw(self, dx, dy):
        c = self.cv; R = 120; cx = cy = 150
        c.delete("all")
        c.create_oval(cx-R, cy-R, cx+R, cy+R, outline="#bbb", width=2, fill="#fbfbf9")
        for ang, nm in ((0, "北"), (90, "东"), (180, "南"), (270, "西")):
            a = math.radians(ang)
            c.create_text(cx+(R-18)*math.sin(a), cy-(R-18)*math.cos(a),
                          text=nm, fill="#999", font=("Microsoft YaHei UI", 9))
        c.create_line(cx-R, cy, cx+R, cy, fill="#e4e4e0")
        c.create_line(cx, cy-R, cx, cy+R, fill="#e4e4e0")
        c.create_oval(cx+dx-26, cy+dy-26, cx+dx+26, cy+dy+26,
                      fill="#3a7afe", outline="#2b5fd0", width=2)

    def _press(self, e):
        self.dragging = True
        self._fromxy(e.x, e.y)

    def _drag(self, e):
        if self.dragging:
            self._fromxy(e.x, e.y)

    def _release(self, e):
        self.dragging = False
        self.drag_bearing = None
        self._sync()
        self._draw(0, 0)

    def _fromxy(self, x, y):
        dx, dy = x-150, y-150
        r = math.hypot(dx, dy)
        if r < 12:
            self.drag_bearing = None
            self._sync()
            self._draw(0, 0)
            return
        if r > 120:
            dx, dy = dx*120/r, dy*120/r
        self.drag_bearing = math.degrees(math.atan2(dx, -dy)) % 360.0
        self._sync()
        self._draw(dx, dy)

    def _go(self, b):
        self.active = True
        self.r.go(b)
        self.r.feeder.resume()

    def _press_dir(self, b):
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
            self.stop(); return
        if k in self.KM:
            self.keys.add(k); self._sync()

    def key_up(self, k):
        if k in self.keys:
            self.keys.remove(k)
            self._sync()

    def _sync(self):
        if self.dirs:
            self._go(self.dirs[-1])
            return
        if self.drag_bearing is not None:
            self._go(self.drag_bearing)
            return
        dn = de = 0.0
        for k in self.keys:
            b = math.radians(self.KM[k]); dn += math.cos(b); de += math.sin(b)
        if abs(dn) + abs(de) < 1e-6:
            self._halt()
        else:
            self._go(math.degrees(math.atan2(de, dn)) % 360.0)

    def _speed(self, _=None):
        v = self.spd.get()
        self.r.set_speed(v)
        self.r.feeder.set_cadence(cadence_for_speed(v*3.6))
        p = 1000.0/v if v > 0 else 0
        self.lb_pace.config(text="约 %d'%02d\"/km" % (p//60, p%60))

    def _halt(self):
        if not self.active:
            return
        self.active = False
        self.r.halt()
        self.r.feeder.pause()

    def stop(self):
        self.dirs.clear()
        self.keys.clear()
        self.dragging = False
        self.drag_bearing = None
        self._halt()
        self._draw(0, 0)

    def reset(self):
        lat = float(self.r.cfg.get("lat", DEFAULTS["lat"]))
        lon = float(self.r.cfg.get("lon", DEFAULTS["lon"]))
        self.r.move_to(lat, lon)
        self.r.total_m = 0.0

    def update_state(self, st):
        if st is None:
            self.lb_state.config(text="等待进入跑步页…")
            self.lb_cp.config(text="打卡点：--")
            return
        p = st.avg_pace_s
        ps = ("%d'%02d\"" % (p//60, p%60)) if p else "--"
        self.lb_state.config(text=f"{st.distance_m/1000:.2f} km    "
                                  f"{st.elapsed_s//60:02d}:{st.elapsed_s%60:02d}    "
                                  f"配速 {ps}    已打卡 {st.punched}")
        self.lb_cp.config(text=(f"打卡点：{st.cp_name}   距 {st.cp_dist_m} 米"
                                if st.cp_name else "打卡点：待分配"))


# ============ Tab ② 路线 ============
class RouteTab(ttk.Frame):
    def __init__(self, master, runner, post):
        super().__init__(master, padding=8)
        self.r = runner
        self.post = post
        self.start_wgs = (float(runner.cfg.get("lat", DEFAULTS["lat"])),
                          float(runner.cfg.get("lon", DEFAULTS["lon"])))
        self.points_gcj = []
        self.mode = "start"
        self.running = False
        self.cancel = threading.Event()
        self.closing = False
        self._search_id = 0
        self._build()
        self._auto_load()

    def _build(self):
        body = ttk.Frame(self); body.pack(fill="both", expand=True)
        left = ttk.Frame(body); left.pack(side="left", fill="both", expand=True)

        # ---- 搜索栏 ----
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="搜索地点").pack(side="left")
        self.q = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.q, width=28)
        ent.pack(side="left", padx=(6, 4))
        ent.bind("<Return>", lambda e: self._search())
        ttk.Button(bar, text="搜索", width=8, command=self._search).pack(side="left")
        ttk.Label(bar, text="  或坐标").pack(side="left", padx=(12, 4))
        self.q_lat = tk.StringVar(); self.q_lon = tk.StringVar()
        ttk.Entry(bar, textvariable=self.q_lat, width=11).pack(side="left")
        ttk.Label(bar, text=",").pack(side="left")
        ttk.Entry(bar, textvariable=self.q_lon, width=11).pack(side="left", padx=(0, 4))
        ttk.Button(bar, text="跳转", width=7, command=self._jump_coord).pack(side="left")

        # ---- 搜索结果下拉 ----
        self.res_var = tk.StringVar()
        self.res_box = ttk.Combobox(left, textvariable=self.res_var, state="readonly",
                                    height=6, font=("Microsoft YaHei UI", 9))
        self.res_box.pack(fill="x", pady=(0, 4))
        self.res_box.bind("<<ComboboxSelected>>", self._on_result)
        self._results = []

        self.map = MapView(left, on_click=self._click, center=self.start_wgs)
        self.map.pack(fill="both", expand=True)

        right = ttk.Frame(body, width=310); right.pack(side="left", fill="y", padx=(8, 0))
        right.pack_propagate(False)

        m = ttk.LabelFrame(right, text=" 点击模式 ", padding=8); m.pack(fill="x")
        self.mode_v = tk.StringVar(value="start")
        ttk.Radiobutton(m, text="① 选起点", value="start", variable=self.mode_v,
                        command=self._mode).pack(anchor="w")
        ttk.Radiobutton(m, text="② 画路线（连续点击）", value="route", variable=self.mode_v,
                        command=self._mode).pack(anchor="w")

        lb = ttk.LabelFrame(right, text=" 当前 ", padding=8); lb.pack(fill="x", pady=(6, 0))
        self.lb_s = ttk.Label(lb, text="起点：未选", font=("Consolas", 9)); self.lb_s.pack(anchor="w")
        self.lb_n = ttk.Label(lb, text="路线点：0", font=("Consolas", 9)); self.lb_n.pack(anchor="w", pady=(3, 0))
        self.lb_l = ttk.Label(lb, text="全程：--", font=("Consolas", 9)); self.lb_l.pack(anchor="w", pady=(3, 0))

        sp = ttk.LabelFrame(right, text=" 跑动速度 ", padding=8); sp.pack(fill="x", pady=(6, 0))
        self.spd = tk.DoubleVar(value=2.8)
        row = ttk.Frame(sp); row.pack(fill="x")
        ttk.Label(row, text="1.0").pack(side="left")
        ttk.Scale(row, from_=1.0, to=6.0, variable=self.spd, orient="horizontal",
                  command=self._speed).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(row, text="6.0").pack(side="left")
        self.lb_p = ttk.Label(sp, text="约 5'57\"/km", foreground="#007acc"); self.lb_p.pack(anchor="w")

        act = ttk.LabelFrame(right, text=" 操作 ", padding=8); act.pack(fill="x", pady=(6, 0))
        ttk.Button(act, text="▶ 沿路线跑", command=self.run).pack(fill="x", pady=2)
        ttk.Button(act, text="■ 停止", command=self.stop).pack(fill="x", pady=2)
        r2 = ttk.Frame(act); r2.pack(fill="x", pady=2)
        ttk.Button(r2, text="保存", command=self.save).pack(side="left", expand=True, fill="x", padx=(0, 3))
        ttk.Button(r2, text="加载", command=self.load).pack(side="left", expand=True, fill="x")
        ttk.Button(act, text="清空路线点", command=self.clear).pack(fill="x", pady=2)
        ttk.Button(act, text="回到起点", command=self.goto).pack(fill="x", pady=2)

        self.log = tk.Text(right, height=9, font=("Consolas", 9), bg="#1e1e1e",
                           fg="#d4d4d4", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

    def _mode(self):
        self.mode = self.mode_v.get()

    # ---- 搜索 ----
    def _search(self):
        text = self.q.get().strip()
        if not text:
            return
        self._log(f"搜索「{text}」…")
        self.res_box.set("搜索中…")
        self._search_id += 1
        request = self._search_id

        def work():
            res = search_place(text)
            def put():
                if request != self._search_id:
                    return
                self._results = res
                names = [r[0][:80] for r in res if r[1] or r[2]]
                self.res_box["values"] = names
                if names:
                    self.res_box.current(0)
                    self.res_var.set("")
                    self.res_box.set(f"找到 {len(names)} 个结果，选一个跳转")
                    self._log(f"找到 {len(names)} 个结果")
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
        if not la and not lo:
            return
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

    def _click(self, glat, glon):
        if self.mode == "start":
            lon, lat = gcj02_to_wgs84(glon, glat)
            self.start_wgs = (lat, lon)
            self.map.markers = [m for m in self.map.markers if m[2] != "start"]
            self.map.markers.insert(0, (glat, glon, "start", "起点"))
            self.lb_s.config(text=f"起点：{lat:.6f}, {lon:.6f}")
            self._log(f"起点 WGS-84 ({lat:.6f}, {lon:.6f})")
            self.mode_v.set("route"); self._mode()
        else:
            self.points_gcj.append((glat, glon))
            self.map.markers = [m for m in self.map.markers if m[2] == "start"]
            for i, (la, lo) in enumerate(self.points_gcj):
                self.map.markers.append((la, lo, "route", str(i+1)))
            self._len()
        self.map.redraw()

    def _len(self):
        self.lb_n.config(text=f"路线点：{len(self.points_gcj)}")
        seq = [self.start_wgs] + [gcj02_to_wgs84(lo, la)[::-1] for la, lo in self.points_gcj]
        if len(seq) >= 2:
            tot = sum(haversine(*seq[i], *seq[i+1]) for i in range(len(seq)-1))
            if len(seq) >= 3:
                tot += haversine(*seq[-1], *seq[0])
            self.lb_l.config(text=f"全程：{tot:.0f} m ({tot/1000:.2f} km)")
        else:
            self.lb_l.config(text="全程：--")

    def _speed(self, _=None):
        v = self.spd.get()
        self.r.set_speed(v)
        self.r.feeder.set_cadence(cadence_for_speed(v*3.6))
        p = 1000.0/v if v > 0 else 0
        self.lb_p.config(text="约 %d'%02d\"/km" % (p//60, p%60))

    def clear(self):
        self.points_gcj.clear()
        self.map.markers = [m for m in self.map.markers if m[2] == "start"]
        self.map.redraw(); self._len()

    def goto(self):
        glon, glat = wgs84_to_gcj02(self.start_wgs[1], self.start_wgs[0])
        self.map.lat, self.map.lon = glat, glon
        self.map.redraw()

    def save(self):
        p = filedialog.asksaveasfilename(defaultextension=".json", initialfile="route.json",
                                         initialdir=HERE, filetypes=[("JSON", "*.json")])
        if not p: return
        try:
            _save_json(p, {"start_wgs": self.start_wgs, "points_gcj": self.points_gcj})
        except (OSError, ValueError) as exc:
            self._log(f"保存失败：{exc}")
            return
        self._log(f"已保存 {os.path.basename(p)}")

    def load(self):
        p = filedialog.askopenfilename(initialdir=HERE, filetypes=[("JSON", "*.json")])
        if p: self._load_from(p)

    def _load_from(self, p):
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
        except (OSError, ValueError) as ex:
            self._log(f"加载失败：{ex}")
            return
        self.start_wgs = start_wgs
        self.points_gcj = points_gcj
        self.map.markers = []
        glon, glat = wgs84_to_gcj02(self.start_wgs[1], self.start_wgs[0])
        self.map.markers.append((glat, glon, "start", "起点"))
        for i, (la, lo) in enumerate(self.points_gcj):
            self.map.markers.append((la, lo, "route", str(i+1)))
        self.map.lat, self.map.lon = glat, glon
        self.map.redraw()
        self.lb_s.config(text=f"起点：{self.start_wgs[0]:.6f}, {self.start_wgs[1]:.6f}")
        self._len()
        self._log(f"已加载 {os.path.basename(p)}：{len(self.points_gcj)} 点")

    def _auto_load(self):
        if os.path.exists(ROUTE_PATH):
            self._load_from(ROUTE_PATH)

    def run(self):
        if self.running or self.closing: return
        if len(self.points_gcj) < 2:
            messagebox.showinfo("提示", "至少点 2 个路线点"); return
        v = self.spd.get()
        seq = [self.start_wgs]
        seq.extend(gcj02_to_wgs84(lo, la)[::-1] for la, lo in self.points_gcj)
        seq.append(self.start_wgs)
        self.running = True
        self.cancel.clear()
        self.r.feeder.resume()
        threading.Thread(target=self._worker, args=(v, seq), daemon=True).start()

    def _worker(self, v, seq):
        try:
            if self.cancel.is_set():
                return
            self._log("="*36)
            self._log(f"沿路线跑：{len(seq)-1} 段 @ {v:.1f} m/s")
            self.r.move_to(*seq[0])
            total = 0.0
            for i in range(len(seq)-1):
                if self.cancel.is_set(): break
                la0, lo0 = seq[i]; la1, lo1 = seq[i+1]
                seg = haversine(la0, lo0, la1, lo1)
                if seg < 0.5: continue
                self._log(f"  段{i+1}/{len(seq)-1} {seg:.0f}m")
                steps = max(1, int(seg/2.5))
                for k in range(1, steps+1):
                    if self.cancel.is_set(): break
                    t = k/steps
                    la = la0 + (la1-la0)*t
                    lo = lo0 + (lo1-lo0)*t
                    self.r.move_to(la, lo)
                    self._robot(la, lo)
                    total += seg/steps
                    if self.cancel.wait(seg/steps/max(v, 0.5)):
                        break
            status = "已停止" if self.cancel.is_set() else "结束"
            self._log(f"{status}，推进 {total:.0f} m")
        except Exception as ex:
            self._log(f"[出错] {ex}")
        finally:
            self.r.feeder.pause()
            self.post(lambda: setattr(self, "running", False))

    def _robot(self, la, lo):
        glon, glat = wgs84_to_gcj02(lo, la)
        def put():
            self.map.robot = (glat, glon)
            self.map.redraw()
        self.post(put)

    def stop(self):
        if self.running:
            self.cancel.set()
            self.r.feeder.pause()

    def _log(self, m):
        def put():
            self.log.insert("end", time.strftime("[%H:%M:%S] ")+m+"\n")
            self.log.see("end")
        self.post(put)


# ============ Tab ③ 全自动 ============
class AutoTab(ttk.Frame):
    def __init__(self, master, runner, post):
        super().__init__(master, padding=8)
        self.r = runner
        self.post = post
        self.proc = None
        self.stopping = False
        self.closing = False
        self._build()

    def _build(self):
        cfg = ttk.LabelFrame(self, text=" 参数 ", padding=8)
        cfg.pack(fill="x")
        self.vars = {}
        rows = [("目标里程 (km)", "km", 2.05), ("目标配速 (分/km)", "pace", 6.0),
                ("打卡点数", "cps", 2)]
        for i, (lb, k, dv) in enumerate(rows):
            ttk.Label(cfg, text=lb, width=16).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.r.cfg.get(k, dv)))
            self.vars[k] = v
            ttk.Entry(cfg, textvariable=v, width=12).grid(row=i, column=1, sticky="w")

        act = ttk.Frame(self); act.pack(fill="x", pady=(8, 0))
        self.btn = ttk.Button(act, text="▶ 全自动开跑", command=self.start)
        self.btn.pack(side="left")
        ttk.Button(act, text="■ 停止", command=self.stop).pack(side="left", padx=(6, 0))

        ttk.Label(self, foreground="#a05000",
                  text="注意：全自动依赖 App 的「距打卡点」读数刷新，实测该读数滞后严重\n"
                       "（走 20 米后仍返回旧值），因此寻路可能不准。优先用摇杆页签。"
                  ).pack(anchor="w", pady=(6, 4))

        self.log = tk.Text(self, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", wrap="word")
        self.log.pack(fill="both", expand=True)

    def start(self):
        if self.proc or self.closing: return
        cmd = [sys.executable, "-u", "campus_run.py",
               "--pace", self.vars["pace"].get(), "--km", self.vars["km"].get(),
               "--cps", self.vars["cps"].get(), "--auto-finish", "--vm-index", "0",
               "--start-lat", str(self.r.cfg.get("lat")), "--start-lon", str(self.r.cfg.get("lon"))]
        self.log.delete("1.0", "end")
        try:
            self.proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                         errors="replace",
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.log.insert("end", f"启动失败：{exc}\n")
            return
        self.btn.config(state="disabled")
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        try:
            with proc.stdout:
                for line in proc.stdout:
                    if line.strip():
                        self.post(lambda line=line: self._append(proc, line))
        finally:
            code = proc.wait()
            self.post(lambda: self._finished(proc, code))

    def _append(self, proc, line):
        if self.proc is proc:
            self.log.insert("end", line)
            self.log.see("end")

    def _finished(self, proc, code):
        if self.proc is not proc:
            return
        self._append(proc, "\n[已停止]\n" if self.stopping else f"\n[退出码 {code}]\n")
        self.proc = None
        self.stopping = False
        self.btn.config(state="normal")

    def stop(self):
        if self.proc is None or self.stopping:
            return
        self.stopping = True
        threading.Thread(target=self._terminate, args=(self.proc,), daemon=True).start()

    def _terminate(self, proc):
        try:
            _terminate_process(proc)
        except (OSError, subprocess.SubprocessError) as exc:
            self.post(lambda exc=exc: self._stop_failed(proc, exc))

    def _stop_failed(self, proc, exc):
        if self.proc is proc:
            self.stopping = False
            self._append(proc, f"停止失败：{exc}\n")


# ============ Tab ④ 工具 ============
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
        act = ttk.Frame(self); act.pack(fill="x")
        ttk.Button(act, text="环境自检", command=self._diagnose).pack(side="left")
        ttk.Button(act, text="轨迹预演", command=self._preview).pack(side="left", padx=(6, 0))
        ttk.Button(act, text="清空日志", command=lambda: self.log.delete("1.0", "end")).pack(side="left", padx=(6, 0))

        cfg = ttk.LabelFrame(self, text=" 配置 ", padding=8)
        cfg.pack(fill="x", pady=(8, 0))
        self.vars = {}
        items = [("起始纬度", "lat"), ("起始经度", "lon"),
                 ("定位下发间隔", "push_interval"), ("读屏间隔", "read_interval")]
        for i, (lb, k) in enumerate(items):
            ttk.Label(cfg, text=lb, width=14).grid(row=i//2, column=(i%2)*2, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.r.cfg.get(k, DEFAULTS.get(k, ""))))
            self.vars[k] = v
            ttk.Entry(cfg, textvariable=v, width=14).grid(row=i//2, column=(i%2)*2+1, sticky="w")
        ttk.Button(cfg, text="保存配置", command=self._save).grid(row=2, column=0, pady=(8, 0), sticky="w")

        self.log = tk.Text(self, font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))

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
            proc = subprocess.Popen([PYTHON, "-u", *args], cwd=HERE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._log(f"启动失败：{exc}")
            return
        self.proc = proc
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
                    self.post(lambda: setattr(self, "proc", None))
        threading.Thread(target=work, daemon=True).start()

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
        def put():
            self.log.insert("end", m+"\n"); self.log.see("end")
        self.post(put)


# ============ 主窗口 ============
class App:
    def __init__(self, root):
        self.root = root
        self._messages = SimpleQueue()
        self._closing = False
        root.title("步道乐跑 一体化控制台")
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
        self._read_timer = None
        self._map_timer = None
        self._tick_timer = None
        self._reset_reader = False
        self._read_error = "正在检查 ADB 连接…"

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.t_joy = JoystickTab(nb, self.r)
        self.t_route = RouteTab(nb, self.r, self._messages.put)
        self.t_auto = AutoTab(nb, self.r, self._messages.put)
        self.t_tools = ToolsTab(nb, self.r, self._messages.put)

        nb.add(self.t_joy, text="  ① 摇杆  ")
        nb.add(self.t_route, text="  ② 路线  ")
        nb.add(self.t_auto, text="  ③ 全自动  ")
        nb.add(self.t_tools, text="  ④ 工具  ")
        self.nb = nb
        # 页签切换时强制重绘地图：MapView 创建时所在页签未显示，拿不到真实尺寸
        nb.bind("<<NotebookTabChanged>>", self._on_tab)

        bar = ttk.Frame(root); bar.pack(fill="x", padx=8, pady=(0, 6))
        self.lb = ttk.Label(bar, text="就绪", font=("Microsoft YaHei UI", 9),
                            cursor="hand2")
        self.lb.pack(side="left")
        self.lb.bind("<Button-1>", lambda e: self._reconnect())
        self.clock = ttk.Label(bar, text="", font=("Consolas", 9))
        self.clock.pack(side="right")

        root.bind("<KeyPress>", self._kd)
        root.bind("<KeyRelease>", self._ku)
        root.protocol("WM_DELETE_WINDOW", self._close)

        self._tick()
        self._poll()
        self._drain()

    def _on_tab(self, _e=None):
        """切到路线页时，等布局完成再重绘地图。"""
        if self._closing:
            return
        current = self.nb.index("current")
        if current != 0:
            self.t_joy.stop()
        if self._map_timer is not None:
            self.root.after_cancel(self._map_timer)
            self._map_timer = None
        if current == 1:
            self._map_timer = self.root.after_idle(self._redraw_map)

    def _redraw_map(self):
        self._map_timer = None
        self.t_route.map.redraw()

    def _kd(self, e):
        if not self._closing and self.nb.index("current") == 0 and not self.t_route.running:
            self.t_joy.key_down(e.keysym.lower())

    def _ku(self, e):
        if self.nb.index("current") == 0:
            self.t_joy.key_up(e.keysym.lower())

    def _reconnect(self):
        if self._closing:
            return
        self._reset_reader = True
        if self._read_timer is not None:
            self.root.after_cancel(self._read_timer)
            self._read_timer = None
        if not self._reading:
            self._poll()

    def _tick(self):
        self._tick_timer = None
        if self._closing:
            return
        # 模拟器连接状态
        try:
            if not self._read_error:
                self.lb.config(text="● ADB 读屏已连接", foreground="#108040")
            else:
                self.lb.config(text="○ " + self._read_error,
                               foreground="#c02020")
            if self.r.last_error:
                text = self.lb.cget("text")
                self.lb.config(text=f"{text}   定位下发失败：{self.r.last_error}", foreground="#c02020")
        except Exception:
            pass
        try:
            inw = 6 <= time.localtime().tm_hour < 23
            self.clock.config(text=time.strftime("%H:%M:%S") + ("   有效时段内" if inw else "   已超时段"),
                              foreground="#007acc" if inw else "#c00")
        except Exception:
            pass
        self._tick_timer = self.root.after(1000, self._tick)

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
        self.reader = reader
        self._read_error = error
        self.t_joy.update_state(state)
        delay = 0 if self._reset_reader else max(1, int(1000 * self.r.cfg["read_interval"]))
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
        if (self._closing and not self.t_route.running and not self.r.is_alive()
                and not self.r.feeder.is_alive()
                and all(proc is None or proc.poll() is not None
                        for proc in (self.t_auto.proc, self.t_tools.proc))):
            self.root.destroy()
            return
        self.root.after(25, self._drain)

    def _close(self):
        if not self._closing:
            self._closing = True
            self.lb.config(text="正在停止后台任务…")
            for timer in (self._read_timer, self._map_timer, self._tick_timer):
                if timer is not None:
                    self.root.after_cancel(timer)
            self._read_timer = self._map_timer = self._tick_timer = None
            self.t_route.closing = True
            self.t_auto.closing = True
            self.t_joy.stop()
            self.t_route.stop()
            self.t_route.map.close()
            self.r.shutdown()
            self.r.feeder.stop()
        self.t_tools.stop()
        self.t_auto.stop()


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
    main()
