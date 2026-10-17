"""Launch profiles for the bundled Chromium that do not advertise automation.

Two modes (brief §8.1):

* ``headless`` – Chromium's new headless mode without the ``--enable-automation``
  switch, with ``AutomationControlled`` disabled, a ``Chrome/`` user agent instead
  of ``HeadlessChrome/`` and a small init script that fills the few properties
  headless Chromium leaves empty. Nothing is faked beyond that; inconsistent
  fingerprints are worse than honest ones.
* ``hidden`` – a real headed Chromium window placed far off-screen and hidden with
  Win32 (no taskbar button, no Alt-Tab entry) plus anti-throttling switches so the
  page keeps running at full speed. The user never sees a browser window.

This module only prepares launch keyword arguments and hides windows; it never
touches the user's own browsers (only windows owned by the bundled chrome.exe).
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

from .runtime import configure_runtime

MODES = ("headless", "hidden")
POLICIES = ("auto", "headless", "hidden")
WINDOW_SIZE = (1440, 1000)
OFFSCREEN = (-32000, -32000)
ONSCREEN = (48, 36)
# Brief §14.1 mandates ShowWindow(SW_HIDE) on every top-level window of the pid
# (10-12 live check: the page keeps running; only screenshots get slower, 1–2 s).
# "offscreen" (tool window at -32000) and "cloak" (DWMWA_CLOAK) remain as options.
HIDE_STRATEGY = "hide"
DWMWA_CLOAK, DWMWA_CLOAKED = 13, 14
BLOCK_STATUSES = (403, 541)
ACCESS_DENIED = re.compile(r"access denied|访问被拒绝|验证你是人类|人机验证|请完成安全验证", re.I)

# Applied before any page script. Each patch only runs when the real value is
# the headless give-away; in hidden-headed mode the script is a no-op.
INIT_SCRIPT = r"""(() => {
  try {
    if (navigator.webdriver) {
      Object.defineProperty(Navigator.prototype, 'webdriver', {get: () => false, configurable: true});
    }
  } catch (e) {}
  try {
    const languages = navigator.languages || [];
    if (!languages.length || languages[0] !== 'zh-CN') {
      Object.defineProperty(Navigator.prototype, 'languages', {get: () => ['zh-CN', 'zh'], configurable: true});
    }
  } catch (e) {}
  try {
    if (!window.chrome) {
      window.chrome = {runtime: {}, loadTimes() { return {}; }, csi() { return {}; }, app: {isInstalled: false}};
    }
  } catch (e) {}
  try {
    if (navigator.plugins && navigator.plugins.length === 0) {
      const mime = {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'};
      const names = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer'];
      const plugins = names.map(name => {
        const plugin = {name, filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1, 0: mime,
                        item(i) { return this[i]; }, namedItem() { return mime; }};
        return plugin;
      });
      const list = {length: plugins.length, item(i) { return plugins[i]; }, namedItem(n) { return plugins.find(p => p.name === n) || null; },
                    refresh() {}, [Symbol.iterator]: function* () { for (const p of plugins) yield p; }};
      plugins.forEach((plugin, i) => { list[i] = plugin; });
      Object.defineProperty(Navigator.prototype, 'plugins', {get: () => list, configurable: true});
    }
  } catch (e) {}
})();"""


def chromium_executable() -> Path:
    runtime = configure_runtime()
    return next(runtime.glob("chromium-*/chrome-win64/chrome.exe"))


def file_version(path: Path | str) -> str:
    """Read the PE version resource (``153.0.8010.12``) without starting a process."""
    if os.name != "nt":
        return ""
    try:
        version = ctypes.windll.version
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, data):
            return ""
        pointer, length = ctypes.c_void_p(), wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return ""

        class FixedFileInfo(ctypes.Structure):
            _fields_ = [("signature", wintypes.DWORD), ("struct_version", wintypes.DWORD),
                        ("file_version_ms", wintypes.DWORD), ("file_version_ls", wintypes.DWORD)]

        info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
        return "%d.%d.%d.%d" % (info.file_version_ms >> 16, info.file_version_ms & 0xFFFF,
                                info.file_version_ls >> 16, info.file_version_ls & 0xFFFF)
    except (OSError, AttributeError, ValueError):
        return ""


def rewrite_user_agent(user_agent: str) -> str:
    """``HeadlessChrome/153.0.0.0`` → ``Chrome/153.0.0.0``; anything else is returned unchanged."""
    return user_agent.replace("HeadlessChrome/", "Chrome/")


def user_agent_for(version: str) -> str:
    """Chrome's reduced user agent string for a major version (minor parts are always 0)."""
    major = re.match(r"\d+", version or "")
    if not major:
        raise ValueError("无法确定内置 Chromium 版本。")
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major[0]}.0.0.0 Safari/537.36")


