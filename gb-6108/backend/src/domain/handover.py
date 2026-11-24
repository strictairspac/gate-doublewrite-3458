"""交班账的纯领域规则。

掉电巡检之后，每页要给下一班留下可读的日志页。跨批次的当前交班账按逻辑页号
（例如 ``N1-021``）保存最近一次成功交班的代次、校验和与文本。本模块不依赖数据库，
并发与重试时的判定都集中在这里，便于单独核验：

* 本页被隔离 / 无可用副本：不覆盖交班账，沿用上次交班内容；从来没有交过班则为未交班。
* 候选代次更新：新代可以接班。
* 候选代次更旧：旧代不能覆盖已经交班的新代（退代拒绝）。
* 同代但校验和（内容）不同：同代副本冲突，也要隔离，不能覆盖原交班。
* 同代同校验和：幂等一致，重试不产生变化。
"""

from dataclasses import dataclass

# --- 稳定的英文状态码（接口/测试可直接断言） ---
HANDED = "handed"                    # 首次交班，或更新代次接班
UNCHANGED = "unchanged"              # 同代同内容，幂等
RETAINED = "retained"                # 本页隔离，沿用上一次交班
STALE_REJECTED = "stale_rejected"    # 旧代试图覆盖更新的交班，拒绝
CONFLICT = "conflict"                # 同代内容不同，隔离并保留原交班
MISSING = "missing"                  # 本页隔离且历史上没有交班

# --- 界面与证据 JSON 使用的中文状态 ---
STATUS_LABELS = {
    HANDED: "已交班",
    UNCHANGED: "同代一致",
    RETAINED: "沿用上次交班",
    STALE_REJECTED: "旧代退代拒绝",
    CONFLICT: "同代内容冲突·已隔离",
    MISSING: "未交班",
}

# 哪些状态下，下一班仍然拿得到可读日志页（交班账非空）。
_HANDED_OVER = {HANDED, UNCHANGED, RETAINED, STALE_REJECTED, CONFLICT}
# 哪些状态需要进入修复隔离单（本批候选页不可信/不可读）。
_QUARANTINE = {RETAINED, MISSING, CONFLICT}
# 哪些状态会改写当前交班账。
_CHANGED = {HANDED}


@dataclass(frozen=True)
class HandoverState:
    """交班账上的一页（上次已交给下一班的内容）。"""

    generation: int
    checksum: str
    text: str

    def as_dict(self):
        return {"generation": self.generation, "checksum": self.checksum, "text": self.text}


@dataclass(frozen=True)
class HandoverDecision:
    """单页交班判定。``generation`` 始终是“实际交给下一班”的代次。"""

    status: str
    handed_over: bool
    generation: int | None
    checksum: str | None
    text: str | None
    candidate_generation: int | None = None
    candidate_checksum: str | None = None
    quarantine: bool = False
    changed: bool = False

    @property
    def status_label(self):
        return STATUS_LABELS[self.status]

    # 向后/近义命名兼容。
    @property
    def label(self):
        return self.status_label

    def as_dict(self):
        return {
            "status": self.status,
            "status_label": self.status_label,
            "label": self.status_label,
            "handed_over": self.handed_over,
            "是否交班": self.handed_over,
            "generation": self.generation,
            "handover_generation": self.generation,
            "交班代次": self.generation,
            "下一班代次": self.generation,
            "checksum": self.checksum,
            "text": self.text,
            "content": self.text,
            "交班内容": self.text,
            "candidate_generation": self.candidate_generation,
            "candidate_checksum": self.candidate_checksum,
            "quarantine": self.quarantine,
            "changed": self.changed,
        }


def _decision(status, current, candidate):
    if current is not None:
        generation, checksum, text = current.generation, current.checksum, current.text
    else:
        generation = checksum = text = None
    cand_gen = candidate.generation if candidate is not None else None
    cand_sum = candidate.checksum if candidate is not None else None
    return HandoverDecision(
        status=status,
        handed_over=status in _HANDED_OVER,
        generation=generation,
        checksum=checksum,
        text=text,
        candidate_generation=cand_gen,
        candidate_checksum=cand_sum,
        quarantine=status in _QUARANTINE,
        changed=status in _CHANGED,
    )


def settle(current, candidate):
    """对照当前交班账，决定本批候选页如何交班。

    :param current: :class:`HandoverState` 或 ``None``（该页从未交班）
    :param candidate: :class:`HandoverState` 或 ``None``（本批该页被隔离/无读本）
    :return: :class:`HandoverDecision`
    """
    if candidate is None:
        if current is None:
            return _decision(MISSING, None, None)
        return _decision(RETAINED, current, None)

    if current is None:
        return _decision(HANDED, candidate, candidate)

    if candidate.generation > current.generation:
        return _decision(HANDED, candidate, candidate)
    if candidate.generation < current.generation:
        # 并发巡检：旧代绝不能覆盖已经交班的新代。
        return _decision(STALE_REJECTED, current, candidate)
    if candidate.checksum != current.checksum:
        # 同代但内容/校验和不一致：同代分叉，隔离候选并保留原交班。
        return _decision(CONFLICT, current, candidate)
    return _decision(UNCHANGED, current, candidate)


# 近义函数名，便于不同调用/测试习惯。
resolve_handover = settle
decide = settle
decide_handover = settle
commit_handover = settle


def plan_batch(candidates, ledger=None):
    """纯函数：给定整批候选与当前账本映射，产出每页判定。

    :param candidates: 可迭代的 ``(page, HandoverState | None)`` 或 dict 列表
        （含 page/generation/checksum/text，候选为空时传 ``candidate=None``）。
    :param ledger: ``{page: HandoverState}`` 当前交班账。
    :returns: ``{page: HandoverDecision}``
    """
    ledger = ledger or {}
    decisions = {}
    for item in candidates:
        if isinstance(item, tuple):
            page, candidate = item
        else:
            page = item["page"]
            candidate = item.get("candidate")
            if candidate is None and item.get("generation") is not None:
                candidate = HandoverState(item["generation"], item["checksum"], item.get("text", ""))
        current = ledger.get(page)
        decisions[page] = settle(current, candidate)
    return decisions
