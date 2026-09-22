"""撮合核心行为测试：覆盖资格分离、轮次冻结、防重复、联合方案、
匹配规则、人工留痕、增量重算、预算并发确认、重启还原与解释隔离。"""
import os
import shutil
import tempfile
import threading
import unittest

from service.engine import MatchEngine
from service.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from service.store import EventStore


def make_engine(directory=None):
    return MatchEngine(EventStore(directory))


def buyer_qual(buyer_id="buyer-1", **over):
    data = {
        "buyer_id": buyer_id,
        "legal_name": "东盟采购有限公司",
        "contact": "procurement@example.org",
        "country": "VN",
        "industries": ["textile"],
        "budget_limit": {"amount": 100000, "currency": "USD"},
        "decision_makers": ["Nguyen Van A"],
    }
    data.update(over)
    return data


def supplier(sid, **over):
    data = {
        "supplier_id": sid,
        "name": f"供应商{sid}",
        "country": "CN",
        "industries": ["textile"],
        "languages": ["ZH", "EN"],
        "capacity_per_round": 500,
    }
    data.update(over)
    return data


def demand(demand_id="d1", **over):
    data = {
        "demand_id": demand_id,
        "buyer_id": "buyer-1",
        "title": "夏季面料采购",
        "industry": "textile",
        "quantity": 300,
        "budget": {"amount": 60000, "currency": "USD"},
        "delivery_window": {"start": "2026-10-01", "end": "2026-12-31"},
        "country_rule": {"policy": "required", "countries": ["CN", "TH"]},
        "languages": ["ZH", "EN"],
        "notes": "",
    }
    data.update(over)
    return data


def response(sid, did="d1", **over):
    data = {
        "demand_id": did,
        "supplier_id": sid,
        "idempotency_key": f"key-{sid}-{did}",
        "quote": {"amount": 50000, "currency": "USD"},
        "deliverable_date": "2026-11-15",
        "languages": ["ZH", "EN"],
    }
    data.update(over)
    return data


class QualificationTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()

    def test_qualification_separated_from_public_demand(self):
        q = self.eng.register_qualification(buyer_qual(), "2026-09-01T00:00:00+00:00")
        self.eng.verify_qualification("buyer-1", "2026-09-01T00:01:00+00:00")
        self.eng.open_round("2026-09-02T00:00:00+00:00")
        d = self.eng.submit_demand(demand(), "2026-09-03T00:00:00+00:00")
        # 公开需求只有匿名 buyer_code，不含法律名称/联系人
        self.assertTrue(d["buyer_code"].startswith("B-"))
        self.assertNotIn("legal_name", d)
        self.assertNotIn("contact", d)
        self.assertNotIn("buyer_id", d)
        # 匿名代号稳定且与 buyer_id 不同
        self.assertNotEqual(d["buyer_code"], "buyer-1")
        # 公开事件日志里不应出现实名信息
        public = [r for r in self.eng.store.replay("events")]
        self.assertTrue(all("legal_name" not in r["payload"] for r in public))

    def test_unverified_buyer_cannot_submit(self):
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.open_round("t")
        with self.assertRaises(ForbiddenError):
            self.eng.submit_demand(demand(), "t")

    def test_budget_limit_enforced(self):
        self.eng.register_qualification(
            buyer_qual(budget_limit={"amount": 1000, "currency": "USD"}), "t")
        self.eng.verify_qualification("buyer-1", "t")
        self.eng.open_round("t")
        with self.assertRaises(ForbiddenError):
            self.eng.submit_demand(demand(budget={"amount": 5000, "currency": "USD"}), "t")

    def test_validation_error_collects_fields(self):
        with self.assertRaises(ValidationError) as ctx:
            self.eng.register_qualification({"buyer_id": "x"}, "t")
        self.assertIn("legal_name", str(ctx.exception.details))


class RoundFreezeTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        self.eng.register_supplier(supplier("s1"), "t")
        self.eng.open_round("t")

    def test_revision_only_before_freeze(self):
        self.eng.submit_demand(demand(), "t")
        d2 = self.eng.revise_demand(
            "d1", demand(notes="修订说明", budget={"amount": 55000, "currency": "USD"}), "t")
        self.assertEqual(d2["revision"], 2)
        self.assertEqual(self.eng.demands["d1"]["revision"], 2)
        # 历史修订全部留存
        self.assertEqual(len(self.eng.demand_history["d1"]), 2)

        self.eng.freeze_round("t")
        with self.assertRaises(ConflictError):
            self.eng.revise_demand("d1", demand(notes="冻结后修订"), "t")
        with self.assertRaises(ConflictError):
            self.eng.submit_response(response("s1"), "t")

    def test_freeze_snapshot_is_immutable_record(self):
        self.eng.submit_demand(demand(), "t")
        resp, _ = self.eng.submit_response(response("s1"), "t")
        self.eng.freeze_round("t")
        self.eng.generate_recommendations(1, "t")
        # 冻结后候选退出，快照仍保持冻结时刻的状态
        self.eng.withdraw_response(resp["response_id"], "t", "退出")
        snap = self.eng.rounds[1]["snapshot"]
        self.assertEqual(len(snap["demands"]), 1)
        self.assertEqual(len(snap["responses"]), 1)
        self.assertEqual(snap["responses"][0]["status"], "active")


class ResponseDedupTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        for sid in ("s1", "s2", "s3"):
            self.eng.register_supplier(supplier(sid), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand(), "t")

    def test_idempotent_retry_does_not_duplicate(self):
        r1, replayed1 = self.eng.submit_response(response("s1", idempotency_key="k1"), "t")
        r2, replayed2 = self.eng.submit_response(response("s1", idempotency_key="k1"), "t")
        self.assertFalse(replayed1)
        self.assertTrue(replayed2)
        self.assertEqual(r1["response_id"], r2["response_id"])
        self.assertEqual(
            len([r for r in self.eng.responses.values() if r["supplier_id"] == "s1"]), 1)

    def test_supplier_cannot_respond_twice_even_with_other_keys(self):
        self.eng.submit_response(response("s1", idempotency_key="k1"), "t")
        with self.assertRaises(ConflictError):
            self.eng.submit_response(response("s1", idempotency_key="k2"), "t")

    def test_joint_member_cannot_join_competing_response(self):
        self.eng.submit_response(
            response("s1", members=["s1", "s2"], idempotency_key="k1"), "t")
        with self.assertRaises(ConflictError) as ctx:
            self.eng.submit_response(response("s2", idempotency_key="k2"), "t")
        self.assertEqual(ctx.exception.details["blocked_member"], "s2")

    def test_joint_response_must_include_lead(self):
        with self.assertRaises(ValidationError):
            self.eng.submit_response(
                response("s1", members=["s2", "s3"], idempotency_key="k9"), "t")


class MatchingRulesTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        self.eng.register_supplier(supplier("good", country="CN", languages=["ZH", "EN"]), "t")
        self.eng.register_supplier(
            supplier("badcountry", country="US", languages=["EN"]), "t")
        self.eng.register_supplier(
            supplier("late", country="CN", languages=["ZH"]), "t")
        self.eng.register_supplier(
            supplier("overprice", country="CN", languages=["ZH", "EN"]), "t")
        self.eng.register_supplier(
            supplier("coi", country="CN", languages=["ZH", "EN"],
                     excludes_buyers=["buyer-1"]), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand(), "t")

    def _freeze_and_generate(self):
        self.eng.freeze_round("t")
        return self.eng.generate_recommendations(1, "t")["d1"]

    def test_hard_constraints_classified(self):
        self.eng.submit_response(response("good", idempotency_key="g", quote={"amount": 48000, "currency": "USD"}), "t")
        self.eng.submit_response(response("badcountry", idempotency_key="b"), "t")
        self.eng.submit_response(
            response("late", idempotency_key="l", deliverable_date="2027-02-01"), "t")
        self.eng.submit_response(
            response("overprice", idempotency_key="o", quote={"amount": 70000, "currency": "USD"}), "t")
        self.eng.submit_response(response("coi", idempotency_key="c"), "t")
        rows = {r["supplier_id"]: r for r in self._freeze_and_generate()}
        self.assertTrue(rows["good"]["eligible"])
        self.assertFalse(rows["badcountry"]["eligible"])
        self.assertIn("国家准入", rows["badcountry"]["reasons"][0])
        self.assertFalse(rows["late"]["eligible"])
        self.assertTrue(any("交付窗口" in x for x in rows["late"]["reasons"]))
        self.assertFalse(rows["overprice"]["eligible"])
        self.assertTrue(any("预算" in x for x in rows["overprice"]["reasons"]))
        self.assertFalse(rows["coi"]["eligible"])
        self.assertTrue(any("利益冲突" in x for x in rows["coi"]["reasons"]))

    def test_language_mismatch_rejected(self):
        self.eng.register_supplier(
            supplier("nolanguage", country="CN", languages=["JA"]), "t")
        self.eng.submit_response(
            response("nolanguage", idempotency_key="n", languages=["JA"]), "t")
        rows = {r["supplier_id"]: r for r in self._freeze_and_generate()}
        self.assertFalse(rows["nolanguage"]["eligible"])
        self.assertIn("语言", rows["nolanguage"]["reasons"][0])

    def test_scoring_orders_candidates(self):
        self.eng.submit_response(
            response("good", idempotency_key="g1", quote={"amount": 40000, "currency": "USD"},
                     deliverable_date="2026-10-20"), "t")
        self.eng.register_supplier(
            supplier("second", country="TH", languages=["EN"]), "t")
        self.eng.submit_response(
            response("second", idempotency_key="g2", quote={"amount": 55000, "currency": "USD"},
                     deliverable_date="2026-12-20", languages=["EN"]), "t")
        rows = self._freeze_and_generate()
        ranked = [r for r in rows if r["eligible"]]
        self.assertEqual(ranked[0]["supplier_id"], "good")
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])
        # 因素分解可解释
        self.assertIn("price", ranked[0]["factors"])


class ManualAdjustmentTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        for sid in ("s1", "s2"):
            self.eng.register_supplier(supplier(sid), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand(), "t")
        self.eng.submit_response(
            response("s1", idempotency_key="k1", quote={"amount": 40000, "currency": "USD"}), "t")
        self.eng.submit_response(
            response("s2", idempotency_key="k2", quote={"amount": 50000, "currency": "USD"}), "t")
        self.eng.freeze_round("t")
        self.eng.generate_recommendations(1, "t")

    def _row(self, sid):
        return next(r for r in self.eng.recommendations[1]["d1"] if r["supplier_id"] == sid)

    def test_exclude_is_audited_and_recomputed(self):
        self.eng.manual_adjust(
            {"action": "exclude", "response_id": self._row("s1")["response_id"],
             "operator_id": "op-7", "reason": "资质材料存疑"}, "t")
        self.assertFalse(self._row("s1")["eligible"])
        self.assertEqual(self._row("s2")["rank"], 1)
        trail = self.eng.audit_trail("d1")
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["type"], "manual_exclude")
        self.assertEqual(trail[0]["payload"]["operator_id"], "op-7")
        self.assertEqual(trail[0]["payload"]["old_rank"], 1)
        self.assertIsNotNone(trail[0]["received_at"])

    def test_override_rank_then_reinstate(self):
        self.eng.manual_adjust(
            {"action": "override_rank", "response_id": self._row("s2")["response_id"],
             "operator_id": "op-7", "new_rank": 1, "reason": "优先本地交付"}, "t")
        self.assertEqual(self._row("s2")["rank"], 1)
        self.assertEqual(self._row("s1")["rank"], 2)
        self.eng.manual_adjust(
            {"action": "reinstate", "response_id": self._row("s2")["response_id"],
             "operator_id": "op-7"}, "t")
        self.assertEqual(self._row("s1")["rank"], 1)
        self.assertEqual(len(self.eng.audit_trail()), 2)

    def test_adjustment_requires_operator(self):
        with self.assertRaises(ValidationError):
            self.eng.manual_adjust(
                {"action": "exclude", "response_id": self._row("s1")["response_id"]}, "t")


class IncrementalRecomputeTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        for sid in ("s1", "s2"):
            self.eng.register_supplier(supplier(sid), "t")
        self.eng.register_supplier(supplier("x1"), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand("d1"), "t")
        self.eng.submit_demand(demand(
            "d2", title="另一需求", budget={"amount": 60000, "currency": "USD"}), "t")
        self.eng.submit_response(response("s1", did="d1", idempotency_key="a"), "t")
        self.eng.submit_response(response("s2", did="d1", idempotency_key="b",
                                          quote={"amount": 55000, "currency": "USD"}), "t")
        self.eng.submit_response(response("x1", did="d2", idempotency_key="c"), "t")
        self.eng.freeze_round("t")
        self.eng.generate_recommendations(1, "t")

    def test_withdraw_only_recomputes_affected_demand(self):
        top = next(r for r in self.eng.recommendations[1]["d1"]
                   if r["rank"] == 1)["response_id"]
        # 记录 d2 已有的推荐生成事件，退出后不应再为 d2 生成
        d2_events_before = [r["seq"] for r in self.eng.store.replay("events")
                            if r["type"] == "recommendations_generated"
                            and r["payload"]["demand_id"] == "d2"]
        self.eng.withdraw_response(top, "t", "主动退出")
        d1_rows = self.eng.recommendations[1]["d1"]
        self.assertTrue(all(r["response_id"] != top for r in d1_rows))
        d2_events_after = [r["seq"] for r in self.eng.store.replay("events")
                           if r["type"] == "recommendations_generated"
                           and r["payload"]["demand_id"] == "d2"]
        self.assertEqual(d2_events_before, d2_events_after)
        # 重算事件带增量标记
        events = [r for r in self.eng.store.replay("events")
                  if r["type"] == "recommendations_generated"]
        self.assertEqual(events[-1]["payload"]["cause"], "incremental_recompute")
        self.assertEqual(events[-1]["payload"]["demand_id"], "d1")

    def test_withdraw_during_negotiation_releases_hold(self):
        rid = self.eng.recommendations[1]["d1"][0]["response_id"]
        self.eng.start_negotiation(
            {"demand_id": "d1", "response_id": rid, "operator_id": "op"}, "t")
        self.eng.withdraw_response(rid, "t", "退出")
        deal = self.eng.deals["d1"]
        self.assertEqual(deal["status"], "cancelled")
        hold = self.eng.holds[deal["hold_id"]]
        self.assertEqual(hold["status"], "released")


class BudgetConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        self.eng.register_supplier(supplier("s1"), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand(), "t")
        self.eng.submit_response(response("s1"), "t")
        self.eng.freeze_round("t")
        self.eng.generate_recommendations(1, "t")
        rid = self.eng.recommendations[1]["d1"][0]["response_id"]
        self.eng.start_negotiation(
            {"demand_id": "d1", "response_id": rid, "operator_id": "op"}, "t")
        self.rid = rid

    def test_parallel_confirmations_commit_once(self):
        outcomes = []

        def confirm():
            try:
                self.eng.confirm_negotiation(
                    {"demand_id": "d1", "operator_id": "op"}, "t")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=confirm) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(outcomes).count("ok"), 1)
        self.assertEqual(sorted(outcomes).count("rejected"), 7)
        committed = [h for h in self.eng.holds.values() if h["status"] == "committed"]
        self.assertEqual(len(committed), 1)

    def test_switching_candidate_releases_old_hold(self):
        self.eng.confirm_negotiation(
            {"demand_id": "d1", "operator_id": "op"}, "t")
        with self.assertRaises(ConflictError):
            self.eng.start_negotiation(
                {"demand_id": "d1", "response_id": self.rid, "operator_id": "op"}, "t")


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="match-data-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self, directory):
        eng = make_engine(directory)
        eng.register_qualification(buyer_qual(), "2026-09-01T00:00:00+00:00")
        eng.verify_qualification("buyer-1", "2026-09-01T00:01:00+00:00")
        eng.register_supplier(supplier("s1"), "t")
        eng.register_supplier(supplier("s2"), "t")
        r1 = eng.open_round("2026-09-02T00:00:00+00:00")
        eng.submit_demand(demand(), "2026-09-03T00:00:00+00:00")
        eng.revise_demand("d1", demand(notes="v2"), "2026-09-03T01:00:00+00:00")
        eng.submit_response(response("s1", idempotency_key="k1",
                                     quote={"amount": 48000, "currency": "USD"}), "t")
        eng.submit_response(response("s2", idempotency_key="k2",
                                     quote={"amount": 52000, "currency": "USD"}), "t")
        eng.freeze_round("2026-09-10T00:00:00+00:00")
        eng.generate_recommendations(r1, "2026-09-10T01:00:00+00:00")
        rid = eng.recommendations[1]["d1"][0]["response_id"]
        eng.start_negotiation(
            {"demand_id": "d1", "response_id": rid, "operator_id": "op-9"},
            "2026-09-11T00:00:00+00:00")
        eng.exchange_offer(
            {"demand_id": "d1", "sender": "buyer", "terms_ref": "terms-1"}, "t")
        return rid

    def test_restart_restores_everything(self):
        rid = self._seed(self.dir)
        restored = make_engine(self.dir)
        # 轮次与冻结快照
        self.assertEqual(restored.rounds[1]["status"], "frozen")
        self.assertIsNotNone(restored.rounds[1]["snapshot"])
        # 需求最新修订与历史
        self.assertEqual(restored.demands["d1"]["revision"], 2)
        self.assertEqual(len(restored.demand_history["d1"]), 2)
        # 防重复索引恢复：轮次未开放，另键重复提交被冲突拒绝
        self.assertIsNone(restored.current_open_round)
        with self.assertRaises(ConflictError):
            restored.submit_response(response("s1", idempotency_key="other"), "t")
        # 幂等键重放在无开放轮时仍直接返回原响应（不产生新事件）
        replay_resp, replayed = restored.submit_response(
            response("s1", idempotency_key="k1"), "t")
        self.assertTrue(replayed)
        self.assertEqual(replay_resp["quote"]["amount"], 48000)
        # 推荐结果还原
        rows = restored.recommendations[1]["d1"]
        self.assertEqual(rows[0]["response_id"], rid)
        # 洽谈与预算持有状态还原
        deal = restored.deals["d1"]
        self.assertEqual(deal["status"], "negotiating")
        self.assertEqual(restored.holds[deal["hold_id"]]["status"], "held")
        events = [h["event"] for h in deal["history"]]
        self.assertEqual(events, ["started", "offer"])
        # 实名侧单独还原
        self.assertEqual(restored.qualifications["buyer-1"]["legal_name"],
                         "东盟采购有限公司")

    def test_logs_split_into_three_files(self):
        self._seed(self.dir)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "events.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "qualifications.jsonl")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "audit.jsonl")))

    def test_confirmed_state_survives_restart_and_blocks_reconfirm(self):
        rid = self._seed(self.dir)
        eng = make_engine(self.dir)
        eng.confirm_negotiation({"demand_id": "d1", "operator_id": "op"}, "t")
        again = make_engine(self.dir)
        self.assertEqual(again.deals["d1"]["status"], "confirmed")
        with self.assertRaises(ConflictError):
            again.confirm_negotiation({"demand_id": "d1", "operator_id": "op"}, "t")


