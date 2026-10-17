"""Windows notifications with a code input box ("输入验证码" straight from the Windows notification).

The app has no WinRT bindings, so each request runs a short-lived, windowless Windows PowerShell 5.1
helper (always present on Windows 10/11) that shows an interactive toast, waits for the user's input
and prints one JSON line per event on stdout. The typed code only ever travels through that pipe:
it is never put on a command line, in a file or in a log.

Events printed by the helper: shown {setting, delivered, app_id}, activated {arguments, code},
dismissed {reason}, failed {error}, cancelled, timeout, error {message}.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from xml.sax.saxutils import escape

from PySide6.QtCore import QObject, Qt, Signal

APP_ID = "Duo.Reservation.Desktop.2"            # same id app.py sets with SetCurrentProcessExplicitAppUserModelID
APP_NAME = "iPhone Duo 预约助手"
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
TIMEOUT_S = 900
MAX_RETRIES = 5        # wrong-length entries in a row before the notification stops re-appearing
CREATE_NO_WINDOW = 0x08000000

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress)); [Console]::Out.Flush() }
$AppId = '__APPID__'; $Fallback = '__FALLBACK__'; $Tag = '__TAG__'; $Group = 'duo-code'
$Cancel = '__CANCEL__'; $Timeout = __TIMEOUT__; $Cache = '__CACHE__'
$Xml = '__XML__'
# Windows PowerShell cannot subscribe to WinRT events (Register-ObjectEvent), so a tiny C# listener is compiled
# once against the system WinMetadata with the in-box .NET Framework compiler and cached by source hash.
$Source = @'
using System;
using System.Collections.Concurrent;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;
public static class DuoToast {
  public static readonly BlockingCollection<string[]> Events = new BlockingCollection<string[]>();
  public static ToastNotification Current;
  public static string Show(string appId, string xml, string tag, string group, int seconds) {
    var doc = new XmlDocument();
    doc.LoadXml(xml);
    var toast = new ToastNotification(doc);
    toast.Tag = tag; toast.Group = group;
    toast.ExpirationTime = DateTimeOffset.Now.AddSeconds(seconds);
    toast.Activated += (s, o) => {
      var a = o as ToastActivatedEventArgs;
      string code = "";
      try { object v; if (a != null && a.UserInput != null && a.UserInput.TryGetValue("code", out v) && v != null) code = v.ToString(); } catch { }
      Events.Add(new[] { "activated", a == null ? "" : a.Arguments, code });
    };
    toast.Dismissed += (s, e) => Events.Add(new[] { "dismissed", e.Reason.ToString(), "" });
    toast.Failed += (s, e) => Events.Add(new[] { "failed", e.ErrorCode == null ? "" : e.ErrorCode.Message, "" });
    var notifier = ToastNotificationManager.CreateToastNotifier(appId);
    Current = toast;
    notifier.Show(toast);
    return notifier.Setting.ToString();
  }
  public static int Delivered(string appId, string tag) {
    int n = 0;
    foreach (var t in ToastNotificationManager.History.GetHistory(appId)) { if (t.Tag == tag) n++; }
    return n;
  }
  public static void Remove(string appId, string tag, string group) {
    try { ToastNotificationManager.History.Remove(tag, group, appId); } catch { }
  }
}
'@
function Load-Listener {
  $hash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Source))).Replace('-', '').Substring(0, 16)
  $dll = Join-Path $Cache ("DuoToast-" + $hash + ".dll")
  if (-not (Test-Path -LiteralPath $dll)) {
    New-Item -ItemType Directory -Force -Path $Cache | Out-Null
    $meta = Join-Path $env:WINDIR 'System32\WinMetadata'
    $fw = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
    $facades = Join-Path $fw 'WPF'
    $cs = Join-Path $Cache ("DuoToast-" + $hash + ".cs")
    [IO.File]::WriteAllText($cs, $Source, (New-Object Text.UTF8Encoding $false))
    $tmp = $dll + '.' + $PID + '.tmp'
    $refs = @('Windows.Foundation.winmd', 'Windows.UI.winmd', 'Windows.Data.winmd') | ForEach-Object { '/r:' + (Join-Path $meta $_) }
    $refs += @('System.Runtime.WindowsRuntime.dll', 'System.Runtime.dll', 'System.Runtime.InteropServices.WindowsRuntime.dll',
               'System.dll', 'System.Core.dll') | ForEach-Object { '/r:' + (Join-Path $fw $_) }
    $out = & (Join-Path $fw 'csc.exe') /nologo /target:library /optimize+ ('/out:' + $tmp) $refs $cs 2>&1
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $tmp)) { throw ('toast listener build failed: ' + ($out -join ' ')) }
    Move-Item -LiteralPath $tmp -Destination $dll -Force
    Remove-Item -LiteralPath $cs -ErrorAction SilentlyContinue
  }
  Add-Type -Path $dll
}
try {
  Load-Listener
  $setting = [DuoToast]::Show($AppId, $Xml, $Tag, $Group, $Timeout)
  Start-Sleep -Milliseconds 600
  $delivered = [DuoToast]::Delivered($AppId, $Tag)
  if ($delivered -eq 0 -and $Fallback) {
    [DuoToast]::Remove($AppId, $Tag, $Group)
    $AppId = $Fallback
    $setting = [DuoToast]::Show($AppId, $Xml, $Tag, $Group, $Timeout)
    Start-Sleep -Milliseconds 600
    $delivered = [DuoToast]::Delivered($AppId, $Tag)
  }
  Say @{ event = 'shown'; setting = $setting; delivered = $delivered; app_id = $AppId }
  $deadline = [DateTime]::Now.AddSeconds($Timeout)
  $done = $false
  while (-not $done -and [DateTime]::Now -lt $deadline) {
    if (Test-Path -LiteralPath $Cancel) {
      [DuoToast]::Remove($AppId, $Tag, $Group)
      Remove-Item -LiteralPath $Cancel -ErrorAction SilentlyContinue
      Say @{ event = 'cancelled' }; $done = $true; break
    }
    $ev = $null
    if (-not [DuoToast]::Events.TryTake([ref]$ev, 500)) { continue }
    switch ($ev[0]) {
      'activated' { Say @{ event = 'activated'; arguments = $ev[1]; code = $ev[2] }; $done = $true }
      'dismissed' { Say @{ event = 'dismissed'; reason = $ev[1] }; if ($ev[1] -ne 'TimedOut') { $done = $true } }
      'failed' { Say @{ event = 'failed'; error = $ev[1] }; $done = $true }
    }
  }
  if (-not $done) { [DuoToast]::Remove($AppId, $Tag, $Group); Say @{ event = 'timeout' } }
} catch { Say @{ event = 'error'; message = $_.Exception.Message } }
"""


