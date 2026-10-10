import sys

from .platforms import configure_stdio

configure_stdio()  # 必须先于 cli/gate：Windows 管道默认 GBK，JSON 与 emoji 都会炸

from .cli import main

sys.exit(main())
