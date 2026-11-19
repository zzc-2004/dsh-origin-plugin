# -*- coding: utf-8 -*-
"""OriginLab 官方文档知识库 —— 吸收自 origin-mcp。

问题
----
Origin 的 API 面极大：996 个 X-Function、752 个 originpro 成员、79 条 LabTalk 命令，
外加类与指南共 1843 条。模型凭记忆写 Origin 代码，函数名和参数十有八九是编的。

做法
----
把官方文档索引离线打包（977 KB），提供关键词检索与按族浏览，让代码里的每个 API
先查得到再写。数据来源：docs.originlab.com，生成时间 2026-07-13。

用法
----
    search("plotxy")                       # 全文关键词
    search("layer font", family="labtalk") # 限定族
    browse("x_function")                   # 列出某族条目
    get("labtalk/commands/plotxy")         # 取单条详情
"""
from __future__ import annotations

import json
import os
import re
import threading

_LOCK = threading.Lock()
_CACHE = {}
_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "knowledge", "official_docs.generated.json")


def _load():
    with _LOCK:
        if "db" in _CACHE:
            return _CACHE["db"]
        if not os.path.exists(_INDEX):
            _CACHE["db"] = None
            return None
        with open(_INDEX, "r", encoding="utf-8") as f:
            db = json.load(f)
        pages = db.get("pages", [])
        # 预建检索词：标题 + 摘要 + 关键词 + path
        for p in pages:
            p["_hay"] = " ".join([
                str(p.get("title", "")), str(p.get("summary", "")),
                str(p.get("path", "")), " ".join(p.get("keywords") or []),
                str(p.get("doc_family", "")), str(p.get("doc_kind", "")),
            ]).lower()
        _CACHE["db"] = db
        return db


def _brief(p, score=None):
    out = {"title": p.get("title"), "path": p.get("path"),
           "family": p.get("doc_family"), "kind": p.get("doc_kind"),
           "url": p.get("url"), "summary": p.get("summary")}
    if score is not None:
        out["score"] = score
    return out


def search(query: str, family: str = None, kind: str = None, limit: int = 20):
    """按关键词检索文档条目。

    Args:
        query:  关键词，多个词用空格分隔（全部命中优先）。
        family: 限定族：x_function | originpro_api | labtalk | python。
        kind:   限定类型：xfunction | member | command | class | guide ...
        limit:  返回条数上限（1–100）。
    """
    db = _load()
    if db is None:
        return {"ok": False, "error_code": "knowledge_missing",
                "error": f"知识库文件不存在: {_INDEX}",
                "next_actions": ["从 origin-mcp 仓库拷贝 official_docs.generated.json 到 knowledge/"]}
    q = str(query or "").lower().strip()
    terms = [t for t in re.split(r"[\s,，]+", q) if t]
    if not terms:
        return {"ok": False, "error_code": "invalid_request", "error": "query 不能为空"}
    limit = max(1, min(int(limit or 20), 100))
    hits = []
    for p in db["pages"]:
        if family and p.get("doc_family") != family:
            continue
        if kind and p.get("doc_kind") != kind:
            continue
        hay = p["_hay"]
        if not all(t in hay for t in terms):
            continue
        # 打分：标题命中权重最高
        title = str(p.get("title", "")).lower()
        score = sum(3 if t in title else 1 for t in terms)
        if family and p.get("doc_family") == family:
            score += 1
        hits.append((score, p))
    hits.sort(key=lambda x: (-x[0], str(x[1].get("title", ""))))
    out = {"ok": True, "query": query, "family": family, "kind": kind,
           "n_matched": len(hits), "returned": min(limit, len(hits)),
           "total_in_db": len(db["pages"]),
           "generated_at": db.get("generated_at"),
           "results": [_brief(p, s) for s, p in hits[:limit]]}
    # 族/类型筛选后零命中时，回传合法取值，避免拼错族名却无提示
    if not hits and (family or kind):
        fam, knd = {}, {}
        for pg in db["pages"]:
            fam[pg.get("doc_family")] = fam.get(pg.get("doc_family"), 0) + 1
            knd[pg.get("doc_kind")] = knd.get(pg.get("doc_kind"), 0) + 1
        if family and family not in fam:
            out["hint"] = f"未知族 family={family!r}"
            out["valid_families"] = fam
            out["ok"] = True
        if kind and kind not in knd:
            out["hint"] = (out.get("hint", "") + f" 未知类型 kind={kind!r}").strip()
            out["valid_kinds"] = knd
    return out


def browse(family: str = None, kind: str = None, limit: int = 100):
    """按族/类型浏览文档条目（不检索）。"""
    db = _load()
    if db is None:
        return {"ok": False, "error_code": "knowledge_missing", "error": "知识库不存在"}
    limit = max(1, min(int(limit or 100), 500))
    items = [p for p in db["pages"]
             if (not family or p.get("doc_family") == family)
             and (not kind or p.get("doc_kind") == kind)]
    fam = {}
    knd = {}
    for p in db["pages"]:
        fam[p.get("doc_family")] = fam.get(p.get("doc_family"), 0) + 1
        knd[p.get("doc_kind")] = knd.get(p.get("doc_kind"), 0) + 1
    return {"ok": True, "family": family, "kind": kind,
            "n_matched": len(items), "returned": min(limit, len(items)),
            "families": fam, "kinds": knd,
            "items": [_brief(p) for p in items[:limit]]}


def get(path: str):
    """按 path 取单条详情（含完整 keywords / versions）。"""
    db = _load()
    if db is None:
        return {"ok": False, "error_code": "knowledge_missing", "error": "知识库不存在"}
    key = str(path or "").strip().lower()
    for p in db["pages"]:
        if str(p.get("path", "")).lower() == key:
            d = dict(p)
            d.pop("_hay", None)
            return {"ok": True, "entry": d}
    return {"ok": False, "error_code": "not_found",
            "error": f"没有这条文档: {path}",
            "next_actions": ["用 origin_search_knowledge 先检索 path"]}


def style_capabilities(plot_type: str = None):
    """查询各类图表**支持哪些样式属性**（源自 origin-mcp 的真机统计）。

    Origin 不同图型支持的属性并不一致——柱状图没有 symbol_kind，散点图没有
    fill_pattern。写样式代码前先查这里，避免属性静默失效。
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "knowledge", "style_capabilities")
    if not os.path.isdir(base):
        return {"ok": False, "error_code": "knowledge_missing",
                "error": f"样式能力目录不存在: {base}"}
    out = {}
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".json"):
            continue
        name = fn[:-5]
        if plot_type and name != plot_type:
            continue
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8") as f:
                out[name] = json.load(f)
        except Exception as e:
            out[name] = {"_error": str(e)}
    if plot_type and plot_type not in out:
        avail = [f[:-5] for f in os.listdir(base) if f.endswith(".json")]
        return {"ok": False, "error_code": "not_found",
                "error": f"没有该图型的样式能力记录: {plot_type}",
                "available": avail}
    return {"ok": True, "plot_type": plot_type, "n": len(out), "capabilities": out}
