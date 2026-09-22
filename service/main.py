"""项目服务入口：跨境需求撮合 HTTP API（仅标准库）。

身份约定：
- X-Actor 头标识操作人（人工留痕必填）；
- X-Role 头取值 staff/buyer/supplier，控制实名信息与报价可见范围。
- Idempotency-Key 头用于响应提交、洽谈确认等关键写操作的幂等重放。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .app import HubApplication
from .errors import HubError
from .event_store import EventStore

_DEFAULT_APP: HubApplication | None = None


def default_app() -> HubApplication:
    global _DEFAULT_APP
    if _DEFAULT_APP is None:
        path = os.environ.get("HUB_EVENT_LOG", "data/hub_events.jsonl")
        _DEFAULT_APP = HubApplication(EventStore(path))
    return _DEFAULT_APP


def reset_default_app() -> None:
    """测试辅助：释放默认应用（会关闭事件文件）。"""
    global _DEFAULT_APP
    if _DEFAULT_APP is not None:
        _DEFAULT_APP.store.close()
        _DEFAULT_APP = None


class Handler(BaseHTTPRequestHandler):
    """撮合服务 HTTP 处理器。测试可通过设置类属性 app 注入应用。"""

    app: HubApplication | None = None

    @property
    def hub(self) -> HubApplication:
        return self.app or default_app()

    # ---------- 基础框架 ----------
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise HubError("请求体不是合法 JSON", code="bad_json", status=400)
        if not isinstance(data, dict):
            raise HubError("请求体必须是 JSON 对象", code="bad_json", status=400)
        return data

    def _actor(self, body: dict) -> str:
        return body.pop("actor", None) or self.headers.get("X-Actor") or ""

    def _role(self) -> str:
        return self.headers.get("X-Role", "staff")

    def _idempotency_key(self) -> str | None:
        return self.headers.get("Idempotency-Key")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            body = self._read_body() if method == "POST" else {}
            self._route(method, path, query, body)
        except HubError as exc:
            self._send_json(exc.status,
                            {"error": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂死
            self._send_json(500, {"error": "internal_error", "message": str(exc)})

    def log_message(self, format, *args):
        return

    # ---------- 路由 ----------
    def _route(self, method: str, path: str, query: dict, body: dict) -> None:
        segs = [s for s in path.split("/") if s]

        if method == "GET" and path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        hub = self.hub

        # /buyers/...
        if segs[:1] == ["buyers"]:
            return self._route_buyers(segs, body, query)
        if segs[:1] == ["suppliers"]:
            return self._route_suppliers(segs, body)
        if path.startswith("/responses/"):
            return self._route_response_action(segs, body)
        if path.startswith("/negotiations/"):
            return self._route_negotiations(segs, body)
        if segs[:1] == ["requirements"]:
            return self._route_requirements(segs, body, query)

        self._send_json(404, {"error": "not_found", "message": f"未知路径 {path}"})

    def _route_buyers(self, segs, body, query) -> None:
        hub = self.hub
        if len(segs) == 2 and self.command == "POST":
            result = hub.register_buyer(
                buyer_ref=segs[1],
                legal_name=body.get("legal_name", ""),
                contact=body.get("contact", ""),
                credentials=body.get("credentials"),
                verified=bool(body.get("verified", False)))
            return self._send_json(201, result)
        if len(segs) == 3 and segs[2] == "verify" and self.command == "POST":
            result = hub.verify_buyer(segs[1], bool(body.get("verified", True)),
                                      actor=self._actor(body) or "staff")
            return self._send_json(200, result)
        if len(segs) == 2 and self.command == "GET":
            result = hub.buyer_identity(segs[1], self._role())
            return self._send_json(200, result)
        raise HubError("未知买家接口", code="not_found", status=404)

    def _route_suppliers(self, segs, body) -> None:
        hub = self.hub
        if len(segs) == 1 and self.command == "POST":
            supplier_id = body.pop("supplier_id", None)
            if not supplier_id:
                from .errors import ValidationError
                raise ValidationError("supplier_id 必填", code="supplier_id_required")
            return self._send_json(201, hub.register_supplier(supplier_id, body))
        if len(segs) == 1 and self.command == "GET":
            return self._send_json(200, {"suppliers": hub.catalog.list_suppliers()})
        raise HubError("未知供应商接口", code="not_found", status=404)

    def _route_requirements(self, segs, body, query) -> None:
        hub = self.hub
        # POST /requirements
        if len(segs) == 1 and self.command == "POST":
            rid = body.pop("requirement_id", None)
            buyer_ref = body.pop("buyer_ref", None)
            if not rid or not buyer_ref:
                from .errors import ValidationError
                raise ValidationError("requirement_id 与 buyer_ref 必填",
                                      code="id_required")
            return self._send_json(201, hub.publish_requirement(rid, buyer_ref, body))
        if len(segs) < 2:
            raise HubError("未知需求接口", code="not_found", status=404)
        rid = segs[1]
        tail = segs[2:]

        if not tail and self.command == "GET":
            return self._send_json(200, hub.requirement_view(rid))
        if tail == ["freeze"] and self.command == "POST":
            return self._send_json(200, hub.freeze_requirement(
                rid, body.get("round_no")))
        if tail == ["revise"] and self.command == "POST":
            return self._send_json(200, hub.revise_requirement(rid, body))
        if tail == ["responses"] and self.command == "POST":
            result = hub.submit_response(
                requirement_id=rid,
                lead_supplier_id=body.get("lead_supplier_id", ""),
                quote=body.get("quote", {}),
                proposal=body.get("proposal"),
                members=body.get("members"),
                response_id=body.get("response_id"),
                idempotency_key=self._idempotency_key())
            return self._send_json(201, result)
        if tail == ["recommendations"] and self.command == "POST":
            return self._send_json(201, hub.generate_recommendations(
                rid, body.get("round_no")))
        # /requirements/{rid}/rounds/{no}/...
        if len(tail) >= 3 and tail[0] == "rounds":
            round_no = int(tail[1])
            action = tail[2]
            if action == "recommendations" and len(tail) == 3 and self.command == "GET":
                view = hub.recommendation_view(
                    rid, round_no, include_quotes=self._role() in ("staff", "buyer"))
                return self._send_json(200, view)
            if action == "overrides" and len(tail) == 3 and self.command == "POST":
                result = hub.apply_override(
                    rid, round_no, body.get("action", ""),
                    body.get("candidate_id", ""), body.get("reason", ""),
                    actor=self._actor(body), position=body.get("position"))
                return self._send_json(201, result)
            if action == "budget" and len(tail) == 3 and self.command == "GET":
                if self._role() not in ("staff", "buyer"):
                    from .errors import ForbiddenError
                    raise ForbiddenError("预算台账仅工作人员与采购方可查看",
                                         code="budget_restricted")
                return self._send_json(200, hub._budget_ledger(rid, round_no))
            if action == "explain" and len(tail) == 4 and self.command == "GET":
                result = hub.explain_decision(rid, round_no, tail[3], self._role())
                return self._send_json(200, result)
        if tail == ["negotiations"] and self.command == "POST":
            result = hub.open_negotiation(
                rid, body.get("candidate_id", ""),
                round_no=body.get("round_no"),
                hold_amount=body.get("hold_amount"),
                actor=self._actor(body) or "buyer")
            return self._send_json(201, result)
        raise HubError("未知需求接口", code="not_found", status=404)

    def _route_response_action(self, segs, body) -> None:
        hub = self.hub
        # /responses/{rid}/...
        if len(segs) == 3:
            rid, action = segs[1], segs[2]
            if action == "amend" and self.command == "POST":
                return self._send_json(200, hub.amend_response(
                    rid, body.get("quote", {}), body.get("proposal")))
            if action == "withdraw" and self.command == "POST":
                return self._send_json(200, hub.withdraw_response(
                    rid, actor=self._actor(body) or "supplier"))
            if action == "explanation" and self.command == "GET":
                return self._send_json(200, hub.explain_for_supplier(rid))
        raise HubError("未知响应接口", code="not_found", status=404)

    def _route_negotiations(self, segs, body) -> None:
        hub = self.hub
        if len(segs) == 2 and self.command == "GET":
            return self._send_json(200, hub.negotiation_view(segs[1]))
        if len(segs) == 3:
            nid, action = segs[1], segs[2]
            if action == "counter" and self.command == "POST":
                return self._send_json(200, hub.counter_negotiation(
                    nid, body.get("amount", 0), note=body.get("note", ""),
                    actor=self._actor(body) or "buyer"))
            if action == "confirm" and self.command == "POST":
                result = hub.confirm_negotiation(
                    nid, amount=body.get("amount"),
                    actor=self._actor(body) or "buyer",
                    idempotency_key=self._idempotency_key())
                return self._send_json(200, result)
            if action == "close" and self.command == "POST":
                return self._send_json(200, hub.close_negotiation(
                    nid, declined=bool(body.get("declined", False)),
                    actor=self._actor(body) or "buyer", note=body.get("note", "")))
        raise HubError("未知洽谈接口", code="not_found", status=404)


def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动本地服务。"""
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    run()
