"""mail 模块契约测试：本轮结束提醒 + 19:00 日报（docs/plans/2026-10-02-mail-degraded.md）。

覆盖（方案「验收覆盖」清单）：19:00 含/10:00 不含/跨日、Stop 早于真正结束不发、
同 turn 重复不发、白天轮次无通知、非登记项目不通知、失败/中断只进日报、
未确认不冒充完成、日报窗口/空日/离线最近补报、并发 tick 只有一个领取、
发送结果不明不自动重发、QUIT 失败不撤销已接受、坏 payload fail-open、
无关配置与项目文件不变。

隔离：临时项目/hq.toml/HQ_HOME/日志目录，注入时钟与 fake SMTP/发送器；
宿主日志行使用 D1 真实样本的行格式。真实 SMTP、真实 Hook 触发、真实计划任务
不在 unittest 范围（现场项 NOT RUN）。
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hqlib import common, mail  # noqa: E402
from hqlib.common import toml_str  # noqa: E402

TZ8 = timezone(timedelta(hours=8))
# D1 固定目标的真实标识与宿主终态行格式（.local/evidence/desktop-single-session/）
SESSION = "sess_f62cd49d-a6d5-4ae3-af29-1015e8389dec"
TURN = "turn_c6c13382-1b35-433f-b60e-43be986cde7a"

STATUS = """---
hq: 1
project: proj
theme: 测试
value: 自用
state: active
updated: 2026-10-02 12:00
---
# proj · 状态

> 一句话现状，用于日报。

## ❓ 待 Henry 判断
- 无

## ⛔ 阻塞
- 依赖外部 API 的额度还没批

## ▶ 下一步
- 补齐回归测试

## ✅ 机器验收

<!-- hq:verify:start -->
- [ ] 机器区不算
<!-- hq:verify:end -->
"""


def bj(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ8)


class Base(unittest.TestCase):
    """隔离四件套：临时项目、临时 hq.toml（[mail] 启用）、临时 HQ_HOME、临时日志目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.tmp.name)
        self.root = self.base / "proj"
        self.root.mkdir()
        (self.root / "STATUS.md").write_text(STATUS, encoding="utf-8")
        self.cfgpath = self.base / "hq.toml"
        self.write_cfg(enabled=True)
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self._old_home = os.environ.get("HQ_HOME")
        self._old_cfg = common.CONFIG_PATH
        os.environ["HQ_HOME"] = str(self.base)
        common.CONFIG_PATH = self.cfgpath
        self.sent = []          # fake sender 的调用记录
        self.sender = self._fake_sender()

    def tearDown(self):
        common.CONFIG_PATH = self._old_cfg
        if self._old_home is None:
            os.environ.pop("HQ_HOME", None)
        else:
            os.environ["HQ_HOME"] = self._old_home
        self.tmp.cleanup()

    def write_cfg(self, enabled: bool, extra_projects: list = None):
        lines = ["wip_limit = 3", "[[projects]]", f"path = {toml_str(str(self.root))}"]
        for p in extra_projects or []:
            lines += ["[[projects]]", f"path = {toml_str(str(p))}"]
        lines += ["[mail]", f"enabled = {str(enabled).lower()}",
                  'smtp_host = "smtp.test"', "smtp_port = 465", 'security = "ssl"',
                  'username = "u"', 'password_env = "HQ_TEST_SMTP_PASS"',
                  'from_addr = "hq@test"', 'to_addr = "me@test"']
        self.cfgpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _fake_sender(self):
        def _send(mcfg, subject, body, message_id):
            self.sent.append({"subject": subject, "body": body, "message_id": message_id})
        return _send

    def cfg(self):
        return common.load_config(self.cfgpath)

    def mcfg(self):
        return mail.load_mail_config(self.cfg())

    def db(self):
        return mail.connect()

    def payload(self, cwd=None, session=SESSION, turn=TURN, preview=None, **extra):
        p = {"cwd": str(cwd or self.root), "session_id": session, "turnId": turn,
             "responsePreview": preview}
        p.update(extra)
        return p

    def seed(self, observed: datetime, **kw):
        return mail.record_stop(self.payload(**kw), self.cfg(), observed)

    def write_log(self, ts: str, outcome: str = "completed", session: str = SESSION,
                  turn: str = TURN, kind: str = "turn-ended"):
        """按宿主真实行格式追加一条终态记录（D1 样本形状）。"""
        date = ts[:10]
        line = (f"[{ts}] [warn] [pid:15844] [main] [host-log] (local-1) [host] "
                f"[{ts}] [pid:9540] [cua-pip-session] [cua-pip-session] "
                f'event delivery dropped {{"eventId":"e-{ts}-{outcome}","kind":"{kind}",'
                f'"sequenceNumber":2108,"sessionId":"{session}","turnId":"{turn}",'
                f'"outcome":"{outcome}","skipReason":"service-disabled"}}')
        path = self.logs / f"{date}.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path

    def observed_before(self, ts: str, minutes: float = 5) -> datetime:
        """观察时刻 = 日志终态前 N 分钟（同一解析路径，机器时区无关）。"""
        at = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").astimezone()
        return (at - timedelta(minutes=minutes)).astimezone(timezone.utc)

    def tick(self, now: datetime, sender=None, clock=None):
        """clock 缺省与注入的 now 同步（确定性）；复审 F1/F2 的用例显式注入
        可变时钟来模拟“发送过程中时间流动/跨越 10:00”。"""
        if clock is None:
            holder = {"t": now}
            clock = lambda: holder["t"]  # noqa: E731
        return mail.tick(self.cfg(), now, self.sender if sender is None else sender,
                         log_dir=self.logs, clock=clock)

    def seed_delivery(self, key: str, kind: str = "daily", state: str = "pending",
                      created: datetime = None, deadline: datetime = None,
                      subject: str = None, body: str = "正文"):
        db = self.db()
        try:
            db.execute("INSERT INTO deliveries(key,kind,subject,body,state,created_at_utc,"
                       "sent_at_utc,error,deadline_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, kind, subject or f"[hq] {key}", body, state,
                        mail.utc_iso((created or bj(2026, 10, 1, 19, 0)).astimezone(timezone.utc)),
                        None, None,
                        mail.utc_iso(deadline) if deadline else None))
        finally:
            db.close()

    def _round_mails(self):
        return [s for s in self.sent if s["subject"].startswith("[hq] 本轮执行结束")]

    def seed_first_tick(self, when: datetime):
        """直接种监测起点（首次 tick 时刻），避免为门控多跑一轮 tick。"""
        db = self.db()
        try:
            db.execute("INSERT INTO meta(key,value) VALUES('first_tick_utc',?)",
                       (mail.utc_iso(when.astimezone(timezone.utc)),))
        finally:
            db.close()

    def seed_confirmed_round(self, confirmed: datetime, project=None, notified: int = 0,
                             session: str = SESSION, turn: str = TURN):
        db = self.db()
        try:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                       (session, turn, str(project or self.root),
                        mail.utc_iso((confirmed - timedelta(minutes=5)).astimezone(timezone.utc)),
                        mail.utc_iso(confirmed.astimezone(timezone.utc)), "completed",
                        "预览", notified))
        finally:
            db.close()

    def delivery(self, key: str):
        db = self.db()
        try:
            return db.execute("SELECT * FROM deliveries WHERE key=?", (key,)).fetchone()
        finally:
            db.close()

    def event(self):
        db = self.db()
        try:
            return db.execute("SELECT * FROM events WHERE session=? AND turn=?",
                              (SESSION, TURN)).fetchone()
        finally:
            db.close()


class TimePolicy(unittest.TestCase):
    def test_window_boundaries(self):
        cases = [(18, 59, False), (19, 0, True), (23, 59, True), (0, 0, True),
                 (9, 59, True), (10, 0, False), (12, 0, False)]
        for h, mi, want in cases:
            self.assertEqual(mail.in_night_window(bj(2026, 10, 2, h, mi)), want,
                             f"{h:02d}:{mi:02d}")

    def test_cross_date_window(self):
        self.assertTrue(mail.in_night_window(bj(2026, 10, 2, 23, 30)))
        self.assertTrue(mail.in_night_window(bj(2026, 10, 3, 0, 1)))

    def test_beijing_conversion_and_fallback(self):
        old = mail.BEIJING
        mail.BEIJING = TZ8  # 模拟无 IANA 数据库的固定 UTC+8 回退
        try:
            self.assertEqual(mail.to_beijing(datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)),
                             bj(2026, 10, 2, 10, 0))
            self.assertFalse(mail.in_night_window(datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)))
        finally:
            mail.BEIJING = old