def detect_user_agent(executable: Path | None = None) -> str:
    return user_agent_for(file_version(executable or chromium_executable()))


def screen_size() -> tuple[int, int]:
    try:
        user32 = ctypes.windll.user32
        width, height = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if width >= 800 and height >= 600:
            return width, height
    except (OSError, AttributeError):
        pass
    return 1920, 1080


def resolve_mode(policy: str, downgraded: bool = False) -> str:
    """Map the user's policy (auto/headless/hidden) to a concrete launch mode."""
    if policy not in POLICIES:
        raise ValueError("未知的浏览器模式：" + str(policy))
    if policy == "auto":
        return "hidden" if downgraded else "headless"
    return policy


def launch_kwargs(mode: str, user_agent: str | None = None) -> dict:
    """Keyword arguments for ``chromium.launch_persistent_context`` (profile dir is passed separately)."""
    if mode not in MODES:
        raise ValueError("未知的浏览器模式：" + str(mode))
    width, height = WINDOW_SIZE
    common = dict(channel="chromium", locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False,
                  timeout=30000, ignore_default_args=["--enable-automation", "--hide-scrollbars"])
    args = ["--disable-blink-features=AutomationControlled", "--lang=zh-CN", "--no-first-run",
            "--no-default-browser-check", f"--window-size={width},{height}"]
    if mode == "headless":
        agent = user_agent or detect_user_agent()
        if "HeadlessChrome/" in agent:
            agent = rewrite_user_agent(agent)
        args.append("--user-agent=" + agent)
        screen_w, screen_h = screen_size()
        return dict(common, headless=True, args=args, viewport={"width": width, "height": height},
                    screen={"width": screen_w, "height": screen_h})
    position = ONSCREEN if HIDE_STRATEGY == "cloak" else OFFSCREEN
    args += [f"--window-position={position[0]},{position[1]}", "--disable-renderer-backgrounding",
             "--disable-backgrounding-occluded-windows", "--disable-background-timer-throttling"]
    return dict(common, headless=False, args=args, no_viewport=True)


def legacy_launch_kwargs() -> dict:
    """The 1.5 options, kept only so the live check can measure the difference."""
    from .runtime import launch_options
    return launch_options()


async def apply_stealth(context, mode: str) -> None:
    await context.add_init_script(INIT_SCRIPT)
    if mode == "hidden":
        hide_browser_windows()


def is_blocked_status(status: int) -> bool:
    return status in BLOCK_STATUSES


def looks_blocked(text: str) -> bool:
    return bool(ACCESS_DENIED.search(text or ""))


# ---------------------------------------------------------------- Win32 window hiding

SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x00000080, 0x00040000
HWND_BOTTOM = 1
SWP_NOSIZE, SWP_NOACTIVATE, SWP_NOZORDER = 0x0001, 0x0010, 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _process_image(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(2048)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def browser_windows(executable: Path | None = None, pids: set[int] | None = None) -> list[dict]:
    """Top-level windows of the bundled chrome.exe: [{hwnd, pid, visible, rect, class}]."""
    if os.name != "nt":
        return []
    user32 = ctypes.windll.user32
    target = str(executable or chromium_executable()).lower()
    found = []
    images: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        if not buffer.value.startswith("Chrome_WidgetWin"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pids is not None and pid.value not in pids:
            return True
        if pid.value not in images:
            images[pid.value] = _process_image(pid.value).lower()
        if images[pid.value] != target:
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append({"hwnd": int(hwnd), "pid": int(pid.value), "visible": bool(user32.IsWindowVisible(hwnd)),
                      "rect": (rect.left, rect.top, rect.right, rect.bottom), "class": buffer.value,
                      "cloaked": is_cloaked(hwnd),
                      "toolwindow": bool(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)})
        return True

    user32.EnumWindows(visit, 0)
    return found


def hide_browser_windows(executable: Path | None = None, pids: set[int] | None = None, strategy: str | None = None) -> int:
    """Hide every visible bundled-Chromium window. Returns how many windows were changed.

    ``strategy="hide"``: ``ShowWindow(SW_HIDE)`` (brief §8.1).
    ``strategy="offscreen"``: keep the window shown but off-screen as a tool window
    (no taskbar button / Alt-Tab) so the page keeps rendering normally.
    """
    if os.name != "nt":
        return 0
    strategy = strategy or HIDE_STRATEGY
    user32 = ctypes.windll.user32
    changed = 0
    for window in browser_windows(executable, pids):
        hwnd = wintypes.HWND(window["hwnd"])
        if strategy == "hide":
            if window["visible"]:
                user32.ShowWindow(hwnd, SW_HIDE)
                changed += 1
            continue
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        wanted = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        left, top = window["rect"][0], window["rect"][1]
        if strategy == "cloak":
            if style != wanted or not window["visible"] or not window.get("cloaked"):
                user32.ShowWindow(hwnd, SW_HIDE)
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, wanted)
                user32.SetWindowPos(hwnd, HWND_BOTTOM, ONSCREEN[0], ONSCREEN[1], 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                cloak_window(hwnd, True)
                changed += 1
            continue
        if style != wanted or (left, top) != OFFSCREEN or not window["visible"]:
            user32.ShowWindow(hwnd, SW_HIDE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, wanted)
            user32.SetWindowPos(hwnd, HWND_BOTTOM, OFFSCREEN[0], OFFSCREEN[1], 0, 0, SWP_NOSIZE | SWP_NOACTIVATE)
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            changed += 1
    return changed


def show_browser_windows(executable: Path | None = None, pids: set[int] | None = None) -> int:
    """§13.2 user-requested reveal: un-cloak, restore the normal frame, show on-screen without stealing focus."""
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    shown = 0
    for window in browser_windows(executable, pids):
        if window["class"] != "Chrome_WidgetWin_1":
            continue
        hwnd = wintypes.HWND(window["hwnd"])
        cloak_window(hwnd, False)
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW)
        user32.SetWindowPos(hwnd, 0, ONSCREEN[0], ONSCREEN[1], 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOZORDER)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        shown += 1
    return shown


def as_hwnd(hwnd) -> "wintypes.HWND":
    """Accept an int or an existing HWND: HWND(HWND(...)) raises "cannot be converted to pointer" (live 10-15)."""
    if isinstance(hwnd, wintypes.HWND):
        return hwnd
    return wintypes.HWND(int(hwnd or 0))


def cloak_window(hwnd, enabled: bool = True) -> bool:
    """DWM cloaking: the window stays "visible" to Chromium (keeps rendering) but is never shown."""
    try:
        value = wintypes.BOOL(1 if enabled else 0)
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(as_hwnd(hwnd), DWMWA_CLOAK, ctypes.byref(value), ctypes.sizeof(value))
        return result == 0
    except (OSError, AttributeError, TypeError, ValueError):
        return False


def is_cloaked(hwnd) -> bool:
    try:
        value = wintypes.DWORD(0)
        ctypes.windll.dwmapi.DwmGetWindowAttribute(as_hwnd(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
        return bool(value.value)
    except (OSError, AttributeError, TypeError, ValueError):
        return False


class WindowHider:
    """Hides bundled-Chromium windows the moment they appear (daemon thread)."""

    def __init__(self, executable: Path | None = None, strategy: str | None = None, interval: float = 0.02):
        self.executable = executable
        self.strategy = strategy or HIDE_STRATEGY
        self.interval = interval
        self.hidden = 0
        self._stop = threading.Event()
        self._thread = None

    def start(self, seconds: float = 15.0):
        self._stop.clear()
        deadline = time.monotonic() + seconds

        def loop():
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    self.hidden += hide_browser_windows(self.executable, strategy=self.strategy)
                except Exception:
                    pass
                time.sleep(self.interval)

        self._thread = threading.Thread(target=loop, name="duo-window-hider", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)


def describe(mode: str) -> str:
    return {"headless": "无头（隐身）", "hidden": "隐藏窗口"}.get(mode, mode)
