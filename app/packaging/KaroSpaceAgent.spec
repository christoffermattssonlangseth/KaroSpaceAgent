# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for `KaroSpace Agent.app`.

Freezes the Python interpreter + app + starlette/uvicorn/pywebview into a
windowed macOS bundle. Run via `build_app.sh` (which sets the signing identity),
or directly:  pyinstaller packaging/KaroSpaceAgent.spec --noconfirm

Notes:
* Data: the restyled chat page ships as package data and must be collected.
* Hidden imports: uvicorn and pywebview load submodules dynamically, so
  PyInstaller can't see them by static analysis — collect them explicitly.
* Signing: identity/entitlements come from the environment so the same spec
  builds unsigned locally and signed in `build_app.sh` / CI.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = collect_data_files("karospace_agent")  # static/index.html
hiddenimports = []
binaries = []
for pkg in ("uvicorn", "webview", "starlette", "claude_agent_sdk"):
    b, d, h = collect_all(pkg)
    binaries += b
    datas += d
    hiddenimports += h
hiddenimports += collect_submodules("uvicorn")

# Bundle the Rust companion so the shipped .app carries the default spatial-graph
# pre-processor (see packaging/README.md and companion_bin()/_bundled_companion()).
# It is host-arch-specific and must be rebuilt when the companion changes; the
# whole app must then be re-signed + re-notarized. If the binary isn't present we
# build without it — the app still runs and degrades to direct export.
_companion_src = os.environ.get("KAROSPACE_COMPANION_SRC") or os.path.abspath(
    os.path.join("..", "..", "KaroSpaceCompanion", "target", "release", "karospace-companion")
)
if os.path.exists(_companion_src):
    binaries += [(_companion_src, ".")]
    print(f"[spec] bundling companion: {_companion_src}")
else:
    print(f"[spec] companion binary not found at {_companion_src}; building without it")

# Optional signing, supplied by build_app.sh (empty = unsigned local build).
_identity = os.environ.get("KAROSPACE_CODESIGN_IDENTITY") or None
_entitlements = os.environ.get("KAROSPACE_ENTITLEMENTS") or None
_icon = os.environ.get("KAROSPACE_ICNS") or None

a = Analysis(
    ["launch.py"],
    pathex=[os.path.abspath(os.path.join(os.getcwd()))],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide6", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KaroSpace Agent",
    console=False,          # windowed app, no terminal
    disable_windowed_traceback=False,
    argv_emulation=True,    # accept files dropped on the dock icon
    target_arch=None,       # build for the host arch; see build_app.sh for universal2
    codesign_identity=_identity,
    entitlements_file=_entitlements,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="KaroSpace Agent",
)
app = BUNDLE(
    coll,
    name="KaroSpace Agent.app",
    icon=_icon,
    bundle_identifier="se.karospace.agent",
    info_plist={
        "CFBundleName": "KaroSpace Agent",
        "CFBundleDisplayName": "KaroSpace Agent",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # The window loads http://127.0.0.1:<port>; allow local networking so
        # WKWebView's App Transport Security doesn't block the localhost load.
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
