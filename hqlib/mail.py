"""hq mail：ZCode 本轮执行结束提醒 + 每日 19:00 日报（人工接力路线）。

方案：docs/plans/2026-10-02-mail-degraded.md（Supervisor 重定版）。
边界：一个 mail 模块、一个 SQLite（HQ_HOME/.hq/mail.sqlite3：events/deliveries
两张业务表＋meta 一行监测起点）、一个 Windows 计划任务；没有通用队列租约、
多级自动重试、doctor 扩展。

- observe（Stop 观察入口）：只把候选轮次写进本地 SQLite；无网络、不阻挡、
  不启动 AI，任何输入/IO 错误都输出 {} 并退出 0。
- 结束确认只认本机 ZCode 桌面日志里结构化 kind=turn-ended 的行，按
  session+turn 精确匹配（D1 样本验证过的宿主终态）；outcome=completed 才算
  “本轮跑完”，其他 outcome 如实记为失败/中断，只进日报。
- 通知窗口为北京时间 [19:00,24:00) ∪ [00:00,10:00)：每轮按自己的完成时刻
  计算独立的“次日 10:00”截止；入队与每封实际发送前都重新校验当前时刻，
  过期一律 skipped 只进日报，不在下一晚补发。
- 日报只读已有记录与项目资料（STATUS.md / .hq/verify.json），不跑验收、
  tokei、代码指纹或模型；缺资料标“未记录”，单项目坏资料不挡其他通知。
- sending 状态属于仍可能存活的发送进程：其他 tick 不回收、不判退出、
  不自动重发（无租约/守护进程）；发送结果不明只标 uncertain 待人工核对。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import ssl
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from . import common

# ---------------------------------------------------------------- 常量与时间

PREVIEW_LIMIT = 300          # 完成邮件回复预览上限（字），避免发送整段会话
SOCKET_TIMEOUT = 15          # 单次 socket 操作上限，防计划轮次无限阻塞
OBSERVE_TIMEOUT_MS = 30000   # 观察 hook 只做本地 SQLite 写入，远小于闸门预算
LOG_TAG = "[hq mail]"

try:
    from zoneinfo import ZoneInfo
    BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # Windows 无 IANA 数据库时：本需求只固定 Asia/Shanghai
    BEIJING = timezone(timedelta(hours=8), "UTC+08:00")

NIGHT_START = dtime(19, 0)   # 北京时间 [19:00,24:00) ∪ [00:00,10:00)
NIGHT_END = dtime(10, 0)


def to_beijing(dt: datetime) -> datetime:
    return dt.astimezone(BEIJING)


def in_night_window(dt: datetime) -> bool:
    """完成/发送资格口径：19:00（含）到次日 10:00（不含）。"""
    t = to_beijing(dt).time()
    return t >= NIGHT_START or t < NIGHT_END


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bj_str(dt: datetime) -> str:
    return to_beijing(dt).strftime("%Y-%m-%d %H:%M")


def round_deadline(confirmed: datetime) -> datetime:
    """该轮完成对应的独立发送截止：所在夜间窗口的“次日 10:00”（北京时间）。

    完成在 [00:00,10:00) → 当天 10:00；完成在 [19:00,24:00) → 次日 10:00。
    每轮各算各的，过期一律 skipped，不在下一晚补发（复审 F1）。
    """
    b = to_beijing(confirmed)
    day = b.date() if b.hour < NIGHT_END.hour else b.date() + timedelta(days=1)
    return datetime.combine(day, NIGHT_END, tzinfo=BEIJING).astimezone(timezone.utc)


# ---------------------------------------------------------------- 配置


class MailConfigError(Exception):
    """[mail] 配置缺失/非法：tick/test 在任何网络访问前拒绝执行，不猜默认 SMTP。"""


_STR_FIELDS = ("smtp_host", "security", "username", "password_env", "from_addr", "to_addr")
# 单个地址：一个 @，不含空白/逗号/分号/引号（\s 已覆盖 CRLF）
_ADDR_RE = re.compile(r"^[^@\s,;\"']+@[^@\s,;\"']+$")


def _validate_addr(name: str, value: str) -> None:
    if not _ADDR_RE.match(value or ""):
        raise MailConfigError(
            f"[mail] {name} 必须是单个邮件地址（不含逗号/空格/CRLF），收到 {value!r}")


def load_mail_config(cfg: dict) -> dict:
    """读取并校验 [mail]。密码只记 password_env 名，绝不读值、不打印、不落盘。

    全部校验（字段类型、端口范围、单收件地址、CRLF）在联网前完成；
    不合法一律 MailConfigError（可控诊断），不出 traceback。
    """
    raw = cfg.get("mail")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise MailConfigError("hq.toml 的 [mail] 应为 TOML 表")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise MailConfigError(f"[mail] enabled 应为布尔值，收到 {enabled!r}")
    out = {"enabled": enabled, "smtp_port": 0}
    for key in _STR_FIELDS:
        v = raw.get(key)
        if v is not None and not isinstance(v, str):
            raise MailConfigError(f"[mail] {key} 应为字符串，收到 {v!r}")
        out[key] = (v or "").strip() if key == "security" else (v or "")
    if not out["enabled"]:
        return out
    if not out["smtp_host"]:
        raise MailConfigError("[mail] enabled=true 但缺 smtp_host")
    port = raw.get("smtp_port")
    if port is None or port == "":
        port = 0
    if isinstance(port, bool) or not isinstance(port, int):
        raise MailConfigError(f"[mail] smtp_port 应为整数，收到 {port!r}")
    if port == 0:
        port = 465 if out["security"] == "ssl" else 587
    if not 1 <= port <= 65535:
        raise MailConfigError(f"[mail] smtp_port 应在 1-65535，收到 {port}")
    out["smtp_port"] = port
    if out["security"] not in ("ssl", "starttls"):
        raise MailConfigError(f"[mail] security 只支持 ssl|starttls，收到 {out['security']!r}")
    for key in ("from_addr", "to_addr", "password_env"):
        if not out[key]:
            raise MailConfigError(f"[mail] enabled=true 但缺 {key}")
    _validate_addr("from_addr", out["from_addr"])
    _validate_addr("to_addr", out["to_addr"])
    return out


# ---------------------------------------------------------------- SQLite

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    session          TEXT NOT NULL,
    turn             TEXT NOT NULL,
    project          TEXT NOT NULL,
    observed_at_utc  TEXT NOT NULL,
    confirmed_at_utc TEXT,
    outcome          TEXT NOT NULL DEFAULT 'pending',
    reply_summary    TEXT,
    notified         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session, turn)
);
CREATE TABLE IF NOT EXISTS deliveries (
    key            TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,
    subject        TEXT NOT NULL,
    body           TEXT NOT NULL,
    state          TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    sent_at_utc    TEXT,
    error          TEXT,
    deadline_utc   TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def db_path() -> Path:
    home = os.environ.get("HQ_HOME") or common.HQ_HOME
    return Path(home) / ".hq" / "mail.sqlite3"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=1, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r["name"] for r in db.execute("PRAGMA table_info(deliveries)")}
    if "deadline_utc" not in cols:  # 旧库原地迁移：每轮独立截止列
        db.execute("ALTER TABLE deliveries ADD COLUMN deadline_utc TEXT")
    return db


def _txn(db: sqlite3.Connection):
    """小事务收口：异常回滚并按 sqlite3 原样抛出。"""
    class _Txn:
        def __enter__(self):
            db.execute("BEGIN IMMEDIATE")
            return db

        def __exit__(self, et, ev, tb):
            if et is None:
                db.execute("COMMIT")
            else:
                try:
                    db.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            return False
    return _Txn()


def round_key(session: str, turn: str) -> str:
    return f"round:{session}:{turn}"


def message_id_for(key: str, from_addr: str = "") -> str:
    """固定派生（同 key 同 ID）：只用于追踪，不宣称收件端幂等。"""
    domain = from_addr.rsplit("@", 1)[-1] if "@" in from_addr else "hq.local"
    return f"<hq-mail-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}@{domain}>"


# ---------------------------------------------------------------- observe（Stop 候选登记）


def match_project(cwd, cfg: dict) -> dict | None:
    """cwd 向上逐层匹配 hq.toml 显式登记的项目路径；未登记 → None。"""
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    registered = {}
    for p in cfg.get("projects", []):
        if isinstance(p, dict) and p.get("path"):
            registered[normalized(p["path"])] = p
    if not registered:
        return None
    try:
        cur = normalized(cwd)
    except OSError:
        return None
    while True:
        if cur in registered:
            return registered[cur]
        if cur == cur.parent:
            return None
        cur = cur.parent


def normalized(p) -> Path:
    """与登记/hook 共用同一条规范路径规则（common.normalize_path）。"""
    return common.normalize_path(p)


def extract_preview(payload: dict, limit: int = PREVIEW_LIMIT) -> str | None:
    for k in ("responsePreview", "last_assistant_message", "responseText"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if len(v) <= limit:
                return v
            return v[:limit] + f"…（截断，原文 {len(v)} 字）"
    return None


def record_stop(payload, cfg: dict, now: datetime, db: sqlite3.Connection | None = None) -> bool:
    """把一条 ZCode Stop payload 登记为候选轮次。缺字段/未登记 → False。

    只接受 JSON object；缺会话/轮次字段不推测、不认领（由调用方记诊断）。
    重复 Stop（同 session+turn）只保留首个观察时间，回复预览更新为最新。
    """
    if not isinstance(payload, dict):
        raise TypeError(f"payload 应为 JSON object，收到 {type(payload).__name__}")
    session = _first_str(payload, "session_id", "sessionId")
    turn = _first_str(payload, "turn_id", "turnId")
    if not session or not turn:
        raise ValueError("payload 缺 session/turn 字段，不推测、不认领")
    proj = match_project(payload.get("cwd"), cfg)
    if proj is None:
        return False
    own_db = db is None
    db = db or connect()
    try:
        with _txn(db):
            db.execute(
                "INSERT INTO events(session,turn,project,observed_at_utc,outcome,reply_summary,notified)"
                " VALUES(?,?,?,?, 'pending', ?, 0)"
                " ON CONFLICT(session,turn) DO UPDATE SET"
                " reply_summary=COALESCE(excluded.reply_summary, reply_summary)",
                (session, turn, str(normalized(proj["path"])), utc_iso(now),
                 extract_preview(payload)))
    finally:
        if own_db:
            db.close()
    return True


def _first_str(payload: dict, *keys) -> str | None:
    for k in keys:
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def cmd_observe(stdin=None, stdout=None) -> int:
    """`hq mail observe` 机器入口：stdout 恒为 {}，退出码恒 0（fail-open）。"""
    stdin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout

    def emit() -> None:
        out.write("{}\n")
        out.flush()

    if getattr(stdin, "isatty", lambda: False)():
        print("hq mail observe 是机器命令（Stop hook 通过 stdin 供给 JSON）。", file=sys.stderr)
        emit()
        return 0
    try:
        raw = stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        record_stop(payload, common.load_config(), datetime.now(timezone.utc))
    except Exception as exc:  # 统一 fail-open：诊断走 stderr，绝不污染 stdout
        first = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
        print(f"{LOG_TAG}:observe fail-open {type(exc).__name__}: {first}", file=sys.stderr)
    emit()
    return 0


# ---------------------------------------------------------------- 宿主日志确认

_TS_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\]")


def default_log_dir() -> Path:
    return Path(os.environ.get("ZCODE_LOG_DIR") or Path.home() / ".zcode" / "v2" / "logs")


def iter_turn_ended(path: Path):
    """只解析结构化 kind=turn-ended 行；前缀时间戳按系统时区（桌面日志口径）
    解析为 aware UTC。结构不符（含缺字段/坏 JSON）一律跳过——等同“版本字段
    不符”，对应候选保持待确认。"""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"kind"' not in line or "turn-ended" not in line:
                continue
            m = _TS_RE.match(line)
            i = line.find('{"')
            if m is None or i < 0:
                continue
            try:
                data = json.loads(line[i:].rstrip())
            except ValueError:
                continue
            if not isinstance(data, dict) or data.get("kind") != "turn-ended":
                continue
            sid, tid, outcome = data.get("sessionId"), data.get("turnId"), data.get("outcome")
            if not all(isinstance(x, str) and x.strip() for x in (sid, tid, outcome)):
                continue
            try:
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f").astimezone()
            except ValueError:
                continue
            yield {"at_utc": at.astimezone(timezone.utc), "session": sid.strip(),
                   "turn": tid.strip(), "outcome": outcome.strip()}


def confirm_rounds(candidates: list[dict], log_paths: list[Path]) -> dict:
    """在宿主日志里为候选找 turn-ended 终态。

    candidates: [{session, turn, observed(aware utc)}]；返回
    {(session, turn): {"at_utc", "outcome"}}，只含确认成功的。日志不可读、
    无匹配行、或终态时间早于观察（关联不明）→ 不出现，保持待确认。
    每个文件每 tick 只读一次；同键多条以最早一条为准。
    """
    index: dict[tuple, dict] = {}
    for p in log_paths:
        try:
            rows = list(iter_turn_ended(Path(p)))
        except OSError as exc:
            print(f"{LOG_TAG} 日志不可读，相关候选保持待确认: "
                  f"{Path(p).name}: {exc}", file=sys.stderr)
            continue
        for r in rows:
            index.setdefault((r["session"], r["turn"]), r)
    out: dict[tuple, dict] = {}
    for c in candidates:
        hit = index.get((c["session"], c["turn"]))
        if hit is None:
            continue
        if hit["at_utc"] < c["observed"]:
            print(f"{LOG_TAG} 终态早于观察（关联不明），保持待确认: "
                  f"{c['session'][:8]}/{c['turn'][:8]}", file=sys.stderr)
            continue
        out[(c["session"], c["turn"])] = hit
    return out


# 单个未知候选的固定日期预算：观察日起连续 MAX_LOG_SCAN_DAYS+1（=8）个宿主
# 日期（含起点）。这是有界化选择——不随候选年龄扩大历史范围；但窗口内文件
# 每次 tick 仍会重读（没有增量/停扫机制，不声称满 7 天后停止 IO）。窗口外
# 日期不再读取，候选保持 pending 且在 status/日报可见，不判完成（复审 F7/R2-3）。
MAX_LOG_SCAN_DAYS = 7


def log_paths_for(candidates: list[dict], now: datetime, log_dir: Path | None = None,
                  tz=None) -> list[Path]:
    """候选涉及的宿主日志文件；每文件最多一次。

    日志文件名/前缀日期按宿主系统时区（tz=None 时用本机时区；北京时区只控制
    通知规则，复审 F7）；tz 可注入以便跨时区测试。每个候选最多回看观察日起
    连续 8 个宿主日期（固定预算，不随候选年龄扩大），之后保持待确认且可见。
    """
    base = Path(log_dir) if log_dir else default_log_dir()
    to_local = (lambda dt: dt.astimezone(tz)) if tz is not None else (lambda dt: dt.astimezone())
    dates: set = set()
    for c in candidates:
        start = to_local(c["observed"]).date()
        end = min(start + timedelta(days=MAX_LOG_SCAN_DAYS), to_local(now).date())
        d = start
        while d <= max(end, start):
            dates.add(d)
            d += timedelta(days=1)
    return [base / f"{d:%Y-%m-%d}.log" for d in sorted(dates)]


# ---------------------------------------------------------------- 日报（只读拼装）


def _read_status_section(root: Path) -> str:
    from .status import parse_status

    st = parse_status(root / common.STATUS_FILE)
    if st is None:
        return "- STATUS：未记录（缺 STATUS.md）"
    lines = [f"- STATUS 摘要：{st['summary'] or '（无）'}（state={st['state']}，updated={st['updated'] or '未知'}）"]
    if st["blocked"]:
        lines += [f"  - ⛔ {i['text']}" for i in st["blocked"][:5]]
    else:
        lines.append("  - ⛔ 阻塞：无")
    if st["next"]:
        lines += [f"  - ▶ {i['text']}" for i in st["next"][:5]]
    else:
        lines.append("  - ▶ 下一步：未记录")
    snap = datetime.fromtimestamp(st["mtime"], tz=timezone.utc)
    lines.append(f"  - snapshot_at：{bj_str(snap)}（STATUS.md）")
    return "\n".join(lines)


def _read_verify_section(root: Path) -> str:
    """只读旧验收快照 .hq/verify.json 的计数/时间格式（mail-only 清理：不再
    import verify/fingerprint，不算代码指纹、不执行 ACCEPTANCE——verify 模块
    已删除，这只是对历史 JSON 的只读兼容）。"""
    path = Path(root) / ".hq" / "verify.json"
    data = common.read_json(path, {}) or {}
    checks = data.get("checks") or {}
    last = data.get("last_run") or {}
    if not checks and not last:
        return "- 机器验收：未记录（无 .hq/verify.json）"
    total = len(checks)
    passed = sum(1 for v in checks.values() if v.get("ok"))
    failed = sum(1 for v in checks.values() if not v.get("ok") and not v.get("not_run"))
    not_run = sum(1 for v in checks.values() if v.get("not_run"))
    snap = last.get("at") or (datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds") if path.exists() else "")
    return ("- 机器验收：{p}/{t} 通过，失败 {f}，未运行 {n}（上次 {at}，tier {tier}；"
            "snapshot_at {snap}）").format(p=passed, t=total, f=failed,
                                          n=not_run, at=last.get("at") or "未知",
                                          tier=last.get("tier") or "未知", snap=snap or "未知")


def build_daily(cfg: dict, events: list[dict], cutoff: datetime, now: datetime,
                coverage_start: datetime | None, missing_dailies: list[str],
                undelivered_dailies: list[str] | None = None) -> tuple[str, str]:
    """日报正文。窗口 [cutoff-24h, cutoff)（北京时间）；只读 events 与项目资料。

    coverage_start 是“监测起点”（首次 tick 与最早观察的较早者）：截止点不早于
    它才生成；单项目资料读取失败只注明，不挡其他项目与其他通知（复审 F3）。
    """
    d = to_beijing(cutoff)
    win_start = cutoff - timedelta(hours=24)
    rounds = [e for e in events if e.get("confirmed") and win_start <= e["confirmed"] < cutoff]
    pend = [e for e in events if not e.get("confirmed")]
    head = [f"hq 日报 · 窗口 {to_beijing(win_start):%Y-%m-%d %H:%M} → {d:%Y-%m-%d %H:%M}（北京时间）"]
    if to_beijing(now) - d > timedelta(minutes=30):
        head.append(f"（本日报延迟生成：实际 {bj_str(now)}；只补最近一期，更早日期不逐日补发）")
    head.append(f"监测起点：{bj_str(coverage_start) if coverage_start else '尚无记录'}"
                "（本日报只覆盖监测起点之后，此前不存在记录）")
    if missing_dailies:
        head.append(f"漏报的日报截止点：{', '.join(missing_dailies)}（只列出，不逐日补发）")
    if undelivered_dailies:
        head.append(f"投递未完成的日报截止点：{', '.join(undelivered_dailies)}"
                    "（状态如实标注，不据此自动重发）")

    if rounds:
        head.append(f"\n## 已确认轮次（本窗口 {len(rounds)} 个）")
        for e in sorted(rounds, key=lambda x: x["confirmed"]):
            state = {"completed": "完成"}.get(e["outcome"], f"{e['outcome']}（失败/中断，不冒充完成）")
            mail_state = e.get("mail")
            note = {"sent": "已发完成通知",
                    "pending": "通知待发",
                    "sending": "通知发送中（结果待核对，不自动重发）",
                    "failed": "通知发送失败（未接受）",
                    "uncertain": "通知结果待核对（不自动重发）",
                    "skipped": "通知已过窗（未发，只进日报）"}.get(mail_state)
            if note is None:
                note = "无完成通知（窗口外完成）" if e["outcome"] == "completed" else "无完成通知"
            head.append(f"- {Path(e['project']).name}：{state} @ {bj_str(e['confirmed'])}"
                        f"（session {e['session'][:8]} / turn {e['turn'][:8]}；{note}）")
    else:
        head.append("\n## 已确认轮次\n\n本窗口无已记录轮次。")
    head.append(f"待确认候选（截至本报告生成时的累计，不限本窗口）：{len(pend)} 个"
                "（Stop 已登记，宿主终态未匹配；不冒充完成）")

    head.append("\n## 项目现状（读取时点快照，只读）")
    for p in cfg.get("projects", []):
        root = Path(p["path"])
        head.append(f"\n### {root.name}（{root}）")
        try:
            head.append(_read_status_section(root))
            head.append(_read_verify_section(root))
        except Exception as exc:  # 单项目坏资料只注明，不挡其他项目/通知
            first = str(exc).strip().splitlines()[0][:120] if str(exc).strip() else ""
            head.append(f"- 项目资料读取失败（不影响其他项目与其他通知）: "
                        f"{type(exc).__name__}: {first}")
    head.append("\n—— 本日报由已有记录只读拼装：不重跑验收/指纹/模型，"
                "不操纵会话；“完成”仅指该轮执行结束，不代表整个任务完成。")
    return f"[hq] 日报 · 截至 {d:%Y-%m-%d} 19:00（北京时间）", "\n".join(head)


# ---------------------------------------------------------------- SMTP


class MailPermanentError(Exception):
    """确定未接受（配置错误/连接失败/认证或收件人拒绝）：状态 failed，不重发。"""


class MailUnknownError(Exception):
    """结果不明（会话中断/超时/进程退出）：状态 uncertain，待核对，不自动重发。"""


def _reject_crlf(name: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise MailPermanentError(f"{name} 含 CRLF，拒绝发送")


def _smtp_factory(mcfg: dict):
    ctx = ssl.create_default_context()

    def make():
        host, port = mcfg["smtp_host"], mcfg["smtp_port"]
        if mcfg["security"] == "ssl":
            return smtplib.SMTP_SSL(host, port, timeout=SOCKET_TIMEOUT, context=ctx)
        return smtplib.SMTP(host, port, timeout=SOCKET_TIMEOUT)

    return make, ctx


def send_mail(settings: dict, subject: str, body: str, message_id: str,
              factory=None) -> None:
    """发一封纯文本 UTF-8 邮件。SSL/STARTTLS 都强制证书校验、不降级。

    确定未接受 → MailPermanentError；结果不明 → MailUnknownError。
    SMTP 接受后的 QUIT 失败不推翻“已接受”（不重新判失败）。
    """
    _reject_crlf("subject", subject)
    _reject_crlf("message_id", message_id)
    password = None
    if settings.get("password_env"):
        password = os.environ.get(settings["password_env"])
        if not password:
            raise MailPermanentError(
                f"环境变量 {settings['password_env']} 未设置（密码不落盘，无法发送）")
    msg = EmailMessage()
    msg["From"] = settings["from_addr"]
    msg["To"] = settings["to_addr"]
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body, charset="utf-8")

    make, ctx = factory() if factory else _smtp_factory(settings)
    smtp = None
    try:
        try:
            smtp = make()
            smtp.ehlo()
            if settings["security"] == "starttls":
                smtp.starttls(context=ctx)
                smtp.ehlo()
            if settings.get("username") and password is not None:
                smtp.login(settings["username"], password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailPermanentError(
                f"认证失败: {exc.smtp_code} {exc.smtp_error!s}"[:200]) from None
        except Exception as exc:
            # 连接/TLS/握手阶段失败：消息确定未交到服务器 → 确定未接受。
            raise MailPermanentError(f"{type(exc).__name__}: {exc}"[:200]) from None
        try:
            refused = smtp.send_message(msg)
            if refused:
                raise MailPermanentError(f"收件人被拒: {sorted(refused)}")
        except (MailPermanentError, MailUnknownError):
            raise
        except smtplib.SMTPRecipientsRefused as exc:
            raise MailPermanentError(f"收件人被拒: {sorted(exc.recipients)}"[:200]) from None
        except smtplib.SMTPResponseException as exc:
            # 服务器给出明确否定应答（SMTPDataError/SenderRefused 等 4xx/5xx）：
            # 消息确定未被接受 → failed。中断/超时才走 unknown。
            raise MailPermanentError(
                f"SMTP 明确拒绝: {exc.smtp_code} {exc.smtp_error!s}"[:200]) from None
        except (smtplib.SMTPServerDisconnected, OSError, ssl.SSLError, ValueError) as exc:
            # 会话建立后的中断/超时无法区分 DATA 是否已被接受 → 结果不明。
            raise MailUnknownError(f"{type(exc).__name__}: {exc}"[:200]) from None
        except Exception as exc:
            raise MailUnknownError(f"{type(exc).__name__}: {exc}"[:200]) from None
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass  # 已接受后的 QUIT 失败不重新判失败


# ---------------------------------------------------------------- tick


def _round_body(e: dict) -> tuple[str, str]:
    name = Path(e["project"]).name
    subject = f"[hq] 本轮执行结束 · {name}"
    body = (
        f"项目：{name}（{e['project']}）\n"
        f"本轮执行结束：{bj_str(e['confirmed'])}（outcome={e['outcome']}）\n"
        f"会话 {e['session'][:8]}… · 轮次 {e['turn'][:8]}…（ZCode 主轮次）\n"
        "状态：本轮已跑完（不代表整个任务完成、不代表验收通过）\n"
        f"\n回复预览（≤{PREVIEW_LIMIT}字）：\n{e.get('reply_summary') or '（Stop 未携带回复预览）'}\n"
        "\n—— hq 自动提醒。是否人工接力由你决定；Codex 决策/复核轮不在通知范围。")
    return subject, body


def _claim_pending(db: sqlite3.Connection) -> list[sqlite3.Row]:
    """原子领取：条件更新 pending→sending，网络 IO 在事务外。"""
    with _txn(db):
        rows = db.execute(
            "SELECT key,subject,body,deadline_utc FROM deliveries WHERE state='pending'"
            " ORDER BY created_at_utc, key").fetchall()
        for r in rows:
            db.execute("UPDATE deliveries SET state='sending' WHERE key=? AND state='pending'",
                       (r["key"],))
    return rows


def _deliver(db: sqlite3.Connection, mcfg: dict, sender, clock=None) -> list[str]:
    """逐封发送。每封实际发送前用 clock() 重新校验该轮截止（复审 F1：
    不能只用 tick 起点的固定时刻，长队列也可能跨过 10:00）。"""
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    rows = _claim_pending(db)
    lines = []
    for r in rows:
        key = r["key"]
        deadline = from_iso(r["deadline_utc"]) if r["deadline_utc"] else None
        if deadline is not None and clock() >= deadline:
            with _txn(db):
                db.execute("UPDATE deliveries SET state='skipped', error=? WHERE key=?",
                           ("已过该通知的次日 10:00 截止（只进日报，不积压）", key))
            lines.append(f"{key}: ⏭ 已过该轮次日 10:00 截止，跳过发送（只进日报）")
            continue
        mid = message_id_for(key, mcfg["from_addr"])
        try:
            if sender is not None:
                sender(mcfg, r["subject"], r["body"], mid)
            else:
                send_mail(mcfg, r["subject"], r["body"], mid)
            state, err = "sent", None
        except MailPermanentError as exc:
            state, err = "failed", str(exc)
        except MailUnknownError as exc:
            state, err = "uncertain", str(exc)
        except Exception as exc:  # 未预期异常同样按“结果不明”处理，不自动重发
            state, err = "uncertain", f"{type(exc).__name__}: {exc}"[:200]
        with _txn(db):
            # sent_at_utc 记录实际发送接受时刻（同一注入时钟路径），非 tick 起点
            # （复审 R2-3：长队列下时间记录才准确）；发送前截止校验保持不变。
            db.execute("UPDATE deliveries SET state=?, sent_at_utc=?, error=? WHERE key=?",
                       (state, utc_iso(clock()) if state == "sent" else None, err, key))
        mark = {"sent": "✅ 已发送", "failed": f"❌ 失败（未接受）: {err}",
                "uncertain": f"⚠ 结果待核对（不自动重发）: {err}"}[state]
        lines.append(f"{key}: {mark}")
    return lines


def _latest_cutoff(now: datetime) -> datetime:
    b = to_beijing(now).replace(hour=19, minute=0, second=0, microsecond=0)
    return b if to_beijing(now) >= b else b - timedelta(days=1)


def _known_start(db: sqlite3.Connection) -> datetime | None:
    """监测起点：首次 tick 时刻与最早观察时刻的较早者（复审 F3：
    截止点不早于它才生成日报——不生成“启用前报告”）。"""
    vals = []
    row = db.execute("SELECT value FROM meta WHERE key='first_tick_utc'").fetchone()
    if row:
        vals.append(from_iso(row["value"]))
    row = db.execute("SELECT MIN(observed_at_utc) AS c FROM events").fetchone()
    if row and row["c"]:
        vals.append(from_iso(row["c"]))
    return min(vals) if vals else None


def _missing_dailies(db: sqlite3.Connection,
                     cutoff: datetime) -> tuple[list[str], list[str]]:
    """（漏报, 投递未完成）分开统计（复审 R2-1）：
    - 漏报＝该截止点在库中完全无记录；
    - 投递未完成＝有记录但状态非 sent（failed/uncertain/sending/skipped），
      如实标注状态，不据此自动重发；已发送不算漏报。"""
    start = _known_start(db)
    if start is None:
        return [], []
    first = to_beijing(start).replace(hour=19, minute=0, second=0, microsecond=0)
    if to_beijing(start) >= first:
        first += timedelta(days=1)
    rows = {r["key"]: r["state"] for r in
            db.execute("SELECT key, state FROM deliveries WHERE key LIKE 'daily:%'")}
    missing: list[str] = []
    undelivered: list[str] = []
    d = first
    while d < cutoff:
        key = f"daily:{d:%Y%m%d}"
        state = rows.get(key)
        if state is None:
            missing.append(f"{d:%Y-%m-%d} 19:00")
        elif state != "sent":
            undelivered.append(f"{d:%Y-%m-%d} 19:00（{state}）")
        d += timedelta(days=1)
    return missing, undelivered


def maybe_daily(db: sqlite3.Connection, cfg: dict, now: datetime) -> str | None:
    """只登记最近一个截止点的日报；已登记（任意状态）不再生成。

    截止点早于监测起点 → 不生成启用前报告（复审 F3：首份为启用后的 19 点）。
    """
    cutoff = _latest_cutoff(now)
    start = _known_start(db)
    if start is not None and cutoff <= start:
        return None
    key = f"daily:{cutoff:%Y%m%d}"
    if db.execute("SELECT 1 FROM deliveries WHERE key=?", (key,)).fetchone():
        return None
    rows = db.execute(
        "SELECT e.*, d.state AS mail FROM events e"
        " LEFT JOIN deliveries d ON d.key = 'round:' || e.session || ':' || e.turn").fetchall()
    events = [{"session": r["session"], "turn": r["turn"], "project": r["project"],
               "confirmed": from_iso(r["confirmed_at_utc"]) if r["confirmed_at_utc"] else None,
               "outcome": r["outcome"], "mail": r["mail"]} for r in rows]
    coverage = _known_start(db)
    missing, undelivered = _missing_dailies(db, cutoff)
    subject, body = build_daily(cfg, events, cutoff, now, coverage,
                                missing, undelivered)
    with _txn(db):
        db.execute("INSERT INTO deliveries(key,kind,subject,body,state,created_at_utc)"
                   " VALUES(?,?,?,?, 'pending', ?) ON CONFLICT(key) DO NOTHING",
                   (key, "daily", subject, body, utc_iso(now)))
    return f"日报已登记：{subject}"


def _backfill_deadlines(db: sqlite3.Connection) -> int:
    """旧库旧行的一次性截止回填：入队时还没有 deadline 列的轮次邮件，
    依据事件确认时刻补算。返回回填行数。"""
    rows = db.execute(
        "SELECT d.key AS k, e.confirmed_at_utc AS c FROM deliveries d"
        " JOIN events e ON d.key = 'round:' || e.session || ':' || e.turn"
        " WHERE d.kind='round' AND d.deadline_utc IS NULL"
        " AND e.confirmed_at_utc IS NOT NULL").fetchall()
    n = 0
    for r in rows:
        with _txn(db):
            db.execute("UPDATE deliveries SET deadline_utc=? WHERE key=? AND deadline_utc IS NULL",
                       (utc_iso(round_deadline(from_iso(r["c"]))), r["k"]))
        n += 1
    return n


def _skip_unregistered_rounds(db: sqlite3.Connection, cfg: dict, lines: list[str]) -> None:
    """发送前按当前登记项目过滤（复审 F7）：候选登记后取消登记的项目，
    其待发完成通知标记 skipped，不发送、不扫描新会话猜替代。"""
    registered = set()
    for p in cfg.get("projects", []):
        if isinstance(p, dict) and p.get("path"):
            registered.add(str(common.normalize_path(p["path"])))
    rows = db.execute(
        "SELECT d.key AS k, e.project AS p FROM deliveries d"
        " JOIN events e ON d.key = 'round:' || e.session || ':' || e.turn"
        " WHERE d.kind='round' AND d.state='pending'").fetchall()
    for r in rows:
        if r["p"] in registered:
            continue
        with _txn(db):
            db.execute("UPDATE deliveries SET state='skipped', error=? WHERE key=? AND state='pending'",
                       ("项目已不在 hq.toml 登记（发送前按当前登记范围过滤）", r["k"]))
        lines.append(f"{r['k']}: ⏭ 项目已取消登记，跳过发送")


def _skip_older_dailies(db: sqlite3.Connection, now: datetime, lines: list[str]) -> None:
    """日报补发口径＝当前应补截止点（复审 R2-1）：只有 key 等于当前应补期的
    pending 日报可发送，其余待发日报一律明确跳过——最近一期已
    sent/failed/uncertain/sending、或本期构建失败，都不能促使补更旧期。
    不取 pending 子集最大日期（那只会在存在唯一旧 pending 时把它误当“最近”）。
    本函数在 maybe_daily 之前执行，日报正文里的“投递未完成”标注才是发送时状态。
    """
    cutoff_key = f"daily:{_latest_cutoff(now):%Y%m%d}"
    rows = db.execute("SELECT key FROM deliveries WHERE kind='daily' AND state='pending'").fetchall()
    for r in rows:
        if r["key"] == cutoff_key:
            continue
        with _txn(db):
            db.execute("UPDATE deliveries SET state='skipped', error=? WHERE key=? AND state='pending'",
                       ("旧日报不补发（只发当前应补截止点一期，不自动重发）", r["key"]))
        lines.append(f"{r['key']}: ⏭ 旧日报跳过补发（非当前应补期）")


def tick(cfg: dict, now: datetime, sender=None, log_dir: Path | None = None,
         clock=None) -> list[str]:
    """确认候选 → 按每轮独立截止入队/跳过 → 登记最近日报 → 原子领取后逐封发送。

    clock：返回 aware UTC 当前时刻的可调用，入队与每封发送前重新校验截止；
    缺省用真实系统时钟（计划任务每次 tick 都重新判断，不复用旧时刻）。
    sending 行属于仍可能存活的发送进程：本函数不回收、不判退出、不自动重发
    （复审 F2：另一个 tick 不能判定活跃发送器已退出）。单封错误不影响其他邮件。
    """
    mcfg = load_mail_config(cfg)
    if not mcfg["enabled"]:
        return ["mail 未启用（hq.toml [mail] enabled=false），tick 跳过"]
    if clock is None:
        clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    db = connect()
    lines: list[str] = []
    try:
        with _txn(db):
            db.execute("INSERT INTO meta(key,value) VALUES('first_tick_utc',?)"
                       " ON CONFLICT(key) DO NOTHING", (utc_iso(now),))
        _backfill_deadlines(db)

        rows = db.execute("SELECT * FROM events WHERE outcome='pending'").fetchall()
        candidates = [{"session": r["session"], "turn": r["turn"], "project": r["project"],
                       "observed": from_iso(r["observed_at_utc"])} for r in rows]
        confirmed = confirm_rounds(candidates, log_paths_for(candidates, now, log_dir))
        for (sid, tid), hit in confirmed.items():
            with _txn(db):
                db.execute("UPDATE events SET confirmed_at_utc=?, outcome=? WHERE session=? AND turn=?",
                           (utc_iso(hit["at_utc"]), hit["outcome"], sid, tid))
        if confirmed:
            lines.append(f"确认 {len(confirmed)} 轮宿主终态")

        for r in db.execute("SELECT * FROM events WHERE notified=0 AND outcome!='pending'").fetchall():
            e = {"session": r["session"], "turn": r["turn"], "project": r["project"],
                 "confirmed": from_iso(r["confirmed_at_utc"]), "outcome": r["outcome"],
                 "reply_summary": r["reply_summary"]}
            if e["outcome"] == "completed" and in_night_window(e["confirmed"]):
                subject, body = _round_body(e)
                deadline = round_deadline(e["confirmed"])
                if clock() < deadline:
                    with _txn(db):
                        db.execute("INSERT INTO deliveries(key,kind,subject,body,state,"
                                   "created_at_utc,error,deadline_utc)"
                                   " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                                   (round_key(e["session"], e["turn"]), "round", subject, body,
                                    "pending", utc_iso(now), None, utc_iso(deadline)))
                    lines.append(f"完成通知入队：{subject}")
                else:
                    with _txn(db):
                        db.execute("INSERT INTO deliveries(key,kind,subject,body,state,"
                                   "created_at_utc,error,deadline_utc)"
                                   " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
                                   (round_key(e["session"], e["turn"]), "round", subject, body,
                                    "skipped", utc_iso(now),
                                    "完成时在夜间窗口，但已过该轮次日 10:00 截止（只进日报）",
                                    utc_iso(deadline)))
                    lines.append(f"完成通知已过该轮次日 10:00 截止，标记 skipped（只进日报）：{subject}")
            with _txn(db):
                db.execute("UPDATE events SET notified=1 WHERE session=? AND turn=?",
                           (e["session"], e["turn"]))

        _skip_unregistered_rounds(db, cfg, lines)
        _skip_older_dailies(db, now, lines)   # 先淘汰旧 pending，日报标注才是发送时状态

        try:
            daily = maybe_daily(db, cfg, now)
        except Exception as exc:  # 日报构建失败不挡其他待发通知（复审 F3）；
            # 本期没生成也不能用旧 pending 日报代替（R2-1：淘汰已在构建前完成）
            print(f"{LOG_TAG} 日报生成失败（不影响待发通知的发送）: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            daily = None
        if daily:
            lines.append(daily)

        lines += _deliver(db, mcfg, sender, clock)
    finally:
        db.close()
    return lines


# ---------------------------------------------------------------- status / test


def status_lines() -> list[str]:
    path = db_path()
    if not path.exists():
        return [f"mail 数据库不存在（{path}）：尚无观察记录。"]
    db = connect(path)
    try:
        n_ev = db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        n_dl = db.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
        out = [f"mail 数据库：{path}（事件 {n_ev} / 通知 {n_dl}）"]
        out.append("最近事件：")
        for r in db.execute("SELECT * FROM events ORDER BY observed_at_utc DESC, session LIMIT 10"):
            out.append(f"  {bj_str(from_iso(r['observed_at_utc']))} {Path(r['project']).name}"
                       f" {r['session'][:8]}/{r['turn'][:8]} outcome={r['outcome']}"
                       f" 通知={_mail_state(db, r)}")
        out.append("通知：")
        for r in db.execute("SELECT * FROM deliveries ORDER BY created_at_utc DESC, key LIMIT 15"):
            tail = f" error={r['error']}" if r["error"] else ""
            sent = f" sent={bj_str(from_iso(r['sent_at_utc']))}" if r["sent_at_utc"] else ""
            out.append(f"  {r['key']} {r['kind']} {r['state']}{sent}{tail}")
        if any(r["state"] == "uncertain" for r in db.execute("SELECT state FROM deliveries")):
            out.append("⚠ 结果待核对（uncertain）＝发送结果不明：本版不自动重发，请人工核对邮箱。")
        n_sending = db.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE state='sending'").fetchone()["n"]
        if n_sending:
            out.append(f"⚠ {n_sending} 封发送中/结果待核对（sending）：可能仍由发送进程持有，"
                       "也可能发送后进程中断；另一个 tick 不能判定其已退出，"
                       "本版不回收、不自动重发，请结合 mail status 时间与邮箱人工核对。")
        return out
    finally:
        db.close()


def _mail_state(db: sqlite3.Connection, r: sqlite3.Row) -> str:
    row = db.execute("SELECT state FROM deliveries WHERE key=?",
                     (round_key(r["session"], r["turn"]),)).fetchone()
    return row["state"] if row else "-"


def cmd_test() -> int:
    """发送一封 TEST 邮件验证 SMTP 配置；真实外发只在用户配置齐备后进行。"""
    try:
        mcfg = load_mail_config(common.load_config())
    except MailConfigError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 2
    if not mcfg["enabled"]:
        print("mail 未启用（hq.toml [mail] enabled=false）。配置后再 test。", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    subject = f"[hq][TEST] 邮件配置验证 {bj_str(now)}"
    body = ("这是 hq 邮件配置测试邮件。\n收到只说明 SMTP 参数可用"
            "（SMTP 接受 ≠ 收件箱到达；也不代表完成通知/日报已验证）。\n"
            f"生成时间：{bj_str(now)}（北京时间）")
    try:
        send_mail(mcfg, subject, body, message_id_for(f"test:{utc_iso(now)}", mcfg["from_addr"]))
    except MailPermanentError as exc:
        print(f"❌ 测试邮件失败（未接受）: {exc}", file=sys.stderr)
        return 1
    except MailUnknownError as exc:
        print(f"⚠ 测试邮件结果不明（待核对，不自动重发）: {exc}", file=sys.stderr)
        return 1
    print("✅ 测试邮件已被 SMTP 接受（不代表收件箱到达）。")
    return 0


# ---------------------------------------------------------------- setup（一次性）


def hook_ctx() -> dict:
    """观察 hook 的安装上下文：与 adapters.context() 同口径，但不依赖它。"""
    return {"python": sys.executable,
            "bootstrap": Path(common.HQ_HOME) / "bin" / "hq.py",
            "hq_config": Path(common.CONFIG_PATH),
            "home": Path.home(),
            "backups": Path(common.HQ_HOME) / "backups"}


def tick_argv(ctx: dict) -> list[str]:
    """计划任务的启动 argv：绝对解释器 + 绝对 bootstrap + 显式配置，无密码。"""
    return [ctx["python"], "-X", "utf8", str(ctx["bootstrap"]), "--hq-config",
            str(ctx["hq_config"]), "mail", "tick"]


def mail_entry(ctx: dict) -> dict:
    """Stop 观察条目。身份 = args 里的 (bootstrap, hq_config, mail, observe) 精确形态。"""
    return {"type": "process", "enabled": True, "command": ctx["python"],
            "args": ["-X", "utf8", str(ctx["bootstrap"]), "--hq-config",
                     str(ctx["hq_config"]), "mail", "observe"],
            "timeoutMs": OBSERVE_TIMEOUT_MS}


def identity_from_mail_args(args) -> tuple | None:
    """从 handler args 解析 mail observe 身份；不是精确形态 → None（陌生不动）。"""
    if not (isinstance(args, list) and len(args) == 7
            and all(isinstance(a, str) and a for a in args)):
        return None
    if args[0] != "-X" or args[1] != "utf8" or args[3] != "--hq-config":
        return None
    if args[5:7] != ["mail", "observe"]:
        return None
    return normalized(args[2]), normalized(args[4])


def _identity_from_gate_args(args) -> tuple | None:
    """旧 gate handler 的 argv 形态（adapters.hook_argv[1:]）：
    ["-X","utf8",<bootstrap>,"--hq-config",<cfg>,"hook","--agent","zcode","--event","stop"]。"""
    if not (isinstance(args, list) and len(args) >= 10
            and all(isinstance(a, str) and a for a in args)):
        return None
    if args[0] != "-X" or args[1] != "utf8" or args[3] != "--hq-config":
        return None
    if args[-5:] != ["hook", "--agent", "zcode", "--event", "stop"]:
        return None
    return normalized(args[2]), normalized(args[4])


def _structure_error(data: dict) -> str | None:
    """hooks 配置结构验证（inspect_setup 与 apply_setup 共用，复审 R2-2）。

    缺省键合法（没有 hooks/events/Stop/组内 hooks 键都算空）；显式 null 或
    类型不符一律拒绝。返回首个错误描述，合法返回 None。
    """
    if "hooks" in data and not isinstance(data["hooks"], dict):
        return "hooks 不是 object（含 null）"
    hooks = data.get("hooks") or {}
    if "events" in hooks and not isinstance(hooks["events"], dict):
        return "hooks.events 不是 object（含 null）"
    events = hooks.get("events") or {}
    for name, groups in events.items():
        if not isinstance(groups, list):
            return f"hooks.events.{name} 不是数组（含 null）"
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                return f"hooks.events.{name}[{gi}] 不是 object"
            if "hooks" in group and not isinstance(group["hooks"], list):
                return f"hooks.events.{name}[{gi}].hooks 不是数组（含 null）"
            for hi, h in enumerate(group.get("hooks") or []):
                if not isinstance(h, dict):
                    return f"hooks.events.{name}[{gi}].hooks[{hi}] 不是 object"
    return None


def zcode_config_path(ctx: dict) -> Path:
    return Path(ctx["home"]) / ".zcode" / "cli" / "config.json"


def inspect_setup(cfg: dict, ctx: dict) -> dict:
    """DryRun 事实：目标配置现状 + 将写入的条目 + 影响面（不改任何文件）。

    与 apply_setup 共用同一结构验证（复审 R2-2）：解析失败、无法读取或
    hooks 结构非法都写入 parse_error，由调用方拒绝安装/预览报错。
    """
    path = zcode_config_path(ctx)
    entry = mail_entry(ctx)
    info = {"target": str(path), "exists": path.exists(), "entry": entry,
            "hooks_enabled": None, "mail_present": False, "other_handlers": 0,
            "old_gate": [], "parse_error": None, "would_change": not path.exists()}
    data, err = _read_json_strict(path)
    if err:
        info["parse_error"] = err
        return info
    bad = _structure_error(data or {})
    if bad:
        info["parse_error"] = bad
        return info
    hooks = (data or {}).get("hooks")
    info["hooks_enabled"] = hooks.get("enabled") if isinstance(hooks, dict) else None
    events = hooks.get("events") if isinstance(hooks, dict) else None
    if isinstance(events, dict):
        mine = (normalized(str(ctx["bootstrap"])), normalized(str(ctx["hq_config"])))
        for ev_name, groups in events.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                for h in group.get("hooks", []) if isinstance(group, dict) else []:
                    if not isinstance(h, dict):
                        continue
                    ident = identity_from_mail_args(h.get("args"))
                    if ev_name == "Stop" and ident is not None and ident == mine:
                        info["mail_present"] = True
                    elif ev_name == "Stop" and _identity_from_gate_args(h.get("args")):
                        info["old_gate"].append({"enabled": h.get("enabled"),
                                                 "cfg": str(_identity_from_gate_args(h.get("args"))[1])})
                    else:
                        info["other_handlers"] += 1
    info["would_change"] = not info["mail_present"]
    info["task_action"] = subprocess.list2cmdline(tick_argv(ctx))
    info["task_hint"] = ("计划任务：powershell -File scripts\\install-mail-task.ps1 -DryRun 预览后安装"
                         "（当前用户、每分钟+登录触发、IgnoreNew、StartWhenAvailable、隐藏窗口；"
                         "参数只含绝对路径与 mail tick，不含密码）")
    return info


def plan_setup_lines(info: dict, dry_run: bool) -> list[str]:
    head = "[DryRun] " if dry_run else ""
    lines = [f"{head}目标配置：{info['target']}（{'存在' if info['exists'] else '不存在，将新建'}）"]
    if info["parse_error"]:
        lines.append(f"! 配置无法解析或结构非法，拒绝写入: {info['parse_error']}")
        return lines
    enabled = info["hooks_enabled"]
    lines.append(f"hooks.enabled：{enabled}"
                 + ("" if enabled is True else "（非 true：观察 hook 不会执行；hq 不代改全局开关，待你启用）"))
    lines.append(f"本实例 mail observe 条目：{'已存在（将按需更新执行字段）' if info['mail_present'] else '将新增'}")
    lines.append(f"  {json.dumps(info['entry'], ensure_ascii=False)}")
    lines.append(f"旧 gate 条目：{len(info['old_gate'])} 条"
                 + ("".join(f"（enabled={g['enabled']}，保持不动）" for g in info["old_gate"])
                    if info["old_gate"] else "（未发现；不会去重新启用它）"))
    lines.append(f"其他 handler：{info['other_handlers']} 个（原样保留，开关不动）")
    lines.append(f"计划任务启动行：{info['task_action']}")
    lines.append(info["task_hint"])
    lines.append("说明：需要新会话才可能生效（运行时加载/已有会话热更新未证明）；"
                 "安装成功≠真实触发，现场验收待自然触发后确认。")
    return lines


def apply_setup(cfg: dict, ctx: dict) -> tuple[list[str], list[str]]:
    """真实 setup：旧文件存在才备份，再只新增/更新精确同身份条目；其他内容一律
    不动。目标缺失/父目录缺失可创建；写入失败转结构化错误，不出 traceback。"""
    path = zcode_config_path(ctx)
    entry = mail_entry(ctx)
    messages: list[str] = []
    errors: list[str] = []
    data, err = _read_json_strict(path)
    if err:
        errors.append(f"{path}: {err}")
        return messages, errors
    data = data if data is not None else {}
    bad = _structure_error(data)   # 与 inspect_setup 共用同一验证（复审 R2-2）
    if bad:
        errors.append(f"{path}: {bad}（保持原文件不变）")
        return messages, errors
    hooks = data.setdefault("hooks", {})
    events = hooks.setdefault("events", {})

    mine = (normalized(str(ctx["bootstrap"])), normalized(str(ctx["hq_config"])))
    found = False
    changed = False
    for group in events.setdefault("Stop", []):
        for h in list(group.get("hooks") or []):   # 缺省键合法：无 hooks 键即无条目
            if identity_from_mail_args(h.get("args")) != mine:
                continue
            if found:  # 重复同身份条目去重（保留首个）
                group["hooks"].remove(h)
                changed = True
                messages.append("移除重复的本实例 mail observe 条目（其他条目不动）")
                continue
            found = True
            updates = {k: v for k, v in entry.items() if h.get(k) != v}
            for k, v in updates.items():
                h[k] = v  # 只写本条目执行字段，未知字段保留
            if updates:
                changed = True
                messages.append("已更新本实例 mail observe 条目执行字段（其余字段原样保留）")
    if not found:
        events["Stop"].append({"hooks": [dict(entry)]})
        changed = True
        messages.append("已新增 ZCode Stop 观察 hook（mail observe）")

    if changed:
        try:
            backup = _backup_and_write(path, data, ctx, "mail-zcode-config")
        except OSError as exc:
            # os.replace 是原子的：失败时目标保持原状（旧文件未动 / 新文件未写出）
            errors.append(f"{path}: 写入失败（目标保持原状）: {exc}")
            return messages, errors
        if backup is not None:
            messages.append(f"原配置已备份: {Path(backup).name}")
        else:
            messages.append(f"目标配置原先不存在，已新建（{path}）")
    else:
        messages.append(f"已确认 mail observe 条目最新（{path}）")
    if hooks.get("enabled") is not True:
        messages.append("注意: hooks.enabled 非 true，观察 hook 不会执行；"
                        "hq 不代改全局开关（启用其他 Hook 的影响由你决定）。")
    messages.append("需要新会话才可能生效（ZCode 在会话启动时快照 hooks 配置，"
                    "运行时热更新未证明）；安装成功≠真实触发，现场验收待自然触发后确认。")
    return messages, errors


def _read_json_strict(path: Path):
    path = Path(path)
    if not path.exists():
        return None, None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"无法读取（拒绝写入，保持原状）: {exc}"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"不是合法 UTF-8 JSON（保持原文件不变）: {exc}"
    if not isinstance(data, dict):
        return None, "顶层必须是 JSON object（保持原文件不变）"
    return data, None


def _backup_and_write(path: Path, data: dict, ctx: dict, label: str) -> Path | None:
    """唯一命名备份（仅当旧文件存在）+ 原子替换（复用 common.write_text）。

    目标/父目录不存在可新建；返回备份路径，未备份（首次新建）返回 None。
    写入失败由调用方转结构化错误；os.replace 保证旧文件不会半途损坏。
    """
    import shutil
    import uuid

    path = Path(path)
    backup = None
    if path.exists():
        backups = Path(ctx["backups"])
        backups.mkdir(parents=True, exist_ok=True)
        src = hashlib.sha1(str(common.normalize_path(path)).encode("utf-8")).hexdigest()[:8]
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        while True:
            cand = backups / f"{label}-{src}-{stamp}-{uuid.uuid4().hex[:8]}{path.suffix}"
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                break
            except FileExistsError:
                continue
        os.close(fd)
        shutil.copy2(path, cand)
        backup = cand
    common.write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return backup
