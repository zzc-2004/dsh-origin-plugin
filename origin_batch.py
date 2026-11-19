# -*- coding: utf-8 -*-
"""批处理 —— 吸收自 origin-mcp。

做什么
------
拿一批数据文件（或已有区间），套同一个分析模板，批量出结果。
底层是 Origin 原生的 `batchprocess` X-Function，不是我们另写的循环。

两种模式
--------
  template   事先在 Origin 里把分析配好，存成 .ogw/.ogwu 模板，
             然后对每个数据文件套一遍。适合「同一套处理跑几百个样品」。
  active     用当前工作簿里正在生效的分析操作，不落模板。

六种数据来源
------------
  files               一串文件路径（多文件时写入 fname$ 换行分隔）
  folder              按扩展名扫整个目录
  existing_xy         已有 XY 区间
  existing_xyz        已有 XYZ 区间
  existing_worksheets 已有工作表
  existing_ranges     已有区间（可另给固定区间）

用法
----
    batch_process(source_type="folder", folder="D:/data",
                  extensions="*.csv", mode="template",
                  analysis_template="D:/tpl/analysis.ogwu")
"""
from __future__ import annotations

import os
import re

_IMPORT_METHOD_RE = re.compile(r"^(ImportWizard|Script|imp[A-Za-z0-9_]+)$")

_SOURCE = {
    "files": "import", "folder": "folder",
    "existing_xy": "existingXY", "existing_xyz": "existingXYZ",
    "existing_worksheets": "existingwks", "existing_ranges": "existingRange",
}


def _q(po, key, value):
    """把参数写成 Origin X-Function 的引号形式 key:="v"。"""
    s = str(value).replace('"', '""')
    return f'{key}:="{s}"'


def _range_arg(value):
    """区间参数：已带 [Book]Sheet! 形式则原样，否则加引号。"""
    v = str(value).strip()
    return v if v.startswith("[") or v.endswith("!") else f'"{v}"'


def _norm_path(path):
    p = str(path).strip().strip('"').replace("\\", "/")
    return os.path.abspath(os.path.expanduser(p))


