"""hq dashboard：本机单页仪表板（展示 / 只读聚合 / 受限配置 / 显式 TEST）。

方案：docs/plans/2026-10-04-local-dashboard.md。边界：
- 只展示已有事实（Stop 候选、宿主终态、邮件处理结果、接入信息）；不推测 AI
  是否在线、任务是否全部完成或端到端健康，没有证据的项显示未验证。
- 页面刷新/查看/保存配置不调用 mail.tick；自动邮件仍由既有 Windows 计划任务
  负责。不修改 hqlib/mail.py、SQLite schema 或既有时间/重发规则。
- 固定监听 127.0.0.1；Python 标准库 HTTP + 单 HTML，无第三方依赖、无外部 CDN。
- 密码仍只放环境变量：只显示"是否存在"布尔值；进程内需读取环境变量做非空
  判断（校验/TEST 用），值不回传页面、不落盘、不写入任何记录（复审 R5 措辞
  更正，与 README 口径一致）。

GET 读取不使用会建库/迁移的 mail.connect，而是 SQLite URI mode=ro + query_only
+ 有界读事务；库缺失/损坏/锁定各自可诊断，不在后台修库。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import common, mail

PAGE_LIMIT_MAX = 200
DEFAULT_PAGE_LIMIT = 50
RECENT_DAILIES = 10
CONFIG_BODY_MAX = 64 * 1024          # 配置请求上限：足够当前小型项目登记
TEST_REQUEST_ID_MAX = 64
SETUP_TIMEOUT = 10                   # 单次本机计划任务元数据查询超时（秒）
PERIODS = ("24h", "7d", "all")
MAIL_EDIT_FIELDS = ("smtp_host", "smtp_port", "security", "username",
                    "password_env", "from_addr", "to_addr")
DEFAULT_PASSWORD_ENV = "HQ_SMTP_PASSWORD"


class ConfigError(Exception):
    """受限保存的业务拒绝：kind ∈ invalid(400) / conflict(409)。"""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


# ---------------------------------------------------------------- 只读数据库


def _db_error_detail(exc: sqlite3.Error) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = str(exc).lower()
    if "locked" in low:
        return f"数据库被锁定（可能有 tick 正在写）：{text}"
    if "not a database" in low:
        return f"坏库（不是 SQLite 文件）：{text}"
    return f"读取失败（可能有 tick 正在写，稍后刷新）：{text}"


_SQLITE_MAGIC = b"SQLite format 3\x00"


def _precheck_db_file(path: Path) -> str | None:
    """打开前诊断（只读文件头与目录清单）：按方案，无法确保无副作用读取的
    状况（WAL 模式、活跃 sidecar）在连接前拒绝，绝不自行创建/删除/修复
    sidecar，也不用 immutable 忽略活跃日志。返回 None 表示可只读打开；
    检查本身不保证与不合作写入方无竞态，存疑一律保守拒绝。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(100)
    except OSError as exc:
        return f"无法读取库文件头（保守拒绝）: {exc}"
    if len(head) < 100 or not head.startswith(_SQLITE_MAGIC):
        return "坏库（文件头不是 SQLite 格式）"
    if head[18] == 2 or head[19] == 2:
        return ("库处于 WAL 模式：只读连接也可能创建/留存 -wal/-shm 副本，"
                "无法保证零副作用读取，已拒绝读取（等写方正常退出并回到"
                " rollback 模式后再刷新）")
    for suffix, what in (("-wal", "WAL 日志"), ("-journal", "回滚日志")):
        side = path.with_name(path.name + suffix)
        try:
            size = side.stat().st_size if side.exists() else 0
        except OSError as exc:
            return f"无法检查 {side.name}（保守拒绝）: {exc}"
        if size > 0:
            return (f"存在活跃{what} {side.name}（{size} 字节）——可能有写方"
                    "进行中，保守拒绝读取")
    return None


def _connect_ro() -> tuple[sqlite3.Connection | None, str | None]:
    """只读打开业务库：缺失返回 (None, "missing")；打不开返回 (None, 原因)。
    绝不创建目录/数据库/sidecar（不用 immutable、不用 mail.connect）。
    打开过程任何一步失败都把已建连接关闭，不留半开资源。"""
    path = mail.db_path()
    if not path.exists():
        return None, "missing"
    reason = _precheck_db_file(path)
    if reason is not None:
        return None, reason
    db = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")            # 有界读事务：快照内查询一致
        return db, None
    except sqlite3.Error as exc:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        return None, _db_error_detail(exc)


def _finish_ro(db: sqlite3.Connection | None) -> None:
    if db is not None:
        try:
            db.execute("ROLLBACK")     # 只读事务收口，无副作用
        except sqlite3.Error:
            pass
        db.close()


# ---------------------------------------------------------------- 行状态口径

# 行状态/需要注意的判定在 SQL 内完成（_SNAPSHOT_CTE 的 label/attention 列），
# COUNT 与分页共用同一表达式，避免两套口径。规则（复审 F3）：
# - 有投递记录先如实展示投递状态（sent/sending/failed/uncertain/skipped/
#   pending/未知原文），pending 按该轮截止区分待处理/待核对；
# - 无投递：outcome 非完成原文展示（pending→待确认）；completed 无完成时间
#   待确认；不在夜间窗口仅日报；夜间窗口内按 mail.round_deadline 同口径从
#   完成时间推导截止：已过截止或 notified=1（tick 已处理却无本轮投递记录）
#   → 待核对；未到期且 notified=0 → 待处理；
# - 时间字段损坏的行如实标记数据异常并计入需要注意，其余可读字段照常展示。


def _display_name(path: str, dup_leaves: set[str]) -> str:
    """项目名来自目录名；同名碰撞按不同规范根目录统计（不是事件条数），
    碰撞时以路径尾部两级区分。"""
    name = Path(path).name or path
    if name in dup_leaves:
        return "/".join(Path(path).parts[-2:])
    return name


# ---------------------------------------------------------------- 快照

DAILY_PAGE_MAX = 50
DAILY_PAGE_DEFAULT = RECENT_DAILIES

