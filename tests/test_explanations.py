"""脱敏解释：向双方说明为何匹配或拒绝，且不泄露竞争报价。"""
import unittest

from service.errors import ForbiddenError
from tests.helpers import make_app, populate, standard_requirement


class ExplanationTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)
        standard_requirement(self.app)
        self.resp_cn = self.app.submit_response(
            "req-1", "sup-cn", {"amount": 80000, "currency": "USD"})
        self.resp_th = self.app.submit_response(
            "req-1", "sup-th", {"amount": 70000, "currency": "USD"})
        self.resp_vn = self.app.submit_response(
            "req-1", "sup-vn", {"amount": 65000, "currency": "USD"})
        self.app.generate_recommendations("req-1")

    def test_supplier_sees_own_explanation_without_competitor_quotes(self):
        view = self.app.explain_for_supplier(self.resp_vn["response_id"])
        self.assertFalse(view["eligible"])
        self.assertIn("category_mismatch", {r["code"] for r in view["reasons"]})
        self.assertEqual(view["your_quote"]["amount"], 65000)
        self.assertEqual(view["budget_ceiling"], {"amount": 100000, "currency": "USD"})
        # 序列化后不得出现任何竞争方报价
        import json
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("80000", raw)
        self.assertNotIn("70000", raw)
        # 也不出现竞争方身份
        self.assertNotIn("sup-cn", raw)
        self.assertNotIn("sup-th", raw)

    def test_ranked_supplier_sees_rank_and_breakdown(self):
        view = self.app.explain_for_supplier(self.resp_cn["response_id"])
        self.assertTrue(view["eligible"])
        self.assertIsNotNone(view["your_rank"])
        self.assertGreaterEqual(view["ranked_count"], 2)
        self.assertTrue(view["score_breakdown"])

    def test_supplier_cannot_get_full_decision(self):
        with self.assertRaises(ForbiddenError):
            self.app.explain_decision("req-1", 1, "sup-cn", role="supplier")

    def test_staff_decision_explains_match_and_reject(self):
        accepted = self.app.explain_decision("req-1", 1, "sup-cn", role="staff")
        self.assertEqual(accepted["decision"], "ranked")
        self.assertIsNotNone(accepted["rank"])
        rejected = self.app.explain_decision("req-1", 1, "sup-vn", role="staff")
        self.assertEqual(rejected["decision"], "rejected")
        self.assertIsNone(rejected["rank"])
        self.assertTrue(rejected["reasons"])

    def test_public_recommendation_hides_quotes_for_supplier_role(self):
        # recommendation_view 由 HTTP 层按角色控制 include_quotes
        hidden = self.app.recommendation_view("req-1", 1, include_quotes=False)
        import json
        raw = json.dumps(hidden, ensure_ascii=False)
        self.assertNotIn("80000", raw)
        self.assertNotIn("70000", raw)
        shown = self.app.recommendation_view("req-1", 1, include_quotes=True)
        raw_shown = json.dumps(shown, ensure_ascii=False)
        self.assertIn("80000", raw_shown)


if __name__ == "__main__":
    unittest.main()
