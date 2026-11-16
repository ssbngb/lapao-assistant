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

def main():
    client = MuMuLocationClient(vm_index=0, manager_path=MANAGER)
    client.ensure_gps_enabled()

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
    plan = make_plan(band, laps=2.0)
    route = PreparedRoute(band, plan)

    feeder = StepFeeder(client, cadence=170, logger=lambda _: None)
    feeder.start()

    dist = 0.0
    t0 = time.time()
    speed = 12.0 / 3.6 # ~3.33 m/s

    try:
        while dist < 320.0: # Run 320 meters
            dt = 0.5
            dist += speed * dt
            lat, lon = route.position_at(dist)
            client.set_wgs84(lat, lon)
            time.sleep(dt)
            if int(dist) % 50 == 0:
                print(f"Dist: {dist:.1f}m / 320m", flush=True)
    finally:
        feeder.stop()

    print("Completed 320m!", flush=True)

if __name__ == "__main__":
    main()