# 快照行状态/截止/需要注意全部在 SQL 内推导；COUNT、聚合计数与分页查询共用
# 这一段 CTE，保证口径一致（复审 F3/F6）。时间比较统一 CAST 成 epoch 整数，
# 避免 SQLite 文本/整数存储级次序；损坏时间串由日期函数返回 NULL，按数据
# 异常行如实展示，不让查询失败。deadline 推导与 mail.round_deadline 同口径：
# 北京完成时间的当日（hour<10）或次日（hour≥10）10:00。
_SNAPSHOT_CTE = """
WITH base AS (
    SELECT e.session AS session, e.turn AS turn, e.project AS project,
           e.observed_at_utc AS observed, e.confirmed_at_utc AS confirmed,
           e.outcome AS outcome, e.reply_summary AS summary,
           COALESCE(e.notified, 0) AS notified,
           d.state AS dstate, d.error AS derror, d.deadline_utc AS ddeadline,
           d.sent_at_utc AS dsent, d.key AS dkey,
           CAST(strftime('%s', e.observed_at_utc) AS INTEGER) AS obs_epoch,
           CAST(strftime('%s', e.confirmed_at_utc) AS INTEGER) AS conf_epoch,
           CAST(strftime('%H', datetime(e.confirmed_at_utc, '+8 hours')) AS INTEGER)
               AS conf_bj_hour,
           date(e.confirmed_at_utc, '+8 hours') AS conf_bj_date,
           CAST(strftime('%s', COALESCE(e.confirmed_at_utc, e.observed_at_utc))
                AS INTEGER) AS when_epoch
    FROM events e
    LEFT JOIN deliveries d ON d.key = 'round:' || e.session || ':' || e.turn
),
mid AS (
    SELECT *,
        CASE WHEN when_epoch IS NULL THEN NULL ELSE
            strftime('%Y-%m-%dT%H:%M:%S', when_epoch, 'unixepoch') || '+00:00'
        END AS when_iso,
        CASE WHEN when_epoch IS NULL THEN NULL ELSE
            strftime('%Y-%m-%d %H:%M', when_epoch, 'unixepoch', '+8 hours')
        END AS when_bj,
        CASE
            WHEN dstate IS NOT NULL AND ddeadline IS NOT NULL
                THEN CAST(strftime('%s', ddeadline) AS INTEGER)
            WHEN conf_epoch IS NOT NULL
                 AND (conf_bj_hour >= 19 OR conf_bj_hour < 10)
                THEN CAST(strftime('%s', conf_bj_date || ' 10:00:00') AS INTEGER)
                - 28800 + (CASE WHEN conf_bj_hour >= 10 THEN 86400 ELSE 0 END)
            ELSE NULL
        END AS dl_epoch,
        -- 复审 R2：deadline_utc 存在但解析失败。SQLite CASE 命中第一分支后
        -- 不再回退，坏值时 dl_epoch=NULL 不会落到完成时间推导值——推导参考值
        -- 不掩盖投递记录中的坏值；坏值本身在此显式成旗标。
        (dstate IS NOT NULL AND ddeadline IS NOT NULL
         AND strftime('%s', ddeadline) IS NULL) AS bad_deadline
    FROM base
),
stat AS (
    SELECT *,
        (dl_epoch IS NOT NULL AND :now_epoch >= dl_epoch) AS expired,
        (conf_bj_hour >= 19 OR conf_bj_hour < 10) AS night
    FROM mid
),
info AS (
    SELECT *,
        CASE
            WHEN when_epoch IS NULL THEN '数据异常（时间无法解析）'
            WHEN confirmed IS NOT NULL AND conf_epoch IS NULL
                THEN '数据异常（完成时间无法解析）'
            WHEN bad_deadline THEN '数据异常（发送截止无法解析）'
            WHEN dstate = 'sent' THEN 'SMTP已接受'
            WHEN dstate = 'sending' THEN '发送中或结果待核对'
            WHEN dstate = 'failed' THEN '发送失败'
            WHEN dstate = 'uncertain' THEN '结果待核对'
            WHEN dstate = 'skipped' THEN '已跳过（只进日报）'
            WHEN dstate = 'pending'
                THEN (CASE WHEN expired THEN '待核对' ELSE '待处理' END)
            WHEN dstate IS NOT NULL THEN dstate || '（未知状态原文）'
            WHEN outcome IS NULL THEN '待确认'
            WHEN outcome <> 'completed'
                THEN (CASE WHEN outcome = 'pending' THEN '待确认' ELSE outcome END)
            WHEN conf_epoch IS NULL THEN '待确认'
            WHEN NOT night THEN '仅日报'
            WHEN expired THEN '待核对'
            WHEN notified = 1 THEN '待核对'
            ELSE '待处理'
        END AS label,
        CASE
            WHEN when_epoch IS NULL THEN 1
            WHEN confirmed IS NOT NULL AND conf_epoch IS NULL THEN 1
            WHEN bad_deadline THEN 1
            WHEN dstate IN ('sending', 'failed', 'uncertain') THEN 1
            WHEN dstate = 'pending' THEN (CASE WHEN expired THEN 1 ELSE 0 END)
            WHEN dstate IN ('sent', 'skipped') THEN 0
            WHEN dstate IS NOT NULL THEN 1
            WHEN outcome IS NULL THEN 0
            WHEN outcome <> 'completed' THEN 0
            WHEN conf_epoch IS NULL THEN 0
            WHEN NOT night THEN 0
            WHEN expired THEN 1
            WHEN notified = 1 THEN 1
            ELSE 0
        END AS attention
    FROM stat
)"""


