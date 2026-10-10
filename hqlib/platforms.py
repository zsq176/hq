"""平台边界：只保留启动编码统一（configure_stdio）。

shell 解析/命令执行/超时杀树/打开文件等旧验收与看板能力已随产品清理移除
（方案 docs/plans/2026-10-03-mail-only-cleanup.md）。不读任务状态，不懂协议。
"""
from __future__ import annotations

import sys


def configure_stdio() -> None:
    """stdin/stdout/stderr 统一 UTF-8。

    Windows 管道默认 GBK/ANSI，中文与 emoji 的 CLI 输出和 JSON 都会炸。测试里被
    替换成 StringIO 的流不支持 reconfigure，保持原样（编码由测试自己控制）。
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass
