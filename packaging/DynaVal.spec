# -*- mode: python ; coding: utf-8 -*-
"""Native onedir builds, derived from NiceGUI's nicegui-pack wrapper."""

import sys
from pathlib import Path

from PyInstaller.compat import is_darwin, is_win
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT / "packaging"))
from packaging_support import prepare_packaging_assets, project_metadata

if not (is_darwin or is_win):
    raise RuntimeError("DynaVal release builds target macOS and Windows; Linux is deferred.")

metadata = project_metadata(ROOT)
generated = prepare_packaging_assets(ROOT)

# Same NiceGUI resources as nicegui-pack, without copying Python sources/tests
# already handled by module analysis. Explicit local CSS/Vue paths work frozen.
datas = collect_data_files("nicegui", excludes=["testing/**", "tests/**", "**/__pycache__/**"])
datas += collect_data_files("webview", subdir="js")
datas += [
    (str(ROOT / "src/dynaval/ui/styles.css"), "dynaval/ui"),
    (str(ROOT / "src/dynaval/ui/components/image_viewer.js"), "dynaval/ui/components"),
    (str(generated / "licenses"), "licenses"),
]
hiddenimports = collect_submodules("dynaval") + [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
]
hiddenimports += ["webview.platforms.cocoa"] if is_darwin else [
    "webview.platforms.winforms", "webview.platforms.edgechromium", "clr", "pythonnet",
]

a = Analysis(
    [str(ROOT / "main.py")], pathex=[str(ROOT / "src")],
    binaries=[], datas=datas, hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["pytest", "mypy", "ruff", "nicegui.testing", "webview.platforms.gtk",
              "webview.platforms.qt", "webview.platforms.cef", "webview.platforms.android"],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="DynaVal",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=str(ROOT / "packaging/icons" / ("dynaval.icns" if is_darwin else "dynaval.ico")),
    version=str(generated / "windows-version.txt") if is_win else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DynaVal")
if is_darwin:
    app = BUNDLE(
        coll, name="DynaVal.app", icon=str(ROOT / "packaging/icons/dynaval.icns"),
        bundle_identifier="org.dynaval.desktop", version=metadata["version"],
        info_plist={"CFBundleShortVersionString": metadata["version"],
                    "NSHighResolutionCapable": True},
    )
