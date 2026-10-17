"""Interactive QA of the production OTP dialog; never saves the response."""
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root), str(root / ".vendor")]
from PySide6.QtWidgets import QApplication
from duo.verification import VerificationDialog

app = QApplication([])
app.setStyle("Fusion")
dialog = VerificationDialog(phone="0000", message=(sys.argv[1] if len(sys.argv) > 1 else ""))
dialog.exec()
# A private anonymous pipe to the QA driver, never a file or public log.
sys.stdout.write(dialog.take_code())
sys.stdout.flush()
