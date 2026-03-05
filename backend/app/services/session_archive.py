"""SQLite 保存用户归属、查询计划、结果和审计记录；结果ID是报告唯一数据入口。"""

from __future__ import annotations
import json, sqlite3
from pathlib import Path
from contextlib import closing
from threading import RLock


class SessionArchive:
    def __init__(self, path: Path, *, enabled=True):
        self.path = Path(path)
        self.enabled = enabled
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS credit_tasks(task_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP,saved INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS credit_owner_session ON credit_tasks(user_id,session_id);
            CREATE TABLE IF NOT EXISTS credit_fields(memory_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS credit_audit(event_id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,user_id TEXT,status TEXT,result_fingerprint TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            """)

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        return con

    def save_task(self, task):
        result = task["result"]
        with self._lock, closing(self._connect()) as con, con:
            owner = con.execute(
                "SELECT user_id FROM credit_tasks WHERE task_id=?", (task["task_id"],)
            ).fetchone()
            if owner and owner["user_id"] != task["user_id"]:
                raise PermissionError("任务归属不匹配")
            con.execute(
                "INSERT INTO credit_tasks(task_id,user_id,session_id,payload) VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload,updated_at=CURRENT_TIMESTAMP",
                (
                    task["task_id"],
                    task["user_id"],
                    task["session_id"],
                    json.dumps(task, ensure_ascii=False),
                ),
            )
            con.execute(
                "INSERT INTO credit_audit(task_id,user_id,status,result_fingerprint) VALUES(?,?,?,?)",
                (
                    task["task_id"],
                    task["user_id"],
                    result["status"],
                    result.get("result_fingerprint", ""),
                ),
            )

    def get_task(self, task_id, user_id):
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT payload FROM credit_tasks WHERE task_id=? AND user_id=?",
                (task_id, user_id),
            ).fetchone()
        if not row:
            raise KeyError("查询结果不存在或无权访问")
        return json.loads(row["payload"])

    def list_session(self, user_id, session_id):
        with closing(self._connect()) as con:
            rows = con.execute(
                "SELECT payload FROM credit_tasks WHERE user_id=? AND session_id=? ORDER BY rowid",
                (user_id, session_id),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def save_result(self, task_id, user_id):
        self.get_task(task_id, user_id)
        with closing(self._connect()) as con, con:
            con.execute(
                "UPDATE credit_tasks SET saved=1 WHERE task_id=? AND user_id=?",
                (task_id, user_id),
            )

    def list_saved(self, user_id):
        with closing(self._connect()) as con:
            tasks = con.execute(
                "SELECT payload,created_at FROM credit_tasks WHERE user_id=? AND saved=1 ORDER BY rowid DESC LIMIT 20",
                (user_id,),
            ).fetchall()
            fields = con.execute(
                "SELECT payload FROM credit_fields WHERE user_id=? ORDER BY rowid DESC LIMIT 20",
                (user_id,),
            ).fetchall()
        output = []
        for row in tasks:
            t = json.loads(row["payload"])
            r = t["result"]
            output.append(
                {
                    "id": "result:" + t["task_id"],
                    "kind": "result_table",
                    "task_id": t["task_id"],
                    "user_id": user_id,
                    "title": r.get("result_title"),
                    "query": t["query"],
                    "columns": r["columns"],
                    "rows": r["rows"],
                    "column_metadata": r.get("column_metadata", {}),
                    "summary": r.get("analysis", ""),
                    "created_at": row["created_at"],
                }
            )
        return output + [json.loads(r["payload"]) for r in fields]

    def save_field(self, item):
        with closing(self._connect()) as con, con:
            con.execute(
                "INSERT OR REPLACE INTO credit_fields(memory_id,user_id,payload) VALUES(?,?,?)",
                (
                    item["user_id"] + ":" + item["id"],
                    item["user_id"],
                    json.dumps(item, ensure_ascii=False),
                ),
            )

    def delete_memory(self, memory_id, user_id):
        with closing(self._connect()) as con, con:
            if memory_id.startswith("result:"):
                con.execute(
                    "UPDATE credit_tasks SET saved=0 WHERE task_id=? AND user_id=?",
                    (memory_id[7:], user_id),
                )
            else:
                con.execute(
                    "DELETE FROM credit_fields WHERE memory_id=? AND user_id=?",
                    (user_id + ":" + memory_id, user_id),
                )
