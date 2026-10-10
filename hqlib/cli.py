"""hq 命令行入口：对用户公开 mail 与 dashboard（本机单页仪表板）。

旧 gate/hook 业务已随产品清理删除（方案 docs/plans/2026-10-03-mail-only-cleanup.md）。
为不干扰未迁移的外部配置，main 在解析前对这两个旧入口直接 {}、exit 0：
无配置读写、无旧模块加载、无阻挡；其余旧命令一律“不支持”。
"""
from __future__ import annotations

import argparse
import sys


def cmd_mail(args) -> int:
    """本轮结束邮件/日报（方案见 docs/plans/2026-10-02-mail-degraded.md）。

    observe 是 Stop 观察入口：stdout 恒 {}、恒退出 0，任何错误 fail-open。
    --dry-run 只对 setup 有意义：其他动作在任何发送前直接拒绝（复审 F5）。
    """
    import traceback
    from datetime import datetime, timezone

    from . import mail
    from .common import load_config

    action = args.action
    if getattr(args, "dry_run", False) and action != "setup":
        print(f"! --dry-run 只支持 mail setup（mail {action} 不提供干跑）", file=sys.stderr)
        return 2
    if action == "observe":
        return mail.cmd_observe()
    if action == "tick":
        try:
            lines = mail.tick(load_config(), datetime.now(timezone.utc))
        except mail.MailConfigError as exc:
            print(f"! {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            traceback.print_exc()
            print(f"! mail tick 执行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        for line in lines:
            print("•", line)
        if any(("❌" in line or "⚠" in line) for line in lines):
            print("! 本次 tick 存在发送失败/结果待核对的邮件（见上行与 mail status）",
                  file=sys.stderr)
            return 1
        return 0
    if action == "status":
        for line in mail.status_lines():
            print(line)
        return 0
    if action == "test":
        return mail.cmd_test()
    # setup：一次性设置（观察 hook 条目 + 计划任务指引）；--dry-run 只读不动文件。
    # 预览发现解析/结构/IO 问题时退出码非零（复审 R2-2）：不能把带错误的预览
    # 当作安装前验收通过；预览路径不调用 apply、不写文件、不发送。
    ctx = mail.hook_ctx()
    if getattr(args, "dry_run", False):
        info = mail.inspect_setup(load_config(), ctx)
        for line in mail.plan_setup_lines(info, True):
            print(line)
        if info.get("parse_error"):
            print("! 预览发现问题（见上），拒绝安装", file=sys.stderr)
            return 1
        return 0
    messages, errors = mail.apply_setup(load_config(), ctx)
    for line in mail.plan_setup_lines(mail.inspect_setup(load_config(), ctx), False):
        print("•", line)
    for m in messages:
        print("•", m)
    for e in errors:
        print("!", e, file=sys.stderr)
    return 1 if errors else 0


def cmd_dashboard(args) -> int:
    """本机单页仪表板：只读聚合、受限配置与显式 TEST；固定监听 127.0.0.1。"""
    from . import dashboard

    return dashboard.serve(port=args.port, open_browser=not args.no_open)


def _port_type(v: str) -> int:
    """--port 公开面：非整数或超出 1-65535 给 argparse 参数错误，
    不让负数/超范围端口在绑定时抛未处理 OverflowError（复审 F8）。"""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"端口必须是整数，收到 {v!r}")
    if not 1 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"端口必须在 1-65535，收到 {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hq", description="henry-hq：本轮执行结束邮件与 19:00 日报")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser(
        "mail",
        help="本轮执行结束邮件与 19:00 日报：observe|tick|status|test|setup")
    s.add_argument("action", choices=["observe", "tick", "status", "test", "setup"])
    s.add_argument("--dry-run", action="store_true",
                   help="setup 专用：只显示将要做的变更，不改任何文件")
    s.set_defaults(func=cmd_mail)
    s = sub.add_parser(
        "dashboard",
        help="本机单页仪表板：执行/邮件记录、日报与接入事实、受限配置、显式测试邮件")
    s.add_argument("--port", type=_port_type, default=8765,
                   help="本机端口（默认 8765；固定监听 127.0.0.1，占用时明确报错）")
    s.add_argument("--no-open", action="store_true",
                   help="不自动打开浏览器（测试或已有浏览器时使用）")
    s.set_defaults(func=cmd_dashboard)
    return p


def main(argv=None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in ("gate", "hook"):
        # 旧 gate/hook 入口的兼容外壳：外部未迁移配置可能仍引用它们。恒
        # stdout={}、exit 0——不读配置、不加载旧模块、不写库/STATUS、不调模型、
        # 不返回 block；不留下“接口在所以功能还在”的暗含承诺。
        print("{}")
        return 0
    args = build_parser().parse_args(raw)
    return args.func(args) or 0
