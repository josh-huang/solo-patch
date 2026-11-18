"""Operator CLI and systemd entry point."""

import argparse
import json
import logging
import os
import re
import sys
from contextlib import closing
from datetime import datetime

import psycopg2

import store
import worker


def natural(value):
    if not value.isascii() or not value.isdecimal() or len(value) > 19:
        raise argparse.ArgumentTypeError("需要非负整数")
    number = int(value)
    if number > 2**63 - 1:
        raise argparse.ArgumentTypeError("整数过大")
    return number


def positive(value):
    number = natural(value)
    if number == 0:
        raise argparse.ArgumentTypeError("需要正整数")
    return number


def count_limit(value):
    number = positive(value)
    if number > 200:
        raise argparse.ArgumentTypeError("最多查询 200 条")
    return number


def duration(value):
    match = re.fullmatch(r"([0-9]{1,8})([smhd])", value)
    if not match:
        raise argparse.ArgumentTypeError("用 30s、15m、2h 或 1d 这样的格式")
    amount, unit = match.groups()
    seconds = int(amount) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if not 1 <= seconds <= store.MAX_INTERVAL:
        raise argparse.ArgumentTypeError("时间间隔应在 1 秒到 365 天之间")
    return seconds


def date_time(value):
    try:
        stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if stamp.utcoffset() is not None:
            return int(stamp.timestamp())
    except (ValueError, OverflowError, OSError):
        pass
    raise argparse.ArgumentTypeError("使用带时区的 ISO 时间，例如 2026-12-01T09:00:00+08:00")


def ask_args(command):
    command.add_argument("text", help="文本内容，用 - 从 stdin 读取")
    command.add_argument("--session", default="main", help="会话名称，默认 main")


