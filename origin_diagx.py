# -*- coding: utf-8 -*-
"""诊断、恢复与安全检查 —— 吸收自 originlab-mcp / origin-ilhae-mcp / Origin-Pro-MCP。

本模块提供三件现有插件没有的能力：

1. **阻塞对话框探测**（origin-ilhae-mcp）
   COM 调用超时的九成原因是 Origin 弹了模态对话框。用 ctypes 直接枚举窗口，
   不依赖 pywin32，把对话框标题与正文取回来，让调用方知道该点哪个按钮。

2. **LabTalk 语句分类**（Origin-Pro-MCP + originlab-mcp）
   现有 `_labtalk_gate` 是硬拦截（命中就拒），本模块给出更细的分级：
   safe / confirm / destructive，并返回命中的具体命令词。
   同时能正确切分语句（字符串、注释、花括号内的分号不算分隔符）。

3. **路径白名单校验**（originlab-mcp）
   限制 load/export/save 只能落在指定根目录内，防止误写系统目录。

另附 `build_doctor_report()` 的结构化健康报告（无需连接 Origin）。
"""
from __future__ import annotations

import os
import platform
import re
import sys
from typing import Any

DIALOG_CLASS = "#32770"  # Windows 标准对话框窗口类
ORIGIN_EXES = ("origin64.exe", "origin.exe", "originpro64.exe", "origin64u.exe")


# ══════════════════════ 1. 阻塞对话框探测 ══════════════════════
def find_blocking_dialogs() -> list[dict[str, Any]]:
    """枚举 Origin 进程拥有的模态对话框。

    Returns:
        [{"title": ..., "text": ..., "class": ..., "exe": ..., "hwnd": ...}]
        非 Windows 平台返回空列表。
    """
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    found: list[dict[str, Any]] = []

    def _text(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _class(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def _exe(pid):
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1].lower()
            return ""
        finally:
            kernel32.CloseHandle(h)

    def _child_text(hwnd):
        """取对话框正文：优先 Static 控件文本。"""
        parts = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                         wintypes.LPARAM)

        def cb(child, _lparam):
            cls = _class(child)
            if cls in ("Static", "Edit"):
                t = _text(child)
                if t:
                    parts.append(t)
            return True

        try:
            user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        except Exception:
            pass
        return "\n".join(parts)

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _class(hwnd) != DIALOG_CLASS:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _exe(pid.value)
        if exe not in ORIGIN_EXES:
            return True
        found.append({"hwnd": int(hwnd), "title": _text(hwnd),
                      "text": _child_text(hwnd), "class": _class(hwnd),
                      "exe": exe, "pid": int(pid.value)})
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(cb), 0)
    except Exception:
        return found
    return found


