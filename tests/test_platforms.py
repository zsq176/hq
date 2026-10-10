"""保留的平台/入口契约：toml_str、configure_stdio、Windows 入口链（hq.cmd/hq.py）。

旧 shell 解析/ACCEPTANCE 检查执行/超时杀树/行式看板与旧 gate 收据测试已随产品
清理删除；原 verify 参数/退出码场景改为 mail 参数错误，继续真实验证字面
%/空格/单引号、UTF-8 及非零退出码（方案 docs/plans/2026-10-03-mail-only-cleanup.md）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import platforms  # noqa: E402
from hqlib.common import normalize_path, toml_str  # noqa: E402

WIN = os.name == "nt"
PY = sys.executable


class TomlStr(unittest.TestCase):
    def test_roundtrip_through_tomllib(self):
        for raw in (r"C:\用户\中文 目录\项目", r'C:\say "hi"\x', "/home/ok", "a\tb\nc"):
            text = f"path = {toml_str(raw)}"
            self.assertEqual(tomllib.loads(text)["path"], raw, text)

    def test_normalize_path_case_alias_windows(self):
        if not WIN:
            self.skipTest("大小写别名仅 Windows 语义")
        base = str(Path(tempfile.gettempdir()).resolve())
        self.assertEqual(normalize_path(base.upper()), normalize_path(base.lower()))


class ConfigureStdio(unittest.TestCase):
    def test_stringio_streams_are_left_alone(self):
        old = sys.stdout, sys.stdin, sys.stderr
        sys.stdout = sys.stdin = sys.stderr = io.StringIO()
        try:
            platforms.configure_stdio()  # 不支持 reconfigure 的流必须保持原样
        finally:
            sys.stdout, sys.stdin, sys.stderr = old


class WinEntry(unittest.TestCase):
    """Windows 入口链：hq.cmd（Python 挑选 + gate/hook JSON 契约）与 hq.py（bootstrap）。"""

    REPO = Path(__file__).resolve().parents[1]

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_gate_outputs_json_without_python_path(self):
        # 模拟 hook 的最小 PATH：没有 python 也会输出 {}（fail-open），退出码 0
        env = dict(os.environ, PATH=r"C:\Windows\System32")
        out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "gate"],
                             input=b'{"cwd": "/"}', capture_output=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {})

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_hook_outputs_json_without_python_path(self):
        # 旧 hook 统一入口同样保持 JSON 契约（无 Python 时 {} exit 0）
        env = dict(os.environ, PATH=r"C:\Windows\System32")
        out = subprocess.run(
            ["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "hook",
             "--agent", "zcode", "--event", "stop"],
            input=b'{"cwd": "/"}', capture_output=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), {})

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_help_and_exit_code(self):
        out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "--help"],
                             capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertIn("henry-hq", out.stdout.decode("utf-8", "replace"))
        # 原为 `verify Z:\no-such` 非零场景；verify 已删，改用 mail 参数错误验证退出码
        out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"),
                              "mail", "Z:\\no-such"],
                             capture_output=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_hq_python_real_venv_with_spaces_and_quote(self):
        """HQ_PYTHON 用真实解释器（含空格/单引号 venv）必须可用，退出码回传。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            venv_dir = Path(d) / "Python With Space's"
            subprocess.run([PY, "-m", "venv", "--without-pip", str(venv_dir)],
                           check=True, capture_output=True, timeout=300)
            venv_py = venv_dir / "Scripts" / "python.exe"
            self.assertTrue(venv_py.is_file(), "venv 未生成解释器，测试自身失效")
            env = dict(os.environ, HQ_PYTHON=str(venv_py))
            out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "--help"],
                                 capture_output=True, env=env, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("henry-hq", out.stdout.decode("utf-8", "replace"))
            out = subprocess.run(
                ["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "mail", "Z:\\no-such"],
                capture_output=True, env=env, timeout=120)
            self.assertNotEqual(out.returncode, 0)  # 退出码经 venv 解释器原样返回

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_literal_percent_argument_survives(self):
        """CALL 二次展开会让字面 %VAR% 被改写。父批处理把含字面百分号的
        参数注入 hq.cmd，CLI 收到的必须是字面原文。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            parent = Path(d) / "parent.bat"
            parent.write_text(
                "@echo off\r\n"
                "setlocal DisableDelayedExpansion\r\n"
                'set "INNER=EXPANDED"\r\n'
                'set "ARG=Z:\\literal-%%INNER%%"\r\n'
                f'"{self.REPO / "bin" / "hq.cmd"}" mail "%ARG%"\r\n',
                encoding="utf-8")
            out = subprocess.run(["cmd", "/c", str(parent)],
                                 capture_output=True, timeout=120)
            err = out.stderr.decode("utf-8", "replace")
            self.assertNotEqual(out.returncode, 0)
            # argparse 以 repr 回显参数值（反斜杠转义为 \\）；判别符取 % 部分：
            # 若被 CALL 二次展开，%INNER% 会变成 EXPANDED
            self.assertIn("literal-%INNER%", err, err)
            self.assertNotIn("literal-EXPANDED", err)     # 不得被二次展开

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_hq_cmd_rejects_bat_wrapper_explicitly(self):
        """HQ_PYTHON 指向 bat/cmd 包装时给出明确不支持提示，不静默回落。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            shim = Path(d) / "py shim.cmd"
            shim.write_text(f'@"{PY}" %*\r\n', encoding="utf-8")
            env = dict(os.environ, HQ_PYTHON=str(shim))
            out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "--help"],
                                 capture_output=True, env=env, timeout=120)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("real interpreter", out.stderr.decode("utf-8", "replace"))
            self.assertNotIn("henry-hq", out.stdout.decode("utf-8", "replace"))  # 未静默回落
            # gate 契约：stdout 恒为合法 JSON
            out = subprocess.run(["cmd", "/c", str(self.REPO / "bin" / "hq.cmd"), "gate"],
                                 input=b'{"cwd": "/"}', capture_output=True, env=env,
                                 timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(json.loads(out.stdout), {})

    @unittest.skipUnless(WIN, "Windows 入口")
    def test_bootstrap_strips_hq_config_and_sets_env(self):
        # 原用 status --json 的登记行数区分配置来源；status 命令已删。改为 mail
        # 参数错误区分：--hq-config 指向“enabled=true 但缺 smtp_host”的配置 →
        # tick 必须以 MailConfigError 退出 2；若 --hq-config 剥离/环境设置时序
        # 错误，配置会落回默认路径，tick 反而打印“未启用”退出 0，无法混过。
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cfg = d / "hq.toml"
            cfg.write_text("[mail]\nenabled = true\n", encoding="utf-8")
            out = subprocess.run(
                [PY, "-X", "utf8", str(self.REPO / "bin" / "hq.py"),
                 "--hq-config", str(cfg), "mail", "tick"],
                capture_output=True, timeout=60, cwd=str(d))
            self.assertEqual(out.returncode, 2, out.stdout + out.stderr)
            self.assertIn("smtp_host", out.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
