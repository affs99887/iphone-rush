"""Private, bundled Chromium. Never select an installed user browser."""
from pathlib import Path
import os
import sys


def configure_runtime():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    runtime = root / "browser-runtime"
    if not any(runtime.glob("chromium-*/chrome-win64/chrome.exe")):
        raise RuntimeError("缺少随软件附带的 Chromium，请使用完整的无头版本。")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
    return runtime


def launch_options():
    return dict(channel="chromium", headless=True, viewport={"width": 1440, "height": 1000},
                locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False, timeout=20000)
