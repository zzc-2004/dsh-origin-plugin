# -*- coding: utf-8 -*-
"""统一字体设置 —— 吸收自 origin-mcp 的 font() 语法。

背景
----
Origin 的 LabTalk 用整数索引表示字体族，索引值随系统字体列表变化，写死数字必然错。
`font("Times New Roman")` 把字体名转成索引，是唯一可靠的写法。

覆盖面
------
一张图里的文字分散在多个对象上，缺一不可：
  layer.x.label        x 轴标题
  layer.y.label        y 轴标题
  layer.x.ticklabel    x 轴刻度标签
  layer.y.ticklabel    y 轴刻度标签
  xb / yl              x/y 轴标题的文本对象（fsize 走这里）
  legend               图例（font / fsize）
  text                 图内手工文本框（逐个 TextN 对象）
多图层图要按图层索引逐个设置。

用法
----
    set_font(op, graph="Graph1", family="Times New Roman", size_pt=10)
    set_font(op, graph="Graph1", family="Arial", size_pt=8, targets=["axis","legend"])
"""
from __future__ import annotations

import origin_edit as oedit
import origin_errors as oerr  # noqa: F401

try:
    from origin_edit import _safe
except Exception:  # pragma: no cover
    def _safe(fn, *a, **kw):
        try:
            return fn(*a, **kw), None
        except Exception as e:
            return None, str(e)


_TARGETS = {
    "axis": ["layer.{ax}.label.font", "layer.{ax}.label.pt",
             "layer.{ax}.ticklabel.font", "layer.{ax}.ticklabel.pt"],
    "textobj": ["{o}.font", "{o}.fsize"],
    "legend": ["legend.font", "legend.fsize"],
}


def _quote(name: str) -> str:
    """LabTalk 字符串转义：双引号包裹，内部双引号用 % 转义。"""
    s = str(name).replace('"', '%"')
    return f'font("{s}")'


def _layers(po, graph):
    """层数（LabTalk 读回，失败按 1 层处理）。"""
    po.LT_execute(f'win -a "{graph}";')
    po.LT_execute("double __dshnl = page.nlayers;")
    val = None
    _, err = _safe(po.LT_get_var, "__dshnl")
    if err is None:
        try:
            val = int(float(po.LT_get_var("__dshnl")))
        except Exception:
            val = None
    return max(1, val or 1)


