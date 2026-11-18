"""PostgreSQL persistence for one operator and one worker process."""

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import Json, RealDictCursor

DEFAULT_DSN = "dbname=alwayson user=alwayson host=/var/run/postgresql"
MAX_ATTEMPTS = 3
MAX_TEXT = 8000
MAX_INTERVAL = 365 * 86400
MAX_TIMERS = 64
MAX_MEMORY = 100
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL,
    session TEXT NOT NULL,
    body TEXT NOT NULL,
    dedupe TEXT UNIQUE,
    timer_id BIGINT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at BIGINT NOT NULL,
    created_at BIGINT NOT NULL,
    finished_at BIGINT,
    error TEXT,
    reply TEXT
);
CREATE INDEX IF NOT EXISTS events_ready ON events(status, available_at, id);
CREATE INDEX IF NOT EXISTS events_session ON events(session, id)
    WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS events_history ON events(session, id DESC) WHERE status = 'done';
CREATE INDEX IF NOT EXISTS events_timer ON events(timer_id)
    WHERE status IN ('queued', 'running') AND timer_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS checkpoints (
    event_id BIGINT PRIMARY KEY REFERENCES events(id),
    state JSONB NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS timers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session TEXT NOT NULL,
    body TEXT NOT NULL,
    due_at BIGINT NOT NULL,
    interval_seconds INTEGER,
    event_kind TEXT NOT NULL DEFAULT 'timer',
    status TEXT NOT NULL DEFAULT 'active'
);
ALTER TABLE timers ADD COLUMN IF NOT EXISTS event_kind TEXT NOT NULL DEFAULT 'timer';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_github_schedule ON timers(event_kind)
    WHERE event_kind = 'github' AND status = 'active';
CREATE TABLE IF NOT EXISTS github_issues (
    repo TEXT NOT NULL,
    issue BIGINT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('skipped', 'published')),
    pr_url TEXT,
    PRIMARY KEY (repo, issue)
);
CREATE TABLE IF NOT EXISTS outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id),
    session TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_session ON outbox(session, id);
CREATE TABLE IF NOT EXISTS approvals (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id BIGINT NOT NULL REFERENCES events(id),
    session TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at BIGINT NOT NULL,
    decided_at BIGINT
);
CREATE TABLE IF NOT EXISTS memory (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
    session TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY,
    requests INTEGER NOT NULL DEFAULT 0,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0
);
"""


def now():
    return int(time.time())


def utc_day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


class Cursor(RealDictCursor):
    def execute(self, query, vars=None):
        super().execute(query, vars)
        return self


def connect(dsn):
    db = psycopg2.connect(
        dsn, cursor_factory=Cursor, connect_timeout=5, application_name="alwayson",
        options="-c statement_timeout=10000 -c idle_in_transaction_session_timeout=15000",
    )
    db.autocommit = True
    return db


@contextmanager
def transaction(db):
    db.execute("BEGIN")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        if not db.connection.closed:
            db.execute("ROLLBACK")
        raise


def init_schema(db):
    with transaction(db):
        db.execute("SELECT pg_advisory_xact_lock(349811, 0)")
        db.execute(SCHEMA)


def enqueue(db, kind, session, body, timestamp, dedupe=None, timer_id=None):
    cursor = db.execute(
        """INSERT INTO events(kind, session, body, available_at, created_at, dedupe, timer_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT(dedupe) DO NOTHING RETURNING id""",
        (kind, session, body, timestamp, timestamp, dedupe, timer_id),
    )
    if cursor.rowcount:
        return cursor.fetchone()["id"]
    existing = db.execute("SELECT * FROM events WHERE dedupe = %s", (dedupe,)).fetchone()
    if (existing["kind"], existing["session"], existing["body"]) != (kind, session, body):
        raise ValueError("这个去重键已用于另一条任务")
    return existing["id"]


def add_timer(db, session, body, due, interval=None, kind='timer'):
    db.execute("SELECT pg_advisory_xact_lock(349811, 2)")
    if kind == "github" and db.execute(
        "SELECT 1 FROM timers WHERE event_kind = 'github' AND status = 'active'"
    ).fetchone():
        raise ValueError("已有 GitHub 定时任务，请先取消它")
    active = db.execute("SELECT COUNT(*) AS count FROM timers WHERE status = 'active'").fetchone()["count"]
    if active >= MAX_TIMERS:
        raise ValueError(f"最多允许 {MAX_TIMERS} 个活动定时器")
    return db.execute(
        """INSERT INTO timers(session, body, due_at, interval_seconds, event_kind)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (session, body, due, interval, kind),
    ).fetchone()["id"]


