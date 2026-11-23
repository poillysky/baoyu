#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""状态持久化 (status_db.py)
用 sqlite 存监控每房的实时状态，供前端秒级读取，避免每次请求现扫盘/ffprobe。
- 看门狗每轮把最新判定 upsert 进 watch_status 表
- /api/watch/list 直接读表返回
- 录制进度(时长/大小)也定期更新进表
"""
import os, sqlite3, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "status.db")
_LOCK = threading.Lock()
_conn = None


def _get():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA busy_timeout=10000")
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


def init():
    with _LOCK:
        c = _get()
        c.execute("""
        CREATE TABLE IF NOT EXISTS rec_tasks (
            rec_id       TEXT PRIMARY KEY,
            room_id      TEXT,
            plat_id      TEXT,
            name         TEXT,
            state        TEXT,
            bytes        INTEGER DEFAULT 0,
            seg          INTEGER DEFAULT 0,
            seg_bytes    INTEGER DEFAULT 0,
            mp4          TEXT,
            segments     TEXT,           -- json 数组
            started_at   REAL,
            retry        INTEGER DEFAULT 0,
            mode         TEXT,
            engine       TEXT,
            updated_at   REAL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS watch_status (
            room_id      TEXT PRIMARY KEY,
            plat_id      TEXT,
            name         TEXT,
            cover        TEXT,
            added_at     REAL,
            online       INTEGER,      -- 1/0/ NULL 未知
            rec_state    TEXT,         -- recording/starting/... / NULL
            rec_id       TEXT,
            rec_started_at REAL,
            rec_duration INTEGER DEFAULT 0,   -- 已录时长(秒) 真实本地累计
            rec_bytes    INTEGER DEFAULT 0,   -- 已录大小(字节) 真实本地
            checked_at   REAL,
            last_msg     TEXT,
            enabled      INTEGER DEFAULT 1,  -- 1 启用 0 关闭(手动开关)
            updated_at   REAL
        )
        """)
        c.commit()
        # 老库可能缺 enabled 列: 直接补列, 幂等
        try:
            c.execute("SELECT enabled FROM watch_status LIMIT 1")
        except Exception:
            c.execute("ALTER TABLE watch_status ADD COLUMN enabled INTEGER DEFAULT 1")
            c.commit()


def upsert_status(row: dict):
    """写入/更新一行状态（按 room_id upsert）。"""
    upsert_many([row])


def upsert_many(rows):
    """单事务批量 upsert，避免每房一次 fsync，也避免前端读到半轮状态。"""
    rows = list(rows)
    if not rows:
        return
    now = time.time()
    values = [(
        str(row.get("room_id")), str(row.get("plat_id") or ""), row.get("name"),
        row.get("cover"), row.get("added_at"),
        None if row.get("online") is None else (1 if row.get("online") else 0),
        row.get("rec_state"), row.get("rec_id"),
        row.get("rec_started_at"), int(row.get("rec_duration") or 0),
        int(row.get("rec_bytes") or 0), row.get("checked_at"), row.get("last_msg"),
        None if row.get("enabled") is None else (1 if row.get("enabled") else 0),
        now,
    ) for row in rows]
    with _LOCK:
        c = _get()
        c.executemany("""
        INSERT INTO watch_status
          (room_id, plat_id, name, cover, added_at, online, rec_state, rec_id,
           rec_started_at, rec_duration, rec_bytes, checked_at, last_msg, enabled, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(room_id) DO UPDATE SET
          plat_id=excluded.plat_id, name=excluded.name, cover=excluded.cover,
          added_at=excluded.added_at, online=excluded.online,
          rec_state=excluded.rec_state, rec_id=excluded.rec_id,
          rec_started_at=excluded.rec_started_at, rec_duration=excluded.rec_duration,
          rec_bytes=excluded.rec_bytes, checked_at=excluded.checked_at,
          last_msg=excluded.last_msg, enabled=excluded.enabled, updated_at=excluded.updated_at
        """, values)
        c.commit()


def update_progress(room_id, duration, nbytes):
    """仅更新录制进度(时长/大小)，轻量调用。"""
    with _LOCK:
        c = _get()
        c.execute("UPDATE watch_status SET rec_duration=?, rec_bytes=?, updated_at=? WHERE room_id=?",
                  (int(duration or 0), int(nbytes or 0), time.time(), str(room_id)))
        c.commit()


def read_list():
    """读全表，返回前端结构(按 added_at 倒序)。"""
    with _LOCK:
        c = _get()
        rows = c.execute("SELECT * FROM watch_status").fetchall()
    out = []
    for r in rows:
        out.append({
            "room_id": r["room_id"], "plat_id": r["plat_id"], "name": r["name"],
            "cover": r["cover"], "added_at": r["added_at"],
            "online": None if r["online"] is None else bool(r["online"]),
            "rec_state": r["rec_state"], "rec_id": r["rec_id"],
            "rec_started_at": r["rec_started_at"],
            "rec_duration": r["rec_duration"] or 0,
            "rec_bytes": r["rec_bytes"] or 0,
            "checked_at": r["checked_at"], "last_msg": r["last_msg"],
            "enabled": None if r["enabled"] is None else bool(r["enabled"]),
        })
    out.sort(key=lambda x: x.get("added_at") or 0, reverse=True)
    return out


def delete(room_id):
    with _LOCK:
        c = _get()
        c.execute("DELETE FROM watch_status WHERE room_id=?", (str(room_id),))
        c.commit()


def set_enabled(room_id, enabled):
    """立即同步一行 enabled 到库(开关点击即时生效, 不必等看门狗下一轮)。"""
    with _LOCK:
        c = _get()
        c.execute("UPDATE watch_status SET enabled=?, updated_at=? WHERE room_id=?",
                  (1 if enabled else 0, time.time(), str(room_id)))
        c.commit()


def prune(alive_ids):
    """删除不在监控清单里的残留行。"""
    alive = {str(x) for x in alive_ids}
    with _LOCK:
        c = _get()
        rows = c.execute("SELECT room_id FROM watch_status").fetchall()
        for r in rows:
            if r["room_id"] not in alive:
                c.execute("DELETE FROM watch_status WHERE room_id=?", (r["room_id"],))
        c.commit()


init()



# ============ rec_tasks (录制任务表) ============
# 录制引擎每秒把当前任务全量 upsert 进本表; /api/rec/status 直接读表返回, 页面秒读、重启不丢历史。
import json as _json


def upsert_rec_tasks(rows):
    rows = list(rows)
    if not rows:
        return
    now = time.time()
    values = [(
        str(r.get("rec_id")), str(r.get("room_id") or ""), str(r.get("plat_id") or ""),
        r.get("name"), r.get("state"), int(r.get("bytes") or 0),
        int(r.get("seg") or 0), int(r.get("seg_bytes") or 0), r.get("mp4"),
        _json.dumps(r.get("segments") or [], ensure_ascii=False),
        r.get("started_at"), int(r.get("retry") or 0),
        r.get("mode"), r.get("engine"), now,
    ) for r in rows]
    with _LOCK:
        c = _get()
        c.executemany(
            "INSERT INTO rec_tasks "
            "(rec_id, room_id, plat_id, name, state, bytes, seg, seg_bytes, mp4, "
            "segments, started_at, retry, mode, engine, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(rec_id) DO UPDATE SET "
            "room_id=excluded.room_id, plat_id=excluded.plat_id, name=excluded.name, "
            "state=excluded.state, bytes=excluded.bytes, seg=excluded.seg, "
            "seg_bytes=excluded.seg_bytes, mp4=excluded.mp4, segments=excluded.segments, "
            "started_at=excluded.started_at, retry=excluded.retry, "
            "mode=excluded.mode, engine=excluded.engine, updated_at=excluded.updated_at",
            values)
        c.commit()


def read_rec_tasks():
    with _LOCK:
        c = _get()
        rows = c.execute("SELECT * FROM rec_tasks").fetchall()
    out = []
    for r in rows:
        seg = []
        try:
            seg = _json.loads(r["segments"] or "[]")
        except Exception:
            seg = []
        out.append({
            "rec_id": r["rec_id"], "room_id": r["room_id"], "plat_id": r["plat_id"],
            "name": r["name"], "state": r["state"], "bytes": r["bytes"] or 0,
            "seg": r["seg"] or 0, "seg_bytes": r["seg_bytes"] or 0,
            "mp4": r["mp4"], "segments": seg, "started_at": r["started_at"],
            "retry": r["retry"] or 0, "mode": r["mode"], "engine": r["engine"],
        })
    out.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return out



def mark_stale_active():
    """服务重启后把库里残留的"活动中"录制标记为已中断(进程没了, 任务已丢)。"""
    active = ("starting", "recording", "restarting", "stopping")
    with _LOCK:
        c = _get()
        marks = ", ".join("?" for _ in active)
        c.execute(
            "UPDATE rec_tasks SET state='interrupted', updated_at=? "
            "WHERE state IN (%s)" % marks,
            (time.time(),) + active)
        c.commit()

