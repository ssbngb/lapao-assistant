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
    # 往北向校园主干道进发 120 米
    p_target = (p_curr[0] + 0.0011, p_curr[1])

    dist = haversine(*p_curr, *p_target)
    print(f"Running 120m north into campus to trigger Checkpoint 2: {dist:.1f}m", flush=True)

    feeder = StepFeeder(client, cadence=168, logger=lambda _: None)
    feeder.start()

    speed = 11.2 / 3.6 # 3.11 m/s
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

        print("Finished 120m, waiting for Checkpoint 2 allocation...", flush=True)
        time.sleep(3.0)

    finally:
        feeder.stop()

if __name__ == "__main__":
    main()
