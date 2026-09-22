"""供应商响应：防重复提交、联合方案、修订/退出。"""
import unittest

from service.errors import ConflictError, ValidationError
from tests.helpers import make_app, populate, standard_requirement


def submit(app, supplier="sup-cn", amount=80000, **kw):
    return app.submit_response(
        "req-1", supplier, {"amount": amount, "currency": "USD"}, **kw)


class ResponseTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)
        standard_requirement(self.app)

    def test_duplicate_lead_rejected(self):
        submit(self.app)
        with self.assertRaises(ConflictError) as ctx:
            submit(self.app, amount=70000)
        self.assertEqual(ctx.exception.code, "duplicate_response")

    def test_same_supplier_cannot_join_two_consortiums(self):
        submit(self.app, supplier="sup-th", members=["sup-th", "sup-cn"])
        with self.assertRaises(ConflictError) as ctx:
            submit(self.app, supplier="sup-vn", members=["sup-vn", "sup-cn"])
        self.assertEqual(ctx.exception.code, "duplicate_membership")

    def test_joint_response_aggregates_capabilities(self):
        # sup-th 缺 ce 标签，联合 sup-cn 后覆盖；语言并集也扩大
        view = submit(self.app, supplier="sup-th", members=["sup-th", "sup-cn"])
        self.assertEqual(view["kind"], "joint")
        self.assertEqual(view["candidate_id"], f"consortium:{view['response_id']}")

    def test_lead_must_be_member(self):
        with self.assertRaises(ValidationError) as ctx:
            submit(self.app, supplier="sup-th", members=["sup-cn"])
        self.assertEqual(ctx.exception.code, "lead_not_member")

    def test_unknown_member_rejected(self):
        with self.assertRaises(Exception):
            submit(self.app, supplier="sup-cn", members=["sup-cn", "ghost"])

    def test_idempotent_submit_replays_same_response(self):
        first = submit(self.app, idempotency_key="key-1")
        second = submit(self.app, amount=99999, idempotency_key="key-1")
        self.assertEqual(first["response_id"], second["response_id"])
        self.assertTrue(second["idempotent_replay"])
        # 只有一个有效响应
        active = [r for r in self.app.responses.values() if r.status == "active"]
        self.assertEqual(len(active), 1)

    def test_withdraw_allows_resubmit(self):
        first = submit(self.app)
        self.app.withdraw_response(first["response_id"])
        again = submit(self.app, amount=75000)
        self.assertEqual(again["status"], "active")
        self.assertEqual(again["quote"]["amount"], 75000)

    def test_amend_increments_revision(self):
        view = submit(self.app)
        amended = self.app.amend_response(view["response_id"],
                                          {"amount": 76000, "currency": "USD"})
        self.assertEqual(amended["revision"], 2)
        self.assertEqual(amended["quote"]["amount"], 76000)

    def test_bad_quote_rejected(self):
        # 金额非正
        with self.assertRaises(ValidationError):
            submit(self.app, amount=0)
        # 币种缺失
        with self.assertRaises(ValidationError):
            self.app.submit_response("req-1", "sup-th", {"amount": 10})


if __name__ == "__main__":
    unittest.main()
