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
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant", "src"))

from lepao.geo import offset_meters                      # noqa: E402
from lepao.mumu import MuMuLocationClient, discover_mumu # noqa: E402
from lepao.sensors import StepFeeder, cadence_for_speed  # noqa: E402

from campus_state import RunState, read_run_xml          # noqa: E402
from screen_state import ScreenReader, read_screen_state  # noqa: E402

MANAGER = r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"

PACE_MIN_S = 180.0
PACE_MAX_S = 480.0
V_MIN, V_MAX = 2.30, 4.40

PROBE_M = 20.0
STEP_MAX_M = 60.0
CP_ARRIVE_M = 20.0
CP_DWELL_S = 12.0
READ_INTERVAL_S = 0.5
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
                 auto_start: bool = False, auto_finish: bool = False):
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
        self.reader = ScreenReader() if source == "screen" else None

        # 默认校园田径场起点 (示例经纬度，可按需修改为自己学校操场坐标)
        self.default_start = (31.638994, 118.896724)
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
        dn = math.cos(math.radians(self.bearing)) * self.v * dt
        de = math.sin(math.radians(self.bearing)) * self.v * dt
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
                next_ctrl = now + 2.0
            if now >= end:
                break
            time.sleep(max(0.0, min(0.2, end - now)))

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
        s0 = self.fresh_state(time.monotonic() + 0.1) if self.watcher else None
        d0 = s0.cp_dist_m if s0 and s0.cp_dist_m is not None else dist
        self.move_meters(PROBE_M, 0.0)
        s1 = self.fresh_state(time.monotonic() + (0.4 if self.watcher else 0.0))
        d1 = s1.cp_dist_m if s1 and s1.cp_dist_m is not None else d0
        self.move_meters(PROBE_M, 90.0)
        s2 = self.fresh_state(time.monotonic() + (0.4 if self.watcher else 0.0))
        d2 = s2.cp_dist_m if s2 and s2.cp_dist_m is not None else d1
        g_n = (d1 - d0) / PROBE_M
        g_e = (d2 - d1) / PROBE_M
        if math.hypot(g_n, g_e) < 0.08:
            self.move_meters(PROBE_M, 180.0)
            s3 = self.fresh_state(time.monotonic() + (0.4 if self.watcher else 0.0))
            d3 = s3.cp_dist_m if s3 and s3.cp_dist_m is not None else d2
            self.move_meters(PROBE_M, 270.0)
            s4 = self.fresh_state(time.monotonic() + (0.4 if self.watcher else 0.0))
            d4 = s4.cp_dist_m if s4 and s4.cp_dist_m is not None else d3
            g_n = (d3 - d2) / PROBE_M
            g_e = (d4 - d3) / PROBE_M
        if math.hypot(g_n, g_e) < 0.05:
            b = (self.bearing + 137.0) % 360.0
        else:
            b = math.degrees(math.atan2(-g_e, -g_n)) % 360.0
        self.log("探测方位：%.1f° (ΔN=%.1fm, ΔE=%.1fm)" % (b, d1 - d0, d2 - d1))
        return b

    def cruise(self, meters: float):
        if not (31.6358 <= self.lat <= 31.6412 and 118.8940 <= self.lon <= 118.9005):
            # 如果当前在校区外，直接朝校园田径场中心方向行进
            dlat = (self.default_start[0] - self.lat) * 111320.0
            dlon = (self.default_start[1] - self.lon) * 111320.0 * math.cos(math.radians(self.lat))
            self.cruise_heading = math.degrees(math.atan2(dlon, dlat)) % 360.0

        while meters > 0:
            step = min(self.cruise_left, meters)
            self.move_meters(step, self.cruise_heading)
            meters -= step
            self.cruise_left -= step
            if self.cruise_left <= 1:
                self.cruise_heading = (self.cruise_heading + 60.0) % 360.0
                self.cruise_left = 100.0

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


def main():
    ap = argparse.ArgumentParser(description="校园定向打卡 + 配速闭环")
    ap.add_argument("--pace", type=float, default=6.0, help="目标平均配速，分钟/公里（默认 6.0）")
    ap.add_argument("--km", type=float, default=2.05, help="目标里程（默认 2.05）")
    ap.add_argument("--cps", type=int, default=2, help="需要完成的打卡点数（默认 2）")
    ap.add_argument("--auto-start", action="store_true", help="若在首页则自动点击开始乐跑")
    ap.add_argument("--auto-finish", action="store_true", help="达标后自动长按结束并保存")
    args = ap.parse_args()

    _lock_fh = _acquire_run_lock()

    # 1. 确保模拟器已启动
    adb_port = 16384
    try:
        chk = subprocess.run([MANAGER, "info", "-v", "0"], capture_output=True, text=True, timeout=15)
        info = json.loads(chk.stdout)
        adb_port = info.get("adb_port") or 16384
        if not info.get("is_android_started"):
            print("正在唤醒 MuMu 模拟器 Android 12…", flush=True)
            subprocess.run([MANAGER, "control", "-v", "0", "launch"], capture_output=True, timeout=15)
            for _ in range(45):
                time.sleep(2)
                chk = subprocess.run([MANAGER, "info", "-v", "0"], capture_output=True, text=True, timeout=15)
                info = json.loads(chk.stdout)
                if info.get("is_android_started"):
                    adb_port = info.get("adb_port") or adb_port
                    break
            time.sleep(3)
    except Exception:
        pass

    # 2. 确保 ADB 已连接且系统启动完成
    disc = discover_mumu()
    adb_bin = disc.get("adb")
    serial = f"127.0.0.1:{adb_port}"
    if adb_bin:
        for _ in range(15):
            subprocess.run([adb_bin, "connect", serial], capture_output=True, timeout=10)
            boot = subprocess.run([adb_bin, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                                  capture_output=True, text=True, timeout=10)
            if boot.stdout.strip() == "1":
                break
            time.sleep(2)

    client = MuMuLocationClient(vm_index=0, manager_path=MANAGER)
    client.ensure_gps_enabled()
    runner = CampusRunner(client, args.pace, args.km, args.cps,
                          auto_start=args.auto_start, auto_finish=args.auto_finish)
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
