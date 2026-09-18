"""SQLite 连接与建表。数据库路径由环境变量 DATABASE_PATH 指定，默认 ./rates.db。"""

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_versions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_no   TEXT NOT NULL,          -- 合同号
    rate          REAL NOT NULL,          -- 费率值
    valid_from    TEXT NOT NULL,          -- 业务生效起点（含），UTC ISO 字符串
    valid_to      TEXT,                   -- 业务生效终点（不含），NULL 表示无限期
    recorded_at   TEXT NOT NULL,          -- 系统记录时间（认知时间起点，含）
    superseded_at TEXT,                   -- 被修订时间（认知时间终点，不含），NULL 表示当前认知
    version       INTEGER NOT NULL,       -- 同一合同内单调递增的版本号
    reason        TEXT                    -- 录入/修订说明
);
CREATE INDEX IF NOT EXISTS idx_rate_versions_contract ON rate_versions (contract_no);
"""


def db_path() -> str:
    return os.environ.get("DATABASE_PATH", "rates.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """建表（幂等），应用启动时自动调用。"""
    with connect() as conn:
        conn.executescript(SCHEMA)
