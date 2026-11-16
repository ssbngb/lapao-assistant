# -*- coding: utf-8 -*-
"""离线仿真：用虚拟时钟和假 App 界面验证 campus_run 的导航与配速闭环。

不连模拟器、不产生真实记录。
"""
import math
import sys
import types

sys.path.insert(0, ".")

import campus_run as cr
from lepao.geo import haversine, offset_meters


class VirtualClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)

    def time(self):
        return self.t


class FakeApp:
    """记录开始后按注入坐标累计里程，并按 250m 规则分配随机打卡点。"""

    def __init__(self, clock, start):
        self.clock = clock
        self.pos = start
        self.path = 0.0
        self.t0 = None
        self.punched = 0
        self.cp = None          # (name, lat, lon)
        self.cp_enter = None
        self.cps = [
            ("实验楼B-2南侧", *offset_meters(*start, 260.0, 240.0)),
            ("学校南大门", *offset_meters(*start, -180.0, 330.0)),
        ]
        self.started = False

    def set_pos(self, lat, lon):
        self.path += haversine(*self.pos, lat, lon)
        self.pos = (lat, lon)
        if self.t0 is None and self.path > 0:
            self.t0 = self.clock.t

    def tick(self, now):
        if self.t0 is None:
            return
        if self.cp is None and self.cps and self.path >= 250.0:
            self.cp = self.cps[0]
        if self.cp:
            d = haversine(*self.pos, self.cp[1], self.cp[2])
            if d <= 20.0:
                if self.cp_enter is None:
                    self.cp_enter = now
                elif now - self.cp_enter >= 2.0:
                    self.punched += 1
                    self.cps.pop(0)
                    self.cp = None
                    self.cp_enter = None
            else:
                self.cp_enter = None

    def elapsed(self):
        return int(self.clock.t - self.t0) if self.t0 else 0

    def xml(self):
        dist = "%.2f" % (self.path / 1000.0)
        e = self.elapsed()
        hms = "%02d:%02d:%02d" % (e // 3600, e % 3600 // 60, e % 60)
        if self.cp:
            d = haversine(*self.pos, self.cp[1], self.cp[2])
            point = "%s(距你%.2f米)" % (self.cp[0], d)
        elif self.cps:
            point = "待分配(再跑%d米后分配打卡点)" % max(1, int(250 - self.path))
        else:
            point = "全部打卡完成"
        nodes = [
            ("tv_distance_value", dist),
            ("chronometer", hms),
            ("tv_log_num_value", str(self.punched)),
            ("tv_point_name", point),
            ("tv_start_pause_continue_run", "长按暂停"),
        ]
        body = "".join(
            '<node index="0" text="%s" resource-id="com.lptiyu.tanke:id/%s" class="android.widget.TextView" '
            'package="com.lptiyu.tanke" enabled="true" bounds="[0,0][10,10]"/>' % (t, i)
            for i, t in nodes)
        return '<hierarchy rotation="0">%s</hierarchy>' % body


class FakeClient:
    def __init__(self, clock, app):
        self.clock = clock
        self.app = app
        self.sets = 0

    def gps_status(self):
        return {"enabled": True, "location": self.app.pos, "satellites": 12}

    def ensure_gps_enabled(self):
        return True

    def set_wgs84(self, lat, lon):
        self.sets += 1
        self.app.set_pos(lat, lon)

    def adb_shell(self, *args):
        self.app.tick(self.clock.t)
        if args[:2] == ("uiautomator", "dump"):
            return "UI hierchary dumped to: /sdcard/x.xml"
        if args[0] == "cat":
            return self.app.xml()
        return ""


class FakeFeeder:
    def start(self):
        pass

    def stop(self):
        pass

    def set_cadence(self, _c):
        pass


CASES = [
    ("常规：两个打卡点分布在起点两侧", [("实验楼B-2南侧", 260.0, 240.0), ("学校南大门", -180.0, 330.0)], 0),
    ("起步拖延 60 秒：配速需要追回", [("实验楼B-2南侧", 260.0, 240.0), ("学校南大门", -180.0, 330.0)], 60),
    ("打卡点在正东 520 米：方位需探测", [("图书馆", 20.0, 520.0), ("综合楼", -300.0, 80.0)], 0),
]


def run_case(title, cps, idle_s):
    clock = VirtualClock()
    start = (31.63970, 118.89640)
    app = FakeApp(clock, start)
    app.cps = [(nm, *offset_meters(*start, dn, de)) for nm, dn, de in cps]
    client = FakeClient(clock, app)

    cr.time = clock
    runner = cr.CampusRunner(client, pace_min_km=6.0, target_km=2.05, cp_needed=2,
                             log=lambda *_: None, watcher=False, source="xml")
    runner.feeder = FakeFeeder()
    app.t0 = clock.t - idle_s
    runner.run()

    pace = app.elapsed() / (app.path / 1000.0)
    ok = app.punched >= 2 and app.path >= 2040 and 180 <= pace <= 480
    print("%-26s 里程 %.2f km  用时 %4d s  均速 %d'%02d\"  打卡 %d  下发 %d 次  %s"
          % (title, app.path / 1000.0, app.elapsed(), pace // 60, pace % 60,
             app.punched, client.sets, "通过" if ok else "未通过"))
    return ok


def main():
    return 0 if all(run_case(*case) for case in CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
