"""The same transient native prompt is used by the operators and live-site QA.

2.0 keeps the 1.5 API (``VerificationDialog(parent, phone, message)`` / ``take_code()``) and adds the
operator label, the ``kind`` ("sms" | "apple-2fa"), six-digit auto-submit and a validity countdown;
the implementation lives in ``duo.ui.dialogs``.
"""
from duo.ui.dialogs import CodeInput, LoginDialog, VerificationDialog

__all__ = ["CodeInput", "LoginDialog", "VerificationDialog"]