def read_snapshot(cfg_path: Path, *, now: datetime, project: str | None,
                  period: str, attention: bool, offset: int, limit: int,
                  daily_offset: int = 0, daily_limit: int | None = None) -> dict:
    """事实快照：轮次、邮件状态、日报与计数；只读。筛选/计数/排序/分页都在
    SQLite 内完成（COUNT + 有界 LIMIT/OFFSET），应用侧只转换当页行与有界
    日报行，不做全量物化（复审 F6）。勾选需要注意时查看全部历史（period
    不参与筛选），聚合计数始终是全库口径。"""
    if period not in PERIODS:
        raise ValueError(f"period 只支持 {'/'.join(PERIODS)}")
    db, source_err = _connect_ro()
    out: dict = {"now": mail.utc_iso(now), "query": {
        "project": project, "period": "all" if attention else period,
        "attention": attention, "offset": offset, "limit": limit}}
    out["source"] = ({"state": "ok"} if db is not None and source_err is None
                     else {"state": "missing" if source_err == "missing" else "error",
                           "detail": None if source_err == "missing" else source_err})
    out["window"] = {"in_window": mail.in_night_window(now),
                     "label": "夜间窗口 19:00–次日10:00" if mail.in_night_window(now)
                             else "日间（完成轮次只进日报）"}
    b = mail.to_beijing(now).replace(hour=19, minute=0, second=0, microsecond=0)
    if b <= mail.to_beijing(now):
        b += timedelta(days=1)
    out["next_daily"] = {"at": mail.utc_iso(b), "beijing": mail.bj_str(b),
                         "note": "计划时间（是否发送取决于计划任务与现场触发，非已验收事实）"}

    cfg_data, _, cfg_err = _read_raw_toml(cfg_path)
    plist: list[dict] = []
    if cfg_data is None:
        out["config"] = {"enabled": None, "source": "error", "detail": cfg_err}
    else:
        mcfg = cfg_data.get("mail") if isinstance(cfg_data, dict) else None
        if mcfg is not None and not isinstance(mcfg, dict):
            out["config"] = {"enabled": None, "source": "error",
                             "detail": f"[mail] 应为 TOML 表，收到 {type(mcfg).__name__}: {mcfg!r}"}
        else:
            m = mcfg if isinstance(mcfg, dict) else {}
            if "enabled" not in m:
                # 合法缺省：按 false（与 read_config 口径一致）
                out["config"] = {"enabled": False, "source": "ok", "detail": None}
            elif isinstance(m["enabled"], bool):
                out["config"] = {"enabled": m["enabled"], "source": "ok",
                                 "detail": None}
            else:
                # 复审 R1：类型损坏不是"ok"，页面不得据此显示确定的暂停/启用
                out["config"] = {"enabled": None, "source": "schema_error",
                                 "detail": f"[mail] enabled 非布尔值，原文 {m['enabled']!r}"}
        for p in (cfg_data.get("projects")
                  if isinstance(cfg_data, dict)
                  and isinstance(cfg_data.get("projects"), list) else []):
            if isinstance(p, dict) and isinstance(p.get("path"), str):
                norm = common.normalize_path(p["path"])
                plist.append({"normalized": str(norm), "name": Path(p["path"]).expanduser().name
                              or str(norm)})
    out["projects_list"] = plist
    out["counts"] = {"projects": len(plist)}

    daily_limit = DAILY_PAGE_DEFAULT if daily_limit is None else daily_limit
    daily_limit = max(1, min(int(daily_limit), DAILY_PAGE_MAX))
    daily_offset = max(0, int(daily_offset))
    out["query"]["daily_offset"] = daily_offset
    out["query"]["daily_limit"] = daily_limit

    if db is None:
        out["counts"].update({"completed_24h": None, "attention": None,
                              "attention_rounds": None, "attention_daily": None,
                              "daily_total": None, "daily_total_filtered": None,
                              "smtp_accepted_24h": None,
                              "pending_confirm": None})
        out["rows"] = []
        out["total"] = 0
        out["recent_dailies"] = []
        out["project_options"] = [{"path": p["normalized"], "name": p["name"]}
                                  for p in plist]
        return out

    params: dict = {"now_epoch": int(now.timestamp()),
                    "cutoff24": int((now - timedelta(days=1)).timestamp()),
                    "limit": limit,
                    "offset": offset, "daily_limit": daily_limit,
                    "daily_offset": daily_offset}
    where: list[str] = []
    try:
        # 名称/筛选口径都用不同规范根目录（DISTINCT project），不是事件条数
        distinct_projects = [r[0] for r in
                             db.execute(
                                 "SELECT DISTINCT project FROM events").fetchall()]
        if project:
            want = str(common.normalize_path(project))
            matches = [p for p in distinct_projects
                       if str(common.normalize_path(p)) == want]
            if matches:
                for i, m in enumerate(matches):
                    params[f"proj{i}"] = m
                where.append("project IN ("
                             + ", ".join(f":proj{i}" for i in range(len(matches)))
                             + ")")
            else:
                where.append("0 = 1")   # 筛选目标不在本库历史项目中
        if not attention:
            if period == "24h":
                where.append("when_iso >= :cutoff")
                params["cutoff"] = mail.utc_iso(now - timedelta(days=1))
            elif period == "7d":
                where.append("when_iso >= :cutoff")
                params["cutoff"] = mail.utc_iso(now - timedelta(days=7))
        if attention:
            where.append("attention = 1")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        agg = db.execute(
            _SNAPSHOT_CTE + " SELECT"
            " COALESCE(SUM(CASE WHEN outcome='completed' AND conf_epoch IS NOT NULL"
            "   AND conf_epoch >= :cutoff24 AND conf_epoch <= :now_epoch"
            "   THEN 1 ELSE 0 END), 0) AS completed_24h,"
            " COALESCE(SUM(CASE WHEN outcome='pending' THEN 1 ELSE 0 END), 0)"
            "   AS pending_confirm,"
            " COALESCE(SUM(attention), 0) AS attention_rounds"
            " FROM info",
            params,
        ).fetchone()
        smtp_24h = db.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE state='sent'"
            " AND CAST(strftime('%s', sent_at_utc) AS INTEGER) >= :cutoff24"
            " AND CAST(strftime('%s', sent_at_utc) AS INTEGER) <= :now_epoch",
            params).fetchone()["n"]
        daily_total = db.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE kind='daily'",
            params).fetchone()["n"]
        attention_daily = db.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE kind='daily'"
            " AND state IN ('failed', 'uncertain', 'sending')",
            params).fetchone()["n"]
        # 复审 R2：勾选需注意时，日报先按同一异常判据筛选再排序/分页；
        # COUNT 与页共用同一 WHERE（attention_daily 即过滤后的总数）。
        daily_page_where = (" WHERE kind='daily' AND state IN"
                            " ('failed', 'uncertain', 'sending')") if attention \
            else " WHERE kind='daily'"
        daily_rows = db.execute(
            "SELECT key, subject, state, created_at_utc AS created,"
            " sent_at_utc AS sent, error FROM deliveries" + daily_page_where +
            " ORDER BY key DESC LIMIT :daily_limit OFFSET :daily_offset",
            params).fetchall()
        total = db.execute(
            _SNAPSHOT_CTE + " SELECT COUNT(*) AS n FROM info" + where_sql,
            params).fetchone()["n"]
        page_rows = db.execute(
            _SNAPSHOT_CTE + " SELECT session, turn, project, observed, confirmed,"
            " outcome, summary, notified, dstate, derror, ddeadline, dsent, dkey,"
            " when_iso, when_bj, label, attention,"
            " ddeadline AS deadline_raw,"
            " CASE WHEN dl_epoch IS NULL THEN NULL ELSE"
            "   strftime('%Y-%m-%dT%H:%M:%S', dl_epoch, 'unixepoch') || '+00:00'"
            " END AS deadline_iso,"
            " CASE WHEN dl_epoch IS NULL THEN NULL ELSE"
            "   strftime('%Y-%m-%d %H:%M', dl_epoch, 'unixepoch', '+8 hours')"
            " END AS deadline_bj,"
            " CASE WHEN when_epoch IS NULL THEN 'observed_at_utc 无法解析'"
            "   WHEN confirmed IS NOT NULL AND conf_epoch IS NULL"
            "   THEN 'confirmed_at_utc 无法解析'"
            "   WHEN bad_deadline"
            "   THEN 'deadline_utc 无法解析（原值见 deadline_raw）' ELSE NULL END AS data_error"
            " FROM info" + where_sql +
            " ORDER BY when_iso DESC, session, turn LIMIT :limit OFFSET :offset",
            params).fetchall()
    except sqlite3.Error as exc:
        # 读事务被写方（如 tick）打断/坏库：降级为可诊断的 error 源，不让
        # 页面崩（连接由 finally 收口）。坏库/锁定分类与打开时一致。
        out["source"] = {"state": "error", "detail": _db_error_detail(exc)}
        out["counts"].update({"completed_24h": None, "attention": None,
                              "attention_rounds": None, "attention_daily": None,
                              "daily_total": None, "daily_total_filtered": None,
                              "smtp_accepted_24h": None,
                              "pending_confirm": None})
        out["rows"] = []
        out["total"] = 0
        out["recent_dailies"] = []
        out["project_options"] = [{"path": p["normalized"], "name": p["name"]}
                                  for p in plist]
        return out
    finally:
        _finish_ro(db)

    # ---- 以下全部是对已取回的有界数据（当页 ≤200 行 + 日报 ≤50 行 +
    # DISTINCT 项目清单）做展示转换，不再触库 ----
    leaf_roots: dict[str, set[str]] = {}
    for p in distinct_projects:
        leaf = Path(p).name or p
        leaf_roots.setdefault(leaf, set()).add(str(common.normalize_path(p)))
    dup_leaves = {leaf for leaf, roots in leaf_roots.items() if len(roots) > 1}

    rows: list[dict] = []
    for r in page_rows:
        rows.append({
            "session": r["session"], "turn": r["turn"], "project": r["project"],
            "project_name": _display_name(r["project"], dup_leaves),
            "outcome": r["outcome"], "observed": r["observed"],
            "confirmed": r["confirmed"],
            "when": r["when_iso"], "when_beijing": r["when_bj"],
            "mail_state": r["dstate"], "deadline": r["deadline_iso"],
            "deadline_beijing": r["deadline_bj"],
            "deadline_raw": r["deadline_raw"],
            "error": r["derror"], "sent": r["dsent"], "summary": r["summary"],
            "delivery_key": r["dkey"], "label": r["label"],
            "attention": bool(r["attention"]), "notified": bool(r["notified"]),
            "data_error": r["data_error"],
        })
    dailies: list[dict] = []
    for r in daily_rows:
        key = r["key"]
        cov = None
        try:
            d = datetime.strptime(key.split(":", 1)[1], "%Y%m%d")
            cov = f"{d:%Y-%m-%d} 19:00（北京时间截止点）"
        except (IndexError, ValueError):
            cov = None
        dailies.append({"key": key, "subject": r["subject"], "state": r["state"],
                        "created": r["created"], "sent": r["sent"], "error": r["error"],
                        "coverage": cov})

    opts: dict[str, str] = {}
    for p in plist:
        opts[p["normalized"]] = p["name"]
    for p in distinct_projects:
        np_ = str(common.normalize_path(p))
        opts.setdefault(np_, _display_name(p, dup_leaves))
    out["project_options"] = [{"path": k, "name": opts[k]} for k in sorted(opts)]

    out["counts"].update({
        "completed_24h": agg["completed_24h"],
        "pending_confirm": agg["pending_confirm"],
        "attention_rounds": agg["attention_rounds"],
        "attention_daily": attention_daily,
        "attention": agg["attention_rounds"] + attention_daily,
        "daily_total": daily_total,
        # 复审 R2：侧区分页在需注意口径下使用过滤后的总数（只读统计字段）
        "daily_total_filtered": attention_daily if attention else daily_total,
        "smtp_accepted_24h": smtp_24h,
        "smtp_note": "近24h按发送接受时间计（含上下界），含轮次通知与日报",
    })
    out["total"] = total
    out["rows"] = rows
    out["recent_dailies"] = dailies
    return out


# ---------------------------------------------------------------- 配置读取/保存