def powershell_path() -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def _ps_literal(value: str) -> str:
    """Contents of a single-quoted PowerShell string."""
    return str(value).replace("'", "''")


def normalize_code(raw: str) -> str:
    """Accept what people actually type: full-width digits and spaces/dashes ("482 913", "４８２９１３")."""
    text = (raw or "").translate({ord(c): ord("0") + i for i, c in enumerate("０１２３４５６７８９")})
    return re.sub(r"[\s\-_.·]", "", text)


def toast_xml(title: str, body: str, attribution: str = APP_NAME, warning: str = "") -> str:
    """Interactive reminder toast: stays on screen until the user acts.

    Windows' toast input box cannot limit its length or submit on Enter (live 10-15: Enter inserts a new
    line), so the code is sent with the 提交验证码 button and the six-digit rule is enforced on submit; a
    wrong entry re-shows the toast with ``warning``.
    """
    def text(value: str) -> str:
        return escape(value, {'"': "&quot;", "'": "&apos;"})
    lines = f'<text>{text(title)}</text>'
    lines += f'<text>{text(warning)}</text>' if warning else ""
    lines += f'<text>{text(body)}</text>'
    return (
        '<toast scenario="reminder" launch="open" activationType="foreground">'
        '<visual><binding template="ToastGeneric">'
        f'{lines}<text placement="attribution">{text(attribution)}</text>'
        '</binding></visual>'
        '<actions>'
        '<input id="code" type="text" placeHolderContent="输入 6 位数字验证码"/>'
        '<action content="提交验证码" arguments="submit" activationType="foreground" hint-inputId="code"/>'
        '<action content="打开软件" arguments="open" activationType="foreground"/>'
        '</actions>'
        '<audio src="ms-winsoundevent:Notification.Reminder"/>'
        '</toast>'
    )


def build_script(xml: str, tag: str, cancel_file: str, app_id: str = APP_ID, fallback: str = POWERSHELL_APP_ID,
                 timeout: int = TIMEOUT_S, cache_dir: str = "") -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tag):
        raise ValueError("bad toast tag")
    cache_dir = cache_dir or str(Path(tempfile.gettempdir()) / "duo-toasts")
    return (SCRIPT.replace("__APPID__", _ps_literal(app_id)).replace("__FALLBACK__", _ps_literal(fallback))
            .replace("__TAG__", tag).replace("__CANCEL__", _ps_literal(cancel_file))
            .replace("__CACHE__", _ps_literal(cache_dir))
            .replace("__TIMEOUT__", str(int(timeout))).replace("__XML__", _ps_literal(xml)))


