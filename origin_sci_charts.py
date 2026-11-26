# -*- coding: utf-8 -*-
"""
origin_sci_charts —— 发表级多样化科研图表引擎（v2.8.0 新增）
==============================================================

解决传统脚本只会画单调柱状图(column)与普通散点图(scatter)的问题，基于本机
Origin 2021+ (9.8+) 真机验证过的原生模板与原语，提供 8 类高辨识度科研图表：

1. horizontal_forest    —— 横向多组/多面板置信区间森林图（实心/空心点 + 带帽须线 + 零线）
2. diverging_lollipop   —— 横向正负双向棒棒糖图（双向配色杆 + 端点符号 + 汇总菱形，支持单/双面板）
3. dumbbell_pair        —— 成对连接哑铃图（基线→改进连桥 + 双态符号，支持单/双面板）
4. ribbon_profile       —— 阴影置信带剖面曲线图（FILLAREA 上下界阴影带 + 均值符号线）
5. annotated_heatmap    —— 2D 矩阵热力图 + 自定义 RIFF .pal 科学色板 + 色标 + 格内数值标注（修复 D8）
6. raincloud            —— 云雨图 / 半小提琴核密度+散点堆叠图（HalfViolin.otpu / ViolinBox.otpu，支持多面板）
7. marginal_calibration —— 边缘地毯散点 + 分位数校准曲线 + 1:1 参考线（ScatterRug.otpu）
8. radar_chart          —— 多维雷达/蜘蛛网对比图（Radar 模板）

严格遵循项目工程铁律：
- 内部 `_xxx_impl(opmod, ...)` 仅在 COM 线程内运行，绝不递归投递 `_run_on_com_thread`；
- 绝不调用在 Origin 2021 会触发原生崩溃的 `add_plot(..., colyerr=...)`；
- 所有分类刻度均通过工作表列绑定（`layer.x/y.label.type=2`），在 OPJU 中 100% 可编辑；
- 页面短名严格控制在 ≤11 ASCII 字符，同时设置完整可读 `lname`。
"""
from __future__ import annotations

import math
import os
import struct
from pathlib import Path
from typing import Any

import numpy as np

import origin_engine as engine
import origin_errors as oerr

NAN = float("nan")

# 预设科学 RIFF .pal 256 阶色板（自动写入 Origin User Files/Palettes）
SCI_PALETTES: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
    "teal": [
        (0.00, (242, 248, 249)),
        (0.30, (186, 219, 224)),
        (0.65, (72, 146, 158)),
        (1.00, (17, 68, 84)),
    ],
    "rdbu_r": [
        (0.00, (24, 74, 128)),
        (0.28, (124, 174, 212)),
        (0.50, (247, 247, 247)),
        (0.72, (225, 145, 130)),
        (1.00, (165, 48, 46)),
    ],
    "slate_amber": [
        (0.00, (245, 247, 249)),
        (0.40, (166, 189, 206)),
        (0.75, (65, 114, 148)),
        (1.00, (25, 58, 88)),
    ],
    "emerald_warm": [
        (0.00, (248, 246, 240)),
        (0.35, (190, 218, 208)),
        (0.70, (56, 136, 122)),
        (1.00, (20, 74, 66)),
    ],
}


# =============================================================================
# 底层公共工具（仅在 COM 线程内调用）
# =============================================================================
def _ensure_op():
    ok, info = engine._connect_impl()
    if not ok:
        raise RuntimeError(info.get("error", "无法连接 Origin COM 服务"))
    import originpro as opmod
    return opmod


def _clean_short(name: str, fallback: str = "SciFig") -> str:
    s = "".join(ch for ch in (name or "") if ch.isalnum() or ch == "_")
    if not s or not s[0].isalpha():
        s = "F_" + s
    return s[:11] if s else fallback


def _new_graph_safe(opmod, short_name: str, long_name: str = "", template: str | None = None):
    sname = _clean_short(short_name)
    # 若已有同名图页，按 2.7.5 安全规则改名备份，绝不静默误删用户图页
    existing = opmod.find_graph(sname)
    if existing is not None:
        try:
            existing.name = _clean_short(f"bk_{sname[:7]}")
        except Exception:
            pass
    if template:
        gp = opmod.new_graph(lname=long_name or sname, template=template)
    else:
        gp = opmod.new_graph(lname=long_name or sname)
    try:
        gp.name = sname
    except Exception:
        pass
    if long_name:
        try:
            gp.lname = long_name
        except Exception:
            pass
    return gp


def _new_sheet_safe(opmod, short_name: str, long_name: str, cols: list[tuple[str, list[Any], str]]):
    """创建工作表并写入列：cols = [(col_lname, values_list, axis_role), ...]。"""
    bshort = _clean_short("W" + short_name, "WData")
    w = opmod.new_sheet("w", lname=long_name or bshort)
    try:
        w.get_book().name = bshort
        w.get_book().lname = long_name or bshort
    except Exception:
        pass
    w.cols = len(cols)
    for idx, (clabel, vals, role) in enumerate(cols):
        w.from_list(idx, list(vals), lname=clabel, axis=role)
    return w


def _set_page_cm(opmod, gp, width_cm: float = 12.0, height_cm: float = 9.0) -> tuple[float, float]:
    gp.activate()
    rx = opmod.lt_float("page.resx")
    ry = opmod.lt_float("page.resy")
    rx = rx if (isinstance(rx, (int, float)) and math.isfinite(rx) and rx > 0) else 600.0
    ry = ry if (isinstance(ry, (int, float)) and math.isfinite(ry) and ry > 0) else 600.0
    gp.set_int("width", round(width_cm * rx / 2.54))
    gp.set_int("height", round(height_cm * ry / 2.54))
    return (
        round(gp.get_int("width") * 2.54 / rx, 3),
        round(gp.get_int("height") * 2.54 / ry, 3),
    )


def _style_layer(
    opmod,
    gl,
    x_title: str | None = None,
    y_title: str | None = None,
    rect_pct: tuple[float, float, float, float] | None = (22, 12, 72, 71),
    font_pt: float = 10.0,
    four_frame: bool = True,
) -> None:
    gl.activate()
    if rect_pct is not None:
        gl.set_int("unit", 1)
        for prop, val in zip(("left", "top", "width", "height"), rect_pct):
            gl.set_float(prop, float(val))
    font = int(opmod.lt_float('font("Times New Roman")'))
    for ax in ("x", "y"):
        gl.set_int(f"{ax}.opposite", 1 if four_frame else 0)
        gl.set_int(f"{ax}.ticks", 1)
        gl.set_int(f"{ax}2.ticks", 0)
        gl.set_int(f"{ax}.label.font", font)
        gl.set_float(f"{ax}.label.fsize", font_pt)
        gl.set_int(f"{ax}.label.bold", 1)
        gl.lt_exec(f"layer.{ax}.thickness = 1.2; layer.{ax}2.thickness = 1.2;")
    for key, title in [("xb", x_title), ("yl", y_title)]:
        if title is None:
            continue
        lbl = gl.label(key)
        if lbl is None:
            continue
        if title == "":
            gl.remove_label(lbl)
            continue
        lbl.set_int("font", font)
        lbl.text = f"\\b({title})"
        lbl.set_float("fsize", font_pt)


def _bind_cat_axis(gl, w, col_idx: int, axis: str = "y") -> None:
    ds = f"{w.get_book().name}_{w.obj[col_idx].GetName()}"
    gl.activate()
    gl.lt_exec(f'layer.{axis}.label.type=2; layer.{axis}.label.dataset$="{ds}";')


def _set_custom_legend(
    opmod,
    gl,
    entries: list[tuple[int, str]] | None,
    left_pct: float = 50.0,
    top_pct: float = 3.5,
    font_pt: float = 9.5,
    horizontal: bool = True,
) -> None:
    gl.activate()
    lg = gl.label("Legend")
    if not entries:
        if lg is not None:
            gl.remove_label(lg)
        return
    if lg is None:
        gl.lt_exec("legend -r;")
        lg = gl.label("Legend")
    if lg is None:
        return
    sep = "      " if horizontal else "\n"
    lg.text = sep.join(f"\\b(\\l({idx}) {txt})" for idx, txt in entries)
    font = int(opmod.lt_float('font("Times New Roman")'))
    lg.set_int("font", font)
    lg.set_float("fsize", font_pt)
    lg.set_int("showframe", 0)
    lg.set_int("background", 0)
    pw = opmod.lt_float("page.width")
    ph = opmod.lt_float("page.height")
    lg.set_int("left", round(pw * left_pct / 100.0 - lg.get_int("width") / 2.0))
    lg.set_int("top", round(ph * top_pct / 100.0))


