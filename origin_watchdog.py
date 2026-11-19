# -*- coding: utf-8 -*-
"""Origin 弹窗观察器 —— 默认**只报告，不点击**。

安全前提（2026-11-19 Codex 审查后重写）
----------------------------------------
旧版本会自动点掉 Origin 的模态对话框，包括「更改保存到项目」时点「否」。
问题：它按**进程名**筛选，用户**手动打开**的 Origin 工程也在范围内。
点「否」会丢弃用户未保存的修改。没有任何理由让自动化替用户做这个决定。

因此本模块改为：

- **自动模式（默认）**：只在「MCP 确实卡住了」时才动手。
- **两条硬闸门，缺一不可**：
  1. **MCP 正在等 COM**（忙碌标志新鲜）—— 你自己操作 Origin 时 MCP 没在等，
     标志为空，观察器绝不触碰任何弹窗。
  2. **弹窗属于我们自己拉起的 Origin** —— 你手动打开的实例从不登记，
     观察器永远碰不到。
- **所有权过滤**：只有 `owned_pids` 里的 Origin 进程弹的窗才会被处理。
  `owned_pids` 由引擎在**自己连上** Origin 时登记，用户手动开的实例不登记。
- 未知按钮一律不点。

为什么不能靠 MCP 自己处理
------------------------
Origin 弹模态框会把 COM 线程挂起，此时 MCP 调不动任何工具。
所以观察器必须独立进程，否则连"报告"都发不出来。

用法
----
    pythonw origin_watchdog.py              # 默认：自动（MCP 卡住时才动手）
    pythonw origin_watchdog.py --observe    # 只观察，绝不点击
    python origin_watchdog.py --once        # 扫一轮并打印，不循环
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time
import traceback

ORIGIN_EXES = ("origin64.exe", "origin.exe", "originpro64.exe")
DIALOG_CLASS = "#32770"

SAVE_WORDS = ("更改保存", "保存到项目", "Save changes", "Save project")

_HOME = os.path.expanduser("~")
_LOG = os.path.join(_HOME, "dsh_origin_watchdog.log")
_HEARTBEAT = os.path.join(_HOME, "dsh_origin_watchdog.pid")
_OWNED = os.path.join(_HOME, "dsh_origin_owned_pids.json")
_BUSY = os.path.join(_HOME, "dsh_origin_mcp_busy.json")


def mcp_busy(max_age=600):
    """MCP 现在是否真的在等一次 COM 调用。返回 dict 或 None。

    这是**唯一的安全闸门**（2026-11-19 用户要求）：
    用户自己操作 Origin 时 MCP 没在等，这里返回 None，观察器一律不点击。
    """
    try:
        with open(_BUSY, "r", encoding="utf-8") as f:
            d = json.load(f)
        age = time.time() - float(d.get("ts", 0))
        if age > max_age:
            return None
        d["age"] = age
        return d
    except Exception:
        return None


# ───────────────────── 所有权登记（由引擎写入）─────────────────────
def register_owned_pid(pid):
    """引擎连上 Origin 后调用，登记"这个 Origin 实例归我管"。"""
    try:
        pids = set()
        if os.path.exists(_OWNED):
            with open(_OWNED, "r", encoding="utf-8") as f:
                pids = set(json.load(f) or [])
        pids.add(int(pid))
        with open(_OWNED, "w", encoding="utf-8") as f:
            json.dump(sorted(pids), f)
    except Exception:
        pass


def owned_pids():
    try:
        if os.path.exists(_OWNED):
            with open(_OWNED, "r", encoding="utf-8") as f:
                return set(int(x) for x in (json.load(f) or []))
    except Exception:
        pass
    return set()


# ───────────────────── Win32 基础 ─────────────────────
def _u32():
    return ctypes.WinDLL("user32", use_last_error=True)


def _k32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _text(h):
    u = _u32()
    n = u.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, b, n + 1)
    return b.value


def _cls(h):
    u = _u32()
    b = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(h, b, 256)
    return b.value


def _exe(pid):
    k = _k32()
    h = k.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        b = ctypes.create_unicode_buffer(1024)
        s = wt.DWORD(1024)
        if k.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(s)):
            return b.value.rsplit("\\", 1)[-1].lower()
        return ""
    finally:
        k.CloseHandle(h)


def _pid_alive(pid):
    try:
        k = _k32()
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = wt.DWORD()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    except Exception:
        return False


# ───────────────────── 扫描 ─────────────────────
def scan(act=False, owned_only=True):
    """枚举 Origin 模态对话框。

    Args:
        act: True 才真正点击；默认 False 仅报告。
        owned_only: True 时只处理引擎登记过的 Origin PID。

    Returns:
        [{"title", "body", "buttons", "pid", "owned", "action"}]
        action: "reported"（未点）/ "clicked:按钮"（已点）
    """
    if sys.platform != "win32":
        return []
    u = _u32()
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    mine = owned_pids()
    found = []

    def cb(hwnd, _l):
        if not u.IsWindowVisible(hwnd) or _cls(hwnd) != DIALOG_CLASS:
            return True
        pid = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pidv = int(pid.value)
        if _exe(pidv) not in ORIGIN_EXES:
            return True

        title = _text(hwnd)
        body, buttons = [], []

        def cb2(ch, _l2):
            c = _cls(ch)
            if c in ("Static", "Edit"):
                t = _text(ch)
                if t.strip():
                    body.append(t)
            elif c == "Button":
                t = _text(ch).strip()
                if t:
                    buttons.append((ch, t))
            return True

        u.EnumChildWindows(hwnd, PROC(cb2), 0)

        owned = pidv in mine
        busy = mcp_busy()
        blob = title + " " + " ".join(body)
        is_save = any(w in blob for w in SAVE_WORDS)
        rec = {"title": title, "body": blob[:200], "pid": pidv,
               "owned": owned, "mcp_busy": bool(busy), "is_save": is_save,
               "buttons": [t for _, t in buttons], "action": "reported"}

        if not act:
            found.append(rec)
            return True
        if not busy:
            # MCP 没在等 —— 这个弹窗是用户自己的，绝不碰
            rec["action"] = "skipped:mcp_idle"
            found.append(rec)
            return True
        if owned_only and not owned:
            rec["action"] = "skipped:not_owned"
            found.append(rec)
            return True
        if not buttons:
            rec["action"] = "skipped:no_buttons"
            found.append(rec)
            return True

        # 只有保存类提示才点「否」；其他一律点「确定 / 是」这类肯定按钮
        def pick(cands):
            for want in cands:
                for ch, t in buttons:
                    if want in t:
                        return ch, t
            return None

        target = None
        if is_save:
            target = pick(("否", "No"))
        if target is None:
            target = pick(("确定", "OK", "是", "Yes"))
        if target is None:
            rec["action"] = "skipped:unknown_buttons"
            found.append(rec)
            return True

        u.SendMessageW(target[0], 0x00F5, 0, 0)  # BM_CLICK
        rec["action"] = f"clicked:{target[1]}"
        found.append(rec)
        return True

    try:
        u.EnumWindows(PROC(cb), 0)
    except Exception:
        pass
    return found


# 兼容旧名
def scan_and_dismiss(button_policy=None):
    """旧名。行为等同 scan(act=False)：只报告。"""
    return scan(act=False)


# ───────────────────── 心跳与常驻 ─────────────────────

_MUTEX_NAME = "Global\\dsh_origin_watchdog_singleton"


def _acquire_singleton():
    """单实例锁：已在跑就返回 None（新实例立刻退出，避免多个观察器互相覆盖心跳）。

    2026-11-19 实测：重复启动会留下 6 个 pythonw，心跳文件被乱写，
    status() 报出错误 PID。
    """
    try:
        k = _k32()
        # CreateMutexW 返回句柄；GetLastError()==183 (ERROR_ALREADY_EXISTS) 说明已存在
        h = k.CreateMutexW(None, False, _MUTEX_NAME)
        if not h:
            return True          # 拿不到锁也不阻塞主流程
        if ctypes.get_last_error() == 183 or k.GetLastError() == 183:
            k.CloseHandle(h)
            return None
        return h                 # 调用方需一直持有
    except Exception:
        return True


def _log(msg):
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def _write_heartbeat(act=False):
    try:
        with open(_HEARTBEAT, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n{time.time()}\n{1 if act else 0}\n")
    except Exception:
        pass


def run(interval=1.0, act=True):
    """常驻循环。默认 observe：只记录不点击。"""
    mode = "AUTO" if act else "OBSERVE"
    handle = _acquire_singleton()
    if handle is None:
        _log(f"watchdog already running, this instance (pid={os.getpid()}) exits")
        return
    _log(f"watchdog started pid={os.getpid()} mode={mode}")
    _write_heartbeat(act)
    tick = 0
    while True:
        try:
            for h in scan(act=act):
                tag = "save" if h["is_save"] else "notice"
                _log(f"[{tag}] pid={h['pid']} owned={h['owned']} "
                     f"{h['title']} | {h['body'][:60]} -> {h['action']}")
        except Exception:
            _log("ERR " + traceback.format_exc()[-300:])
        tick += 1
        if tick % 30 == 0:
            _write_heartbeat(act)
        time.sleep(interval)


def status():
    """报告运行状态。"""
    running = False
    pid = None
    mode = None
    if os.path.exists(_HEARTBEAT):
        try:
            with open(_HEARTBEAT, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            pid = int(lines[0])
            mode = "act" if len(lines) > 2 and lines[2].strip() == "1" else "observe"
            running = _pid_alive(pid)
        except Exception:
            running = False
    recent = []
    if os.path.exists(_LOG):
        try:
            with open(_LOG, "r", encoding="utf-8", errors="replace") as f:
                recent = f.readlines()[-10:]
        except Exception:
            pass
    return {"ok": True, "running": running, "pid": pid, "mode": mode,
            "owned_pids": sorted(owned_pids()),
            "log_path": _LOG,
            "recent_log": [r.rstrip() for r in recent],
            "detail": (f"观察器在运行（pid={pid}，模式={mode}）"
                       if running else
                       "观察器未运行；MCP 启动时会自动拉起（默认 observe 模式）")}


if __name__ == "__main__":
    act = "--observe" not in sys.argv
    if "--once" in sys.argv:
        print(json.dumps(scan(act=act), ensure_ascii=False, indent=1))
    else:
        run(act=act)
