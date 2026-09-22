"""实名资格与公开需求分离、轮次冻结相关测试。"""
import unittest

from service.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from tests.helpers import make_app, populate, standard_requirement


class IdentitySeparationTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)

    def test_public_view_has_no_legal_name(self):
        view = self.app.requirement_view(standard_requirement(self.app)["requirement_id"])
        self.assertEqual(view["buyer_ref"], "buyer-A")
        self.assertNotIn("legal_name", view)
        self.assertNotIn("contact", view)
        self.assertNotIn("credentials", view)
        self.assertTrue(view["buyer_verified"])

    def test_identity_requires_staff_role(self):
        with self.assertRaises(ForbiddenError):
            self.app.buyer_identity("buyer-A", role="supplier")
        with self.assertRaises(ForbiddenError):
            self.app.buyer_identity("buyer-A", role="buyer")
        identity = self.app.buyer_identity("buyer-A", role="staff")
        self.assertEqual(identity["legal_name"], "东盟优品贸易有限公司")
        self.assertEqual(identity["credentials"], {"license": "GX-001"})

    def test_duplicate_buyer_registration_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            self.app.register_buyer("buyer-A", "重复登记", "x@example.com")
        self.assertEqual(ctx.exception.code, "buyer_registered")

    def test_publishing_for_unknown_buyer_fails(self):
        with self.assertRaises(NotFoundError):
            self.app.publish_requirement("req-2", "ghost", {
                "title": "t", "country": "TH", "category": "machinery",
                "budget": {"amount": 1, "currency": "USD"},
                "delivery_window": {"start": "2026-10-01", "end": "2026-12-01"}})


class RoundFreezeTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)
        standard_requirement(self.app)

    def test_revise_open_round_increments_revision(self):
        view = self.app.revise_requirement("req-1", {"title": "包装设备 30 台"})
        self.assertEqual(view["current_round_no"], 1)
        self.assertEqual(view["rounds"][0]["revision"], 2)
        self.assertFalse(view["rounds"][0]["frozen"])

    def test_revise_after_freeze_opens_new_immutable_round(self):
        self.app.freeze_requirement("req-1")
        view = self.app.revise_requirement("req-1", {"title": "第二轮标题"})
        self.assertEqual(view["current_round_no"], 2)
        self.assertEqual(len(view["rounds"]), 2)
        round1, round2 = view["rounds"]
        self.assertTrue(round1["frozen"])
        self.assertEqual(round1["data"]["title"], "包装设备 20 台")
        self.assertFalse(round2["frozen"])
        self.assertEqual(round2["data"]["title"], "第二轮标题")

    def test_freeze_is_idempotent(self):
        r1 = self.app.freeze_requirement("req-1")
        r2 = self.app.freeze_requirement("req-1")
        self.assertEqual(r1["frozen_at"], r2["frozen_at"])

    def test_bad_budget_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.publish_requirement("req-bad", "buyer-A", {
                "title": "t", "country": "TH", "category": "machinery",
                "budget": {"amount": -1, "currency": "USD"},
                "delivery_window": {"start": "2026-10-01", "end": "2026-12-01"}})


if __name__ == "__main__":
    unittest.main()
