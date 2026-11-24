"""一键开跑：从「乐跑首页」接管，走完所有弹窗，开始跑步并自动结算。

前提：MuMu 已启动，步道乐跑已打开并停在「乐跑」首页（看到"开始乐跑"按钮即可）。

用法：
    .venv\\Scripts\\python.exe auto_start.py                # 默认 2.05km
    .venv\\Scripts\\python.exe auto_start.py --km 1.7       # 指定里程
    .venv\\Scripts\\python.exe auto_start.py --no-run       # 只走到跑步页，不接管跑动

所有按钮都按 OCR 识别出的文字位置点击，适配不同 App 版本/分辨率。
"""
import sys, os, time, argparse, subprocess

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import numpy as np
from screen_state import ScreenReader
from lepao.mumu import MuMuLocationClient, discover_mumu

ADB = r"D:\Program Files\Netease\MuMu\nx_main\adb.exe"
SERIAL = "127.0.0.1:16384"
LAT, LON = float(os.environ.get("LEPAO_START_LAT", "0")), \
           float(os.environ.get("LEPAO_START_LON", "0"))   # 起始坐标（WGS-84），未设置时为 0,0

_ocr = None


def ocr_texts(reader):
    """返回 [(text, cx, cy, score)]，坐标已换算到屏幕像素。"""
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    img = reader.shot()
    out = _ocr(np.array(img))
    res = out[0] if isinstance(out, tuple) else out
    items = []
    if res:
        for box, text, score in res:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            items.append((text, int(sum(xs) / 4), int(sum(ys) / 4), float(score)))
    return items


def tap(x, y):
    subprocess.run([ADB, "-s", SERIAL, "shell", "input", "tap", str(int(x)), str(int(y))],
                   capture_output=True, timeout=15)


def tap_text(items, keyword, pick_last=False):
    """按文字找按钮并点击，返回是否点到。"""
    hits = [it for it in items if keyword in it[0]]
    if not hits:
        return False
    it = hits[-1] if pick_last else hits[0]
    tap(it[1], it[2])
    print(f"    点击「{it[0]}」 @ ({it[1]},{it[2]})", flush=True)
    return True


def join_text(items):
    return " ".join(t for t, *_ in items)


def goto_running(reader, c):
    """从乐跑首页走到「跑步进行中」。返回 'running' / 'free' / None。"""
    print("[1/2] 正在从乐跑首页进入跑步…", flush=True)
    last_action = ""
    for i in range(50):
        items = ocr_texts(reader)
        txt = join_text(items)

        # 只有出现「长按暂停」才算真正在记录（未开始时页面同样有"公里/用时/已打卡"）
        if "长按暂停" in txt:
            print("  ==> 已进入跑步状态（计时进行中）", flush=True)
            return "running"

        # 跑步页但还没开始：点「开始乐跑」
        if "开始乐跑" in txt:
            if tap_text(items, "开始乐跑"):
                time.sleep(4)
                continue

        # 超时段，只能自由跑
        if "自由跑" in txt and ("不会关联成绩" in txt or "不是学校规定" in txt):
            print("  !! 当前不在有效时段，将进入自由跑（不计成绩）", flush=True)
            if tap_text(items, "自由跑"):
                time.sleep(3)
                continue

        # 安全提示弹窗
        if "进入乐跑" in txt:
            if tap_text(items, "进入乐跑"):
                time.sleep(3)
                continue

        # 定位权限
        if "同意并使用" in txt:
            if tap_text(items, "同意并使用"):
                time.sleep(3)
                continue

        # 跑区标准说明
        if "我知道了" in txt:
            if tap_text(items, "我知道了"):
                time.sleep(2)
                continue

        # 上次未完成，是否恢复
        if "是否恢复跑步" in txt or ("恢复" in txt and "放弃" in txt):
            if tap_text(items, "放弃"):
                time.sleep(2)
                continue

        # 跑步前页面，点开始
        if "开始乐跑" in txt:
            if tap_text(items, "开始乐跑"):
                time.sleep(4)
                continue

        # 还在首页/其他 tab：切到乐跑
        if "乐跑" in txt and "公里" not in txt:
            if last_action != "tab":
                print("    切到「乐跑」标签", flush=True)
                tap(540, 1830)
                last_action = "tab"
                time.sleep(2)
                continue

        last_action = ""
        time.sleep(1.5)

    print("  !! 超时，未能进入跑步状态", flush=True)
    return None


def ensure_adb():
    """确保 adb 已连上目标实例；断开则自动重连（MuMu 重启后常见）。"""
    try:
        subprocess.run([ADB, "start-server"], capture_output=True, timeout=25)
    except Exception:
        pass
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=20).stdout
    for line in out.splitlines():
        if line.startswith(SERIAL) and "device" in line and "offline" not in line:
            return True
    print(f"  adb 未连接，重连 {SERIAL} …", flush=True)
    try:
        subprocess.run([ADB, "connect", SERIAL], capture_output=True, timeout=20, text=True)
    except Exception:
        pass
    time.sleep(1.5)
    out = subprocess.run([ADB, "devices"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=20).stdout
    return any(line.startswith(SERIAL) and "device" in line and "offline" not in line
               for line in out.splitlines())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pace", type=float, default=6.0)
    ap.add_argument("--km", type=float, default=2.05)
    ap.add_argument("--cps", type=int, default=2)
    ap.add_argument("--no-run", action="store_true", help="只走到跑步页，不接管跑动")
    args = ap.parse_args()

    disc = discover_mumu()
    print("=" * 62)
    print(" 步道乐跑 一键开跑")
    print("=" * 62)
    print(f"  MuMu: {disc.get('root')}   实例 #{disc.get('vm_index')}   网关 {disc.get('gateway_port')}")

    # 前置检查
    if not ensure_adb():
        print(f"  !! 无法连接 {SERIAL}")
        print("     请确认：1) MuMu 已启动   2) 设置→其他→ADB 调试 已开启")
        print("     若刚重启模拟器，等 10~20 秒再试")
        return 1
    pid = subprocess.run([ADB, "-s", SERIAL, "shell", "pidof com.lptiyu.tanke"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=15).stdout.strip()
    if not pid:
        print("  !! 步道乐跑未运行，请先在模拟器里打开 App")
        return 1
    print(f"  App 进程 PID {pid}")

    c = MuMuLocationClient()
    c.set_wgs84(LAT, LON)
    print(f"  定位已下发 ({LAT}, {LON})")

    reader = ScreenReader(adb_path=ADB, serial=SERIAL)

    state = goto_running(reader, c)
    if state is None:
        return 2
    if args.no_run:
        print("  --no-run：已停在跑步页，不接管跑动")
        return 0

    print("[2/2] 启动闭环跑动…", flush=True)
    cmd = [sys.executable, "-u", "campus_run.py",
           "--pace", str(args.pace), "--km", str(args.km), "--cps", str(args.cps),
           "--auto-finish", "--vm-index", "0",
           "--start-lat", str(LAT), "--start-lon", str(LON)]
    return subprocess.call(cmd, cwd=HERE)


if __name__ == "__main__":
    sys.exit(main())