def batch_process(engine, source_type="files", files=None, folder=None,
                  extensions="*.*", input_range=None, fixed_range=None,
                  batch_range=None, worksheets=None, analysis_template=None,
                  mode="template", data_sheet=None, result_sheet=None,
                  output_sheet=None, dataset_identifier="File Name",
                  import_method="impASC", theme=None, import_filter=None,
                  import_script=None, remove_intermediate=True, clear_output=True,
                  append_mode="rows", before_script=None, loop_script=None,
                  end_script=None):
    """执行批处理。参数语义见模块文档。"""
    src = str(source_type or "").strip().lower().replace("-", "_")
    if src not in _SOURCE:
        return {"ok": False, "error_code": "invalid_request",
                "error": f"source_type 必须是 {list(_SOURCE)} 之一，收到 {source_type!r}"}
    mode_key = str(mode or "").strip().lower()
    if mode_key not in ("template", "active"):
        return {"ok": False, "error_code": "invalid_request",
                "error": "mode 必须是 template 或 active"}
    if not _IMPORT_METHOD_RE.fullmatch(str(import_method or "").strip()):
        return {"ok": False, "error_code": "invalid_request",
                "error": "import_method 必须是 ImportWizard / Script / imp* X-Function"}
    app_key = str(append_mode or "").strip().lower()
    if app_key not in ("rows", "columns", "cols"):
        return {"ok": False, "error_code": "invalid_request",
                "error": "append_mode 必须是 rows 或 columns"}

    args = [f"batch:={mode_key}", f"data:={_SOURCE[src]}"]
    norm_files, norm_folder, tpl = [], None, None

    if src == "files":
        if not files:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "source_type='files' 需要提供 files"}
        for f in files:
            p = _norm_path(f)
            if not os.path.exists(p):
                return {"ok": False, "error_code": "file_not_found",
                        "error": f"文件不存在: {p}"}
            norm_files.append(p)
        args.append("fname:=fname$")
    elif src == "folder":
        if not folder:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "source_type='folder' 需要提供 folder"}
        fp = _norm_path(folder)
        if not os.path.isdir(fp):
            return {"ok": False, "error_code": "file_not_found",
                    "error": f"目录不存在: {fp}"}
        norm_folder = fp
        args.append(f'path:="{fp}"')
        args.append(f'ext:="{extensions}"')
    elif src in ("existing_xy", "existing_xyz"):
        if not input_range:
            return {"ok": False, "error_code": "invalid_request",
                    "error": f"source_type={src!r} 需要 input_range"}
        args.append(f"{'iy' if src == 'existing_xy' else 'iz'}:={_range_arg(input_range)}")
    elif src == "existing_worksheets":
        if not worksheets:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "source_type='existing_worksheets' 需要 worksheets"}
        args.append(f"iw:={_range_arg(worksheets)}")
    else:
        if not batch_range:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "source_type='existing_ranges' 需要 batch_range"}
        args.append(f"irngb:={_range_arg(batch_range)}")
        if fixed_range:
            args.append(f"irngf:={_range_arg(fixed_range)}")

    if mode_key == "template":
        if not analysis_template:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "mode='template' 需要 analysis_template（.ogw/.ogwu）"}
        tp = _norm_path(analysis_template)
        if not os.path.exists(tp):
            return {"ok": False, "error_code": "file_not_found",
                    "error": f"分析模板不存在: {tp}"}
        if os.path.splitext(tp)[1].lower() not in (".ogw", ".ogwu"):
            return {"ok": False, "error_code": "invalid_request",
                    "error": "分析模板必须是 .ogw 或 .ogwu"}
        tpl = tp
        args.append(f'name:="{tp}"')

    if src in ("files", "folder"):
        args.append(f'method:="{import_method.strip()}"')
        if theme:
            args.append(f'theme:="{theme}"')
        if import_filter:
            args.append(f'filter:="{import_filter}"')
        if import_script:
            args.append(f'script:="{import_script}"')

    for key, val in (("fill", data_sheet), ("append", result_sheet),
                     ("ow", output_sheet), ("id", dataset_identifier)):
        if val:
            args.append(_q(None, key, val))
    args += [f"remove:={int(bool(remove_intermediate))}",
             f"clear:={int(bool(clear_output))}",
             f"mode:={0 if app_key == 'rows' else 1}"]
    for key, val in (("beforescript", before_script),
                     ("loopscript", loop_script),
                     ("endscript", end_script)):
        if val:
            args.append(_q(None, key, val))

    script = "batchprocess " + " ".join(args) + ";"

    try:
        ok, conn = engine._connect_impl()
        if not ok:
            return conn
        po = engine._origin_app.po
        # 多文件时先写 fname$（换行分隔），再跑命令
        if norm_files:
            joined = "\n".join(norm_files)
            po.LT_execute(f'double __dshprev; __dshprev = 1;')
            # set_lt_str 不可用时退回 LabTalk 赋值
            try:
                engine._origin_app.set_lt_str("fname", joined)
            except Exception:
                safe = joined.replace('"', '""')
                po.LT_execute(f'fname$ = "{safe}";')
        res = po.LT_execute(script)
        return {"ok": res is not False, "source_type": src, "mode": mode_key,
                "files": norm_files, "folder": norm_folder,
                "analysis_template": tpl, "script": script,
                "detail": "批处理已提交" if res is not False
                          else "Origin 拒绝了批处理（检查模板与数据匹配）"}
    except Exception as exc:
        return {"ok": False, "error_code": "origin_operation_error",
                "error": f"{type(exc).__name__}: {exc}", "script": script}


def clone_import(engine, files, book_name=None, sheet_name=None):
    """克隆导入：用现有工作表的导入设置批量导入多个文件。"""
    if not files:
        return {"ok": False, "error_code": "invalid_request", "error": "需要 files"}
    norm = []
    for f in files:
        p = _norm_path(f)
        if not os.path.exists(p):
            return {"ok": False, "error_code": "file_not_found",
                    "error": f"文件不存在: {p}"}
        norm.append(p)
    try:
        ok, conn = engine._connect_impl()
        if not ok:
            return conn
        op = engine._origin_app
        try:
            wks = op.find_sheet("w", book_name or "") if book_name else None
            rng = str(wks.lt_range()).rstrip("!") + "!" if wks is not None else None
        except Exception:
            rng = None
        if not rng:
            return {"ok": False, "error_code": "invalid_request",
                    "error": "找不到源工作表，请给 book_name"}
        script = f"cloneimport orng:={rng};"
        joined = "\n".join(norm)
        try:
            op.set_lt_str("fname", joined)
        except Exception:
            op.po.LT_execute(f'fname$ = "{joined.replace(chr(34), chr(34)*2)}";')
        res = op.po.LT_execute(script)
        return {"ok": res is not False, "source_range": rng, "files": norm,
                "script": script}
    except Exception as exc:
        return {"ok": False, "error_code": "origin_operation_error",
                "error": f"{type(exc).__name__}: {exc}"}
