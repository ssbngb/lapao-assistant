import os
import sys
import time
import math
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "assistant", "src"))

from lepao.mumu import MuMuLocationClient
from lepao.sensors import StepFeeder
from lepao.geo import haversine

MANAGER = r"D:\Program Files\Netease\MuMu\nx_main\MuMuManager.exe"

def main():
    client = MuMuLocationClient(vm_index=0, manager_path=MANAGER)
    client.ensure_gps_enabled()

    p_curr = client.gps_status()['location']
    p_target = (31.634900, 118.897750) # 学校南大门精确核心点

    dist = haversine(*p_curr, *p_target)
    print(f"Moving to exact 南大门: {dist:.1f}m", flush=True)

    feeder = StepFeeder(client, cadence=168, logger=lambda _: None)
    feeder.start()

    speed = 10.5 / 3.6 # ~2.9 m/s
    travelled = 0.0
    dt = 0.5

    try:
        while travelled < dist:
            travelled += speed * dt
            prog = min(1.0, travelled / dist)
            lat = p_curr[0] + (p_target[0] - p_curr[0]) * prog
            lon = p_curr[1] + (p_target[1] - p_curr[1]) * prog
            client.set_wgs84(lat, lon)
            time.sleep(dt)

        print("At 南大门 center, staying 8 seconds to trigger punch...", flush=True)
        for _ in range(16):
            client.set_wgs84(p_target[0] + random.gauss(0, 0.000003),
                             p_target[1] + random.gauss(0, 0.000003))
            time.sleep(0.5)

    finally:
        feeder.stop()

    print("Finished reaching gate!", flush=True)

if __name__ == "__main__":
    main()
