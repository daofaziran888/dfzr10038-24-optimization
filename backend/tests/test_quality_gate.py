"""质量阻断台账的回归测试。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import quality_gate  # noqa: E402
from app.services.quality_rule import QualityRuleService  # noqa: E402
from app.services.report_task import ReportTaskService  # noqa: E402
from app.store import store  # noqa: E402


class QualityGateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例重建示例数据与事实台账，互不干扰。
        quality_gate._FACTS.clear()
        quality_gate._FACT_BY_ID.clear()
        quality_gate._FACT_ORDER.clear()
        quality_gate._REPORT_FACTS.clear()
        quality_gate._RULE_LATEST.clear()
        quality_gate._SEQ = 0
        from app.seed import SEED_ROWS

        store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
        self.rule = QualityRuleService()
        self.report = ReportTaskService()
        self.params = {
            "数据批": "B1",
            "检查轮次": "1",
            "检查类型": "空值检查",
            "拦截分区": "P;Q",
            "分区证据": "P:P-原证-001",
        }

    # -- 小工具 -----------------------------------------------------------

    def confirm(self, params: dict | None = None):
        entry, message, gate = self.rule.run_action(1, "确认阻断", dict(params or self.params))
        self.assertIsNotNone(gate)
        return gate

    def drafts(self) -> list[int]:
        return [int(row["id"]) for row in store.rows("report_task") if row["status"] == "草稿"]

    def act(self, action: str, **extra):
        params = {**self.params, **extra}
        gate = self.rule.run_action(1, action, params)[2]
        self.assertIsNotNone(gate, f"动作 {action} 没有进入质量阻断台账")
        return gate

    # -- 去重与并发 -------------------------------------------------------

    def test_repeated_confirm_reuses_single_fact_and_draft(self):
        first = self.confirm()
        second = self.confirm()
        self.assertTrue(second["reused"])
        self.assertEqual(first["fact"]["检查事实"], second["fact"]["检查事实"])
        self.assertEqual(first["report"]["id"], second["report"]["id"])
        self.assertEqual(len(quality_gate.list_facts()), 1)
        self.assertEqual(self.drafts(), [])

    def test_concurrent_confirm_claims_one_draft(self):
        barrier = threading.Barrier(2)
        results: list[dict] = []

        def worker() -> None:
            barrier.wait()
            results.append(self.confirm())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual({item["fact"]["检查事实"] for item in results}, {results[0]["fact"]["检查事实"]})
        self.assertEqual({item["report"]["id"] for item in results}, {results[0]["report"]["id"]})
        self.assertEqual(len(quality_gate.list_facts()), 1)

    def test_same_batch_other_check_type_is_independent(self):
        first = self.confirm()
        second = self.confirm({**self.params, "检查类型": "异常值检查"})
        self.assertNotEqual(first["fact"]["检查事实"], second["fact"]["检查事实"])
        self.assertNotEqual(first["report"]["id"], second["report"]["id"])

    # -- 写入中断与证据 ---------------------------------------------------

    def test_interrupted_write_stops_at_p_proven_q_missing(self):
        self.confirm()
        gate = self.act("中断写入")
        self.assertEqual(gate["fact"]["成稿材料"], {"P": "已写入", "Q": "缺分区证据"})
        self.assertEqual(gate["fact"]["缺证分区"], ["Q"])
        self.assertEqual(gate["report"]["status"], "生成失败")

    def test_finalize_blocked_until_all_evidence_present(self):
        self.confirm()
        gate = self.act("完成成稿")
        self.assertFalse(gate["ok"])
        self.assertIn("Q", gate["message"])
        gate = self.act("补齐证据", 分区证据="Q:Q-原证-002")
        self.assertEqual(gate["fact"]["缺证分区"], [])
        gate = self.act("完成成稿")
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["report"]["status"], "已生成")

    def test_retry_uses_original_draft_and_keeps_scope(self):
        gate = self.confirm()
        report_id = gate["report"]["id"]
        fact_id = gate["fact"]["检查事实"]
        retry = self.report.run_action(report_id, "重试写入", {"检查事实编号": fact_id})[2]
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["report"]["id"], report_id)
        self.assertEqual(self.drafts(), [])

    # -- 恢复申请 ---------------------------------------------------------

    def test_recovery_releases_only_proven_partitions(self):
        fact_id = self.confirm()["fact"]["检查事实"]
        self.act("修复分区", 检查事实编号=fact_id, 分区="P")
        gate = self.act("申请恢复", 检查事实编号=fact_id, 申请分区="P;Q")
        application = gate["recovery"]
        self.assertEqual(application["受理分区"], ["P"])
        self.assertEqual(application["退回分区"], ["Q"])
        self.assertTrue(application["当班可接入"])
        self.assertEqual(gate["fact"]["实际接入范围"], ["P"])
        self.assertEqual(gate["fact"]["阻断分区"], ["Q"])
        self.assertEqual(gate["report"]["实际接入范围"], "P")
        self.assertEqual(gate["report"]["阻断分区"], "Q")

    def test_report_unfinished_but_proven_partition_can_be_accessed(self):
        fact_id = self.confirm()["fact"]["检查事实"]
        self.act("修复分区", 检查事实编号=fact_id, 分区="P")
        gate = self.act("申请恢复", 检查事实编号=fact_id, 申请分区="P")
        self.assertFalse(gate["recovery"]["报告补齐"])
        self.assertTrue(gate["recovery"]["当班可接入"])
        self.assertEqual(gate["recovery"]["留待下班"], ["Q"])

    # -- 成稿与对账 -------------------------------------------------------

    def test_full_flow_finalize_without_reclaiming_and_reconcile(self):
        gate = self.confirm()
        fact_id = gate["fact"]["检查事实"]
        report_id = gate["report"]["id"]
        self.act("修复分区", 检查事实编号=fact_id, 分区="P")
        self.act("申请恢复", 检查事实编号=fact_id, 申请分区="P")
        self.act("补齐证据", 检查事实编号=fact_id, 分区证据="Q:Q-原证-002")
        gate = self.act("完成成稿", 检查事实编号=fact_id)
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["report"]["实际接入范围"], "P")
        self.assertEqual(gate["report"]["阻断分区"], "Q")
        self.assertEqual(self.drafts(), [])

        view = quality_gate.report_view(report_id)
        self.assertEqual(view["fact"]["检查事实"], fact_id)
        self.assertEqual(view["released"], ["P"])
        self.assertEqual(view["blocked"], ["Q"])

        reconcile = self.act("核对进度")
        self.assertTrue(reconcile["ok"])

    def test_existing_report_scope_is_preserved(self):
        gate = self.confirm()
        scope = gate["report"]["特征范围"]
        # 被认领的样例草稿原本带着自己的特征范围，合并后不能丢。
        self.assertIn("质量规则样例1", scope)
        self.assertTrue(scope.startswith("特征报告样例1"))

    # -- 新一轮检查 -------------------------------------------------------

    def test_new_round_gets_independent_fact_and_report(self):
        first = self.confirm()
        second = self.rule.run_action(1, "发起下一批检查", {"拦截分区": "R", "分区证据": "R:R-原证-003"})[2]
        self.assertNotEqual(first["fact"]["检查事实"], second["fact"]["检查事实"])
        self.assertNotEqual(first["report"]["id"], second["report"]["id"])
        self.assertNotEqual(first["report"]["报告来源"], second["report"]["报告来源"])
        self.assertEqual(second["fact"]["数据批"], "B2")
        self.assertEqual(second["fact"]["检查轮次"], "R2")


if __name__ == "__main__":
    unittest.main()