class MailConfig(unittest.TestCase):
    """F6：配置在网络访问前完成校验——单收件人、字段类型、端口范围、CRLF。"""

    def base(self, **kw):
        raw = {"enabled": True, "smtp_host": "smtp.test", "smtp_port": 465,
               "security": "ssl", "username": "u", "password_env": "P",
               "from_addr": "a@b.test", "to_addr": "c@d.test"}
        raw.update(kw)
        return raw

    def test_ok(self):
        out = mail.load_mail_config({"mail": self.base()})
        self.assertEqual(out["smtp_port"], 465)
        self.assertEqual(out["to_addr"], "c@d.test")

    def test_multiple_recipients_rejected(self):
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(to_addr="b@test,c@test")})
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(to_addr="b@test c@test")})
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(to_addr="b@test;c@test")})

    def test_disabled_config_skips_validation(self):
        out = mail.load_mail_config({"mail": self.base(to_addr="bad", enabled=False,
                                                       smtp_host="", password_env="")})
        self.assertFalse(out["enabled"])

    def test_field_types_rejected(self):
        for kw in ({"smtp_port": "465"}, {"smtp_port": True}, {"smtp_port": 1.5},
                   {"smtp_host": 465}, {"enabled": "yes"}, {"username": ["x"]}):
            with self.assertRaises(mail.MailConfigError, msg=str(kw)):
                mail.load_mail_config({"mail": self.base(**kw)})

    def test_port_range(self):
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(smtp_port=70000)})
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(smtp_port=-1)})
        # 0/缺省 = 按 security 选默认端口
        self.assertEqual(mail.load_mail_config({"mail": self.base(smtp_port=0)})["smtp_port"], 465)
        no_port = {k: v for k, v in self.base().items() if k != "smtp_port"}
        self.assertEqual(mail.load_mail_config({"mail": no_port})["smtp_port"], 465)
        no_port["security"] = "starttls"
        self.assertEqual(mail.load_mail_config({"mail": no_port})["smtp_port"], 587)

    def test_addr_shape_and_crlf(self):
        for bad in ("no-at", "a@b@c", "a b@c", "a@b\nc", "a@b,c", '"a"@b.test'):
            with self.assertRaises(mail.MailConfigError, msg=bad):
                mail.load_mail_config({"mail": self.base(to_addr=bad)})
            with self.assertRaises(mail.MailConfigError, msg=bad):
                mail.load_mail_config({"mail": self.base(from_addr=bad)})

    def test_missing_required_fields(self):
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(password_env="")})
        with self.assertRaises(mail.MailConfigError):
            mail.load_mail_config({"mail": self.base(security="tls")})


class LogPaths(Base):
    """F7：宿主日志日期按宿主系统时区（可注入），北京时区只控制通知规则；
    单候选扫描窗口有界。"""

    def test_host_timezone_dates(self):
        obs = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)   # 北京 10-02 10:00 / UTC-5 10-01 21:00
        now = datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc)  # 北京 10-02 22:00 / UTC-5 10-02 09:00
        utc5 = timezone(timedelta(hours=-5))
        cand = [{"session": "s", "turn": "t", "observed": obs}]
        names = [p.name for p in mail.log_paths_for(cand, now, self.logs, tz=utc5)]
        self.assertEqual(names, ["2026-10-01.log", "2026-10-02.log"])
        # 北京口径不会产生 10-01（此前实现按北京日期算，在非 UTC+8 宿主会漏文件）
        names_bj = [p.name for p in mail.log_paths_for(cand, now, self.logs, tz=TZ8)]
        self.assertEqual(names_bj, ["2026-10-02.log"])

    def test_default_uses_system_zone_and_unique(self):
        obs = bj(2026, 10, 1, 20, 0).astimezone(timezone.utc)
        now = bj(2026, 10, 2, 20, 0)
        paths = mail.log_paths_for([{"session": "s", "turn": "t", "observed": obs}], now, self.logs)
        self.assertGreaterEqual(len(paths), 1)
        self.assertEqual(len(paths), len({p.name for p in paths}))

    def test_scan_window_bounded(self):
        obs = bj(2026, 9, 1, 20, 0).astimezone(timezone.utc)
        now = bj(2026, 10, 2, 12, 0)                 # 31 天后仍待确认的候选
        paths = mail.log_paths_for([{"session": "s", "turn": "t", "observed": obs}], now, self.logs)
        self.assertLessEqual(len(paths), mail.MAX_LOG_SCAN_DAYS + 1)
        self.assertTrue(all(p.name.startswith("2026-09") for p in paths))  # 停在窗口末，不再追到今天


class RecordStop(Base):
    def test_registered_candidate_recorded(self):
        self.assertTrue(self.seed(bj(2026, 10, 2, 20, 0)))
        row = self.event()
        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["project"], str(common.normalize_path(self.root)))

    def test_preview_truncated_to_300(self):
        self.seed(bj(2026, 10, 2, 20, 0), preview="回" * 400)
        p = self.event()["reply_summary"]
        self.assertLessEqual(len(p), 300 + 20)
        self.assertIn("截断", p)

    def test_duplicate_stop_keeps_single_row(self):
        self.seed(bj(2026, 10, 2, 20, 0), preview="第一版")
        self.seed(bj(2026, 10, 2, 20, 1), preview="第二版")
        db = self.db()
        rows = db.execute("SELECT * FROM events").fetchall()
        db.close()
        self.assertEqual(len(rows), 1)
        self.assertIn("第二版", rows[0]["reply_summary"])

    def test_unregistered_project_dropped(self):
        self.assertFalse(self.seed(bj(2026, 10, 2, 20, 0), cwd=self.base / "other"))
        self.assertIsNone(self.event())

    def test_missing_turn_rejected_without_claim(self):
        with self.assertRaises(ValueError):
            mail.record_stop({"cwd": str(self.root), "sessionId": SESSION},
                             self.cfg(), bj(2026, 10, 2, 20, 0))

    def _run_observe(self, raw: str, env_hq_home=None):
        if env_hq_home:
            os.environ["HQ_HOME"] = env_hq_home
        out = io.StringIO()
        rc = mail.cmd_observe(stdin=io.StringIO(raw), stdout=out)
        return rc, out.getvalue()

    def test_bad_payloads_fail_open(self):
        for raw in ["", "not json", "[1,2]", '"str"', '{"cwd": "x"}']:
            rc, text = self._run_observe(raw)
            self.assertEqual((rc, text), (0, "{}\n"), raw)

    def test_observe_ok_payload_emits_empty_json(self):
        rc, text = self._run_observe(json.dumps(self.payload()))
        self.assertEqual((rc, text), (0, "{}\n"))
        self.assertIsNotNone(self.event())

    def test_db_unavailable_still_fail_open(self):
        blocker = self.base / "blocked"
        blocker.write_text("not a dir", encoding="utf-8")
        rc, text = self._run_observe(json.dumps(self.payload()), env_hq_home=str(blocker))
        self.assertEqual((rc, text), (0, "{}\n"))


