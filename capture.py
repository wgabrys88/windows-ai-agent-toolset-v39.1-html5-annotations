from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import struct
import sys
import time
import zlib
from typing import Final

import config as _cfg

_SRCCOPY: Final = 0x00CC0020
_CAPTUREBLT: Final = 0x40000000
_BI_RGB: Final = 0
_DIB_RGB: Final = 0
_HALFTONE: Final = 4

try:
    ctypes.WinDLL("shcore", use_last_error=True).SetProcessDpiAwareness(2)
except Exception:
    pass

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_W = ctypes.wintypes
_vp = ctypes.c_void_p
_ci = ctypes.c_int


def _sig(obj, attr, args, res):
    fn = getattr(obj, attr); fn.argtypes = args; fn.restype = res


_sig(_user32, "GetDC", [_W.HWND], _W.HDC)
_sig(_user32, "ReleaseDC", [_W.HWND, _W.HDC], _ci)
_sig(_user32, "GetSystemMetrics", [_ci], _ci)
_sig(_gdi32, "CreateCompatibleDC", [_W.HDC], _W.HDC)
_sig(_gdi32, "CreateDIBSection", [_W.HDC, _vp, _W.UINT, ctypes.POINTER(_vp), _W.HANDLE, _W.DWORD], _W.HBITMAP)
_sig(_gdi32, "SelectObject", [_W.HDC, _W.HGDIOBJ], _W.HGDIOBJ)
_sig(_gdi32, "BitBlt", [_W.HDC, _ci, _ci, _ci, _ci, _W.HDC, _ci, _ci, _W.DWORD], _W.BOOL)
_sig(_gdi32, "StretchBlt", [_W.HDC, _ci, _ci, _ci, _ci, _W.HDC, _ci, _ci, _ci, _ci, _W.DWORD], _W.BOOL)
_sig(_gdi32, "SetStretchBltMode", [_W.HDC, _ci], _ci)
_sig(_gdi32, "SetBrushOrgEx", [_W.HDC, _ci, _ci, _vp], _W.BOOL)
_sig(_gdi32, "DeleteObject", [_W.HGDIOBJ], _W.BOOL)
_sig(_gdi32, "DeleteDC", [_W.HDC], _W.BOOL)

del _sig, _W, _vp, _ci


def _log(msg: str) -> None:
    sys.stderr.write(f"[capture] {msg}\n"); sys.stderr.flush()


def screen_size() -> tuple[int, int]:
    w, h = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    return (w, h) if w > 0 and h > 0 else (1920, 1080)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD), ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG), ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD), ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD), ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG), ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.wintypes.DWORD * 3)]


def _make_bmi(w: int, h: int) -> _BITMAPINFO:
    bmi = _BITMAPINFO()
    hdr = bmi.bmiHeader
    hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    hdr.biWidth, hdr.biHeight = w, -h
    hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, _BI_RGB
    return bmi


def _create_dib(sdc, w: int, h: int):
    bits = ctypes.c_void_p()
    hbmp = _gdi32.CreateDIBSection(sdc, ctypes.byref(_make_bmi(w, h)), _DIB_RGB, ctypes.byref(bits), None, 0)
    return (hbmp, bits) if hbmp and bits.value else (None, None)


def _read_dib(bits, n: int) -> bytes | None:
    try:
        return bytes((ctypes.c_ubyte * n).from_address(bits.value))
    except Exception as e:
        _log(f"DIB read failed: {e}"); return None