def _add_page_label(
    opmod,
    gl,
    name: str,
    text_markup: str,
    left_pct: float,
    top_pct: float,
    font_pt: float = 10.5,
    color: str = "#1E242B",
    center_x: bool = False,
) -> None:
    gl.activate()
    old = gl.label(name)
    if old is not None:
        gl.remove_label(old)
    lbl = gl.add_label(text_markup, 0, 0)
    try:
        lbl.name = name
    except Exception:
        pass
    font = int(opmod.lt_float('font("Times New Roman")'))
    lbl.set_int("font", font)
    lbl.set_float("fsize", font_pt)
    lbl.color = color
    lbl.set_int("showframe", 0)
    lbl.set_int("background", 0)
    pw = opmod.lt_float("page.width")
    ph = opmod.lt_float("page.height")
    lx = round(pw * left_pct / 100.0 - (lbl.get_int("width") / 2.0 if center_x else 0.0))
    ly = round(ph * top_pct / 100.0)
    lbl.set_int("left", lx)
    lbl.set_int("top", ly)


def _add_ref_line(gl, x0: float, y0: float, x1: float, y1: float, color: str = "#7A8691", width: float = 0.85, dash: int = 2) -> None:
    ln = gl.add_line(x0, y0, x1, y1)
    ln.color = color
    ln.width = width
    ln.type = dash  # 1=solid, 2=dash, 3=dot


def _segments_to_xy(segs: list[tuple[float, float, float, float]]) -> tuple[list[float], list[float]]:
    xs: list[float] = []
    ys: list[float] = []
    for x0, y0, x1, y1 in segs:
        xs.extend([float(x0), float(x1), NAN])
        ys.extend([float(y0), float(y1), NAN])
    return xs, ys


def _hex_to_lt_color(hex_str: str) -> str:
    h = (hex_str or "#888888").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"color({r},{g},{b})"


def _ensure_riff_palette(opmod, palette_name: str = "teal") -> str:
    stops = SCI_PALETTES.get(palette_name, SCI_PALETTES["teal"])
    xs = np.linspace(0.0, 1.0, 256)
    sp = [s[0] for s in stops]
    rs = np.interp(xs, sp, [s[1][0] for s in stops]).round().astype(int)
    gs = np.interp(xs, sp, [s[1][1] for s in stops]).round().astype(int)
    bs = np.interp(xs, sp, [s[1][2] for s in stops]).round().astype(int)
    entries = b"".join(struct.pack("BBBB", int(r), int(g), int(b), 0) for r, g, b in zip(rs, gs, bs))
    data_chunk = b"data" + struct.pack("<IHH", 4 + len(entries), 0x0300, 256) + entries
    riff = b"RIFF" + struct.pack("<I", 4 + len(data_chunk)) + b"PAL " + data_chunk
    pdir = Path(opmod.path("u")) / "Palettes"
    pdir.mkdir(parents=True, exist_ok=True)
    out_p = pdir / f"Dsh_{palette_name}.pal"
    out_p.write_bytes(riff)
    return str(out_p)


def _export_outputs(gp, file_path: str | None = None, pdf_path: str | None = None, width: int = 1800) -> dict[str, Any]:
    res: dict[str, Any] = {}
    gp.activate()
    if file_path:
        fp = Path(file_path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        gp.save_fig(str(fp), type="png", width=int(width))
        res["file"] = str(fp)
        res["size"] = fp.stat().st_size if fp.exists() else 0
    if pdf_path:
        pp = Path(pdf_path)
        pp.parent.mkdir(parents=True, exist_ok=True)
        gp.save_fig(str(pp), type="pdf")
        res["pdf_file"] = str(pp)
        res["pdf_size"] = pp.stat().st_size if pp.exists() else 0
    return res


# =============================================================================
# 1. 横向多组/多面板置信区间森林图 (horizontal_forest)
# =============================================================================
def _forest_horizontal_impl(
    opmod,
    panels: list[dict[str, Any]],
    short_name: str = "SciForest",
    long_name: str = "Horizontal Forest Plot",
    page_cm: tuple[float, float] = (16.0, 8.6),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    """panels: 1 或 2 个面板字典：
    {
      "panel_title": "(a) nBDI",
      "categories": ["Interior · Void", ...],
      "x_title": "Change in nBDI (10\\+(-3))",
      "x_lim": (-12, 22, 5),
      "zero_line": 0.0,
      "divider_y": 2.5,  # 可选分组水平分隔线
      "series": [
        {
          "name": "Loose",
          "y_pos": [2, 4] 或与 categories 等长 + dodged offset,
          "est": [...], "ci_low": [...], "ci_high": [...],
          "color": "#1F6F78", "shape": 2, "open_symbol": False, "cap": 0.07
        }, ...
      ]
    }
    """
    n_pan = len(panels)
    cols_spec: list[tuple[str, list[Any], str]] = []
    panel_col_map: list[dict[str, Any]] = []

    for pidx, pan in enumerate(panels):
        cats = list(pan["categories"])
        n_cat = len(cats)
        cat_col = len(cols_spec)
        cols_spec.append((f"P{pidx+1} Category", cats, "X"))
        row_col = len(cols_spec)
        cols_spec.append((f"P{pidx+1} Row", list(range(1, n_cat + 1)), "Y"))

        s_maps = []
        for sidx, sr in enumerate(pan["series"]):
            ys = list(sr["y_pos"])
            est = list(sr["est"])
            lo = list(sr["ci_low"])
            hi = list(sr["ci_high"])
            cap = float(sr.get("cap", 0.075))
            segs = []
            for yv, l_v, h_v in zip(ys, lo, hi):
                if l_v is None or h_v is None or (isinstance(l_v, float) and math.isnan(l_v)):
                    continue
                segs.extend([
                    (l_v, yv, h_v, yv),
                    (l_v, yv - cap, l_v, yv + cap),
                    (h_v, yv - cap, h_v, yv + cap),
                ])
            wx, wy = _segments_to_xy(segs)
            c_est = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} Est", est, "X"))
            c_yp = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} Y", ys, "Y"))
            c_lo = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} CI_low", [v if v is not None else NAN for v in lo], "Y"))
            c_hi = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} CI_high", [v if v is not None else NAN for v in hi], "Y"))
            c_wx = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} WhiskX", wx, "X"))
            c_wy = len(cols_spec)
            cols_spec.append((f"P{pidx+1} {sr['name']} WhiskY", wy, "Y"))
            s_maps.append({
                "sr": sr, "c_est": c_est, "c_yp": c_yp,
                "c_wx": c_wx, "c_wy": c_wy, "has_whisk": len(wx) > 0,
            })
        panel_col_map.append({"cat_col": cat_col, "n_cat": n_cat, "s_maps": s_maps, "pan": pan})

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name)
    dims = _set_page_cm(opmod, gp, *page_cm)

    for pidx, pmap in enumerate(panel_col_map):
        gl = gp[0] if pidx == 0 else gp.add_layer()
        gl.activate()
        pan = pmap["pan"]
        n_cat = pmap["n_cat"]

        legend_entries: list[tuple[int, str]] = []
        plot_counter = 0
        for sm in pmap["s_maps"]:
            sr = sm["sr"]
            col_hex = sr.get("color", "#1F6F78")
            if sm["has_whisk"]:
                pw_line = gl.add_plot(w, sm["c_wy"], sm["c_wx"], type="l")
                pw_line.color = col_hex
                pw_line.set_cmd(f"-wp {sr.get('whisk_width', 1.5)}")
                plot_counter += 1
            ps = gl.add_plot(w, sm["c_yp"], sm["c_est"], type="s")
            ps.color = col_hex
            ps.symbol_kind = int(sr.get("shape", 2))
            ps.symbol_size = float(sr.get("size", 7.5))
            if sr.get("open_symbol", False):
                ps.set_cmd("-kf 1", "-kh 22")  # 白底纯空心符号 + 加粗彩色描边
            else:
                ps.symbol_interior = 1
            plot_counter += 1
            if sr.get("in_legend", True):
                legend_entries.append((plot_counter, sr["name"]))

        xlim = pan.get("x_lim")
        if xlim:
            gl.set_xlim(*xlim)
        ylim = pan.get("y_lim", (0.45, n_cat + 0.55, 1))
        gl.set_ylim(*ylim)

        if n_pan == 1:
            rect = pan.get("rect_pct", (26, 16, 68, 66))
        else:
            rect = pan.get("rect_pct", (21, 17, 34, 65) if pidx == 0 else (62, 17, 34, 65))

        y_t = pan.get("y_title", "") if (pidx == 0 or pan.get("show_y_labels", False)) else ""
        _style_layer(opmod, gl, pan.get("x_title", ""), y_t, rect_pct=rect, font_pt=9.5)
        if pidx == 0 or pan.get("show_y_labels", False):
            _bind_cat_axis(gl, w, pmap["cat_col"], axis="y")
        else:
            gl.lt_exec("layer.y.showLabels = 0;")

        if pan.get("zero_line") is not None:
            z0 = float(pan["zero_line"])
            _add_ref_line(gl, z0, ylim[0], z0, ylim[1], color="#5B6670", width=0.95, dash=2)
        if pan.get("divider_y") is not None and xlim:
            dy = float(pan["divider_y"])
            _add_ref_line(gl, xlim[0], dy, xlim[1], dy, color="#D0D7DE", width=0.75, dash=1)

        if pan.get("panel_title"):
            _add_page_label(opmod, gl, f"PTitle{pidx}", f"\\b({pan['panel_title']})", rect[0], rect[1] - 7.5, font_pt=10.2)

        if pidx == 0 and legend_entries and pan.get("show_legend", True):
            lx = 54.0 if n_pan > 1 else (rect[0] + rect[2] / 2.0)
            _set_custom_legend(opmod, gl, legend_entries, left_pct=lx, top_pct=3.5, font_pt=9.5)
        else:
            _set_custom_legend(opmod, gl, None)

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "horizontal_forest", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": n_pan, **exp}


