#!/usr/bin/env python3
"""Build an offline-only .app using this Mac's existing runtime and model.

No dependency/model downloads, research data, or provider credentials are bundled.
Run from app/: python packaging/build_offline_app.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]


def build(destination, python, model):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("The offline app currently requires macOS on Apple Silicon.")
    sys.path.insert(0, str(APP))
    from karospace_agent.offline import model_path, runtime_path

    runtime = runtime_path(python)
    selected_model = model_path(model)
    # Inspect only installed runtime metadata, never load a dataset or model.
    check = subprocess.run([str(runtime), "-I", "-B", "-c",
        "import importlib.util,json,sys; "
        "required=('mlx.core','mlx_lm','jsonschema','anndata'); "
        "missing=[name for name in required if importlib.util.find_spec(name) is None]; "
        "print(json.dumps({'base_bin':sys.base_prefix+'/bin','missing':missing}))"],
        check=True, capture_output=True, text=True)
    metadata = json.loads(check.stdout)
    if metadata["missing"]:
        raise RuntimeError("Missing offline dependencies: " + ", ".join(metadata["missing"]))
    destination = Path(destination).absolute()
    if destination.suffix != ".app" or destination.exists():
        raise ValueError("Choose a new .app destination; existing apps are never overwritten.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="karo-offline-build-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        bundle = temporary / destination.name
        contents = bundle / "Contents"
        executable = contents / "MacOS/KaroSpaceOffline"
        resources = contents / "Resources"
        executable.parent.mkdir(parents=True)
        resources.mkdir()
        config = {"source": str(APP), "python": str(runtime), "model": str(selected_model),
                  "base_bin": metadata["base_bin"]}
        (resources / "offline-config.json").write_text(json.dumps(config, indent=2) + "\n")
        shutil.copyfile(APP / "packaging/offline_launch.py", resources / "offline_launch.py")
        info = {"CFBundleExecutable": executable.name,
                "CFBundleIdentifier": "se.karospace.agent.offline",
                "CFBundleName": "KaroSpace Offline", "CFBundleDisplayName": "KaroSpace Offline",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.1.0",
                "CFBundleVersion": "1", "LSMinimumSystemVersion": "13.0",
                "NSHighResolutionCapable": True,
                "NSHumanReadableCopyright": "KaroSpace — local offline launcher"}
        # Generate the icon from the repository's UI asset, not external files.
        iconset = temporary / "Offline.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                target = iconset / f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["/usr/bin/sips", "-z", str(size * scale), str(size * scale),
                                str(APP / "karospace_agent/static/appicon.png"), "--out", str(target)],
                               check=True, capture_output=True)
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Offline.icns")], check=True)
        info["CFBundleIconFile"] = "Offline.icns"
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        ui = resources / "Offline UI.app"
        ui_resources = ui / "Contents/Resources"
        ui_binary = ui / "Contents/MacOS/OfflineUI"
        ui_resources.mkdir(parents=True)
        ui_binary.parent.mkdir()
        ui_info = dict(info, CFBundleExecutable="OfflineUI", CFBundleIdentifier="se.karospace.agent.offline.ui")
        (ui / "Contents/Info.plist").write_bytes(plistlib.dumps(ui_info))
        shutil.copyfile(resources / "Offline.icns", ui_resources / "Offline.icns")
        shutil.copyfile(APP / "karospace_agent/static/index.html", ui_resources / "index.html")
        shutil.copyfile(APP / "packaging/offline-bridge.js", ui_resources / "offline-bridge.js")
        ui_entitlements = temporary / "ui-entitlements.plist"
        ui_entitlements.write_bytes(plistlib.dumps({"com.apple.security.app-sandbox": True}))
        subprocess.run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos13.0",
                        "-module-cache-path", str(temporary / "swift-cache"),
                        str(APP / "packaging/OfflineLocalWebView.swift"), "-o", str(ui_binary)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements", str(ui_entitlements), str(ui)], check=True)
        subprocess.run(["xcrun", "swiftc", "-O", "-target", "arm64-apple-macos13.0",
                        "-module-cache-path", str(temporary / "swift-cache"),
                        str(APP / "packaging/OfflineCoordinator.swift"), "-lsandbox", "-o", str(executable)], check=True)
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
        bundle.rename(destination)
    print(f"Built: {destination}")
    print("Uses this Mac's existing source checkout, Python runtime and local model.")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(APP / "dist/KaroSpace Offline.app"))
    parser.add_argument("--python", help="Existing offline Python runtime")
    parser.add_argument("--model", help="Already-downloaded MLX model directory")
    args = parser.parse_args()
    build(args.output, args.python, args.model)


if __name__ == "__main__":
    main()
