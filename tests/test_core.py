"""清理后的核心保护：只读 STATUS 解析、公共配置、原子写、CLI 只公开 mail、
旧 gate/hook 无操作兼容、已删除模块不可加载。

STATUS 解析与配置用例迁移自旧 tests/test_hq.py（对应功能已删，仅保留仍有效的
只读断言）；CLI/Hook/模块加载为本次清理新增的必要回归
（方案 docs/plans/2026-10-03-mail-only-cleanup.md）。
"""
import contextlib
import importlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import common, status  # noqa: E402

REPO = Path(__file__).resolve().parents[1]

STATUS = """---
hq: 1
project: demo
theme: 游戏
value: 好玩
state: active
updated: 2026-09-26 09:00
---
# demo · 状态

> 联机房间号已上线，等真机确认。

## ❓ 待 Henry 判断
- [ ] 真机试听鸭叫 — 怎么验：… — 建议：有意见就写 `→ Henry: …`
- [x] 已看过的截图
- [ ] 水雾浓度 → Henry: 再淡一点
- 无

## ⛔ 阻塞
- 无

## ▶ 下一步（agent 自主推进，无需回复）
- 补 e2e

## ✅ 机器验收

<!-- hq:verify:start -->
- [ ] 这里的列表不算
<!-- hq:verify:end -->
"""


class StatusReadOnly(unittest.TestCase):
    """STATUS.md 只读解析：保留摘要/state/updated/blocked/next/mtime 六字段口径。"""

    def test_parse_returns_kept_fields_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "STATUS.md"
            p.write_text(STATUS, encoding="utf-8")
            st = status.parse_status(p)
        self.assertEqual(set(st), {"summary", "state", "updated", "blocked", "next", "mtime"})
        self.assertEqual(st["summary"], "联机房间号已上线，等真机确认。")
        self.assertEqual(st["state"], "active")
        self.assertEqual(st["updated"], "2026-09-26 09:00")
        self.assertEqual(st["blocked"], [])            # “无”不算条目
        self.assertEqual([i["text"] for i in st["next"]], ["补 e2e"])
        self.assertGreater(st["mtime"], 0)

    def test_missing_file_is_none_and_machine_block_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(status.parse_status(Path(d) / "STATUS.md"))
            p = Path(d) / "STATUS.md"
            p.write_text(STATUS, encoding="utf-8")
            before = status.parse_status(p)
            p.write_text(
                STATUS.replace("- [ ] 这里的列表不算", "- [ ] 换成别的内容"),
                encoding="utf-8")
            after = status.parse_status(p)             # 机器区变化不影响只读解析结果
        self.assertEqual(before["blocked"], after["blocked"])
        self.assertEqual(before["next"], after["next"])
        self.assertEqual(before["summary"], after["summary"])


class CommonConfig(unittest.TestCase):
    def test_load_config_missing_file_is_empty_registry(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(common.load_config(Path(d) / "hq.toml"), {"projects": []})

    def test_load_config_expanduser_and_keeps_unknown_fields(self):
        with tempfile.TemporaryDirectory() as d:
            cfgp = Path(d) / "hq.toml"
            cfgp.write_text('[[projects]]\npath = "~/code/中文 项目"\n'
                            'custom = "保留"\n', encoding="utf-8")
            cfg = common.load_config(cfgp)
            self.assertEqual(len(cfg["projects"]), 1)
            self.assertTrue(Path(cfg["projects"][0]["path"]).is_absolute())
            self.assertEqual(cfg["projects"][0]["custom"], "保留")   # 未知字段不重写不删除

    def test_write_text_creates_parents_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a" / "b" / "f.txt"
            common.write_text(p, "第一行\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "第一行\n")
            common.write_text(p, "第二行\n")                        # 原子替换
            self.assertEqual(p.read_text(encoding="utf-8"), "第二行\n")
            self.assertEqual([x.name for x in p.parent.iterdir()], ["f.txt"])


class CliSurface(unittest.TestCase):
    """对用户公开 mail 与 dashboard；帮助文本不出现已删命令。"""

    def test_public_subcommands(self):
        from hqlib.cli import build_parser

        p = build_parser()
        sub = next(a for a in p._actions if a.dest == "cmd")
        self.assertEqual(list(sub.choices), ["mail", "dashboard"])

    def test_help_lists_mail_and_dashboard(self):
        # 复审 F8：dashboard 已是公开子命令，帮助测试命名/断言随之更新；
        # 已删命令不出现的核心契约保留。
        from hqlib.cli import build_parser

        help_text = build_parser().format_help()
        self.assertIn("mail", help_text)
        self.assertIn("dashboard", help_text)
        for gone in ("verify", "todo", "brief", "init", "mine", "doctor",
                     "nightly", "scheduled", "sync-global", "gate", "hook"):
            self.assertNotIn(gone, help_text, gone)


class OldHookCompat(unittest.TestCase):
    """旧 gate/hook 入口：恒 {}、exit 0，无配置读取、无业务模块加载。"""

    def test_gate_and_hook_noop_without_touching_config(self):
        from hqlib import cli

        real = common.load_config

        def refuse(*a, **k):
            raise AssertionError("旧 Hook 兼容路径不得读配置")

        common.load_config = refuse
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["gate"]), 0)
                self.assertEqual(cli.main(["hook", "--agent", "zcode", "--event", "stop"]), 0)
        finally:
            common.load_config = real
        self.assertEqual(buf.getvalue(), "{}\n{}\n")

    def test_fresh_process_loads_no_business_modules(self):
        """子进程验证：import cli + gate/hook 全程不加载 mail/旧模块
        （主进程内其他测试可能已导入这些模块，不能在进程内判断）。"""
        script = (
            "import contextlib, io, sys\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "from hqlib import cli\n"
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    rc1 = cli.main(['gate'])\n"
            "    rc2 = cli.main(['hook', '--agent', 'zcode', '--event', 'stop'])\n"
            "assert (rc1, rc2) == (0, 0), (rc1, rc2)\n"
            "assert buf.getvalue() == '{}\\n{}\\n', repr(buf.getvalue())\n"
            "for dead in ('hqlib.mail', 'hqlib.gate', 'hqlib.verify', 'hqlib.adapters',"
            " 'hqlib.status', 'hqlib.install'):\n"
            "    assert dead not in sys.modules, dead\n"
            "print('ok')\n")
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", script],
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(b"ok", out.stdout)

    def test_removed_modules_cannot_be_imported(self):
        for name in ("answer", "brief", "fingerprint", "gate", "install", "mine",
                     "tui", "tui_plain", "verify", "adapters"):
            with self.assertRaises(ModuleNotFoundError, msg=name):
                importlib.import_module(f"hqlib.{name}")
            sys.modules.pop(f"hqlib.{name}", None)   # 清理 raise 过程可能留下的桩


if __name__ == "__main__":
    unittest.main()
