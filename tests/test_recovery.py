"""重启恢复：重放事件日志还原每轮推荐与洽谈状态。"""
import os
import tempfile
import unittest

from tests.helpers import make_app, populate, standard_requirement


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "events.jsonl")

    def _full_lifecycle(self):
        app = make_app(self.path)
        populate(app)
        standard_requirement(app)
        app.freeze_requirement("req-1")
        app.revise_requirement("req-1", {"title": "包装设备 25 台"})  # 开第 2 轮
        app.submit_response("req-1", "sup-cn", {"amount": 80000, "currency": "USD"})
        app.submit_response("req-1", "sup-th",
                            {"amount": 70000, "currency": "USD"})
        app.generate_recommendations("req-1", 2)
        app.apply_override("req-1", 2, "adjust_rank", "sup-th",
                           "主办方战略合作优先", actor="staff-wang", position=1)
        nid = app.open_negotiation("req-1", "sup-th", round_no=2)["negotiation_id"]
        app.counter_negotiation(nid, 68000, note="议价", actor="buyer")
        app.confirm_negotiation(nid, amount=68000, idempotency_key="k1")
        app.store.close()

    def test_state_restored_after_restart(self):
        self._full_lifecycle()
        app = make_app(self.path)
        # 需求两轮，第 1 轮冻结、第 2 轮由生成推荐时冻结
        view = app.requirement_view("req-1")
        self.assertEqual(view["current_round_no"], 2)
        self.assertEqual(view["rounds"][0]["data"]["title"], "包装设备 20 台")
        self.assertEqual(view["rounds"][1]["data"]["title"], "包装设备 25 台")

        # 实名信息分离恢复
        self.assertEqual(app.vault.get("buyer-A").legal_name, "东盟优品贸易有限公司")
        self.assertTrue(app.vault.is_verified("buyer-A"))

        # 推荐与人工调整恢复
        reco = app.recommendation_view("req-1", 2)
        self.assertEqual(reco["ranked"][0]["supplier_id"], "sup-th")
        self.assertEqual(len(reco["manual_overrides"]), 1)
        self.assertTrue(reco["locked"])

        # 洽谈状态与预算台账恢复
        neg = next(n for n in app.negotiations.values())
        self.assertEqual(neg.status, "confirmed")
        self.assertEqual(neg.committed_amount, 68000)
        ledger = app._budget_ledger("req-1", 2)
        self.assertEqual(ledger["committed"], 68000)
        self.assertEqual(ledger["held"], 0)

        # 幂等表恢复：重启后同键仍回放
        replay = app.confirm_negotiation(neg.negotiation_id, amount=68000,
                                         idempotency_key="k1")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(app._budget_ledger("req-1", 2)["committed"], 68000)
        app.store.close()

    def test_replay_is_deterministic_across_two_restarts(self):
        self._full_lifecycle()
        app1 = make_app(self.path)
        ranking1 = [c["supplier_id"] for c in app1.recommendation_view("req-1", 2)["ranked"]]
        app1.store.close()
        app2 = make_app(self.path)
        ranking2 = [c["supplier_id"] for c in app2.recommendation_view("req-1", 2)["ranked"]]
        self.assertEqual(ranking1, ranking2)
        app1.store.close()
        app2.store.close()


if __name__ == "__main__":
    unittest.main()