def encoded_command(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def register_app_identity(icon_path: str = "", app_id: str = APP_ID, display_name: str = APP_NAME, winreg=None) -> bool:
    """Give the app's AppUserModelID a name and icon so Windows labels the notification correctly (HKCU only)."""
    if os.name != "nt" and winreg is None:
        return False
    try:
        if winreg is None:
            import winreg  # noqa: PLC0415
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Software\\Classes\\AppUserModelId\\" + app_id, 0,
                                 winreg.KEY_SET_VALUE)
        try:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            if icon_path:
                winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, icon_path)
        finally:
            winreg.CloseKey(key)
        return True
    except OSError:
        return False


class WindowsCodeToast(QObject):
    """One interactive Windows notification per pending verification code."""

    code_entered = Signal(str, str)     # key, six-digit code
    open_requested = Signal(str)        # key (user clicked the notification or "打开软件")
    helper_event = Signal(str, dict)    # key, helper event without the code (diagnostics)
    _line = Signal(str, str)            # internal: reader thread → GUI thread

    def __init__(self, enabled: bool = True, popen=None, folder: Path | None = None, app_id: str = APP_ID):
        super().__init__()
        self.enabled = bool(enabled) and (popen is not None or (os.name == "nt" and os.path.exists(powershell_path())))
        self.popen = popen or subprocess.Popen
        self.folder = Path(folder or Path(tempfile.gettempdir()) / "duo-toasts")
        self.app_id = app_id
        self.procs: dict[str, object] = {}
        self.last: dict[str, dict] = {}
        self.requests: dict[str, tuple[str, str]] = {}      # key → (title, body), to re-show after a bad entry
        self.retries: dict[str, int] = {}
        self._line.connect(self._handle_line, Qt.ConnectionType.QueuedConnection)

    def available(self) -> bool:
        return self.enabled

    def _cancel_file(self, key: str) -> Path:
        return self.folder / (key + ".cancel")

    def show_code_request(self, key: str, title: str, body: str, warning: str = "") -> bool:
        if not self.enabled:
            return False
        retries = self.retries.get(key, 0) if warning else 0
        self.cancel(key)
        if retries:
            self.retries[key] = retries          # a re-show after a bad entry keeps counting toward MAX_RETRIES
        self.requests[key] = (title, body)
        self.folder.mkdir(parents=True, exist_ok=True)
        cancel = self._cancel_file(key)
        cancel.unlink(missing_ok=True)
        script = build_script(toast_xml(title, body, warning=warning), key, str(cancel), self.app_id,
                              cache_dir=str(self.folder))
        args = [powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden", "-EncodedCommand", encoded_command(script)]
        try:
            proc = self.popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW)
        except OSError:
            return False
        self.procs[key] = proc
        threading.Thread(target=self._read, args=(key, proc), name="duo-toast-" + key, daemon=True).start()
        return True

    def _read(self, key: str, proc) -> None:
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return
        for raw in stream:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            line = line.strip().lstrip("\ufeff")
            if line:
                self._line.emit(key, line)

    def _handle_line(self, key: str, line: str) -> None:
        try:
            data = json.loads(line)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        code = normalize_code(str(data.pop("code", "") or ""))
        typed = bool(code)
        self.last[key] = dict(data)
        if data.get("event") == "activated":
            if re.fullmatch(r"\d{6}", code):
                self.procs.pop(key, None)
                self.code_entered.emit(key, code)
            elif data.get("arguments") == "submit" or typed:
                # Wrong length / not digits: never submit it. Re-show the notification at once with a warning.
                data["event"] = "invalid-code"
                self.procs.pop(key, None)
                self.retries[key] = self.retries.get(key, 0) + 1
                if key in self.requests and self.retries[key] <= MAX_RETRIES:
                    title, body = self.requests[key]
                    self.show_code_request(key, title, body,
                                           warning="⚠ 需要 6 位数字（刚才输入的不是 6 位数字），请重新输入后点“提交验证码”。")
            else:
                self.procs.pop(key, None)
                self.open_requested.emit(key)
        code = ""
        self.helper_event.emit(key, data)

    def cancel(self, key: str) -> None:
        """Withdraw the notification (code entered elsewhere, request superseded); it will not re-appear."""
        self.requests.pop(key, None)
        self.retries.pop(key, None)
        proc = self.procs.pop(key, None)
        if proc is None:
            return
        try:
            if getattr(proc, "poll", lambda: 0)() is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                self._cancel_file(key).write_text("cancel", encoding="utf-8")
        except OSError:
            pass

    def forget(self, key: str) -> None:
        """The helper finished on its own (code entered or dismissed): drop it without a cancel file."""
        self.procs.pop(key, None)
        self.requests.pop(key, None)
        self.retries.pop(key, None)

    def shutdown(self) -> None:
        for key in list(self.procs):
            self.cancel(key)