class ExplainTest(unittest.TestCase):
    def setUp(self):
        self.eng = make_engine()
        self.eng.register_qualification(buyer_qual(), "t")
        self.eng.verify_qualification("buyer-1", "t")
        self.eng.register_supplier(supplier("s1"), "t")
        self.eng.register_supplier(
            supplier("s2", country="US", languages=["EN"]), "t")
        self.eng.open_round("t")
        self.eng.submit_demand(demand(), "t")
        self.eng.submit_response(
            response("s1", idempotency_key="k1", quote={"amount": 48000, "currency": "USD"}), "t")
        self.eng.submit_response(
            response("s2", idempotency_key="k2", quote={"amount": 30000, "currency": "USD"}), "t")
        self.eng.freeze_round("t")
        self.eng.generate_recommendations(1, "t")

    def test_buyer_sees_names_but_no_quotes(self):
        view = self.eng.explain("d1", {"kind": "buyer", "buyer_id": "buyer-1"})
        text = str(view)
        self.assertIn("供应商s1", text)
        self.assertNotIn("48000", text)
        self.assertNotIn("30000", text)
        self.assertIn("score_factors", text)

    def test_other_buyer_forbidden(self):
        self.eng.register_qualification(buyer_qual(
            "buyer-2", legal_name="其他公司", budget_limit={"amount": 999999, "currency": "USD"}), "t")
        with self.assertRaises(ForbiddenError):
            self.eng.explain("d1", {"kind": "buyer", "buyer_id": "buyer-2"})

    def test_supplier_sees_only_own_row(self):
        view = self.eng.explain("d1", {"kind": "supplier", "supplier_id": "s1"})
        self.assertIn("your_result", view)
        text = str(view)
        # 自己的报价可见
        self.assertIn("48000", text)
        # 竞争对手的报价与标识不可见
        self.assertNotIn("30000", text)
        self.assertNotIn("s2", text)

    def test_operator_sees_full_picture(self):
        view = self.eng.explain("d1", {"kind": "operator"})
        text = str(view)
        self.assertIn("48000", text)
        self.assertIn("30000", text)


if __name__ == "__main__":
    unittest.main()