class Confirm(Base):
    def test_completed_confirmed_by_host_log(self):
        ts = "2026-10-02 20:05:00.100"
        self.seed(self.observed_before(ts))
        self.write_log(ts)
        lines = self.tick(bj(2026, 10, 2, 20, 10))
        row = self.event()
        self.assertEqual(row["outcome"], "completed")
        want = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f").astimezone() \
            .astimezone(timezone.utc).replace(microsecond=0)  # 落库秒级精度
        self.assertEqual(mail.from_iso(row["confirmed_at_utc"]), want)
        self.assertTrue(any("完成通知" in l for l in lines))

    def test_without_host_log_stays_pending_and_no_mail(self):
        self.seed(bj(2026, 10, 2, 20, 0))
        self.tick(bj(2026, 10, 2, 20, 10))
        self.assertEqual(self.event()["outcome"], "pending")
        self.assertIsNone(self.delivery(mail.round_key(SESSION, TURN)))

    def test_end_before_observed_keeps_pending(self):
        ts = "2026-10-02 20:05:00.100"
        observed = self.observed_before(ts)
        mail.record_stop(self.payload(), self.cfg(),
                         observed + timedelta(minutes=10))  # 观察晚于“终态”
        self.write_log(ts)
        self.tick(observed + timedelta(minutes=11))
        self.assertEqual(self.event()["outcome"], "pending")

    def test_other_outcome_recorded_no_completion_mail(self):
        ts = "2026-10-02 20:05:00.100"
        self.seed(self.observed_before(ts))
        self.write_log(ts, outcome="failed")
        self.tick(bj(2026, 10, 2, 20, 10))
        self.assertEqual(self.event()["outcome"], "failed")
        self.assertIsNone(self.delivery(mail.round_key(SESSION, TURN)))

    def test_other_kind_line_ignored(self):
        ts = "2026-10-02 20:05:00.100"
        self.seed(self.observed_before(ts))
        self.write_log(ts, kind="turn-terminal")
        self.tick(bj(2026, 10, 2, 20, 10))
        self.assertEqual(self.event()["outcome"], "pending")

    def test_unreadable_log_keeps_pending(self):
        self.seed(bj(2026, 10, 2, 20, 0))
        (self.logs / "2026-10-02.log").write_bytes(b"\xff\xfe\x00bad")
        out = io.StringIO()
        old = sys.stderr
        sys.stderr = out
        try:
            mail.confirm_rounds(
                [{"session": SESSION, "turn": TURN,
                  "observed": bj(2026, 10, 2, 20, 0).astimezone(timezone.utc)}],
                [self.logs / "missing-2026-10-02.log"])
        finally:
            sys.stderr = old
        self.assertEqual(self.event()["outcome"], "pending")