# =============================================================================
# 2. 横向正负双向棒棒糖图 (diverging_lollipop)
# =============================================================================
def _diverging_lollipop_impl(
    opmod,
    panels: list[dict[str, Any]],
    short_name: str = "SciLolli",
    long_name: str = "Diverging Lollipop Plot",
    page_cm: tuple[float, float] = (16.0, 9.8),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    n_pan = len(panels)
    cols_spec: list[tuple[str, list[Any], str]] = []
    pmaps = []

    for pidx, pan in enumerate(panels):
        cats = list(pan["categories"])
        vals = [float(v) for v in pan["values"]]
        pooled_idx = pan.get("pooled_row_index")  # 可选：如最后一行是 Pooled 汇总菱形
        n_rows = len(cats)
        ix = list(range(1, n_rows + 1))

        pos_seg, neg_seg = [], []
        pos_x, pos_y, neg_x, neg_y = [], [], [], []
        pool_x, pool_y = [], []

        for r_i, (yv, v) in enumerate(zip(ix, vals)):
            if pooled_idx is not None and r_i == pooled_idx:
                pool_x.append(v)
                pool_y.append(yv)
                continue
            if v >= 0:
                pos_seg.append((0.0, yv, v, yv))
                pos_x.append(v)
                pos_y.append(yv)
            else:
                neg_seg.append((0.0, yv, v, yv))
                neg_x.append(v)
                neg_y.append(yv)

        psx, psy = _segments_to_xy(pos_seg)
        nsx, nsy = _segments_to_xy(neg_seg)

        c_cat = len(cols_spec); cols_spec.append((f"P{pidx+1} Line", cats, "X"))
        c_row = len(cols_spec); cols_spec.append((f"P{pidx+1} Row", ix, "Y"))
        c_val = len(cols_spec); cols_spec.append((f"P{pidx+1} Value", vals, "X"))
        c_psx = len(cols_spec); cols_spec.append((f"P{pidx+1} PosStemX", psx or [NAN], "X"))
        c_psy = len(cols_spec); cols_spec.append((f"P{pidx+1} PosStemY", psy or [NAN], "Y"))
        c_px  = len(cols_spec); cols_spec.append((f"P{pidx+1} PosDotX", pos_x or [NAN], "X"))
        c_py  = len(cols_spec); cols_spec.append((f"P{pidx+1} PosDotY", pos_y or [NAN], "Y"))
        c_nsx = len(cols_spec); cols_spec.append((f"P{pidx+1} NegStemX", nsx or [NAN], "X"))
        c_nsy = len(cols_spec); cols_spec.append((f"P{pidx+1} NegStemY", nsy or [NAN], "Y"))
        c_nx  = len(cols_spec); cols_spec.append((f"P{pidx+1} NegDotX", neg_x or [NAN], "X"))
        c_ny  = len(cols_spec); cols_spec.append((f"P{pidx+1} NegDotY", neg_y or [NAN], "Y"))
        c_plx = len(cols_spec); cols_spec.append((f"P{pidx+1} PoolX", pool_x or [NAN], "X"))
        c_ply = len(cols_spec); cols_spec.append((f"P{pidx+1} PoolY", pool_y or [NAN], "Y"))

        pmaps.append({
            "pan": pan, "n_rows": n_rows, "c_cat": c_cat,
            "c_psx": c_psx, "c_psy": c_psy, "c_px": c_px, "c_py": c_py,
            "c_nsx": c_nsx, "c_nsy": c_nsy, "c_nx": c_nx, "c_ny": c_ny,
            "c_plx": c_plx, "c_ply": c_ply,
            "has_neg": len(neg_x) > 0, "has_pool": len(pool_x) > 0,
            "pool_val": pool_x[0] if pool_x else None,
            "pool_y": pool_y[0] if pool_y else None,
        })

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name)
    dims = _set_page_cm(opmod, gp, *page_cm)

    for pidx, pm in enumerate(pmaps):
        gl = gp[0] if pidx == 0 else gp.add_layer()
        gl.activate()
        pan = pm["pan"]
        pos_col = pan.get("pos_color", "#1F7A79")
        neg_col = pan.get("neg_color", "#B64342")
        pool_col = pan.get("pool_color", "#1B365D")

        p1 = gl.add_plot(w, pm["c_psy"], pm["c_psx"], type="l")
        p1.color = pos_col; p1.set_cmd("-wp 2.0")
        p2 = gl.add_plot(w, pm["c_py"], pm["c_px"], type="s")
        p2.color = pos_col; p2.symbol_kind = 2; p2.symbol_size = 6.8; p2.symbol_interior = 1

        if pm["has_neg"]:
            p3 = gl.add_plot(w, pm["c_nsy"], pm["c_nsx"], type="l")
            p3.color = neg_col; p3.set_cmd("-wp 2.0")
            p4 = gl.add_plot(w, pm["c_ny"], pm["c_nx"], type="s")
            p4.color = neg_col; p4.symbol_kind = 2; p4.symbol_size = 6.8; p4.symbol_interior = 1

        if pm["has_pool"]:
            p5 = gl.add_plot(w, pm["c_ply"], pm["c_plx"], type="s")
            p5.color = pool_col; p5.symbol_kind = 5; p5.symbol_size = 9.0; p5.symbol_interior = 1

        xlim = pan.get("x_lim", (-6, 48, 10))
        ylim = (0.4, pm["n_rows"] + 0.6, 1)
        gl.set_xlim(*xlim)
        gl.set_ylim(*ylim)

        if n_pan == 1:
            rect = pan.get("rect_pct", (22, 13, 72, 71))
        else:
            rect = pan.get("rect_pct", (18, 14, 36, 70) if pidx == 0 else (60, 14, 36, 70))

        _style_layer(opmod, gl, pan.get("x_title", "MSE reduction (%)"), "" if pidx > 0 else pan.get("y_title", ""), rect_pct=rect, font_pt=9.2)
        if pidx == 0:
            _bind_cat_axis(gl, w, pm["c_cat"], axis="y")
        else:
            gl.lt_exec("layer.y.showLabels = 0;")

        _add_ref_line(gl, 0.0, ylim[0], 0.0, ylim[1], color="#4E5964", width=0.95, dash=2)
        if pan.get("divider_y") is not None:
            dy = float(pan["divider_y"])
            _add_ref_line(gl, xlim[0], dy, xlim[1], dy, color="#CBD3DA", width=0.8, dash=1)

        if pan.get("panel_title"):
            _add_page_label(opmod, gl, f"LTitle{pidx}", f"\\b({pan['panel_title']})", rect[0], rect[1] - 7.0, font_pt=10.0)

        if pm["has_pool"] and pan.get("pool_label"):
            lbl = gl.add_label(f"\\b({pan['pool_label']})", pm["pool_val"] + (xlim[1] - xlim[0]) * 0.04, pm["pool_y"] + 0.18)
            font = int(opmod.lt_float('font("Times New Roman")'))
            lbl.set_int("font", font); lbl.set_float("fsize", 9.0); lbl.color = pool_col
            lbl.set_int("showframe", 0); lbl.set_int("background", 0)

        _set_custom_legend(opmod, gl, None)

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "diverging_lollipop", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": n_pan, **exp}


