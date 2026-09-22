"""预算预留/承诺：并发确认不能重复承诺同一预算。"""
import threading
import unittest

from service.errors import BudgetExhaustedError, ConflictError
from tests.helpers import make_app, populate, standard_requirement


def two_candidates(app):
    standard_requirement(app)
    app.submit_response("req-1", "sup-cn", {"amount": 60000, "currency": "USD"})
    app.submit_response("req-1", "sup-th", {"amount": 55000, "currency": "USD"})
    app.generate_recommendations("req-1")


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        populate(self.app)
        two_candidates(self.app)

    def open_neg(self, candidate):
        return self.app.open_negotiation("req-1", candidate)["negotiation_id"]

    def test_hold_blocks_second_hold_over_budget(self):
        # 预算 100000：预留 60000 后，55000 无法再预留
        self.open_neg("sup-cn")
        with self.assertRaises(BudgetExhaustedError) as ctx:
            self.open_neg("sup-th")
        self.assertEqual(ctx.exception.code, "budget_exhausted")
        ledger = self.app._budget_ledger("req-1", 1)
        self.assertEqual(ledger["held"], 60000)
        self.assertEqual(ledger["available"], 40000)

    def test_releasing_hold_frees_budget(self):
        nid = self.open_neg("sup-cn")
        self.app.close_negotiation(nid, declined=True)
        ledger = self.app._budget_ledger("req-1", 1)
        self.assertEqual(ledger["held"], 0)
        self.assertEqual(ledger["available"], 100000)
        # 释放后另一候选可以开启洽谈
        nid2 = self.open_neg("sup-th")
        self.assertTrue(nid2)

    def test_confirm_commits_and_rejects_second_confirmation(self):
        nid = self.open_neg("sup-cn")
        self.app.confirm_negotiation(nid, amount=60000)
        ledger = self.app._budget_ledger("req-1", 1)
        self.assertEqual(ledger["committed"], 60000)
        self.assertEqual(ledger["held"], 0)
        # 已承诺 60000，剩 40000，sup-th 55000 无法承诺（无 hold 时开洽谈即失败）
        with self.assertRaises(BudgetExhaustedError):
            self.open_neg("sup-th")
        # 同一洽谈不能重复确认
        with self.assertRaises(ConflictError) as ctx:
            self.app.confirm_negotiation(nid)
        self.assertEqual(ctx.exception.code, "already_confirmed")

    def test_concurrent_confirms_only_one_commits(self):
        """两个洽谈：释放部分额度使两者总额超过预算，并发确认只能成功一个。"""
        n1 = self.open_neg("sup-cn")  # hold 60000
        self.app.close_negotiation(n1, declined=True)
        # 重新开启两个 55000/60000 的洽谈无法同时预留；改为各自 60000、40000 内
        # 直接构造：sup-cn 60000 + sup-th 55000 不能同时 hold，
        # 因此用并发确认两个金额各 60000 的洽谈（先 hold 再释放再开第二个）。
        # 场景：两个洽谈都只 hold 报价的一部分后并发确认总额超限。
        a = self.app.open_negotiation("req-1", "sup-cn", hold_amount=30000)["negotiation_id"]
        # 剩余 70000：sup-th hold 30000
        b = self.app.open_negotiation("req-1", "sup-th", hold_amount=30000)["negotiation_id"]
        # 并发把两个洽谈都确认到 60000（总额 120000 > 100000）
        results = {}

        def confirm(nid, key):
            try:
                self.app.confirm_negotiation(nid, amount=60000,
                                             idempotency_key=f"key-{key}")
                results[key] = "ok"
            except BudgetExhaustedError:
                results[key] = "budget"

        t1 = threading.Thread(target=confirm, args=(a, "a"))
        t2 = threading.Thread(target=confirm, args=(b, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results.values()), ["budget", "ok"])
        ledger = self.app._budget_ledger("req-1", 1)
        self.assertEqual(ledger["committed"], 60000)

    def test_idempotent_confirm_does_not_double_commit(self):
        nid = self.open_neg("sup-cn")
        first = self.app.confirm_negotiation(nid, amount=60000,
                                             idempotency_key="confirm-1")
        # 同幂等键再次确认：命中幂等表直接回放，不抛 already_confirmed，也不二次承诺
        replay = self.app.confirm_negotiation(nid, amount=60000,
                                              idempotency_key="confirm-1")
        self.assertEqual(first["negotiation_id"], replay["negotiation_id"])
        self.assertTrue(replay["idempotent_replay"])
        ledger = self.app._budget_ledger("req-1", 1)
        self.assertEqual(ledger["committed"], 60000)

    def test_counter_then_confirm(self):
        nid = self.open_neg("sup-th")  # 60000 held by cn? no—cn holds nothing here
        self.app.counter_negotiation(nid, 50000, note="买方还价", actor="buyer-chen")
        view = self.app.confirm_negotiation(nid, amount=50000)
        self.assertEqual(view["committed_amount"], 50000)
        self.assertEqual(view["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
