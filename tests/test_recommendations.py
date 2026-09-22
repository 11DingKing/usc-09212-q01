"""推荐生成、人工留痕、候选退出增量重算。"""
import unittest

from service.errors import ConflictError, NotFoundError, ValidationError
from tests.helpers import make_app, populate, standard_requirement


def setup_ranked(app, amounts=None):
    standard_requirement(app)
    amounts = amounts or {"sup-cn": 80000, "sup-th": 70000, "sup-vn": 90000}
    ids = {}
    for sid, amount in amounts.items():
        ids[sid] = app.submit_response(
            "req-1", sid, {"amount": amount, "currency": "USD"})["response_id"]
    view = app.generate_recommendations("req-1")
    return view, ids


class RecommendationTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)

    def test_ranking_and_rejection_reasons(self):
        view, ids = setup_ranked(self.app)
        # sup-vn 行业不匹配（electronics），应在拒绝列表
        ranked = [c["supplier_id"] for c in view["ranked"]]
        self.assertEqual(set(ranked), {"sup-cn", "sup-th"})
        rejected = {c["supplier_id"]: c for c in view["rejected"]}
        self.assertIn("sup-vn", rejected)
        self.assertFalse(rejected["sup-vn"]["eligible"])
        self.assertIn("category_mismatch",
                      {r["code"] for r in rejected["sup-vn"]["reasons"]})

    def test_generation_freezes_round(self):
        view, _ = setup_ranked(self.app)
        req = self.app.requirement_view("req-1")
        self.assertTrue(req["rounds"][0]["frozen"])
        self.assertEqual(view["generation"], 1)

    def test_over_budget_candidate_rejected(self):
        view, _ = setup_ranked(self.app, {"sup-cn": 120000, "sup-th": 70000})
        rejected = {c["supplier_id"]: c for c in view["rejected"]}
        self.assertIn("sup-cn", rejected)
        self.assertIn("over_budget",
                      {r["code"] for r in rejected["sup-cn"]["reasons"]})

    def test_manual_override_requires_actor_and_reason(self):
        setup_ranked(self.app)
        with self.assertRaises(ValidationError) as ctx:
            self.app.apply_override("req-1", 1, "exclude", "sup-th", "", actor="zhang")
        self.assertEqual(ctx.exception.code, "override_reason_required")

    def test_manual_exclude_is_recorded_and_changes_ranking(self):
        view, ids = setup_ranked(self.app)
        result = self.app.apply_override(
            "req-1", 1, "exclude", "sup-th", "该供应商存在未决合规调查", actor="staff-li")
        self.assertIn("ranking_before", result)
        self.assertEqual(result["ranking_after"], ["sup-cn"])
        view = self.app.recommendation_view("req-1", 1)
        self.assertEqual(len(view["manual_overrides"]), 1)
        ov = view["manual_overrides"][0]
        self.assertEqual(ov["actor"], "staff-li")
        self.assertEqual(ov["reason"], "该供应商存在未决合规调查")
        # 被排除者进入拒绝列表并带人工原因
        th = next(c for c in view["rejected"] if c["supplier_id"] == "sup-th")
        self.assertIn("manually_excluded", {r["code"] for r in th["reasons"]})

    def test_manual_rank_adjustment(self):
        setup_ranked(self.app)
        before = [c["supplier_id"] for c in
                  self.app.recommendation_view("req-1", 1)["ranked"]]
        self.app.apply_override("req-1", 1, "adjust_rank", before[-1],
                                "主办方战略合作优先", actor="staff-wang", position=1)
        after = [c["supplier_id"] for c in
                 self.app.recommendation_view("req-1", 1)["ranked"]]
        self.assertEqual(after[0], before[-1])

    def test_withdrawal_incrementally_recomputes_only_affected(self):
        view, ids = setup_ranked(self.app)
        ranked_before = [c["supplier_id"] for c in view["ranked"]]
        top = ranked_before[0]
        result = self.app.withdraw_response(ids[top])
        recomputed = result["recommendations_recomputed"]
        self.assertEqual(len(recomputed), 1)
        self.assertEqual(recomputed[0]["candidate_id"], top)
        view2 = self.app.recommendation_view("req-1", 1)
        # 只剩另一候选在榜，退出者进拒绝列表；重算记录保留前后名次
        ranked_after = [c["supplier_id"] for c in view2["ranked"]]
        self.assertNotIn(top, ranked_after)
        rec = view2["recomputations"][-1]
        self.assertEqual(rec["ranking_before"], ranked_before)
        self.assertEqual(rec["ranking_after"], ranked_after)
        self.assertEqual(rec["trigger"], "candidate_withdrawn")

    def test_withdrawal_without_recommendations_is_noop(self):
        standard_requirement(self.app)
        rid = self.app.submit_response(
            "req-1", "sup-cn", {"amount": 80000, "currency": "USD"})
        result = self.app.withdraw_response(rid["response_id"])
        self.assertEqual(result["recommendations_recomputed"], [])

    def test_override_blocked_after_confirmation_locks_round(self):
        view, ids = setup_ranked(self.app)
        top = view["ranked"][0]["supplier_id"]
        self.app.open_negotiation("req-1", top)
        neg = self.app.open_negotiation  # noqa
        # 找到洽谈 id 并确认
        negotiation = next(n for n in self.app.negotiations.values()
                           if n.candidate_id == top)
        self.app.confirm_negotiation(negotiation.negotiation_id)
        with self.assertRaises(ConflictError) as ctx:
            self.app.apply_override("req-1", 1, "exclude", top, "x", actor="a")
        self.assertEqual(ctx.exception.code, "recommendation_locked")


if __name__ == "__main__":
    unittest.main()
