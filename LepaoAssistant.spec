from PyInstaller.utils.hooks import collect_data_files

analysis = Analysis(
    ["app.py"],
    datas=[("LICENSE", ".")] + collect_data_files("rapidocr_onnxruntime"),
    hiddenimports=["check_env", "check_track"],
)
exe = EXE(
    PYZ(analysis.pure),
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="步道乐跑助手",
    console=True,
    hide_console="hide-early",
    upx=False,
)