# =============================================================================
# 3. 成对连接哑铃图 (dumbbell_pair)
# =============================================================================
def _dumbbell_pair_impl(
    opmod,
    panels: list[dict[str, Any]],
    short_name: str = "SciDumb",
    long_name: str = "Connected Dumbbell Plot",
    page_cm: tuple[float, float] = (16.0, 8.6),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    n_pan = len(panels)
    cols_spec: list[tuple[str, list[Any], str]] = []
    pmaps = []

    for pidx, pan in enumerate(panels):
        cats = list(pan["categories"])
        v_a = [float(x) for x in pan["val_a"]]
        v_b = [float(x) for x in pan["val_b"]]
        ix = list(range(1, len(cats) + 1))
        bx, by = _segments_to_xy([(a, yv, b, yv) for a, b, yv in zip(v_a, v_b, ix)])

        c_cat = len(cols_spec); cols_spec.append((f"P{pidx+1} Cohort", cats, "X"))
        c_row = len(cols_spec); cols_spec.append((f"P{pidx+1} Row", ix, "Y"))
        c_va  = len(cols_spec); cols_spec.append((f"P{pidx+1} {pan.get('name_a','Baseline')}", v_a, "X"))
        c_vb  = len(cols_spec); cols_spec.append((f"P{pidx+1} {pan.get('name_b','Improved')}", v_b, "X"))
        c_bx  = len(cols_spec); cols_spec.append((f"P{pidx+1} BridgeX", bx, "X"))
        c_by  = len(cols_spec); cols_spec.append((f"P{pidx+1} BridgeY", by, "Y"))
        pmaps.append({"pan": pan, "n_rows": len(cats), "c_cat": c_cat, "c_row": c_row,
                      "c_va": c_va, "c_vb": c_vb, "c_bx": c_bx, "c_by": c_by, "va": v_a, "vb": v_b, "ix": ix})

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name)
    dims = _set_page_cm(opmod, gp, *page_cm)

    for pidx, pm in enumerate(pmaps):
        gl = gp[0] if pidx == 0 else gp.add_layer()
        gl.activate()
        pan = pm["pan"]
        col_a = pan.get("color_a", "#64727D")
        col_b = pan.get("color_b", "#1E7A79")
        col_br = pan.get("bridge_color", "#9CC2C2")

        p_br = gl.add_plot(w, pm["c_by"], pm["c_bx"], type="l")
        p_br.color = col_br; p_br.set_cmd("-wp 2.8")

        pa = gl.add_plot(w, pm["c_row"], pm["c_va"], type="s")
        pa.color = col_a; pa.symbol_kind = 2; pa.symbol_size = 7.8
        pa.set_cmd("-kf 1", "-kh 22")

        pb = gl.add_plot(w, pm["c_row"], pm["c_vb"], type="s")
        pb.color = col_b; pb.symbol_kind = 1; pb.symbol_size = 7.8; pb.symbol_interior = 1

        xlim = pan.get("x_lim", (0.0, 1.05, 0.2))
        ylim = (0.45, pm["n_rows"] + 0.55, 1)
        gl.set_xlim(*xlim)
        gl.set_ylim(*ylim)

        if n_pan == 1:
            rect = pan.get("rect_pct", (28, 18, 66, 63))
        else:
            rect = pan.get("rect_pct", (23, 19, 33, 62) if pidx == 0 else (62, 19, 34, 62))

        _style_layer(opmod, gl, pan.get("x_title", ""), "", rect_pct=rect, font_pt=9.4)
        if pidx == 0:
            _bind_cat_axis(gl, w, pm["c_cat"], axis="y")
        else:
            gl.lt_exec("layer.y.showLabels = 0;")

        if pan.get("divider_y") is not None:
            dy = float(pan["divider_y"])
            _add_ref_line(gl, xlim[0], dy, xlim[1], dy, color="#D0D7DE", width=0.8, dash=1)

        # 可选端值标注（如 +0.48 增量或命中分数 7/8）
        badge_texts = pan.get("right_annotations")
        if badge_texts:
            font = int(opmod.lt_float('font("Times New Roman")'))
            for yv, vb_v, btxt in zip(pm["ix"], pm["vb"], badge_texts):
                if not btxt:
                    continue
                lbl = gl.add_label(f"\\b({btxt})", vb_v + (xlim[1] - xlim[0]) * 0.035, yv + 0.14)
                lbl.set_int("font", font); lbl.set_float("fsize", 8.8); lbl.color = col_b
                lbl.set_int("showframe", 0); lbl.set_int("background", 0)

        if pan.get("panel_title"):
            _add_page_label(opmod, gl, f"DTitle{pidx}", f"\\b({pan['panel_title']})", rect[0], rect[1] - 7.5, font_pt=10.0)

        if pidx == 0:
            lx = 55.0 if n_pan > 1 else (rect[0] + rect[2] / 2.0)
            _set_custom_legend(opmod, gl, [(2, pan.get("name_a", "Context only")), (3, pan.get("name_b", "Context + coverage"))], left_pct=lx, top_pct=3.5, font_pt=9.5)
        else:
            _set_custom_legend(opmod, gl, None)

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "dumbbell_pair", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": n_pan, **exp}