def set_font(op, graph, family="Times New Roman", size_pt=None,
             targets=None, bold=None, layer=None):
    """把整张图的字体族/字号统一。

    Args:
        graph:    图短名。
        family:   字体族名，如 "Times New Roman" / "Arial"。
        size_pt:  字号（pt）；None 只改字体不改字号。
        targets:  要改的对象子集，默认 ["axis", "legend", "textobj"]。
        bold:     0/1 加粗；None 不改。
        layer:    只处理指定图层索引；None 处理全部图层。

    Returns:
        {"ok": True, "changes": [{item, requested, status, readback}], "n_applied": N}
    """
    targets = list(targets or ["axis", "legend", "textobj"])
    short, err = oedit.ensure_active_graph(op, op.po, graph)
    if err:
        return err

    n_layers = _layers(op.po, short)
    idxs = [int(layer)] if layer is not None else list(range(n_layers))
    fam = _quote(family)
    changes = []

    def rec(item, requested, status, readback=None, note=None):
        e = {"item": item, "requested": requested, "status": status}
        if readback is not None:
            e["readback"] = readback
        if note:
            e["note"] = note
        changes.append(e)

    # ── 轴标题 + 刻度标签 ──
    if "axis" in targets:
        for li in idxs:
            if li >= n_layers:
                continue
            _safe(op.po.LT_execute, f"win -a \"{short}\"; layer -s {li + 1};")
            for ax in ("x", "y"):
                for obj, attr in ((f"layer.{ax}.label", "font"),
                                  (f"layer.{ax}.ticklabel", "font")):
                    _, e = _safe(op.po.LT_execute, f"{obj}.{attr} = {fam};")
                    back, _ = _safe(oedit.lt_float, op.po, f"{obj}.{attr}")
                    rec(f"L{li}.{obj}.{attr}", family,
                        "applied" if e is None else "rejected", readback=back,
                        note=None if e is None else str(e)[:120])
                if size_pt is not None:
                    for obj in (f"layer.{ax}.label", f"layer.{ax}.ticklabel"):
                        _safe(op.po.LT_execute, f"{obj}.pt = {float(size_pt)};")
                        back, _ = _safe(oedit.lt_float, op.po, f"{obj}.pt")
                        ok = back is not None and abs(float(back) - float(size_pt)) < 0.01
                        rec(f"L{li}.{obj}.pt", size_pt,
                            "applied" if ok else "applied_unverified", readback=back)
                if bold is not None:
                    _safe(op.po.LT_execute, f"layer.{ax}.label.bt = {int(bold)};")
            # xb / yl：轴标题文本对象（部分版本才有）
            for o in ("xb", "yl"):
                _, e = _safe(op.po.LT_execute, f"{o}.font = {fam};")
                if e is None and size_pt is not None:
                    _safe(op.po.LT_execute, f"{o}.fsize = {float(size_pt)};")
                if e is None:
                    rec(f"L{li}.{o}.font", family, "applied")

    # ── 图例 ──
    if "legend" in targets:
        _safe(op.po.LT_execute, f'win -a "{short}";')
        _, e = _safe(op.po.LT_execute, f"legend.font = {fam};")
        back, _ = _safe(oedit.lt_float, op.po, "legend.font")
        rec("legend.font", family, "applied" if e is None else "rejected",
            readback=back, note=None if e is None else str(e)[:120])
        if size_pt is not None:
            _safe(op.po.LT_execute, f"legend.fsize = {float(size_pt)};")
            back, _ = _safe(oedit.lt_float, op.po, "legend.fsize")
            ok = back is not None and abs(float(back) - float(size_pt)) < 0.01
            rec("legend.fsize", size_pt,
                "applied" if ok else "applied_unverified", readback=back)

    # ── 图内文本框（TextN，数量由 LabTalk 变量给出）──
    if "textobj" in targets:
        _safe(op.po.LT_execute, f'win -a "{short}";')
        n_txt = None
        ok, err2 = _safe(lambda: op.po.LT_execute("double __dshtn = page.nTextObjects;"))
        if ok is not None or err2 is None:
            try:
                n_txt = int(float(op.po.LT_get_var("__dshtn")))
            except Exception:
                n_txt = None
        for k in range(1, (n_txt or 0) + 1):
            _safe(op.po.LT_execute, f"Text{k}.font = {fam};")
            if size_pt is not None:
                _safe(op.po.LT_execute, f"Text{k}.fsize = {float(size_pt)};")
        # 图标题（DshTitle）不在 TextN 序列里，单独处理
        try:
            op.po.LT_execute(f'win -a "{short}";')
            title_txt = oedit.lt_str(op.po, "DshTitle.text$")
            if title_txt:
                _safe(op.po.LT_execute, f"DshTitle.font = {fam};")
                if size_pt is not None:
                    _safe(op.po.LT_execute, f"DshTitle.fsize = {float(size_pt)};")
                rec("title_object", "DshTitle", "applied")
        except Exception:
            pass
        if n_txt:
            rec("text_objects", f"{n_txt} 个", "applied")

    n_ok = sum(1 for c in changes if c["status"] == "applied")
    return {"ok": True, "graph": short, "family": family, "size_pt": size_pt,
            "layers": idxs, "targets": targets,
            "changes": changes, "n_applied": n_ok, "n_total": len(changes),
            "detail": f"字体 {family}，{size_pt if size_pt is not None else '字号不变'}"
                      f"，覆盖 {len(idxs)} 层 / {len(targets)} 类对象，"
                      f"{n_ok}/{len(changes)} 项写入成功"}
