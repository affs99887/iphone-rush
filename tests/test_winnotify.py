"""Windows code notification helper: XML, script construction, event parsing (no real toast is shown)."""
import base64
import io
import json
import os
import sys
import unittest
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".vendor"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication

from duo import winnotify


def pump(times=10):
    for _ in range(times):
        QCoreApplication.processEvents()


class FakeProc:
    def __init__(self, lines):
        self.stdout = io.BytesIO("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines).encode("utf-8"))
        self.args = None

    def poll(self):
        return None


class WinNotifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_toast_xml_is_interactive_and_escaped(self):
        xml = winnotify.toast_xml("门店自取 · Apple 短信验证码", "输入尾号 <0000> 的 6 位验证码 & 回车")
        doc = minidom.parseString(xml)
        toast = doc.getElementsByTagName("toast")[0]
        self.assertEqual(toast.getAttribute("scenario"), "reminder", "stays on screen until handled")
        inp = doc.getElementsByTagName("input")[0]
        self.assertEqual((inp.getAttribute("id"), inp.getAttribute("type")), ("code", "text"))
        self.assertIn("6 位", inp.getAttribute("placeHolderContent"))
        self.assertNotIn("回车", inp.getAttribute("placeHolderContent"), "Enter does not submit in a toast box")
        submit = doc.getElementsByTagName("action")[0]
        self.assertEqual((submit.getAttribute("content"), submit.getAttribute("hint-inputId")), ("提交验证码", "code"))
        self.assertEqual(submit.getAttribute("imageUri"), "", "plain text button, no icon")
        warned = minidom.parseString(winnotify.toast_xml("t", "b", warning="需要 6 位数字"))
        self.assertEqual([t.firstChild.data for t in warned.getElementsByTagName("text")][:3], ["t", "需要 6 位数字", "b"])
        texts = [t.firstChild.data for t in doc.getElementsByTagName("text")]
        self.assertIn("门店自取 · Apple 短信验证码", texts)
        self.assertIn("输入尾号 <0000> 的 6 位验证码 & 回车", texts)

    def test_script_embeds_values_safely_and_is_encoded(self):
        xml = winnotify.toast_xml("送货到家 · O'Neil", "x")
        self.assertIn("O&apos;Neil", xml, "XML-escaped before it reaches PowerShell")
        script = winnotify.build_script("<toast>it's</toast>", "delivery-3", r"C:\Temp\it's\d.cancel")
        self.assertIn("$Xml = '<toast>it''s</toast>'", script, "single quotes doubled inside a PowerShell literal")
        self.assertIn(r"C:\Temp\it''s\d.cancel", script)
        self.assertIn("$Tag = 'delivery-3'", script)
        self.assertNotIn("__XML__", script)
        decoded = base64.b64decode(winnotify.encoded_command(script)).decode("utf-16-le")
        self.assertEqual(decoded, script)
        with self.assertRaises(ValueError):
            winnotify.build_script(xml, "bad tag; rm -rf", "c")

    def test_helper_events_emit_the_code_once_and_never_keep_it(self):
        procs = []

        def popen(args, **kwargs):
            proc = FakeProc([{"event": "shown", "setting": "Enabled", "delivered": 1, "app_id": "x"},
                             {"event": "activated", "arguments": "submit", "code": " 482913 "}])
            proc.args = args
            procs.append(proc)
            return proc

        toast = winnotify.WindowsCodeToast(popen=popen)
        codes, seen = [], []
        toast.code_entered.connect(lambda key, code: codes.append((key, code)))
        toast.helper_event.connect(lambda key, data: seen.append(data))
        self.assertTrue(toast.show_code_request("pickup-1", "门店自取 · Apple 短信验证码", "输入验证码"))
        for _ in range(50):
            pump()
            if codes:
                break
        self.assertEqual(codes, [("pickup-1", "482913")])
        self.assertTrue(all("code" not in d for d in seen), "the code is not forwarded in diagnostics")
        self.assertNotIn("482913", json.dumps(toast.last, ensure_ascii=False))
        self.assertIn("-EncodedCommand", procs[0].args)
        self.assertNotIn("482913", " ".join(procs[0].args))

    def test_wrong_length_is_never_submitted_and_the_notification_comes_back_with_a_warning(self):
        launches = []
        replies = [[{"event": "activated", "arguments": "submit", "code": "5625759"}],     # 7 digits (live 10-14)
                   [{"event": "activated", "arguments": "submit", "code": "12ab"}],
                   [{"event": "activated", "arguments": "submit", "code": "５２５ ７５９"}]]  # full-width + space

        def popen(args, **kwargs):
            launches.append(base64.b64decode(args[-1]).decode("utf-16-le"))
            return FakeProc(replies[len(launches) - 1] if len(launches) <= len(replies) else [])

        toast = winnotify.WindowsCodeToast(popen=popen)
        codes, seen = [], []
        toast.code_entered.connect(lambda key, code: codes.append(code))
        toast.helper_event.connect(lambda key, data: seen.append(data.get("event")))
        toast.show_code_request("delivery-1", "送货到家 · Apple 短信验证码", "输入尾号 0000 收到的 6 位验证码，然后点“提交验证码”。")
        for _ in range(200):
            pump()
            if codes:
                break
        self.assertEqual(seen[:2], ["invalid-code", "invalid-code"])
        self.assertEqual(codes, ["525759"], "only the six-digit entry is submitted")
        self.assertEqual(len(launches), 3, "the notification was shown again after each wrong entry")
        self.assertNotIn("需要 6 位数字", launches[0])
        self.assertIn("需要 6 位数字", launches[1])
        self.assertIn("提交验证码", launches[0])
        self.assertNotIn("imageUri", launches[0])
        self.assertNotIn("5625759", "".join(launches))

    def test_re_show_stops_after_the_retry_limit_or_when_withdrawn(self):
        launches = []
        toast = winnotify.WindowsCodeToast(popen=lambda args, **kw: (launches.append(1), FakeProc(
            [{"event": "activated", "arguments": "submit", "code": "1"}]))[1])
        toast.show_code_request("pickup-9", "t", "b")
        for _ in range(400):
            pump()
        self.assertEqual(len(launches), 1 + winnotify.MAX_RETRIES)
        withdrawn = []
        toast2 = winnotify.WindowsCodeToast(popen=lambda args, **kw: (withdrawn.append(1), FakeProc([]))[1])
        toast2.show_code_request("pickup-10", "t", "b")
        toast2.cancel("pickup-10")
        toast2._handle_line("pickup-10", json.dumps({"event": "activated", "arguments": "submit", "code": "12"}))
        self.assertEqual(len(withdrawn), 1, "a withdrawn request is not shown again")

    def test_codes_are_normalized_before_the_six_digit_check(self):
        self.assertEqual(winnotify.normalize_code(" 482-913 "), "482913")
        self.assertEqual(winnotify.normalize_code("４８２９１３"), "482913")
        self.assertEqual(winnotify.normalize_code("48291a"), "48291a")

    def test_body_click_requests_the_app_and_cancel_writes_a_marker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            toast = winnotify.WindowsCodeToast(popen=lambda args, **kw: FakeProc([{"event": "activated", "arguments": "open", "code": ""}]),
                                               folder=Path(tmp))
            opened = []
            toast.open_requested.connect(opened.append)
            toast.show_code_request("pickup-2", "t", "b")
            for _ in range(50):
                pump()
                if opened:
                    break
            self.assertEqual(opened, ["pickup-2"])
            toast.show_code_request("pickup-3", "t", "b")
            toast.cancel("pickup-3")
            self.assertTrue((Path(tmp) / "pickup-3.cancel").exists())

    def test_app_identity_is_registered_under_hkcu(self):
        calls = []

        class FakeReg:
            HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

            def CreateKeyEx(self, root, path, reserved, access):
                calls.append(("key", root, path))
                return "k"

            def SetValueEx(self, key, name, reserved, kind, value):
                calls.append(("value", name, value))

            def CloseKey(self, key):
                calls.append(("close",))

        self.assertTrue(winnotify.register_app_identity("C:/icon.png", winreg=FakeReg()))
        self.assertEqual(calls[0], ("key", "HKCU", "Software\\Classes\\AppUserModelId\\Duo.Reservation.Desktop.2"))
        self.assertIn(("value", "DisplayName", "iPhone Duo 预约助手"), calls)
        self.assertIn(("value", "IconUri", "C:/icon.png"), calls)


if __name__ == "__main__":
    unittest.main()