def dismiss_dialogs(button_texts=("OK", "确定", "Cancel", "取消", "是", "Yes")):
    """点掉 Origin 的模态对话框（按按钮文本匹配）。

    Returns:
        {"ok": bool, "clicked": [{"dialog":..., "button":...}], "remaining": N}
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "仅 Windows 支持", "clicked": []}
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hit = []
    for d in find_blocking_dialogs():
        hwnd = d["hwnd"]
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        targets = []

        def cb(child, _l):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, buf, 256)
            if buf.value == "Button":
                n = user32.GetWindowTextLengthW(child)
                tb = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(child, tb, n + 1)
                if tb.value.strip() in button_texts:
                    targets.append((child, tb.value))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
        for child, label in targets:
            if user32.SendMessageW(child, 0x00F5, 0, 0):  # BM_CLICK
                hit.append({"dialog": d["title"], "button": label})
                break
    return {"ok": True, "clicked": hit, "remaining": len(find_blocking_dialogs())}


# ══════════════════════ 2. LabTalk 语句分类 ══════════════════════
def _strip_strings_and_comments(script: str) -> str:
    """把字符串字面量与注释替换为等长空白，保持偏移不变。"""
    out = []
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        if c == '"':
            j = i + 1
            while j < n:
                if script[j] == '\\':
                    j += 2
                    continue
                if script[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
        elif script.startswith("//", i):
            j = script.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif script.startswith("/*", i):
            j = script.find("*/", i)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


# 分级表：(级别, 命令正则, 说明)
_LABTALK_LEVELS = [
    ("destructive", r"\b(delete|del\s+-\w|doc\s+-s|exit|quit|win\s+-c|win\s+-d)\b",
     "删除/关闭/退出类，会丢失未保存数据"),
    ("destructive", r"\b(reset|new\s+project|doc\s+-n)\b", "重置或新建项目"),
    ("confirm", r"\b(save|saveas|open|doc\s+-o)\b", "写文件或打开文件"),
    ("confirm", r"\b(expGraph|expASCII|expImage|expMat|expWks)\b", "导出文件"),
    ("confirm", r"\b(system|run|dos|exec)\b", "调用外部程序"),
    ("confirm", r"\b(type\s+-b|type\s+-a|dlg\.|dlg\s|getn|getstr)\b",
     "弹模态对话框，会卡死 COM"),
]
_LABTALK_COMPILED = [(lv, re.compile(p, re.IGNORECASE), why)
                     for lv, p, why in _LABTALK_LEVELS]


def classify_labtalk_script(script: str) -> dict:
    """把 LabTalk 脚本按风险分级。

    Returns:
        {"level": "safe"|"confirm"|"destructive",
         "hits": [{"level","token","reason","position"}],
         "statements": N, "blocked": bool}
        注意：本函数据实分级，是否放行由调用方决定。
    """
    cleaned = _strip_strings_and_comments(str(script or ""))
    hits = []
    for lv, pat, why in _LABTALK_COMPILED:
        for m in pat.finditer(cleaned):
            hits.append({"level": lv, "token": m.group(0).strip(),
                         "reason": why, "position": m.start()})
    hits.sort(key=lambda h: h["position"])
    level = "safe"
    if any(h["level"] == "destructive" for h in hits):
        level = "destructive"
    elif hits:
        level = "confirm"
    return {"level": level, "hits": hits,
            "statements": len(split_labtalk_statements(script)),
            "blocked": level == "destructive"}


def split_labtalk_statements(script: str) -> list[str]:
    """按顶层分号切分 LabTalk 语句（字符串/注释/花括号内的分号不切）。"""
    s = str(script or "")
    cleaned = _strip_strings_and_comments(s)
    out, start, depth = [], 0, 0
    for i, c in enumerate(cleaned):
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == ";" and depth == 0:
            frag = s[start:i + 1].strip()
            if frag and frag != ";":
                out.append(frag)
            start = i + 1
    tail = s[start:].strip()
    if tail:
        out.append(tail)
    return out


# ══════════════════════ 3. 路径白名单 ══════════════════════
_ALLOWED_ENV = "DSH_ORIGIN_ALLOWED_ROOTS"


def _parse_roots(environ=None):
    env = environ if environ is not None else os.environ
    raw = env.get(_ALLOWED_ENV, "").strip()
    if not raw:
        return None
    roots = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(os.path.realpath(os.path.expanduser(part)))
        except OSError:
            continue
    return roots or None


def check_allowed_path(path: str, environ=None):
    """校验路径是否落在白名单根目录内。

    Returns:
        None 表示允许（或未设白名单）；否则返回错误说明字符串。
        白名单由环境变量 DSH_ORIGIN_ALLOWED_ROOTS 配置（分号/冒号分隔）。
    """
    if not path or not str(path).strip():
        return "路径不能为空"
    roots = _parse_roots(environ)
    if roots is None:
        return None
    try:
        resolved = os.path.realpath(os.path.expanduser(str(path)))
    except OSError as exc:
        return f"无法解析路径 '{path}': {exc}"
    for r in roots:
        rp = r.rstrip(os.sep) + os.sep
        if resolved == r or resolved.startswith(rp):
            return None
    return (f"路径不在允许的根目录内: {resolved}。允许的根: "
            f"{os.pathsep.join(roots)}（由 {_ALLOWED_ENV} 配置）")


# ══════════════════════ 4. 结构化健康报告 ══════════════════════
def build_doctor_report(engine=None, ping_origin: bool = False) -> dict:
    """不依赖 Origin 的系统健康报告（ping_origin=True 时额外尝试连接）。"""
    checks = []

    def ck(name, status, detail, hint=""):
        e = {"check": name, "status": status, "detail": detail}
        if hint:
            e["hint"] = hint
        checks.append(e)

    system = platform.system()
    ck("platform", "ok" if system == "Windows" else "warn",
       f"{system} {platform.release()} ({platform.machine()})",
       "" if system == "Windows" else "Origin COM 自动化需要 Windows + OriginLab")
    ck("python", "ok",
       f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")

    # Origin 安装
    cands = []
    for base in (r"C:\Program Files\OriginLab", r"C:\Program Files (x86)\OriginLab"):
        if os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                exe = os.path.join(base, d, "Origin64.exe")
                if os.path.exists(exe):
                    cands.append(exe)
    ck("origin_install", "ok" if cands else "error",
       f"找到 {len(cands)} 个安装" + (f"：{cands[-1]}" if cands else ""),
       "" if cands else "未发现 Origin64.exe，请确认 Origin 已安装")

    # 进程
    procs = 0
    try:
        import subprocess
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Origin64.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        procs = out.lower().count("origin64.exe")
    except Exception:
        procs = -1
    ck("origin_process", "ok" if procs <= 1 else "warn",
       f"运行中的 Origin64 进程数：{procs}",
       "" if procs <= 1 else "多实例会让 COM 连错，建议只保留一个")

    # 对话框阻塞
    try:
        dlgs = find_blocking_dialogs()
        ck("blocking_dialogs", "warn" if dlgs else "ok",
           f"检测到 {len(dlgs)} 个模态对话框",
           "；".join(d["title"] for d in dlgs[:3]) if dlgs
           else "")
    except Exception as exc:
        ck("blocking_dialogs", "warn", f"检测失败: {exc}")

    # 知识库
    kb = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "knowledge", "official_docs.generated.json")
    ck("knowledge_base", "ok" if os.path.exists(kb) else "warn",
       f"{os.path.getsize(kb) / 1024:.0f} KB" if os.path.exists(kb) else "缺失",
       "" if os.path.exists(kb) else "从 origin-mcp 拷贝 official_docs.generated.json")

    # 路径白名单
    roots = _parse_roots()
    ck("path_whitelist", "ok",
       "未启用" if roots is None else f"{len(roots)} 个根目录",
       "" if roots is not None else f"如需限制，设 {_ALLOWED_ENV}")

    # 可选：真实连接
    if ping_origin and engine is not None:
        try:
            ok, conn = engine.status()
            ck("origin_connection", "ok" if ok else "error",
               "已连接" if ok else str(conn)[:160])
        except Exception as exc:
            ck("origin_connection", "error", f"{type(exc).__name__}: {exc}")

    worst = "ok"
    if any(c["status"] == "error" for c in checks):
        worst = "error"
    elif any(c["status"] == "warn" for c in checks):
        worst = "warn"
    return {"ok": worst != "error", "status": worst, "checks": checks,
            "n_checks": len(checks)}
