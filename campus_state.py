# -*- coding: utf-8 -*-
"""跑步状态的公共数据结构与 XML 解析（离线仿真、暂停态读取用）。"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET

DUMP_PATH = "/sdcard/codex-lepao-run.xml"


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


def _text_map(xml: str) -> dict:
    root = ET.fromstring(xml)
    out = {}
    for node in root.iter("node"):
        rid = node.get("resource-id", "")
        if rid:
            out[rid.split("/")[-1]] = node.get("text", "").strip()
    return out


def parse_point(text: str):
    """待分配(再跑250米后分配打卡点) -> ("", None, 250)；实验楼B-2(距你300.93米) -> ("实验楼B-2", 300.93, None)。"""
    text = (text or "").strip()
    if not text:
        return "", None, None
    m = re.search(r"再跑\s*([\d.]+)\s*米", text)
    if m:
        return "", None, float(m.group(1))
    m = re.search(r"^(.*?)\s*[（(]距你\s*([\d.]+)\s*米[)）]\s*$", text)
    if m:
        return m.group(1).strip(), float(m.group(2)), None
    return text, None, None


def parse_run_xml(xml: str) -> RunState:
    t = _text_map(xml)
    try:
        distance_m = float(t.get("tv_distance_value", "0") or 0) * 1000.0
    except ValueError:
        distance_m = 0.0
    elapsed = 0
    m = re.match(r"(\d+):(\d{2}):(\d{2})", t.get("chronometer", ""))
    if m:
        elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    try:
        punched = int(float(t.get("tv_log_num_value", "0") or 0))
    except ValueError:
        punched = 0
    name, dist, pending = parse_point(t.get("tv_point_name", ""))
    return RunState(distance_m, elapsed, punched, t.get("tv_point_name", ""),
                    name, dist, pending, t.get("tv_start_pause_continue_run", ""))


def read_run_xml(client, retries: int = 2):
    """仅用于离线仿真/暂停态：解析 uiautomator 的 XML dump。"""
    for attempt in range(retries):
        try:
            out = client.adb_shell("uiautomator", "dump", DUMP_PATH)
            if "dumped to" in out or attempt == retries - 1:
                xml = client.adb_shell("cat", DUMP_PATH)
                if "<hierarchy" in xml:
                    return parse_run_xml(xml)
        except Exception:
            pass
        time.sleep(0.3)
    return None