def capture_screen(w: int, h: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    memdc = _gdi32.CreateCompatibleDC(sdc)
    if not memdc:
        _user32.ReleaseDC(0, sdc); return None
    hbmp, bits = _create_dib(sdc, w, h)
    if not hbmp:
        _gdi32.DeleteDC(memdc); _user32.ReleaseDC(0, sdc); return None
    old = _gdi32.SelectObject(memdc, hbmp)
    _gdi32.BitBlt(memdc, 0, 0, w, h, sdc, 0, 0, _SRCCOPY | _CAPTUREBLT)
    result = _read_dib(bits, w * h * 4)
    _gdi32.SelectObject(memdc, old)
    _gdi32.DeleteObject(hbmp)
    _gdi32.DeleteDC(memdc)
    _user32.ReleaseDC(0, sdc)
    return result


def _resize_bgra(src: bytes, sw: int, sh: int, dw: int, dh: int) -> bytes | None:
    sdc = _user32.GetDC(0)
    if not sdc:
        return None
    src_dc, dst_dc = _gdi32.CreateCompatibleDC(sdc), _gdi32.CreateCompatibleDC(sdc)
    if not src_dc or not dst_dc:
        for dc in (src_dc, dst_dc):
            if dc: _gdi32.DeleteDC(dc)
        _user32.ReleaseDC(0, sdc); return None
    src_bmp, src_bits = _create_dib(sdc, sw, sh)
    if not src_bmp:
        _gdi32.DeleteDC(src_dc); _gdi32.DeleteDC(dst_dc); _user32.ReleaseDC(0, sdc); return None
    ctypes.memmove(src_bits.value, src, sw * sh * 4)
    old_src = _gdi32.SelectObject(src_dc, src_bmp)
    dst_bmp, dst_bits = _create_dib(sdc, dw, dh)
    if not dst_bmp:
        _gdi32.SelectObject(src_dc, old_src); _gdi32.DeleteObject(src_bmp)
        _gdi32.DeleteDC(src_dc); _gdi32.DeleteDC(dst_dc); _user32.ReleaseDC(0, sdc); return None
    old_dst = _gdi32.SelectObject(dst_dc, dst_bmp)
    _gdi32.SetStretchBltMode(dst_dc, _HALFTONE)
    _gdi32.SetBrushOrgEx(dst_dc, 0, 0, None)
    _gdi32.StretchBlt(dst_dc, 0, 0, dw, dh, src_dc, 0, 0, sw, sh, _SRCCOPY)
    result = _read_dib(dst_bits, dw * dh * 4)
    _gdi32.SelectObject(dst_dc, old_dst); _gdi32.SelectObject(src_dc, old_src)
    _gdi32.DeleteObject(dst_bmp); _gdi32.DeleteObject(src_bmp)
    _gdi32.DeleteDC(dst_dc); _gdi32.DeleteDC(src_dc)
    _user32.ReleaseDC(0, sdc)
    return result


def encode_png(bgra: bytes, w: int, h: int) -> bytes:
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * stride:(y + 1) * stride]
        for i in range(0, len(row), 4):
            raw.extend((row[i + 2], row[i + 1], row[i], 255))

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def crop_bgra(bgra: bytes, sw: int, sh: int, x1: int, y1: int, x2: int, y2: int) -> tuple[bytes, int, int]:
    x1, y1 = max(0, min(x1, sw)), max(0, min(y1, sh))
    x2, y2 = max(x1, min(x2, sw)), max(y1, min(y2, sh))
    if x1 >= x2 or y1 >= y2:
        return bgra, sw, sh
    cw, ch = x2 - x1, y2 - y1
    out = bytearray(cw * ch * 4)
    ss, ds = sw * 4, cw * 4
    for y in range(ch):
        so = (y1 + y) * ss + x1 * 4
        do = y * ds
        out[do:do + ds] = bgra[so:so + ds]
    return bytes(out), cw, ch


def preview_b64(max_width: int = 800) -> str:
    sw, sh = screen_size()
    bgra = capture_screen(sw, sh)
    if bgra is None:
        return ""
    dw = min(sw, max_width)
    dh = int(sh * (dw / sw))
    if (dw, dh) != (sw, sh):
        resized = _resize_bgra(bgra, sw, sh, dw, dh)
        if resized is not None:
            bgra = resized
        else:
            dw, dh = sw, sh
    return base64.b64encode(encode_png(bgra, dw, dh)).decode("ascii")


def capture(crop: dict | None = None) -> str:
    sw, sh = screen_size()
    delay = float(_cfg.CAPTURE_DELAY)
    if delay > 0:
        time.sleep(delay)
    bgra = capture_screen(sw, sh)
    if bgra is None:
        return ""

    bw, bh = sw, sh
    if crop and all(k in crop for k in ("x1", "y1", "x2", "y2")):
        cx1, cy1 = int(crop["x1"]), int(crop["y1"])
        cx2, cy2 = int(crop["x2"]), int(crop["y2"])
        if cx2 > cx1 and cy2 > cy1:
            bgra, bw, bh = crop_bgra(bgra, sw, sh, cx1, cy1, cx2, cy2)

    dw = int(_cfg.WIDTH) if int(_cfg.WIDTH) > 0 else bw
    dh = int(_cfg.HEIGHT) if int(_cfg.HEIGHT) > 0 else bh
    if (dw, dh) != (bw, bh):
        resized = _resize_bgra(bgra, bw, bh, dw, dh)
        if resized is not None:
            bgra = resized
        else:
            dw, dh = bw, bh

    return base64.b64encode(encode_png(bgra, dw, dh)).decode("ascii")


def main() -> None:
    try:
        req = json.loads(sys.stdin.read() or "{}")
        crop = req.get("crop")
        b64 = capture(crop=crop)
        sys.stdout.write(json.dumps({"screenshot_b64": b64}))
        sys.stdout.flush()
    except Exception as exc:
        _log(f"FATAL: {exc}")
        sys.stdout.write(json.dumps({"screenshot_b64": "", "error": str(exc)}))
        sys.stdout.flush()


if __name__ == "__main__":
    main()