import re

from .fixtures import FIXTURES
from .handover import CONFLICT, STATUS_LABELS, HandoverState, settle
from .image import Image
from .recovery import choose_copy

# 导入服务会连带把交班表注册到共享 metadata，测试直接 metadata.create_all 也能建表。
from src.services.handovers import apply_batch


CONFIG = {
    "title": "闸北水工·启闭日志双写巡检",
    "team": "闸北水工 9 人闸控值守与数据库巡检组",
    "description": "在值守交班前识别撕裂、旧代或双损日志页，生成可修复与隔离清单。",
    "direction": "双写缓冲与数据库页撕裂恢复",
    "mechanism": "字节级部分写入与页号、代次、内容校验，按已知持久线选择可恢复页。",
    "modules": ["启闭值守", "故障页清单", "双写复核", "修复隔离单"],
    "unit": "掉电巡检单",
    "max_parameter": 128,
    "parameter_label": "撕裂写入前缀（字节，1–128）",
    "default_parameter": 6,
    "facts": ["已修复", "隔离页", "原页通过"],
    "palette": 8,
    "fixture": "GT-N1-1124 / 北一闸",
}
EVENTS = {
    "complete": "写入完成",
    "torn": "目标页撕裂",
    "both": "双份损坏",
    "before_shadow": "双写前断电",
    "before_target": "目标写入前断电",
    "stale": "双写区只剩旧代",
    "disagree": "同代副本冲突",
}


def normalize(payload):
    if not isinstance(payload, dict):
        raise ValueError("输入需为对象")
    pages = payload.get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 80:
        raise ValueError("pages 需有 1 至 80 个日志页")
    seen = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("日志页需为对象")
        key = page.get("page")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key) or key in seen:
            raise ValueError("日志页号须唯一且仅含 1 至 32 位 ASCII 字母、数字、连字符或下划线")
        seen.add(key)
        if type(page.get("generation")) is not int or not 1 <= page["generation"] <= 1_000_000:
            raise ValueError("generation 需为 1 至 1000000 的整数")
        event = page.get("event")
        if not isinstance(event, str) or event not in EVENTS:
            raise ValueError("不支持该断电阶段")
        for key in ("before", "after"):
            value = page.get(key)
            if not isinstance(value, str) or not 1 <= len(value) <= 120:
                raise ValueError("前后日志内容须各有 1 至 120 字符")
            try:
                value.encode()
            except UnicodeEncodeError:
                raise ValueError("前后日志需为有效 UTF-8") from None
    return pages


def validate(payload):
    normalize(payload)


def fault_images(page, prefix_bytes):
    key, generation, event = page["page"], page["generation"], page["event"]
    prior = Image.seal(key, generation - 1, page["before"])
    intended = Image.seal(key, generation, page["after"])
    # Tear must leave at least the final byte unwritten for a multi-byte page.
    written = min(prefix_bytes, len(intended.data) - 1)
    broken = intended.torn(prior, written)
    match event:
        case "complete":
            return generation, intended, intended, None
        case "torn":
            return generation, broken, intended, written
        case "both":
            return generation, broken.damaged(), intended.damaged(), written
        case "before_shadow":
            return generation - 1, prior, None, None
        case "before_target":
            return generation, prior, intended, None
        case "stale":
            return generation, broken.damaged(), prior, written
        case "disagree":
            divergent = Image.seal(key, generation, page["after"] + " / 对侧登记")
            return generation, intended, divergent, None


def evaluate(payload, parameter):
    """纯计算：产出每页的基础判定、可读候选页与计数，不触碰数据库。"""
    pages = normalize(payload)
    evaluated = []
    counts = {"已修复": 0, "隔离页": 0, "原页通过": 0}
    metrics_by_decision = {"从双写区修复": "已修复", "隔离": "隔离页", "保留数据页": "原页通过"}
    for page in pages:
        key = page["page"]
        durable, target, shadow, written = fault_images(page, parameter)
        decision = choose_copy(key, durable, target, shadow)
        counts[metrics_by_decision[decision.label]] += 1
        text = decision.image.data.decode() if decision.image else None
        candidate = (
            HandoverState(decision.image.generation, decision.image.checksum, text)
            if decision.image is not None else None
        )
        row = {
            "页号": key, "掉电阶段": EVENTS[page["event"]], "持久代": durable,
            "目标代": target.generation, "目标校验": "通过" if target.valid(key) else "损坏",
            "双写代": shadow.generation if shadow else None,
            "双写校验": ("通过" if shadow.valid(key) else "损坏") if shadow else "无副本",
            "前缀写入字节": written, "结论": decision.label, "说明": decision.note,
            "读取文本": text,
        }
        evaluated.append({"page": key, "row": row, "candidate": candidate, "note": decision.note})
    return {"pages": evaluated, "counts": counts}