class Tick(Base):
    def _round_mails(self):
        return [s for s in self.sent if s["subject"].startswith("[hq] 本轮执行结束")]

    def test_stop_before_end_not_sent_until_confirmed(self):
        ts = "2026-10-02 20:05:00.100"
        self.seed(self.observed_before(ts))
        self.tick(bj(2026, 10, 2, 20, 2))
        self.assertEqual(self._round_mails(), [])    # 未确认：不发
        self.write_log(ts)
        self.tick(bj(2026, 10, 2, 20, 10))
        self.assertEqual(len(self._round_mails()), 1)
        self.assertIn("本轮执行结束", self.sent[-1]["subject"])
        self.assertIn(SESSION[:8], self.sent[-1]["body"])

    def test_same_turn_repeated_tick_no_resend(self):
        ts = "2026-10-02 20:05:00.100"
        self.seed(self.observed_before(ts))
        self.write_log(ts)
        self.tick(bj(2026, 10, 2, 20, 10))
        self.tick(bj(2026, 10, 2, 20, 30))
        self.assertEqual(len(self._round_mails()), 1)

    def test_daytime_completion_no_mail_only_digest(self):
        ts = "2026-10-02 14:00:00.000"
        self.seed(self.observed_before(ts))
        self.write_log(ts)
        self.tick(bj(2026, 10, 2, 14, 5))
        self.assertIsNone(self.delivery(mail.round_key(SESSION, TURN)))
        self.assertEqual(self.event()["outcome"], "completed")
        # 19:00 后的日报包含该轮，并注明无完成通知（窗口外完成）
        self.tick(bj(2026, 10, 2, 19, 5))
        daily = self.delivery("daily:20261002")
        self.assertIsNotNone(daily)
        self.assertIn("窗口外完成", daily["body"])
        self.assertIn(SESSION[:8], daily["body"])

    def test_completed_in_window_but_send_time_outside_skips(self):
        ts = "2026-10-01 21:00:00.000"
        self.seed(self.observed_before(ts))
        self.write_log(ts)
        self.tick(bj(2026, 10, 2, 12, 0))            # 次日 12:00 首个 tick
        d = self.delivery(mail.round_key(SESSION, TURN))
        self.assertEqual(d["state"], "skipped")
        self.assertIn("次日 10:00", d["error"])
        self.assertEqual([s for s in self.sent if s["subject"].startswith("[hq] 本轮")], [])

    def test_first_tick_next_evening_inside_window_still_skips(self):
        """F1：完成 10-01 21:00，首个 tick 是 10-02 20:00（夜间窗口内！）。
        该轮自己的截止是 10-02 10:00，已过 → skipped，不补发。"""
        ts = "2026-10-01 21:00:00.000"
        self.seed(self.observed_before(ts))
        self.write_log(ts)
        lines = self.tick(bj(2026, 10, 2, 20, 0))    # 旧实现此时会 sent
        d = self.delivery(mail.round_key(SESSION, TURN))
        self.assertEqual(d["state"], "skipped")
        self.assertEqual(self._round_mails(), [])
        self.assertTrue(any("skipped" in l or "截止" in l for l in lines))

    def test_same_day_early_completion_deadline_is_today_10(self):
        """F1：完成在 [00:00,10:00)，截止是当天 10:00（不是次日）。"""
        dl = mail.round_deadline(bj(2026, 10, 2, 8, 30))
        self.assertEqual(dl, bj(2026, 10, 2, 10, 0).astimezone(timezone.utc))
        # 09:59 仍可发；10:00（含）起过期
        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           subject="[hq] 本轮执行结束 · proj",
                           created=bj(2026, 10, 2, 8, 35), deadline=dl)
        self.tick(bj(2026, 10, 2, 9, 59))
        self.assertEqual(self.delivery(mail.round_key(SESSION, TURN))["state"], "sent")
        self.seed_delivery("round:s2:t2", kind="round", subject="[hq] 本轮执行结束 · proj2",
                           created=bj(2026, 10, 2, 8, 36), deadline=dl)
        self.tick(bj(2026, 10, 2, 10, 0), clock=lambda: bj(2026, 10, 2, 10, 0))
        self.assertEqual(self.delivery("round:s2:t2")["state"], "skipped")
        self.assertEqual(len(self._round_mails()), 1)

    def test_queue_crosses_10oclock_between_mails(self):
        """F1：逐封校验——队列第一封在 09:59 发出，第二封在实际发送前时钟
        已跨 10:00 → skipped；不得用 tick 起点固定时刻放行整队。"""
        now0 = bj(2026, 10, 2, 9, 59)
        self.seed_confirmed_round(bj(2026, 10, 1, 21, 0), notified=1,
                                  session="sess-a", turn="turn-a")
        self.seed_confirmed_round(bj(2026, 10, 1, 22, 0), notified=1,
                                  session="sess-b", turn="turn-b")
        deadline = mail.utc_iso(mail.round_deadline(bj(2026, 10, 1, 21, 0)))
        db = self.db()
        try:
            for s, t in (("sess-a", "turn-a"), ("sess-b", "turn-b")):
                db.execute("INSERT INTO deliveries(key,kind,subject,body,state,created_at_utc,"
                           "sent_at_utc,error,deadline_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                           (mail.round_key(s, t), "round", f"[hq] 本轮执行结束 · {s}", "正文",
                            "pending",
                            mail.utc_iso(bj(2026, 10, 1, 21, 5).astimezone(timezone.utc)),
                            None, None, deadline))
        finally:
            db.close()
        clock_vals = iter([bj(2026, 10, 2, 9, 59, 30), bj(2026, 10, 2, 10, 0, 30)])
        last = {"t": bj(2026, 10, 2, 9, 59, 30)}

        def clock():
            try:
                last["t"] = next(clock_vals)
            except StopIteration:
                pass                          # 序列耗尽后停在最后一刻
            return last["t"]

        self.tick(now0, clock=clock)
        self.assertEqual(self.delivery(mail.round_key("sess-a", "turn-a"))["state"], "sent")
        self.assertEqual(self.delivery(mail.round_key("sess-b", "turn-b"))["state"], "skipped")
        self.assertEqual(len(self._round_mails()), 1)

    def test_pending_round_mail_ages_out_at_window_end(self):
        self.seed_confirmed_round(bj(2026, 10, 1, 21, 5), notified=1)
        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           subject="[hq] 本轮执行结束 · proj",
                           created=bj(2026, 10, 1, 21, 5),
                           deadline=mail.round_deadline(bj(2026, 10, 1, 21, 5)))
        self.tick(bj(2026, 10, 2, 10, 30))           # 已过该轮截止 10:00
        self.assertEqual(self.delivery(mail.round_key(SESSION, TURN))["state"], "skipped")
        self.assertEqual(self._round_mails(), [])    # 完成通知不发送（前一天日报补发属正常）

    def test_legacy_round_delivery_deadline_backfilled(self):
        """旧库旧行没有 deadline 列值：tick 依据事件确认时刻回填并按截止处理。"""
        self.seed_confirmed_round(bj(2026, 10, 1, 21, 5), notified=1)
        db = self.db()
        try:
            db.execute("INSERT INTO deliveries(key,kind,subject,body,state,created_at_utc)"
                       " VALUES(?,?,?,?,?,?)",
                       (mail.round_key(SESSION, TURN), "round", "s", "正文", "pending",
                        mail.utc_iso(bj(2026, 10, 1, 21, 5).astimezone(timezone.utc))))
        finally:
            db.close()
        self.assertIsNone(self.delivery(mail.round_key(SESSION, TURN))["deadline_utc"])
        self.tick(bj(2026, 10, 2, 10, 30))
        row = self.delivery(mail.round_key(SESSION, TURN))
        self.assertIsNotNone(row["deadline_utc"])    # 回填
        self.assertEqual(row["state"], "skipped")    # 并按截止正确过期
        self.assertEqual(self._round_mails(), [])

    def test_uncertain_not_resent_other_mail_still_sent(self):
        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           created=bj(2026, 10, 1, 20, 0),
                           deadline=bj(2026, 10, 2, 10, 0))
        self.seed_delivery("daily:20261001", kind="daily", created=bj(2026, 10, 1, 20, 0))

        def flaky(mcfg, subject, body, mid):
            self.sent.append({"subject": subject, "body": body, "message_id": mid})
            if "round" in subject:
                raise mail.MailUnknownError("连接中断")

        self.tick(bj(2026, 10, 2, 9, 0), sender=flaky)   # 窗口内：未过截止
        self.assertEqual(self.delivery(mail.round_key(SESSION, TURN))["state"], "uncertain")
        self.assertEqual(self.delivery("daily:20261001")["state"], "sent")
        before = len(self.sent)
        self.tick(bj(2026, 10, 2, 9, 5), sender=flaky)
        self.assertEqual(len(self.sent), before)     # 不自动重发

    def test_sending_row_not_recycled_by_next_tick(self):
        """F2：崩溃遗留的 sending 行不被下一个 tick 回收/重发/改判——
        另一个 tick 无法判定活跃发送器是否已退出。"""
        self.seed_delivery("daily:20261001", kind="daily", state="sending",
                           created=bj(2026, 10, 1, 19, 0))
        lines = self.tick(bj(2026, 10, 2, 10, 0))
        row = self.delivery("daily:20261001")
        self.assertEqual(row["state"], "sending")    # 保持，不标 uncertain
        self.assertEqual(self.sent, [])              # 不自动重发
        self.assertFalse(any("结果待核对" in l for l in lines))

    def test_overlapping_ticks_do_not_recycle_active_sender(self):
        """F2：真正重叠的完整 tick——第一 tick 的发送器仍阻塞存活时，
        第二 tick 不得把它的 sending 行改判 uncertain，也不重发。"""
        import threading

        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           subject="[hq] 本轮执行结束 · proj",
                           created=bj(2026, 10, 2, 19, 5),
                           deadline=mail.round_deadline(bj(2026, 10, 2, 19, 0)))
        started, release = threading.Event(), threading.Event()

        def slow_sender(mcfg, subject, body, mid):
            started.set()
            release.wait(timeout=30)
            self.sent.append({"subject": subject, "body": body, "message_id": mid})

        t1_result = {}

        def first_tick():
            t1_result["lines"] = self.tick(bj(2026, 10, 2, 20, 0), sender=slow_sender)

        t1 = threading.Thread(target=first_tick)
        t1.start()
        self.assertTrue(started.wait(timeout=10), "第一 tick 未进入发送器")
        import time
        for _ in range(200):                          # 等待 sending 状态出现
            if self.delivery(mail.round_key(SESSION, TURN))["state"] == "sending":
                break
            time.sleep(0.05)
        else:
            self.fail("sending 状态未出现")
        # 第二个 tick 与活跃发送器重叠运行
        lines2 = self.tick(bj(2026, 10, 2, 20, 1))
        row = self.delivery(mail.round_key(SESSION, TURN))
        self.assertEqual(row["state"], "sending")    # 不改判
        self.assertFalse(any("待核对" in l for l in lines2))
        self.assertEqual([s for s in self.sent if "本轮" in s["subject"]], [])  # 第二 tick 未发
        release.set()
        t1.join(timeout=30)
        self.assertFalse(t1.is_alive())
        row = self.delivery(mail.round_key(SESSION, TURN))
        self.assertEqual(row["state"], "sent")       # 活跃发送器正常落库
        self.assertEqual(len(self._round_mails()), 1)

    def test_unregistered_project_round_not_sent(self):
        """F7：发送前按当前登记范围过滤——取消登记的项目不发其通知。"""
        self.seed_confirmed_round(bj(2026, 10, 1, 21, 0), notified=1)
        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           subject="[hq] 本轮执行结束 · proj",
                           created=bj(2026, 10, 1, 21, 5),
                           deadline=mail.round_deadline(bj(2026, 10, 1, 21, 0)))
        # 重写配置：不再登记任何项目
        lines = ["wip_limit = 3", "[mail]", "enabled = true", 'smtp_host = "smtp.test"',
                 "smtp_port = 465", 'security = "ssl"', 'username = "u"',
                 'password_env = "HQ_TEST_SMTP_PASS"', 'from_addr = "hq@test"',
                 'to_addr = "me@test"']
        self.cfgpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self.tick(bj(2026, 10, 2, 9, 0))
        row = self.delivery(mail.round_key(SESSION, TURN))
        self.assertEqual(row["state"], "skipped")
        self.assertIn("已不在 hq.toml 登记", row["error"])
        self.assertEqual(self._round_mails(), [])
        self.assertTrue(any("取消登记" in l for l in out))

    def test_disabled_tick_is_noop(self):
        self.write_cfg(enabled=False)
        self.seed(bj(2026, 10, 2, 20, 0))
        lines = mail.tick(self.cfg(), bj(2026, 10, 2, 20, 5), self.sender, log_dir=self.logs)
        self.assertTrue(any("未启用" in l for l in lines))
        self.assertEqual(self.sent, [])

    def test_config_error_rejects_tick(self):
        self.write_cfg(enabled=True)
        p = self.cfgpath
        p.write_text(p.read_text(encoding="utf-8").replace('security = "ssl"',
                                                          'security = "tls"'),
                     encoding="utf-8")
        with self.assertRaises(mail.MailConfigError):
            mail.tick(self.cfg(), bj(2026, 10, 2, 20, 5), self.sender, log_dir=self.logs)


