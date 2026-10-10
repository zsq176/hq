"""STATUS.md 只读解析（日报用）。

解析刻意宽松：只认 front matter、第一条引用行，以及以 ⛔ ▶ 开头的二级标题下的
列表项。只读：不写项目文件、不算人工/代码指纹、不解析 ACCEPTANCE（这些能力已随
旧验收/看板产品一并移除）。
"""
from __future__ import annotations

import re
from pathlib import Path

_VERIFY_START = "<!-- hq:verify:start -->"
_VERIFY_END = "<!-- hq:verify:end -->"

SECTION_KEYS = {"⛔": "blocked", "▶": "next"}
_EMPTY_ITEMS = {"无", "暂无", "none", "(无)", "（无）", "-"}
_ITEM_RE = re.compile(r"^\s{0,3}[-*]\s+(?:\[(?P<mark>[ xX])\]\s+)?(?P<text>.+?)\s*$")


def split_front_matter(text: str):
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            meta[k.strip()] = v
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def _strip_verify_block(text: str) -> str:
    s, e = text.find(_VERIFY_START), text.find(_VERIFY_END)
    if s >= 0 and e > s:
        return text[:s] + text[e + len(_VERIFY_END):]
    return text


def parse_status(path: Path) -> dict | None:
    """只读解析 STATUS.md：摘要/状态/更新时间/阻塞/下一步 + mtime 快照。
    文件缺失返回 None（调用方按“未记录”展示）。"""
    path = Path(path)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    meta, body = split_front_matter(text)
    body_wo_machine = _strip_verify_block(body)
    summary = ""
    sections = {v: [] for v in SECTION_KEYS.values()}
    current = None
    for line in body_wo_machine.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip()
            current = next((key for emoji, key in SECTION_KEYS.items() if title.startswith(emoji)), None)
            continue
        if stripped.startswith("# "):
            current = None
            continue
        if not summary and stripped.startswith(">"):
            summary = re.sub(r"\*\*(.+?)\*\*", r"\1", stripped.lstrip(">").strip())
            continue
        if current is None:
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        item_text = m.group("text").strip()
        if item_text.lower() in _EMPTY_ITEMS:
            continue
        sections[current].append({"text": item_text})
    return {
        "summary": summary,
        "state": meta.get("state", "active"),
        "updated": meta.get("updated", ""),
        "blocked": sections["blocked"],
        "next": sections["next"],
        "mtime": path.stat().st_mtime,
    }
