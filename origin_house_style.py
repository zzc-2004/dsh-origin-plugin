# -*- coding: utf-8 -*-
"""用户个人作图风格（house style）—— 本会话实测固化的配方。

为什么单独一个模块
------------------
Origin 里"把图统一成某个样子"看似简单，实际有大量反直觉的坑，
每一条都是踩过之后才确认的（见下方"实测结论"）。把这些顺序与属性名
固化成一份配方，避免每次重新试错。

实测结论（踩过的坑）
--------------------
1. 刻度标签字号 = `layer.<ax>.label.fsize`（COM: `label.fsize`），
   不是 `ticklabel.pt`。插件 `edit_axis(label_font_pt=)` 走的是这条。
2. 刻度标签加粗只有 COM 通道有效（`edit_axis(label_bold=1)`）；
   LabTalk 的 `layer.x.ticklabel.bold` / `.bt` 都是哑属性，写了不生效。
3. 轴标题字号 = `xb.fsize` / `yl.fsize`，与刻度标签是两套。
4. 轴标题加粗必须在设字体之后写富文本标记：
       xb.font = font("Times New Roman");   // 必须先
       xb.text$ = "\\b(标题)";                // 再标粗
   顺序反了、或之后又调用 edit_axis(title=...) 重写标题，粗体都会丢。
5. 刻度朝里用 **`layer.x.ticks`**（不是 `layer.x.ticks.tickdir`，后者不存在）：
   1 = 主刻度朝内，5 = 主次都朝内。上/右轴用 `layer.x2.ticks` / `layer.y2.ticks` 设 0。
   这条由 Codex 2026-11-19 提供官方文档依据，真机已复现。
6. 四边框 = `layer.x.opposite = 1` + `layer.y.opposite = 1`。
7. 正负值柱状图：x 轴会自动落在 y=0，导致底部没有框线。
   用 `axis -ps X A 3` 能补底边但会多出右边/上边的刻度线；
   正确做法是手工画一条线：
       draw -n BOT -l {x_from, y_from, x_to, y_from};
8. LabTalk 字符串里的换行不能用 JSON 的 \\r\\n（会变成字面量 "rn"），
   图例文本要用插件的 `edit_legend(text=...)` 接口。

用户偏好（不可自行改动）
------------------------
- **图例位置由用户自己拖**：AI 只负责清字号、去边框、修文字乱码，
  不要尝试摆位置（`legend.x/y` 单位混乱，摆不好反而添乱）。
- 示意图 / 流程图 / 象限图**不需要**在 Origin 里画，直接跳过。
"""
from __future__ import annotations

import origin_edit as oedit
import origin_errors as oerr

# ── 本机已验证的字体索引（font("Times New Roman") 的返回值）──
TNR_INDEX = 429

# ── 用户偏好（2026-11-18 会话中明确要求）──
HOUSE = {
    "font": "Times New Roman",
    "axis_title_pt": 10,
    "tick_label_pt": 10,
    "legend_pt": 9,
    "axis_title_bold": True,
    "tick_label_bold": True,
    "four_sided_frame": True,
    "ticks_on_bottom_left_only": True,
    "ticks_inward": True,          # 用 layer.x.ticks=1 实现（见文档第 5 条）
    "page_cm": (12.0, 9.0),
    "layer_pct": {"left": 27, "top": 6, "width": 65, "height": 74},
    # ── 用户明确交代的偏好，AI 不得自作主张 ──
    "legend_position_by_user": True,   # 图例位置用户自己拖，AI 只清字号/边框/文字
    "skip_schematic_figures": True,    # 示意图/流程图/象限图不在 Origin 里画
}


def _require_engine(engine):
    """engine 为空时返回结构化错误，而不是抛 AttributeError。"""
    if engine is None or not hasattr(engine, "_connect_impl"):
        return {"ok": False, "error_code": "invalid_request",
                "error": "需要 Origin 引擎句柄（engine），调用方未提供",
                "next_actions": ["通过 MCP 工具调用（会自动传入 engine）"]}
    return None


def _safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, str(e)