class Digest(Base):
    def insert_event(self, confirmed: datetime, outcome="completed", project=None):
        db = self.db()
        try:
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                       (f"s-{confirmed:%m%d%H%M}", f"t{confirmed:%m%d%H%M}",
                        str(project or self.root),
                        mail.utc_iso((confirmed - timedelta(minutes=5)).astimezone(timezone.utc)),
                        mail.utc_iso(confirmed.astimezone(timezone.utc)), outcome, None, 1))
        finally:
            db.close()

    def daily_mail(self):
        got = [s for s in self.sent if s["subject"].startswith("[hq] 日报")]
        self.assertEqual(len(got), 1)
        return got[0]

    def test_empty_day_still_sends(self):
        self.tick(bj(2026, 10, 2, 12, 0))            # 首次 tick＝监测起点 12:00
        self.tick(bj(2026, 10, 2, 19, 5))            # 启用后的首个 19:00：空日也发
        body = self.daily_mail()["body"]
        self.assertIn("本窗口无已记录轮次", body)
        self.assertIn("监测起点", body)
        self.assertIn("一句话现状", body)           # 项目 STATUS 摘要照常拼接
        self.assertIn("依赖外部 API", body)         # ⛔ 阻塞
        self.assertIn("补齐回归测试", body)         # ▶ 下一步

    def test_first_tick_no_pre_enable_digest(self):
        """F3：监测起点之前没有已知历史——12:00 的首次 tick 不得补发
        “昨天 19:00”的启用前报告；首份日报是启用后的 19:00。"""
        self.tick(bj(2026, 10, 2, 12, 0))
        self.assertIsNone(self.delivery("daily:20261001"))   # 昨天 19:00：不生成
        self.assertIsNone(self.delivery("daily:20261002"))
        self.tick(bj(2026, 10, 2, 19, 5))
        self.assertIsNotNone(self.delivery("daily:20261002"))
        self.assertIsNone(self.delivery("daily:20261001"))

    def test_window_boundaries(self):
        # 直接种监测起点，避免先跑一次 tick 生成上一期日报干扰发送计数
        self.insert_event(bj(2026, 10, 1, 19, 0))   # 左闭：含
        self.insert_event(bj(2026, 10, 1, 18, 59))  # 窗口前：不含
        self.insert_event(bj(2026, 10, 2, 19, 0))   # 恰在截止：不含（右开）
        db = self.db()
        try:
            db.execute("INSERT INTO meta(key,value) VALUES('first_tick_utc',?)",
                       (mail.utc_iso(bj(2026, 10, 1, 12, 0).astimezone(timezone.utc)),))
        finally:
            db.close()
        self.tick(bj(2026, 10, 2, 19, 5))
        body = self.daily_mail()["body"]
        self.assertIn("t1001190", body)              # 正文只显示 turn 前 8 位
        self.assertNotIn("t1001185", body)
        self.assertNotIn("t1002190", body)

    def test_offline_backfills_only_latest_and_notes_gap(self):
        self.insert_event(bj(2026, 10, 1, 10, 0))   # 覆盖起点（10-01 09:55 观察）
        self.seed_delivery("daily:20261001", kind="daily", state="sent",
                           created=bj(2026, 10, 1, 19, 5))
        # 10-02 19:00 的日报错过（离线），10-03 12:00 才恢复
        self.tick(bj(2026, 10, 3, 12, 0))
        _db = self.db()
        try:
            keys = [r["key"] for r in _db.execute("SELECT key FROM deliveries WHERE kind='daily'")]
        finally:
            _db.close()
        self.assertEqual(keys, ["daily:20261001", "daily:20261002"])
        body = self.daily_mail()["body"]
        self.assertIn("延迟生成", body)
        self.assertIn("2026-10-02 19:00", body)     # 漏报区间如实注明

    def test_older_pending_daily_skipped_only_latest_backfills(self):
        """R2-1：旧 daily pending 不能与新一期一同补发——恢复后只发当前应补期。"""
        self.insert_event(bj(2026, 10, 1, 10, 0))
        self.seed_delivery("daily:20261001", kind="daily", state="pending",
                           created=bj(2026, 10, 1, 19, 5))
        self.tick(bj(2026, 10, 3, 12, 0))            # 中途 10-02 的日报也错过
        self.assertEqual(self.delivery("daily:20261002")["state"], "sent")
        row = self.delivery("daily:20261001")
        self.assertEqual(row["state"], "skipped")
        self.assertIn("旧日报不补发", row["error"])
        got = [s for s in self.sent if s["subject"].startswith("[hq] 日报")]
        self.assertEqual(len(got), 1)
        self.assertIn("2026-10-02", got[0]["subject"])

    def test_old_pending_not_backfilled_when_latest_already_sent(self):
        """R2-1 反例：最新一期已 sent 时，唯一旧 pending 不得被当作“最近”补发。"""
        self.seed_first_tick(bj(2026, 10, 1, 12, 0))
        self.seed_delivery("daily:20261001", kind="daily", state="pending",
                           created=bj(2026, 10, 1, 19, 5))
        self.seed_delivery("daily:20261002", kind="daily", state="sent",
                           created=bj(2026, 10, 2, 19, 5))
        self.tick(bj(2026, 10, 3, 12, 0))
        self.assertEqual(self.delivery("daily:20261001")["state"], "skipped")
        self.assertEqual(self.delivery("daily:20261002")["state"], "sent")  # 不动
        self.assertEqual(self.sent, [])              # 零发送

    def test_old_pending_not_backfilled_when_latest_failed_uncertain_sending(self):
        """R2-1：最新一期 failed/uncertain/sending 都不能让旧 pending 顶替补发。"""
        for state in ("failed", "uncertain", "sending"):
            with self.subTest(state=state):
                self.tearDown()
                self.setUp()
                self.seed_first_tick(bj(2026, 10, 1, 12, 0))
                self.seed_delivery("daily:20261001", kind="daily", state="pending",
                                   created=bj(2026, 10, 1, 19, 5))
                self.seed_delivery("daily:20261002", kind="daily", state=state,
                                   created=bj(2026, 10, 2, 19, 5))
                self.tick(bj(2026, 10, 3, 12, 0))
                self.assertEqual(self.delivery("daily:20261001")["state"], "skipped")
                self.assertEqual(self.delivery("daily:20261002")["state"], state)  # 原样保留
                self.assertEqual(self.sent, [])

    def test_build_failure_does_not_backfill_older(self):
        """R2-1：最新一期构建失败也不能用旧 pending 日报代替。"""
        self.seed_first_tick(bj(2026, 10, 1, 12, 0))
        self.seed_delivery("daily:20261001", kind="daily", state="pending",
                           created=bj(2026, 10, 1, 19, 5))
        real_build = mail.build_daily

        def boom(*a, **k):
            raise RuntimeError("构建失败")

        mail.build_daily = boom
        try:
            self.tick(bj(2026, 10, 3, 12, 0))
        finally:
            mail.build_daily = real_build
        self.assertIsNone(self.delivery("daily:20261002"))   # 本期未生成
        self.assertEqual(self.delivery("daily:20261001")["state"], "skipped")
        self.assertEqual(self.sent, [])

    def test_digest_splits_missing_from_undelivered(self):
        """R2-1：漏报（无记录）与投递未完成（failed 等）分开标注，不据此重发。"""
        self.seed_first_tick(bj(2026, 10, 1, 12, 0))
        self.seed_delivery("daily:20261001", kind="daily", state="failed",
                           created=bj(2026, 10, 1, 19, 5))
        self.tick(bj(2026, 10, 3, 20, 0))            # 当前应补期 10-03 无记录→本期构建
        row = self.delivery("daily:20261003")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "sent")
        body = row["body"]
        self.assertIn("漏报的日报截止点：2026-10-02 19:00", body)          # 无记录
        self.assertIn("投递未完成的日报截止点：2026-10-01 19:00（failed）", body)
        self.assertIn("不据此自动重发", body)
        # failed 旧报不重发：只发本期构建的这一封
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["subject"], row["subject"])

    def test_sent_at_records_acceptance_time(self):
        """R2-3：sent_at_utc 记录实际发送接受时刻（注入时钟），非 tick 起点。"""
        t0, t1 = bj(2026, 10, 2, 20, 0), bj(2026, 10, 2, 20, 7)
        self.seed_delivery("daily:20261002", kind="daily", created=bj(2026, 10, 2, 19, 5))
        self.tick(t0, clock=lambda: t1)
        row = self.delivery("daily:20261002")
        self.assertEqual(row["state"], "sent")
        self.assertEqual(mail.from_iso(row["sent_at_utc"]), t1.astimezone(timezone.utc))

    def test_digest_notes_failed_uncertain_sending_not_window_miss(self):
        """F3：failed/uncertain/sending 的完成通知不得写成“窗口外完成”。"""
        for i, state in enumerate(["failed", "uncertain", "sending", "skipped"]):
            s, t = f"sess-{state}", f"turn-{state}"
            self.seed_confirmed_round(bj(2026, 10, 2, 21, i), session=s, turn=t)
            db = self.db()
            try:
                db.execute("INSERT INTO deliveries(key,kind,subject,body,state,created_at_utc,"
                           "sent_at_utc,error,deadline_utc) VALUES(?,?,?,?,?,?,?,?,?)",
                           (mail.round_key(s, t), "round", f"[hq] {state}", "正文", state,
                            mail.utc_iso(bj(2026, 10, 2, 21, i).astimezone(timezone.utc)),
                            None, None,
                            mail.utc_iso(mail.round_deadline(bj(2026, 10, 2, 21, i)))))
            finally:
                db.close()
        self.tick(bj(2026, 10, 2, 12, 0))
        self.tick(bj(2026, 10, 3, 19, 5))
        body = self.daily_mail()["body"]
        self.assertIn("通知发送失败（未接受）", body)
        self.assertIn("通知结果待核对（不自动重发）", body)
        self.assertIn("通知发送中（结果待核对，不自动重发）", body)
        self.assertIn("通知已过窗（未发，只进日报）", body)
        self.assertNotIn("无完成通知（窗口外完成）", body)

    def test_bad_status_file_isolated_from_rounds_and_digest(self):
        """F3：单项目 STATUS.md 非法 UTF-8——完成通知照常发送、日报注明读取失败，
        tick 不再整体抛 UnicodeDecodeError。"""
        (self.root / "STATUS.md").write_bytes(b"\xff\xfe\x00\xffbad")
        self.seed_confirmed_round(bj(2026, 10, 1, 21, 0), notified=1)
        self.seed_delivery(mail.round_key(SESSION, TURN), kind="round",
                           subject="[hq] 本轮执行结束 · proj",
                           created=bj(2026, 10, 1, 21, 5),
                           deadline=mail.round_deadline(bj(2026, 10, 1, 21, 0)))
        self.tick(bj(2026, 10, 2, 9, 0))             # 旧实现此处抛异常、零发送
        self.assertEqual(len(self._round_mails()), 1)
        self.assertEqual(self.delivery(mail.round_key(SESSION, TURN))["state"], "sent")
        self.tick(bj(2026, 10, 2, 19, 5))            # 19:00 日报照常生成
        body = self.daily_mail()["body"]
        self.assertIn("项目资料读取失败", body)
        self.assertIn("不影响其他项目与其他通知", body)

    def test_digest_is_readonly_and_uses_verify_record(self):
        (self.root / ".hq").mkdir()
        (self.root / ".hq" / "verify.json").write_text(json.dumps({
            "checks": {"unit": {"ok": True, "at": "2026-10-02T12:00:00", "tier": "quick"}},
            "last_run": {"at": "2026-10-02T12:00:00", "tier": "quick", "ok": True,
                         "ids": ["unit"]}}), encoding="utf-8")
        targets = [self.root / "STATUS.md", self.root / ".hq" / "verify.json"]
        before = {t: t.read_bytes() for t in targets}
        self.seed_first_tick(bj(2026, 10, 1, 12, 0))    # 监测起点早于截止点
        self.tick(bj(2026, 10, 2, 19, 5))
        for t in targets:
            self.assertEqual(t.read_bytes(), before[t], str(t))
        body = self.daily_mail()["body"]
        self.assertIn("1/1 通过", body)
        self.assertIn("snapshot_at", body)

    def test_missing_project_files_marked_unrecorded(self):
        other = self.base / "empty-proj"
        other.mkdir()
        self.write_cfg(enabled=True, extra_projects=[other])
        self.seed_first_tick(bj(2026, 10, 1, 12, 0))
        self.tick(bj(2026, 10, 2, 19, 5))
        body = self.daily_mail()["body"]
        self.assertIn("未记录（缺 STATUS.md）", body)
        self.assertIn("未记录（无 .hq/verify.json）", body)

    def test_unconfirmed_counted_not_masqueraded(self):
        self.seed(bj(2026, 10, 2, 18, 0))           # Stop 已登记，终态未匹配
        self.tick(bj(2026, 10, 2, 19, 5))
        body = self.daily_mail()["body"]
        self.assertIn("待确认候选（截至本报告生成时的累计，不限本窗口）：1 个", body)
        self.assertIsNone(self.delivery(mail.round_key(SESSION, TURN)))