def parser():
    root = argparse.ArgumentParser(description="单机常驻 Claude 助手，输出为 JSON。")
    commands = root.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="提交一条任务")
    ask_args(ask)
    ask.add_argument("--key", help="可选去重键，重复的相同任务返回已有 ID")
    schedule = commands.add_parser("schedule", help="添加一次性或固定间隔的定时任务")
    ask_args(schedule)
    starts = schedule.add_mutually_exclusive_group(required=True)
    starts.add_argument("--in", dest="delay", type=duration, help="首次执行前的延时，例如 1h")
    starts.add_argument("--at", type=date_time, help="首次执行时间，包含时区")
    schedule.add_argument("--every", type=duration, help="重复间隔，例如 1d，至少 60 秒")
    for name, help_text in [
        ("status", "查看队列和今天的 API 用量"), ("timers", "查看定时器"),
        ("memory", "查看已保存记忆"), ("init-db", "初始化数据库表"),
        ("worker", "运行常驻 worker"),
    ]:
        commands.add_parser(name, help=help_text)

    for name, help_text in [("outbox", "查看回执"), ("events", "查看队列与任务历史")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--session", help="只查看指定会话")
        command.add_argument("--limit", type=count_limit, default=30)
        if name == "outbox":
            command.add_argument("--after", type=natural, help="获取此回执 ID 之后的记录")
    github_run = commands.add_parser("github-run", help="立即处理配置仓库中的一个 issue")
    github_run.add_argument("--apply", action="store_true", help="实际创建 Draft PR，默认仅生成预览")
    github_schedule = commands.add_parser("github-schedule", help="定期处理 issue 并创建 Draft PR")
    github_schedule.add_argument("--in", dest="delay", type=duration, default=60, help="首次触发前的延时")
    github_schedule.add_argument("--every", type=duration, default=259200, help="重复间隔，默认 3d")
    approvals = commands.add_parser("approvals", help="查看待审批的实际参数")
    approvals.add_argument("--all", action="store_true", help="包含已处理的审批")
    approvals.add_argument("--limit", type=count_limit, default=30)

    for name, help_text in [
        ("show", "查看单条任务和 checkpoint 状态"), ("approve", "批准原样执行这条请求"),
        ("reject", "拒绝这条请求"), ("cancel-timer", "取消定时器及其未运行的任务"),
        ("cancel", "取消排队中或失败的任务"), ("retry", "重新排队失败任务"),
    ]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id", type=positive)
        if name == "retry":
            command.add_argument("--fresh", action="store_true", help="丢弃旧 checkpoint，用当前配置从第一轮运行")

    instructions = commands.add_parser("instructions", help="查看或设定长期指令")
    instructions.add_argument("text", nargs="?", help="省略时读取，空字符串清空，用 - 从 stdin 读取")
    forget = commands.add_parser("forget", help="删除一条已批准的长期记忆")
    forget.add_argument("key")
    return root


def read_text(value, limit, label, allow_empty=False):
    if value == "-":
        value = sys.stdin.read(limit + 1)
    return worker.text_value(value, limit, label, allow_empty)


def present(row):
    result = dict(row)
    for field in ("created_at", "available_at", "due_at", "finished_at", "decided_at", "updated_at", "checkpoint_at"):
        if result.get(field) is not None:
            result[field] = store.iso(result[field])
    if "payload" in result:
        result["payload"] = json.loads(result["payload"])
    if result.get("kind") in ("approval", "decision") and "body" in result:
        result["body"] = json.loads(result["body"])
    return result


def retry_or_cancel(db, args, timestamp):
    with store.transaction(db):
        if args.command == "retry":
            row = db.execute(
                """UPDATE events SET status = 'queued', attempts = 0, available_at = %s,
                   error = NULL, finished_at = NULL WHERE id = %s AND status = 'failed' RETURNING id""",
                (timestamp, args.id),
            ).fetchone()
            if row is None:
                raise ValueError("只能重试 failed 状态的任务")
            if args.fresh:
                db.execute("DELETE FROM checkpoints WHERE event_id = %s", (args.id,))
            return {"event_id": args.id, "status": "queued"}
        row = db.execute(
            """UPDATE events SET status = 'cancelled', finished_at = %s
               WHERE id = %s AND status IN ('queued', 'failed') RETURNING id""",
            (timestamp, args.id),
        ).fetchone()
        if row is None:
            raise ValueError("只能取消排队中或失败的任务")
        db.execute("DELETE FROM checkpoints WHERE event_id = %s", (args.id,))
        return {"event_id": args.id, "status": "cancelled"}


def execute(db, args):
    command = args.command
    timestamp = store.now()
    if command == "init-db":
        store.init_schema(db)
        return {"database": "ready"}
    if command == "worker":
        worker.serve(db)
        return None
    if command == "github-run":
        return {"event_id": store.enqueue(db, "github", "github", "apply" if args.apply else "preview", timestamp)}
    if command == "github-schedule":
        if args.every < 3600:
            raise ValueError("GitHub 任务的重复间隔至少 1 小时")
        due = timestamp + args.delay
        with store.transaction(db):
            timer_id = store.add_timer(db, "github", "apply", due, args.every, kind="github")
        return present({"timer_id": timer_id, "due_at": due, "interval_seconds": args.every})
    if command in ("ask", "schedule"):
        body = read_text(args.text, store.MAX_TEXT, "任务")
        session = worker.key_value(args.session)
        if command == "ask":
            dedupe = "user:" + worker.key_value(args.key) if args.key is not None else None
            return {"event_id": store.enqueue(db, "user", session, body, timestamp, dedupe)}
        due = timestamp + args.delay if args.delay is not None else args.at
        if not timestamp < due <= timestamp + store.MAX_INTERVAL:
            raise ValueError("首次执行时间必须在未来 365 天内")
        if args.every is not None and args.every < 60:
            raise ValueError("重复间隔至少 60 秒")
        with store.transaction(db):
            timer_id = store.add_timer(db, session, body, due, args.every)
        return present({"timer_id": timer_id, "due_at": due, "interval_seconds": args.every})
    if command == "status":
        counts = db.execute("SELECT status, COUNT(*) AS count FROM events GROUP BY status").fetchall()
        usage = db.execute("SELECT * FROM usage WHERE day = %s", (store.utc_day(timestamp),)).fetchone()
        waiting = db.execute("SELECT COUNT(*) AS count FROM approvals WHERE status = 'pending'").fetchone()
        return {
            "events": {row["status"]: row["count"] for row in counts},
            "pending_approvals": waiting["count"],
            "usage_utc": dict(usage) if usage else {
                "day": store.utc_day(timestamp), "requests": 0, "input_tokens": 0, "output_tokens": 0,
            },
        }
    if command == "events":
        session = worker.key_value(args.session) if args.session is not None else None
        rows = db.execute(
            """SELECT id, kind, session, status, attempts, created_at, available_at, finished_at, error
               FROM events WHERE (%s IS NULL OR session = %s) ORDER BY id DESC LIMIT %s""",
            (session, session, args.limit),
        ).fetchall()
        return [present(row) for row in rows]
    if command == "outbox":
        session = worker.key_value(args.session) if args.session is not None else None
        if args.after is None:
            rows = db.execute(
                "SELECT * FROM outbox WHERE (%s IS NULL OR session = %s) ORDER BY id DESC LIMIT %s",
                (session, session, args.limit),
            ).fetchall()
            return [present(row) for row in reversed(rows)]
        rows = db.execute(
            "SELECT * FROM outbox WHERE id > %s AND (%s IS NULL OR session = %s) ORDER BY id LIMIT %s",
            (args.after, session, session, args.limit),
        ).fetchall()
        return [present(row) for row in rows]
    if command == "show":
        row = db.execute(
            """SELECT e.*, c.updated_at AS checkpoint_at, (c.state->>'rounds')::int AS checkpoint_rounds
               FROM events e LEFT JOIN checkpoints c ON c.event_id = e.id WHERE e.id = %s""",
            (args.id,),
        ).fetchone()
        if row is None:
            raise ValueError("找不到该任务")
        return present(row)
    if command == "timers":
        rows = db.execute("SELECT * FROM timers ORDER BY (status = 'active') DESC, due_at DESC LIMIT 200").fetchall()
        return [present(row) for row in rows]
    if command == "cancel-timer":
        return {"timer_id": args.id, "cancelled_events": store.cancel_timer(db, args.id, timestamp)}
    if command in ("cancel", "retry"):
        return retry_or_cancel(db, args, timestamp)
    if command == "approvals":
        rows = db.execute(
            "SELECT * FROM approvals WHERE (%s OR status = 'pending') ORDER BY id DESC LIMIT %s",
            (args.all, args.limit),
        ).fetchall()
        return [present(row) for row in rows]
    if command in ("approve", "reject"):
        return store.decide(db, args.id, "approved" if command == "approve" else "rejected", timestamp)
    if command == "memory":
        rows = db.execute("SELECT * FROM memory ORDER BY updated_at DESC, key").fetchall()
        return [present(row) for row in rows]
    if command == "forget":
        key = worker.key_value(args.key)
        return {"key": key, "deleted": bool(db.execute("DELETE FROM memory WHERE key = %s", (key,)).rowcount)}
    if args.text is not None:
        value = read_text(args.text, 3000, "长期指令", allow_empty=True)
        db.execute(
            """INSERT INTO settings(key, value) VALUES ('instructions', %s)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (value,),
        )
    else:
        row = db.execute("SELECT value FROM settings WHERE key = 'instructions'").fetchone()
        value = row["value"] if row else ""
    return {"instructions": value}


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        with closing(store.connect(os.environ.get("ALWAYSON_DATABASE_URL", store.DEFAULT_DSN))) as conn:
            with conn.cursor() as db:
                result = execute(db, args)
        if result is not None:
            print(store.dumps(result))
        return 0
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except psycopg2.Error as exc:
        detail = exc.diag.message_primary
        message = f"数据库操作失败（SQLSTATE {exc.pgcode or '连接错误'}）。"
        if detail and exc.pgcode:
            message += detail
        print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
