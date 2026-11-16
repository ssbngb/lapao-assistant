import os
import sys
import time
import math
import random
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant", "src"))

from lepao.geo import gcj02_to_wgs84, offset_meters
from lepao.mumu import MuMuLocationClient
from lepao.sensors import StepFeeder
from lepao.routes import StadiumBand, make_plan, PreparedRoute

MANAGER = r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
SERIAL = "127.0.0.1:16384"

def adb_shell(cmd):
    return subprocess.run([ADB, "-s", SERIAL, "shell", cmd],
                          capture_output=True, text=True, timeout=15).stdout.strip()

def tap(x, y):
    adb_shell(f"input tap {x} {y}")

def swipe(x1, y1, x2, y2, duration_ms):
    adb_shell(f"input swipe {x1} {y1} {x2} {y2} {duration_ms}")

def main():
    print("=" * 50, flush=True)
    print("      步道乐跑 · 自由跑 2 公里实测程序", flush=True)
    print("=" * 50, flush=True)

    client = MuMuLocationClient(vm_index=0, manager_path=MANAGER)
    client.ensure_gps_enabled()

    # 1. 跑道参考线六点 (标准田径场示例坐标)
    pts_gcj = [
        [31.638375, 118.901398],
        [31.637982, 118.901982],
        [31.637124, 118.901982],
        [31.636731, 118.901398],
        [31.637124, 118.900813],
        [31.637982, 118.900813],
    ]
    pts_wgs = [list(reversed(gcj02_to_wgs84(p[1], p[0]))) for p in pts_gcj]
    band = StadiumBand(pts_wgs)
    plan = make_plan(band, laps=6.0)
    route = PreparedRoute(band, plan)

    # 2. 提前下发起点定位
    start_pos = route.position_at(0)
    client.set_wgs84(*start_pos)
    print(f"[1/4] 定位已就绪：田径场起点 ({start_pos[0]:.6f}, {start_pos[1]:.6f})", flush=True)

    # 3. 调起自由跑页面
    top = adb_shell("dumpsys activity activities | grep -E 'topResumedActivity'")
    if "MapRunActivity" not in top:
        print("[2/4] 正在模拟点击进入自由跑…", flush=True)
        # 点击开始乐跑
        tap(540, 1209)
        time.sleep(1.2)
        # 点击自由跑
        tap(735, 1165)
        time.sleep(2.0)
        
        # 确认是否已进入 MapRunActivity
        for _ in range(10):
            top = adb_shell("dumpsys activity activities | grep -E 'topResumedActivity'")
            if "MapRunActivity" in top:
                break
            time.sleep(1)

    print("[3/4] 步道乐跑记录界面已启动，开始下发 GPS 轨迹与传感器步数…", flush=True)

    # 4. 配置运行参数
    target_dist_m = 2100.0  # 2.10 公里，满 2 公里达标
    base_speed_kmh = 11.2   # 约 5分21秒/公里，在 3'00" ~ 8'00" 安全区间
    base_speed_mps = base_speed_kmh / 3.6
    cadence = 168           # 168 步/分 自然步频
    lap_length = route.lap_length_m

    feeder = StepFeeder(client, cadence=cadence, logger=lambda _: None)
    feeder.start()

    dist = 0.0
    t0 = time.time()
    last_log = t0
    next_tick = t0
    stats_points = 0
    drift_n, drift_e = 0.0, 0.0
    last_lap = -1

    try:
        while dist < target_dist_m:
            now = time.time()
            dt = 0.5 * (1.0 + random.uniform(-0.05, 0.05))
            # 微量速度波动
            speed = base_speed_mps * (1.0 + random.uniform(-0.06, 0.06))
            dist += speed * dt

            lap_now = int(dist // lap_length)
            if lap_now != last_lap:
                last_lap = lap_now
                ang = random.uniform(0, 2 * math.pi)
                r = random.uniform(0, 0.8)
                drift_n, drift_e = r * math.cos(ang), r * math.sin(ang)

            lat, lon = route.position_at(dist)
            bearing = math.radians(route.bearing_at(dist))
            lat, lon = offset_meters(lat, lon,
                                     drift_n + random.gauss(0, 0.4),
                                     drift_e + random.gauss(0, 0.4))

            try:
                client.set_wgs84(lat, lon)
                stats_points += 1
            except Exception:
                pass

            if now - last_log >= 15.0 or dist >= target_dist_m:
                elapsed = now - t0
                pct = min(100.0, dist / target_dist_m * 100.0)
                remaining = max(0.0, target_dist_m - dist)
                eta_s = int(remaining / speed)
                pace_s = int(1000.0 / speed)
                pace_str = f"{pace_s // 60}'{pace_s % 60:02d}\""
                print(f"[进度] 已跑 {dist/1000.0:.2f} / 2.10 km ({pct:.1f}%) | "
                      f"配速: {pace_str} | 步数: {feeder.steps} | "
                      f"耗时: {int(elapsed)//60:02d}:{int(elapsed)%60:02d} | "
                      f"预计剩余: {eta_s//60:02d}:{eta_s%60:02d}", flush=True)
                last_log = now

            next_tick += dt
            sl = next_tick - time.time()
            if sl > 0:
                time.sleep(sl)
            else:
                next_tick = time.time()
    finally:
        feeder.stop()

    print("[4/4] 2.10 公里已跑满，正在暂停并结束记录…", flush=True)
    # 长按暂停
    swipe(540, 1812, 540, 1812, 2500)
    time.sleep(1.5)
    # 长按停止
    swipe(792, 1812, 792, 1812, 2500)
    time.sleep(1.5)
    # 点击确认保存/结束
    tap(735, 1128)
    time.sleep(3.0)

    # 截图
    adb_shell("screencap -p /sdcard/freerun_done.png")
    subprocess.run([ADB, "-s", SERIAL, "pull", "/sdcard/freerun_done.png", "./freerun_done.png"],
                   capture_output=True)
    print("=" * 50, flush=True)
    print("      测试自由跑 2.10 公里顺利完成！", flush=True)
    print(f"      总用时: {int(time.time() - t0)//60} 分钟 | 总步数: {feeder.steps}", flush=True)
    print("=" * 50, flush=True)

if __name__ == "__main__":
    main()
