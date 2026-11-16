import os
import sys
import time
import math
import random
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant", "src"))

from lepao.mumu import MuMuLocationClient
from lepao.sensors import StepFeeder
from lepao.geo import haversine

MANAGER = r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"
ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
SERIAL = "127.0.0.1:16384"

def main():
    client = MuMuLocationClient(vm_index=0, manager_path=MANAGER)
    client.ensure_gps_enabled()

    p_start = (31.639704, 118.896416) # 当前田径场位置
    p_end = (31.634850, 118.895650)   # 学校南大门目标点 (稍微靠南偏向门外)

    total_dist = haversine(*p_start, *p_end)
    print(f"Starting navigation to 学校南大门, total distance: {total_dist:.1f}m", flush=True)

    feeder = StepFeeder(client, cadence=168, logger=lambda _: None)
    feeder.start()

    speed = 11.2 / 3.6 # 3.11 m/s
    travelled = 0.0
    t0 = time.time()
    dt = 0.5

    try:
        while travelled < total_dist:
            travelled += speed * dt
            progress = min(1.0, travelled / total_dist)
            lat = p_start[0] + (p_end[0] - p_start[0]) * progress + random.gauss(0, 0.000005)
            lon = p_start[1] + (p_end[1] - p_start[1]) * progress + random.gauss(0, 0.000005)
            client.set_wgs84(lat, lon)
            time.sleep(dt)

            if int(travelled) % 50 < 3:
                rem = max(0.0, total_dist - travelled)
                print(f"[前往打卡点1] 已进发 {travelled:.1f}m / {total_dist:.1f}m | 距南大门剩余: {rem:.1f}m", flush=True)

        # 到了南大门，在打卡点附近停留微步运动 5 秒确保触发打卡
        print("已到达学校南大门打卡区，正在停留触发打卡…", flush=True)
        for _ in range(10):
            lat = p_end[0] + random.gauss(0, 0.000008)
            lon = p_end[1] + random.gauss(0, 0.000008)
            client.set_wgs84(lat, lon)
            time.sleep(0.5)

    finally:
        feeder.stop()

    print("打卡点1到达完成！", flush=True)

if __name__ == "__main__":
    main()
