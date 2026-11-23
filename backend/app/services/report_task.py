"""特征报告领域服务：成稿动作落回质量阻断台账，保证草稿不被重领。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.quality_gate import handle_action


class ReportTaskService(BaseModuleService):
    MODULE = "report_task"
    ENTITY = "分析报表"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "报告编号", "报告类型", "特征范围", "统计周期", "图表配置", "输出格式", "分发列表",
        "最近生成", "检查事实", "报告来源", "数据批", "成稿进度", "实际接入范围", "阻断分区",
    ]
    REQUIRED_FIELDS = ["报告编号", "报告类型", "特征范围"]
    STATUS_ORDER = ["草稿", "生成中", "已生成", "生成失败"]
    ACTION_RULES = {
        "启动生成": "生成中",
        "中止生成": "已生成",
        "标记失败": "生成失败",
        "重试写入": "生成中",
        "完成成稿": "已生成",
        "核对进度": "生成中",
    }
    NEGATIVE_ACTIONS = ["标记失败"]

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
                return entry, str(gate.get("message", "报告成稿未完成")), gate
            return entry, str(gate.get("message", "报告成稿已处理")), gate
        return super().run_action(entry_id, action, params)
