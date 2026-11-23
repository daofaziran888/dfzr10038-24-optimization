"""质量阻断的检查事实台账与特征报告成稿编排。

这一层把「质量检查确认阻断」收敛成一条可追踪的检查事实，并把同一事件
产生的异常报告明细稳定交给特征报告。核心约束：

* 同一检查事件（规则 + 检查类型 + 数据批 + 检查轮次）只对应一份检查事实；
  重复确认、并发确认都复用同一份事实，不会反复认领报告草稿。
* 认领草稿、写入报告、恢复补写都在同一把锁内完成，一次事件最多认领一张
  草稿；写入中断后从原成稿动作重试，仍然使用原草稿。
* 分区证据决定实际接入范围：只有取得可证依据并提交恢复申请的分区才会解除
  阻断，缺证分区始终保持阻断。
* 检查事实会同步到质量检查和特征报告两张记录上，刷新页面或重新进入工作台
  都能读取同一份检查依据。
* 既有报告引用的特征范围会保留并合并，不会被同一事件重复改写。

实现只依赖内存仓库，和 app.store 的示例数据保持同一生命周期。
"""
from __future__ import annotations

import datetime
import threading
from typing import Any

from app.store import store

# ---------------------------------------------------------------------------
# 事实台账：模块级内存，按检查事件唯一
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_FACTS: dict[str, dict[str, Any]] = {}
_FACT_BY_ID: dict[str, dict[str, Any]] = {}
_FACT_ORDER: list[str] = []
_REPORT_FACTS: dict[int, str] = {}
_RULE_LATEST: dict[int, str] = {}
_SEQ = 0

CONFIRM_ACTIONS = {
    "确认阻断", "提交阻断", "阻断", "确认质量阻断", "确认接入阻断", "确认阻断接入",
    "空值检查确认阻断",
}
RECOVERY_ACTIONS = {
    "申请恢复", "提交恢复申请", "提交分区恢复接入", "恢复接入", "分区恢复接入",
    "申请分区恢复", "申请恢复接入", "提交恢复接入", "恢复申请",
}
EVIDENCE_ACTIONS = {
    "补齐证据", "补写证据", "登记证据", "更新证据", "恢复补写", "补齐分区证据",
    "补充证据", "补证",
}
REPAIR_ACTIONS = {"修复分区", "标记可证", "确认可证", "修复源数据", "标记已修复"}
FINALIZE_ACTIONS = {
    "完成成稿", "结束成稿", "完成报告", "成稿", "结束本次阻断", "完成本次成稿",
    "中止生成", "结束成稿动作",
}
RETRY_ACTIONS = {
    "重试写入", "恢复写入", "重试成稿", "重试生成", "恢复成稿",
    "启动生成", "重新成稿",
}
RECONCILE_ACTIONS = {"核对进度", "对账", "核对", "核对成稿", "进度对账", "刷新对账"}
NEW_ROUND_ACTIONS = {
    "发起新一轮检查", "新一轮检查", "发起下一批检查", "下一批检查", "发起新一批检查",
    "再发起一批检查", "发起新检查", "新一轮阻断", "发起下一批阻断",
}
INTERRUPT_ACTIONS = {"中断写入", "写入中断", "模拟写入中断", "中断成稿"}

BATCH_KEYS = ("数据批", "数据批次", "批次", "检查批次", "批次号", "batch", "batch_no")
ROUND_KEYS = ("检查轮次", "轮次", "检查轮", "round")
CHECK_TYPE_KEYS = ("检查类型", "检查项", "check_type", "checkType")
PARTITION_KEYS = ("拦截分区", "数据分区", "分区列表", "阻断分区", "分区", "partitions")
EVIDENCE_KEYS = ("分区证据", "检查证据", "证据", "检查原证", "原证", "evidence")
RECOVERY_PART_KEYS = ("恢复分区", "申请分区", "解除分区", "接入分区", "分区", "partitions")
REPAIR_TARGET_KEYS = ("修复分区", "修复数据分区", "已修复分区", "分区", "partitions")
NEW_PART_KEYS = ("新增分区", "补充分区", "分区", "partitions")
FACT_ID_KEYS = ("检查事实编号", "检查事实", "事实编号", "fact_id", "factId", "block_id")

