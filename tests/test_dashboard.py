"""本机单页仪表板（docs/plans/2026-10-04-local-dashboard.md）单元验证。

覆盖：事实只读聚合（缺库不建库/状态口径/聚合不受分页截断/筛选/未知原文）、
受限配置（未知字段保留/备份/409/写失败保持原状/reparse 拒绝/启用校验）、
TOML 无损编码（roundtrip/有损拒绝）、HTTP 边界（Host/Origin/CSRF/413/上限）、
显式 TEST（fake sender/重复 ID/忙拒绝）、PS StatusJson（结构化失败）。
mail.py 与 test_mail.py 冻结：本文件只调用其公开契约。
"""
import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import common, dashboard, mail  # noqa: E402


def bj(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=mail.BEIJING)


# 访问日志行格式：`[hq dashboard] METHOD PATH`（复审 B4：不能把
# "[hq dashboard]" 误当一个 token 再用 parts[1]/[2] 索引）。
_LOG_LINE = re.compile(r"^\[hq dashboard\] (\S+) (\S+)$")


def assert_dashboard_log(testcase, text, expect_lines=()):
    """日志断言的正向控制（复审 B4）：先断言捕获非空且每行都符合固定格式、
    预期行确实存在，再由调用方断言哨兵/未知内容不存在——空捕获不能证明
    任何"未出现"。返回解析出的 (method, path) 元组列表。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    testcase.assertTrue(lines, "日志捕获为空——捕获夹具空跑（正向控制失败）")
    parsed = []
    for ln in lines:
        m = _LOG_LINE.match(ln)
        testcase.assertIsNotNone(m, f"日志行格式不符: {ln!r}")
        parsed.append((m.group(1), m.group(2)))
    for want in expect_lines:
        testcase.assertIn(want, parsed, f"缺少预期日志行 {want}（正向控制）")
    return parsed


class _Home(unittest.TestCase):
    """每个用例独立的 HQ_HOME（隔离配置/数据库/备份）。

    复审 F7：common.HQ_HOME / common.CONFIG_PATH 在模块导入时已固定，
    只改 os.environ 会让 save_config 备份落到导入时的真实根目录。这里
    两个都补丁并在用例结束后恢复。"""

    def setUp(self):
        self._old_home = os.environ.get("HQ_HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HQ_HOME"] = self._tmp.name
        self._old_common_home = common.HQ_HOME
        self._old_config_path = common.CONFIG_PATH
        common.HQ_HOME = Path(self._tmp.name)
        common.CONFIG_PATH = Path(self._tmp.name) / "hq.toml"
        self.home = Path(self._tmp.name)
        self._stage_scripts()
        self.addCleanup(self._cleanup)

    def _stage_scripts(self):
        """把仓库的 install-mail-task.ps1 复制进隔离 HQ_HOME/scripts，
        _task_status_json 的脚本存在性检查才与现实一致（不注册任何任务）。"""
        src = Path(__file__).resolve().parent.parent / "scripts" / "install-mail-task.ps1"
        if src.is_file():
            dst = Path(self.home if hasattr(self, "home") else self._tmp.name) / "scripts"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst / src.name)

    def _cleanup(self):
        if self._old_home is None:
            os.environ.pop("HQ_HOME", None)
        else:
            os.environ["HQ_HOME"] = self._old_home
        common.HQ_HOME = self._old_common_home
        common.CONFIG_PATH = self._old_config_path
        self._tmp.cleanup()

    # ---- 常用构造 ----

    def write_config(self, mail_enabled=False, smtp_host="smtp.example.com",
                     projects=(), extra="", to_addr="v@example.com"):
        proj_lines = "".join(
            f'\n[[projects]]\npath = "{Path(p).as_posix()}"\n' for p in projects)
        cfg = self.home / "hq.toml"
        cfg.write_text(
            "[mail]\n"
            f"enabled = {str(mail_enabled).lower()}\n"
            f'smtp_host = "{smtp_host}"\n'
            "smtp_port = 465\n"
            'security = "ssl"\n'
            'username = "u@example.com"\n'
            'password_env = "HQ_SMTP_PASSWORD_DASH_TEST"\n'
            'from_addr = "u@example.com"\n'
            f'to_addr = "{to_addr}"\n'
            + proj_lines + extra, encoding="utf-8")
        return cfg

    def make_dirs(self, *names):
        out = []
        for n in names:
            p = self.home / n
            p.mkdir(parents=True, exist_ok=True)
            out.append(str(p))
        return out

    def seed_db(self, events=(), deliveries=()):
        """经冻结的 mail.connect 造库（测试数据侧允许；仪表板读路径仍只读）。"""
        db = mail.connect()
        try:
            for e in events:
                db.execute("INSERT OR REPLACE INTO events"
                           "(session,turn,project,observed_at_utc,confirmed_at_utc,"
                           "outcome,reply_summary,notified) VALUES (?,?,?,?,?,?,?,?)",
                           (e["session"], e["turn"], e["project"], e["observed"],
                            e.get("confirmed"), e["outcome"], e.get("summary"),
                            e.get("notified", 0)))
            for d in deliveries:
                db.execute("INSERT OR REPLACE INTO deliveries"
                           "(key,kind,subject,body,state,created_at_utc,sent_at_utc,"
                           "error,deadline_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                           (d["key"], d["kind"], d["subject"], d["body"], d["state"],
                            d["created"], d.get("sent"), d.get("error"),
                            d.get("deadline")))
        finally:
            db.close()

    def snap(self, cfg, **kw):
        now = kw.pop("now", None) or datetime.now(timezone.utc)
        kw.setdefault("project", None)
        kw.setdefault("period", "all")
        kw.setdefault("attention", False)
        kw.setdefault("offset", 0)
        kw.setdefault("limit", 50)
        return dashboard.read_snapshot(cfg, now=now, **kw)


class SnapshotFacts(_Home):
    def test_missing_db_creates_nothing(self):
        cfg = self.write_config()
        snap = self.snap(cfg)
        self.assertEqual(snap["source"], {"state": "missing", "detail": None})
        self.assertIsNone(snap["counts"]["completed_24h"])
        self.assertIsNone(snap["counts"]["pending_confirm"])
        self.assertFalse((self.home / ".hq").exists(), "缺库时不得创建 .hq 目录")
        self.assertFalse((self.home / ".hq" / "mail.sqlite3").exists())

    def test_corrupt_db_classified(self):
        cfg = self.write_config()
        (self.home / ".hq").mkdir()
        (self.home / ".hq" / "mail.sqlite3").write_bytes(b"definitely not sqlite")
        snap = self.snap(cfg)
        self.assertEqual(snap["source"]["state"], "error")
        self.assertIn("坏库", snap["source"]["detail"])

    def test_locked_db_degrades_to_error(self):
        cfg = self.write_config()
        self.seed_db(events=[dict(session="s", turn="1", project=str(self.home),
                                  observed=mail.utc_iso(bj(2026, 10, 3, 20, 0)),
                                  outcome="pending")])
        writer = mail.connect()
        try:
            writer.execute("BEGIN EXCLUSIVE")
            snap = self.snap(cfg)          # 读被写锁打断 → 降级而非抛异常
            self.assertEqual(snap["source"]["state"], "error")
            self.assertIn("锁定", snap["source"]["detail"])
            self.assertIsNone(snap["counts"]["completed_24h"])
            self.assertEqual(snap["rows"], [])
        finally:
            writer.execute("ROLLBACK")
            writer.close()

    def test_state_labels_full_matrix(self):
        cfg = self.write_config()
        # 固定 now：北京 2026-10-03 21:00（夜间窗口内）
        now = bj(2026, 10, 3, 21, 0)
        ok = bj(2026, 10, 3, 20, 0)                    # 窗口内完成
        day = bj(2026, 10, 3, 15, 0)                   # 日间完成 → 仅日报
        past = bj(2026, 10, 2, 22, 0)                  # 已过截止
        day_iso, ok_iso, past_iso = map(mail.utc_iso, (day, ok, past))
        ev = [
            dict(session="s1", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed", summary="ok"),
            dict(session="s2", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s3", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s4", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s5", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s6", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s7", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s8", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="completed"),
            dict(session="s9", turn="1", project=str(self.home), observed=day_iso,
                 confirmed=day_iso, outcome="completed"),
            dict(session="s10", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=None, outcome="pending"),
            dict(session="s11", turn="1", project=str(self.home), observed=ok_iso,
                 confirmed=ok_iso, outcome="crashed"),   # 未知终态 → 原文
        ]
        dl = [
            dict(key=mail.round_key("s1", "1"), kind="round", subject="t", body="b",
                 state="sent", created=ok_iso, sent=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
            dict(key=mail.round_key("s2", "1"), kind="round", subject="t", body="b",
                 state="pending", created=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),       # 未过期 → 待处理
            dict(key=mail.round_key("s3", "1"), kind="round", subject="t", body="b",
                 state="pending", created=past_iso,
                 deadline=mail.utc_iso(mail.round_deadline(past))),     # 已过期 → 待核对
            dict(key=mail.round_key("s4", "1"), kind="round", subject="t", body="b",
                 state="failed", created=ok_iso, error="boom",
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
            dict(key=mail.round_key("s5", "1"), kind="round", subject="t", body="b",
                 state="uncertain", created=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
            dict(key=mail.round_key("s6", "1"), kind="round", subject="t", body="b",
                 state="sending", created=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
            dict(key=mail.round_key("s7", "1"), kind="round", subject="t", body="b",
                 state="skipped", created=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
            dict(key=mail.round_key("s8", "1"), kind="round", subject="t", body="b",
                 state="weird-state", created=ok_iso,
                 deadline=mail.utc_iso(mail.round_deadline(ok))),
        ]
        self.seed_db(ev, dl)
        snap = self.snap(cfg, now=now)
        labels = {r["session"]: (r["label"], r["attention"]) for r in snap["rows"]}
        self.assertEqual(labels["s1"], ("SMTP已接受", False))
        self.assertEqual(labels["s2"], ("待处理", False))
        self.assertEqual(labels["s3"], ("待核对", True))
        self.assertEqual(labels["s4"], ("发送失败", True))
        self.assertEqual(labels["s5"], ("结果待核对", True))
        self.assertEqual(labels["s6"], ("发送中或结果待核对", True))
        self.assertEqual(labels["s7"], ("已跳过（只进日报）", False))
        self.assertEqual(labels["s8"], ("weird-state（未知状态原文）", True))
        self.assertEqual(labels["s9"], ("仅日报", False))
        self.assertEqual(labels["s10"], ("待确认", False))
        self.assertEqual(labels["s11"], ("crashed", False), "未知 outcome 原文展示")
        self.assertEqual(snap["counts"]["completed_24h"], 9)
        self.assertEqual(snap["counts"]["attention"], 5)
        self.assertEqual(snap["counts"]["pending_confirm"], 1)
        self.assertEqual(snap["counts"]["smtp_accepted_24h"], 1)

    def test_counts_survive_pagination(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 3, 20, 0))
        self.seed_db([dict(session=f"s{i}", turn="1", project=str(self.home),
                           observed=ok, confirmed=ok, outcome="completed")
                      for i in range(3)])
        snap = self.snap(cfg, now=bj(2026, 10, 3, 21, 0), limit=1)
        self.assertEqual(len(snap["rows"]), 1)
        self.assertEqual(snap["total"], 3, "分页只截断展示行")
        self.assertEqual(snap["counts"]["completed_24h"], 3, "聚合计数来自全量")

    def test_filters(self):
        pa, pb = self.make_dirs("proj-a", "proj-b")
        cfg = self.write_config(projects=(pa,))
        # 复审 F3 后：夜间窗口完成且无投递记录的轮次按完成时间推导截止，
        # 已过期即"待核对"并计入需要注意。本用例验证筛选与 attention 组合，
        # 因此种子全部用日间完成（仅日报）或 pending，避免混入需要注意行。
        ok = bj(2026, 10, 3, 15, 0)
        old = bj(2026, 9, 1, 15, 0)
        self.seed_db([
            dict(session="a", turn="1", project=pa, observed=mail.utc_iso(ok),
                 confirmed=mail.utc_iso(ok), outcome="pending"),
            dict(session="b", turn="1", project=pb, observed=mail.utc_iso(ok),
                 confirmed=mail.utc_iso(ok), outcome="completed", summary="b"),
            dict(session="c", turn="1", project=pa, observed=mail.utc_iso(old),
                 confirmed=mail.utc_iso(old), outcome="completed", summary="old"),
        ])
        self.assertEqual(self.snap(cfg, project=pa)["total"], 2)
        self.assertEqual(self.snap(cfg, now=bj(2026, 10, 3, 21, 0),
                                   project=pa, period="24h")["total"], 1)
        self.assertEqual(self.snap(cfg, now=bj(2026, 10, 3, 21, 0),
                                   project=pa, period="7d")["total"], 1,
                         "c 案例早于 7 天窗口")
        self.assertEqual(self.snap(cfg, now=bj(2026, 10, 3, 21, 0),
                                   period="24h")["total"], 2)
        at = self.snap(cfg, attention=True)
        self.assertEqual(at["total"], 0)
        # 计数仍是全库口径
        self.assertEqual(at["counts"]["pending_confirm"], 1)

    def test_recent_dailies_coverage(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 3, 19, 0))
        self.seed_db(deliveries=[
            dict(key="daily:20261003", kind="daily", subject="日报", body="b",
                 state="sent", created=ok, sent=ok),
            dict(key="weird", kind="daily", subject="x", body="b", state="pending",
                 created=ok),
        ])
        snap = self.snap(cfg)
        cov = {d["key"]: d["coverage"] for d in snap["recent_dailies"]}
        self.assertEqual(cov["daily:20261003"], "2026-10-03 19:00（北京时间截止点）")
        self.assertIsNone(cov["weird"])

    def test_read_only_no_sidecars(self):
        cfg = self.write_config()
        self.seed_db(events=[dict(session="s", turn="1", project=str(self.home),
                                  observed=mail.utc_iso(bj(2026, 10, 3, 20, 0)),
                                  outcome="pending")])
        dbdir = self.home / ".hq"
        before = sorted(p.name for p in dbdir.iterdir())
        self.snap(cfg)
        self.snap(cfg)
        after = sorted(p.name for p in dbdir.iterdir())
        self.assertEqual(before, after, f"只读访问不得产生 sidecar: {before} -> {after}")

    def test_read_delivery(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 3, 19, 0))
        self.seed_db(deliveries=[dict(key=mail.round_key("s1", "2"), kind="round",
                                      subject="完成通知", body="正文X", state="sent",
                                      created=ok, sent=ok)])
        row = dashboard.read_delivery(mail.round_key("s1", "2"))
        self.assertEqual(row["subject"], "完成通知")
        self.assertEqual(row["body"], "正文X")
        self.assertIsNone(dashboard.read_delivery("round:nope:9"))


class ConfigPanel(_Home):
    def test_read_config_no_secrets(self):
        os.environ["HQ_SMTP_PASSWORD_DASH_TEST"] = "super-secret-value"
        self.addCleanup(os.environ.pop, "HQ_SMTP_PASSWORD_DASH_TEST", None)
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        self.assertEqual(rc["source"], "ok")
        self.assertEqual(len(rc["revision"]), 64)
        self.assertTrue(rc["password_present"])
        self.assertEqual(rc["password_env"], "HQ_SMTP_PASSWORD_DASH_TEST")
        blob = json.dumps(rc, ensure_ascii=False)
        self.assertNotIn("super-secret-value", blob, "密码值绝不回传")
        self.assertNotIn("password_value", blob)

    def test_save_preserves_unknown_fields(self):
        pa, pb = self.make_dirs("proj-a", "proj-b")
        cfg = self.write_config(projects=(pa,),
                                extra="\n[extra]\nkeep_me = \"yes\"\n")
        rc = dashboard.read_config(cfg)
        # 项目带未知字段
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       .replace(f'path = "{Path(pa).as_posix()}"',
                                f'path = "{Path(pa).as_posix()}"\nowner = "henry"'),
                       encoding="utf-8")
        rc = dashboard.read_config(cfg)
        res = dashboard.save_config(cfg, {"projects": [{"path": str(pa)},
                                                       {"path": str(pb)}]},
                                    rc["revision"])
        self.assertTrue(res["ok"])
        raw = cfg.read_text(encoding="utf-8")
        self.assertIn("owner", raw, "项目未知字段按规范路径合并保留")
        self.assertIn("keep_me", raw, "顶层未知表保留")
        self.assertEqual(len(dashboard.read_config(cfg)["projects"]), 2)

    def test_save_backup_and_first_create(self):
        pa = self.make_dirs("proj-a")[0]
        cfg = self.write_config(projects=(pa,))
        rc = dashboard.read_config(cfg)
        before = cfg.read_bytes()
        res = dashboard.save_config(cfg, {"mail": {"to_addr": "w@example.com"}},
                                    rc["revision"])
        backup = Path(res["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), before, "备份=改动前原字节")
        self.assertNotEqual(res["revision"], rc["revision"])

        # 首建：文件不存在 + revision="missing" → 允许，无备份
        cfg2 = self.home / "new.toml"
        res2 = dashboard.save_config(cfg2, {"mail": {"smtp_host": "s.example.com",
                                                     "smtp_port": 587,
                                                     "security": "starttls",
                                                     "from_addr": "a@b.c",
                                                     "to_addr": "d@e.f",
                                                     "password_env": "HQ_SMTP_PASSWORD_DASH_TEST"},
                                           "projects": [{"path": str(pa)}]},
                                     "missing")
        self.assertTrue(res2["ok"])
        self.assertIsNone(res2["backup"])
        self.assertEqual(dashboard.read_config(cfg2)["mail"]["smtp_host"], "s.example.com")

    def test_save_conflict_on_stale_revision(self):
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        cfg.write_text(cfg.read_text(encoding="utf-8") + "\n# external\n",
                       encoding="utf-8")
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {"to_addr": "x@example.com"}},
                                  rc["revision"])
        self.assertEqual(cm.exception.kind, "conflict")

    def test_save_rechecks_revision_before_write(self):
        cfg = self.write_config()
        orig = dashboard._read_raw_toml
        state = {"n": 0}

        def spy(p):
            data, raw, err = orig(p)
            if state["n"] == 0:
                state["n"] = 1
                p.write_bytes(raw + b"\n# touched-by-race\n")  # 模拟写入前外部修改
            return data, raw, err

        dashboard._read_raw_toml = spy
        try:
            rc = dashboard.read_config(cfg)
            with self.assertRaises(dashboard.ConfigError) as cm:
                dashboard.save_config(cfg, {"mail": {"to_addr": "x@example.com"}},
                                      rc["revision"])
        finally:
            dashboard._read_raw_toml = orig
        self.assertEqual(cm.exception.kind, "conflict")
        self.assertIn("touched-by-race", cfg.read_text(encoding="utf-8"),
                      "被拒保存不得覆盖外部修改")

    def test_save_write_failure_keeps_original(self):
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        before = cfg.read_bytes()
        orig = common.write_text

        def boom(path, text):
            raise OSError("disk full (simulated)")

        common.write_text = boom
        try:
            with self.assertRaises(dashboard.ConfigError) as cm:
                dashboard.save_config(cfg, {"mail": {"to_addr": "x@example.com"}},
                                      rc["revision"])
        finally:
            common.write_text = orig
        self.assertEqual(cm.exception.kind, "conflict")
        self.assertEqual(cfg.read_bytes(), before, "写失败后原文件逐字节不变")

    def test_save_rejects_bad_patches(self):
        pa = self.make_dirs("proj-a")[0]
        cfg = self.write_config(projects=(pa,))
        rc = dashboard.read_config(cfg)
        for patch in (
                {"unknown_key": 1},
                {"projects": "nope"},
                {"projects": [{"path": str(pa), "extra": 1}]},
                {"projects": [{"path": str(self.home / "nope-dir")}]},
                {"projects": [{"path": "relative/path"}]},
                {"projects": [{"path": str(pa)}, {"path": str(pa)}]},
                {"mail": {"smtp_port": "465"}},
                {"mail": {"smtp_port": 0}},
                {"mail": {"security": "tls"}},
                {"mail": {"from_addr": "not-an-address"}},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(dashboard.ConfigError) as cm:
                    dashboard.save_config(cfg, patch, rc["revision"])
                self.assertEqual(cm.exception.kind, "invalid")

    def test_save_refuses_reparse_chain(self):
        # 用 Windows reparse point（junction）拦截：无特权可建 junction 目录
        pa = self.make_dirs("proj-a")[0]
        link = self.home / "link-dir"
        made = False
        try:
            import _winapi
            _winapi.CreateJunction(str(self.home / "proj-a"), str(link))
            made = True
        except (ImportError, OSError):
            self.skipTest("无法创建 junction")
        if not made:  # pragma: no cover
            return
        target = link / "hq.toml"
        target.write_text('[mail]\nenabled = false\nsmtp_host = "s"\nsmtp_port = 1\n'
                          'security = "ssl"\nusername = "u@x.y"\n'
                          'password_env = "HQ_SMTP_PASSWORD_DASH_TEST"\n'
                          'from_addr = "u@x.y"\nto_addr = "v@x.y"\n', encoding="utf-8")
        rc = dashboard.read_config(target)
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(target, {"mail": {"to_addr": "z@x.y"}},
                                  rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertIn("reparse", cm.exception.detail)

    def test_enable_validation_and_password_gate(self):
        cfg = self.write_config()          # 字段齐全，只缺密码环境变量
        os.environ.pop("HQ_SMTP_PASSWORD_DASH_TEST", None)
        rc = dashboard.read_config(cfg)
        self.assertFalse(rc["password_present"])
        self.assertEqual([p for p in rc["validation"]["enable_problems"]
                          if "密码环境变量" in p], rc["validation"]["enable_problems"],
                         "字段齐全时启用阻塞只剩密码")
        os.environ["HQ_SMTP_PASSWORD_DASH_TEST"] = "x"
        try:
            rc = dashboard.read_config(cfg)
            self.assertEqual(rc["validation"]["enable_problems"], [])
        finally:
            os.environ.pop("HQ_SMTP_PASSWORD_DASH_TEST", None)
        # enabled 仍为 false，密码检查不落盘改配置
        self.assertFalse(rc["mail"]["enabled"])
        # smtp_host 为空 → 启用被拒
        cfg2 = self.write_config(smtp_host="")
        rc2 = dashboard.read_config(cfg2)
        self.assertTrue(any("smtp_host" in p for p in rc2["validation"]["enable_problems"]))

    def test_draft_problems_even_when_paused(self):
        cfg = self.write_config(to_addr="bad addr")
        rc = dashboard.read_config(cfg)
        self.assertTrue(any("from_addr" in p or "to_addr" in p
                            for p in rc["validation"]["draft_problems"]),
                        rc["validation"])


class TomlCodec(_Home):
    def test_roundtrip_tricky_values(self):
        data = {
            "plain": "v", "uni": "引号\"与\n换行", "num": 42, "port": 465,
            "flag": True, "off": False, "flt": 1.5,
            "arr": ["a", "b"], "mixed": [1, "x", True],
            "dotted key": "quoted", "表": {"nested": "值", "n": 2},
            "projects": [{"path": "D:/x", "owner": "h"}],
        }
        enc = dashboard._toml_encode(data)
        reparsed = __import__("tomllib").loads(enc)
        self.assertTrue(dashboard._same_value(reparsed, data))
        self.assertEqual(dashboard._same_value(True, 1), False, "bool/int 不得混同")
        self.assertEqual(dashboard._same_value(1, 1.0), False)

    def test_lossy_types_refused(self):
        with self.assertRaises(dashboard._Lossy):
            dashboard._toml_encode({"when": datetime(2026, 10, 4, tzinfo=timezone.utc)})
        with self.assertRaises(dashboard._Lossy):
            dashboard._toml_encode({"x": [1, {"y": 2}]}, )  # 混合列表无法内联
        with self.assertRaises(dashboard._Lossy):
            dashboard._toml_encode({"arr": [[1, 2], [3]]})  # 列表套列表

    def test_lossy_end_to_end_keeps_file(self):
        pa = self.make_dirs("proj-a")[0]
        cfg = self.write_config(projects=(pa,),
                                extra="\n[archive]\nwhen = 2026-10-04T00:00:00Z\n")
        before = cfg.read_bytes()
        rc = dashboard.read_config(cfg)
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {"to_addr": "z@example.com"}},
                                  rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertEqual(cfg.read_bytes(), before, "有损风险下原文件一字节不动")


def _fake_ps_result(*, returncode=0, stdout=b"", stderr=b""):
    class _R:
        pass
    r = _R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TaskStatusJson(_Home):
    def setUp(self):
        super().setUp()
        self.cfg = self.write_config()

    def _run_with(self, runner):
        orig = dashboard.subprocess.run
        dashboard.subprocess.run = runner
        try:
            return dashboard._task_status_json(self.cfg)
        finally:
            dashboard.subprocess.run = orig

    def test_ok_payload_passthrough(self):
        payload = {"ok": True, "task": "hq-mail-tick-12345678",
                   "instance_config": "x", "installed": True, "state": "Ready",
                   "last_run_time": "2026-10-04T09:00:00+08:00",
                   "last_task_result": 0}
        out = self._run_with(lambda *a, **k: _fake_ps_result(
            stdout=json.dumps(payload).encode()))
        self.assertEqual(out["source"], "ok")
        self.assertEqual(out["task"], "hq-mail-tick-12345678")
        self.assertEqual(out["last_task_result"], 0)

    def test_structured_failures(self):
        cases = {
            "timeout": (lambda *a, **k: (_ for _ in ()).throw(
                __import__("subprocess").TimeoutExpired(cmd="ps", timeout=10)),
                "超时"),
            "nonzero": (lambda *a, **k: _fake_ps_result(returncode=1, stderr=b"boom"),
                        "退出码"),
            "badjson": (lambda *a, **k: _fake_ps_result(stdout=b"not json"),
                        "不是 JSON"),
            "okfalse": (lambda *a, **k: _fake_ps_result(
                stdout=json.dumps({"ok": False, "error": "x"}).encode()), "异常"),
        }
        for name, (runner, expect) in cases.items():
            with self.subTest(case=name):
                out = self._run_with(runner)
                self.assertEqual(out["source"], "error")
                self.assertIn(expect, out["detail"])


class SendTest(_Home):
    def setUp(self):
        super().setUp()
        self.cfg = self.write_config()
        os.environ["HQ_SMTP_PASSWORD_DASH_TEST"] = "x"
        self.addCleanup(os.environ.pop, "HQ_SMTP_PASSWORD_DASH_TEST", None)

    def _fake_sender(self, exc=None):
        calls = []

        def sender(mcfg, subject, body, mid):
            calls.append((mcfg, subject, body, mid))
            if exc is not None:
                raise exc
        return calls, sender

    def test_accepted_via_fake_sender(self):
        calls, sender = self._fake_sender()
        before = self.cfg.read_bytes()
        out = dashboard.send_test(self.cfg, sender=sender,
                                  now=bj(2026, 10, 4, 15, 0))
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(out["to"], "v@example.com")
        self.assertEqual(len(calls), 1)
        self.assertIn("[hq][TEST]", calls[0][1])
        self.assertEqual(self.cfg.read_bytes(), before,
                         "TEST 不得修改配置文件")

    def test_rejected_and_unknown(self):
        calls, sender = self._fake_sender(mail.MailPermanentError("550 no"))
        self.assertEqual(dashboard.send_test(self.cfg, sender=sender)["status"],
                         "rejected")
        calls, sender = self._fake_sender(mail.MailUnknownError("timeout"))
        self.assertEqual(dashboard.send_test(self.cfg, sender=sender)["status"],
                         "unknown")
        calls, sender = self._fake_sender(RuntimeError("boom"))
        out = dashboard.send_test(self.cfg, sender=sender)
        self.assertEqual(out["status"], "unknown", "未预期异常同样按结果不明")

    def test_disabled_config_temporarily_enabled(self):
        # enabled=false 但字段完整：显式 TEST 允许（临时按启用校验），不落盘
        out = dashboard.send_test(self.cfg,
                                  sender=lambda *a: None,
                                  now=bj(2026, 10, 4, 15, 0))
        self.assertEqual(out["status"], "accepted")
        self.assertFalse(dashboard.read_config(self.cfg)["mail"]["enabled"])

    def test_incomplete_config_rejected(self):
        cfg2 = self.write_config(smtp_host="")
        with self.assertRaises(mail.MailConfigError):
            dashboard.send_test(cfg2, sender=lambda *a: None)

    def test_no_db_writes(self):
        self.seed_db(events=[dict(session="s", turn="1", project=str(self.home),
                                  observed=mail.utc_iso(bj(2026, 10, 3, 20, 0)),
                                  outcome="pending")])
        dbdir = self.home / ".hq"
        before = sorted(p.name for p in dbdir.iterdir())
        dashboard.send_test(self.cfg, sender=lambda *a: None)
        after = sorted(p.name for p in dbdir.iterdir())
        self.assertEqual(before, after, "TEST 不写业务库")


class _HttpBase(unittest.TestCase):
    """进程内真实 HTTP 服务（127.0.0.1 随机端口），头级控制用 http.client。"""

    _tmp = None
    _old_home = None
    _old_common_home = None
    _old_config_path = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._old_home = os.environ.get("HQ_HOME")
        os.environ["HQ_HOME"] = cls._tmp.name
        # 复审 F7：与 _Home 同口径，导入时固定的 common 路径一并补丁
        cls._old_common_home = common.HQ_HOME
        cls._old_config_path = common.CONFIG_PATH
        common.HQ_HOME = Path(cls._tmp.name)
        common.CONFIG_PATH = Path(cls._tmp.name) / "hq.toml"
        _Home._stage_scripts(cls)
        cls.addClassCleanup(cls._tmp.cleanup)
        (Path(cls._tmp.name) / "proj").mkdir()
        cfg = Path(cls._tmp.name) / "hq.toml"
        cfg.write_text(
            "[mail]\nenabled = false\nsmtp_host = \"smtp.example.com\"\n"
            "smtp_port = 465\nsecurity = \"ssl\"\nusername = \"u@example.com\"\n"
            "password_env = \"HQ_SMTP_PASSWORD_DASH_TEST\"\n"
            "from_addr = \"u@example.com\"\nto_addr = \"v@example.com\"\n"
            f"\n[[projects]]\npath = \"{(Path(cls._tmp.name) / 'proj').as_posix()}\"\n",
            encoding="utf-8")
        cls._srv = dashboard._Server(("127.0.0.1", 0), dashboard._Handler)
        cls.app = dashboard._App(cfg, secrets.token_urlsafe(32))
        cls._srv.app = cls.app
        cls._srv.page = dashboard._load_page().replace(
            b"@@TOKEN@@", cls.app.token.encode("ascii"))
        cls._thread = threading.Thread(target=cls._srv.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls._srv.shutdown()
        cls._srv.server_close()
        if cls._old_home is None:
            os.environ.pop("HQ_HOME", None)
        else:
            os.environ["HQ_HOME"] = cls._old_home
        common.HQ_HOME = cls._old_common_home
        common.CONFIG_PATH = cls._old_config_path

    def req(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self._srv.server_address[1],
                                       timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            data = r.read()
            return r.status, data, {k.lower(): v for k, v in r.getheaders()}
        finally:
            c.close()

    def origin(self):
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def post_json(self, path, obj, *, csrf=True, origin=True, ctype="application/json"):
        h = {}
        if origin:
            h["Origin"] = self.origin()
        if ctype:
            h["Content-Type"] = ctype
        if csrf:
            h["X-HQ-CSRF"] = self.app.token
        return self.req("POST", path, headers=h,
                        body=json.dumps(obj, ensure_ascii=False).encode("utf-8"))


class HttpBoundaries(_HttpBase):
    def test_page_and_security_headers(self):
        status, body, h = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"@@", body, "token 占位必须已替换")
        self.assertIn(self.app.token.encode(), body, "页面内嵌 CSRF/nonce")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertIn("nonce-" + self.app.token, h["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])

    def test_snapshot_api(self):
        status, body, _ = self.req("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        j = json.loads(body.decode("utf-8"))
        self.assertEqual(j["counts"]["projects"], 1)
        self.assertEqual(j["test"]["status"], "not_tested")

    def test_bad_params(self):
        for path in ("/api/snapshot?limit=500", "/api/snapshot?limit=0",
                     "/api/snapshot?limit=abc", "/api/snapshot?period=year"):
            with self.subTest(path=path):
                status, _, _ = self.req("GET", path)
                self.assertEqual(status, 400)

    def test_host_origin_rejections(self):
        status, body, _ = self.req(
            "GET", "/api/snapshot", headers={"Host": "evil.example:1"})
        self.assertEqual(status, 403)
        status, _, _ = self.req("GET", "/api/snapshot",
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.req(
            "GET", "/api/snapshot",
            headers={"Sec-Fetch-Site": "cross-site", "Origin": self.origin()})
        self.assertEqual(status, 403)

    def test_write_security(self):
        # 无 Origin
        status, _, _ = self.req("POST", "/api/config",
                                headers={"Content-Type": "application/json"},
                                body=json.dumps({"revision": "x", "patch": {}}))
        self.assertEqual(status, 403)
        # 有 Origin 无 CSRF
        status, _, _ = self.req("POST", "/api/config",
                                headers={"Origin": self.origin(),
                                         "Content-Type": "application/json"},
                                body=json.dumps({"revision": "x", "patch": {}}))
        self.assertEqual(status, 403)
        # 错 CSRF
        status, _, _ = self.post_json("/api/config",
                                      {"revision": "x", "patch": {}}, csrf=False)
        self.assertEqual(status, 403)
        # 错 Content-Type
        status, _, _ = self.req("POST", "/api/config",
                                headers={"Origin": self.origin(),
                                         "Content-Type": "text/plain",
                                         "X-HQ-CSRF": self.app.token},
                                body=json.dumps({"revision": "x", "patch": {}}))
        self.assertEqual(status, 403)
        # 非 JSON object
        status, _, _ = self.req("POST", "/api/config",
                                headers={"Origin": self.origin(),
                                         "Content-Type": "application/json",
                                         "X-HQ-CSRF": self.app.token},
                                body=b"[1,2]")
        self.assertEqual(status, 400)
        # 413 超大请求体
        status, _, _ = self.req("POST", "/api/config",
                                headers={"Origin": self.origin(),
                                         "Content-Type": "application/json",
                                         "X-HQ-CSRF": self.app.token},
                                body=b"x" * (dashboard.CONFIG_BODY_MAX + 1))
        self.assertEqual(status, 413)

    def test_unknown_paths_and_delivery(self):
        status, _, _ = self.req("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, body, _ = self.req("POST", "/api/nope",
                                   headers={"Origin": self.origin(),
                                            "Content-Type": "application/json",
                                            "X-HQ-CSRF": self.app.token},
                                   body=b"{}")
        self.assertEqual(status, 404)
        status, _, _ = self.req("GET", "/api/delivery?key=")
        self.assertEqual(status, 404)
        status, _, _ = self.req("GET", "/api/delivery?key=round:none:0")
        self.assertEqual(status, 404, "未知邮件记录 404")
        status, _, _ = self.req("GET", "/api/delivery?key=" + "k" * 201)
        self.assertEqual(status, 404)

    def test_config_save_flow_via_http(self):
        status, _, _ = self.post_json("/api/config", {"patch": {}})
        self.assertEqual(status, 400, "缺 revision")
        status, body, _ = self.req("GET", "/api/config")
        rc = json.loads(body.decode("utf-8"))
        status, body, _ = self.post_json(
            "/api/config", {"revision": rc["revision"],
                            "patch": {"mail": {"to_addr": "http@example.com"}}})
        self.assertEqual(status, 200)
        j = json.loads(body.decode("utf-8"))
        self.assertTrue(j["ok"])
        self.assertEqual(j["config"]["mail"]["to_addr"], "http@example.com")
        # 旧 revision 再发 → 409
        status, body, _ = self.post_json(
            "/api/config", {"revision": rc["revision"], "patch": {}})
        self.assertEqual(status, 409)

    def test_check_setup_endpoint(self):
        # B5 隔离纪律：read_setup 经 mail.hook_ctx 绑定宿主；单元测试用隔离
        # ctx，不读取真实 ~/.zcode/cli/config.json
        home = Path(os.environ["HQ_HOME"])
        ctx = {"python": sys.executable,
               "bootstrap": home / "bin" / "hq.py",
               "hq_config": self.app.cfg_path,
               "home": home, "backups": home / "backups"}
        with patch.object(mail, "hook_ctx", return_value=ctx):
            status, body, _ = self.post_json("/api/check-setup", {})
        self.assertEqual(status, 200)
        j = json.loads(body.decode("utf-8"))
        self.assertTrue(j["ok"])
        self.assertIn("hook", j["setup"])
        self.assertIn("task", j["setup"])
        with patch.object(mail, "hook_ctx", return_value=ctx):
            status, _, _ = self.post_json("/api/check-setup", {"x": 1})
        self.assertEqual(status, 400, "不接受业务参数")

    def test_test_mail_registry_and_busy(self):
        orig = dashboard.send_test
        calls = []

        def fake(cfg_path):
            calls.append(cfg_path)
            return {"status": "accepted", "detail": "fake",
                    "to": "v@example.com", "at": "2026-10-04T07:00:00+00:00"}

        dashboard.send_test = fake
        try:
            rid = "req-1"
            status, body, _ = self.post_json("/api/test-mail", {"request_id": rid})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "accepted")
            status, body, _ = self.post_json("/api/test-mail", {"request_id": rid})
            j = json.loads(body.decode("utf-8"))
            self.assertTrue(j.get("duplicate"), "重复 request_id 返回原结果")
            self.assertEqual(len(calls), 1, "重复 ID 不再外发")
            # 忙：占住 op_lock → 409 不排队
            self.assertTrue(self.app.op_lock.acquire(blocking=False))
            try:
                status, body, _ = self.post_json(
                    "/api/test-mail", {"request_id": "req-2"})
                self.assertEqual(status, 409)
                status, _, _ = self.post_json("/api/config", {"revision": "x",
                                                              "patch": {}})
                self.assertEqual(status, 409)
            finally:
                self.app.op_lock.release()
            # 配置不完整 → rejected；意外异常 → unknown
            def bad(cfg_path):
                raise mail.MailConfigError("缺 host")

            dashboard.send_test = bad
            status, body, _ = self.post_json("/api/test-mail",
                                             {"request_id": "req-3"})
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "rejected")

            def ugly(cfg_path):
                raise RuntimeError("boom")

            dashboard.send_test = ugly
            status, body, _ = self.post_json("/api/test-mail",
                                             {"request_id": "req-4"})
            self.assertEqual(json.loads(body.decode("utf-8"))["status"], "unknown")
            # 非法 request_id
            status, _, _ = self.post_json("/api/test-mail", {"request_id": ""})
            self.assertEqual(status, 400)
            status, _, _ = self.post_json("/api/test-mail",
                                          {"request_id": "x" * 65})
            self.assertEqual(status, 400)
        finally:
            dashboard.send_test = orig


class ReworkCounterexamples(_Home):
    """复审 F1–F8 反例的回归化（.local/evidence/local-dashboard-rework/
    probes/ 的探针为一次性原始证据；这里落成可重复用例）。"""

    # ---- F1 配置生命周期 ----

    def test_missing_config_lifecycle(self):
        cfg = self.home / "hq.toml"
        rc = dashboard.read_config(cfg)
        self.assertEqual(rc["source"], "missing")
        self.assertEqual(rc["revision"], "missing", "缺文件给显式 missing revision")
        with patch.object(mail, "inspect_setup", return_value={}), \
                patch.object(dashboard, "_task_status_json",
                             return_value={"installed": False, "source": "ok"}):
            setup = dashboard.read_setup(cfg)
        self.assertFalse(setup["mail_config"]["complete"], "缺配置绝不显示 complete")
        self.assertEqual(setup["mail_config"]["state"], "missing")
        # 首建：revision="missing" 可保存为暂停草稿
        pa = self.make_dirs("proj-a")[0]
        res = dashboard.save_config(cfg, {"projects": [{"path": str(pa)}],
                                          "mail": {"smtp_host": "s.example.com"}},
                                    "missing")
        self.assertTrue(res["ok"])
        self.assertFalse(dashboard.read_config(cfg)["mail"]["enabled"])

    def test_corrupt_config_not_overwritten(self):
        cfg = self.home / "hq.toml"
        cfg.write_bytes(b"private_unknown = [1,\n# broken TOML\n")
        before = cfg.read_bytes()
        rc = dashboard.read_config(cfg)
        self.assertEqual(rc["source"], "parse_error")
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {"enabled": False}}, rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertEqual(cfg.read_bytes(), before, "坏 TOML 原字节逐位保留")

    def test_relative_project_rejected(self):
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"projects": [{"path": "."}]}, rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertIn("绝对路径", cm.exception.detail)

    def test_address_type_rejected(self):
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {"from_addr": 7}}, rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid", "类型先拒绝，不抛 TypeError")

    def test_wrong_mail_table_snapshot(self):
        cfg = self.write_config()
        cfg.write_text("mail = 7\n", encoding="utf-8")
        snap = self.snap(cfg)
        self.assertEqual(snap["config"]["source"], "error")
        self.assertIn("[mail]", snap["config"]["detail"], "按组件诊断")
        self.assertIsNone(snap["config"]["enabled"])

    # ---- R1 配置保全与真实状态（复审返工2反例回归化） ----

    def test_invalid_project_entry_diagnosed_and_preserved(self):
        # R1-1：缺 path 的条目必须诊断，不得静默丢弃后生成可保存的残缺视图
        pa = self.make_dirs("proj-a")[0]
        cfg = self.write_config(
            projects=[pa],
            extra='\n[[projects]]\nprivate_note = "KEEP_UNKNOWN_ENTRY"\n')
        before = cfg.read_bytes()
        rc = dashboard.read_config(cfg)
        self.assertEqual(rc["source"], "schema_error")
        self.assertIn("第 2", rc["detail"])
        self.assertEqual([p["path"] for p in rc["projects"]],
                         [Path(pa).as_posix()], "合法条目照常展示")
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"projects": [{"path": p["path"]}
                                                     for p in rc["projects"]]},
                                  rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertIn("不可经仪表板替换", cm.exception.detail)
        self.assertEqual(cfg.read_bytes(), before, "拒绝前后原字节逐位一致")
        self.assertIn(b"KEEP_UNKNOWN_ENTRY", cfg.read_bytes(), "未知内容原样保留")
        self.assertFalse((self.home / ".local" / "backups" / "dashboard-config")
                         .exists(), "拒绝不产生备份")

    def test_non_bool_enabled_not_coerced(self):
        # R1-2：enabled="false" 不得布尔化成 true，也不得显示成确定暂停/可启用
        m = dict(smtp_host="smtp.example.com", smtp_port=465, security="ssl",
                 username="u@example.com", password_env="HQ_SMTP_PASSWORD_DASH_TEST",
                 from_addr="u@example.com", to_addr="v@example.com",
                 enabled="false")
        cfg = self.home / "hq.toml"
        cfg.write_text(dashboard._toml_encode({"mail": m}), encoding="utf-8")
        rc = dashboard.read_config(cfg)
        self.assertEqual(rc["source"], "schema_error")
        self.assertEqual(rc["mail"]["enabled"], "false", "编辑数据保留原值")
        self.assertTrue(any("enabled" in p
                            for p in rc["validation"]["draft_problems"]))
        snap = self.snap(cfg)
        self.assertEqual(snap["config"]["source"], "schema_error")
        self.assertIsNone(snap["config"]["enabled"])
        self.assertIn("非布尔", snap["config"]["detail"])
        with patch.object(mail, "inspect_setup", return_value={}), \
                patch.object(dashboard, "_task_status_json",
                             return_value={"installed": False, "source": "ok"}):
            setup = dashboard.read_setup(cfg)
        self.assertEqual(setup["mail_config"]["state"], "schema_error")
        self.assertFalse(setup["mail_config"]["complete"])
        # 保存侧同界：mail 组件损坏时 mail patch 拒绝，原字节不动
        before = cfg.read_bytes()
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {"enabled": True}}, rc["revision"])
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertEqual(cfg.read_bytes(), before)

    def test_first_create_rejects_enabled_true(self):
        # R1-3：首建只允许暂停草稿；服务端拒绝启用且不落盘
        cfg = self.home / "hq.toml"
        self.assertFalse(cfg.exists())
        full = dict(smtp_host="smtp.example.com", smtp_port=465, security="ssl",
                    username="u@example.com",
                    password_env="HQ_SMTP_PASSWORD_DASH_TEST",
                    from_addr="u@example.com", to_addr="v@example.com")
        with self.assertRaises(dashboard.ConfigError) as cm:
            dashboard.save_config(cfg, {"mail": {**full, "enabled": True}},
                                  "missing")
        self.assertEqual(cm.exception.kind, "invalid")
        self.assertIn("首建", cm.exception.detail)
        self.assertFalse(cfg.exists(), "首建拒绝启用不建文件")
        res = dashboard.save_config(cfg, {"mail": {**full, "enabled": False}},
                                    "missing")
        self.assertTrue(res["ok"], "合法暂停首建可落盘")
        self.assertFalse(dashboard.read_config(cfg)["mail"]["enabled"])

    def test_r1_http_cannot_bypass(self):
        # 页面与 HTTP 走同一 save_config 收口；HTTP 直发同形保存同样被拒
        import contextlib
        broken = self.write_config(
            extra='\n[[projects]]\nprivate_note = "KEEP_UNKNOWN_ENTRY"\n')
        before = broken.read_bytes()
        srv = dashboard._Server(("127.0.0.1", 0), dashboard._Handler)
        srv.app = dashboard._App(broken, secrets.token_urlsafe(16))
        srv.page = b"synthetic"
        port = srv.server_address[1]

        def post(obj):
            body = json.dumps(obj).encode()
            request = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                       f"Origin: http://127.0.0.1:{port}\r\n"
                       f"X-HQ-CSRF: {srv.app.token}\r\n"
                       f"Content-Type: application/json\r\n"
                       f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                       ).encode() + body
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(request)
                resp = bytearray()
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    resp += chunk
            return bytes(resp)

        rc = dashboard.read_config(broken)
        with contextlib.redirect_stderr(io.StringIO()):
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                resp = post({"revision": rc["revision"],
                             "patch": {"projects": [{"path": p["path"]}
                                                    for p in rc["projects"]]}})
                self.assertIn(b"400", resp.split(b"\r\n", 1)[0])
                self.assertEqual(broken.read_bytes(), before, "HTTP 拒绝同样零写入")
                # 首建（缺文件）enabled=true 同样拒绝且不建文件
                missing = self.home / "hq-first.toml"
                srv.app.cfg_path = missing
                resp = post({"revision": "missing",
                             "patch": {"mail": dict(
                                 smtp_host="smtp.example.com", smtp_port=465,
                                 security="ssl", username="u@example.com",
                                 password_env="HQ_SMTP_PASSWORD_DASH_TEST",
                                 from_addr="u@example.com",
                                 to_addr="v@example.com", enabled=True)}})
                self.assertIn(b"400", resp.split(b"\r\n", 1)[0])
                self.assertFalse(missing.exists())
            finally:
                srv.shutdown()
                srv.server_close()
        thread.join(timeout=5)

    # ---- R2 需注意与坏时间（复审返工2反例回归化） ----

    def test_attention_daily_filter_before_paging(self):
        # R2-4：15 期日报只有最旧一期 failed——需注意首页必须直接是它
        cfg = self.write_config()
        rows = []
        for i in range(1, 16):
            rows.append(dict(key=f"daily:202609{i:02d}", kind="daily",
                             subject="s", body="b",
                             state="failed" if i == 1 else "sent",
                             created=mail.utc_iso(bj(2026, 10, 5, 12, 0))))
        self.seed_db(deliveries=rows)
        s = self.snap(cfg, attention=True)
        self.assertEqual(s["counts"]["attention_daily"], 1)
        self.assertEqual(s["counts"]["daily_total"], 15, "全库总数保留")
        self.assertEqual(s["counts"]["daily_total_filtered"], 1)
        self.assertEqual([d["key"] for d in s["recent_dailies"]],
                         ["daily:20260901"], "旧失败日报直接进首屏")
        # 过滤后页数与下一页：limit=10 → 共 1 期，next 禁用语义由页内长度保证
        s2 = self.snap(cfg, attention=True, daily_limit=10, daily_offset=1)
        self.assertEqual(s2["recent_dailies"], [], "过滤后第 2 页为空")
        # 关闭需注意回到全库口径
        s3 = self.snap(cfg, attention=False)
        self.assertEqual(s3["counts"]["daily_total_filtered"], 15)
        self.assertEqual(len(s3["recent_dailies"]), 10)

    def test_bad_deadline_visible_and_counted(self):
        # R2-5：deadline='not-a-time' 不得显示成正常待处理；坏值保留原值与诊断
        cfg = self.write_config()
        at = mail.utc_iso(bj(2026, 10, 4, 13, 0))
        self.seed_db(
            events=[dict(session="bad-dl", turn="1", project=str(self.home),
                         observed=at, confirmed=at, outcome="completed")],
            deliveries=[dict(key="round:bad-dl:1", kind="round", subject="s",
                             body="b", state="pending", created=at,
                             deadline="not-a-time")])
        s = self.snap(cfg)
        row = s["rows"][0]
        self.assertEqual(row["label"], "数据异常（发送截止无法解析）",
                         "坏截止不显示为待处理")
        self.assertTrue(row["attention"], "坏截止计入需注意")
        self.assertEqual(s["counts"]["attention"], 1)
        self.assertEqual(row["deadline_raw"], "not-a-time", "原值保留")
        self.assertIn("deadline_utc", row["data_error"] or "")
        self.assertIsNone(row["deadline"], "不给出伪造的解析结果")
        # 正常行不受影响：再种一条夜间完成（10-04 21:00，参考截止 10-05 10:00，
        # 快照时刻 10-05 21:00 已过→待核对），两行都可见可读
        at2 = mail.utc_iso(bj(2026, 10, 4, 21, 0))
        self.seed_db(
            events=[dict(session="good", turn="1", project=str(self.home),
                         observed=at2, confirmed=at2, outcome="completed")])
        s2 = self.snap(cfg, now=bj(2026, 10, 5, 21, 0))
        labels = {r["session"]: r["label"] for r in s2["rows"]}
        self.assertEqual(labels["good"], "待核对", "夜间完成未过截止仍是既有口径")
        self.assertEqual(labels["bad-dl"], "数据异常（发送截止无法解析）")

    # ---- R3 HTTP 正文 framing 与固定路由日志（复审返工2反例回归化） ----

    def _raw_roundtrip(self, port, payload):
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(payload)
            resp = bytearray()
            while True:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                resp += chunk
            return bytes(resp)

    def _start_logged_server(self, cfg):
        """返回 (srv, log, thread)。复审 B4：调用方必须在
        `with contextlib.redirect_stderr(log):` 内完成 thread.start()、全部
        流量、shutdown 和 thread.join——处理线程在运行与退出时都会写
        sys.stderr（动态查找），只在 start 周围重定向会让捕获空跑，
        随后的"哨兵未出现"断言全部失去证明力。"""
        srv = dashboard._Server(("127.0.0.1", 0), dashboard._Handler)
        srv.app = dashboard._App(cfg, secrets.token_urlsafe(16))
        srv.page = b"synthetic"
        log = io.StringIO()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        return srv, log, thread

    def _run_logged(self, log, thread, srv, *payloads):
        """在有效捕获窗口内完成流量与关闭（含 join），返回响应列表。"""
        import contextlib
        out = []
        with contextlib.redirect_stderr(log):
            thread.start()
            try:
                for payload in payloads:
                    out.append(self._raw_roundtrip(srv.server_address[1], payload))
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
        return out

    def test_r3_transfer_encoding_rejected(self):
        # R3-6：TE+CL 请求不得当普通 JSON 接受并改配置
        cfg = self.write_config()
        before = cfg.read_bytes()
        rc = dashboard.read_config(cfg)
        srv, log, thread = self._start_logged_server(cfg)
        port = srv.server_address[1]
        body = json.dumps({"revision": rc["revision"],
                           "patch": {"mail": {"smtp_host": "changed.example"}}}
                          ).encode()
        request = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Origin: http://127.0.0.1:{port}\r\n"
                   f"X-HQ-CSRF: {srv.app.token}\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Transfer-Encoding: chunked\r\n"
                   f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                   ).encode() + body
        [resp] = self._run_logged(log, thread, srv, request)
        self.assertIn(b"400", resp.split(b"\r\n", 1)[0])
        self.assertEqual(cfg.read_bytes(), before, "framing 拒绝零写入")
        self.assertEqual(dashboard.read_config(cfg)["mail"]["smtp_host"],
                         "smtp.example.com")
        # 正向控制先行：拒绝响应也应记一行固定路由日志，再断言无正文泄漏
        assert_dashboard_log(self, log.getvalue(), expect_lines=[("POST", "/api/config")])
        self.assertNotIn("changed.example", log.getvalue())

    def test_r3_get_body_not_parsed_or_logged(self):
        # R3-7：GET 带正文——拒绝关连接，正文不得成为第二请求，哨兵不得进日志
        cfg = self.write_config()
        srv, log, thread = self._start_logged_server(cfg)
        port = srv.server_address[1]
        sentinel = "FAKE_BODY_PATH_SECRET_SUITE"
        body = (f"GET /{sentinel} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Connection: close\r\n\r\n").encode()
        request = (f"GET /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        [resp] = self._run_logged(log, thread, srv, request)
        self.assertIn(b"400", resp.split(b"\r\n", 1)[0])
        self.assertEqual(resp.count(b"HTTP/1.1"), 1, "正文不得被解析成第二请求")
        assert_dashboard_log(self, log.getvalue(), expect_lines=[("GET", "/api/config")])
        self.assertNotIn(sentinel, log.getvalue(), "正文哨兵不得进日志")
        self.assertNotIn(sentinel.encode(), resp)

    def test_r3_ambiguous_and_illegal_length_rejected(self):
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        srv, log, thread = self._start_logged_server(cfg)
        port = srv.server_address[1]
        body = json.dumps({"revision": rc["revision"],
                           "patch": {"mail": {"smtp_host": "changed.example"}}}
                          ).encode()
        base = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                f"X-HQ-CSRF: {srv.app.token}\r\n"
                f"Content-Type: application/json\r\n")
        cases = {
            "dup-length": (base + f"Content-Length: {len(body)}\r\n"
                           f"Content-Length: {len(body)}\r\n"
                           f"Connection: close\r\n\r\n").encode() + body,
            "dup-conflict": (base + f"Content-Length: {len(body)}\r\n"
                             "Content-Length: 3\r\n"
                             f"Connection: close\r\n\r\n").encode() + body,
            "illegal": (base + "Content-Length: abc\r\n"
                        f"Connection: close\r\n\r\n").encode(),
        }
        resps = self._run_logged(log, thread, srv, *cases.values())
        for name, resp in zip(cases, resps):
            with self.subTest(case=name):
                self.assertIn(b"400", resp.split(b"\r\n", 1)[0],
                              "歧义/非法长度必须拒绝")
        self.assertEqual(dashboard.read_config(cfg)["mail"]["smtp_host"],
                         "smtp.example.com", "拒绝路径未改配置")
        # 正向控制：三条拒绝各自记固定路由行（非法 CL 的路径仍可安全解析）
        parsed = assert_dashboard_log(self, log.getvalue(),
                                      expect_lines=[("POST", "/api/config")])
        self.assertEqual(len(parsed), 3)

    def test_r3_normal_traffic_and_fixed_route_log(self):
        # 正常 GET/POST 不受影响；日志只出现固定路由集合或常量占位
        cfg = self.write_config()
        rc = dashboard.read_config(cfg)
        srv, log, thread = self._start_logged_server(cfg)
        port = srv.server_address[1]
        body = json.dumps({"revision": rc["revision"],
                           "patch": {"mail": {"smtp_host": "normal.example"}}}
                          ).encode()
        post = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                f"X-HQ-CSRF: {srv.app.token}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                ).encode() + body
        get_unknown = (f"GET /definitely/not/a/route?q=1 HTTP/1.1\r\n"
                       f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n").encode()
        resp_page, resp_post, resp_unknown = self._run_logged(
            log, thread, srv,
            (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
             f"Connection: close\r\n\r\n").encode(),
            post, get_unknown)
        self.assertIn(b"200", resp_page.split(b"\r\n", 1)[0])
        self.assertIn(b"200", resp_post.split(b"\r\n", 1)[0])
        self.assertEqual(dashboard.read_config(cfg)["mail"]["smtp_host"],
                         "normal.example", "合法 POST 照常生效")
        self.assertIn(b"404", resp_unknown.split(b"\r\n", 1)[0])
        # 正向控制：先要求三条预期行都被捕获，再做白名单与占位断言（B4）
        parsed = assert_dashboard_log(
            self, log.getvalue(),
            expect_lines=[("GET", "/"), ("POST", "/api/config"),
                          ("GET", "<unparsed>")])
        for method, path in parsed:
            self.assertIn(method, ("GET", "POST", "HEAD", "<invalid>"))
            self.assertIn(path, set(dashboard._KNOWN_ROUTES) | {"<unparsed>"})
        self.assertNotIn("/definitely/not/a/route", log.getvalue(),
                         "未知路由只记占位")

    # ---- F3 事实口径 ----

    def test_expired_notified_without_delivery(self):
        cfg = self.write_config()
        now = bj(2026, 10, 5, 21, 0)
        old = mail.utc_iso(bj(2026, 10, 2, 21, 0))
        self.seed_db(events=[dict(session="missing", turn="1",
                                  project=str(self.home), observed=old,
                                  confirmed=old, outcome="completed",
                                  notified=1)])
        s = self.snap(cfg, now=now)
        self.assertEqual(s["rows"][0]["label"], "待核对")
        self.assertEqual(s["rows"][0]["attention"], True)
        self.assertEqual(s["counts"]["attention"], 1)

    def test_failed_daily_in_attention(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 4, 19, 0))
        self.seed_db(deliveries=[dict(key="daily:20261002", kind="daily",
                                      subject="日报", body="b", state="failed",
                                      created=ok, error="boom")])
        s = self.snap(cfg, now=bj(2026, 10, 5, 21, 0))
        self.assertEqual(s["counts"]["attention_daily"], 1)
        self.assertEqual(s["counts"]["attention"], 1, "全库注意数含失败日报")
        self.assertEqual(s["counts"]["daily_total"], 1)

    def test_attention_view_covers_all_history(self):
        cfg = self.write_config()
        old = mail.utc_iso(bj(2026, 10, 2, 21, 0))
        self.seed_db(
            events=[dict(session="uncertain", turn="1", project=str(self.home),
                         observed=old, confirmed=old, outcome="completed",
                         notified=1)],
            deliveries=[dict(key=mail.round_key("uncertain", "1"), kind="round",
                             subject="t", body="b", state="uncertain",
                             created=old)])
        s = self.snap(cfg, now=bj(2026, 10, 5, 21, 0), period="24h",
                      attention=True)
        self.assertEqual(s["total"], 1, "需注意自动覆盖全部历史")
        self.assertEqual(s["query"]["period"], "all")

    def test_project_names_by_distinct_roots(self):
        pa, pb = self.make_dirs("projdup", "solo")
        other_dup = str(self.home / "nested" / "projdup")
        Path(other_dup).mkdir(parents=True, exist_ok=True)
        ok = mail.utc_iso(bj(2026, 10, 3, 20, 0))
        self.seed_db([
            dict(session="p1", turn="1", project=pa, observed=ok,
                 confirmed=ok, outcome="completed"),
            dict(session="p2", turn="1", project=pa, observed=ok,
                 confirmed=ok, outcome="completed"),
            dict(session="p3", turn="1", project=other_dup, observed=ok,
                 confirmed=ok, outcome="completed"),
            dict(session="p4", turn="1", project=pb, observed=ok,
                 confirmed=ok, outcome="completed"),
            dict(session="p5", turn="1", project=pb, observed=ok,
                 confirmed=ok, outcome="completed"),
        ])
        s = self.snap(cfg_file(self.home), now=bj(2026, 10, 3, 21, 0))
        names = {r["session"]: r["project_name"] for r in s["rows"]}
        self.assertEqual(names["p1"], names["p2"],
                         "同根项目按规范根目录计数，不因事件条数变碰撞名")
        self.assertEqual(names["p1"], f"{Path(self.home).name}/projdup")
        self.assertEqual(names["p3"], "nested/projdup", "不同根同名 → 尾部区分")
        self.assertEqual(names["p4"], "solo", "solo 同根两条仍是纯目录名")
        self.assertEqual(names["p5"], "solo")
        opts = {o["path"]: o["name"] for o in s["project_options"]}
        self.assertIn(str(Path(other_dup).resolve()), opts, "历史项目进下拉")

    def test_malformed_time_row_classified(self):
        cfg = self.write_config()
        self.seed_db(events=[dict(session="bad-time", turn="1",
                                  project=str(self.home),
                                  observed=mail.utc_iso(bj(2026, 10, 5, 20, 0)),
                                  outcome="pending")])
        db = mail.connect()
        db.execute("UPDATE events SET observed_at_utc='not-a-time'")
        db.commit()
        db.close()
        s = self.snap(cfg, now=bj(2026, 10, 5, 21, 0))
        self.assertEqual(s["source"]["state"], "ok", "坏时间是行级数据问题")
        self.assertEqual(s["rows"][0]["label"], "数据异常（时间无法解析）")
        self.assertIsNotNone(s["rows"][0]["data_error"])
        self.assertIsNone(s["rows"][0]["when_beijing"])

    def test_snapshot_beijing_time_and_derived_deadline(self):
        cfg = self.write_config()
        confirmed = bj(2026, 10, 4, 15, 36)          # 北京 15:36（日间）
        utc = mail.utc_iso(confirmed)
        self.seed_db(events=[dict(session="tz", turn="1", project=str(self.home),
                                  observed=utc, confirmed=utc,
                                  outcome="completed")])
        s = self.snap(cfg, now=bj(2026, 10, 5, 21, 0))
        row = s["rows"][0]
        self.assertEqual(row["when_beijing"], "2026-10-04 15:36")
        self.assertIsNone(row["deadline"], "日间完成不推导轮次截止（仅日报）")
        # 夜间窗口完成的未投递轮次：按完成时间推导 deadline（F3）
        night = mail.utc_iso(bj(2026, 10, 4, 21, 0))
        self.seed_db(events=[dict(session="tz2", turn="1", project=str(self.home),
                                  observed=night, confirmed=night,
                                  outcome="completed")])
        s2 = self.snap(cfg, now=bj(2026, 10, 4, 22, 0))
        row2 = [r for r in s2["rows"] if r["session"] == "tz2"][0]
        self.assertEqual(row2["deadline_beijing"], "2026-10-05 10:00")

    # ---- F2 只读边界 ----

    def test_wal_db_refused_before_open(self):
        cfg = self.write_config()
        self.seed_db(events=[dict(session="wal", turn="1", project=str(self.home),
                                  observed=mail.utc_iso(bj(2026, 10, 3, 20, 0)),
                                  outcome="pending")])
        dbpath = mail.db_path()
        writer = sqlite3.connect(dbpath)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.close()
        dbdir = dbpath.parent
        names = lambda: sorted(p.name for p in dbdir.iterdir())
        before = names()
        s = self.snap(cfg)
        self.assertEqual(s["source"]["state"], "error", "WAL 读取前拒绝")
        self.assertIn("WAL", s["source"]["detail"])
        self.assertEqual(names(), before, "拒绝路径不创建 sidecar")
        with self.assertRaises(sqlite3.OperationalError):
            dashboard.read_delivery("round:x:1"), "库不可读不得伪装 404"

    def test_page_query_bounded(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 3, 20, 0))
        self.seed_db([dict(session=f"s{i}", turn="1", project=str(self.home),
                           observed=ok, confirmed=ok, outcome="completed")
                      for i in range(5000)])
        fetched = []
        orig_connect = dashboard._connect_ro

        class CursorProxy:
            def __init__(self, cur, sql):
                self._cur, self._sql = cur, sql
            def fetchall(self):
                rows = self._cur.fetchall()
                fetched.append(len(rows))
                return rows
            def __getattr__(self, name):
                return getattr(self._cur, name)

        class DbProxy:
            def __init__(self, db):
                self._db = db
            def execute(self, sql, *a, **k):
                return CursorProxy(self._db.execute(sql, *a, **k), sql)
            def __getattr__(self, name):
                return getattr(self._db, name)

        def proxy_connect():
            db, err = orig_connect()
            return (DbProxy(db) if db is not None else None), err

        dashboard._connect_ro = proxy_connect
        try:
            s = self.snap(cfg, now=bj(2026, 10, 3, 21, 0), limit=1)
        finally:
            dashboard._connect_ro = orig_connect
        self.assertEqual(s["total"], 5000)
        self.assertEqual(len(s["rows"]), 1)
        self.assertTrue(fetched and max(fetched) <= 200,
                        f"应用侧只取有界行，实际 fetchall 行数 {fetched}")

    # ---- F6 日报有界分页 ----

    def test_daily_pagination(self):
        cfg = self.write_config()
        ok = mail.utc_iso(bj(2026, 10, 3, 19, 0))
        self.seed_db(deliveries=[
            dict(key=f"daily:202609{i:02d}", kind="daily", subject=f"日报{i}",
                 body="b", state="sent", created=ok, sent=ok)
            for i in range(1, 16)])
        s1 = self.snap(cfg, now=bj(2026, 10, 3, 21, 0))
        self.assertEqual(s1["counts"]["daily_total"], 15)
        self.assertEqual(len(s1["recent_dailies"]), 10)
        self.assertEqual(s1["query"]["daily_limit"], 10)
        s2 = self.snap(cfg, now=bj(2026, 10, 3, 21, 0), daily_offset=10)
        self.assertEqual(len(s2["recent_dailies"]), 5)
        self.assertEqual(s2["recent_dailies"][0]["key"], "daily:20260905")

    # ---- F5 拒绝请求正文不进日志（原始 socket 哨兵） ----

    def test_http_rejected_body_not_in_log(self):
        import contextlib
        srv = dashboard._Server(("127.0.0.1", 0), dashboard._Handler)
        srv.app = dashboard._App(self.write_config(), secrets.token_urlsafe(16))
        srv.page = dashboard._load_page().replace(
            b"@@TOKEN@@", srv.app.token.encode("ascii"))
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        log = io.StringIO()
        port = srv.server_address[1]
        secret = "FAKE_SECRET_SENTINEL_SUITE"
        body = json.dumps({"untrusted_secret": secret}, separators=(",", ":")).encode()
        request = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Origin: http://cross-site.example\r\n"
                   f"Content-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body
        request += (f"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Connection: close\r\n\r\n").encode()
        with contextlib.redirect_stderr(log):
            thread.start()
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                sock.sendall(request)
                response = bytearray()
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    response += chunk
                sock.close()
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)      # 捕获须覆盖处理线程运行与退出（B4）
        self.assertIn(b"403", response, "跨站 POST 先被拒")
        # 正向控制先行（复审 B4）：403 拒绝应记固定路由行，再断言正文不泄漏
        assert_dashboard_log(self, log.getvalue(),
                             expect_lines=[("POST", "/api/config")])
        self.assertNotIn(secret, log.getvalue(), "正文哨兵不得进日志")
        self.assertNotIn("GET /api/snapshot", log.getvalue(),
                         "管道化第二请求不得被解析")


def cfg_file(home: Path) -> Path:
    return home / "hq.toml"


class BoundaryInputHTTP(_Home):
    """边界方案 B1 回归（2026-10-08）：请求错误必须在不可抛异常的解析边界
    定形——畸形 target 结构化 400、超长数字 CL 分流、慢滴正文受总预算约束、
    keep-alive 空闲不受正文预算影响。全部原始 socket，合成数据。"""

    def _raw(self, port, payload, timeout=5):
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(payload)
            resp = bytearray()
            while True:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                resp += chunk
            return bytes(resp)

    def _raw_halfclose(self, port, payload, timeout=10):
        """发送后立即半关写侧：让拒绝路径的排空立刻读到 EOF，用例不必等
        总预算走完（预算耗尽行为由 post 探针与慢滴用例覆盖）。"""
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            resp = bytearray()
            while True:
                try:
                    chunk = sock.recv(65536)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                resp += chunk
            return bytes(resp)

    def _start_logged(self, cfg):
        srv = dashboard._Server(("127.0.0.1", 0), dashboard._Handler)
        srv.app = dashboard._App(cfg, secrets.token_urlsafe(16))
        srv.page = b"synthetic"
        log = io.StringIO()
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        return srv, log, thread

    def _logged_traffic(self, srv, log, thread, fn):
        """捕获窗口必须覆盖处理线程运行与退出（复审 B4）。fn(port)→任意。"""
        import contextlib
        with contextlib.redirect_stderr(log):
            thread.start()
            try:
                out = fn(srv.server_address[1])
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
        return out

    def test_malformed_target_structured_400_without_log_leak(self):
        # absolute-form/畸形 authority：结构化 400 并关连接；urlparse 类解析
        # 不得再进入日志/路由路径，哨兵与 traceback 都不得出现
        cfg = self.write_config()
        srv, log, thread = self._start_logged(cfg)
        port = srv.server_address[1]
        sentinel = "HQ_BOUNDARY_CANARY_SUITE"
        bad = (f"GET http://[{sentinel}]/ HTTP/1.1\r\n"
               f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n").encode()
        good = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Connection: close\r\n\r\n").encode()
        resp_bad, resp_good = self._logged_traffic(
            srv, log, thread, lambda p: (self._raw(p, bad), self._raw(p, good)))
        self.assertIn(b"400", resp_bad.split(b"\r\n", 1)[0],
                      "畸形目标必须结构化拒绝，不得空响应")
        self.assertIn(b'"kind"', resp_bad, "拒绝体是结构化 JSON 错误")
        self.assertIn(b"200", resp_good.split(b"\r\n", 1)[0], "正常流量不受影响")
        parsed = assert_dashboard_log(
            self, log.getvalue(),
            expect_lines=[("GET", "<unparsed>"), ("GET", "/")])
        self.assertNotIn(sentinel, log.getvalue(), "哨兵不得进日志")
        self.assertNotIn("Traceback", log.getvalue(), "不得刷未处理 traceback")
        self.assertEqual(sum(1 for m, _ in parsed if m == "GET"), 2)

    def test_oversized_numeric_content_length_bounded(self):
        # 5000 位数字 CL：GET→400、POST→413（超正文上限分流），全程不得让
        # int 数字转换上限异常穿出（无空响应、无 traceback）
        cfg = self.write_config()
        srv, log, thread = self._start_logged(cfg)
        port = srv.server_address[1]
        huge = "9" * 5000
        get_req = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Content-Length: {huge}\r\nConnection: close\r\n\r\n").encode()
        post_req = (f"POST /api/config HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Origin: http://127.0.0.1:{port}\r\n"
                    f"X-HQ-CSRF: {srv.app.token}\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {huge}\r\nConnection: close\r\n\r\n").encode()
        resp_get, resp_post = self._logged_traffic(
            srv, log, thread,
            lambda p: (self._raw_halfclose(p, get_req),
                       self._raw_halfclose(p, post_req)))
        self.assertIn(b"400", resp_get.split(b"\r\n", 1)[0])
        self.assertIn(b"413", resp_post.split(b"\r\n", 1)[0],
                      "超过正文上限的巨大 CL 按 413 分流")
        assert_dashboard_log(self, log.getvalue(),
                             expect_lines=[("GET", "/"), ("POST", "/api/config")])
        self.assertNotIn("Traceback", log.getvalue())
        self.assertEqual(dashboard.read_config(cfg)["mail"]["smtp_host"],
                         "smtp.example.com", "拒绝路径未改配置")

    def test_slow_drip_body_fails_total_budget(self):
        # 每 2 秒补 1 字节、约 6 秒补齐的 8 字节 JSON：正文阶段 5 秒总预算
        # 耗尽即结束连接，不得进入业务分发（旧实现每次读取刷新 5s 超时，
        # 6 秒后仍返回 404）
        import time
        cfg = self.write_config()
        before = cfg.read_bytes()
        srv, log, thread = self._start_logged(cfg)
        port = srv.server_address[1]
        body = b'      {}'
        drip = (f"POST /not-a-route HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Origin: http://127.0.0.1:{port}\r\n"
                f"X-HQ-CSRF: {srv.app.token}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        control = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Connection: close\r\n\r\n").encode()

        def traffic(p):
            # 只滴 3 字节（t=0/2/4），然后等结局：总预算 5 秒的实现在 ~5s
            # 关闭连接（recv 得到 EOF/RST）；若实现是"每次读取刷新 5 秒超时"，
            # 最后一次读取在 t=4 起算会等到 ~9s，本用例以关闭时刻判别。
            with socket.create_connection(("127.0.0.1", p), timeout=20) as sock:
                start = time.monotonic()
                sock.sendall(drip + body[:1])
                for _ in range(2):
                    time.sleep(2)
                    sock.sendall(body[1:2])
                sock.settimeout(10)
                try:
                    ended = sock.recv(65536) == b""
                except (ConnectionResetError, OSError):
                    ended = True                     # RST 同样视为服务端已关
                return ended, time.monotonic() - start

        import contextlib
        with contextlib.redirect_stderr(log):
            thread.start()
            try:
                ended, elapsed = traffic(port)
                resp_ctl = self._raw(port, control)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
        self.assertTrue(ended, "服务端应结束连接（不挂到客户端超时）")
        self.assertLess(elapsed, 5.9,
                        f"正文总预算 5 秒应先于滴流完成截断（实际 {elapsed:.2f}s）")
        self.assertIn(b"200", resp_ctl.split(b"\r\n", 1)[0], "后续正常流量不受影响")
        assert_dashboard_log(self, log.getvalue(), expect_lines=[("GET", "/")])
        self.assertNotIn("Traceback", log.getvalue())
        self.assertEqual(cfg.read_bytes(), before, "预算截断零写入")

    def test_keepalive_idle_reuse_not_bounded_by_body_budget(self):
        # 复用连接空闲 6 秒（> 正文预算 5 秒）后再发请求仍 200：总预算只施
        # 加于正文阶段，10s 轮询的 keep-alive 不被服务端掐断
        import time
        cfg = self.write_config()
        srv, log, thread = self._start_logged(cfg)
        port = srv.server_address[1]
        r1 = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n").encode()
        r2 = (f"GET /api/snapshot HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
              f"Connection: close\r\n\r\n").encode()

        def recv_one(sock):
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                part = sock.recv(65536)
                if not part:
                    break
                buf += part
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            n = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    n = int(line.split(b":", 1)[1].strip())
            while len(rest) < n:
                part = sock.recv(65536)
                if not part:
                    break
                rest += part
            return head + b"\r\n\r\n" + rest

        import contextlib
        with contextlib.redirect_stderr(log):
            thread.start()
            try:
                with socket.create_connection(("127.0.0.1", port),
                                              timeout=15) as sock:
                    sock.sendall(r1)
                    first = recv_one(sock)
                    time.sleep(6)
                    sock.sendall(r2)
                    second = recv_one(sock)
            finally:
                srv.shutdown()
                srv.server_close()
                thread.join(timeout=5)
        self.assertIn(b"200", first.split(b"\r\n", 1)[0])
        self.assertIn(b"200", second.split(b"\r\n", 1)[0],
                      "空闲 6 秒后复用连接仍可用（预算不外溢到空闲等待）")
        assert_dashboard_log(self, log.getvalue(),
                             expect_lines=[("GET", "/"), ("GET", "/api/snapshot")])


class HookContract(_Home):
    """B3：read_setup 对真实 mail.inspect_setup 两种错误渠道的归一化
    （mail.py 冻结不改）。宿主绑定经 mail.hook_ctx 补丁落在隔离根，
    绝不读取真实宿主（B5）。"""

    def _read_setup_isolated(self, cfg):
        ctx = {"python": sys.executable,
               "bootstrap": self.home / "bin" / "hq.py",
               "hq_config": cfg,
               "home": self.home,
               "backups": self.home / "backups"}
        with patch.object(mail, "hook_ctx", return_value=ctx), \
                patch.object(dashboard, "_task_status_json",
                             return_value={"installed": False, "source": "ok"}):
            return dashboard.read_setup(cfg)

    def test_parse_error_normalized_to_component_error(self):
        # 真实 inspect_setup 读到合成坏 JSON：parse_error 非空 → 组件错误，
        # 原因进 detail；mail_present/hooks_enabled 未知（None），不得把实际
        # 读取失败显示成"未安装"
        target = self.home / ".zcode" / "cli" / "config.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"{bad\n")
        before = target.read_bytes()
        cfg = self.write_config()
        setup = self._read_setup_isolated(cfg)
        h = setup["hook"]
        self.assertEqual(h["source"], "error")
        self.assertTrue(h["detail"], "parse_error 必须作为原因呈现")
        self.assertEqual(h["detail"], h["parse_error"])
        self.assertIsNone(h["mail_present"])
        self.assertIsNone(h["hooks_enabled"])
        self.assertEqual(h["target"], str(target), "target 等事实保留")
        self.assertEqual(target.read_bytes(), before, "只读检查不改宿主文件")

    def test_missing_hook_file_still_shows_not_installed(self):
        # 合法缺文件：parse_error 为空 → 照常显示未安装（source=ok）
        cfg = self.write_config()
        setup = self._read_setup_isolated(cfg)
        h = setup["hook"]
        self.assertEqual(h["source"], "ok")
        self.assertFalse(h["mail_present"])
        self.assertIsNone(h["parse_error"])
        self.assertEqual(h["target"],
                         str(self.home / ".zcode" / "cli" / "config.json"))

    def test_exception_path_same_error_shape(self):
        # 抛异常渠道沿用同一形状：source=error + detail + 未知置 None
        cfg = self.write_config()
        with patch.object(mail, "inspect_setup",
                          side_effect=RuntimeError("synthetic boom")), \
                patch.object(dashboard, "_task_status_json",
                             return_value={"installed": False, "source": "ok"}):
            setup = dashboard.read_setup(cfg)
        h = setup["hook"]
        self.assertEqual(h["source"], "error")
        self.assertIn("RuntimeError", h["detail"])
        self.assertIsNone(h["mail_present"])
        self.assertIsNone(h["hooks_enabled"])


class CliEntry(_Home):
    def test_dashboard_subcommand_lazy(self):
        from hqlib import cli
        calls = {}
        orig = dashboard.serve

        def fake(*, port, open_browser):
            calls["args"] = (port, open_browser)
            return 7

        dashboard.serve = fake
        try:
            rc = cli.main(["dashboard", "--port", "9001", "--no-open"])
            self.assertEqual(rc, 7)
            self.assertEqual(calls["args"], (9001, False))
            rc = cli.main(["dashboard"])
            self.assertEqual(calls["args"], (8765, True), "默认端口 8765 并开浏览器")
        finally:
            dashboard.serve = orig

    def test_dashboard_rejects_invalid_port(self):
        # 复审 F8：负数/超范围/非整数端口 → argparse 参数错误（退出码 2），
        # 不触发未处理 OverflowError，也不调用 serve
        from hqlib import cli
        calls = []
        orig = dashboard.serve
        dashboard.serve = lambda *, port, open_browser: calls.append(port) or 0
        try:
            for bad in ("-1", "0", "65536", "99999", "abc"):
                with self.subTest(port=bad):
                    buf, err = io.StringIO(), io.StringIO()
                    old_out, old_err = sys.stdout, sys.stderr
                    sys.stdout, sys.stderr = buf, err
                    try:
                        with self.assertRaises(SystemExit) as cm:
                            cli.main(["dashboard", "--port", bad])
                    finally:
                        sys.stdout, sys.stderr = old_out, old_err
                    self.assertEqual(cm.exception.code, 2)
                    self.assertIn("端口", err.getvalue())
            self.assertEqual(calls, [], "无效端口不得进入 serve")
        finally:
            dashboard.serve = orig

    def test_serve_port_conflict_message(self):
        # 占住端口 → 明确报错退出 1，不随机换端口
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        buf = io.StringIO()
        err = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, err
        try:
            rc = dashboard.serve(port=port, open_browser=False)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
        s.close()
        self.assertEqual(rc, 1)
        self.assertIn(str(port), err.getvalue())
        self.assertIn("--port", err.getvalue())


if __name__ == "__main__":
    unittest.main()