def _read_raw_toml(cfg_path: Path) -> tuple[dict | None, bytes | None, str | None]:
    """（解析结果, 原始字节, 错误）。读原始 TOML（未经 expanduser）作为编辑底稿。"""
    p = Path(cfg_path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        return None, None, f"无法读取: {exc}"
    try:
        data = tomllib.load(__import__("io").BytesIO(raw))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, raw, f"不是合法 UTF-8 TOML: {exc}"
    return data, raw, None


def _password_env_of(mail_cfg: dict) -> str:
    v = mail_cfg.get("password_env")
    return v if isinstance(v, str) and v.strip() else DEFAULT_PASSWORD_ENV


def _mail_problems(mail_cfg: dict) -> list[str]:
    """草稿校验（暂停态也校验已填字段）：类型/端口/安全方式/地址。"""
    problems: list[str] = []
    enabled = mail_cfg.get("enabled", False)
    if not isinstance(enabled, bool):
        problems.append(f"[mail] enabled 应为布尔值，收到 {enabled!r}")
    for key in MAIL_EDIT_FIELDS:
        if key == "smtp_port":
            continue
        v = mail_cfg.get(key)
        if v is not None and not isinstance(v, str):
            problems.append(f"[mail] {key} 应为字符串，收到 {v!r}")
    port = mail_cfg.get("smtp_port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                             or not 1 <= port <= 65535):
        problems.append(f"[mail] smtp_port 应为 1-65535 的整数，收到 {port!r}")
    sec = mail_cfg.get("security")
    if sec is not None and sec not in ("ssl", "starttls"):
        problems.append(f"[mail] security 只支持 ssl|starttls，收到 {sec!r}")
    for key in ("from_addr", "to_addr"):
        v = mail_cfg.get(key)
        # 类型先拒绝，再做地址格式验证：非字符串直接进 regex 会抛 TypeError
        # 而不是结构化的 ConfigError（复审 F1 反例 invalid_address_type）。
        if isinstance(v, str) and v:
            try:
                mail._validate_addr(key, v)
            except mail.MailConfigError as exc:
                problems.append(str(exc))
    return problems


def read_config(cfg_path: Path) -> dict:
    """受限编辑数据：可编辑字段、revision、目标路径、校验问题、密码存在布尔值。
    秘密绝不返回（密码只回 password_env 变量名与是否存在）。

    生命周期状态（复审 F1）：source ∈
    - missing      文件不存在：revision="missing"，允许带该 revision 首建；
    - io_error     文件存在但读不出字节：revision=None，绝不覆盖；
    - parse_error  读到了字节但不是合法 UTF-8 TOML：revision=原字节哈希，
      绝不覆盖（页面不得给出可保存的空替代表单）；
    - schema_error 结构按组件损坏（如 [mail] 不是表）：可读组件照常给出；
    - ok           可编辑。
    """
    target = Path(cfg_path)
    data, raw, err = _read_raw_toml(target)
    out: dict = {"target": str(target), "exists": target.exists()}
    if raw is None:
        out.update({"source": "missing" if not target.exists() else "io_error",
                    "detail": None if not target.exists() else err,
                    "revision": "missing" if not target.exists() else None,
                    "projects": None, "mail": None, "password_present": None})
        return out
    revision = hashlib.sha256(raw).hexdigest()
    if data is None:
        out.update({"source": "parse_error", "detail": err, "revision": revision,
                    "projects": None, "mail": None, "password_present": None})
        return out
    schema_problems: list[str] = []
    raw_projects = data.get("projects") if isinstance(data.get("projects"), list) else []
    if data.get("projects") is not None and not isinstance(data.get("projects"), list):
        schema_problems.append(
            f"[projects] 应为数组，收到 {type(data.get('projects')).__name__}")
    projects = []
    for i, p in enumerate(raw_projects):
        if not isinstance(p, dict) or not isinstance(p.get("path"), str):
            # 复审 R1：每个条目都必须诊断，不能静默跳过后生成"可保存"的
            # 残缺视图——页面同形保存会把没展示出来的条目从文件里删掉。
            schema_problems.append(
                f"[projects] 第 {i + 1} 条目缺少合法 path"
                "（应为含 path 字符串的表）；该条目未展示，原样保留在文件中")
            continue
        norm = common.normalize_path(p["path"])
        projects.append({"path": p["path"], "normalized": str(norm),
                         "exists": norm.is_dir()})
    mcfg = data.get("mail")
    if mcfg is not None and not isinstance(mcfg, dict):
        schema_problems.append(
            f"[mail] 应为 TOML 表，收到 {type(mcfg).__name__}")
    if isinstance(mcfg, dict) and not isinstance(mcfg.get("enabled", False), bool):
        # 复审 R1：enabled 合法缺省为 false；非布尔原值不做布尔转换，
        # 状态=配置错误（未知），由 schema_error 统一表现。
        schema_problems.append(
            f"[mail] enabled 非布尔值，原文 {mcfg['enabled']!r}")
    if mcfg is not None and not isinstance(mcfg, dict):
        # [mail] 组件损坏：可读组件照常给出，mail 编辑数据不可用。
        out.update({"source": "schema_error",
                    "detail": "；".join(schema_problems),
                    "revision": revision, "projects": projects,
                    "mail": None, "password_present": None,
                    "validation": None})
        return out
    mail_cfg = mcfg if isinstance(mcfg, dict) else {}   # 无 [mail] 表：空草稿
    mail_fields = {k: mail_cfg.get(k) for k in MAIL_EDIT_FIELDS}
    mail_fields["enabled"] = mail_cfg.get("enabled", False)   # 原值，不布尔化
    pw_env = _password_env_of(mail_cfg)
    full_for_check = dict(mail_cfg)
    full_for_check["enabled"] = True      # 校验时临时视 enabled 为 true，绝不保存
    try:
        mail.load_mail_config({**data, "mail": full_for_check})
        enable_problems: list[str] = []
    except mail.MailConfigError as exc:
        enable_problems = [str(exc)]
    if not os.environ.get(pw_env, "").strip():
        enable_problems.append(f"密码环境变量 {pw_env} 未设置（当前进程内为空）——"
                               "启用自动邮件前按 README 设置，设置后重启相关进程")
    out.update({"source": "schema_error" if schema_problems else "ok",
                "detail": "；".join(schema_problems) or None,
                "revision": revision,
                "projects": projects, "mail": mail_fields,
                "password_env": pw_env,
                "password_present": bool(os.environ.get(pw_env, "").strip()),
                "validation": {"draft_problems": _mail_problems(mail_cfg),
                               "enable_problems": enable_problems}})
    return out


class _Lossy(Exception):
    """TOML 编码遇到无法无损保留的值。"""


def _toml_inline(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return common.toml_str(v)
    if isinstance(v, list) and all(isinstance(x, (bool, int, float, str)) for x in v):
        return "[" + ", ".join(_toml_inline(x) for x in v) + "]"
    raise _Lossy(f"无法无损编码的值类型: {type(v).__name__}")


def _same_value(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same_value(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    return a == b


def _toml_encode(data: dict) -> str:
    """模块内小型 TOML 编码器：覆盖 tomllib 产出的值类型；不支持的原样拒绝，
    不引入第三方库。编码后由调用方重读并做类型/值等价校验。"""
    lines: list[str] = []

    def emit_table(prefix: str, table: dict) -> None:
        scalars = {k: v for k, v in table.items()
                   if not isinstance(v, dict) and not _is_aot(v)}
        subs = [(k, v) for k, v in table.items()
                if isinstance(v, dict) or _is_aot(v)]
        for k, v in scalars.items():
            lines.append(f"{_key(k)} = {_toml_inline(v)}")
        for k, v in subs:
            full = f"{prefix}.{_key(k)}" if prefix else _key(k)
            if _is_aot(v):
                for item in v:
                    lines.append(f"\n[[{full}]]")
                    emit_table(full, item)
            else:
                lines.append(f"\n[{full}]")
                emit_table(full, v)

    def _is_aot(v) -> bool:
        return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)

    def _key(k: str) -> str:
        # TOML 1.0 裸键只允许 ASCII 字母/数字/下划线/连字符；其余用基本字符串键
        if k and all(("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")
                     or c in "-_" for c in k):
            return k
        return common.toml_str(k)

    emit_table("", data)
    return "\n".join(lines).lstrip("\n") + ("\n" if lines else "")


def _guard_writable(path: Path) -> None:
    """目标与其祖先链存在 symlink/reparse point 时拒绝写入（Windows 目录约束）。
    必须用未解析路径逐级检查：resolve() 会直接跟过去，junction 就看不见了。"""
    if path.is_symlink():
        raise ConfigError("invalid", f"目标是符号链接，拒绝写入: {path}")
    cur = Path(path).absolute()
    chain: list[Path] = []
    while True:
        chain.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    for anc in chain:
        try:
            attrs = anc.lstat().st_file_attributes
            if attrs & 0x400:                      # FILE_ATTRIBUTE_REPARSE_POINT
                raise ConfigError("invalid", f"路径链含 reparse point，拒绝写入: {anc}")
        except (AttributeError, OSError):
            pass                                   # 非 Windows / 目标尚不存在
        if anc.is_symlink():
            raise ConfigError("invalid", f"路径链含符号链接，拒绝写入: {anc}")


def save_config(cfg_path: Path, patch: dict, revision: str) -> dict:
    """受限保存：只合并约定字段；revision 冲突 409；原字节唯一命名备份后原子替换。
    调用方需持有服务内操作互斥（本函数不做进程内加锁）。"""
    if not isinstance(patch, dict):
        raise ConfigError("invalid", "patch 必须是 JSON object")
    unknown = set(patch) - {"projects", "mail"}
    if unknown:
        raise ConfigError("invalid", f"拒绝未约定的请求键: {sorted(unknown)}")
    target = Path(cfg_path)
    data, raw, err = _read_raw_toml(target)
    if raw is None:
        if target.exists():
            raise ConfigError("conflict", f"目标无法读取，拒绝覆盖: {err}")
        # 首建：文件不存在。revision 必须是 "missing"，下方常规校验会核对。
    elif data is None:
        # 既有文件不是合法 TOML：有字节但解析失败。无论 revision 是否匹配
        # 都拒绝保存——备份存在不等于允许覆盖，页面不得提供可保存的空替代
        # 表单来重建它（复审 F1 反例 corrupt_config_overwrite）。
        raise ConfigError("invalid",
                          f"目标不是合法 TOML，拒绝覆盖（先人工修复原文件）: {err}")
    current = "missing" if raw is None else hashlib.sha256(raw).hexdigest()
    if revision != current:
        raise ConfigError("conflict",
                          "配置已被外部修改（revision 不一致），请重新加载后重试，不能覆盖")

    # 复审 R1：结构损坏的组件禁止经仪表板替换（防止覆盖未知内容）。read_config
    # 已按 schema_error 拒绝出编辑表单，这里守住 HTTP 直发同一边界；未触碰的
    # 组件不受影响，其未知内容经重新编码原样保留。
    if data is not None:
        broken: dict[str, str] = {}
        rp = data.get("projects")
        if rp is not None and not isinstance(rp, list):
            broken["projects"] = f"[projects] 应为数组，收到 {type(rp).__name__}"
        elif isinstance(rp, list):
            for i, p in enumerate(rp):
                if not isinstance(p, dict) or not isinstance(p.get("path"), str):
                    broken["projects"] = (f"[projects] 第 {i + 1} 条目缺少合法 path，"
                                          "结构损坏")
                    break
        mraw = data.get("mail")
        if mraw is not None and not isinstance(mraw, dict):
            broken["mail"] = f"[mail] 应为 TOML 表，收到 {type(mraw).__name__}"
        elif isinstance(mraw, dict) and "enabled" in mraw \
                and not isinstance(mraw["enabled"], bool):
            broken["mail"] = f"[mail] enabled 非布尔值，原文 {mraw['enabled']!r}"
        for comp in ("projects", "mail"):
            if comp in patch and comp in broken:
                raise ConfigError(
                    "invalid",
                    f"{broken[comp]}；该组件不可经仪表板替换，请人工修复原文件"
                    "（未知内容原样保留，本次未写入）")

    new_data = dict(data) if data is not None else {}
    if "projects" in patch:
        items = patch["projects"]
        if not isinstance(items, list):
            raise ConfigError("invalid", "projects 必须是数组")
        old = data.get("projects") if isinstance(data, dict) \
            and isinstance(data.get("projects"), list) else []
        merged: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {"path"} \
                    or not isinstance(item["path"], str) or not item["path"].strip():
                raise ConfigError("invalid", "每个项目只接受非空 path 字段")
            raw_path = item["path"]
            # 按原始输入判断绝对性：normalize_path 会把 "." 解析成进程 cwd，
            # 相对输入必须先拒绝（~ 展开仍是既定支持，展开后即为绝对）。
            if not Path(os.path.expanduser(raw_path)).is_absolute():
                raise ConfigError("invalid",
                                  f"项目路径必须是绝对路径: {raw_path}")
            norm = common.normalize_path(raw_path)
            if not norm.is_absolute() or not norm.is_dir():
                raise ConfigError("invalid", f"项目目录不存在或不是绝对路径: {raw_path}")
            key = str(norm)
            if key in seen:
                raise ConfigError("invalid", f"项目按规范路径去重后重复: {raw_path}")
            seen.add(key)
            keep = next((p for p in old if isinstance(p, dict)
                         and isinstance(p.get("path"), str)
                         and str(common.normalize_path(p["path"])) == key), None)
            merged.append(dict(keep) if keep is not None else {"path": raw_path})
        new_data["projects"] = merged

    if "mail" in patch:
        mpatch = patch["mail"]
        if not isinstance(mpatch, dict):
            raise ConfigError("invalid", "mail 必须是 JSON object")
        bad = set(mpatch) - ({"enabled"} | set(MAIL_EDIT_FIELDS))
        if bad:
            raise ConfigError("invalid", f"[mail] 拒绝未约定的请求键: {sorted(bad)}")
        base_mail = dict(data.get("mail")) if isinstance(data, dict) \
            and isinstance(data.get("mail"), dict) else {}
        for k, v in mpatch.items():
            if v is None:
                base_mail.pop(k, None)      # 显式 null = 清除该字段（草稿）
            else:
                base_mail[k] = v
        if base_mail.get("enabled") is True:
            if raw is None:
                # 复审 R1：首建只允许暂停草稿——不能靠页面禁用控件做唯一防线，
                # HTTP 直发同样拒绝；落盘后再显式启用。
                raise ConfigError(
                    "invalid",
                    "首建只允许创建暂停草稿（enabled=false）；"
                    "保存成功后再显式启用")
            check = {**new_data, "mail": base_mail}
            try:
                mail.load_mail_config(check)   # 完整校验（复用既有实现）
            except mail.MailConfigError as exc:
                raise ConfigError("invalid", f"拒绝启用自动邮件: {exc}") from exc
            pw_env = _password_env_of(base_mail)
            if not os.environ.get(pw_env, "").strip():
                raise ConfigError("invalid",
                                  f"拒绝启用自动邮件: 密码环境变量 {pw_env} 未设置"
                                  "（按 README 设置后重启相关进程）")
        else:
            problems = _mail_problems(base_mail)
            if problems:
                raise ConfigError("invalid",
                                  "草稿字段不合法: " + "；".join(problems))
        new_data["mail"] = base_mail

    try:
        encoded = _toml_encode(new_data)
    except _Lossy as exc:
        raise ConfigError("invalid",
                          f"无法无损保留现有配置（已拒绝保存，原文件未动）: {exc}") from exc
    try:
        reparsed = tomllib.loads(encoded)
    except ValueError as exc:  # pragma: no cover - 编码器自检
        raise ConfigError("invalid", f"编码自检失败（原文件未动）: {exc}") from exc
    if not _same_value(reparsed, new_data):
        raise ConfigError("invalid", "编码后与原配置不等价（已拒绝保存，原文件未动）")

    _guard_writable(target)
    try:  # 写入前再次核对 revision（临界区内最后一步校验）
        _, raw2, _ = _read_raw_toml(target)
        current2 = "missing" if raw2 is None else hashlib.sha256(raw2).hexdigest()
        if revision != current2:
            raise ConfigError("conflict", "写入前发现配置已被外部修改，已取消（原文件未动）")
        backup_path = None
        if raw2 is not None:
            bdir = Path(common.HQ_HOME) / ".local" / "backups" / "dashboard-config"
            bdir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_path = bdir / f"dashboard-config-{stamp}-{secrets.token_hex(4)}.toml"
            backup_path.write_bytes(raw2)
        common.write_text(target, encoded)
        new_raw = Path(target).read_bytes()
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError("conflict", f"写入失败（旧文件保持原状）: {exc}") from exc
    return {"ok": True, "revision": hashlib.sha256(new_raw).hexdigest(),
            "backup": str(backup_path) if backup_path else None, "target": str(target)}


# ---------------------------------------------------------------- 接入 / TEST


def _setup_ctx(cfg_path: Path) -> dict:
    ctx = mail.hook_ctx()
    ctx["hq_config"] = Path(cfg_path)
    return ctx


def _task_status_json(cfg_path: Path) -> dict:
    """计划任务只读查询：既有脚本的 -Status -Json。固定脚本路径/argv、
    CREATE_NO_WINDOW、10s 超时；不用 shell 字符串。失败结构化返回。"""
    ps = Path(common.HQ_HOME) / "scripts" / "install-mail-task.ps1"
    if not ps.is_file():
        return {"source": "error", "detail": f"查询脚本不存在: {ps}"}
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-File", str(ps),
           "-Status", "-Json", "-HqConfig", str(cfg_path)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=SETUP_TIMEOUT,
                           creationflags=flags)
    except subprocess.TimeoutExpired:
        return {"source": "error", "detail": f"任务查询超过 {SETUP_TIMEOUT}s 超时"}
    except OSError as exc:
        return {"source": "error", "detail": f"无法启动 PowerShell: {exc}"}
    if r.returncode != 0:
        tail = r.stderr.decode("utf-8", "replace").strip()[-300:]
        return {"source": "error", "detail": f"任务查询退出码 {r.returncode}: {tail}"}
    try:
        info = json.loads(r.stdout.decode("utf-8", "replace"))
    except ValueError as exc:
        return {"source": "error", "detail": f"任务查询输出不是 JSON: {exc}"}
    if not isinstance(info, dict) or info.get("ok") is not True:
        return {"source": "error", "detail": f"任务查询结果异常: {info}"}
    info["source"] = "ok"
    return info


def read_setup(cfg_path: Path) -> dict:
    """接入事实（只读）：观察条目 / hooks 开关 / 计划任务状态 / 密码存在布尔 /
    最近观察记录。不执行 apply_setup，不合并成"端到端健康"。
    配置缺失/损坏按组件诊断（复审 F1）：hook/task 照常查询，mail_config
    按生命周期状态如实显示，绝不把读不了的配置当成 complete/可启用。"""
    out: dict = {}
    try:
        cfg = common.load_config(cfg_path)
        info = mail.inspect_setup(cfg, _setup_ctx(cfg_path))
        # 边界方案 B3：inspect_setup 的两种既有错误渠道（抛异常、返回非空
        # parse_error）在这里统一归一为组件错误（source=error + detail）；
        # mail_present/hooks_enabled 一并置 None（未知），不把实际读取失败
        # 当成"未安装"或确定开关状态。合法缺文件/合法无条目（parse_error
        # 为空）才可显示未安装。mail.py 冻结不改。
        hook = {"target": info.get("target"), "exists": info.get("exists"),
                "old_gate": info.get("old_gate"),
                "other_handlers": info.get("other_handlers"),
                "parse_error": info.get("parse_error"),
                "note": "观察条目存在≠已通过自然触发验收；hooks.enabled 非 true 时不执行"}
        if info.get("parse_error"):
            hook.update({"source": "error", "detail": info["parse_error"],
                         "mail_present": None, "hooks_enabled": None})
        else:
            hook.update({"source": "ok",
                         "mail_present": info.get("mail_present"),
                         "hooks_enabled": info.get("hooks_enabled")})
        out["hook"] = hook
    except Exception as exc:                  # 配置坏/读不了不挡其他接入事实
        # 抛异常路径沿用同一错误形状（source=error + detail + 未知置 null）
        out["hook"] = {"source": "error",
                       "detail": f"无法读取配置进行接入检查: {type(exc).__name__}: {exc}",
                       "target": None, "exists": None, "mail_present": None,
                       "hooks_enabled": None,
                       "note": "观察条目存在≠已通过自然触发验收；hooks.enabled 非 true 时不执行"}
    out["task"] = _task_status_json(cfg_path)
    cfg_data, _, err = _read_raw_toml(cfg_path)
    mcfg = cfg_data.get("mail") if isinstance(cfg_data, dict) else None
    mail_err = None
    if mcfg is not None and not isinstance(mcfg, dict):
        mail_err = f"[mail] 应为 TOML 表，收到 {type(mcfg).__name__}"
        mcfg = {}
    pw_env = _password_env_of(mcfg if isinstance(mcfg, dict) else {})
    rc = read_config(cfg_path)
    rcv = rc.get("validation") or {}
    out["mail_config"] = {
        "state": rc.get("source"),
        "detail": rc.get("detail") or mail_err or err,
        # 复审 R1：可启用=无草稿问题且无启用问题；结构/类型损坏（schema_error）
        # 一律不算 complete
        "complete": rc.get("source") == "ok"
                    and not rcv.get("draft_problems")
                    and not rcv.get("enable_problems")}
    out["password"] = {"env": pw_env,
                       "present": bool(os.environ.get(pw_env, "").strip())}
    db, serr = _connect_ro()
    if db is not None:
        try:
            row = db.execute("SELECT MAX(observed_at_utc) AS m FROM events").fetchone()
            out["last_observed"] = row["m"] if row and row["m"] else None
        finally:
            _finish_ro(db)
    else:
        out["last_observed"] = None
        out["db_source"] = serr
    return out


def send_test(cfg_path: Path, *, sender=None, now: datetime | None = None) -> dict:
    """显式测试邮件：按已保存的 SMTP 配置发一封 TEST；校验时临时视 enabled 为
    true（不保存该临时值）。不运行 tick、不发送历史待发、不写业务库。"""
    cfg = common.load_config(cfg_path)
    mraw = dict(cfg.get("mail")) if isinstance(cfg.get("mail"), dict) else {}
    mraw["enabled"] = True                      # 临时校验口径
    mcfg = mail.load_mail_config({**cfg, "mail": mraw})
    now = now or datetime.now(timezone.utc)
    subject = f"[hq][TEST] 仪表板测试邮件 {mail.bj_str(now)}"
    body = ("这是从本机仪表板手动发送的 SMTP 配置测试邮件。\n"
            "收到只说明 SMTP 参数可用：不代表完成通知/日报已验证，"
            "也不代表收件箱到达。\n"
            f"生成时间：{mail.bj_str(now)}（北京时间）")
    mid = mail.message_id_for(f"dashboard-test:{mail.utc_iso(now)}", mcfg["from_addr"])
    result = {"to": mcfg["to_addr"], "at": mail.utc_iso(now)}
    try:
        if sender is not None:
            sender(mcfg, subject, body, mid)
        else:
            mail.send_mail(mcfg, subject, body, mid)
    except mail.MailPermanentError as exc:
        result.update({"status": "rejected", "detail": f"发送失败（未接受）: {exc}"})
    except mail.MailUnknownError as exc:
        result.update({"status": "unknown", "detail": f"结果待核对（不自动重发）: {exc}"})
    except Exception as exc:                     # 未预期异常同样按结果不明处理
        result.update({"status": "unknown",
                       "detail": f"结果待核对: {type(exc).__name__}: {exc}"[:200]})
    else:
        result.update({"status": "accepted",
                       "detail": "SMTP 已接受（不代表收件箱到达）"})
    return result


def read_delivery(key: str) -> dict | None:
    """参数化读取一条现存邮件（主题/正文/状态）；未知返回 None。
    库存在但不可读（WAL 拒绝/锁定/坏库）时抛 sqlite3.Error → HTTP 503，
    不伪装成"没有这条记录"（复审 F2）。"""
    db, serr = _connect_ro()
    if db is not None:
        try:
            r = db.execute("SELECT key, kind, subject, body, state, error,"
                           " created_at_utc AS created, sent_at_utc AS sent,"
                           " deadline_utc AS deadline FROM deliveries WHERE key=?",
                           (key,)).fetchone()
        finally:
            _finish_ro(db)
        if r is None:
            return None
        return dict(r)
    if serr != "missing":
        raise sqlite3.OperationalError(f"业务库不可读: {serr}")
    return None


# ---------------------------------------------------------------- HTTP 服务


class _App:
    """单次服务的共享状态：CSRF token、操作互斥、TEST 记录、接入缓存。"""

    def __init__(self, cfg_path: Path, token: str):
        self.cfg_path = Path(cfg_path)
        self.token = token
        self.op_lock = threading.Lock()          # 配置/TEST/接入刷新共用，不排队
        self.tests: dict[str, dict] = {}         # request_id → 结果（本进程内）
        self.setup_cache: dict | None = None
        self.setup_queried_at: str | None = None

    def test_summary(self) -> dict:
        """当前/最近一次 TEST 动作摘要；重启后为“本次尚未测试”。"""
        if not self.tests:
            return {"status": "not_tested", "detail": "本次进程尚未测试"}
        last = list(self.tests.values())[-1]
        return last

    def refresh_setup(self) -> dict:
        try:
            info = read_setup(self.cfg_path)
        except Exception as exc:                  # 接入查询失败不阻塞页面
            info = {"source": "error", "detail": f"{type(exc).__name__}: {exc}"}
        self.setup_cache = info
        self.setup_queried_at = mail.utc_iso(datetime.now(timezone.utc))
        return info


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# 复审 R3：访问日志只允许出现这个固定路由集合；集合外的任何路径
# （含查询串、未知路由、解析失败的伪造 requestline）一律记常量占位。
_KNOWN_ROUTES = frozenset({
    "/", "/api/snapshot", "/api/config", "/api/delivery",
    "/api/check-setup", "/api/test-mail",
})

# 边界方案（2026-10-08·关键判断1）：请求错误必须先在一个不会抛异常的边界
# 里定形，再分发路由。目标解析与 CL 解析都不使用 urlparse/int 直接处理
# 不可信输入（前者对畸形 authority 抛 ValueError 并把原文带进日志路径，
# 后者会触发解释器数字转换上限）。
_TARGET_MAX = 2048          # 本页最长目标（delivery key 编码后）的宽松上限


def _parse_target(raw) -> tuple[str, str] | None:
    """仅接受本页使用的 origin-form 请求目标：以 / 开始的 path[?query]，
    打印 ASCII（不含空格/控制字符/DEL/高位字节），不接受 fragment。
    合法返回 (path, query)；absolute-form、畸形 authority 等一律 None，
    由调用方结构化 400 并关连接。字符串操作不会对畸形输入抛异常。"""
    if not isinstance(raw, str) or not raw or not raw.startswith("/"):
        return None
    if len(raw) > _TARGET_MAX:
        return None
    if any(not 0x21 <= ord(c) <= 0x7E for c in raw):
        return None
    if "#" in raw:
        return None
    path, _, query = raw.partition("?")
    return path, query


def _content_length_bounded(v: str) -> int:
    """把已通过 ASCII 十进制校验的 CL 值转换成有界整数：超过 CONFIG_BODY_MAX
    返回哨兵 CONFIG_BODY_MAX+1。先用字符串长度限额分流，int 只转换一次，
    超长数字串不会触发 int 转换限制异常（B1）。"""
    digits = v.lstrip("0") or "0"
    if len(digits) > len(str(CONFIG_BODY_MAX)):
        return CONFIG_BODY_MAX + 1
    n = int(digits)
    return n if n <= CONFIG_BODY_MAX else CONFIG_BODY_MAX + 1


class _Handler(BaseHTTPRequestHandler):
    server_version = "hq-dashboard/1"
    protocol_version = "HTTP/1.1"

    # ---- 安全与输出 ----

    BODY_BUDGET_SECONDS = 5

    @contextlib.contextmanager
    def _body_budget(self):
        """正文阶段 5 秒总预算（边界方案 B1）：从进入本块到结束用 monotonic
        截止点，每次有限读取只获得剩余时间——读取原语不会在内部不断刷新
        同一个 5 秒超时。预算只施加于正文读取/排空；keep-alive 空闲等下一
        请求不设超时，否则本页 10s 轮询的复用连接会被服务端提前掐断
        （返工2现场复现）。预算耗尽抛 TimeoutError：结束连接，不进入业务
        分发；拒绝路径的排空让位于及时回应。"""
        old = self.connection.gettimeout()
        deadline = time.monotonic() + self.BODY_BUDGET_SECONDS
        try:
            yield deadline
        finally:
            try:
                self.connection.settimeout(old)
            except OSError:
                pass

    def _read_bounded(self, n: int, deadline: float) -> bytes:
        """总预算内读取至多 n 字节；返回实际读到的字节（短读=对端提前收尾）。
        读取粒度是单字节：socket 超时按"每次 recv"生效，而 BufferedReader 的
        大块 read 内部会循环 recv 并沿用陈旧超时（等价于不断刷新超时，总预算
        形同虚设）；被超时打断的部分读取还会丢失已缓冲字节。逐字节读取保证
        每次 recv 的超时就是当前剩余预算。耗尽抛 TimeoutError。"""
        out = bytearray()
        while len(out) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("正文阶段总预算耗尽")
            try:
                self.connection.settimeout(remaining)
            except OSError as exc:
                raise ConnectionError(str(exc)) from exc
            chunk = self.rfile.read(1)
            if not chunk:
                break
            out += chunk
        return bytes(out)

    def _parse_framing(self, write: bool) -> tuple[int | None, str | None]:
        """路由/副作用之前统一解析正文 framing（复审 R3 + 边界方案 B1）。
        返回 (declared, err)：declared 为可信 Content-Length——缺失=0、
        合法=0..CONFIG_BODY_MAX、超过正文上限=CONFIG_BODY_MAX+1（哨兵，
        供 GET 400 / POST 413 分流与有界排空使用）；err 非 None 时 declared
        不可信（歧义/非法长度不靠读第一项推测 body，不排空直接关）。本服务
        无 chunked 支持，含 Transfer-Encoding 一律拒绝。"""
        if self.headers.get("Transfer-Encoding") is not None:
            return None, "请求含 Transfer-Encoding，本服务无 chunked 支持，拒绝"
        cls = self.headers.get_all("Content-Length", []) or []
        if len(cls) > 1:
            return None, "Content-Length 重复出现，拒绝歧义长度"
        if not cls:
            return 0, None
        v = cls[0].strip()
        if not (v.isascii() and v.isdigit()) or not v:
            return None, "Content-Length 非法"
        return _content_length_bounded(v), None

    def _security_error(self, write: bool) -> str | None:
        port = self.server.server_address[1]
        expected = f"http://127.0.0.1:{port}"
        host = self.headers.get("Host", "")
        if host.lower() != f"127.0.0.1:{port}":
            return f"Host 不匹配（{host or '缺失'}）"
        origin = self.headers.get("Origin")
        if origin is not None and origin != expected:
            return "拒绝跨站 Origin"
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None and sfs not in ("same-origin", "none"):
            return f"拒绝跨站请求（Sec-Fetch-Site={sfs}）"
        if write:
            if origin != expected:
                return "写请求缺少精确同源 Origin"
            ct = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ct != "application/json":
                return "写请求 Content-Type 必须是 application/json"
            if self.headers.get("X-HQ-CSRF") != self.server.app.token:
                return "CSRF token 无效"
        return None

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        port = self.server.server_address[1]
        token = self.server.app.token
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'nonce-" + token + "'; "
                         "style-src 'nonce-" + token + "'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)
    def _send_json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8",
                   extra=extra)

    def _error(self, status: int, kind: str, detail: str) -> None:
        self._send_json(status, {"error": {"kind": kind, "detail": detail}})

    def _reject(self, status: int, kind: str, detail: str) -> None:
        """安全拒绝：响应后必须关闭连接。此时尚未消费请求体（或请求体本身
        不可信），留着它会被 HTTP/1.1 keep-alive 当成下一个请求解析——正文
        会泄漏进日志（复审 F5 反例）。拒绝路径一律 Connection: close。"""
        self._send_json(status, {"error": {"kind": kind, "detail": detail}},
                        extra={"Connection": "close"})
        self.close_connection = True

    def log_message(self, fmt, *args):
        """只记固定路由集合与方法白名单（复审 R3/B1）：本方法不调用 urlparse
        等可能对不可信输入抛异常的解析器——路由判定只做集合成员比较（str 操作
        不会抛异常），未验证/未知/解析失败一律常量占位，协议解析尚未产生合法
        target 时同样安全记常量。被拒请求的未读正文可能已被解析器当成
        method/requestline，绝不能原样输出（复审 F5）；ASCII/长度校验不能
        代替路由白名单。"""
        cmd = getattr(self, "command", None)
        cmd = cmd if cmd in ("GET", "POST", "HEAD") else "<invalid>"
        raw = getattr(self, "path", None)
        path = raw.split("?", 1)[0] if isinstance(raw, str) else ""
        path = path if path in _KNOWN_ROUTES else "<unparsed>"
        sys.stderr.write("[hq dashboard] %s %s\n" % (cmd, path))

    def _drain_bounded(self, n: int, deadline: float,
                       cap: int = 1024 * 1024) -> None:
        """有界消费已声明长度的正文：让合法客户端（如触到 413 上限的浏览器、
        被拒请求的发送方）能写完正文并读到拒绝响应，而不是在写入中途被 RST
        打断。超过上限不再消费（由连接关闭兜底）；总预算耗尽提前放弃排空、
        让位给及时回应（无法排空时不无限等待）。与 _read_bounded 同理按单
        字节读取，每次 recv 的超时是当前剩余预算（大块读取会在缓冲层内部
        循环 recv 并沿用陈旧超时）。"""
        remaining = min(max(n, 0), cap)
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    return
                try:
                    self.connection.settimeout(budget)
                except OSError:
                    return
                chunk = self.rfile.read(1)
                if not chunk:
                    break
                remaining -= 1
        except (ConnectionError, TimeoutError, OSError):
            pass

    def _drain_declared(self, declared: int | None) -> None:
        """拒绝路径按已解析的可信 Content-Length 有界排空：既不让未读正文
        被当成下一个请求解析（F5），也避免"带未读数据关连接"在 Windows 上
        触发 RST 使合法客户端读不到响应。声明不可信（None/0）则不排空。"""
        if not declared:
            return
        with self._body_budget() as deadline:
            self._drain_bounded(declared, deadline)

    def _refuse(self, declared: int | None, status: int, kind: str,
                detail: str):
        """统一拒绝路径：先判拒绝，再在正文总预算内有限排空，然后回应并关
        连接（更正旧"读取正文前拒绝"口径——是先判定拒绝，可有限排空后返回，
        不是不读就拒绝）。declared 不可信时跳过排空直接关。"""
        self._drain_declared(declared)
        return self._reject(status, kind, detail)

    def _read_body(self, declared: int) -> dict:
        """读取并解析请求正文（declared 已由 _parse_framing 校验为单个合法
        值，≤CONFIG_BODY_MAX）。读取在正文总预算内；预算耗尽抛 TimeoutError
        结束连接，不进入业务分发。"""
        if declared <= 0:
            raise ValueError("缺少请求体")
        with self._body_budget() as deadline:
            raw = self._read_bounded(declared, deadline)
        if len(raw) < declared:
            raise ValueError("请求体不完整（连接提前结束）")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON object")
        return obj

    # ---- 路由 ----

    def do_GET(self):
        try:
            return self._do_get()
        except sqlite3.Error as exc:
            return self._reject(503, "db_error", f"业务库读取失败: {exc}")

    def _do_get(self):
        # 边界方案 B1：framing → target 合法性 → 安全检查 → 路由分发，全部
        # 在不会抛异常的解析边界之后。拒绝即关连接；可信声明长度先有限排空，
        # 让客户端可靠读到响应；不可信则不排空直接关。
        declared, ferr = self._parse_framing(write=False)
        if ferr:
            return self._reject(400, "invalid", ferr)
        t = _parse_target(getattr(self, "path", None))
        if t is None:
            return self._refuse(declared, 400, "invalid",
                                "仅支持 origin-form 请求目标（以 / 开始）")
        if declared > 0:
            return self._refuse(declared, 400, "invalid", "GET 不接受请求正文")
        err = self._security_error(write=False)
        if err:
            return self._refuse(declared, 403, "forbidden", err)
        path, query = t
        qs = parse_qs(query)
        if path == "/":
            return self._send(200, self.server.page, "text/html; charset=utf-8")
        if path == "/api/snapshot":
            return self._get_snapshot(qs)
        if path == "/api/config":
            return self._send_json(200, read_config(self.server.app.cfg_path))
        if path == "/api/delivery":
            key = (qs.get("key") or [""])[0]
            if not key or len(key) > 200:
                return self._reject(404, "not_found", "key 缺失或非法")
            row = read_delivery(key)
            if row is None:
                return self._reject(404, "not_found", "没有这条邮件记录")
            return self._send_json(200, row)
        return self._reject(404, "not_found", f"未知路径: {path}")

    def _get_snapshot(self, qs: dict):
        now = datetime.now(timezone.utc)
        try:
            project = (qs.get("project") or [None])[0]
            period = (qs.get("period") or ["24h"])[0]
            attention = (qs.get("attention") or ["0"])[0] in ("1", "true", "True")
            offset = max(0, int((qs.get("offset") or ["0"])[0]))
            limit = int((qs.get("limit") or [str(DEFAULT_PAGE_LIMIT)])[0])
            daily_offset = max(0, int((qs.get("daily_offset") or ["0"])[0]))
            daily_limit = int((qs.get("daily_limit")
                               or [str(DAILY_PAGE_DEFAULT)])[0])
        except ValueError:
            return self._reject(400, "invalid", "分页参数必须是整数")
        if period not in PERIODS:
            return self._reject(400, "invalid", f"period 只支持 {'/'.join(PERIODS)}")
        if not 1 <= limit <= PAGE_LIMIT_MAX:
            return self._reject(400, "invalid",
                                f"limit 必须在 1-{PAGE_LIMIT_MAX}")
        try:
            snap = read_snapshot(self.server.app.cfg_path, now=now, project=project,
                                 period=period, attention=attention, offset=offset,
                                 limit=limit, daily_offset=daily_offset,
                                 daily_limit=daily_limit)
        except ValueError as exc:
            return self._reject(400, "invalid", str(exc))
        app: _App = self.server.app
        snap["test"] = app.test_summary()
        snap["setup"] = app.setup_cache
        snap["setup_queried_at"] = app.setup_queried_at
        self._send_json(200, snap)

    def do_POST(self):
        try:
            return self._do_post()
        except sqlite3.Error as exc:
            return self._reject(503, "db_error", f"业务库读取失败: {exc}")

    def _do_post(self):
        # 边界方案 B1：与 GET 同一解析边界。通过后 CL 必为单个有界合法值，
        # 413 排空与 403 排空才有可信边界；超长数字串经字符串长度限额分流，
        # 不触发 int 转换限制异常。
        declared, ferr = self._parse_framing(write=True)
        if ferr:
            return self._reject(400, "invalid", ferr)
        t = _parse_target(getattr(self, "path", None))
        if t is None:
            return self._refuse(declared, 400, "invalid",
                                "仅支持 origin-form 请求目标（以 / 开始）")
        err = self._security_error(write=True)
        if err:
            return self._refuse(declared, 403, "forbidden", err)
        path, _ = t
        if declared > CONFIG_BODY_MAX:
            return self._refuse(declared, 413, "too_large",
                                f"请求体超过 {CONFIG_BODY_MAX} 字节上限")
        try:
            body = self._read_body(declared)
        except ValueError as exc:
            return self._reject(400, "invalid", str(exc))
        app: _App = self.server.app
        if path == "/api/config":
            return self._post_config(app, body)
        if path == "/api/check-setup":
            return self._post_check_setup(app, body)
        if path == "/api/test-mail":
            return self._post_test(app, body)
        return self._reject(404, "not_found", f"未知路径: {path}")

    def _post_config(self, app: _App, body: dict):
        if app.op_lock.acquire(blocking=False):
            try:
                revision = body.get("revision")
                patch = body.get("patch")
                if not isinstance(revision, str) or not isinstance(patch, dict):
                    return self._reject(400, "invalid", "需要 revision 与 patch 字段")
                try:
                    result = save_config(app.cfg_path, patch, revision)
                except ConfigError as exc:
                    status = 409 if exc.kind == "conflict" else 400
                    return self._reject(status, exc.kind, exc.detail)
                result["config"] = read_config(app.cfg_path)   # 保存后回读给表单
                return self._send_json(200, result)
            finally:
                app.op_lock.release()
        return self._reject(409, "busy", "另一个写操作正在执行，未排队")

    def _post_check_setup(self, app: _App, body: dict):
        if body:
            return self._reject(400, "invalid", "本接口不接受业务参数")
        if app.op_lock.acquire(blocking=False):
            try:
                info = app.refresh_setup()
                return self._send_json(200, {"ok": True, "setup": info,
                                             "queried_at": app.setup_queried_at})
            finally:
                app.op_lock.release()
        return self._reject(409, "busy", "另一个写操作正在执行，未排队")

    def _post_test(self, app: _App, body: dict):
        rid = body.get("request_id")
        if not isinstance(rid, str) or not rid.strip() or len(rid) > TEST_REQUEST_ID_MAX:
            return self._reject(400, "invalid",
                               f"request_id 必须是 1-{TEST_REQUEST_ID_MAX} 字符")
        rid = rid.strip()
        if rid in app.tests:                     # 重复 ID：返回原状态，不再外发
            return self._send_json(200, {**app.tests[rid], "duplicate": True})
        if app.op_lock.acquire(blocking=False):
            try:
                if rid in app.tests:             # 双重检查（并发同 ID）
                    return self._send_json(200, {**app.tests[rid], "duplicate": True})
                running = {"status": "running", "detail": "发送中…", "at":
                           mail.utc_iso(datetime.now(timezone.utc)), "request_id": rid}
                app.tests[rid] = running
                try:
                    result = send_test(app.cfg_path)
                except mail.MailConfigError as exc:
                    result = {"status": "rejected",
                              "detail": f"配置不完整，未发送: {exc}",
                              "at": mail.utc_iso(datetime.now(timezone.utc))}
                except Exception as exc:          # 兜底：结果不明，不出 traceback
                    result = {"status": "unknown",
                              "detail": f"结果待核对: {type(exc).__name__}: {exc}"[:200],
                              "at": mail.utc_iso(datetime.now(timezone.utc))}
                result["request_id"] = rid
                app.tests[rid] = result          # 只保留状态/摘要，不含秘密
                return self._send_json(200, result)
            finally:
                app.op_lock.release()
        return self._reject(409, "busy", "另一个写操作正在执行，未排队")


def _load_page() -> bytes:
    path = Path(__file__).resolve().parent / "web" / "dashboard.html"
    if not path.is_file():
        raise SystemExit(f"! hq dashboard: 页面文件缺失: {path}")
    html = path.read_text(encoding="utf-8")
    if "@@TOKEN@@" not in html:
        raise SystemExit("! hq dashboard: 页面缺少 @@TOKEN@@ 占位（CSP nonce/CSRF）")
    return html.encode("utf-8")


class _Server(ThreadingHTTPServer):
    allow_reuse_address = False     # Windows 上避免二次绑定同一端口
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 断连/读写超时是本机服务的正常噪音，不刷未处理 traceback（复审 F5）；
        # 其他异常仍完整记录。
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve(*, port: int, open_browser: bool) -> int:
    """固定 loopback 单页服务：绑定 127.0.0.1:port；端口占用明确报错。
    退出终端/Ctrl+C 即结束；不自动安装任何启动任务。"""
    cfg_path = Path(common.CONFIG_PATH)
    app = _App(cfg_path, secrets.token_urlsafe(32))
    page = _load_page().replace(b"@@TOKEN@@", app.token.encode("ascii"))
    try:
        httpd = _Server(("127.0.0.1", port), _Handler)
    except OSError as exc:
        print(f"! hq dashboard: 端口 {port} 无法监听（{exc}）。"
              "可能已被占用（如另一个仪表板实例），请用 --port 换端口；"
              "不会随机换端口或结束其他进程。", file=sys.stderr)
        return 1
    httpd.app = app
    httpd.page = page
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"hq dashboard：{url}（仅本机可访问）")
    print(f"  配置目标：{cfg_path}")
    print("  刷新只读不触发 tick；自动邮件仍由既有计划任务负责。退出终端即结束。")
    threading.Thread(target=app.refresh_setup, daemon=True).start()  # 初始化接入缓存
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            print("  （自动打开浏览器失败，请手动访问上面的地址）", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print("hq dashboard 已退出。")
    return 0
