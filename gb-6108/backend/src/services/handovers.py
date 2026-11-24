"""交班账持久化：当前账本 ``handover_pages`` 与批次交班记录 ``handovers``。

所有函数都接收调用方管理的 SQLAlchemy 连接（worker 在同一个事务里执行巡检、
写交班记录并完结批次），从而保证“巡检结果 + 交班 + 批次完成”原子提交。
"""

from time import time

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from src.domain.handover import HandoverState, settle
from src.models.handovers import handover_pages, handovers


def _now():
    return int(time())


def _state_from_row(row):
    if row is None:
        return None
    return HandoverState(row["generation"], row["checksum"], row["content"])


def _existing_record(db, run_id, page):
    if run_id is None:
        return None
    return db.execute(
        select(handovers).where(handovers.c.run_id == run_id, handovers.c.page == page)
    ).mappings().first()


def _record_outcome(row):
    """把已落库的批次交班记录还原成与 HandoverDecision.as_dict 同构的结果。"""
    generation = row["generation"]
    return {
        "page": row["page"],
        "status": row["status"],
        "handed_over": bool(row["handed_over"]),
        "是否交班": bool(row["handed_over"]),
        "generation": generation,
        "handover_generation": generation,
        "交班代次": generation,
        "下一班代次": generation,
        "checksum": row["checksum"],
        "text": row["content"],
        "content": row["content"],
        "交班内容": row["content"],
        "candidate_generation": row["candidate_generation"],
        "note": row["note"],
        "idempotent_replay": True,
    }


def _lock_page(db, page):
    # MySQL/InnoDB 下是真正的行锁；SQLite 不支持 FOR UPDATE，直接普通读取。
    statement = select(handover_pages).where(handover_pages.c.page == page)
    if db.dialect.name in ("mysql", "postgresql"):
        statement = statement.with_for_update()
    return db.execute(statement).mappings().first()


def _store_current(db, page, candidate, run_id):
    """条件更新当前交班账：仅当候选代次更高时覆盖；不存在则插入。

    行锁内已经做过一次 settle，这里的 ``generation < candidate`` 条件是
    数据库层面的第二道 monotonicity 防线；首插竞争用 savepoint 兜住。
    """
    values = {
        "generation": candidate.generation,
        "checksum": candidate.checksum,
        "content": candidate.text,
        "source_run_id": run_id,
        "updated_at": _now(),
    }
    matched = db.execute(
        update(handover_pages)
        .where(handover_pages.c.page == page)
        .where(handover_pages.c.generation < candidate.generation)
        .values(**values)
    ).rowcount
    if matched:
        return

    if db.execute(select(handover_pages.c.page).where(handover_pages.c.page == page)).first():
        # 同代或更新的行已存在（并发竞争失败方）：保留已有交班账。
        return

    try:
        with db.begin_nested():  # SAVEPOINT：首插撞唯一键时只回滚插入。
            db.execute(handover_pages.insert().values(page=page, **values))
    except IntegrityError:
        # 另一个并发事务抢先插入；它的代次更新或相同，旧代不得覆盖。
        pass


def apply_page(db, page, candidate, run_id=None, note=None):
    """应用单页交班，返回 outcome dict。同批同页重放时直接返回既有记录。"""
    prior_record = _existing_record(db, run_id, page)
    if prior_record is not None:
        return _record_outcome(prior_record)

    current_row = _lock_page(db, page)
    current = _state_from_row(current_row)
    decision = settle(current, candidate)
    outcome = decision.as_dict()
    outcome["page"] = page
    outcome["note"] = note

    if decision.changed and candidate is not None:
        _store_current(db, page, candidate, run_id)

    if run_id is not None:
        db.execute(handovers.insert().values(
            run_id=run_id,
            page=page,
            status=decision.status,
            handed_over=decision.handed_over,
            generation=decision.generation,
            candidate_generation=decision.candidate_generation,
            checksum=decision.checksum,
            content=decision.text,
            note=note,
            created_at=_now(),
        ))
    return outcome


def apply_batch(db, run_id, candidates):
    """应用整批交班。

    :param candidates: ``[{"page": str, "candidate": HandoverState | None, "note": str?}]``
    :returns: ``{page: outcome_dict}``
    """
    outcomes = {}
    # 固定按页号顺序加锁，避免两个并发批次以不同顺序锁页而死锁。
    for item in sorted(candidates, key=lambda entry: entry["page"]):
        outcomes[item["page"]] = apply_page(
            db, item["page"], item.get("candidate"), run_id, item.get("note")
        )
    return outcomes


def list_current(db):
    rows = db.execute(
        select(handover_pages).order_by(handover_pages.c.page)
    ).mappings().all()
    return [dict(row) for row in rows]


def get_current(db, page):
    row = db.execute(
        select(handover_pages).where(handover_pages.c.page == page)
    ).mappings().first()
    return dict(row) if row else None


def list_for_run(db, run_id):
    rows = db.execute(
        select(handovers)
        .where(handovers.c.run_id == run_id)
        .order_by(handovers.c.id)
    ).mappings().all()
    return [dict(row) for row in rows]


def count_for_run(db, run_id):
    return db.scalar(
        select(func.count()).select_from(handovers).where(handovers.c.run_id == run_id)
    )
