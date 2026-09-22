"""跨境需求撮合枢纽 HTTP 服务（纯标准库，可独立运行）。

路由概览：
  实名侧      POST /qualifications，POST /qualifications/{id}/verify，GET /qualifications/{id}
  目录        POST /suppliers
  轮次        POST /rounds/open，POST /rounds/{no}/freeze，GET /rounds/{no}
  需求        POST /demands，PUT /demands/{id}，GET /demands，GET /demands/{id}
  响应        POST /responses，POST /responses/{id}/withdraw
  撮合        POST /rounds/{no}/recommendations
  人工调整    POST /manual-adjustments，GET /audit
  洽谈/预算   POST /negotiations/start|offer|confirm|cancel
  解释        GET /demands/{id}/explain?kind=buyer|supplier|operator&id=...
数据目录由环境变量 MATCH_DATA_DIR 指定，默认 ./data；置为 ":memory:" 时不落盘。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .clock import normalize_event_time
from .engine import MatchEngine
from .errors import DomainError
from .store import EventStore

_DATA_DIR = os.environ.get("MATCH_DATA_DIR", os.path.join(os.getcwd(), "data"))


def build_engine(data_dir=None):
    directory = _DATA_DIR if data_dir is None else data_dir
    return MatchEngine(EventStore(None if directory == ":memory:" else directory))


class Handler(BaseHTTPRequestHandler):
    engine = None  # 由 make_server 注入（类属性，进程内单例）

    # ------------------------------------------------------------ 基础
    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise DomainError("请求体不是合法 JSON", code="invalid_json", status=400)
        if not isinstance(payload, dict):
            raise DomainError("请求体必须是 JSON 对象", code="invalid_json", status=400)
        return payload

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            payload = self._read_json() if method == "POST" or method == "PUT" else {}
            event_time = normalize_event_time(payload.pop("event_time", None))
            self.route(method, parts.path.strip("/").split("/"), query, payload, event_time)
        except DomainError as exc:
            self._send(exc.status, exc.to_dict())
        except ValueError as exc:
            self._send(422, {"error": "validation_error", "message": str(exc)})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def log_message(self, fmt, *args):
        return

    # ------------------------------------------------------------ 路由
    def route(self, method, seg, query, payload, event_time):
        eng = self.engine
        if method == "GET" and seg == ["health"]:
            self._send(200, {"status": "ok"})
            return

        if method == "POST" and seg == ["qualifications"]:
            self._send(201, eng.register_qualification(payload, event_time))
        elif method == "POST" and len(seg) == 3 and seg[0] == "qualifications" \
                and seg[2] == "verify":
            self._send(200, eng.verify_qualification(seg[1], event_time))
        elif method == "GET" and len(seg) == 2 and seg[0] == "qualifications":
            qual = eng.qualifications.get(seg[1])
            if not qual:
                from .errors import NotFoundError
                raise NotFoundError("采购方资格不存在")
            self._send(200, qual)
        elif method == "POST" and seg == ["suppliers"]:
            self._send(201, eng.register_supplier(payload, event_time))
        elif method == "POST" and seg == ["rounds", "open"]:
            self._send(201, {"round_no": eng.open_round(event_time)})
        elif method == "POST" and len(seg) == 3 and seg[0] == "rounds" and seg[2] == "freeze":
            self._send(200, {"round_no": eng.freeze_round(event_time)})
        elif method == "GET" and len(seg) == 2 and seg[0] == "rounds":
            self._send(200, eng.round_state(int(seg[1])))
        elif method == "POST" and seg == ["demands"]:
            self._send(201, eng.submit_demand(payload, event_time))
        elif method == "PUT" and len(seg) == 2 and seg[0] == "demands":
            self._send(200, eng.revise_demand(seg[1], payload, event_time))
        elif method == "GET" and seg == ["demands"]:
            self._send(200, {"demands": list(eng.demands.values())})
        elif method == "GET" and len(seg) == 2 and seg[0] == "demands":
            from .errors import NotFoundError
            d = eng.demands.get(seg[1])
            if not d:
                raise NotFoundError("需求不存在")
            self._send(200, d)
        elif method == "POST" and seg == ["responses"]:
            record, replayed = eng.submit_response(payload, event_time)
            self._send(200 if replayed else 201,
                       {"replayed_idempotent": replayed, **record})
        elif method == "POST" and len(seg) == 3 and seg[0] == "responses" \
                and seg[2] == "withdraw":
            self._send(200, eng.withdraw_response(
                seg[1], event_time, payload.get("reason", "")))
        elif method == "POST" and len(seg) == 3 and seg[0] == "rounds" \
                and seg[2] == "recommendations":
            items = eng.generate_recommendations(
                int(seg[1]), event_time, payload.get("demand_id"))
            self._send(201, {"round_no": int(seg[1]), "results": items})
        elif method == "POST" and seg == ["manual-adjustments"]:
            self._send(200, eng.manual_adjust(payload, event_time))
        elif method == "POST" and seg == ["negotiations", "start"]:
            self._send(201, eng.start_negotiation(payload, event_time))
        elif method == "POST" and seg == ["negotiations", "offer"]:
            self._send(200, eng.exchange_offer(payload, event_time))
        elif method == "POST" and seg == ["negotiations", "confirm"]:
            self._send(200, eng.confirm_negotiation(payload, event_time))
        elif method == "POST" and seg == ["negotiations", "cancel"]:
            self._send(200, eng.cancel_negotiation(payload, event_time))
        elif method == "GET" and len(seg) == 3 and seg[0] == "demands" \
                and seg[2] == "explain":
            viewer = {"kind": query.get("kind", ""),
                      "buyer_id": query.get("id"),
                      "supplier_id": query.get("id")}
            self._send(200, eng.explain(seg[1], viewer))
        elif method == "GET" and seg == ["audit"]:
            self._send(200, {"records": eng.audit_trail(query.get("demand_id"))})
        else:
            self._send(404, {"error": "not_found", "message": f"无此路由: {method} /{'/'.join(seg)}"})


def make_server(host="127.0.0.1", port=8000, data_dir=None):
    Handler.engine = build_engine(data_dir)
    return ThreadingHTTPServer((host, port), Handler)


def run():
    host = os.environ.get("MATCH_HOST", "127.0.0.1")
    port = int(os.environ.get("MATCH_PORT", "8000"))
    server = make_server(host, port)
    print(f"跨境需求撮合枢纽已启动: http://{host}:{port}  数据目录={_DATA_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    run()