class Claim(Base):
    def seed_pending(self, keys):
        for i, key in enumerate(keys):
            self.seed_delivery(key, kind="daily", created=bj(2026, 10, 1, 19, i))

    def test_concurrent_tick_single_claim(self):
        keys = ["daily:20261001", "daily:20261002"]
        self.seed_pending(keys)
        db1, db2 = mail.connect(), mail.connect()
        try:
            got1 = mail._deliver(db1, self.mcfg(), self.sender)
            got2 = mail._deliver(db2, self.mcfg(), self.sender)
        finally:
            db1.close()
            db2.close()
        self.assertEqual(len(got1), 2)
        self.assertEqual(got2, [])
        self.assertEqual(len(self.sent), 2)

    def test_permanent_failure_marks_failed(self):
        self.seed_pending(["daily:20261001"])

        def refusing(mcfg, subject, body, mid):
            raise mail.MailPermanentError("认证失败")

        db = mail.connect()
        try:
            lines = mail._deliver(db, self.mcfg(), refusing)
        finally:
            db.close()
        self.assertEqual(self.delivery("daily:20261001")["state"], "failed")
        self.assertIn("失败", lines[0])


class SendMail(unittest.TestCase):
    def setUp(self):
        self._old_pass = os.environ.get("HQ_TEST_SMTP_PASS")
        os.environ["HQ_TEST_SMTP_PASS"] = "pw"

    def tearDown(self):
        if self._old_pass is None:
            os.environ.pop("HQ_TEST_SMTP_PASS", None)
        else:
            os.environ["HQ_TEST_SMTP_PASS"] = self._old_pass

    def settings(self, security="ssl", password_env="HQ_TEST_SMTP_PASS"):
        return {"enabled": True, "smtp_host": "smtp.test", "smtp_port": 465,
                "security": security, "username": "u", "password_env": password_env,
                "from_addr": "a@b.test", "to_addr": "c@d.test"}

    class FakeSMTP:
        instances = []

        def __init__(self, host=None, port=None, timeout=None, context=None,
                     send_error=None, refused=None, quit_error=None,
                     auth_error=None, connect_error=None):
            self.host, self.port = host, port
            self.starttls_called = False
            self.logged = None
            self.quit_error = quit_error
            self.send_error = send_error
            self.refused = refused or {}
            self.auth_error = auth_error
            if connect_error:
                raise connect_error
            type(self).instances.append(self)

        def ehlo(self):
            pass

        def starttls(self, context=None):
            self.starttls_called = True

        def login(self, u, p):
            if self.auth_error:
                raise self.auth_error
            self.logged = (u, p)

        def send_message(self, msg):
            if self.send_error:
                raise self.send_error
            return self.refused

        def quit(self):
            if self.quit_error:
                raise self.quit_error

    def run_send(self, **smtp_kw):
        made = []

        def factory():
            def make():
                s = self.FakeSMTP(**smtp_kw)
                made.append(s)
                return s
            import ssl as _ssl
            return make, _ssl.create_default_context()

        mail.send_mail(self.settings(), "[hq] 主题", "正文", "<x@y>", factory=factory)
        return made

    def test_accept_then_quit_failure_still_success(self):
        made = self.run_send(quit_error=__import__("smtplib").SMTPServerDisconnected("bye"))
        self.assertEqual(len(made), 1)

    def test_recipient_refused_is_permanent(self):
        with self.assertRaises(mail.MailPermanentError):
            self.run_send(refused={"c@d.test": (550, b"no such user")})

    def test_smtp_data_error_is_permanent(self):
        """F6：DATA 阶段服务器明确拒绝（如 554）＝确定未接受 → failed，不是未知。"""
        import smtplib
        with self.assertRaises(mail.MailPermanentError) as cm:
            self.run_send(send_error=smtplib.SMTPDataError(554, b"message rejected"))
        self.assertIn("明确拒绝", str(cm.exception))

    def test_sender_refused_is_permanent(self):
        import smtplib
        with self.assertRaises(mail.MailPermanentError):
            self.run_send(send_error=smtplib.SMTPSenderRefused(550, b"sender denied", "a@b.test"))

    def test_auth_error_still_permanent_before_response_handler(self):
        import smtplib
        with self.assertRaises(mail.MailPermanentError):
            self.run_send(auth_error=smtplib.SMTPAuthenticationError(535, b"bad"))

    def test_disconnected_mid_send_is_unknown(self):
        with self.assertRaises(mail.MailUnknownError):
            self.run_send(send_error=__import__("smtplib").SMTPServerDisconnected("dropped"))

    def test_connect_failure_is_permanent(self):
        with self.assertRaises(mail.MailPermanentError):
            self.run_send(connect_error=ConnectionRefusedError("refused"))

    def test_auth_failure_is_permanent(self):
        import smtplib
        with self.assertRaises(mail.MailPermanentError):
            self.run_send(auth_error=smtplib.SMTPAuthenticationError(535, b"bad"))

    def test_password_env_missing_is_permanent_without_connect(self):
        old = os.environ.get("HQ_TEST_SMTP_PASS")
        os.environ.pop("HQ_TEST_SMTP_PASS", None)
        try:
            with self.assertRaises(mail.MailPermanentError):
                self.run_send()
        finally:
            if old is not None:
                os.environ["HQ_TEST_SMTP_PASS"] = old

    def test_header_crlf_rejected(self):
        with self.assertRaises(mail.MailPermanentError):
            mail.send_mail(self.settings(), "bad\nsubject", "正文", "<x@y>")

    def test_starttls_used_only_when_configured(self):
        for security, want in (("starttls", True), ("ssl", False)):
            made = []

            def factory(security=security, made=made):
                def make():
                    s = self.FakeSMTP()
                    made.append(s)
                    return s
                import ssl as _ssl
                return make, _ssl.create_default_context()

            mail.send_mail(self.settings(security), "s", "b", "<x@y>", factory=factory)
            self.assertEqual(made[-1].starttls_called, want, security)

    def test_message_id_deterministic(self):
        self.assertEqual(mail.message_id_for("round:a:b", "a@b.test"),
                         mail.message_id_for("round:a:b", "a@b.test"))
        self.assertIn("@b.test", mail.message_id_for("k", "a@b.test"))


