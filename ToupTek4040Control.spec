# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH).resolve()
sdk_candidates = (
    root / "toupcamsdk.20250623",
    root.parent.parent / "toupcamsdk.20250623",
)
sdk_root = next(path for path in sdk_candidates if path.is_dir())
sdk_python = sdk_root / "python"
icon = root / "assets" / "touptek4040.ico"

common_pathex = [str(root), str(sdk_python)]
common_datas = [
    (str(root / "touptek4040" / "web"), "touptek4040/web"),
    (str(root / "touptek4040.json"), "."),
    (str(root / "使用说明.txt"), "."),
]
common_binaries = [(str(sdk_root / "win" / "x64" / "toupcam.dll"), ".")]
common_hiddenimports = [
    "toupcam",
    "touptek4040.camera_owner",
    "touptek4040.owner_backend",
    "touptek4040.owner_protocol",
    "touptek4040.shared_frames",
    "multiprocessing",
    "multiprocessing.connection",
    "multiprocessing.popen_spawn_win32",
    "multiprocessing.resource_tracker",
    "multiprocessing.shared_memory",
    "multiprocessing.spawn",
]


def application(entry):
    return Analysis(
        [str(root / entry)],
        pathex=common_pathex,
        binaries=common_binaries,
        datas=common_datas,
        hiddenimports=common_hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )


gui_analysis = application("run_gui.py")
alpaca_analysis = application("run_alpaca.py")
gui_pyz = PYZ(gui_analysis.pure)
alpaca_pyz = PYZ(alpaca_analysis.pure)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ToupTek4040GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=[str(icon)],
    contents_directory="_runtime",
)
alpaca_exe = EXE(
    alpaca_pyz,
    alpaca_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ToupTek4040Alpaca",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=[str(icon)],
    contents_directory="_runtime",
)

collection = COLLECT(
    gui_exe,
    alpaca_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    alpaca_analysis.binaries,
    alpaca_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ToupTek4040Control",
)
