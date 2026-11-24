# -*- coding: utf-8 -*-
"""校园定向打卡跑：按 App 随机分配的打卡点导航，并锁定有效配速区间。

规则（本校 2026-2027 上学期，见 App「跑步规则说明」）：
  * 单次里程 2.00 ~ 10.00 公里
  * 平均配速 3'00" ~ 8'00" / 公里
  * 必须完成 2 个打卡点（定向打卡：跑满 250 米分配第一个，打完再分配下一个）

两个核心问题：
  1. 随机打卡点：读取当前目标（名称 + 距你米数），用两次正交位移估距离梯度，
     沿负梯度方向跑，每 60 米复核；进入 20 米内原地停留等系统自动打卡。
  2. 配速：以 App 计时器与里程闭环控速，目标 6'00"/km，瞬时速度锁 2.30~4.40 m/s，
     打卡停留损失的时间由随后加速补回。

重要：跑步页计时器每秒刷新，`uiautomator dump` 会被系统杀掉（实测 rc=137），
所以状态读取走截图 + 离线 OCR（screen_state.py），放后台线程，不阻塞定位下发。

只做定位与步数注入，不代替 App 结束记录。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant"))

# 输出统一 UTF-8，避免经 GUI 管道回传时中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant", "src"))

from lepao.geo import offset_meters, meters_per_degree   # noqa: E402
from lepao.mumu import MuMuLocationClient, discover_mumu # noqa: E402
from lepao.sensors import StepFeeder, cadence_for_speed  # noqa: E402

from campus_state import RunState, read_run_xml          # noqa: E402
from screen_state import ScreenReader, read_screen_state  # noqa: E402

MANAGER = r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"

PACE_MIN_S = 180.0
PACE_MAX_S = 480.0
# 允许 GUI/环境变量覆盖（LEPAO_VMAX / LEPAO_PUSH_INTERVAL / LEPAO_READ_INTERVAL）
V_MIN = 2.30
V_MAX = float(os.environ.get("LEPAO_VMAX", "6.00"))

# 标准 400m 跑道几何（体育场形：两直道 + 两半圆）
LANE_STRAIGHT = 84.39      # 直道长度
LANE_RADIUS = 36.5         # 弯道半径（内圈）
LANE_LEN = 2 * LANE_STRAIGHT + 2 * math.pi * LANE_RADIUS   # ≈398.1m 一圈
# MuMu 虚拟定位下发节流：App 的 GPS 采样率约 1Hz，下发间隔需与之匹配。
# 实测接收率：200ms→52%，500ms→42%，2000ms→40%，1.0s 为最佳匹配点。
LOCATION_PUSH_INTERVAL_S = float(os.environ.get("LEPAO_PUSH_INTERVAL", "1.0"))

PROBE_M = 20.0
# 单步位移上限：实测 >8m 且间隔短时会被步道乐跑判定为瞬移并终止跑步。
# 原值 60m 会在打卡导航时一步跳 60 米，必然触发反作弊。
STEP_MAX_M = 8.0
CP_ARRIVE_M = 20.0
CP_DWELL_S = 12.0
READ_INTERVAL_S = float(os.environ.get("LEPAO_READ_INTERVAL", "0.5"))
LAST_ERROR = None


class StateWatcher(threading.Thread):
    """后台持续读取跑步页，主循环只取最新快照。"""

    def __init__(self, read_fn, interval=READ_INTERVAL_S):
        super().__init__(daemon=True)
        self.read_fn = read_fn
        self.interval = interval
        self.latest = None
        self.stamp = 0.0
        self.fatal = None
        self._stop = threading.Event()

    def run(self):
        misses = 0
        while not self._stop.is_set():
            shot_at = time.monotonic()
            try:
                st = self.read_fn()
            except Exception as exc:
                st = None
                self.fatal = "%s: %s" % (type(exc).__name__, exc) if misses >= 4 else None
            if st is not None:
                self.latest, self.stamp = st, shot_at
                misses = 0
            else:
                misses += 1
                if misses >= 12 and self.fatal:
                    return
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def wait_fresh(self, since: float, timeout: float = 10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.stamp >= since and self.latest is not None:
                return self.latest
            if self.fatal:
                raise RuntimeError("界面读取失败：%s" % self.fatal)
            time.sleep(0.2)
        return self.latest


class CampusRunner:
    def __init__(self, client, pace_min_km: float, target_km: float, cp_needed: int,
                 log=lambda s: print(s, flush=True), watcher: bool = True, source: str = "screen",
                 auto_start: bool = False, auto_finish: bool = False,
                 start_lat: float | None = None, start_lon: float | None = None,
                 adb_serial: str | None = None):
        self.c = client
        self.log = log
        self.target_pace_s = max(PACE_MIN_S + 30, min(PACE_MAX_S - 30, pace_min_km * 60.0))
        self.base_v = 1000.0 / self.target_pace_s
        self.v = self.base_v
        self.target_km = target_km
        self.cp_needed = cp_needed
        self.punched = 0
        self.source = source
        self.auto_start = auto_start
        self.auto_finish = auto_finish
        self.reader = ScreenReader(
            adb_path=getattr(client, "adb_path", None),
            serial=adb_serial,
        ) if source == "screen" else None

        # 默认校园田径场起点 (支持参数传入或环境变量，默认提供示例坐标)
        def_lat = start_lat if start_lat is not None else float(os.environ.get("LEPAO_START_LAT", "31.638994"))
        def_lon = start_lon if start_lon is not None else float(os.environ.get("LEPAO_START_LON", "118.896724"))
        self.default_start = (def_lat, def_lon)
        already_in_run = False
        if source == "screen":
            try:
                top_lines = [l for l in client.adb_shell("dumpsys", "activity", "activities").splitlines()
                             if "topResumedActivity" in l or "mResumedActivity" in l]
                already_in_run = any("MapRunActivity" in l for l in top_lines)
            except Exception:
                pass
        if source == "screen" and not already_in_run:
            client.set_wgs84(*self.default_start)
            self.lat, self.lon = self.default_start
        else:
            fix = client.gps_status().get("location") or self.default_start
            self.lat, self.lon = fix
        self.bearing = 0.0
        self.last = time.monotonic()
        self.cruise_heading = 0.0
        self.cruise_left = 120.0
        # 每 26 米转 23°，约 400 米一圈（标准田径场内圈），可按场地调整
        self.turn_step = 23.0
        self.lane_s = 0.0            # 沿标准 400m 跑道的累计距离
        self.feeder = StepFeeder(client, cadence=cadence_for_speed(self.v * 3.6),
                                 logger=lambda *_: None)
        self.watcher = StateWatcher(self._read) if watcher else None

    # ---------- 状态 ----------
    def _read(self):
        if self.reader is not None:
            return read_screen_state(self.reader)
        return read_run_xml(self.c)

    def state(self):
        if self.watcher is not None:
            if self.watcher.fatal:
                raise RuntimeError("界面读取失败：%s" % self.watcher.fatal)
            return self.watcher.latest
        return self._read()

    def fresh_state(self, since: float):
        if self.watcher is not None:
            return self.watcher.wait_fresh(since)
        return self._read()

    # ---------- 运动学 ----------
    def _apply(self, dt: float):
        # 航向抖动：模拟 GPS 漂移与身体左右摆动，让轨迹呈自然弧线而非直线
        self.bearing = (self.bearing + random.uniform(-4.0, 4.0) * dt) % 360.0
        # 步长微扰：速度在 ±5% 内自然波动，避免恒速机器特征
        v = self.v * random.uniform(0.95, 1.05)
        dn = math.cos(math.radians(self.bearing)) * v * dt
        de = math.sin(math.radians(self.bearing)) * v * dt
        self.lat, self.lon = offset_meters(self.lat, self.lon, dn, de)
        self.c.set_wgs84(self.lat, self.lon)

    def move_for(self, seconds: float, control: bool = True):
        end = time.monotonic() + max(0.0, seconds)
        next_ctrl = 0.0
        while True:
            now = time.monotonic()
            dt = now - self.last
            self.last = now
            if dt > 0:
                self._apply(min(dt, 2.0))
            if control and now >= next_ctrl:
                st = self.state()
                if st is not None:
                    self.update_speed(st)
                next_ctrl = now + 5.0
            if now >= end:
                break
            time.sleep(max(0.0, min(LOCATION_PUSH_INTERVAL_S, end - now)))

    def move_meters(self, meters: float, bearing: float | None = None):
        if bearing is not None:
            self.bearing = bearing % 360.0
        self.move_for(abs(meters) / max(self.v, 0.5))

    # ---------- 配速闭环 ----------
    def update_speed(self, st: RunState):
        if st.elapsed_s <= 2:
            return
        desired_m = st.elapsed_s * self.base_v
        err = desired_m - st.distance_m
        v = self.base_v + max(-0.9, min(1.6, err / 25.0))
        avg = st.avg_pace_s
        if avg is not None:
            if avg < PACE_MIN_S + 30:
                v = min(v, self.base_v * 0.90)
            elif avg > PACE_MAX_S - 45:
                v = max(v, 4.15)
        self.v = max(V_MIN, min(V_MAX, v))
        self.feeder.set_cadence(cadence_for_speed(self.v * 3.6))

    # ---------- 导航 ----------
    def bearing_to_checkpoint(self, dist: float) -> float:
        """用「三边定位」解出打卡点坐标，再朝它走。

        为什么不用梯度（原版做法）：
          原版走 L 形后取
              g_n = (d1-d0)/PROBE_M
              g_e = (d2-d1)/PROBE_M
          但 g_e 是「北 20 米处再向东」的偏导，和 g_n 不在同一点上。
          远场近似没问题，靠近打卡点时距离场弯曲，方向会算偏，
          表现为「先靠近、再走过头、距离反弹」。

        现在的做法：走 L 形拿到三个点 (P0,P1,P2) 和三个距离 (d0,d1,d2)，
        直接解二元二次方程组定位打卡点，再取方位角。这是精确解，不依赖近似。

        历史坑（务必保留）：
          * 回退绝不能直接改坐标 + set_wgs84 —— 那是瞬移，会被反作弊终止。
          * PROBE_M 不能太小 —— 走 4 米仅 1.5 秒，App 距离还没刷新，读到旧值。
        """
        s0 = self.fresh_state(time.monotonic() + 0.1) if self.watcher else None
        d0 = s0.cp_dist_m if s0 and s0.cp_dist_m is not None else dist

        mlat, mlon = meters_per_degree(self.default_start[0])

        def wait_refresh(extra=1.2):
            if self.watcher:
                self.fresh_state(time.monotonic() + extra)

        # P0 = 原点；P1 = 向北 PROBE_M；P2 = P1 再向东 PROBE_M
        p0 = (0.0, 0.0)
        self.move_meters(PROBE_M, 0.0)
        wait_refresh()
        s1 = self.state()
        d1 = s1.cp_dist_m if s1 and s1.cp_dist_m is not None else d0
        p1 = (0.0, PROBE_M)

        self.move_meters(PROBE_M, 90.0)
        wait_refresh()
        s2 = self.state()
        d2 = s2.cp_dist_m if s2 and s2.cp_dist_m is not None else d1
        p2 = (PROBE_M, PROBE_M)

        # ---- 三边定位：解 (x,y) 使 |P-Pi| = di ----
        # 以 pa 为原点展开，两式相减消去二次项，得线性方程组：
        #   ax*x + ay*y = (ax² + ay² - db² + da²) / 2
        #   bx*x + by*y = (bx² + by² - dc² + da²) / 2
        # 其中 (ax,ay) = pb-pa, (bx,by) = pc-pa
        def trilaterate(pa, da, pb, db, pc, dc):
            ax, ay = pb[0] - pa[0], pb[1] - pa[1]
            bx, by = pc[0] - pa[0], pc[1] - pa[1]
            rhs_a = (ax * ax + ay * ay - db * db + da * da) / 2.0
            rhs_b = (bx * bx + by * by - dc * dc + da * da) / 2.0
            det = ax * by - ay * bx
            if abs(det) < 1e-9:
                return None
            return ((rhs_a * by - ay * rhs_b) / det,
                    (ax * rhs_b - rhs_a * bx) / det)

        pos = None
        if d0 and d1 and d2 and min(d0, d1, d2) > 0.5:
            pos = trilaterate(p0, d0, p1, d1, p2, d2)
            # 校验：解出的位置反算三个距离，误差过大就丢弃
            if pos:
                ex, ey = pos
                err = max(abs(math.hypot(ex - p[0], ey - p[1]) - d)
                          for p, d in ((p0, d0), (p1, d1), (p2, d2)))
                if err > 15.0:
                    self.log("三边定位误差 %.1f 米偏大，改用梯度方向" % err)
                    pos = None

        if pos is not None:
            ex, ey = pos
            # 解出的位置是「相对进入探测时的原点」，而当前实际位置在 (PROBE_M, PROBE_M)
            dx, dy = ex - PROBE_M, ey - PROBE_M
            b = math.degrees(math.atan2(dx, dy)) % 360.0     # 北为 0°，东为 90°
            self.log("三边定位：目标偏移 (%.1f m 东, %.1f m 北)，方位 %.1f°" % (dx, dy, b))
            return b

        # ---- 兜底：梯度方向 ----
        g_n = (d1 - d0) / PROBE_M
        g_e = (d2 - d1) / PROBE_M
        if math.hypot(g_n, g_e) < 0.05:
            dn_c = (self.default_start[0] - self.lat) * mlat
            de_c = (self.default_start[1] - self.lon) * mlon
            b = math.degrees(math.atan2(de_c, dn_c)) % 360.0
            self.log("梯度无效（距离可能未刷新），朝跑道中心 %.1f° 回归" % b)
        else:
            b = math.degrees(math.atan2(-g_e, -g_n)) % 360.0
            self.log("探测方位（梯度）：%.1f° (N=%.2f, E=%.2f /米)" % (b, g_n, g_e))
        return b

    def _lane_pos(self, s: float):
        """标准 400m 跑道参数化：两条直道 + 两个半圆弯道（逆时针）。

        以跑道中心为原点，北为 +y、东为 +x。
        直道 84.39m、弯道半径 36.5m，一圈约 398.1m。
        返回 (北向米, 东向米)。
        """
        A = LANE_STRAIGHT / 2.0                      # 半直道 = 42.195
        R = LANE_RADIUS                              # 36.5
        P1 = LANE_STRAIGHT                           # 一条直道结束
        P2 = P1 + math.pi * R                        # + 北半圆
        P3 = 2 * P1 + math.pi * R                    # + 西直道
        P4 = 2 * P1 + 2 * math.pi * R                # 一圈结束
        s = s % P4

        if s < P1:                                   # 东直道：y 从 -A 到 +A
            return (-A + s, R)
        elif s < P2:                                 # 北半圆：θ 0→π
            th = (s - P1) / R
            return (A + R * math.sin(th), R * math.cos(th))
        elif s < P3:                                 # 西直道：y 从 +A 到 -A
            return (A - (s - P2), -R)
        else:                                        # 南半圆：θ π→2π
            th = math.pi + (s - P3) / R
            return (-A + R * math.sin(th), R * math.cos(th))

    def cruise(self, meters: float):
        """沿标准 400m 跑道跑圈（增量推进，避免里程跳跃）。

        关键：必须用「从当前位置挪一小步」的增量写法。
        若每步直接 set 绝对目标坐标，步道乐跑的里程会跳变式增长
        （实测 6 秒记 50 米 → 配速瞬间 2'00"），闭环来不及拉回就被判异常。
        原版这里还硬编码了作者学校(31.63,118.89)的经纬度范围，会导致航向锁死正北。
        """
        c_lat, c_lon = self.default_start           # 跑道中心
        m_per_lat, m_per_lon = meters_per_degree(c_lat)   # 与 offset_meters 一致的椭球系数
        step_size = 2.5                              # 每步 2.5 米（已验证安全的节奏）
        remaining = meters
        next_ctrl = 0.0
        while remaining > 0:
            now = time.monotonic()
            # 定期用 App 实时配速校正速度，保持闭环有效
            if now >= next_ctrl:
                st = self.state()
                if st is not None:
                    self.update_speed(st)
                next_ctrl = now + 5.0

            st_m = min(step_size, remaining)
            self.lane_s = (self.lane_s + st_m) % (
                2 * LANE_STRAIGHT + 2 * math.pi * LANE_RADIUS)
            dn, de = self._lane_pos(self.lane_s)

            # 全部用「米」在跑道坐标系里算，不再经 lat/lon 中转，
            # 避免硬编码系数(111320/101300)与 geo 的椭球系数(约 110765/101359)打架导致几何变形。
            cur_n = (self.lat - c_lat) * m_per_lat
            cur_e = (self.lon - c_lon) * m_per_lon
            mv_n = dn - cur_n
            mv_e = de - cur_e
            dist = math.hypot(mv_n, mv_e)
            if dist > 1e-6 and dist > st_m * 2.0:
                k = (st_m * 2.0) / dist          # 限幅，避免一步跳太远
                mv_n *= k
                mv_e *= k
            mv_n += random.uniform(-0.4, 0.4)    # GPS 自然抖动
            mv_e += random.uniform(-0.4, 0.4)
            self.lat, self.lon = offset_meters(self.lat, self.lon, mv_n, mv_e)
            self.c.set_wgs84(self.lat, self.lon)
            time.sleep(st_m / max(self.v, 0.5))
            remaining -= st_m

    def _punch_signal(self, st: RunState, name: str, cand):
        """判断是否已过点：目标行变成"待分配"，或目标换成另一个名字，或已打卡数增加，或打卡点距离消失。"""
        if st is None:
            return False, cand
        if st.punched > self.punched:
            return True, cand
        if st.pending_m:
            return True, cand
        if st.cp_name and st.cp_name != name:
            return (cand == st.cp_name), st.cp_name
        if st.cp_dist_m is None and not st.cp_name:
            return True, cand
        return False, cand

    def go_to_checkpoint(self, st: RunState) -> bool:
        name, dist = st.cp_name, st.cp_dist_m
        self.log("→ 前往打卡点「%s」，距 %.1f 米" % (name, dist))
        confirm, cand = 0, None
        need_probe = True
        for _ in range(60):
            cur = self.state()
            if cur is None:
                self.move_for(2.0)
                continue
            hit, cand = self._punch_signal(cur, name, cand)
            confirm = confirm + 1 if hit else 0
            if confirm >= 2:
                self.punched += 1
                self.log("✔ 打卡成功（%s），累计 %d 个" % (name, self.punched))
                return True
            if dist is not None and dist <= CP_ARRIVE_M:
                break
            if cur.cp_dist_m is None:
                self.move_for(2.0)
                continue
            dist = cur.cp_dist_m
            if dist <= CP_ARRIVE_M:
                break
            if need_probe:
                self.bearing = self.bearing_to_checkpoint(dist)
                need_probe = False
                cur_after_probe = self.fresh_state(time.monotonic() + (0.2 if self.watcher else 0.0))
                if cur_after_probe and cur_after_probe.cp_dist_m is not None:
                    dist = cur_after_probe.cp_dist_m
                if dist <= CP_ARRIVE_M:
                    break
            step = min(max(dist - CP_ARRIVE_M * 0.6, 15.0), STEP_MAX_M)
            self.move_meters(step)
            nxt = self.fresh_state(time.monotonic() + (0.4 if self.watcher else 0.0))
            if nxt is not None and nxt.cp_dist_m is not None:
                pace_s = nxt.avg_pace_s
                pace_str = "%d'%02d\"" % (pace_s // 60, pace_s % 60) if pace_s else "--"
                self.log("→ 逼近「%s」：距 %.1f m (原 %.1f m) | %.2fkm 均速%s" % (
                    name, nxt.cp_dist_m, dist, nxt.distance_m / 1000.0, pace_str))
                if nxt.cp_dist_m > dist - (step * 0.3):
                    self.log("距离未明显缩短 (%.1f -> %.1f)，重新校准方向" % (dist, nxt.cp_dist_m))
                    need_probe = True

        self.log("已进入打卡半径，原地停留 %.0f 秒等待系统打卡" % CP_DWELL_S)
        end = time.monotonic() + CP_DWELL_S
        while time.monotonic() < end:
            cur = self.state()
            hit, cand = self._punch_signal(cur, name, cand)
            confirm = confirm + 1 if hit else 0
            if confirm >= 2:
                self.punched += 1
                self.log("✔ 打卡成功（%s），累计 %d 个" % (name, self.punched))
                return True
            self.move_for(1.0)
        return False

    # ---------- 辅助操作 ----------
    def _top_activity(self):
        lines = [l for l in self.c.adb_shell("dumpsys", "activity", "activities").splitlines()
                 if "topResumedActivity" in l or "mResumedActivity" in l]
        return "\n".join(lines)

    def _ensure_screen_awake(self):
        try:
            pw = self.c.adb_shell("dumpsys", "power")
            if "mWakefulness=Awake" not in pw:
                self.log("检测到模拟器屏幕未唤醒，正在唤醒屏幕…")
                self.c.adb_shell("input", "keyevent", "224")
                time.sleep(1.0)
        except Exception:
            pass

    def _launch_main_activity(self) -> str:
        """启动步道乐跑并确认进入 MainActivity（解决 SplashActivity 偶发闪退回桌面）。"""
        for launch_try in range(3):
            self.c.set_wgs84(*self.default_start)
            self.lat, self.lon = self.default_start
            self.c.adb_shell("am", "force-stop", "com.lptiyu.tanke")
            time.sleep(1.0)
            try:
                self.c.adb_shell("am", "start", "-W", "-n", "com.lptiyu.tanke/.activities.splash.SplashActivity")
            except Exception:
                self.c.adb_shell("monkey", "-p", "com.lptiyu.tanke", "-c", "android.intent.category.LAUNCHER", "1")
            ad_skipped = False
            for tick in range(16):
                time.sleep(1.5)
                top = self._top_activity()
                if "MainActivity" in top or "MapRunActivity" in top:
                    time.sleep(1.5)
                    return top
                # 若掉回桌面或未在前台，说明开屏闪退，立即重试启动
                if "com.lptiyu.tanke" not in top and tick >= 1:
                    self.log("检测到步道乐跑启动闪退回桌面，重试第 %d 次…" % (launch_try + 2))
                    break
                # 若长时间停留在开屏页（广告），尝试点右上角跳过
                if "SplashActivity" in top and tick >= 4 and not ad_skipped:
                    ad_skipped = True
                    self.c.adb_shell("input", "tap", "960", "120")
            else:
                top = self._top_activity()
                if "MainActivity" in top or "MapRunActivity" in top:
                    return top
        raise RuntimeError("连续 3 次启动步道乐跑均未进入 MainActivity（当前：%s）" % self._top_activity())

    def auto_start_if_needed(self):
        self._ensure_screen_awake()
        top = self._top_activity()
        if "MapRunActivity" in top:
            if self.reader is not None:
                img = self.reader.shot()
                btns = " ".join(self.reader._ocr_crop(img, (150, 1740, 950, 1890)))
                if "继续" in btns:
                    self.log("检测到跑步处于暂停状态，点击继续恢复记录…")
                    self.c.adb_shell("input", "tap", "290", "1813")
                    time.sleep(1.0)
                    return
                if "长按暂停" in btns:
                    self.log("检测到跑步正在进行中，直接接管控制…")
                    return
            self.c.set_wgs84(*self.default_start)
            self.lat, self.lon = self.default_start
            time.sleep(1.0)
            if self.reader is not None:
                img = self.reader.shot()
                dlg = " ".join(self.reader._ocr_crop(img, (350, 1240, 750, 1450)))
                if "我知道了" in dlg:
                    self.c.adb_shell("input", "tap", "540", "1349")
                    time.sleep(1.0)
            self.c.adb_shell("input", "tap", "559", "1812")
            time.sleep(1.0)
            return

        if "MainActivity" not in top:
            self.log("未在乐跑主页面，正在启动步道乐跑 App…")
            top = self._launch_main_activity()

        if "MainActivity" in top:
            self.c.set_wgs84(*self.default_start)
            self.lat, self.lon = self.default_start
            time.sleep(1.0)
            self.log("检测到在乐跑首页，正在自动进入乐跑…")
            entered = False
            for attempt in range(3):
                if "MainActivity" not in self._top_activity():
                    top = self._launch_main_activity()
                    if "MapRunActivity" in top:
                        entered = True
                        break
                # 先确保切到「乐跑」标签页，再点击「开始乐跑」
                self.c.adb_shell("input", "tap", "540", "1833")
                time.sleep(1.0)
                self.c.adb_shell("input", "tap", "540", "1209")
                time.sleep(1.5)
                if self.reader is not None:
                    img = self.reader.shot()
                    dlg = " ".join(self.reader._ocr_crop(img, (50, 750, 1030, 1880)))
                    if "暂不开启" in dlg or "必要权限" in dlg or "重要提示" in dlg:
                        self.log("检测到权限提示弹窗，自动点击「暂不开启继续跑步」…")
                        if "重要提示" in dlg:
                            self.c.adb_shell("input", "tap", "540", "1320")
                        else:
                            self.c.adb_shell("input", "tap", "540", "1525")
                        time.sleep(1.2)
                        img = self.reader.shot()
                        dlg = " ".join(self.reader._ocr_crop(img, (50, 750, 1030, 1880)))
                        if "暂不开启" in dlg or "重要提示" in dlg:
                            self.c.adb_shell("input", "tap", "540", "1320")
                            time.sleep(1.2)
                            img = self.reader.shot()
                            dlg = " ".join(self.reader._ocr_crop(img, (50, 750, 1030, 1880)))

                    if "进入乐跑" in dlg:
                        self.c.adb_shell("input", "tap", "690", "1775")
                    elif "自由跑" in dlg:
                        if self.cp_needed == 0:
                            self.c.adb_shell("input", "tap", "735", "1165")
                        else:
                            self.c.adb_shell("input", "tap", "344", "1165")
                            raise RuntimeError("当前不在学校规定锻炼时段（弹窗为自由跑），停止自动起跑")
                    elif attempt < 2:
                        self.log("未检测到「进入乐跑」弹窗，尝试返回消除遮挡后重试…")
                        self.c.adb_shell("input", "keyevent", "4")
                        time.sleep(1.5)
                        continue
                    else:
                        raise RuntimeError("未能触发「进入乐跑」弹窗，当前可能被弹窗/公告遮挡")
                else:
                    self.c.adb_shell("input", "tap", "690", "1775")
                # 等待 MapRunActivity 加载完成
                for _ in range(10):
                    time.sleep(1.0)
                    if "MapRunActivity" in self._top_activity():
                        entered = True
                        break
                if entered:
                    break
                if attempt < 2:
                    self.log("未成功进入跑步页，返回重试一次…")
                    self.c.adb_shell("input", "keyevent", "4")
                    time.sleep(1.5)
            if not entered:
                raise RuntimeError("点击后未能进入 MapRunActivity 跑步页")
            time.sleep(1.5)
            if self.reader is not None:
                img = self.reader.shot()
                dlg = " ".join(self.reader._ocr_crop(img, (350, 1240, 750, 1450)))
                if "我知道了" in dlg:
                    self.c.adb_shell("input", "tap", "540", "1349")
                    time.sleep(1.0)
            self.c.adb_shell("input", "tap", "559", "1812")
            time.sleep(1.0)

    def auto_finish_record(self):
        self.log("正在自动长按结束跑步并保存…")
        try:
            for _ in range(2):
                self.c.adb_shell("input", "swipe", "559", "1812", "559", "1812", "2500")
                time.sleep(1.5)
                if self.reader is None:
                    break
                img = self.reader.shot()
                btns = " ".join(self.reader._ocr_crop(img, (50, 1730, 1030, 1900)))
                if "继续" in btns or "结束" in btns:
                    break

            for _ in range(2):
                self.c.adb_shell("input", "swipe", "792", "1812", "792", "1812", "2600")
                time.sleep(2.0)
                if "MapRunActivity" not in self._top_activity():
                    break
                if self.reader is not None:
                    img = self.reader.shot()
                    dlg = " ".join(self.reader._ocr_crop(img, (150, 800, 950, 1300)))
                    if "结束" in dlg or "确定" in dlg:
                        self.c.adb_shell("input", "tap", "735", "1165")
                        time.sleep(3.0)
                        break
                    btns = " ".join(self.reader._ocr_crop(img, (50, 1730, 1030, 1900)))
                    if "结束" not in btns and "继续" not in btns:
                        break
            else:
                time.sleep(2.0)
            try:
                if self.reader is not None:
                    self.reader.shot().save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run_finish.png"))
            except Exception:
                pass
            self.log("已保存本次成绩！")
        except Exception as exc:
            self.log("自动结束失败，请手动在手机上长按结束：%s" % exc)

    # ---------- 主循环 ----------
    def run(self):
        if self.auto_start:
            self.auto_start_if_needed()
        if self.watcher is not None:
            self.watcher.start()
        self.log("等待 App 开始记录（请在乐跑点「开始乐跑」）…")
        while True:
            st = self.state()
            if st is not None and st.elapsed_s >= 1:
                break
            if self.watcher is not None and self.watcher.fatal:
                raise RuntimeError("界面读取失败：%s" % self.watcher.fatal)
            time.sleep(0.5)

        self.log("记录已开始：%s" % st)
        self.log("目标配速 %d'%02d\"/km，目标里程 %.2f km，需打卡 %d 个" % (
            self.target_pace_s // 60, self.target_pace_s % 60,
            self.target_km, self.cp_needed))
        self.last = time.monotonic()
        self.feeder.start()
        try:
            while True:
                st = self.state()
                if st is None:
                    self.move_for(2.0, control=False)
                    continue

                if st.punched > self.punched:
                    self.punched = st.punched
                self.update_speed(st)
                if st.cp_dist_m is not None and self.punched < self.cp_needed:
                    if self.go_to_checkpoint(st):
                        continue

                if self.punched >= self.cp_needed and st.distance_m >= self.target_km * 1000:
                    pace = st.avg_pace_s
                    if pace is not None:
                        if pace > PACE_MAX_S - 30:  # 大于 7'30"/km，偏慢
                            self.log("里程已达标但均速偏慢 (当前 %d'%02d\"/km)，正在加速拉低平均配速…" % (pace // 60, pace % 60))
                            self.v = V_MAX
                            self.cruise(25.0)
                            continue
                        elif pace < PACE_MIN_S + 20:  # 小于 3'20"/km，偏快
                            self.log("里程已达标但均速偏快 (当前 %d'%02d\"/km)，正在减速稳定配速…" % (pace // 60, pace % 60))
                            self.v = V_MIN
                            self.cruise(25.0)
                            continue
                    self.log("✔ 打卡点数已满 (%d/%d)、里程达标 (%.2fkm)、平均配速在合格区间！" % (
                        self.punched, self.cp_needed, st.distance_m / 1000.0))
                    break
                if st.distance_m >= 9.9 * 1000:
                    break

                self.log(str(st))
                self.cruise(8.0)
        finally:
            self.feeder.stop()
            if self.watcher is not None:
                self.watcher.stop()

        st = self.state() or st
        self.log("=" * 46)
        self.log("收尾状态：%s" % st)
        pace = st.avg_pace_s
        if pace:
            self.log("平均配速：%d'%02d\"" % (pace // 60, pace % 60))
        if self.auto_finish:
            self.auto_finish_record()
        else:
            self.log("里程与打卡点已满足规则，请回到 App 长按结束记录（本脚本不代按）。")


def _acquire_run_lock():
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campus_run.lock")
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        if sys.platform == "win32":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        fh.seek(0)
        fh.truncate()
        fh.write("%d\n" % os.getpid())
        fh.flush()
    except OSError:
        fh.seek(0)
        holder = fh.read().strip()
        fh.close()
        raise RuntimeError("已有另一份 campus_run.py 正在运行 (PID=%s)，已阻止重复启动" % (holder or "unknown"))
    return fh


def _parse_vm_info(raw: str, vm_index: int) -> dict:
    """兼容 MuMuManager.exe info 返回的单对象、字典映射或列表结构。"""
    data = json.loads(raw)
    if isinstance(data, list):
        for item in data:
            if str(item.get("index")) == str(vm_index):
                return item
        return data[0] if data else {}
    if isinstance(data, dict) and "index" not in data:
        return data.get(str(vm_index), {})
    return data if isinstance(data, dict) else {}


def _android_major_version(ver_str: str) -> int | None:
    try:
        return int(str(ver_str).strip().split(".")[0])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="校园定向打卡 + 配速闭环")
    ap.add_argument("--pace", type=float, default=6.0, help="目标平均配速，分钟/公里（默认 6.0）")
    ap.add_argument("--km", type=float, default=2.05, help="目标里程（默认 2.05）")
    ap.add_argument("--cps", type=int, default=2, help="需要完成的打卡点数（默认 2）")
    ap.add_argument("--auto-start", action="store_true", help="若在首页则自动点击开始乐跑")
    ap.add_argument("--auto-finish", action="store_true", help="达标后自动长按结束并保存")
    ap.add_argument("--start-lat", type=float, default=None, help="起始点纬度（WGS-84，默认读环境变量 LEPAO_START_LAT 或内置示例）")
    ap.add_argument("--start-lon", type=float, default=None, help="起始点经度（WGS-84，默认读环境变量 LEPAO_START_LON 或内置示例）")
    ap.add_argument("--vm-index", type=int, default=None, help="MuMu 模拟器多开实例编号（默认读环境变量 MUMU_VM_INDEX，未设置则为 0）")
    args = ap.parse_args()

    _lock_fh = _acquire_run_lock()

    vm_index = args.vm_index if args.vm_index is not None else int(os.environ.get("MUMU_VM_INDEX", "0"))
    os.environ["MUMU_VM_INDEX"] = str(vm_index)

    disc = discover_mumu()
    manager = disc.get("manager") or (MANAGER if os.path.exists(MANAGER) else None)

    # 1. 检测实例版本并确保模拟器已启动
    adb_host = "127.0.0.1"
    adb_port = 16384 + vm_index * 32
    if manager:
        try:
            chk = subprocess.run([manager, "info", "-v", str(vm_index)], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=15)
            info = _parse_vm_info(chk.stdout, vm_index)
            android_ver = str(info.get("android_version") or "").strip()
            major = _android_major_version(android_ver)
            if major is not None and major >= 15:
                print(
                    f"[警告] 当前 MuMu 实例 #{vm_index} 的系统版本为 Android {android_ver}。\n"
                    f"       官方版步道乐跑在 Android 15 上因加固壳 ART ABI 变更极易闪退！\n"
                    f"       强烈建议在 MuMu 多开器中新建 Android 12 实例（关闭 ROOT），并通过 --vm-index <编号> 指定该实例。",
                    flush=True,
                )
            adb_host = info.get("adb_host_ip") or adb_host
            adb_port = info.get("adb_port") or adb_port
            if not info.get("is_android_started"):
                ver_label = f"Android {android_ver}" if android_ver else "Android"
                print(f"正在唤醒 MuMu 模拟器实例 #{vm_index} ({ver_label})…", flush=True)
                subprocess.run([manager, "control", "-v", str(vm_index), "launch"], capture_output=True, timeout=15)
                for _ in range(45):
                    time.sleep(2)
                    chk = subprocess.run([manager, "info", "-v", str(vm_index)], capture_output=True, text=True,
                                         encoding="utf-8", errors="replace", timeout=15)
                    info = _parse_vm_info(chk.stdout, vm_index)
                    if info.get("is_android_started"):
                        adb_host = info.get("adb_host_ip") or adb_host
                        adb_port = info.get("adb_port") or adb_port
                        break
                time.sleep(3)
        except Exception as exc:
            print(f"[提示] 查询或唤醒 MuMu 实例 #{vm_index} 异常：{exc}", flush=True)

    # 2. 确保 ADB 已连接且系统启动完成
    adb_bin = disc.get("adb")
    serial = f"{adb_host}:{adb_port}"
    if adb_bin:
        for _ in range(30):
            try:
                subprocess.run([adb_bin, "connect", serial], capture_output=True, timeout=15)
                boot = subprocess.run([adb_bin, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                                      capture_output=True, text=True, timeout=15)
                if boot.stdout.strip() == "1":
                    break
            except (subprocess.TimeoutExpired, Exception):
                pass
            time.sleep(2)

    client = MuMuLocationClient(vm_index=vm_index, manager_path=manager)
    client.ensure_gps_enabled()
    runner = CampusRunner(client, args.pace, args.km, args.cps,
                          auto_start=args.auto_start, auto_finish=args.auto_finish,
                          start_lat=args.start_lat, start_lon=args.start_lon,
                          adb_serial=serial)
    try:
        runner.run()
    except KeyboardInterrupt:
        runner.feeder.stop()
        if runner.watcher:
            runner.watcher.stop()
        print("\n已中断，定位停留在当前位置。")
    finally:
        try:
            _lock_fh.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