# =============================================================================
# 4. 阴影置信带剖面曲线图 (ribbon_profile)
# =============================================================================
def _ribbon_profile_impl(
    opmod,
    x_vals: list[float],
    series_list: list[dict[str, Any]],
    x_title: str = "Sensor offset (m)",
    y_title: str = "Change in D(r)/D\\-(0) per +10 pp",
    x_lim: tuple[float, float, float] | None = None,
    y_lim: tuple[float, float, float] | None = None,
    zero_line: float | None = 0.0,
    short_name: str = "SciRibbon",
    long_name: str = "Shaded Ribbon Profile",
    page_cm: tuple[float, float] = (12.8, 9.2),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    """用闭合多边形包络 (-pf 1 -pfv 2) 绘制真正半透明彩色 95% CI 置信带 + 粗均值符号主线。"""
    xv = [float(v) for v in x_vals]
    cols_spec: list[tuple[str, list[Any], str]] = [("X", xv, "X")]
    s_meta = []
    for sr in series_list:
        name = sr["name"]
        est = [float(v) for v in sr["est"]]
        lo = [float(v) for v in sr["ci_low"]]
        hi = [float(v) for v in sr["ci_high"]]
        poly_x = xv + list(reversed(xv)) + [xv[0]]
        poly_y = hi + list(reversed(lo)) + [hi[0]]
        c_px = len(cols_spec); cols_spec.append((f"{name} PolyX", poly_x, "X"))
        c_py = len(cols_spec); cols_spec.append((f"{name} PolyY", poly_y, "Y"))
        c_hi = len(cols_spec); cols_spec.append((f"{name} CI_high", hi, "Y"))
        c_lo = len(cols_spec); cols_spec.append((f"{name} CI_low", lo, "Y"))
        c_m  = len(cols_spec); cols_spec.append((f"{name}", est, "Y"))
        s_meta.append({"sr": sr, "c_px": c_px, "c_py": c_py, "c_hi": c_hi, "c_lo": c_lo, "c_m": c_m})

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name)
    dims = _set_page_cm(opmod, gp, *page_cm)
    gl = gp[0]
    gl.activate()

    # 先画所有半透明闭合置信带包络（底层），再画所有均值主线在最顶层！
    plot_idx = 0
    for sm in s_meta:
        sr = sm["sr"]
        fill_hex = sr.get("fill_color", "#DCE8F2")
        edge_hex = sr.get("edge_color", sr.get("color", fill_hex))
        lt_fc = _hex_to_lt_color(fill_hex)
        ppoly = gl.add_plot(w, sm["c_py"], sm["c_px"], type="l")
        ppoly.color = edge_hex
        ppoly.set_cmd("-pf 1", "-pfv 2", f"-cf {lt_fc}", f"-pfb {lt_fc}", "-wp 0.55", "-d 3")
        ppoly.transparency = int(sr.get("fill_transparency", 58))
        plot_idx += 1

    legend_entries: list[tuple[int, str]] = []
    for k, sm in enumerate(s_meta):
        sr = sm["sr"]
        pm = gl.add_plot(w, sm["c_m"], 0, type="y")
        pm.color = sr.get("color", "#1F5A8A")
        pm.set_cmd(f"-wp {sr.get('line_width', 2.2)}")
        pm.symbol_kind = int(sr.get("shape", k + 1))
        pm.symbol_size = float(sr.get("size", 7.2))
        if sr.get("open_symbol", False):
            pm.set_cmd("-kf 1", "-kh 22")
        else:
            pm.symbol_interior = 1
        plot_idx += 1
        legend_entries.append((plot_idx, sr["name"]))

    if x_lim:
        gl.set_xlim(*x_lim)
    else:
        gl.rescale("x")
    if y_lim:
        gl.set_ylim(*y_lim)
    else:
        gl.rescale("y")

    _style_layer(opmod, gl, x_title, y_title, rect_pct=(21, 14, 73, 69), font_pt=10.0)
    if zero_line is not None:
        xl = gl.xlim
        _add_ref_line(gl, xl[0], float(zero_line), xl[1], float(zero_line), color="#5B6670", width=0.95, dash=2)

    _set_custom_legend(opmod, gl, legend_entries, left_pct=57.0, top_pct=3.2, font_pt=9.5, horizontal=True)
    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "ribbon_profile", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": 1, **exp}


# =============================================================================
# 5. 2D 矩阵热力图 + 自定义科学色板 + 色标 + 数值标注 (annotated_heatmap)
# =============================================================================
def _annotated_heatmap_impl(
    opmod,
    matrix_rows: list[list[float]],
    x_labels: list[str],
    y_labels: list[str],
    x_title: str = "",
    y_title: str = "",
    cbar_title: str = "Value",
    z_levels: list[float] | None = None,
    palette: str = "teal",
    fmt_str: str = "{:.1f}",
    short_name: str = "SciHeat",
    long_name: str = "Annotated Matrix Heatmap",
    page_cm: tuple[float, float] = (12.8, 9.0),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    arr = np.asarray(matrix_rows, dtype=float)
    nrow, ncol = arr.shape
    cols_spec: list[tuple[str, list[Any], str]] = [
        ("X Labels", list(x_labels), "X"),
        ("Y Labels", list(y_labels), "Y"),
    ]
    for r_i in range(nrow):
        cols_spec.append((f"Row_{y_labels[r_i]}", list(arr[r_i]), "Y"))
    w = _new_sheet_safe(opmod, short_name, f"{long_name} Labels", cols_spec)

    mshort = _clean_short("M" + short_name, "MHeat")
    mat = opmod.new_sheet("m", lname=f"{long_name} Matrix")
    try:
        mat.get_book().name = mshort
    except Exception:
        pass
    mat.from_np(arr)
    # 设全角空格 \u3000（兼容 GBK 与 Unicode 且不被 C trim 剥离），彻底阻止中文版回退显示“颜色刻度标题”！
    mat.set_label(0, "\u3000")
    mat.xymap = 1, ncol, 1, nrow

    pal_path = _ensure_riff_palette(opmod, palette)
    gp = _new_graph_safe(opmod, short_name, long_name, template="Heat_Map.otpu")
    dims = _set_page_cm(opmod, gp, *page_cm)
    gl = gp[0]
    gl.activate()

    hm = gl.add_mplot(mat, 0, type=105)
    hm.colormap = pal_path
    if z_levels is None:
        vmin, vmax = float(np.nanmin(arr)), float(np.nanmax(arr))
        z_levels = list(np.linspace(vmin, vmax, 5))
    sc = hm.zlevels
    sc["levels"] = [float(v) for v in z_levels]
    sc["minors"] = 32
    hm.zlevels = sc

    gl.set_xlim(0.5, ncol + 0.5, 1)
    gl.set_ylim(0.5, nrow + 0.5, 1)
    left, top, wd, ht = 19.0, 15.0, 52.0, 62.0
    _style_layer(opmod, gl, x_title, y_title, rect_pct=(left, top, wd, ht), font_pt=10.0)
    # Heat_Map 模板默认 X 刻度文字竖排旋转 90 度，统一重设为水平 0 度！
    gl.lt_exec("layer.x.label.rotate = 0; layer.y.label.rotate = 0;")
    _bind_cat_axis(gl, w, 0, axis="x")
    _bind_cat_axis(gl, w, 1, axis="y")

    # 定位并格式化右侧 SPECTRUM1 色标与 Times New Roman 标题，确保零裁切
    gl.activate()
    font = int(opmod.lt_float('font("Times New Roman")'))
    pw = opmod.lt_float("page.width")
    ph = opmod.lt_float("page.height")
    sp = gl.label("SPECTRUM1")
    if sp is not None:
        try:
            sp.set_int("font", font)
            sp.set_float("fsize", 9.0)
            sp.set_int("left", round(pw * 0.76))
            sp.set_int("top", round(ph * (top / 100.0)))
            sp.set_int("height", round(ph * (ht / 100.0)))
        except Exception:
            pass
    if cbar_title:
        _add_page_label(opmod, gl, "CBarTitle", f"\\b({cbar_title})", 80.5, top - 7.2, font_pt=10.0, center_x=True)
    zmin, zmax = float(z_levels[0]), float(z_levels[-1])
    zspan = max(zmax - zmin, 1e-9)
    for i in range(nrow):
        for j in range(ncol):
            val = float(arr[i, j])
            rel = (val - zmin) / zspan
            use_white = (palette in ("teal", "slate_amber", "emerald_warm") and rel > 0.58) or (palette == "rdbu_r" and (rel < 0.22 or rel > 0.78))
            tcol = "#FFFFFF" if use_white else "#1E242B"
            lbl = gl.add_label(f"\\b({fmt_str.format(val)})", j + 1, i + 1)
            lbl.set_int("font", font)
            lbl.set_float("fsize", 10.5)
            lbl.color = tcol
            lbl.set_int("showframe", 0)
            lbl.set_int("background", 0)
            x_pct = left + wd * (j + 0.5) / ncol
            y_pct = top + ht * (1.0 - (i + 0.5) / nrow)
            lbl.set_int("left", round(x_pct * pw / 100.0 - lbl.get_int("width") / 2.0))
            lbl.set_int("top", round(y_pct * ph / 100.0 - lbl.get_int("height") / 2.0))

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "annotated_heatmap", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False),
            "matrix": mat.obj.GetName(), "page_cm": dims, "layers": 1, **exp}


