"""HTTP API 端到端测试（线程内起服，内存事件）。"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from service.app import HubApplication
from service.event_store import EventStore
from service.main import Handler
from tests.helpers import populate, standard_requirement


class Server:
    def __init__(self):
        app = HubApplication(EventStore(None))
        populate(app)
        standard_requirement(app)

        class BoundHandler(Handler):
            pass

        BoundHandler.app = app
        self.app = app
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BoundHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        parsed = json.loads(data) if data else None
        return resp.status, parsed

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.srv = Server()

    def tearDown(self):
        self.srv.stop()

    def test_health(self):
        status, body = self.srv.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_full_flow_over_http(self):
        srv = self.srv
        # 供应商响应（幂等头）
        headers = {"Content-Type": "application/json",
                   "Idempotency-Key": "resp-cn-1"}
        status, body = srv.request("POST", "/requirements/req-1/responses",
                                   {"lead_supplier_id": "sup-cn",
                                    "quote": {"amount": 80000, "currency": "USD"}},
                                   headers)
        self.assertEqual(status, 201, body)
        response_id = body["response_id"]

        # 幂等重放
        status, replay = srv.request("POST", "/requirements/req-1/responses",
                                     {"lead_supplier_id": "sup-cn",
                                      "quote": {"amount": 80000, "currency": "USD"}},
                                     headers)
        self.assertEqual(status, 201)
        self.assertEqual(replay["response_id"], response_id)
        self.assertTrue(replay["idempotent_replay"])

        srv.request("POST", "/requirements/req-1/responses",
                    {"lead_supplier_id": "sup-th",
                     "quote": {"amount": 70000, "currency": "USD"}},
                    {"Content-Type": "application/json"})
        # 重复牵头被拒
        status, body = srv.request("POST", "/requirements/req-1/responses",
                                   {"lead_supplier_id": "sup-cn",
                                    "quote": {"amount": 79000, "currency": "USD"}},
                                   {"Content-Type": "application/json"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "duplicate_response")

        # 生成推荐
        status, reco = srv.request("POST", "/requirements/req-1/recommendations", {})
        self.assertEqual(status, 201)
        ranked = [c["supplier_id"] for c in reco["ranked"]]
        self.assertIn("sup-cn", ranked)

        # 供应商角色看不到报价
        status, public_reco = srv.request(
            "GET", "/requirements/req-1/rounds/1/recommendations",
            headers={"X-Role": "supplier"})
        raw = json.dumps(public_reco, ensure_ascii=False)
        self.assertNotIn("80000", raw)
        self.assertNotIn("70000", raw)

        # 供应商自身解释
        status, explanation = srv.request(
            "GET", f"/responses/{response_id}/explanation")
        self.assertEqual(status, 200)
        self.assertTrue(explanation["eligible"])
        self.assertNotIn("sup-th", json.dumps(explanation, ensure_ascii=False))

        # 人工调整必须带操作人与原因
        status, err = srv.request(
            "POST", "/requirements/req-1/rounds/1/overrides",
            {"action": "exclude", "candidate_id": "sup-th", "reason": ""},
            {"Content-Type": "application/json"})
        self.assertEqual(status, 422)

        status, ov = srv.request(
            "POST", "/requirements/req-1/rounds/1/overrides",
            {"action": "exclude", "candidate_id": "sup-th",
             "reason": "合规调查未决"},
            {"Content-Type": "application/json", "X-Actor": "staff-li"})
        self.assertEqual(status, 201)
        self.assertEqual(ov["actor"], "staff-li")

        # 开洽谈 -> 并发安全确认
        status, neg = srv.request("POST", "/requirements/req-1/negotiations",
                                  {"candidate_id": "sup-cn"},
                                  {"Content-Type": "application/json"})
        self.assertEqual(status, 201)
        nid = neg["negotiation_id"]

        status, confirmed = srv.request(
            "POST", f"/negotiations/{nid}/confirm", {"amount": 80000},
            {"Content-Type": "application/json", "Idempotency-Key": "confirm-1"})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")

        status, replay = srv.request(
            "POST", f"/negotiations/{nid}/confirm", {"amount": 80000},
            {"Content-Type": "application/json", "Idempotency-Key": "confirm-1"})
        self.assertEqual(status, 200)
        self.assertTrue(replay["idempotent_replay"])

        # 预算台账
        status, ledger = srv.request(
            "GET", "/requirements/req-1/rounds/1/budget")
        self.assertEqual(status, 200)
        self.assertEqual(ledger["committed"], 80000)

        # 供应商角色不能看预算与实名
        status, err = srv.request("GET", "/requirements/req-1/rounds/1/budget",
                                  headers={"X-Role": "supplier"})
        self.assertEqual(status, 403)
        status, err = srv.request("GET", "/buyers/buyer-A",
                                  headers={"X-Role": "supplier"})
        self.assertEqual(status, 403)
        status, identity = srv.request("GET", "/buyers/buyer-A",
                                       headers={"X-Role": "staff"})
        self.assertEqual(status, 200)
        self.assertEqual(identity["legal_name"], "东盟优品贸易有限公司")

    def test_validation_error_shape(self):
        status, body = self.srv.request("POST", "/requirements/req-1/responses",
                                        {"lead_supplier_id": "sup-cn",
                                         "quote": {"amount": 0, "currency": "USD"}},
                                        {"Content-Type": "application/json"})
        self.assertEqual(status, 422)
        self.assertIn("error", body)
        self.assertIn("message", body)


if __name__ == "__main__":
    unittest.main()
