# -*- coding: utf-8 -*-
"""导出图的 DPI 元数据修复 —— 吸收自 origin-hands-off-thesis。

问题
----
Origin 导出 PNG/TIFF 时，**写进文件头部的 dpi 一律是 300**，与你指定的像素宽度无关。
于是 600 dpi 的图插进 Word，物理尺寸会变成两倍，排版全乱。

做法
----
直接改文件头，不重编码、不损失画质：
  PNG   pHYs 块（必须在 IDAT 之前）
  TIFF  tag 282/283
  JPEG  JFIF APP0 段
  BMP   biXPelsPerMeter / biYPelsPerMeter

用法
----
    fix_file("out.png", 600)      # 按扩展名自动分派
    fix_dir("D:/figs", 600)       # 批量修整个目录
"""
from __future__ import annotations

import os
import struct
import zlib


def _png_set_dpi(path, dpi):
    """写/改 PNG 的 pHYs 块（Origin 导出的 dpi 元数据固定是 300，与像素宽无关）。"""
    ppm = int(round(dpi / 0.0254))
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    body = struct.pack(">IIB", ppm, ppm, 1)
    chunk = (struct.pack(">I", len(body)) + b"pHYs" + body +
             struct.pack(">I", zlib.crc32(b"pHYs" + body) & 0xFFFFFFFF))
    pos, out, done = 8, bytearray(data[:8]), False
    while pos + 12 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        end = pos + 12 + ln
        if typ == b"pHYs":
            out += chunk
            done = True
        else:
            out += data[pos:end]
            if typ == b"IHDR" and not done:      # pHYs 必须在 IDAT 之前
                out += chunk
                done = True
        pos = end
    with open(path, "wb") as f:
        f.write(bytes(out))
    return done


def _tif_set_dpi(path, dpi):
    """改 TIFF 第 0 个 IFD 的 XResolution/YResolution（RATIONAL，值在偏移处）。"""
    with open(path, "r+b") as f:
        head = f.read(8)
        if head[:2] == b"II":
            bo = "<"
        elif head[:2] == b"MM":
            bo = ">"
        else:
            return False
        ifd = struct.unpack(bo + "I", head[4:8])[0]
        f.seek(ifd)
        n = struct.unpack(bo + "H", f.read(2))[0]
        entries = f.read(n * 12)
        hit = False
        for i in range(n):
            tag, typ, cnt = struct.unpack(bo + "HHI", entries[i * 12:i * 12 + 8])
            raw = entries[i * 12 + 8:i * 12 + 12]
            if tag in (282, 283) and typ == 5 and cnt == 1:
                off = struct.unpack(bo + "I", raw)[0]
                f.seek(off)
                f.write(struct.pack(bo + "II", int(round(dpi)), 1))
                hit = True
            elif tag == 296 and typ == 3:        # ResolutionUnit = inch
                f.seek(ifd + 2 + i * 12 + 8)     # SHORT 存在值域前 2 字节
                f.write(struct.pack(bo + "HH", 2, 0))
        return hit


def _jpg_set_dpi(path, dpi):
    """改 JPEG 的 JFIF(APP0) 密度字段。"""
    with open(path, "r+b") as f:
        data = f.read(4096)
        i = data.find(b"\xff\xe0")
        if i < 0 or data[i + 4:i + 9] != b"JFIF\x00":
            return False
        f.seek(i + 11)                            # units(1) + Xdensity(2) + Ydensity(2)
        f.write(struct.pack(">BHH", 1, int(round(dpi)), int(round(dpi))))
        return True


def _bmp_set_dpi(path, dpi):
    """改 BMP 的 biXPelsPerMeter / biYPelsPerMeter。"""
    ppm = int(round(dpi / 0.0254))
    with open(path, "r+b") as f:
        if f.read(2) != b"BM":
            return False
        f.seek(38)
        f.write(struct.pack("<ii", ppm, ppm))
        return True


def fix_dpi(path, ext, dpi):
    """把文件里声明的 dpi 改成真实 dpi；返回 True 表示改成功。"""
    try:
        if ext == "png":
            return _png_set_dpi(path, dpi)
        if ext == "tif":
            return _tif_set_dpi(path, dpi)
        if ext == "jpg":
            return _jpg_set_dpi(path, dpi)
        if ext == "bmp":
            return _bmp_set_dpi(path, dpi)
    except Exception:                            # pylint: disable=broad-except
        return False
    return False


# ------------------------------------------------------------- 名字处理 -----
_BAD_CHARS = set('<>:"/\\|?*%$`')                # 后三个是 LabTalk 字符串里的雷


# ══════════════════════ 统一入口 ══════════════════════
_FIXERS = {".png": _png_set_dpi, ".tif": _tif_set_dpi, ".tiff": _tif_set_dpi,
           ".jpg": _jpg_set_dpi, ".jpeg": _jpg_set_dpi, ".bmp": _bmp_set_dpi}


def fix_file(path, dpi=600):
    """修正单个文件的 dpi 元数据。

    Args:
        path: 图片路径。
        dpi:  目标 dpi（投稿印刷常用 600）。
    Returns:
        {"ok": bool, "file": ..., "format": ..., "dpi": ..., "status": ...}
    """
    ext = os.path.splitext(str(path))[1].lower()
    fn = _FIXERS.get(ext)
    if fn is None:
        return {"ok": False, "file": path, "format": ext, "dpi": dpi,
                "status": "unsupported",
                "error": f"不支持该格式：{ext}（支持 png/tif/jpg/bmp）"}
    if not os.path.exists(path):
        return {"ok": False, "file": path, "format": ext, "dpi": dpi,
                "status": "missing", "error": "文件不存在"}
    try:
        changed = fn(str(path), int(dpi))
        return {"ok": bool(changed), "file": path, "format": ext,
                "dpi": int(dpi),
                "status": "fixed" if changed else "no_header_written"}
    except Exception as exc:
        return {"ok": False, "file": path, "format": ext, "dpi": int(dpi),
                "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def fix_dir(directory, dpi=600, recursive=False):
    """批量修正目录内图片的 dpi 元数据。"""
    if not os.path.isdir(directory):
        return {"ok": False, "error": f"目录不存在: {directory}"}
    results = []
    if recursive:
        for root, _d, files in os.walk(directory):
            for f in files:
                if os.path.splitext(f)[1].lower() in _FIXERS:
                    results.append(fix_file(os.path.join(root, f), dpi))
    else:
        for f in sorted(os.listdir(directory)):
            p = os.path.join(directory, f)
            if os.path.isfile(p) and os.path.splitext(f)[1].lower() in _FIXERS:
                results.append(fix_file(p, dpi))
    n_ok = sum(1 for r in results if r.get("ok"))
    return {"ok": n_ok > 0 or not results, "n_total": len(results),
            "n_fixed": n_ok, "dpi": dpi, "results": results,
            "detail": f"{n_ok}/{len(results)} 个文件的 dpi 元数据已改为 {dpi}"}
