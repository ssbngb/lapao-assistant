"""通过查询设备和读取截图检查环境，避免改动模拟器位置或传感器。"""

import argparse
import os
import subprocess
import sys

from lepao.mumu import MuMuGateway
from screen_state import ScreenReader

PACKAGE = "com.lptiyu.tanke"


def _check(label, read):
    try:
        result = read()
    except Exception as exc:
        print(f"  [FAIL] {label}：{exc}")
        return False
    print(f"  [OK]   {label}：{result}")
    return True


def _adb(client, *args, allowed_codes=(0,)):
    result = subprocess.run(
        [client.adb_path, *args], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode not in allowed_codes or (result.returncode and result.stderr.strip()):
        raise RuntimeError(result.stderr.strip() or f"ADB 退出码 {result.returncode}")
    return result.stdout.strip()


def _device(client, serial):
    output = _adb(client, "devices")
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == serial:
            if fields[1] == "device":
                return serial
            raise RuntimeError(f"{serial} 当前状态：{fields[1]}")
    raise RuntimeError(f"{serial} 未连接")


def _app(client, serial):
    pid = _adb(client, "-s", serial, "shell", "pidof", PACKAGE, allowed_codes=(0, 1))
    if not pid:
        raise RuntimeError("应用未运行")
    return f"PID {pid}"


def _version(client, serial):
    dump = _adb(client, "-s", serial, "shell", "dumpsys", "package", PACKAGE)
    for line in dump.splitlines():
        if line.strip().startswith("versionName="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("未读取到应用版本")


def _deps():
    import numpy
    from rapidocr_onnxruntime import RapidOCR

    return f"Python {sys.version.split()[0]} / NumPy {numpy.__version__} / {RapidOCR.__name__}"


def _ocr(client, serial):
    import numpy
    from rapidocr_onnxruntime import RapidOCR

    image = ScreenReader(adb_path=client.adb_path, serial=serial, reconnect=False).shot()
    result, _ = RapidOCR()(numpy.asarray(image))
    if not result:
        raise RuntimeError("截图成功，但未识别到文字")
    return f"{image.width} × {image.height}，识别到 {len(result)} 条文字"


def main(argv=None):
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-index", type=int, help="MuMu 实例编号")
    args = parser.parse_args(argv)
    if args.vm_index is not None:
        os.environ["MUMU_VM_INDEX"] = str(args.vm_index)

    print("步道乐跑环境自检")
    deps_ok = _check("Python 依赖", _deps)
    try:
        client = MuMuGateway()
        if not client.manager_path or not client.adb_path:
            raise RuntimeError("未找到 MuMuManager 或 adb")
        serial = client.adb_serial()
    except Exception as exc:
        print(f"  [FAIL] MuMu 安装与实例：{exc}")
        return 1
    print(f"  [OK]   MuMu 安装与实例：#{client.vm_index} / {client.adb_path}")
    if not _check("ADB 连接", lambda: _device(client, serial)):
        return 1
    results = [
        _check("应用进程", lambda: _app(client, serial)),
        _check("应用版本", lambda: _version(client, serial)),
    ]
    if deps_ok:
        results.append(_check("截图与 OCR", lambda: _ocr(client, serial)))
    return 0 if deps_ok and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
