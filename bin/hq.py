"""hq 的绝对路径 bootstrap：hooks/调度不依赖用户 PATH 和当前目录。

用法：<python> -X utf8 bin/hq.py --hq-config <绝对路径> <业务参数…>
以自身位置定位源码，--hq-config 在导入 hqlib 之前剥离并写入 HQ_CONFIG——
先设环境再 import，保证 common.CONFIG_PATH 拿到的就是 hook 配置里那一份。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HQ_ROOT = Path(__file__).resolve().parent.parent


def _take_config(argv: list[str]) -> list[str]:
    """剥掉 --hq-config <路径>（或 --hq-config=<路径>），返回业务 argv。"""
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--hq-config":
            if i + 1 >= len(argv):
                sys.stderr.write("hq: --hq-config 需要一个绝对路径参数\n")
                raise SystemExit(2)
            os.environ["HQ_CONFIG"] = os.path.abspath(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--hq-config="):
            os.environ["HQ_CONFIG"] = os.path.abspath(arg.split("=", 1)[1])
            i += 1
            continue
        out.append(arg)
        i += 1
    return out


def main() -> int:
    # 先剥离 --hq-config 再导入 hqlib：common 在 import 时读 HQ_CONFIG，
    # 顺序反了配置就会落到默认路径（已实测复现）。
    argv = _take_config(sys.argv[1:])
    sys.path.insert(0, str(HQ_ROOT))
    from hqlib.platforms import configure_stdio

    configure_stdio()
    from hqlib.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
