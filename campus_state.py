# -*- coding: utf-8 -*-
"""读屏与控制台共用的跑步状态。"""
from __future__ import annotations


class RunState:
    __slots__ = ("distance_m", "elapsed_s", "punched", "point_text",
                 "cp_name", "cp_dist_m", "pending_m", "control")

    def __init__(self, distance_m=0.0, elapsed_s=0, punched=0, point_text="",
                 cp_name="", cp_dist_m=None, pending_m=None, control=""):
        self.distance_m = distance_m
        self.elapsed_s = elapsed_s
        self.punched = punched
        self.point_text = point_text
        self.cp_name = cp_name
        self.cp_dist_m = cp_dist_m
        self.pending_m = pending_m
        self.control = control

    @property
    def avg_pace_s(self):
        if self.distance_m < 50:
            return None
        return self.elapsed_s / (self.distance_m / 1000.0)

    @property
    def paused(self):
        return "继续" in self.control

    def __str__(self):
        pace = self.avg_pace_s
        pace_txt = "%d'%02d\"" % (pace // 60, pace % 60) if pace else "--"
        if self.cp_dist_m is not None:
            tgt = "%s %.1fm" % (self.cp_name, self.cp_dist_m)
        elif self.pending_m:
            tgt = "待分配(%.0fm)" % self.pending_m
        else:
            tgt = self.point_text or "-"
        return "%.2fkm %02d:%02d:%02d 均速%s 打卡%d 目标:%s" % (
            self.distance_m / 1000.0, self.elapsed_s // 3600,
            self.elapsed_s % 3600 // 60, self.elapsed_s % 60, pace_txt,
            self.punched, tgt)
