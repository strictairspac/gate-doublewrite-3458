from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Table,
)

from .base import metadata


# 按逻辑页号保存“当前已交给下一班”的那一页（跨批次、并发巡检下按代次比较更新）。
handover_pages = Table(
    "handover_pages",
    metadata,
    Column("page", String(32, collation="utf8mb4_bin"), primary_key=True),
    Column("generation", Integer, nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("content", Text, nullable=False),
    Column("source_run_id", ForeignKey("runs.id"), nullable=True, index=True),
    Column("updated_at", BigInteger, nullable=False),
)


# 每个批次、每页一条的交班记录。(run_id, page) 唯一：
# 批次中断续跑 / 同一批重试都不会新增重复交班记录。
handovers = Table(
    "handovers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey("runs.id"), nullable=True),
    Column("page", String(32, collation="utf8mb4_bin"), nullable=False),
    Column("status", String(24), nullable=False),
    Column("handed_over", Boolean, nullable=False),
    # 实际交给下一班的代次（隔离沿用 / 旧代拦截时为历史代次；从未交班为 NULL）。
    Column("generation", Integer, nullable=True),
    Column("candidate_generation", Integer, nullable=True),
    Column("checksum", String(64), nullable=True),
    Column("content", Text, nullable=True),
    Column("note", String(320), nullable=True),
    Column("created_at", BigInteger, nullable=False),
    UniqueConstraint("run_id", "page", name="uq_handovers_run_page"),
    Index("ix_handovers_page", "page"),
)