def cancel_timer(db, timer_id, timestamp):
    with transaction(db):
        cursor = db.execute("UPDATE timers SET status = 'cancelled' WHERE id = %s AND status != 'cancelled'", (timer_id,))
        if not cursor.rowcount:
            raise ValueError("找不到可取消的定时器")
        cancelled = db.execute(
            """UPDATE events SET status = 'cancelled', finished_at = %s
               WHERE timer_id = %s AND status = 'queued' RETURNING id""",
            (timestamp, timer_id),
        ).fetchall()
        for event in cancelled:
            db.execute("DELETE FROM checkpoints WHERE event_id = %s", (event["id"],))
        return len(cancelled)


def tick(db, timestamp):
    with transaction(db):
        due = db.execute(
            "SELECT * FROM timers WHERE status = 'active' AND due_at <= %s ORDER BY due_at, id FOR UPDATE",
            (timestamp,),
        ).fetchall()
        for timer in due:
            pending = db.execute(
                "SELECT 1 FROM events WHERE timer_id = %s AND status IN ('queued', 'running') LIMIT 1",
                (timer["id"],),
            ).fetchone()
            if not pending:
                enqueue(
                    db, timer["event_kind"], timer["session"], timer["body"], timestamp,
                    dedupe=f"timer:{timer['id']}:{timer['due_at']}", timer_id=timer["id"],
                )
            interval = timer["interval_seconds"]
            if interval is None:
                db.execute("UPDATE timers SET status = 'fired' WHERE id = %s", (timer["id"],))
            else:
                next_due = timer["due_at"] + ((timestamp - timer["due_at"]) // interval + 1) * interval
                db.execute("UPDATE timers SET due_at = %s WHERE id = %s", (next_due, timer["id"]))


def emit(db, event, kind, body, timestamp):
    db.execute(
        "INSERT INTO outbox(event_id, session, kind, body, created_at) VALUES (%s, %s, %s, %s, %s)",
        (event["id"], event["session"], kind, body, timestamp),
    )


def claim(db, timestamp):
    with transaction(db):
        event = db.execute(
            """SELECT e.id FROM events e WHERE e.status = 'queued' AND e.available_at <= %s
               AND NOT EXISTS (
                   SELECT 1 FROM events older
                   WHERE older.session = e.session AND older.id < e.id
                   AND older.status IN ('queued', 'running')
               )
               ORDER BY e.available_at, e.id LIMIT 1 FOR UPDATE OF e SKIP LOCKED""",
            (timestamp,),
        ).fetchone()
        if event is None:
            return None
        db.execute("UPDATE events SET status = 'running', attempts = attempts + 1 WHERE id = %s", (event["id"],))
        return db.execute("SELECT * FROM events WHERE id = %s", (event["id"],)).fetchone()


def _can_retry(db, event):
    if event["timer_id"] is None:
        return True
    row = db.execute("SELECT status FROM timers WHERE id = %s FOR UPDATE", (event["timer_id"],)).fetchone()
    return row["status"] != "cancelled"


def record_failure(db, event, message, timestamp, retry_delay=None):
    if retry_delay is not None and event["attempts"] < MAX_ATTEMPTS and _can_retry(db, event):
        db.execute(
            "UPDATE events SET status = 'queued', available_at = %s, error = %s WHERE id = %s",
            (timestamp + retry_delay, message, event["id"]),
        )
    else:
        db.execute(
            "UPDATE events SET status = 'failed', finished_at = %s, error = %s WHERE id = %s",
            (timestamp, message, event["id"]),
        )
        emit(db, event, "error", f"任务 #{event['id']} 失败。{message}", timestamp)


def recover(db, timestamp):
    with transaction(db):
        for event in db.execute("SELECT * FROM events WHERE status = 'running'").fetchall():
            record_failure(db, event, "常驻进程中断，未确认本次运行结果。", timestamp, retry_delay=0)


def postpone_for_quota(db, event, timestamp):
    tomorrow = (timestamp // 86400 + 1) * 86400
    with transaction(db):
        if not _can_retry(db, event):
            db.execute("UPDATE events SET status = 'cancelled', finished_at = %s WHERE id = %s", (timestamp, event["id"]))
            db.execute("DELETE FROM checkpoints WHERE event_id = %s", (event["id"],))
            return
        db.execute(
            """UPDATE events SET status = 'queued', available_at = %s, attempts = attempts - 1,
               error = %s WHERE id = %s""",
            (tomorrow, "达到每日 API 请求上限，将在下一个 UTC 日重试。", event["id"]),
        )


def reserve_request(db, day, limit):
    with transaction(db):
        db.execute("INSERT INTO usage(day) VALUES (%s) ON CONFLICT(day) DO NOTHING", (day,))
        cursor = db.execute("UPDATE usage SET requests = requests + 1 WHERE day = %s AND requests < %s", (day, limit))
        return bool(cursor.rowcount)


def add_usage(db, day, usage):
    def tokens(key):
        value = usage.get(key, 0)
        return value if type(value) is int and 0 <= value <= 10_000_000 else 0

    db.execute(
        "UPDATE usage SET input_tokens = input_tokens + %s, output_tokens = output_tokens + %s WHERE day = %s",
        (tokens("input_tokens"), tokens("output_tokens"), day),
    )


def load_checkpoint(db, event_id):
    row = db.execute("SELECT state FROM checkpoints WHERE event_id = %s", (event_id,)).fetchone()
    return row["state"] if row else None


def save_checkpoint(db, event_id, state, timestamp):
    db.execute(
        """INSERT INTO checkpoints(event_id, state, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT(event_id) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at""",
        (event_id, Json(state, dumps=dumps), timestamp),
    )


def save_run(db, event, reply, progress, proposals, timestamp, github_result=None):
    with transaction(db):
        if github_result:
            db.execute(
                """INSERT INTO github_issues(repo, issue, fingerprint, status, pr_url)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT(repo, issue) DO UPDATE SET fingerprint = excluded.fingerprint,
                       status = excluded.status, pr_url = excluded.pr_url""",
                github_result,
            )
        if progress is not None:
            db.execute(
                """INSERT INTO progress(session, value, updated_at) VALUES (%s, %s, %s)
                   ON CONFLICT(session) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (event["session"], progress, timestamp),
            )
        for action, payload in proposals:
            approval_id = db.execute(
                """INSERT INTO approvals(event_id, session, action, payload, created_at)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (event["id"], event["session"], action, dumps(payload), timestamp),
            ).fetchone()["id"]
            emit(db, event, "approval", dumps({"approval_id": approval_id, "action": action, "payload": payload}), timestamp)
        if reply:
            emit(db, event, "reply", reply, timestamp)
        db.execute(
            "UPDATE events SET status = 'done', finished_at = %s, reply = %s, error = NULL WHERE id = %s",
            (timestamp, reply, event["id"]),
        )
        db.execute("DELETE FROM checkpoints WHERE event_id = %s", (event["id"],))


def put_memory(db, key, value, timestamp):
    db.execute("SELECT pg_advisory_xact_lock(349811, 3)")
    if db.execute("SELECT 1 FROM memory WHERE key = %s", (key,)).fetchone() is None:
        if db.execute("SELECT COUNT(*) AS count FROM memory").fetchone()["count"] >= MAX_MEMORY:
            raise ValueError(f"记忆达到 {MAX_MEMORY} 条上限，请先删除旧条目")
    db.execute(
        """INSERT INTO memory(key, value, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, timestamp),
    )


def decide(db, approval_id, status, timestamp):
    with transaction(db):
        row = db.execute("SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval_id,)).fetchone()
        if row is None or row["status"] != "pending":
            raise ValueError("找不到待审批的请求")
        payload = json.loads(row["payload"])
        decision = {"approval_id": approval_id, "status": status, "action": row["action"], "payload": payload}
        if status == "approved":
            if row["action"] == "memory":
                put_memory(db, payload["key"], payload["value"], timestamp)
            elif row["action"] == "followup":
                decision["timer_id"] = add_timer(db, row["session"], payload["task"], timestamp + payload["delay_seconds"])
            else:
                raise ValueError("未知审批动作")
        db.execute("UPDATE approvals SET status = %s, decided_at = %s WHERE id = %s", (status, timestamp, approval_id))
        event_id = enqueue(db, "decision", row["session"], dumps(decision), timestamp, f"decision:{approval_id}")
    return decision | {"event_id": event_id}


def release_on_shutdown(db, event, timestamp):
    with transaction(db):
        if _can_retry(db, event):
            db.execute(
                """UPDATE events SET status = 'queued', available_at = %s, attempts = attempts - 1
                   WHERE id = %s""",
                (timestamp, event["id"]),
            )
        else:
            db.execute("UPDATE events SET status = 'cancelled', finished_at = %s WHERE id = %s", (timestamp, event["id"]))
            db.execute("DELETE FROM checkpoints WHERE event_id = %s", (event["id"],))