class Setup(Base):
    def setUp(self):
        super().setUp()
        self.home = self.base / "home"
        zcode_dir = self.home / ".zcode" / "cli"
        zcode_dir.mkdir(parents=True)
        self.target = zcode_dir / "config.json"
        self.ctx = {"python": str(self.base / "py.exe"),
                    "bootstrap": self.base / "bin" / "hq.py",
                    "hq_config": self.cfgpath, "home": self.home,
                    "backups": self.base / "backups"}
        self.other_handler = {"type": "command", "command": "other-tool --watch"}
        self.gate_args = ["-X", "utf8", str(self.ctx["bootstrap"]), "--hq-config",
                          str(self.cfgpath), "hook", "--agent", "zcode", "--event", "stop"]
        self.base_config = {"hooks": {"enabled": True, "events": {
            "Stop": [{"hooks": [
                {"type": "process", "enabled": False, "command": "py.exe",
                 "args": list(self.gate_args), "timeoutMs": 300000},
                dict(self.other_handler)]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "greeter"}]}]}}}
        self.write_target(self.base_config)

    def write_target(self, data):
        self.target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")

    def read_target(self):
        return json.loads(self.target.read_text(encoding="utf-8"))

    def test_dry_run_changes_nothing_and_reports(self):
        before = self.target.read_bytes()
        info = mail.inspect_setup(self.cfg(), self.ctx)
        lines = mail.plan_setup_lines(info, True)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(info["mail_present"])
        self.assertTrue(info["would_change"])
        self.assertEqual(len(info["old_gate"]), 1)
        self.assertEqual(info["old_gate"][0]["enabled"], False)
        self.assertEqual(info["other_handlers"], 2)  # other-tool + SessionStart greeter
        joined = "\n".join(lines)
        self.assertIn("旧 gate 条目：1 条", joined)
        self.assertIn("保持不动", joined)
        self.assertIn("hooks.enabled：True", joined)
        self.assertIn("mail observe", joined)
        self.assertIn("mail tick", joined)

    def test_apply_preserves_others_and_is_idempotent(self):
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(errors, [])
        data = self.read_target()
        stop = [h for g in data["hooks"]["events"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(stop), 3)
        mine = [h for h in stop if mail.identity_from_mail_args(h.get("args"))]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0]["enabled"])
        self.assertEqual(mine[0]["timeoutMs"], mail.OBSERVE_TIMEOUT_MS)
        # 旧 gate 与其他 handler 原样保留（含 enabled=False 的停用状态）
        self.assertEqual(stop[0]["args"], self.gate_args)
        self.assertFalse(stop[0]["enabled"])
        self.assertEqual({k: v for k, v in stop[1].items() if k in self.other_handler},
                         self.other_handler)
        self.assertTrue(list(self.ctx["backups"].iterdir()))
        before = self.target.read_bytes(), self.target.stat().st_mtime_ns
        messages2, errors2 = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(errors2, [])
        self.assertEqual(messages2[0], f"已确认 mail observe 条目最新（{self.target}）")
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), before)

    def test_apply_reports_disabled_global_switch(self):
        cfg = self.read_target()
        cfg["hooks"]["enabled"] = False
        self.write_target(cfg)
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(errors, [])
        self.assertTrue(any("hooks.enabled 非 true" in m for m in messages))
        self.assertFalse(self.read_target()["hooks"]["enabled"])  # 不代改全局开关

    def test_bad_json_no_write(self):
        self.target.write_text("{not json", encoding="utf-8")
        before = self.target.read_bytes()
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertTrue(errors)
        self.assertEqual(self.target.read_bytes(), before)

    def test_apply_missing_target_creates_without_backup(self):
        """F4：目标 config.json 不存在——首次 setup 可创建（含父目录），
        不做备份、不再抛 FileNotFoundError/残留空备份。"""
        self.target.unlink()
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(errors, [])
        data = self.read_target()
        stop = [h for g in data["hooks"]["events"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(stop), 1)
        self.assertTrue(stop[0]["enabled"])
        backups = Path(self.ctx["backups"])
        self.assertFalse(backups.exists() and any(backups.iterdir()))  # 无旧文件则无备份
        self.assertTrue(any("已新建" in m for m in messages))
        # 幂等：再次 setup 不变
        before = self.target.read_bytes()
        messages2, errors2 = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(errors2, [])
        self.assertEqual(self.target.read_bytes(), before)

    def test_apply_hooks_null_structured_error(self):
        """F4：hooks/events 为 null 等坏结构——结构化错误，不 traceback，文件原样。"""
        for broken in ({"hooks": None},
                       {"hooks": {"enabled": True, "events": None}},
                       {"hooks": {"enabled": True, "events": {"Stop": None}}},
                       {"hooks": []}):
            self.write_target(broken)
            before = self.target.read_bytes()
            messages, errors = mail.apply_setup(self.cfg(), self.ctx)
            self.assertEqual(messages, [])
            self.assertTrue(errors, str(broken))
            self.assertEqual(self.target.read_bytes(), before, str(broken))
            # DryRun 共享结构验证：同样不 traceback、不改文件
            info = mail.inspect_setup(self.cfg(), self.ctx)
            lines = mail.plan_setup_lines(info, True)
            self.assertTrue(lines)

    def test_dry_run_reports_structural_errors_with_content(self):
        """R2-2 反例：inspect_setup 对坏结构给出明确 parse_error（含原 TypeError
        反例 Stop 组 hooks=null），预览行带“拒绝写入”，与 apply 错误同源、零副作用。"""
        cases = ({"hooks": None},
                 {"hooks": {"enabled": True, "events": None}},
                 {"hooks": {"enabled": True, "events": {"Stop": None}}},
                 {"hooks": {"enabled": True, "events": {"Stop": [{"hooks": None}]}}},
                 {"hooks": {"enabled": True, "events": {"Stop": {"hooks": []}}}},
                 {"hooks": {"enabled": True, "events": {"Stop": [{"hooks": [None]}]}}},
                 {"hooks": []})
        for broken in cases:
            with self.subTest(case=str(broken)):
                self.write_target(broken)
                before = self.target.read_bytes()
                info = mail.inspect_setup(self.cfg(), self.ctx)
                self.assertTrue(info["parse_error"], str(broken))
                joined = "\n".join(mail.plan_setup_lines(info, True))
                self.assertIn("拒绝写入", joined)
                self.assertIn(info["parse_error"], joined)
                messages, errors = mail.apply_setup(self.cfg(), self.ctx)
                self.assertEqual(messages, [])
                self.assertIn(info["parse_error"], "\n".join(errors))  # 同一验证器
                self.assertEqual(self.target.read_bytes(), before)

    def test_dry_run_missing_keys_are_legal(self):
        """R2-2：缺省键合法（无 hooks/events/Stop/组内 hooks 都算空），不误报。"""
        for legal in ({}, {"hooks": {}},
                      {"hooks": {"enabled": True, "events": {}}},
                      {"hooks": {"enabled": True, "events": {"Stop": [{}]}}}):
            with self.subTest(case=str(legal)):
                self.write_target(legal)
                info = mail.inspect_setup(self.cfg(), self.ctx)
                self.assertIsNone(info["parse_error"], str(legal))
                joined = "\n".join(mail.plan_setup_lines(info, True))
                self.assertNotIn("拒绝写入", joined)

    def test_unreadable_target_rejected_without_write(self):
        """R2-2：目标不可读（替换为目录）→ 明确诊断、拒绝写入、原状保持。"""
        self.target.unlink()
        self.target.mkdir()
        info = mail.inspect_setup(self.cfg(), self.ctx)
        self.assertIn("无法读取", info["parse_error"] or "")
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertEqual(messages, [])
        self.assertTrue(any("无法读取" in e for e in errors))
        self.assertTrue(self.target.is_dir())

    def test_apply_write_failure_structured(self):
        """F4：写入失败转结构化错误（目标父目录换成文件，无法创建）。"""
        self.target.unlink()
        self.target.parent.rmdir()
        self.target.parent.write_text("not a dir", encoding="utf-8")
        messages, errors = mail.apply_setup(self.cfg(), self.ctx)
        self.assertTrue(errors)
        self.assertIn("写入失败", "\n".join(errors))
        self.assertFalse(any("Traceback" in e for e in errors))

    def test_identity_shapes(self):
        mine = mail.identity_from_mail_args(
            ["-X", "utf8", str(self.ctx["bootstrap"]), "--hq-config", str(self.cfgpath),
             "mail", "observe"])
        self.assertEqual(mine, (common.normalize_path(self.ctx["bootstrap"]),
                                common.normalize_path(self.cfgpath)))
        self.assertIsNone(mail.identity_from_mail_args(mine and [
            "-X", "utf8", str(self.ctx["bootstrap"]), "--hq-config", str(self.cfgpath),
            "mail", "observe", "extra"]))
        other = mail.identity_from_mail_args(
            ["-X", "utf8", str(self.ctx["bootstrap"]), "--hq-config",
             str(self.base / "other.toml"), "mail", "observe"])
        self.assertNotEqual(mine, other)
        self.assertEqual(mail._identity_from_gate_args(self.gate_args),
                         (common.normalize_path(self.ctx["bootstrap"]),
                          common.normalize_path(self.cfgpath)))
        self.assertIsNone(mail._identity_from_gate_args(["-X", "utf8", "b", "--hq-config",
                                                         "c", "mail", "tick"]))

    def test_tick_argv_absolute_and_password_free(self):
        argv = mail.tick_argv(self.ctx)
        self.assertEqual(argv[-2:], ["mail", "tick"])
        for part in (argv[0], argv[3], argv[5]):     # python / bootstrap / hq-config 值
            self.assertTrue(Path(part).is_absolute(), part)
        self.assertNotIn("PASSWORD", " ".join(argv))


class CliSurface(unittest.TestCase):
    def test_mail_subparser_routes(self):
        from hqlib.cli import build_parser

        for action in ("observe", "tick", "status", "test", "setup"):
            args = build_parser().parse_args(["mail", action])
            self.assertEqual(args.action, action)
        args = build_parser().parse_args(["mail", "setup", "--dry-run"])
        self.assertTrue(args.dry_run)


class CliGuard(Base):
    """F5：--dry-run 只允许 setup；tick 的失败/未知在 CLI 结果中可诊断。"""

    def _run_cmd(self, action, dry_run=False):
        import contextlib
        import io

        from hqlib.cli import cmd_mail

        ns = type("NS", (), {"action": action, "dry_run": dry_run})()
        out, err, rc = io.StringIO(), io.StringIO(), None
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = cmd_mail(ns)
        finally:
            sys.stdout, sys.stderr = old
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_rejected_for_non_setup_before_any_send(self):
        for action in ("tick", "test", "status", "observe"):
            rc, out, err = self._run_cmd(action, dry_run=True)
            self.assertEqual(rc, 2, action)
            self.assertIn("只支持 mail setup", err)
        self.assertEqual(self.sent, [])

    def test_dry_run_allowed_for_setup(self):
        rc, out, err = self._run_cmd("setup", dry_run=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("DryRun", out)

    def test_setup_dry_run_bad_structure_nonzero_no_side_effects(self):
        """R2-2：CLI 预览对坏结构退出码非零、无 traceback、无写入/备份/发送副作用。"""
        home = self.base / "home2"
        zdir = home / ".zcode" / "cli"
        zdir.mkdir(parents=True)
        target = zdir / "config.json"
        target.write_text(json.dumps({"hooks": None}), encoding="utf-8")
        ctx = {"python": "py.exe", "bootstrap": self.base / "bin" / "hq.py",
               "hq_config": self.cfgpath, "home": home,
               "backups": self.base / "backups2"}
        real_hook_ctx = mail.hook_ctx
        mail.hook_ctx = lambda: ctx
        before = target.read_bytes()
        try:
            rc, out, err = self._run_cmd("setup", dry_run=True)
        finally:
            mail.hook_ctx = real_hook_ctx
        self.assertEqual(rc, 1, err)
        self.assertNotIn("Traceback", out + err)
        self.assertIn("拒绝写入", out)
        self.assertIn("预览发现问题", err)
        self.assertEqual(target.read_bytes(), before)      # 目标文件原样
        self.assertFalse(Path(ctx["backups"]).exists())    # 无备份目录/文件
        self.assertEqual(self.sent, [])                    # 不触发送

    def test_tick_exit_code_marks_failed_and_uncertain(self):
        """tick 出现 failed/uncertain → CLI 退出码非 0（计划任务不能只看到成功）。"""
        # CLI tick 用真实时钟：种当前应补截止点那一期（R2-1 口径下其余待发会被淘汰）
        key = f"daily:{mail._latest_cutoff(datetime.now(timezone.utc)):%Y%m%d}"
        self.seed_delivery(key, kind="daily", created=bj(2026, 10, 2, 19, 1))
        real_send = mail.send_mail
        try:
            def failing(mcfg, subject, body, mid):
                raise mail.MailPermanentError("认证失败")

            mail.send_mail = failing
            rc, out, err = self._run_cmd("tick")
            self.assertEqual(rc, 1, err)
            self.assertIn("失败", err)
            self.assertEqual(self.delivery(key)["state"], "failed")

            db = self.db()
            try:
                db.execute("UPDATE deliveries SET state='pending' WHERE key=?", (key,))
            finally:
                db.close()

            def unknown(mcfg, subject, body, mid):
                raise mail.MailUnknownError("连接中断")

            mail.send_mail = unknown
            rc, out, err = self._run_cmd("tick")
            self.assertEqual(rc, 1, err)
            self.assertIn("待核对", err)
            self.assertEqual(self.delivery(key)["state"], "uncertain")
        finally:
            mail.send_mail = real_send

    def test_tick_exit_code_zero_when_all_sent(self):
        key = f"daily:{mail._latest_cutoff(datetime.now(timezone.utc)):%Y%m%d}"
        self.seed_delivery(key, kind="daily", created=bj(2026, 10, 2, 19, 0))
        real_send = mail.send_mail
        mail.send_mail = lambda *a, **k: None       # 隔离：绝不触网
        try:
            rc, out, err = self._run_cmd("tick")
        finally:
            mail.send_mail = real_send
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(self.delivery(key)["state"], "sent")

    def test_status_shows_sending_row(self):
        """F2：遗留 sending 行在 status 明确展示为发送中/结果待核对。"""
        self.seed_delivery("daily:20261001", kind="daily", state="sending",
                           created=bj(2026, 10, 1, 19, 0))
        lines = mail.status_lines()
        text = "\n".join(lines)
        self.assertIn("sending", text)
        self.assertIn("发送中/结果待核对", text)
        self.assertIn("不回收", text)


if __name__ == "__main__":
    unittest.main()
