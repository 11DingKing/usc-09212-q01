"""匹配引擎硬约束与评分测试。"""
import unittest

from service import matcher
from tests.helpers import make_app, populate, standard_requirement


def assess(app: "HubApplication", requirement_id="req-1", round_no=None):
    req = app.catalog.get_requirement(requirement_id)
    target = req.round(round_no)
    snap = {**target.snapshot(), "buyer_ref": req.buyer_ref}
    return {sid: matcher.assess(snap, app.catalog.get_supplier(sid))
            for sid in ("sup-cn", "sup-th", "sup-vn")}, snap


class HardConstraintTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)

    def test_category_and_window_pass_for_eligible_supplier(self):
        standard_requirement(self.app)
        results, _ = assess(self.app)
        self.assertTrue(results["sup-cn"].eligible)
        self.assertTrue(results["sup-th"].eligible)

    def test_origin_whitelist_rejects(self):
        standard_requirement(self.app, allowed_origin_countries=["ID"])
        results, _ = assess(self.app)
        self.assertFalse(results["sup-cn"].eligible)
        self.assertFalse(results["sup-th"].eligible)
        codes = {r["code"] for r in results["sup-vn"].reasons}
        self.assertIn("origin_not_allowed", codes)

    def test_language_gate(self):
        standard_requirement(self.app, languages=["km"])
        results, _ = assess(self.app)
        for result in results.values():
            self.assertFalse(result.eligible)
            self.assertIn("language_unsupported",
                          {r["code"] for r in result.reasons})

    def test_delivery_window_no_overlap_rejects(self):
        standard_requirement(self.app,
                             delivery_window={"start": "2027-06-01", "end": "2027-07-01"})
        results, _ = assess(self.app)
        self.assertFalse(results["sup-cn"].eligible)
        self.assertFalse(results["sup-th"].eligible)
        self.assertFalse(results["sup-vn"].eligible)
        for result in results.values():
            self.assertIn("delivery_window_unavailable",
                          {r["code"] for r in result.reasons})

    def test_conflict_of_interest_excluded(self):
        standard_requirement(self.app, excluded_supplier_ids=["sup-cn"])
        results, _ = assess(self.app)
        self.assertFalse(results["sup-cn"].eligible)
        self.assertIn("conflict_of_interest_excluded",
                      {r["code"] for r in results["sup-cn"].reasons})
        self.assertTrue(results["sup-th"].eligible)

    def test_supplier_declared_conflict_rejects(self):
        self.app.register_supplier("sup-my", {
            "name": "吉隆坡厂", "origin_countries": ["MY"],
            "categories": ["machinery"], "languages": ["en"], "conflicts": ["buyer-A"]})
        standard_requirement(self.app, allowed_origin_countries=["MY"])
        result = matcher.assess(
            {**assess(self.app)[1]}, self.app.catalog.get_supplier("sup-my"))
        self.assertFalse(result.eligible)
        self.assertIn("conflict_of_interest_declared",
                      {r["code"] for r in result.reasons})

    def test_required_capability_missing(self):
        # 电子行业 + rohs 硬性能力：sup-th 行业匹配但缺 rohs，sup-vn 通过
        standard_requirement(self.app, category="electronics",
                             required_capabilities=["rohs"])
        results, _ = assess(self.app)
        self.assertFalse(results["sup-cn"].eligible)  # 行业不匹配
        self.assertFalse(results["sup-th"].eligible)
        self.assertIn("capability_missing",
                      {r["code"] for r in results["sup-th"].reasons})
        self.assertTrue(results["sup-vn"].eligible)

    def test_score_is_within_100_and_explained(self):
        standard_requirement(self.app, required_capabilities=["iso9001", "ce"])
        results, _ = assess(self.app)
        self.assertTrue(results["sup-cn"].eligible)
        self.assertGreater(results["sup-cn"].score, 0)
        self.assertLessEqual(results["sup-cn"].score, 100)
        items = {b["item"]: b for b in results["sup-cn"].score_breakdown}
        self.assertEqual(set(items),
                         {"industry_fit", "country_rule_fit",
                          "language_fit", "delivery_window_fit"})
        self.assertAlmostEqual(
            sum(b["points"] for b in results["sup-cn"].score_breakdown),
            results["sup-cn"].score, places=2)


if __name__ == "__main__":
    unittest.main()