def _outcome_view(item, stored):
    """把纯判定或已落库记录统一成带中文标签的交班视图。"""
    if stored is None:
        view = settle(None, item["candidate"]).as_dict()
    else:
        view = dict(stored)
    status = view["status"]
    view["status_label"] = STATUS_LABELS[status]
    view["label"] = STATUS_LABELS[status]
    # 记录重放路径可能缺这些别名，统一补齐。
    handed = bool(view.get("handed_over"))
    generation = view.get("generation")
    view.setdefault("是否交班", handed)
    view.setdefault("handover_generation", generation)
    view.setdefault("交班代次", generation)
    view.setdefault("下一班代次", generation)
    text = view.get("text", view.get("content"))
    view["text"] = text
    view["content"] = text
    view["交班内容"] = text
    view["交班状态"] = STATUS_LABELS[status]
    return view


def compose(evaluated, outcomes=None):
    """把交班判定并入业务明细，生成最终批次结果。"""
    outcomes = outcomes or {}
    rows = []
    handovers_view = []
    handed_count = 0
    counts = dict(evaluated["counts"])
    metric_of_decision = {"从双写区修复": "已修复", "隔离": "隔离页", "保留数据页": "原页通过"}
    for item in evaluated["pages"]:
        view = _outcome_view(item, outcomes.get(item["page"]))
        row = dict(item["row"])
        handed = bool(view["handed_over"])
        generation = view.get("generation")
        text = view.get("text")
        # 跨批次发现“同代但内容不同”：候选页必须隔离，结论与计数并入隔离口径。
        if view.get("status") == CONFLICT and row.get("结论") != "隔离":
            bucket = metric_of_decision.get(row.get("结论"))
            if bucket and counts.get(bucket, 0) > 0:
                counts[bucket] -= 1
            counts["隔离页"] = counts.get("隔离页", 0) + 1
            row["结论"] = "隔离"
            row["说明"] = f"{row.get('说明', '')}；同代内容与已交班版本不一致，已隔离并保留原交班".lstrip("；")
        short_label = "已交班" if handed else "未交班"
        row.update({
            # 界面直接使用的中文列
            "是否交班": handed,
            "交班": short_label,
            "交班状态": view["status_label"],
            "交班情况": view["status_label"],
            "交班代次": generation,
            "下一班代次": generation,
            "交给下一班的代次": generation,
            "交班内容": text,
            "下一班日志页": text,
            # 结构化/近义字段，方便接口与测试断言
            "handed_over": handed,
            "handover_status": view["status"],
            "handover_status_label": view["status_label"],
            "handover_generation": generation,
            "shift_generation": generation,
            "handover_text": text,
            "handover_content": text,
        })
        rows.append(row)
        if view["handed_over"]:
            handed_count += 1
        handovers_view.append({
            "page": item["page"],
            "页号": item["page"],
            "status": view["status"],
            "status_label": view["status_label"],
            "handover_status": view["status"],
            "handover_status_label": view["status_label"],
            "交班状态": view["status_label"],
            "交班情况": view["status_label"],
            "交班": "已交班" if bool(view["handed_over"]) else "未交班",
            "handed_over": bool(view["handed_over"]),
            "是否交班": bool(view["handed_over"]),
            "generation": view.get("generation"),
            "交班代次": view.get("generation"),
            "下一班代次": view.get("generation"),
            "交给下一班的代次": view.get("generation"),
            "handover_generation": view.get("generation"),
            "candidate_generation": view.get("candidate_generation"),
            "checksum": view.get("checksum"),
            "content": view.get("text"),
            "text": view.get("text"),
            "交班内容": view.get("text"),
            "下一班日志页": view.get("text"),
            "note": view.get("note"),
            "说明": view.get("note"),
        })
    total = len(evaluated["pages"])
    return {
        "summary": (
            f"核查 {total} 页，修复 {counts['已修复']} 页，"
            f"隔离 {counts['隔离页']} 页。"
        ),
        "handover_summary": f"已为 {handed_count}/{total} 页确定下一班可读日志页。",
        "metrics": counts,
        "rows": rows,
        "handovers": handovers_view,
    }


def run(payload, parameter, db=None, run_id=None):
    """业务入口：计算巡检结论，并在给定数据库连接时原子应用交班账。

    向后兼容 ``run(payload, parameter, db)`` 的三参调用；不传 ``db`` 时只按空账本
    做纯判定（可独立运行/测试）。
    """
    evaluated = evaluate(payload, parameter)
    outcomes = None
    if db is not None:
        candidates = [
            {"page": item["page"], "candidate": item["candidate"], "note": item["note"]}
            for item in evaluated["pages"]
        ]
        outcomes = apply_batch(db, run_id, candidates)
    return compose(evaluated, outcomes)