# =============================================================================
# 6. 云雨图 / 半小提琴核密度+散点堆叠图 (raincloud)
# =============================================================================
def _raincloud_impl(
    opmod,
    panels: list[dict[str, Any]],
    variant: str = "halfviolin",  # "halfviolin" | "violinbox"
    short_name: str = "SciRain",
    long_name: str = "Raincloud Distribution Plot",
    page_cm: tuple[float, float] = (16.5, 8.2),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    """支持 1..3 个并排原生 HalfViolin.otpu 或 ViolinBox.otpu 面板！"""
    tpl = "HalfViolin.otpu" if variant == "halfviolin" else "ViolinBox.otpu"
    n_pan = len(panels)
    cols_spec: list[tuple[str, list[Any], str]] = []
    for pan in panels:
        cols_spec.append((pan["name"], [float(v) for v in pan["values"]], "Y"))

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    sname = _clean_short(short_name)
    existing = opmod.find_graph(sname)
    if existing is not None:
        try:
            existing.name = _clean_short(f"bk_{sname[:7]}")
        except Exception:
            pass

    if n_pan == 1:
        gp = _new_graph_safe(opmod, short_name, long_name, template=tpl)
        gl0 = gp[0]
        gl0.activate()
        p0 = gl0.add_plot(w, 0, -1, type="?")
        p0.color = panels[0].get("color", "#237A79")
        gl0.rescale()
        lg0 = gl0.label("Legend")
        if lg0 is not None:
            gl0.remove_label(lg0)
    else:
        before_names = {p.obj.GetName() for p in opmod.pages("g")}
        tmp_names = []
        for pidx, pan in enumerate(panels):
            gk = opmod.new_graph(template=tpl)
            glk = gk[0]
            glk.activate()
            pk = glk.add_plot(w, pidx, -1, type="?")
            pk.color = pan.get("color", "#237A79")
            glk.rescale()
            lgk = glk.label("Legend")
            if lgk is not None:
                glk.remove_label(lgk)
            tmp_names.append(gk.obj.GetName())
        joined = "|".join(tmp_names)
        opmod.lt_exec(f'merge_graph option:=specified graphs:="{joined}" row:=1 col:={n_pan} keep:=0;')
        after_pages = [p.obj.GetName() for p in opmod.pages("g") if p.obj.GetName() not in before_names]
        gp = opmod.find_graph(after_pages[-1])
        try:
            gp.name = sname
        except Exception:
            pass
        if long_name:
            try:
                gp.lname = long_name
            except Exception:
                pass

    dims = _set_page_cm(opmod, gp, *page_cm)

    for pidx, pan in enumerate(panels):
        gl = gp[pidx]
        gl.activate()
        if pan.get("y_lim"):
            gl.set_ylim(*pan["y_lim"])

        if n_pan == 1:
            rect = pan.get("rect_pct", (22, 14, 70, 70))
        elif n_pan == 2:
            rect = (15 + pidx * 46, 16, 35, 66)
        else:
            rect = (9.5 + pidx * 31.8, 17, 20.5, 64)

        _style_layer(opmod, gl, pan.get("x_title", ""), pan.get("y_title", pan["name"]), rect_pct=rect, font_pt=9.5)
        # 隐藏底部单箱冗余 X 刻度文字，把物理量清晰标在 Y 轴与面板标题上
        gl.lt_exec("layer.x.showLabels = 0; layer.x.ticks = 0;")
        if pan.get("zero_line") is not None:
            xl = gl.xlim
            _add_ref_line(gl, xl[0], float(pan["zero_line"]), xl[1], float(pan["zero_line"]), color="#6C7782", width=0.85, dash=2)

        if pan.get("panel_title"):
            _add_page_label(opmod, gl, f"RTitle{pidx}", f"\\b({pan['panel_title']})", rect[0] + rect[2] / 2.0, rect[1] - 7.8, font_pt=9.6, center_x=True)
        if pan.get("stat_note"):
            _add_page_label(opmod, gl, f"RStat{pidx}", pan["stat_note"], rect[0] + rect[2] / 2.0, rect[1] + rect[3] + 3.5, font_pt=8.8, color="#4F5A65", center_x=True)

        lg = gl.label("Legend")
        if lg is not None:
            gl.remove_label(lg)

    if n_pan >= 1:
        sub_txt = "\\b(Left: individual observations (n = 615)   |   Right: kernel density & mean ± 1 SD)" if variant == "halfviolin" else "\\b(Violin: kernel density   |   Inner box: 25%–75% IQR & median)"
        _add_page_label(opmod, gp[0], "RainHeader", sub_txt, 50.0, 2.2, font_pt=9.0, color="#4F5A65", center_x=True)

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "raincloud", "variant": variant,
            "graph": gp.obj.GetName(), "long_name": long_name,
            "worksheet": w.lt_range(False), "page_cm": dims, "layers": n_pan, **exp}


# =============================================================================
# 7. 边缘地毯散点 + 分位数校准曲线 + 1:1 线 (marginal_calibration)
# =============================================================================
def _marginal_calibration_impl(
    opmod,
    x_obs: list[float],
    y_pred: list[float],
    n_bins: int = 8,
    x_title: str = "Observed value",
    y_title: str = "Predicted value",
    axis_lim: tuple[float, float, float] | None = None,
    stat_badge: str = "",
    short_name: str = "SciCalib",
    long_name: str = "Marginal Rug & Quantile Calibration",
    page_cm: tuple[float, float] = (11.8, 10.2),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    xa = np.asarray(x_obs, dtype=float)
    ya = np.asarray(y_pred, dtype=float)

    # 计算等分位（或非零分箱）校准均值 ± 95% CI
    edges = np.unique(np.quantile(xa, np.linspace(0.0, 1.0, n_bins + 1)))
    bx, by, blo, bhi = [], [], [], []
    segs = []
    span = float(np.nanmax(xa) - np.nanmin(xa))
    cap = span * 0.012
    for b_i in range(len(edges) - 1):
        if b_i < len(edges) - 2:
            m = (xa >= edges[b_i]) & (xa < edges[b_i + 1])
        else:
            m = (xa >= edges[b_i]) & (xa <= edges[b_i + 1])
        if int(m.sum()) < 3:
            continue
        xm = float(np.mean(xa[m]))
        ym = float(np.mean(ya[m]))
        se = float(np.std(ya[m], ddof=1) / math.sqrt(int(m.sum())))
        lo_v, hi_v = ym - 1.96 * se, ym + 1.96 * se
        bx.append(xm); by.append(ym); blo.append(lo_v); bhi.append(hi_v)
        segs.extend([(xm, lo_v, xm, hi_v), (xm - cap, lo_v, xm + cap, lo_v), (xm - cap, hi_v, xm + cap, hi_v)])

    wx, wy = _segments_to_xy(segs)
    cols_spec: list[tuple[str, list[Any], str]] = [
        ("Observed", list(xa), "X"),
        ("Predicted", list(ya), "Y"),
        ("Bin Mean X", bx, "X"),
        ("Bin Mean Y", by, "Y"),
        ("Bin CI_low", blo, "Y"),
        ("Bin CI_high", bhi, "Y"),
        ("CI WhiskX", wx or [NAN], "X"),
        ("CI WhiskY", wy or [NAN], "Y"),
    ]
    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name, template="ScatterRug.otpu")
    dims = _set_page_cm(opmod, gp, *page_cm)

    gl = gp[0]
    gl.activate()
    # 主散点（连带模板边缘地毯刻线自动联动！）
    p_raw = gl.add_plot(w, 1, 0, type="?")
    p_raw.color = "#7C97AD"
    p_raw.symbol_kind = 2
    p_raw.symbol_size = 4.6
    p_raw.transparency = 45

    # 叠加分位数均值校准线与 95% CI 须线
    if wx:
        pw_ci = gl.add_plot(w, 7, 6, type="l")
        pw_ci.color = "#B64342"
        pw_ci.set_cmd("-wp 1.5")
    p_cal = gl.add_plot(w, 3, 2, type="y")
    p_cal.symbol_kind = 1
    p_cal.symbol_size = 6.8
    p_cal.symbol_interior = 1
    p_cal.color = "#B64342"
    lt_cal = _hex_to_lt_color("#B64342")
    p_cal.set_cmd("-wp 2.1", f"-c {lt_cal}", f"-khc {lt_cal}")

    if axis_lim is None:
        lo_all = min(float(xa.min()), float(ya.min()))
        hi_all = max(float(xa.max()), float(ya.max()))
        pad = (hi_all - lo_all) * 0.06
        step = round((hi_all - lo_all) / 5.0, 2) or 0.1
        axis_lim = (round(lo_all - pad, 2), round(hi_all + pad, 2), step)

    # ScatterRug 含 3 个联动图层：微上移 1.2% 兼顾顶部图例间距与底部标题留白
    font = int(opmod.lt_float('font("Times New Roman")'))
    ph_u = opmod.lt_float("page.height")
    gl.set_xlim(*axis_lim)
    gl.set_ylim(*axis_lim)
    for l_idx in range(gp.obj.Layers.Count):
        gli = gp[l_idx]
        gli.activate()
        try:
            gli.set_int("unit", 1)
            gli.set_float("top", gli.get_float("top") - 1.1)
        except Exception:
            pass
        for ax in ("x", "y"):
            gli.set_int(f"{ax}.label.font", font)
            gli.set_float(f"{ax}.label.fsize", 9.5)
            gli.set_int(f"{ax}.label.bold", 1)
    # 同步边缘地毯轴范围
    if gp.obj.Layers.Count >= 3:
        gp[1].set_ylim(*axis_lim)
        gp[2].set_xlim(*axis_lim)

    gl.activate()
    for key, title in [("xb", x_title), ("yl", y_title)]:
        # ScatterRug 轴标题可能在主层或边缘层，逐层查找更新
        for l_idx in range(gp.obj.Layers.Count):
            lbl = gp[l_idx].label(key)
            if lbl is not None:
                lbl.set_int("font", font)
                lbl.text = f"\\b({title})"
                lbl.set_float("fsize", 10.0)
                if key == "xb":
                    lbl.set_int("top", round(lbl.get_int("top") - ph_u * 0.015))

    _add_ref_line(gl, axis_lim[0], axis_lim[0], axis_lim[1], axis_lim[1], color="#303841", width=1.0, dash=2)

    # 清除出界的默认图例，改在图框上方留白处写清晰图例与统计指标
    for l_idx in range(gp.obj.Layers.Count):
        lg = gp[l_idx].label("Legend")
        if lg is not None:
            gp[l_idx].remove_label(lg)

    _set_custom_legend(opmod, gl, [(1, "Individual points"), (3 if wx else 2, "Quantile mean ± 95% CI")], left_pct=56.0, top_pct=1.4, font_pt=9.0)
    if stat_badge:
        _add_page_label(opmod, gl, "CalibBadge", f"\\b({stat_badge})", 26.0, 13.5, font_pt=9.2, color="#1E242B")

    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "marginal_calibration", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": gp.obj.Layers.Count, **exp}


