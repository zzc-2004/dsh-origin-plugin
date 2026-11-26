# -*- coding: utf-8 -*-
"""排版预设档 —— 吸收自 origin-hands-off-thesis 的样式档 + youngminsw 的出版模板。

问题
----
同一课题组十几张图，字号、线宽、刻度长度各不相同，拼在一起就露馅。
把这些数值固化进一个 JSON，每张图开工前套一次，全篇统一。

档位
----
  thesis      博士论文插图（4:3 单图，Times New Roman，刻度 9 / 轴题 10.5 加粗）
  composite   手工拼好的多面板大图（期刊整页宽 17.5 cm，刻度 6.5 / 轴题 7.5）
  manuscript  期刊单栏（89 mm，刻度 6.5 / 轴题 7）
  nature      高影响期刊（刻度 6 / 轴题 7，无网格，细线 0.5 pt）

用法
----
    apply_preset("Graph1", "thesis")     # 一次套完字体/字号/线宽/刻度长度
"""
from __future__ import annotations

import json
import os

_STYLES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "knowledge", "thesis_styles.json")

# 补充档位（原文件只有 thesis/composite/manuscript）
_EXTRA = {
    "nature": {
        "label": "高影响期刊（Nature 系）",
        "description": "无网格、细线、小字号，单栏 89 mm / 双栏 183 mm",
        "style": {
            "font": "Times New Roman",
            "tick_pt": 6.0, "axis_title_pt": 7.0, "legend_pt": 6.0,
            "annot_pt": 6.0, "panel_pt": 8.0,
            "axis_thickness_pt": 0.5, "tick_thickness_pt": 0.5,
            "tick_length_pt": 2.5, "line_pt": 0.75, "symbol_pt": 3.0,
            "show_grids": 0,
        },
    },
}


def _resolve_profile(name, profiles, seen=None):
    """解析档位的 inherits/overrides 继承链，返回合并后的 style 字典。"""
    seen = seen or set()
    if name in seen or name not in profiles:
        return {}, {}
    seen.add(name)
    prof = profiles[name]
    style, layout = {}, {}
    parent = prof.get("inherits")
    if parent:
        style, layout = _resolve_profile(parent, profiles, seen)
        style = dict(style)
        layout = dict(layout)
    own = prof.get("style")
    if isinstance(own, dict):
        style.update(own)
    ov = prof.get("overrides")
    if isinstance(ov, dict):
        if isinstance(ov.get("style"), dict):
            style.update(ov["style"])
        for k, v in ov.items():
            if k != "style":
                layout[k] = v
    return style, layout


def list_presets() -> dict:
    """列出全部可用排版档（已解析 inherits 继承链）。"""
    out = {}
    default_font = "Times New Roman"
    if os.path.exists(_STYLES):
        with open(_STYLES, "r", encoding="utf-8") as f:
            d = json.load(f)
        default_font = (d.get("fonts") or {}).get("default") or default_font
        profiles = d.get("profiles") or {}
        for k, v in profiles.items():
            style, layout = _resolve_profile(k, profiles)
            out[k] = {"label": v.get("label"), "description": v.get("description"),
                      "style": style or None,
                      "inherits": v.get("inherits"),
                      "layout": layout or None,
                      "source": "thesis_styles.json"}
    for k, v in _EXTRA.items():
        out.setdefault(k, dict(v))
    for k in out:
        out[k].setdefault("source", "builtin")
    return {"ok": True, "n": len(out), "default_font": default_font,
            "presets": out}


def get_preset(name: str) -> dict:
    """取单个档位的完整样式值（含单位说明）。"""
    r = list_presets()
    p = (r.get("presets") or {}).get(str(name))
    if not p:
        return {"ok": False, "error_code": "not_found",
                "error": f"没有该排版档: {name}",
                "available": list((r.get("presets") or {}).keys())}
    return {"ok": True, "preset": name, **p,
            "note": "所有 *_pt 为磅值，其余为 cm"}


def apply_preset(engine, graph: str, name: str = "thesis") -> dict:
    """把排版档套到图上：字体族 + 各级字号 + 轴线与刻度 + 线宽。

    依赖 engine.set_font / engine.edit_axis / engine.edit_page 三个底层能力。
    """
    p = get_preset(name)
    if not p.get("ok"):
        return p
    st = p.get("style") or {}
    if not st:
        return {"ok": False, "error_code": "preset_empty",
                "error": f"排版档 {name} 没有可用的样式值"}
    applied, failed = [], []

    font = st.get("font", "Times New Roman")
    axis_pt = st.get("axis_title_pt") or st.get("title_pt") or 10
    tick_pt = st.get("tick_pt") or st.get("series_pt") or 9
    legend_pt = st.get("legend_pt") or 9

    # 1) 字体族：先整体设成档位字体（字号后面按角色分别设）
    r1 = engine.set_font(graph, family=font, size_pt=None)
    (applied if r1.get("ok") else failed).append({"step": "font_family",
                                                  "result": r1})

    # 2) 轴标题字号 / 刻度字号（按角色分开设，set_font 的 size 会一刀切）
    for ax in ("x", "y"):
        r = engine.edit_axis(graph, axis=ax, layer=0, title="",
                             from_value=None, to_value=None, scale=None,
                             props={"label_font_pt": axis_pt})
        (applied if r.get("ok") else failed).append(
            {"step": f"{ax}_title_pt={axis_pt}", "result": r})
    r2 = engine.edit_legend(graph, {"font_size_pt": legend_pt})
    (applied if r2.get("ok") else failed).append({"step": "legend_pt", "result": r2})

    # 3) 刻度长度
    for ax in ("x", "y"):
        r = engine.edit_axis(graph, axis=ax, layer=0, title="",
                             from_value=None, to_value=None, scale=None,
                             props={"tick_length": int(round(st.get("tick_length_pt", 4) * 2))})
        (applied if r.get("ok") else failed).append(
            {"step": f"{ax}_tick_length", "result": r})

    # 4) 网格
    if "show_grids" in st:
        for ax in ("x", "y"):
            engine.edit_axis(graph, axis=ax, layer=0, title="",
                             from_value=None, to_value=None, scale=None,
                             props={"show_grids": int(st["show_grids"])})

    # 5) 线宽（逐条曲线）
    lw = st.get("line_pt")
    if lw:
        try:
            edits = [{"plot": i, "line_width_pt": float(lw)} for i in range(12)]
            r3 = engine.edit_plot(graph, edits)
            applied.append({"step": f"line_width={lw}", "result": r3})
        except Exception as exc:
            failed.append({"step": "line_width", "error": str(exc)[:120]})

    return {"ok": len(failed) == 0, "graph": graph, "preset": name,
            "font": font, "axis_title_pt": axis_pt, "tick_pt": tick_pt,
            "legend_pt": legend_pt, "applied": applied, "failed": failed,
            "detail": f"排版档 {name} 已套用：{len(applied)} 项成功"
                      f"{'，' + str(len(failed)) + ' 项失败' if failed else ''}"}
