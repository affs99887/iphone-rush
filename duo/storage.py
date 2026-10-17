"""Local Windows DPAPI settings; never store passwords or identity numbers."""
from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path

from .model import Config


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("本软件仅支持 Windows。")
    source = ctypes.create_string_buffer(data)
    inp = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.windll.crypt32
    # CRYPTPROTECT_UI_FORBIDDEN; encryption is bound to the current Windows user.
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not fn(ctypes.byref(inp), None, None, None, None, 1, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.data, out.size)
    finally:
        ctypes.windll.kernel32.LocalFree(out.data)


def save_config(path: Path, config: Config):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = protect(json.dumps(config.to_dict(), ensure_ascii=False).encode("utf-8"))
    temp = path.with_suffix(".tmp")
    temp.write_bytes(content)
    temp.replace(path)


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    cfg = Config.from_dict(json.loads(protect(path.read_bytes(), decrypt=True)))
    cfg.validate()
    return cfg
