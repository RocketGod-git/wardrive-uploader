# PyInstaller spec — build a single windowed WDGWarsUploader.exe
#   pyinstaller wdgwars_uploader.spec
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hiddenimports = [
    "pystray._win32",
    "PIL._tkinter_finder",
]
hiddenimports += collect_submodules("meshcore")
hiddenimports += collect_submodules("serial")
# meshcore's deps: BLE backend (bleak, dynamic winrt import) + Cayenne LPP.
for _dep in ("bleak", "bleak.backends.winrt", "cayennelpp"):
    try:
        hiddenimports += collect_submodules(_dep)
    except Exception:
        pass

datas = collect_data_files("meshcore")
# the dashboard + map HTML served by the local server
datas += [
    ("wdgwars_uploader/webui/dashboard.html", "wdgwars_uploader/webui"),
    ("wdgwars_uploader/webui/map.html", "wdgwars_uploader/webui"),
    ("wdgwars_uploader/webui/static", "wdgwars_uploader/webui/static"),
]

# Optional native-window backend: only bundle if pywebview is installed.
try:
    import webview  # noqa: F401
    hiddenimports += collect_submodules("webview")
    datas += collect_data_files("webview")
except Exception:
    pass

# Meshtastic support: pulls in protobuf + pypubsub + dotmap, which PyInstaller's
# static analysis misses. Only bundle if installed.
try:
    import meshtastic  # noqa: F401
    for _m in ("meshtastic", "google.protobuf", "pubsub", "dotmap", "tabulate"):
        try:
            hiddenimports += collect_submodules(_m)
        except Exception:
            pass
    datas += collect_data_files("meshtastic")
except Exception:
    pass

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["FixTk", "tcl", "tk", "_tkinter.test", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WDGWarsUploader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed: no console window
    disable_windowed_traceback=False,
    icon=None,
)
