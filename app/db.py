"""SQLite 存储层。

一致性要点：
- 所有"读-改-写"（标注修订号 check-and-set、单份发布、批次发布）都在
  ``BEGIN IMMEDIATE`` 事务内完成，SQLite 写锁将其串行化，
  因此发布读到的标注修订与写入记录的修订必然一致；批次内多文档同样与
  并发的标注修改、单份发布、其他批次发布互斥。
- 标注矩形按 (doc_id, revision) 追加存储、永不改写，
  已发布成品对应的遮盖坐标因此不可变。
- 批次记录（publication_batches/batch_publications）与各文档 publications
  行在同一事务内提交：任一文档校验或渲染失败即整体回滚，批次下不会留下
  任何版本记录或批次号，也就不存在被占用的版本号。
- 幂等键（batch_idempotency）与首次成功批次在同一事务内落库：键的
  检查-发布-登记被写锁串行化，并发重试最多发布一次；失败回滚时键记录
  一并撤销，调用方可用原键重试。
- 撤回记录（publication_withdrawals）按 (doc_id, version) 追加存储、
  永不改写：重复撤回命中主键直接返回首次记录；撤回只插入该表，
  publications 行、成品文件与版本号序列全部保留。
- 定时公开时间（publications.publish_at / publication_batches.publish_at）
  在发布事务内一并固化：成品与版本号在发布当下就生成/占用，"未到点"仅是
  公开读取时按当前 UTC 时间做的判定，到点无需任何补操作；列为 NULL 即立即
  公开。失败事务回滚时该列随记录一并消失，不留下定时版本。
- 只读快照事务（snapshot_tx）供遮盖范围比较与服务数据归档使用：延迟 ``BEGIN``
  后的第一条 SELECT 建立 WAL 读快照，事务内的全部读取都落在同一快照上，与并发
  写入互不干扰；事务纯只读，不产生版本、不写入任何改动记录。归档还会在该事务
  内读取引用文件（原图/成品）并复算摘要：这些文件落盘后不可变，故快照记录与
  文件字节同属一个一致时点；读盘期间不持写锁，并发写入同样不受阻塞。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id                  TEXT PRIMARY KEY,
    filename            TEXT NOT NULL,
    media_type          TEXT NOT NULL,
    original_path       TEXT NOT NULL,
    original_sha256     TEXT NOT NULL,
    width               INTEGER NOT NULL,   -- EXIF 旋正后的宽
    height              INTEGER NOT NULL,   -- EXIF 旋正后的高
    annotation_revision INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS annotation_rects (
    doc_id   TEXT NOT NULL REFERENCES documents(id),
    revision INTEGER NOT NULL,
    idx      INTEGER NOT NULL,
    x        INTEGER NOT NULL,
    y        INTEGER NOT NULL,
    w        INTEGER NOT NULL,
    h        INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    PRIMARY KEY (doc_id, revision, idx)
);

CREATE TABLE IF NOT EXISTS publications (
    doc_id              TEXT NOT NULL REFERENCES documents(id),
    version             INTEGER NOT NULL,
    annotation_revision INTEGER NOT NULL,
    original_sha256     TEXT NOT NULL,
    output_sha256       TEXT NOT NULL,
    output_path         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    batch_id            TEXT,
    publish_at          TEXT,                 -- 统一 UTC 公开时间；NULL=立即公开
    PRIMARY KEY (doc_id, version)
);

CREATE TABLE IF NOT EXISTS publication_batches (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    publish_at TEXT                            -- 整批统一 UTC 公开时间；NULL=立即公开
);

CREATE TABLE IF NOT EXISTS batch_publications (
    batch_id            TEXT NOT NULL REFERENCES publication_batches(id),
    position            INTEGER NOT NULL,   -- 请求中的次序，从 0 开始
    doc_id              TEXT NOT NULL REFERENCES documents(id),
    version             INTEGER NOT NULL,
    annotation_revision INTEGER NOT NULL,
    original_sha256     TEXT NOT NULL,
    output_sha256       TEXT NOT NULL,
    PRIMARY KEY (batch_id, position),
    UNIQUE (doc_id, version),
    FOREIGN KEY (doc_id, version) REFERENCES publications(doc_id, version)
);

CREATE TABLE IF NOT EXISTS batch_idempotency (
    key          TEXT PRIMARY KEY,          -- 调用方提供的全局唯一幂等键
    request_hash TEXT NOT NULL,             -- 有序 (doc_id, expected_revision) 列表的摘要
    batch_id     TEXT NOT NULL REFERENCES publication_batches(id),
    response     TEXT NOT NULL,             -- 首次成功的完整响应体（JSON，原样重放）
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_withdrawals (
    doc_id       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    reason       TEXT NOT NULL,             -- 首次撤回时的原因（重复撤回不改写）
    withdrawn_at TEXT NOT NULL,             -- 首次撤回时间
    PRIMARY KEY (doc_id, version),
    FOREIGN KEY (doc_id, version) REFERENCES publications(doc_id, version)
);
"""


def init_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        # 轻量迁移：旧卷上的 publications 表可能没有 batch_id / publish_at 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(publications)")}
        if "batch_id" not in cols:
            conn.execute("ALTER TABLE publications ADD COLUMN batch_id TEXT")
        if "publish_at" not in cols:
            conn.execute("ALTER TABLE publications ADD COLUMN publish_at TEXT")
        bcols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(publication_batches)")}
        if "publish_at" not in bcols:
            conn.execute(
                "ALTER TABLE publication_batches ADD COLUMN publish_at TEXT")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
    finally:
        conn.close()


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None：手动控制事务边界
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def read_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def snapshot_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """只读快照事务：事务内全部 SELECT 共享同一个一致性快照。

    SQLite 的延迟事务（``BEGIN``）以第一条读语句开始，WAL 下该读快照在
    整个事务内保持不变，因此随后提交的写入（标注修改、发布、撤回等）
    对本事务内后续读取都不可见——遮盖范围比较需要在**同一读取快照**中
    同时读取文档、当前修订标注与基准发布冻结的标注，本助手保证三者一致。
    比较纯只读：不获取写锁、不阻塞任何写入，也绝不产生版本或改动记录。
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.rollback()  # 只读事务：仅结束快照，没有任何待提交的改动
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """独占写事务：BEGIN IMMEDIATE 使并发写者串行执行。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
