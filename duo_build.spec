# Windows 10/11 supply UCRT and API-set DLLs. Bundling an old copy breaks Qt.
from PyInstaller.utils.hooks import collect_all
import os
from pathlib import Path

play_data, play_binary, play_hidden = collect_all('playwright')
runtime_data = [(str(p), 'browser-runtime/' + p.name) for p in Path('browser-runtime').glob('chromium-*') if p.is_dir()]
if not runtime_data:
    raise RuntimeError('Install the project Chromium runtime before building.')
a = Analysis(
    ['app.py'], pathex=['.vendor'], binaries=play_binary, datas=play_data + runtime_data,
    hiddenimports=play_hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQuick', 'tkinter'],
    noarchive=False, optimize=0,
)
a.binaries = [entry for entry in a.binaries if
    entry[0].replace('\\', '/').split('/')[-1].lower() != 'ucrtbase.dll' and
    not entry[0].replace('\\', '/').split('/')[-1].lower().startswith(('api-ms-win-', 'ext-ms-win-'))]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name=os.environ.get('DUO_EXE_NAME', 'iPhoneDuo预约助手'),
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, icon='assets/duo.png',
)
