from PyInstaller.utils.hooks import collect_data_files

analysis = Analysis(
    ["app.py"],
    datas=[("LICENSE", "."), ("assets/logo.ico", "assets")] + collect_data_files("rapidocr_onnxruntime"),
    hiddenimports=["check_env", "check_track"],
)
exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="步道乐跑助手",
    icon="assets/logo.ico",
    console=True,
    hide_console="hide-early",
    upx=False,
)
