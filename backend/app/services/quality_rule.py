"""质量检查领域服务：在通用状态流转之上接入质量阻断事实台账。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.quality_gate import handle_action


class QualityRuleService(BaseModuleService):
    MODULE = "quality_rule"
    ENTITY = "质量检查"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "规则编号", "目标特征", "检查类型", "空值阈值", "异常值阈值", "阻断级别", "采样比例",
        "最近检查", "检查事实", "数据批", "检查轮次", "可证分区", "实际接入范围", "阻断分区", "成稿进度",
    ]
    REQUIRED_FIELDS = ["规则编号", "目标特征", "检查类型"]
    STATUS_ORDER = ["草稿", "监控中", "已阻断", "已暂停"]
    ACTION_RULES = {
        "启用监控": "监控中",
        "确认阻断": "已阻断",
        "暂停规则": "已暂停",
        # 以下动作由质量阻断台账处理，目标状态同样是阻断态，或保持阻断不变。
        "核对进度": "已阻断",
        "申请恢复": "已阻断",
        "提交恢复申请": "已阻断",
        "修复分区": "已阻断",
        "补齐证据": "已阻断",
        "中断写入": "已阻断",
        "重试写入": "已阻断",
        "完成成稿": "已阻断",
        "发起新一轮检查": "已阻断",
    }
    NEGATIVE_ACTIONS = ["确认阻断"]

    def run_action(
        self,
        entry_id: int,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        gate = handle_action(self.MODULE, entry_id, action, params)
        if gate is not None:
            entry = self.get_entry(entry_id)
            if not gate.get("ok"):
                return entry, str(gate.get("message", "质量阻断处理失败")), gate
            return entry, str(gate.get("message", "质量阻断已处理")), gate
        return super().run_action(entry_id, action, params)