def _finite(v):
    """数值是否有限（过滤 NaN / inf / None / 不可转类型）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


# ══════════════════════════════════════════════════════════════════
# 套用风格 —— 逐项校验，任何必需项失败都不报成功
# ══════════════════════════════════════════════════════════════════
#: 必需步骤：这些没成功，整次套用就判失败
REQUIRED_STEPS = (
    "tick_font", "title_font",
    "x_tick_label", "y_tick_label",
    "four_side_frame", "ticks_inward", "axis_titles_bold",
)


def apply_house_style(engine, graph, x_title=None, y_title=None,
                      page_cm=None, layer_pct=None, legend=True,
                      bottom_edge_at=None):
    """把一张图套成用户的标准风格。**逐项校验，不谎报成功**。

    2026-11-19 Codex 审查 P1 修复：旧实现只收集「脚本没抛异常」的步骤名，
    完全不看 LabTalk 返回值（`LT_execute` 返回 False 也算通过），也不看
    `edit_axis` 返回里 `changes[].status == "rejected"` 的条目。
    结果：10 次写入全被拒、2 次轴编辑零项应用，函数仍返回 `ok=True`。

    现在的判定：
      - LabTalk 步骤：`LT_execute` 返回值必须为真
      - 轴编辑步骤：`changes` 里不得有 rejected
      - 必需步骤（REQUIRED_STEPS）任一失败 → `ok=False`，`partial=True`
      - 每次调用都返回逐项明细，调用方可看到到底哪步没成

    Args:
        graph: 图短名。
        x_title / y_title: 轴标题文本；给 None 则不改动标题内容
            （此时也不会去动已有标题的粗体，语义与 check 保持一致）。
        page_cm: (宽, 高) 厘米，默认 12×9。
        layer_pct: 图层几何（%页）。
        legend: 是否整理图例（只清字号与边框，不摆位置）。
        bottom_edge_at: 正负值柱状图专用的底框线 y 值。

    Returns:
        {"ok", "partial", "graph", "steps":[{item,status,detail}],
         "failed":[...], "notes":[...]}
    """
    bad = _require_engine(engine)
    if bad:
        return bad
    ok, conn = engine._connect_impl()
    if not ok:
        return conn
    op = engine._origin_app
    po = op.po
    short, err = oedit.ensure_active_graph(op, po, graph)
    if err:
        return err

    page_cm = page_cm or HOUSE["page_cm"]
    layer_pct = layer_pct or HOUSE["layer_pct"]
    steps, failed, notes = [], [], []

    def rec(item, okk, detail=""):
        steps.append({"item": item, "status": "applied" if okk else "failed",
                      "detail": detail})
        if not okk:
            failed.append({"item": item, "detail": detail})

    def lt(script, label):
        """执行 LabTalk 并**检查返回值**。"""
        r, e = _safe(po.LT_execute, script)
        if e:
            rec(label, False, e[:160])
            return False
        if r is False:
            rec(label, False, "LabTalk 返回 False")
            return False
        rec(label, True)
        return True

    def axis(ax, props, title=None, label=None):
        """调 edit_axis 并**检查 changes[] 里有无 rejected**。"""
        kw = {"axis": ax, "layer": 0, "props": props}
        if title is not None:
            kw["title"] = title
        try:
            r = engine.edit_axis(short, **kw)
        except Exception as exc:
            rec(label or f"{ax}_axis", False, f"{type(exc).__name__}: {exc}")
            return False
        if not r.get("ok"):
            rec(label or f"{ax}_axis", False, str(r.get("error"))[:160])
            return False
        rejected = [c for c in (r.get("changes") or [])
                    if c.get("status") == "rejected"]
        if rejected:
            rec(label or f"{ax}_axis", False,
                "rejected: " + ", ".join(str(c.get("item")) for c in rejected)[:160])
            return False
        rec(label or f"{ax}_axis", True, f"{len(r.get('changes') or [])} 项")
        return True

    # 1. 字体族
    lt('layer.x.ticklabel.font = font("Times New Roman");'
       'layer.y.ticklabel.font = font("Times New Roman");', "tick_font")
    lt('xb.font = 429; yl.font = 429;', "title_font")

    # 2. 刻度标签（COM 通道才有效）
    axis("x", {"label_font_pt": HOUSE["tick_label_pt"],
               "label_bold": 1 if HOUSE["tick_label_bold"] else 0},
         label="x_tick_label")
    axis("y", {"label_font_pt": HOUSE["tick_label_pt"],
               "label_bold": 1 if HOUSE["tick_label_bold"] else 0},
         label="y_tick_label")

    # 3. 四边框
    if HOUSE["four_sided_frame"]:
        lt("layer.x.opposite = 1; layer.y.opposite = 1;", "four_side_frame")

    # 3.5 刻度朝内（layer.x.ticks: 1=主刻度朝内, 5=主次都朝内；上/右设 0）
    if HOUSE["ticks_inward"]:
        lt("layer.x.ticks = 1; layer.y.ticks = 1; "
           "layer.x2.ticks = 0; layer.y2.ticks = 0;", "ticks_inward")

    # 4. 版面
    lt(f"page.width = {int(round(page_cm[0] / 2.54 * 600))}; "
       f"page.height = {int(round(page_cm[1] / 2.54 * 600))};", "page_size")
    lt(f"layer.left = {layer_pct['left']}; layer.top = {layer_pct['top']}; "
       f"layer.width = {layer_pct['width']}; layer.height = {layer_pct['height']};",
       "layer_geometry")

    # 5. 轴标题：先设字体（第 1 步已做），再写富文本粗体标记
    if x_title is None and y_title is None:
        rec("axis_titles_bold", True, "未传标题，跳过（不动已有标题）")
    else:
        parts = []
        if x_title is not None:
            parts.append(f'xb.text$ = "\\b({x_title})";')
        if y_title is not None:
            parts.append(f'yl.text$ = "\\b({y_title})";')
        parts.append(f'xb.fsize = {HOUSE["axis_title_pt"]};')
        parts.append(f'yl.fsize = {HOUSE["axis_title_pt"]};')
        lt(" ".join(parts), "axis_titles_bold")

    # 6. 图例：只清字号与边框
    if legend:
        lt(f'legend.font = 429; legend.fsize = {HOUSE["legend_pt"]};', "legend_font")
        _safe(po.LT_execute, "legend.showframe = 0;")

    # 7. 正负值柱状图补底框
    if bottom_edge_at is not None:
        x0 = _finite(_safe(op.lt_float, "layer.x.from")[0]) if hasattr(op, "lt_float") else None
        x1 = _finite(_safe(op.lt_float, "layer.x.to")[0]) if hasattr(op, "lt_float") else None
        if x0 is None or x1 is None:
            rec("bottom_edge", False, "读不到 x 轴范围，未画底框")
        else:
            lt(f"draw -n BOTFRAME -l -w 1.5 {{{x0},{bottom_edge_at},{x1},{bottom_edge_at}}};",
               "bottom_edge")
            # 2026-11-19：draw 对象线宽用 linewidth（width/thickness 无效），
            # 与坐标轴线宽对齐，避免补线比另外三边细。
            lt("BOTFRAME.linewidth = 1.5;", "bottom_edge_width")

    # ── 判定 ──
    req_failed = [f for f in failed if f["item"] in REQUIRED_STEPS]
    all_ok = (len(failed) == 0)
    partial = (not all_ok) and (len(req_failed) == 0)
    return {
        "ok": all_ok,
        "partial": partial,
        "graph": short,
        "steps": steps,
        "failed": failed,
        "required_failed": [f["item"] for f in req_failed],
        "notes": notes,
        "detail": ("风格已全部套用并通过校验" if all_ok else
                   ("部分成功：必需项均已应用，但非必需项失败 "
                    + ", ".join(f["item"] for f in failed) if partial else
                    "套用失败：必需项未通过 —— "
                    + ", ".join(f["item"] for f in req_failed))),
    }


# ══════════════════════════════════════════════════════════════════
# 风格检查 —— 区分 通过/不符/未核实，NaN 不抛异常
# ══════════════════════════════════════════════════════════════════
#: 不可读时判为「未核实」（verdict=unverified，ok=False）而不是「符合」的项。
#: 用户明确要求「所有文字加粗」，所以粗体读不回来就不能宣告全部通过。
REQUIRED_CHECKS = ("x_title.font", "y_title.font",
                   "x_title.fsize", "y_title.fsize",
                   "x_title.bold", "y_title.bold",
                   "x.ticks(朝内)", "y.ticks(朝内)")


def check_house_style(engine, graph):
    """检查一张图是否符合用户风格。

    2026-11-19 Codex 审查 P1 修复：
      - 旧版对 `xb.fszie` 等读回值直接 `int()`，遇 NaN 抛 ValueError
      - 旧版把「读不回」计为通过，于是 2 个必需项不可读仍报 ok=True

    现在每项状态为 pass / fail / unreadable 三态；
    需要给出结论时用 `verdict`：pass | mismatch | unverified。
    """
    bad = _require_engine(engine)
    if bad:
        return bad
    ok, conn = engine._connect_impl()
    if not ok:
        return conn
    op = engine._origin_app
    po = op.po
    short, err = oedit.ensure_active_graph(op, po, graph)
    if err:
        return err

    info = engine.inspect_graph(short)
    if not info.get("ok"):
        return info
    lay = (info.get("layers") or [{}])[0]
    axes = lay.get("axes", {})
    checks = []

    def ck(item, expected, actual, status):
        checks.append({"item": item, "expected": expected,
                       "actual": actual, "status": status})

    # 刻度标签与四边框（来自 inspect_graph，可靠）
    for ax in ("x", "y"):
        a = axes.get(ax, {})
        fsz = _finite(a.get("label.fsize"))
        if fsz is None:
            ck(f"{ax}.label.fsize", HOUSE["tick_label_pt"], a.get("label.fsize"), "unreadable")
        else:
            ck(f"{ax}.label.fsize", HOUSE["tick_label_pt"], fsz,
               "pass" if abs(fsz - HOUSE["tick_label_pt"]) < 0.6 else "fail")
        bold = _finite(a.get("label.bold"))
        if bold is None:
            ck(f"{ax}.label.bold", 1, a.get("label.bold"), "unreadable")
        else:
            ck(f"{ax}.label.bold", 1, bold, "pass" if int(bold) == 1 else "fail")
        opp = _finite(a.get("opposite"))
        if opp is None:
            ck(f"{ax}.opposite(四边框)", 1, a.get("opposite"), "unreadable")
        else:
            ck(f"{ax}.opposite(四边框)", 1, opp, "pass" if int(opp) == 1 else "fail")

    # 轴标题字号与字体
    lt_float = getattr(op, "lt_float", None)
    for obj, name in (("xb", "x_title"), ("yl", "y_title")):
        for label, expr, want in (
                (f"{name}.fsize", f"{obj}.fsize", HOUSE["axis_title_pt"]),
                (f"{name}.font", f"{obj}.font", TNR_INDEX)):
            v = None
            if callable(lt_float):
                v, _ = _safe(lt_float, expr)
            fv = _finite(v)
            if fv is None:
                ck(label, want, v, "unreadable")
            else:
                ck(label, want, fv, "pass" if abs(fv - want) < 0.6 else "fail")

    # 轴标题粗体（文本里的  标记）
    for obj, name in (("xb", "x_title"), ("yl", "y_title")):
        t, _ = _safe(oedit.lt_str, po, f"{obj}.text$")
        if not isinstance(t, str):
            ck(f"{name}.bold", "\\b(...)", None, "unreadable")
        else:
            ck(f"{name}.bold", "\\b(...)", t[:48],
               "pass" if ("\\b(" in t or t.lstrip().startswith("\\b")) else "fail")

    # 刻度朝向（layer.x.ticks: 1=主刻度朝内）
    for ax, want in (("x", 1), ("y", 1)):
        v = None
        if callable(lt_float):
            v, _ = _safe(lt_float, f"layer.{ax}.ticks")
        fv = _finite(v)
        if fv is None:
            ck(f"{ax}.ticks(朝内)", want, v, "unreadable")
        else:
            ck(f"{ax}.ticks(朝内)", want, fv, "pass" if int(fv) == want else "fail")

    # 图层几何
    geo = _finite(lay.get("width_%page"))
    if geo is None:
        ck("layer.width_%page", HOUSE["layer_pct"]["width"], lay.get("width_%page"), "unreadable")
    else:
        ck("layer.width_%page", HOUSE["layer_pct"]["width"], geo,
           "pass" if abs(geo - HOUSE["layer_pct"]["width"]) < 3 else "fail")

    n_pass = sum(1 for c in checks if c["status"] == "pass")
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_unread = sum(1 for c in checks if c["status"] == "unreadable")
    req_unread = [c["item"] for c in checks
                  if c["status"] == "unreadable" and c["item"] in REQUIRED_CHECKS]

    if n_fail:
        verdict = "mismatch"
    elif req_unread:
        verdict = "unverified"
    else:
        verdict = "pass"

    return {"ok": verdict == "pass", "verdict": verdict, "graph": short,
            "checks": checks, "n_pass": n_pass, "n_fail": n_fail,
            "n_unreadable": n_unread, "n_total": len(checks),
            "required_unreadable": req_unread,
            "detail": (f"风格检查：{n_pass}/{len(checks)} 通过，"
                       f"{n_fail} 不符，{n_unread} 未核实；结论={verdict}")}


# ══════════════════════════════════════════════════════════════════
# 会话经验库 —— 2026-11-18 一整晚踩坑记录，供后续会话直接查
# ══════════════════════════════════════════════════════════════════
LESSONS = {
    "preferences": [
        "全图字号 10pt",
        "字体统一 Times New Roman",
        "所有文字加粗（刻度标签与轴标题都要）",
        "刻度线朝里（本机 Origin 无可写入口，需手动）",
        "四边都要有轴线",
        "刻度线只保留 x 轴下边 + y 轴左边",
        "图例位置由用户自己拖，AI 不要摆",
        "页面 12×9 cm",
        "示意图/流程图/象限图不需要在 Origin 里画",
    ],
    "workflow": [
        "一个 OPJU 一张图",
        "CSV → 导入 → 画 → 套风格 → 存 OPJU",
        "另存 PNG 预览到单独文件夹",
        "文件名必须 ASCII（中文名 doc -o 打不开）",
        "交付前必须导出 PNG 亲眼看，不能凭想象",
    ],
    "gotchas": [
        {"id": "C1", "level": "致命",
         "item": "改插件必须部署到 DSH 实际加载的包目录（通常 .../dsh_origin_plugin/pkg）",
         "why": "~/.dsh/origin-plugin/ 是另一份副本，改了不生效，白忙一晚"},
        {"id": "C2", "level": "致命",
         "item": "Origin 安装路径不一定在 C:\\Program Files，以进程实际路径为准",
         "why": "版本探测可能按 Program Files 误报；以运行进程的 exe 路径为准"},
        {"id": "C3", "level": "高",
         "item": "origin_view_graph 渲染按图名缓存",
         "why": "改了看不出来，会误判「没生效」。必须 origin_export 到新文件名再看"},
        {"id": "C4", "level": "致命",
         "item": "LabTalk save 会触发备份失败并损坏 OPJU",
         "why": "备份路径拼成 D:\\Origin\\Backup\\+完整路径；实测把 Fig05 存成 514KB 打不开。必须用 origin_save_project"},
        {"id": "C5", "level": "高",
         "item": "doc -o 会弹「更改保存到项目」阻塞 COM",
         "why": "项目脏标记不因插件保存而清除；用看门狗点「否」"},
        {"id": "C6", "level": "高",
         "item": "刻度标签字号 = label.fsize，不是 ticklabel.pt",
         "why": "用户看到的 18pt 数字就是 label.fsize"},
        {"id": "C7", "level": "高",
         "item": "刻度标签加粗只有 COM 通道有效",
         "why": "LabTalk 的 ticklabel.bold / .bt 是哑属性，写了不生效"},
        {"id": "C8", "level": "高",
         "item": "轴标题加粗必须先设 xb.font 再写 \\b()",
         "why": "顺序反了或之后被 edit_axis 重写标题，粗体都会丢"},
        {"id": "C9", "level": "中",
         "item": "\\b...\\b() 是错的，会显示字面量",
         "why": "正确写法是 \\b(文本)"},
        {"id": "C10", "level": "中",
         "item": "LabTalk 字符串里的 \\r\\n 变字面量 rn",
         "why": "图例文本必须用插件的 edit_legend(text=...) 接口"},
        {"id": "C11", "level": "中",
         "item": "正负值柱状图 x 轴落在 y=0，底部无框线",
         "why": "axis -ps X A 3 会多出右边/上边刻度；正确做法是 draw 画一条线"},
        {"id": "C12", "level": "低",
         "item": "端口 47632/48632 被 Windows 拦，49632+ 可用",
         "why": "桥接试验时 bind 报 WinError 10013"},
        {"id": "C13", "level": "低",
         "item": "Origin 内置 Python 3.8.3，有 originpro，无 numpy/pandas",
         "why": "走 LabTalk py.函数() 调它时只能力用标准库 + originpro"},
        {"id": "C14", "level": "高",
         "item": "py.函数() 能调内置 Python，但模块状态活不过一次调用",
         "why": "socket 桥接架构因此不可行（已实测：netstat 有监听，status 报 running=False）"},
    ],
    "watchdog": [
        "必须绕开 MCP（COM 线程被堵死时 MCP 调不动任何工具）",
        "纯 ctypes 枚举 #32770，不依赖 pywin32",
        "保存提示点「否」，其余点「确定」",
        "按钮文本带快捷键后缀「否(&N)」，要子串匹配",
    ],
}


def get_lessons(section=None):
    """返回本会话固化的经验库。

    Args:
        section: preferences | workflow | gotchas | watchdog；留空返回全部
    """
    if section:
        if section not in LESSONS:
            return {"ok": False, "error_code": "not_found",
                    "error": f"没有该分组: {section}",
                    "available": list(LESSONS)}
        return {"ok": True, "section": section, "items": LESSONS[section]}
    return {"ok": True, "total": sum(len(v) for v in LESSONS.values()),
            "sections": LESSONS}


def check_environment(engine=None):
    """自检：确认当前环境是否符合本会话确定的事实。

    检查项：插件部署路径 / Origin 实际安装位置 / 端口占用 / 看门狗存活。
    """
    import os
    import subprocess
    checks = []

    def ck(item, status, detail):
        checks.append({"item": item, "status": status, "detail": detail})

    # 插件实际加载路径（不写死用户名：以本文件位置为准）
    here = os.path.dirname(os.path.abspath(__file__))
    in_pkg = "dsh_origin_plugin" in here.replace("/", "\\") and "pkg" in here
    ck("plugin_path", "ok" if in_pkg else "warn",
       f"当前模块在 {here}；DSH 通常加载同目录下的 pkg/ 副本")

    # Origin 进程与路径
    origin_path = None
    try:
        import ctypes
        import ctypes.wintypes as wt
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        TH32 = 2

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                        ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                        ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                        ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]

        snap = k.CreateToolhelp32Snapshot(TH32, 0)
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        if k.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if "origin" in pe.szExeFile.lower():
                    h = k.OpenProcess(0x1000, False, pe.th32ProcessID)
                    if h:
                        b = ctypes.create_unicode_buffer(1024)
                        s2 = wt.DWORD(1024)
                        if k.QueryFullProcessImageNameW(h, 0, b, ctypes.byref(s2)):
                            origin_path = b.value
                        k.CloseHandle(h)
                    break
                if not k.Process32NextW(snap, ctypes.byref(pe)):
                    break
        k.CloseHandle(snap)
    except Exception:
        pass
    ck("origin_exe", "ok" if origin_path else "error",
       origin_path or "未找到 Origin64 进程")

    # 看门狗
    try:
        import origin_watchdog as wd
        st = wd.status()
        ck("watchdog", "ok" if st.get("running") else "warn", st.get("detail", ""))
    except Exception as exc:
        ck("watchdog", "warn", f"检查失败: {exc}")

    worst = "ok"
    if any(c["status"] == "error" for c in checks):
        worst = "error"
    elif any(c["status"] == "warn" for c in checks):
        worst = "warn"
    return {"ok": worst != "error", "status": worst, "checks": checks,
            "n_checks": len(checks)}
