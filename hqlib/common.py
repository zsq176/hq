"""路径、配置与少量共享助手（产品只保留 mail；旧协议/品味/简报助手已删）。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - bin/hq re-execs with a newer python
    tomllib = None

HQ_HOME = Path(os.environ.get("HQ_HOME") or Path(__file__).resolve().parent.parent)
CONFIG_PATH = Path(os.environ.get("HQ_CONFIG") or HQ_HOME / "hq.toml")

STATUS_FILE = "STATUS.md"


def load_config(path: Path = None) -> dict:
    """读登记表；缺失返回空登记。未知字段原样保留（已有实例不重写、不删除）。"""
    path = Path(path or CONFIG_PATH)
    if not path.exists():
        return {"projects": []}
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    cfg.setdefault("projects", [])
    for p in cfg["projects"]:
        p["path"] = os.path.expanduser(p["path"])
    return cfg


def normalize_path(p) -> Path:
    """展开 ~ 并解析为规范绝对路径。登记比较与 hook 的已登记项目过滤共用这一条
    规则：Windows 的大小写别名/8.3 短名由 resolve 归一到磁盘上的真实形态。"""
    path = Path(os.path.expanduser(str(p)))
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def toml_str(value: str) -> str:
    """TOML 基本字符串字面量：反斜杠/引号/控制字符必须转义，登记的 Windows 路径
    才能被 tomllib 原样读回（中文等非 ASCII 字符保持字面，文件本身是 UTF-8）。"""
    out = []
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_text(path: Path, text: str) -> None:
    """原子写：避免 agent 与 hq 同时写时读到半截文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
