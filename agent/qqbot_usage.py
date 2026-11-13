"""QQ 主动发送记录：按机器人与目标关系持久统计，限制由部署者配置。"""
import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


class ActiveMessageLimitReached(RuntimeError):
    pass


class ActiveMessageUsage:
    def __init__(self, path: str, app_id: str, monthly_limit: int = 0):
        self.path = path
        self.app_id = app_id
        self.monthly_limit = max(0, int(monthly_limit))

    def _target(self, key: str) -> str:
        return hashlib.sha256(f"{self.app_id}\0{key}".encode()).hexdigest()

    def _connect(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS sends (id INTEGER PRIMARY KEY, "
                   "target TEXT NOT NULL, month TEXT NOT NULL, outcome TEXT NOT NULL)")
        return db

    @staticmethod
    def _month():
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def reserve(self, key: str) -> int:
        """占用一次主动发送配额；limit=0 只记录不限制（用 COUNT 与上限同句判定，避免读改写竞态）。"""
        target, month = self._target(key), self._month()
        limit = self.monthly_limit or 2 ** 31
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO sends (target, month, outcome) "
                "SELECT ?, ?, 'pending' WHERE ("
                "SELECT COUNT(*) FROM sends WHERE target=? AND month=?) < ?",
                (target, month, target, month, limit))
            if cursor.rowcount != 1:
                raise ActiveMessageLimitReached(
                    "本地设置的主动发送月尝试上限已达到，请发 /status 获取结果")
            return cursor.lastrowid

    def finish(self, reservation: int, success: bool):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE sends SET outcome=? WHERE id=?",
                       ("sent" if success else "failed", reservation))

    def summary(self, key: str) -> dict:
        counts = {"pending": 0, "sent": 0, "failed": 0}
        if os.path.isfile(self.path):
            with closing(self._connect()) as db:
                rows = db.execute(
                    "SELECT outcome, COUNT(*) FROM sends WHERE target=? AND month=? "
                    "GROUP BY outcome", (self._target(key), self._month())).fetchall()
                counts.update(dict(rows))
        return {**counts, "attempts": sum(counts.values()), "limit": self.monthly_limit}