_SPLIT_CHARS = ";；,，|、/\n\t"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _first_param(params: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in params:
            value = params.get(key)
            if value is not None and value != "" and value != [] and value != {}:
                return value
    return None


def _as_list(value: Any) -> list[str]:
    """把分号/逗号/顿号分隔的字符串或列表统一成去重后的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, dict):
        raw: list[Any] = list(value.keys())
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    result: list[str] = []
    for item in raw:
        for piece in str(item).replace("\n", ";").split(";"):
            for part in _split_mixed(piece):
                name = part.strip()
                if name and name not in result:
                    result.append(name)
    return result


def _split_mixed(text: str) -> list[str]:
    result = [text]
    for char in _SPLIT_CHARS:
        if char == ";":
            continue
        next_result: list[str] = []
        for piece in result:
            next_result.extend(piece.split(char))
        result = next_result
    return result


def _as_evidence(value: Any) -> dict[str, str]:
    """解析分区证据：支持 {"P": "..."} 与 "P:...;Q:..." 两种写法。"""
    result: dict[str, str] = {}
    if value is None:
        return result
    if isinstance(value, dict):
        for key, val in value.items():
            name = _text(key)
            if name:
                result[name] = _text(val)
        return result
    for chunk in str(value).replace("；", ";").replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        separator = ":" if ":" in chunk else ("：" if "：" in chunk else None)
        if separator:
            name, _, detail = chunk.partition(separator)
            result[_text(name)] = _text(detail)
        else:
            result[chunk] = chunk
    return result


def _merge_scope(*scopes: Any) -> str:
    """合并特征范围，保留既有报告引用顺序，重复项只出现一次。"""
    merged: list[str] = []
    for scope in scopes:
        for name in _as_list(scope):
            if name not in merged:
                merged.append(name)
    return ";".join(merged)


def _canonical_check_type(value: Any) -> str:
    text = _text(value)
    if text in {"空值", "空值校验", "空值检查", "null", "null_check"}:
        return "空值检查"
    if text in {"异常值", "异常值校验", "异常值检查", "outlier", "outlier_check"}:
        return "异常值检查"
    return text or "空值检查"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _next_report_no() -> str:
    numbers = []
    for row in store.rows("report_task"):
        text = _text(row.get("报告编号"))
        if text.startswith("REPO-") and text[5:].isdigit():
            numbers.append(int(text[5:]))
    return f"REPO-{max(numbers, default=0) + 1:04d}"


def _next_batch(batch: str) -> str:
    """按末位字母/数字递推数据批标识，保持批次可读且不重复。"""
    text = _text(batch)
    if not text:
        return "BATCH-1"
    if text[-1].isalpha():
        base = text[:-1]
        letter = chr(ord(text[-1].upper()) + 1)
        return f"{base}{letter}"
    if text[-1].isdigit():
        index = len(text) - 1
        while index >= 0 and text[index].isdigit():
            index -= 1
        prefix, digits = text[: index + 1], text[index + 1:]
        return f"{prefix}{int(digits) + 1}"
    return f"{text}-1"


def _round_value(value: Any) -> str:
    text = _text(value)
    if not text:
        return "R1"
    if text.upper().startswith("R"):
        return text.upper()
    return f"R{text}"


def _fact_by_id(fact_id: str) -> dict[str, Any] | None:
    return _FACT_BY_ID.get(_text(fact_id))


def _latest_fact_for_rule(rule_id: int) -> dict[str, Any] | None:
    fact_id = _RULE_LATEST.get(rule_id)
    return _FACTS.get(fact_id) if fact_id else None


def _report_fact(report_id: int) -> dict[str, Any] | None:
    fact_id = _REPORT_FACTS.get(report_id)
    return _FACTS.get(fact_id) if fact_id else None


def _public_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """对外输出的快照；直接深拷贝一层，避免调用方改到台账。"""
    result: dict[str, Any] = {}
    for key, value in fact.items():
        if isinstance(value, list):
            result[key] = [dict(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            result[key] = dict(value)
        else:
            result[key] = value
    return result


def _evidence_text(evidence: dict[str, str]) -> str:
    if not evidence:
        return ""
    return ";".join(
        f"{name}:{detail or '缺证据'}" for name, detail in evidence.items()
    )


def _refresh_material(fact: dict[str, Any]) -> None:
    """按当前证据重算成稿材料覆盖情况；缺证分区如实标出。"""
    material: dict[str, str] = {}
    for part in fact["拦截分区"]:
        if _text(fact["分区证据"].get(part)):
            material[part] = "已写入"
        else:
            material[part] = "缺分区证据"
    fact["成稿材料"] = material
    missing = [part for part in fact["拦截分区"] if material[part] != "已写入"]
    fact["缺证分区"] = missing
    fact["可证分区"] = [part for part in fact["拦截分区"] if part not in missing]
    fact["阻断分区"] = [
        part for part in fact["拦截分区"] if part not in fact["已解除"]
    ]


def _material_state(fact: dict[str, Any]) -> str:
    if fact.get("写入状态") == "interrupted":
        return "中断"
    if fact["缺证分区"]:
        return "缺证据"
    return "完整"


def _material_summary(fact: dict[str, Any]) -> str:
    state = _material_state(fact)
    if state == "完整":
        return "成稿材料已齐备"
    written = "、".join(part for part, state_text in fact["成稿材料"].items() if state_text == "已写入")
    lacking = "、".join(fact["缺证分区"])
    prefix = "成稿写入中断" if state == "中断" else "成稿材料缺分区证据"
    if lacking:
        if written:
            return f"{prefix}：{written} 已写入；{lacking} 缺分区证据"
        return f"{prefix}：{lacking} 缺分区证据"
    return f"{prefix}：已写入分区未确认落盘"


# ---------------------------------------------------------------------------
# 事件身份与事实构造
# ---------------------------------------------------------------------------


def _event_parts(rule: dict[str, Any], params: dict[str, Any]) -> dict[str, str]:
    batch = _text(_first_param(params, BATCH_KEYS) or rule.get("数据批")) or "默认批"
    check_type = _canonical_check_type(_first_param(params, CHECK_TYPE_KEYS) or rule.get("检查类型"))
    round_text = _round_value(_first_param(params, ROUND_KEYS) or rule.get("检查轮次"))
    return {"batch": batch, "check_type": check_type, "round": round_text}


def _event_key(rule_id: int, parts: dict[str, str]) -> str:
    return f"{rule_id}|{parts['check_type']}|{parts['batch']}|{parts['round']}"


def _new_fact(rule: dict[str, Any], params: dict[str, Any], parts: dict[str, str]) -> dict[str, Any]:
    seq = _next_seq()
    fact_id = f"BLK-{seq:04d}"
    partitions = _as_list(_first_param(params, PARTITION_KEYS) or rule.get("拦截分区"))
    evidence = _as_evidence(_first_param(params, EVIDENCE_KEYS) or rule.get("分区证据"))
    fact: dict[str, Any] = {
        "检查事实": fact_id,
        "规则ID": int(rule.get("id", 0)),
        "规则编号": _text(rule.get("规则编号")),
        "目标特征": _text(rule.get("目标特征")),
        "检查类型": parts["check_type"],
        "数据批": parts["batch"],
        "检查轮次": parts["round"],
        "事件键": _event_key(int(rule.get("id", 0)), parts),
        "拦截分区": partitions,
        "分区证据": evidence,
        "可证分区": [],
        "阻断分区": list(partitions),
        "实际接入范围": [],
        "已解除": [],
        "成稿材料": {},
        "缺证分区": [],
        "报告ID": None,
        "报告编号": "",
        "报告来源": "",
        "成稿动作": "",
        "成稿状态": "待写入",
        "写入状态": "pending",
        "恢复申请": [],
        "日志": [],
        "确认次数": 1,
        "创建时间": _now(),
        "更新时间": _now(),
    }
    _refresh_material(fact)
    return fact


def _log(fact: dict[str, Any], action: str, message: str, **extra: Any) -> None:
    record = {"时间": _now(), "动作": action, "说明": message}
    record.update(extra)
    fact["日志"].append(record)
    fact["更新时间"] = record["时间"]


# ---------------------------------------------------------------------------
# 报告认领：同一事实只占一张草稿
# ---------------------------------------------------------------------------


def _report_fact_id(row: dict[str, Any]) -> str:
    return _text(row.get("检查事实"))


def _claim_report(fact: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    """在锁内认领一张草稿；同一事实已有认领则直接复用，绝不重复占用。"""
    rows = store.rows("report_task")
    claimed = None
    # 1) 本事实此前已认领过，继续用那一张（哪怕已进入成稿状态）。
    if fact.get("报告ID"):
        claimed = store.find("report_task", int(fact["报告ID"]))
    # 2) 报告行上已经写着本事实编号，也视为同一认领。
    if claimed is None:
        for row in rows:
            if _report_fact_id(row) == fact["检查事实"]:
                claimed = row
                break
    # 3) 否则只从尚未被任何事实占用的草稿里挑一张。
    if claimed is None:
        for row in rows:
            if int(row.get("id", 0)) in _REPORT_FACTS:
                continue
            link = _report_fact_id(row)
            if link and link != fact["检查事实"]:
                continue
            if _text(row.get("status")) == "草稿":
                claimed = row
                break
    if claimed is None:
        # 没有空闲草稿时补一张，仍然只属于当前事实。
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        claimed = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "报告编号": _next_report_no(),
            "报告类型": "质量异常报告",
            "特征范围": "",
            "统计周期": "",
            "图表配置": "",
            "输出格式": "",
            "分发列表": "",
            "最近生成": "",
        }
        rows.append(claimed)

    report_id = int(claimed.get("id", 0))
    fact["报告ID"] = report_id
    fact["报告编号"] = _text(claimed.get("报告编号"))
    fact["报告来源"] = f"quality_rule/{fact['规则编号']}@{fact['数据批']}/{fact['检查轮次']}"
    fact["成稿动作"] = "完成成稿"
    _REPORT_FACTS[report_id] = fact["检查事实"]

    # 兼容既有报告引用的特征范围：保留原范围，再并入本次阻断的目标特征。
    existing_scope = claimed.get("特征范围")
    claimed["特征范围"] = _merge_scope(existing_scope, fact["目标特征"])
    claimed["报告类型"] = "质量异常报告"
    claimed["检查事实"] = fact["检查事实"]
    claimed["报告来源"] = fact["报告来源"]
    claimed["数据批"] = fact["数据批"]
    claimed["检查轮次"] = fact["检查轮次"]
    claimed["触发规则"] = fact["规则编号"]
    claimed["异常报告明细"] = _abnormal_detail(fact)
    claimed["拦截分区"] = ";".join(fact["拦截分区"])
    claimed["分区证据"] = _evidence_text(fact["分区证据"])
    claimed["上报时间"] = _now()
    _log(fact, "认领草稿", f"为检查事实 {fact['检查事实']} 认领报告草稿 {fact['报告编号']}")
    return claimed


def _abnormal_detail(fact: dict[str, Any]) -> str:
    detail = [
        f"规则 {fact['规则编号']} 在数据批 {fact['数据批']} 的{fact['检查类型']}中超阈",
        f"目标特征 {fact['目标特征']}",
        f"拦截分区 {'、'.join(fact['拦截分区']) or '无'}",
    ]
    evidence_names = [name for name in fact["拦截分区"] if _text(fact["分区证据"].get(name))]
    lacking = [name for name in fact["拦截分区"] if name not in evidence_names]
    if evidence_names:
        detail.append(f"已有可验证检查原证：{'、'.join(evidence_names)}")
    if lacking:
        detail.append(f"尚缺证据：{'、'.join(lacking)}")
    return "；".join(detail)


def _write_report(fact: dict[str, Any], *, stop_on_missing: bool = True) -> None:
    """按事实重写成稿材料；缺证分区停在原位并如实记录。"""
    report = store.find("report_task", int(fact["报告ID"])) if fact.get("报告ID") else None
    _refresh_material(fact)
    complete = not fact["缺证分区"]
    was_finished = fact.get("成稿状态") == "成稿完成"
    if complete:
        fact["写入状态"] = "ok"
        if not was_finished:
            fact["成稿状态"] = "待成稿"
    else:
        fact["写入状态"] = "interrupted" if stop_on_missing else "partial"
        if not was_finished:
            fact["成稿状态"] = "写入中断"
    if report is not None:
        report["特征范围"] = _merge_scope(report.get("特征范围"), fact["目标特征"])
        report["异常报告明细"] = _abnormal_detail(fact)
        report["检查事实"] = fact["检查事实"]
        report["报告来源"] = fact["报告来源"]
        report["拦截分区"] = ";".join(fact["拦截分区"])
        report["分区证据"] = _evidence_text(fact["分区证据"])
        report["实际接入范围"] = ";".join(fact["已解除"])
        report["阻断分区"] = ";".join(fact["阻断分区"])
        report["成稿进度"] = _material_summary(fact)
        report["最近生成"] = _now()
        report["成稿动作"] = fact["成稿动作"]
        report["成稿材料"] = ";".join(
            f"{part}:{state}" for part, state in fact["成稿材料"].items()
        )
        if fact.get("成稿状态") == "成稿完成":
            report["status"] = "已生成"
            report["pending"] = False
        else:
            report["status"] = "生成失败" if not complete else "生成中"
            report["pending"] = True


def _sync_rule(rule: dict[str, Any], fact: dict[str, Any]) -> None:
    rule["检查事实"] = fact["检查事实"]
    rule["数据批"] = fact["数据批"]
    rule["检查轮次"] = fact["检查轮次"]
    rule["拦截分区"] = ";".join(fact["拦截分区"])
    rule["分区证据"] = _evidence_text(fact["分区证据"])
    rule["可证分区"] = ";".join(fact["可证分区"])
    rule["实际接入范围"] = ";".join(fact["已解除"])
    rule["阻断分区"] = ";".join(fact["阻断分区"])
    rule["成稿进度"] = _material_summary(fact)
    rule["最近检查"] = fact["更新时间"]
    rule["status"] = "已阻断"
    rule["pending"] = True
    rule["abnormal"] = bool(fact["阻断分区"])


# ---------------------------------------------------------------------------
# 对外动作：确认阻断（幂等 + 并发安全）
# ---------------------------------------------------------------------------


def confirm_block(rule_id: int, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """确认阻断：同一事件只建一份事实、只认领一份草稿。"""
    params = dict(params or {})
    with _LOCK:
        rule = store.find("quality_rule", rule_id)
        if rule is None:
            return {"ok": False, "message": f"质量检查 {rule_id} 不存在或已归档"}
        parts = _event_parts(rule, params)
        key = _event_key(rule_id, parts)
        fact = _FACTS.get(key)
        reused = fact is not None
        if fact is None:
            fact = _new_fact(rule, params, parts)
            _FACTS[key] = fact
            _FACT_BY_ID[fact["检查事实"]] = fact
            _FACT_ORDER.append(key)
            _RULE_LATEST[rule_id] = key
        else:
            fact["确认次数"] = int(fact.get("确认次数", 0)) + 1
            # 后续确认只补新证据/分区，不改事件身份，也不重复认领。
            extra_parts = _as_list(_first_param(params, PARTITION_KEYS))
            for part in extra_parts:
                if part not in fact["拦截分区"]:
                    fact["拦截分区"].append(part)
            fact["分区证据"].update(
                _as_evidence(_first_param(params, EVIDENCE_KEYS))
            )
            _log(fact, "重复确认", "同一检查事件再次确认，复用既有检查事实")

        _claim_report(fact, rule)
        # 已经成稿结束的事实，重复确认只补记录，不把成稿打回生成态。
        if fact.get("成稿状态") != "成稿完成":
            _write_report(fact)
        _sync_rule(rule, fact)
        detail = (
            "复用已有检查事实，未重复认领草稿"
            if reused else "已建立检查事实并认领报告草稿"
        )
        if fact.get("成稿状态") == "成稿完成":
            detail += "；该事件已成稿结束，本次只补记确认"
        _log(fact, "确认阻断", detail)
        return {
            "ok": True,
            "message": f"质量检查已阻断：{detail}（{fact['检查事实']}）",
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
            "reused": reused,
        }


# ---------------------------------------------------------------------------
# 证据 / 修复 / 恢复申请
# ---------------------------------------------------------------------------


def _resolve_fact(rule_id: int | None, params: dict[str, Any]) -> dict[str, Any] | None:
    explicit = _text(_first_param(params, FACT_ID_KEYS))
    if explicit:
        return _fact_by_id(explicit)
    if rule_id is not None:
        return _latest_fact_for_rule(rule_id)
    return None


def apply_evidence(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """为某一检查事实补记分区证据（不另起事实、不重领草稿）。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法补写证据"}
        incoming = _as_evidence(_first_param(params, EVIDENCE_KEYS))
        new_parts = _as_list(_first_param(params, NEW_PART_KEYS))
        for part in new_parts:
            if part not in fact["拦截分区"]:
                fact["拦截分区"].append(part)
        if not incoming and new_parts:
            incoming = {part: "已复核" for part in new_parts}
        for name, detail in incoming.items():
            fact["分区证据"][name] = detail or "已复核"
            if name not in fact["拦截分区"]:
                fact["拦截分区"].append(name)
        _log(fact, "补写证据", f"补记分区证据 {_evidence_text(incoming) or '无'}")
        _write_report(fact, stop_on_missing=False)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
        return {
            "ok": True,
            "message": f"检查事实 {fact['检查事实']} 已补写证据；未重新认领草稿",
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
        }


def repair_partitions(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """修复源数据后把分区标记为可证，但解除阻断仍要经过恢复申请。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法登记修复"}
        target = _as_list(_first_param(params, REPAIR_TARGET_KEYS))
        evidence = _as_evidence(_first_param(params, EVIDENCE_KEYS))
        if target:
            for part in target:
                fact["分区证据"][part] = evidence.get(part) or "源数据已修复，分区可证"
                if part not in fact["拦截分区"]:
                    fact["拦截分区"].append(part)
        _log(fact, "修复分区", f"分区 {'、'.join(target) or '全部'} 修复后可证")
        _write_report(fact, stop_on_missing=False)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
        return {
            "ok": True,
            "message": f"检查事实 {fact['检查事实']} 的分区已标记可证，等待恢复申请",
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
        }


def request_recovery(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """提交恢复申请：只解除有可证依据的分区，其余继续阻断。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法提交恢复申请"}
        wanted = _as_list(_first_param(params, RECOVERY_PART_KEYS))
        if not wanted:
            wanted = [part for part in fact["拦截分区"] if _text(fact["分区证据"].get(part))]
        accepted: list[str] = []
        rejected: list[str] = []
        for part in wanted:
            if _text(fact["分区证据"].get(part)):
                accepted.append(part)
            else:
                rejected.append(part)
        for part in accepted:
            if part not in fact["已解除"]:
                fact["已解除"].append(part)
        _refresh_material(fact)
        fact["实际接入范围"] = [
            part for part in fact["拦截分区"] if part in fact["已解除"]
        ]
        application = {
            "时间": _now(),
            "申请分区": wanted,
            "受理分区": accepted,
            "退回分区": rejected,
            "报告补齐": not fact["缺证分区"],
            # 可接入只看分区本身是否有可证依据；报告未补齐只影响成稿，
            # 不影响已可证分区的实际接入。
            "当班可接入": bool(accepted),
            "留待下班": rejected or list(fact["缺证分区"]),
        }
        fact["恢复申请"].append(application)
        if rejected:
            message = (
                f"恢复申请已登记：{'、'.join(accepted) or '无可证分区'} 可按可证分区处理；"
                f"{'、'.join(rejected)} 尚无解除依据，继续阻断"
            )
            if fact["缺证分区"]:
                message += "；报告未补齐，可留待下班继续办理"
        elif accepted:
            message = f"恢复申请已受理：{'、'.join(accepted)} 可恢复接入"
            if fact["缺证分区"]:
                message += (
                    f"；报告仍缺 {'、'.join(fact['缺证分区'])} 的分区证据，"
                    "成稿留待下班继续办理"
                )
        else:
            message = "当前没有取得可证依据的分区，阻断保持不变"
        _log(fact, "申请恢复", message, 当班可接入=application["当班可接入"])
        _write_report(fact, stop_on_missing=False)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
        return {
            "ok": True,
            "message": message,
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
            "recovery": application,
        }


# ---------------------------------------------------------------------------
# 成稿：中断、重试、结束本次阻断
# ---------------------------------------------------------------------------


def interrupt_write(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """把报告写入停在当前位置：已有证据的分区写进去，缺证分区留空。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法中断写入"}
        _refresh_material(fact)
        _log(fact, "中断写入", _material_summary(fact))
        _write_report(fact)
        fact["写入状态"] = "interrupted"
        if fact.get("成稿状态") != "成稿完成":
            fact["成稿状态"] = "写入中断"
        report = _report_payload(fact)
        if report is not None:
            report["写入状态"] = "interrupted"
            report["成稿进度"] = _material_summary(fact)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
        return {
            "ok": True,
            "message": f"报告写入中断，停在校验位置：{_material_summary(fact)}",
            "fact": _public_fact(fact),
            "report": report,
        }


def retry_finalize(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """从原成稿动作重试：沿用原草稿与原事实，不重新认领。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法重试成稿"}
        report_id = fact.get("报告ID")
        if not report_id or store.find("report_task", int(report_id)) is None:
            return {"ok": False, "message": "原成稿草稿不存在，无法从原动作重试"}
        _refresh_material(fact)
        _write_report(fact)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
        report = _report_payload(fact)
        if fact["缺证分区"]:
            message = f"重试未完成：{_material_summary(fact)}；本次仍不重新认领草稿"
            ok = False
        else:
            if report is not None:
                report["写入状态"] = "ok"
            report_scope = set(_as_list((report or {}).get("实际接入范围")))
            fact_scope = set(fact["已解除"])
            if report_scope == fact_scope:
                message = (
                    f"已从原成稿动作重试完成，报告 {fact['报告编号']} 可结束成稿；"
                    f"实际接入范围与阻断证据一致（{'、'.join(fact['已解除']) or '无'}）"
                )
            else:
                message = (
                    f"已从原成稿动作重试完成，报告 {fact['报告编号']} 可结束成稿；"
                    f"接入范围待复核：报告 {sorted(report_scope)} / 事实 {sorted(fact_scope)}"
                )
            ok = True
        _log(fact, "重试写入", message)
        return {
            "ok": ok,
            "message": message,
            "fact": _public_fact(fact),
            "report": report,
        }


def finalize_report(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """特征报告依据异常报告明细结束本次阻断的成稿。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法完成成稿"}
        _refresh_material(fact)
        _write_report(fact)
        if fact["缺证分区"]:
            message = (
                f"成稿材料缺分区证据，无法结束本次阻断：{'、'.join(fact['缺证分区'])}"
            )
            _log(fact, "完成成稿", message)
            return {
                "ok": False,
                "message": message,
                "fact": _public_fact(fact),
                "report": _report_payload(fact),
            }
        report = store.find("report_task", int(fact["报告ID"]))
        if report is not None:
            report["status"] = "已生成"
            report["pending"] = False
            report["成稿状态"] = "成稿完成"
            report["成稿动作"] = "完成成稿"
            report["最近生成"] = _now()
            report["成稿进度"] = _material_summary(fact)
        fact["成稿状态"] = "成稿完成"
        fact["写入状态"] = "ok"
        blocked = fact["阻断分区"]
        message = (
            f"特征报告 {fact['报告编号']} 已结束本次阻断成稿；"
            f"实际接入范围 {'、'.join(fact['已解除']) or '无'}；"
            f"仍阻断分区 {'、'.join(blocked) or '无'}"
        )
        _log(fact, "完成成稿", message)
        rule = store.find("quality_rule", int(fact["规则ID"]))
        if rule is not None:
            _sync_rule(rule, fact)
            rule["成稿进度"] = _material_summary(fact)
        return {
            "ok": True,
            "message": message,
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
        }


# ---------------------------------------------------------------------------
# 新一轮检查：同一规则的新事件得到独立事实
# ---------------------------------------------------------------------------


def open_new_round(rule_id: int, params: dict[str, Any]) -> dict[str, Any]:
    """修复源数据后再检查：新数据批/新轮次生成独立事实与独立草稿。"""
    with _LOCK:
        rule = store.find("quality_rule", rule_id)
        if rule is None:
            return {"ok": False, "message": f"质量检查 {rule_id} 不存在或已归档"}
        # 默认参数以上一条事实为基准递推，避免把上一次的事件身份带进新轮次。
        current = _latest_fact_for_rule(rule_id)
        base_batch = _text(current["数据批"]) if current else (_text(rule.get("数据批")) or "B1")
        base_round = _text(current["检查轮次"]) if current else _text(rule.get("检查轮次"))
        batch = _text(_first_param(params, BATCH_KEYS)) or _next_batch(base_batch)
        round_text = _text(_first_param(params, ROUND_KEYS))
        if not round_text:
            digits = base_round.lstrip("Rr")
            numbers = int(digits) if digits.isdigit() else 0
            round_text = _round_value(numbers + 1)
        parts = {
            "batch": batch,
            "check_type": _canonical_check_type(
                _first_param(params, "检查类型") or rule.get("检查类型")
            ),
            "round": _round_value(round_text),
        }
        # 如果推导出的身份仍然撞上已有事实，继续推进轮次，保证独立。
        while _event_key(rule_id, parts) in _FACTS:
            digits = parts["round"].lstrip("Rr")
            numbers = int(digits) if digits.isdigit() else 0
            parts["round"] = _round_value(numbers + 1)
        batch, round_text = parts["batch"], parts["round"]
        next_params = dict(params)
        next_params["数据批"] = batch
        next_params["检查轮次"] = round_text
        if not _first_param(next_params, "检查类型"):
            next_params["检查类型"] = _canonical_check_type(rule.get("检查类型"))
        result = confirm_block(rule_id, next_params)
        new_fact = _fact_by_id(_text((result.get("fact") or {}).get("检查事实")))
        if new_fact is not None and not result.get("reused"):
            _log(new_fact, "新一轮检查",
                 f"数据批 {batch} / 轮次 {round_text} 独立成检查事实")
        elif result.get("reused"):
            return {**result, "ok": False,
                    "message": "新一轮检查被识别为同一事件，请调整数据批或检查轮次"}
        return result


# ---------------------------------------------------------------------------
# 对账与查询
# ---------------------------------------------------------------------------


def _report_payload(fact: dict[str, Any]) -> dict[str, Any] | None:
    report_id = fact.get("报告ID")
    if not report_id:
        return None
    report = store.find("report_task", int(report_id))
    return dict(report) if report is not None else None


def reconcile(rule_id: int | None, params: dict[str, Any]) -> dict[str, Any]:
    """按事实与成稿的关联核对进度，兼容刷新后重新进入工作台。"""
    with _LOCK:
        fact = _resolve_fact(rule_id, params)
        if fact is None:
            return {"ok": False, "message": "未找到对应的检查事实，无法对账"}
        report = _report_payload(fact)
        if report is None:
            return {"ok": False, "message": "检查事实尚未关联报告，无法对账"}
        _refresh_material(fact)
        linked = _text(report.get("检查事实")) == fact["检查事实"]
        scope_ok = _text(fact["目标特征"]) in _as_list(report.get("特征范围"))
        scope_parts = [part for part in _as_list(report.get("拦截分区")) if part]
        progress = "一致" if linked and scope_ok else "需复核"
        message = (
            f"报告 {fact['报告编号']} 与事实 {fact['检查事实']} 关联{progress}；"
            f"{_material_summary(fact)}；实际接入范围 {'、'.join(fact['实际接入范围']) or '无'}"
        )
        return {
            "ok": linked and scope_ok,
            "message": message,
            "linked": linked,
            "scope": _as_list(report.get("特征范围")),
            "partitions": scope_parts,
            "fact": _public_fact(fact),
            "report": report,
        }


def fact_overview(rule_id: int | None = None,
                  report_id: int | None = None) -> dict[str, Any]:
    """工作台/报告页共用的检查依据视图。"""
    with _LOCK:
        fact = None
        if rule_id is not None:
            fact = _latest_fact_for_rule(rule_id)
        if fact is None and report_id is not None:
            fact = _report_fact(report_id)
        if fact is None and rule_id is None and report_id is None:
            return {"facts": [_public_fact(_FACTS[key]) for key in _FACT_ORDER]}
        if fact is None:
            return {"ok": False, "message": "未找到检查事实"}
        return {
            "ok": True,
            "fact": _public_fact(fact),
            "report": _report_payload(fact),
            "evidence": dict(fact["分区证据"]),
            "material": dict(fact["成稿材料"]),
            "blocked": list(fact["阻断分区"]),
            "released": list(fact["已解除"]),
        }


def list_facts() -> list[dict[str, Any]]:
    with _LOCK:
        return [_public_fact(_FACTS[key]) for key in _FACT_ORDER]


# ---------------------------------------------------------------------------
# 动作分派：供质量检查服务与报告服务共用
# ---------------------------------------------------------------------------


def handle_action(module: str, rule_id: int, action: str,
                  params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """识别质量阻断相关动作；不属于本层时返回 None，交回基础骨架。"""
    params = dict(params or {})
    action = _text(action)
    if module == "quality_rule":
        if action in CONFIRM_ACTIONS:
            return confirm_block(rule_id, params)
        if action in EVIDENCE_ACTIONS or action in REPAIR_ACTIONS:
            if action in REPAIR_ACTIONS:
                return repair_partitions(rule_id, params)
            return apply_evidence(rule_id, params)
        if action in RECOVERY_ACTIONS:
            return request_recovery(rule_id, params)
        if action in NEW_ROUND_ACTIONS:
            return open_new_round(rule_id, params)
        if action in INTERRUPT_ACTIONS:
            return interrupt_write(rule_id, params)
        if action in RECONCILE_ACTIONS:
            return reconcile(rule_id, params)
        if action in RETRY_ACTIONS:
            return retry_finalize(rule_id, params)
        if action in FINALIZE_ACTIONS:
            return finalize_report(rule_id, params)
        return None
    if module == "report_task":
        # 报告页的动作同样落在事实台账上：按报告反查事实，再走同一套逻辑。
        fact = _report_fact(rule_id)
        if fact is None:
            row = store.find("report_task", rule_id)
            if row is not None and _text(row.get("检查事实")):
                fact = _fact_by_id(_text(row.get("检查事实")))
        if fact is None:
            return None
        source = int(fact["规则ID"])
        focused = {**params, "检查事实编号": fact["检查事实"]}
        if action in RETRY_ACTIONS:
            return retry_finalize(source, focused)
        if action in INTERRUPT_ACTIONS:
            return interrupt_write(source, focused)
        if action in FINALIZE_ACTIONS:
            return finalize_report(source, focused)
        if action in RECONCILE_ACTIONS:
            return reconcile(source, focused)
        if action in EVIDENCE_ACTIONS:
            return apply_evidence(source, focused)
        if action in RECOVERY_ACTIONS:
            return request_recovery(source, focused)
        return None
    return None


def report_view(report_id: int) -> dict[str, Any]:
    """报告页读取：返回报告行以及其关联的检查依据。"""
    with _LOCK:
        report = store.find("report_task", report_id)
        if report is None:
            return {"ok": False, "message": "报告不存在"}
        fact = _report_fact(report_id)
        if fact is None:
            linked = _text(report.get("检查事实"))
            fact = _fact_by_id(linked) if linked else None
        payload: dict[str, Any] = {"ok": True, "report": dict(report)}
        if fact is not None:
            payload.update({
                "fact": _public_fact(fact),
                "evidence": dict(fact["分区证据"]),
                "material": dict(fact["成稿材料"]),
                "blocked": list(fact["阻断分区"]),
                "released": list(fact["已解除"]),
            })
        return payload


def attach_report_detail(rule_id: int) -> dict[str, Any] | None:
    """把同一事件的异常报告明细稳定写回已认领报告；没有认领则不新建。"""
    with _LOCK:
        fact = _latest_fact_for_rule(rule_id)
        if fact is None or not fact.get("报告ID"):
            return None
        report = store.find("report_task", int(fact["报告ID"]))
        if report is None or _REPORT_FACTS.get(int(report["id"])) != fact["检查事实"]:
            return None
        report["异常报告明细"] = _abnormal_detail(fact)
        report["检查事实"] = fact["检查事实"]
        report["报告来源"] = fact["报告来源"]
        report["特征范围"] = _merge_scope(report.get("特征范围"), fact["目标特征"])
        report["成稿进度"] = _material_summary(fact)
        report["成稿材料"] = ";".join(
            f"{part}:{state}" for part, state in fact["成稿材料"].items()
        )
        return {
            "报告ID": int(report["id"]),
            "报告编号": _text(report.get("报告编号")),
            "检查事实": fact["检查事实"],
            "明细": report["异常报告明细"],
            "特征范围": report["特征范围"],
        }