# =============================================================================
# 8. 多维雷达/蜘蛛网对比图 (radar_chart —— 等比例多边形蛛网 + 半透明阴影闭合多边形)
# =============================================================================
def _radar_chart_impl(
    opmod,
    categories: list[str],
    series_dict: dict[str, list[float]],
    colors: list[str] | None = None,
    radial_lim: tuple[float, float, float] = (0.0, 1.0, 0.25),
    short_name: str = "SciRadar",
    long_name: str = "Multi-Metric Radar Chart",
    page_cm: tuple[float, float] = (13.6, 10.8),
    file_path: str | None = None,
    pdf_path: str | None = None,
    width: int = 1800,
) -> dict[str, Any]:
    """在严格 1:1 等比例笛卡尔图层上绘制高颜值科研多边形蛛网/雷达图（彻底规避内置 Radar 模板的宋体裁切与单列限制）。"""
    cats = list(categories)
    n_ax = len(cats)
    r_min, r_max, r_step = float(radial_lim[0]), float(radial_lim[1]), float(radial_lim[2])
    r_span = max(r_max - r_min, 1e-9)
    angles = [math.pi / 2.0 - 2.0 * math.pi * k / n_ax for k in range(n_ax)]

    def _to_r(val: float) -> float:
        return max(0.0, (float(val) - r_min) / r_span)

    # 1) 同心正多边形网格线 + 辐射轴线
    rings = list(np.arange(r_min + r_step, r_max + 1e-9, r_step))
    inner_segs: list[tuple[float, float, float, float]] = []
    outer_segs: list[tuple[float, float, float, float]] = []
    spoke_segs: list[tuple[float, float, float, float]] = []

    for rv in rings:
        rn = _to_r(rv)
        target = outer_segs if abs(rv - r_max) < 1e-6 else inner_segs
        for k in range(n_ax):
            a0, a1 = angles[k], angles[(k + 1) % n_ax]
            target.append((rn * math.cos(a0), rn * math.sin(a0), rn * math.cos(a1), rn * math.sin(a1)))

    for a0 in angles:
        spoke_segs.append((0.0, 0.0, math.cos(a0), math.sin(a0)))

    ix_x, ix_y = _segments_to_xy(inner_segs)
    ox_x, ox_y = _segments_to_xy(outer_segs)
    sp_x, sp_y = _segments_to_xy(spoke_segs)

    cols_spec: list[tuple[str, list[Any], str]] = [
        ("Spoke", cats, "X"),
        ("InnerWebX", ix_x or [NAN], "X"), ("InnerWebY", ix_y or [NAN], "Y"),
        ("OuterWebX", ox_x or [NAN], "X"), ("OuterWebY", ox_y or [NAN], "Y"),
        ("SpokeX", sp_x, "X"), ("SpokeY", sp_y, "Y"),
    ]
    s_cols = []
    for sname, vals in series_dict.items():
        vlist = [float(v) for v in vals]
        c_raw = len(cols_spec); cols_spec.append((f"{sname} Raw", vlist, "Y"))
        sx = [_to_r(vlist[k % n_ax]) * math.cos(angles[k % n_ax]) for k in range(n_ax + 1)]
        sy = [_to_r(vlist[k % n_ax]) * math.sin(angles[k % n_ax]) for k in range(n_ax + 1)]
        cx = len(cols_spec); cols_spec.append((f"{sname} X", sx, "X"))
        cy = len(cols_spec); cols_spec.append((f"{sname} Y", sy, "Y"))
        s_cols.append((sname, cx, cy, c_raw))

    w = _new_sheet_safe(opmod, short_name, f"{long_name} Data", cols_spec)
    gp = _new_graph_safe(opmod, short_name, long_name)
    dims = _set_page_cm(opmod, gp, *page_cm)
    gl = gp[0]
    gl.activate()

    # 严格保证物理长宽比与坐标跨度 1:1 等比例，确保正六边形绝不变形！
    pw_cm, ph_cm = dims
    h_pct = 76.0
    h_cm = ph_cm * h_pct / 100.0
    x_half, y_half = 1.78, 1.32
    w_cm = h_cm * (x_half / y_half)
    w_pct = min(90.0, w_cm / pw_cm * 100.0)
    h_cm = pw_cm * w_pct / 100.0 * (y_half / x_half)
    h_pct = h_cm / ph_cm * 100.0
    left_pct = (100.0 - w_pct) / 2.0
    top_pct = 15.0

    gl.set_int("unit", 1)
    for prop, val in zip(("left", "top", "width", "height"), (left_pct, top_pct, w_pct, h_pct)):
        gl.set_float(prop, float(val))
    gl.set_xlim(-x_half, x_half, 0.5)
    gl.set_ylim(-y_half, y_half, 0.5)
    # 隐藏直角坐标轴框与刻度文字
    gl.lt_exec(
        "layer.x.showAxes=0; layer.y.showAxes=0; "
        "layer.x.showLabels=0; layer.y.showLabels=0; "
        "layer.x2.showAxes=0; layer.y2.showAxes=0;"
    )
    for k_lbl in ("xb", "yl", "xt", "yr"):
        lb = gl.label(k_lbl)
        if lb is not None:
            gl.remove_label(lb)

    plot_idx = 0
    if ix_x:
        p_in = gl.add_plot(w, 2, 1, type="l")
        p_in.color = "#CBD4DC"; p_in.set_cmd("-wp 0.75", "-d 2")
        plot_idx += 1
    p_sp = gl.add_plot(w, 6, 5, type="l")
    p_sp.color = "#BCC6D0"; p_sp.set_cmd("-wp 0.85")
    plot_idx += 1
    if ox_x:
        p_out = gl.add_plot(w, 4, 3, type="l")
        p_out.color = "#8D9AA6"; p_out.set_cmd("-wp 1.1")
        plot_idx += 1

    palette = colors or ["#63707E", "#1E7A79", "#B64342", "#1F5A8A"]
    fill_tints = ["#B9C2CC", "#84C5C4", "#E29B9A", "#95B7D9"]
    # 先画半透明多边形填充带
    for idx, (sname, cx, cy, _) in enumerate(s_cols):
        col_hex = palette[idx % len(palette)]
        ft_hex = fill_tints[idx % len(fill_tints)]
        lt_fc = _hex_to_lt_color(ft_hex)
        pf = gl.add_plot(w, cy, cx, type="l")
        pf.color = col_hex
        pf.set_cmd("-pf 1", "-pfv 2", f"-cf {lt_fc}", f"-pfb {lt_fc}", "-wp 0.3")
        pf.transparency = 68 if idx == 0 else 58
        plot_idx += 1

    # 再画顶层粗轮廓线 + 顶点符号
    legend_entries: list[tuple[int, str]] = []
    for idx, (sname, cx, cy, _) in enumerate(s_cols):
        col_hex = palette[idx % len(palette)]
        pr = gl.add_plot(w, cy, cx, type="y")
        pr.color = col_hex
        pr.symbol_kind = 2 if idx == 0 else 1
        pr.symbol_size = 7.2
        if idx == 0:
            pr.set_cmd("-wp 1.9", "-d 2", "-kf 1", "-kh 22")
        else:
            pr.set_cmd("-wp 2.4")
            pr.symbol_interior = 1
        plot_idx += 1
        legend_entries.append((plot_idx, sname))

    # 添加径向刻度数字与六个顶点指标名称（Times New Roman）
    font = int(opmod.lt_float('font("Times New Roman")'))
    pw_u = opmod.lt_float("page.width")
    ph_u = opmod.lt_float("page.height")

    def _data_to_page(dx: float, dy: float) -> tuple[float, float]:
        px_pct = left_pct + (dx + x_half) / (2.0 * x_half) * w_pct
        py_pct = top_pct + (y_half - dy) / (2.0 * y_half) * h_pct
        return px_pct * pw_u / 100.0, py_pct * ph_u / 100.0

    for rv in rings:
        rn = _to_r(rv)
        rtxt = f"{rv:.2f}".rstrip("0").rstrip(".") if rv not in (0.25, 0.5, 0.75, 1.0) else f"{rv:.2f}"
        lb = gl.add_label(rtxt, 0, 0)
        lb.set_int("font", font); lb.set_float("fsize", 8.3); lb.color = "#525E6A"
        lb.set_int("showframe", 0); lb.set_int("background", 0)
        ux, uy = _data_to_page(0.05, rn)
        lb.set_int("left", round(ux))
        lb.set_int("top", round(uy - lb.get_int("height") / 2.0))

    for k, (cat_txt, ang) in enumerate(zip(cats, angles)):
        ca, sa = math.cos(ang), math.sin(ang)
        rad_lbl = 1.13 if abs(ca) < 0.25 else 1.09
        ux, uy = _data_to_page(rad_lbl * ca, rad_lbl * sa)
        lb = gl.add_label(f"\\b({cat_txt})", 0, 0)
        lb.set_int("font", font); lb.set_float("fsize", 9.6); lb.color = "#1E242B"
        lb.set_int("showframe", 0); lb.set_int("background", 0)
        lw, lh = lb.get_int("width"), lb.get_int("height")
        if abs(ca) < 0.22:
            lx = ux - lw / 2.0
            ly = (uy - lh * 1.05) if sa > 0 else (uy + lh * 0.10)
        elif ca > 0:
            lx = ux
            ly = uy - lh / 2.0
        else:
            lx = ux - lw
            ly = uy - lh / 2.0
        lb.set_int("left", round(lx))
        lb.set_int("top", round(ly))

    _set_custom_legend(opmod, gl, legend_entries, left_pct=50.0, top_pct=3.2, font_pt=9.6, horizontal=True)
    exp = _export_outputs(gp, file_path=file_path, pdf_path=pdf_path, width=width)
    return {"ok": True, "chart_type": "radar_chart", "graph": gp.obj.GetName(),
            "long_name": long_name, "worksheet": w.lt_range(False), "page_cm": dims,
            "layers": 1, **exp}


