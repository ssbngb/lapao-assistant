# -*- coding: utf-8 -*-
"""跑步页状态读取（截图 OCR 版）。

跑步页的计时器每秒刷新，`uiautomator dump` 会因等不到 idle 被系统杀掉（实测 rc=137），
因此改用 `exec-out screencap` + 离线 OCR（RapidOCR）识别固定区域：
  * 顶部面板：里程、用时
  * 底部打卡行：打卡点名称、距你多少米 / 待分配还剩多少米

一次完整读取约 3~5 s，放在后台线程里跑，不阻塞 1 Hz 的定位下发。
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import threading
import time

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "assistant", "src"))

from lepao.mumu import discover_mumu  # noqa: E402

from campus_state import RunState  # noqa: E402

# 1080x1920 下的固定区域
CROP_STATS = (100, 110, 900, 440)
CROP_POINT = (60, 1568, 1060, 1715)

_OCR = None
_OCR_LOCK = threading.Lock()


def _ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


class ScreenReader:
    def __init__(self, adb_path: str | None = None, serial: str | None = None, *,
                 reconnect=True):
        self.adb = adb_path or discover_mumu().get("adb")
        self.serial = serial or "127.0.0.1:16384"
        self.reconnect = reconnect
        self._last_dist_m = 0.0
        self._last_elapsed_s = 0
        if not self.adb:
            raise RuntimeError("未找到 adb")

    def shot(self) -> Image.Image:
        out = subprocess.run([self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                             capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.stdout and self.reconnect:
            # 自动尝试重连 ADB
            subprocess.run([self.adb, "connect", self.serial],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = subprocess.run([self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                                 capture_output=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.stdout:
            raise RuntimeError("截图失败")
        return Image.open(io.BytesIO(out.stdout)).convert("RGB")

    def _ocr_crop(self, img: Image.Image, box) -> list[str]:
        with _OCR_LOCK:
            res, _ = _ocr()(img.crop(box))
        return [t for _, t, _ in (res or [])]

    def stats(self, img=None, *, fresh_only=False):
        img = img or self.shot()
        texts = self._ocr_crop(img, CROP_STATS)
        distance_m = elapsed_s = None
        for t in texts:
            s = t.replace(" ", "")
            if distance_m is None and re.fullmatch(r"\d+\.\d{2}", s):
                distance_m = float(s) * 1000.0
            elif elapsed_s is None and re.fullmatch(r"\d{2}:\d{2}:\d{2}", s):
                h, m, sec = s.split(":")
                elapsed_s = int(h) * 3600 + int(m) * 60 + int(sec)
        if fresh_only:
            return distance_m, elapsed_s
        if distance_m is not None:
            self._last_dist_m = max(self._last_dist_m, distance_m)
        if elapsed_s is not None:
            self._last_elapsed_s = max(self._last_elapsed_s, elapsed_s)
        return (distance_m if distance_m is not None else self._last_dist_m,
                elapsed_s if elapsed_s is not None else self._last_elapsed_s)

    def point_info(self, img=None):
        img = img or self.shot()
        texts = self._ocr_crop(img, CROP_POINT)
        punched = sum(1 for t in texts if t.strip() == "已打卡")
        best = None
        for t in texts:
            if "距你" in t:
                name, cp_dist, pending = parse_point(t)
                return name, cp_dist, pending, punched
            if "再跑" in t and best is None:
                best = t
        name, cp_dist, pending = parse_point(best or "")
        return name, cp_dist, pending, punched

    def point(self, img=None):
        name, cp_dist, pending, _ = self.point_info(img)
        return name, cp_dist, pending


def parse_point(text: str):
    """返回 (名称, 距你米数, 待分配剩余米数)。

    田径场 西北角（距你26.43米)  -> ("田径场 西北角", 26.43, None)
    待分配(再跑116米后分配打卡点) -> ("", None, 116.0)
    """
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return "", None, None
    m = re.search(r"再跑\s*([\d.]+)\s*米", text)
    if m:
        return "", None, float(m.group(1))
    m = re.search(r"(?:距你|距离你)\s*([\d.]+)\s*米", text)
    if m:
        name = re.sub(r"[（(]?\s*(?:距你|距离你).*$", "", text).strip(" ()（）")
        return name, float(m.group(1)), None
    return "", None, None


def read_once(reader: ScreenReader):
    img = reader.shot()
    dist, elapsed = reader.stats(img)
    name, cp_dist, pending = reader.point(img)
    return dist, elapsed, name, cp_dist, pending


def read_screen_state(reader: ScreenReader, *, fresh_only=False):
    """一次截图读出 (里程, 用时, 打卡点)。读不到关键数字时返回 None。"""
    img = reader.shot()
    dist, elapsed = reader.stats(img, fresh_only=fresh_only) if fresh_only else reader.stats(img)
    if dist is None or elapsed is None or (dist == 0.0 and elapsed == 0):
        return None
    name, cp_dist, pending, punched = reader.point_info(img)
    return RunState(dist, elapsed, punched, "", name, cp_dist, pending, "")


if __name__ == "__main__":
    import sys
    r = ScreenReader()
    for path in sys.argv[1:] or []:
        img = Image.open(path).convert("RGB")
        t = time.time()
        dist, elapsed = r.stats(img)
        name, cp_dist, pending = r.point(img)
        print("%s  %.1fs  里程=%s 用时=%s 点=%r 距=%s 待分配=%s"
              % (path, time.time() - t, dist, elapsed, name, cp_dist, pending))