# =============================================================================
# 线程安全公开接口（供 MCP 服务器与外部调用）
# =============================================================================
def forest_horizontal(panels: list, short_name: str = "SciForest", long_name: str = "Horizontal Forest Plot",
                      page_cm: list | tuple = (16.0, 8.6), file_path: str | None = None,
                      pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _forest_horizontal_impl(_ensure_op(), panels, short_name, long_name, tuple(page_cm), file_path, pdf_path, width)
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"forest_horizontal 失败: {exc}")


def diverging_lollipop(panels: list, short_name: str = "SciLolli", long_name: str = "Diverging Lollipop Plot",
                       page_cm: list | tuple = (16.0, 9.8), file_path: str | None = None,
                       pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _diverging_lollipop_impl(_ensure_op(), panels, short_name, long_name, tuple(page_cm), file_path, pdf_path, width)
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"diverging_lollipop 失败: {exc}")


def dumbbell_pair(panels: list, short_name: str = "SciDumb", long_name: str = "Connected Dumbbell Plot",
                  page_cm: list | tuple = (16.0, 8.6), file_path: str | None = None,
                  pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _dumbbell_pair_impl(_ensure_op(), panels, short_name, long_name, tuple(page_cm), file_path, pdf_path, width)
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"dumbbell_pair 失败: {exc}")


def ribbon_profile(x_vals: list, series_list: list, x_title: str = "X", y_title: str = "Y",
                   x_lim: list | tuple | None = None, y_lim: list | tuple | None = None,
                   zero_line: float | None = 0.0, short_name: str = "SciRibbon",
                   long_name: str = "Shaded Ribbon Profile", page_cm: list | tuple = (12.8, 9.2),
                   file_path: str | None = None, pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _ribbon_profile_impl(
                _ensure_op(), x_vals, series_list, x_title, y_title,
                tuple(x_lim) if x_lim else None, tuple(y_lim) if y_lim else None,
                zero_line, short_name, long_name, tuple(page_cm), file_path, pdf_path, width
            )
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"ribbon_profile 失败: {exc}")


def annotated_heatmap(matrix_rows: list, x_labels: list, y_labels: list,
                      x_title: str = "", y_title: str = "", cbar_title: str = "Value",
                      z_levels: list | None = None, palette: str = "teal", fmt_str: str = "{:.1f}",
                      short_name: str = "SciHeat", long_name: str = "Annotated Matrix Heatmap",
                      page_cm: list | tuple = (12.8, 9.0), file_path: str | None = None,
                      pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _annotated_heatmap_impl(
                _ensure_op(), matrix_rows, x_labels, y_labels, x_title, y_title,
                cbar_title, z_levels, palette, fmt_str, short_name, long_name,
                tuple(page_cm), file_path, pdf_path, width
            )
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"annotated_heatmap 失败: {exc}")


def raincloud(panels: list, variant: str = "halfviolin", short_name: str = "SciRain",
              long_name: str = "Raincloud Distribution Plot", page_cm: list | tuple = (16.5, 8.2),
              file_path: str | None = None, pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _raincloud_impl(_ensure_op(), panels, variant, short_name, long_name, tuple(page_cm), file_path, pdf_path, width)
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"raincloud 失败: {exc}")


def marginal_calibration(x_obs: list, y_pred: list, n_bins: int = 8,
                         x_title: str = "Observed", y_title: str = "Predicted",
                         axis_lim: list | tuple | None = None, stat_badge: str = "",
                         short_name: str = "SciCalib", long_name: str = "Marginal Rug & Calibration",
                         page_cm: list | tuple = (11.8, 10.2), file_path: str | None = None,
                         pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _marginal_calibration_impl(
                _ensure_op(), x_obs, y_pred, n_bins, x_title, y_title,
                tuple(axis_lim) if axis_lim else None, stat_badge,
                short_name, long_name, tuple(page_cm), file_path, pdf_path, width
            )
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"marginal_calibration 失败: {exc}")


def radar_chart(categories: list, series_dict: dict, colors: list | None = None,
                radial_lim: list | tuple = (0.0, 1.0, 0.25), short_name: str = "SciRadar",
                long_name: str = "Multi-Metric Radar Chart", page_cm: list | tuple = (12.4, 10.2),
                file_path: str | None = None, pdf_path: str | None = None, width: int = 1800) -> dict:
    try:
        return engine._run_on_com_thread(
            lambda: _radar_chart_impl(
                _ensure_op(), categories, series_dict, colors, tuple(radial_lim),
                short_name, long_name, tuple(page_cm), file_path, pdf_path, width
            )
        )
    except Exception as exc:
        return oerr.fail("plot_error", f"radar_chart 失败: {exc}")
