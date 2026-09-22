"""撮合核心状态机：校验、轮次冻结、匹配评分、人工调整、增量重算、预算持有与重放还原。

所有写方法都在单一状态锁内串行化；事件先追加落盘再更新内存，
崩溃/重启时通过重放 events / qualifications / audit 三类日志完整还原。
"""
import copy
import hashlib
import threading
import uuid
from datetime import date

from .clock import now_utc
from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .models import (
    FORBIDDEN_COUNTRIES,
    REQUIRED_COUNTRIES,
    parse_country_rule,
    parse_date,
    parse_money,
)

SCORE_WEIGHTS = {
    "price": 40,
    "delivery": 20,
    "language": 15,
    "country": 10,
    "capacity": 10,
    "joint": 5,
}


def _buyer_code(buyer_id):
    """公开侧使用的匿名采购方代号；实名映射只存在于资格日志。"""
    return "B-" + hashlib.sha256(f"buyer:{buyer_id}".encode()).hexdigest()[:12]


def _d(value):
    return date.fromisoformat(value)


class MatchEngine:
    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        # 实名侧
        self.qualifications = {}        # buyer_id -> qual dict
        self.buyer_codes = {}           # buyer_code -> buyer_id（仅实名服务可访问）
        # 公开侧
        self.suppliers = {}             # supplier_id -> supplier dict
        self.demands = {}               # demand_id -> demand dict（当前修订版）
        self.demand_history = {}        # demand_id -> [修订快照]
        self.responses = {}             # response_id -> response dict
        self.by_supplier_demand = {}    # (supplier_id, demand_id) -> response_id（防重复）
        self.idempotency = {}           # (supplier_id, key) -> response_id
        self.rounds = {}                # round_no -> round 状态
        self.current_open_round = None
        # 撮合产物 round_no -> demand_id -> [recommendation]
        self.recommendations = {}
        self.exclusions = {}            # (round_no, demand_id, response_id) -> reason
        self.overrides = {}             # (round_no, demand_id, response_id) -> pinned_rank
        # 洽谈与预算
        self.deals = {}                 # demand_id -> deal dict
        self.holds = {}                 # hold_id -> hold dict
        self.restore()

    # ------------------------------------------------------------------ 还原
    def restore(self):
        """重放全部日志，还原每轮推荐、快照与洽谈状态。"""
        with self._lock:
            for record in self.store.replay("qualifications"):
                self._apply(record)
            for record in self.store.replay("audit"):
                self._apply(record)
            for record in self.store.replay("events"):
                self._apply(record)

    def _append_event(self, etype, payload, event_time, log="events"):
        stamp = now_utc()
        seq = self.store.append(log, etype, payload, event_time, stamp)
        self._apply({"seq": seq, "type": etype, "event_time": event_time,
                     "received_at": stamp, "payload": payload})
        return seq

    def _apply(self, record):
        etype = record["type"]
        p = record["payload"]
        if etype == "qualification_registered":
            self.qualifications[p["buyer_id"]] = p
            self.buyer_codes[p["buyer_code"]] = p["buyer_id"]
        elif etype == "qualification_verified":
            if p["buyer_id"] in self.qualifications:
                self.qualifications[p["buyer_id"]]["verified"] = True
        elif etype == "supplier_registered":
            self.suppliers[p["supplier_id"]] = p
        elif etype == "round_opened":
            self.rounds[p["round_no"]] = {
                "round_no": p["round_no"], "status": "open",
                "opened_at": record["received_at"], "frozen_at": None, "snapshot": None,
            }
            self.current_open_round = p["round_no"]
        elif etype == "round_frozen":
            rnd = self.rounds[p["round_no"]]
            rnd["status"] = "frozen"
            rnd["frozen_at"] = record["received_at"]
            rnd["snapshot"] = p["snapshot"]
            if self.current_open_round == p["round_no"]:
                self.current_open_round = None
        elif etype in ("demand_submitted", "demand_revised"):
            self.demands[p["demand_id"]] = p
            self.demand_history.setdefault(p["demand_id"], []).append(p)
        elif etype == "response_submitted":
            self.responses[p["response_id"]] = p
            for member in p["members"]:
                self.by_supplier_demand[(member, p["demand_id"])] = p["response_id"]
            self.idempotency[(p["supplier_id"], p["idempotency_key"])] = p["response_id"]
        elif etype == "response_withdrawn":
            resp = self.responses[p["response_id"]]
            resp["status"] = "withdrawn"
            for member in resp["members"]:
                self.by_supplier_demand.pop((member, resp["demand_id"]), None)
        elif etype == "recommendations_generated":
            self.recommendations.setdefault(p["round_no"], {})[p["demand_id"]] = p["items"]
        elif etype == "manual_exclude":
            self.exclusions[(p["round_no"], p["demand_id"], p["response_id"])] = p["reason"]
        elif etype == "manual_reinstate":
            self.exclusions.pop((p["round_no"], p["demand_id"], p["response_id"]), None)
            self.overrides.pop((p["round_no"], p["demand_id"], p["response_id"]), None)
        elif etype == "manual_override_rank":
            self.overrides[(p["round_no"], p["demand_id"], p["response_id"])] = p["new_rank"]
        elif etype == "negotiation_started":
            self.deals[p["demand_id"]] = {
                "demand_id": p["demand_id"], "response_id": p["response_id"],
                "status": "negotiating", "history": [
                    {"at": record["received_at"], "event": "started", "by": p.get("operator_id")}],
                "hold_id": p.get("hold_id"),
            }
        elif etype == "offer_exchanged":
            deal = self.deals[p["demand_id"]]
            deal["history"].append({"at": record["received_at"], "event": "offer",
                                    "by": p["sender"], "terms_ref": p.get("terms_ref")})
        elif etype == "negotiation_confirmed":
            deal = self.deals[p["demand_id"]]
            deal["status"] = "confirmed"
            deal["confirmed_at"] = record["received_at"]
            deal["history"].append({"at": record["received_at"], "event": "confirmed",
                                    "by": p.get("operator_id")})
        elif etype == "negotiation_cancelled":
            deal = self.deals.get(p["demand_id"])
            if deal:
                deal["status"] = "cancelled"
                deal["history"].append({"at": record["received_at"], "event": "cancelled",
                                        "reason": p.get("reason", "")})
        elif etype in ("hold_granted", "hold_released", "budget_committed"):
            if etype == "hold_granted":
                self.holds[p["hold_id"]] = dict(p, status="held",
                                                granted_at=record["received_at"])
            elif etype == "budget_committed":
                hold = self.holds.get(p["hold_id"])
                if hold:
                    hold["status"] = "committed"
                    hold["committed_at"] = record["received_at"]
            else:
                hold = self.holds.get(p["hold_id"])
                if hold:
                    hold["status"] = "released"
                    hold["released_at"] = record["received_at"]

    # ---------------------------------------------------------- 实名资格
    def register_qualification(self, payload, event_time):
        errors = []
        buyer_id = payload.get("buyer_id")
        if not isinstance(buyer_id, str) or not buyer_id.strip():
            errors.append("buyer_id 必须是非空字符串")
        legal_name = payload.get("legal_name")
        if not isinstance(legal_name, str) or not legal_name.strip():
            errors.append("legal_name 必须是非空字符串（实名）")
        contact = payload.get("contact")
        if not isinstance(contact, str) or not contact.strip():
            errors.append("contact 必填")
        country = payload.get("country")
        if not isinstance(country, str) or not country.strip():
            errors.append("country 必填")
        industries = payload.get("industries", [])
        if not isinstance(industries, list) or not industries:
            errors.append("industries 必须是非空数组")
        budget_limit = parse_money(payload, "budget_limit", errors)
        decision_makers = payload.get("decision_makers", [])
        if not isinstance(decision_makers, list) or not all(
                isinstance(m, str) and m.strip() for m in decision_makers):
            errors.append("decision_makers 必须是非空字符串数组（具决策权联系人）")
        if errors:
            raise ValidationError("资格信息校验失败", details={"errors": errors})
        data = {
            "buyer_id": buyer_id.strip(),
            "buyer_code": _buyer_code(buyer_id.strip()),
            "legal_name": legal_name.strip(),
            "contact": contact.strip(),
            "country": country.strip().upper(),
            "industries": industries,
            "budget_limit": budget_limit,
            "decision_makers": decision_makers,
            "verified": bool(payload.get("verified", False)),
        }
        with self._lock:
            if data["buyer_id"] in self.qualifications:
                raise ConflictError("该采购方资格已登记，如需变更请走资格更新流程")
            self.store.append("qualifications", "qualification_registered", data,
                              event_time, now_utc())
            self._apply_lite_qual(data)
            return data

    def _apply_lite_qual(self, data):
        self.qualifications[data["buyer_id"]] = data
        self.buyer_codes[data["buyer_code"]] = data["buyer_id"]

    def verify_qualification(self, buyer_id, event_time):
        with self._lock:
            qual = self.qualifications.get(buyer_id)
            if not qual:
                raise NotFoundError("采购方资格不存在")
            qual["verified"] = True
            # 资格更新仍为仅追加：登记一条核验事件
            self.store.append("qualifications", "qualification_verified",
                              {"buyer_id": buyer_id}, event_time, now_utc())
            return qual

    # ---------------------------------------------------------- 供应商
    def register_supplier(self, payload, event_time):
        errors = []
        sid = payload.get("supplier_id")
        if not isinstance(sid, str) or not sid.strip():
            errors.append("supplier_id 必须是非空字符串")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("name 必填")
        country = payload.get("country")
        if not isinstance(country, str) or not country.strip():
            errors.append("country 必填")
        industries = payload.get("industries", [])
        if not isinstance(industries, list) or not industries:
            errors.append("industries 必须是非空数组")
        languages = payload.get("languages", [])
        if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
            errors.append("languages 必须是字符串数组")
        capacity = payload.get("capacity_per_round")
        if not isinstance(capacity, (int, float)) or isinstance(capacity, bool) or capacity <= 0:
            errors.append("capacity_per_round 必须是正数")
        excludes = payload.get("excludes_buyers", [])
        if not isinstance(excludes, list) or not all(isinstance(x, str) for x in excludes):
            errors.append("excludes_buyers 必须是字符串数组（利益冲突申报）")
        if errors:
            raise ValidationError("供应商信息校验失败", details={"errors": errors})
        data = {
            "supplier_id": sid.strip(),
            "name": name.strip(),
            "country": country.strip().upper(),
            "industries": list(industries),
            "languages": [x.upper() for x in languages],
            "capacity_per_round": capacity,
            "excludes_buyers": list(excludes),
        }
        with self._lock:
            if data["supplier_id"] in self.suppliers:
                raise ConflictError("该供应商已登记")
            self._append_event("supplier_registered", data, event_time)
            return self._public_demand_safe_supplier(data)

    @staticmethod
    def _public_demand_safe_supplier(data):
        return dict(data)

    # ---------------------------------------------------------- 轮次
    def open_round(self, event_time):
        with self._lock:
            if self.current_open_round is not None:
                raise ConflictError(f"轮次 {self.current_open_round} 仍开放，请先冻结")
            round_no = (max(self.rounds) + 1) if self.rounds else 1
            self._append_event("round_opened", {"round_no": round_no}, event_time)
            return round_no

    def freeze_round(self, event_time):
        """冻结当前轮：需求与响应在此刻定型，生成不可变快照。"""
        with self._lock:
            round_no = self.current_open_round
            if round_no is None:
                raise ConflictError("当前没有开放中的轮次")
            snapshot = {
                # 深拷贝：冻结后响应退出等就地变更不得污染不可变快照
                "demands": copy.deepcopy([d for d in self.demands.values() if d["round_no"] == round_no]),
                "responses": copy.deepcopy([r for r in self.responses.values() if r["round_no"] == round_no]),
            }
            self._append_event("round_frozen",
                               {"round_no": round_no, "snapshot": snapshot}, event_time)
            return round_no

    # ---------------------------------------------------------- 需求
    def _validate_demand_payload(self, payload, partial=False):
        errors = []
        data = {}
        data["title"] = payload.get("title")
        if not isinstance(data["title"], str) or not data["title"].strip():
            errors.append("title 必填")
        data["industry"] = payload.get("industry")
        if not isinstance(data["industry"], str) or not data["industry"].strip():
            errors.append("industry 必填")
        qty = payload.get("quantity")
        if not isinstance(qty, (int, float)) or isinstance(qty, bool) or qty <= 0:
            errors.append("quantity 必须是正数")
        data["quantity"] = qty
        data["budget"] = parse_money(payload, "budget", errors)
        window = payload.get("delivery_window")
        if not isinstance(window, dict):
            errors.append("delivery_window 必须包含 start 和 end (YYYY-MM-DD)")
            data["delivery_window"] = None
        else:
            start = parse_date(window, "start", errors, True)
            end = parse_date(window, "end", errors, True)
            if start and end and _d(end) < _d(start):
                errors.append("delivery_window.end 不能早于 start")
            data["delivery_window"] = {"start": start, "end": end} if start else None
        data["country_rule"] = parse_country_rule(payload, errors)
        languages = payload.get("languages", [])
        if not isinstance(languages, list) or not all(isinstance(x, str) and x.strip() for x in languages):
            errors.append("languages 必须是非空字符串数组")
        data["languages"] = [x.upper() for x in languages] if isinstance(languages, list) else []
        data["notes"] = payload.get("notes", "")
        return data, errors

    def submit_demand(self, payload, event_time):
        with self._lock:
            buyer_id = payload.get("buyer_id")
            qual = self.qualifications.get(buyer_id) if isinstance(buyer_id, str) else None
            if not qual:
                raise NotFoundError("采购方资格不存在，请先完成实名登记")
            if not qual["verified"]:
                raise ForbiddenError("采购方资格尚未通过核验，不能发布需求")
            if self.current_open_round is None:
                raise ConflictError("当前没有开放轮次，无法提交需求")
            data, errors = self._validate_demand_payload(payload)
            demand_id = payload.get("demand_id")
            if not isinstance(demand_id, str) or not demand_id.strip():
                errors.append("demand_id 必须由调用方提供稳定标识")
            if errors:
                raise ValidationError("需求校验失败", details={"errors": errors})
            if demand_id.strip() in self.demands:
                raise ConflictError("demand_id 已存在，请使用修订接口")
            if data["budget"]["currency"] != qual["budget_limit"]["currency"] or \
                    data["budget"]["amount"] > qual["budget_limit"]["amount"]:
                raise ForbiddenError("需求预算超出采购方登记的预算限额或币种")
            record = {
                "demand_id": demand_id.strip(),
                "buyer_code": qual["buyer_code"],  # 公开侧只有匿名代号
                "round_no": self.current_open_round,
                "revision": 1,
                "status": "open",
                "event_time": event_time,
                **{k: v for k, v in data.items()},
            }
            self._append_event("demand_submitted", record, event_time)
            return self._public_demand(record)

    def revise_demand(self, demand_id, payload, event_time):
        with self._lock:
            current = self.demands.get(demand_id)
            if not current:
                raise NotFoundError("需求不存在")
            if current["round_no"] != self.current_open_round:
                raise ConflictError("需求所在轮次已冻结，修订只能发生在轮次冻结前")
            data, errors = self._validate_demand_payload(payload)
            if errors:
                raise ValidationError("需求修订校验失败", details={"errors": errors})
            qual = self.qualifications[self.buyer_codes[current["buyer_code"]]]
            if data["budget"]["currency"] != qual["budget_limit"]["currency"] or \
                    data["budget"]["amount"] > qual["budget_limit"]["amount"]:
                raise ForbiddenError("修订后预算超出采购方登记的预算限额")
            record = {
                **current,
                "revision": current["revision"] + 1,
                "event_time": event_time,
                **data,
            }
            self._append_event("demand_revised", record, event_time)
            return self._public_demand(record)

    @staticmethod
    def _public_demand(record):
        """公开需求视图：不含任何实名字段。"""
        view = dict(record)
        view.pop("buyer_id", None)
        return view

    # ---------------------------------------------------------- 供应商响应
    def submit_response(self, payload, event_time):
        with self._lock:
            errors = []
            did = payload.get("demand_id")
            sid = payload.get("supplier_id")
            idem = payload.get("idempotency_key")
            demand = self.demands.get(did) if isinstance(did, str) else None
            supplier = self.suppliers.get(sid) if isinstance(sid, str) else None
            if demand is None:
                errors.append("demand_id 不存在")
            if supplier is None:
                errors.append("supplier_id 未登记")
            if not isinstance(idem, str) or not idem.strip():
                errors.append("idempotency_key 必填（防重复提交）")
            members = payload.get("members", [sid] if sid else [])
            if not isinstance(members, list) or not members or not all(
                    isinstance(m, str) for m in members):
                errors.append("members 必须是非空供应商编号数组（联合方案含全部成员）")
            elif sid not in members:
                errors.append("联合方案 members 必须包含牵头方 supplier_id")
            elif any(m not in self.suppliers for m in members):
                errors.append("members 中存在未登记供应商")
            if errors:
                raise ValidationError("响应校验失败", details={"errors": errors})

            # 幂等重试：同键直接返回首次结果，不重复落库
            existing = self.idempotency.get((sid, idem))
            if existing:
                return self.responses[existing], True

            if self.current_open_round != demand["round_no"]:
                raise ConflictError("该需求轮次不在开放期，不能提交响应")
            # 防重复：牵头方及联合体每个成员都只能在该需求上出现一次
            for member in members:
                if (member, did) in self.by_supplier_demand:
                    raise ConflictError(
                        f"供应商 {member} 已对该需求存在有效响应（含联合方案成员身份）",
                        details={"blocked_member": member})

            verrs = []
            quote = parse_money(payload, "quote", verrs)
            deliverable = parse_date(payload, "deliverable_date", verrs, True)
            languages = payload.get("languages", supplier["languages"])
            if not isinstance(languages, list) or not languages:
                verrs.append("languages 必须是非空数组")
            if verrs:
                raise ValidationError("响应内容校验失败", details={"errors": verrs})

            response_id = "resp-" + uuid.uuid4().hex[:12]
            record = {
                "response_id": response_id,
                "demand_id": did,
                "supplier_id": sid,
                "round_no": demand["round_no"],
                "members": list(dict.fromkeys(members)),
                "lead_id": sid,
                "quote": quote,
                "deliverable_date": deliverable,
                "languages": [x.upper() for x in languages],
                "idempotency_key": idem,
                "status": "active",
                "event_time": event_time,
            }
            self._append_event("response_submitted", record, event_time)
            return record, False

    def withdraw_response(self, response_id, event_time, reason=""):
        with self._lock:
            resp = self.responses.get(response_id)
            if not resp:
                raise NotFoundError("响应不存在")
            if resp["status"] == "withdrawn":
                raise ConflictError("响应已退出")
            self._append_event("response_withdrawn",
                               {"response_id": response_id, "reason": reason}, event_time)
            # 候选退出：取消依赖它的洽谈并释放预算持有
            deal = self.deals.get(resp["demand_id"])
            if deal and deal["response_id"] == response_id and deal["status"] == "negotiating":
                self._cancel_deal(resp["demand_id"], event_time, "候选供应商退出")
            # 若推荐已生成，仅重算该需求这一条关系链
            rnd = resp["round_no"]
            if self.rounds.get(rnd, {}).get("status") == "frozen":
                self._recompute(resp["demand_id"], event_time)
            return {"response_id": response_id, "status": "withdrawn"}

    # ---------------------------------------------------------- 匹配
    def _hard_constraints(self, demand, resp):
        """返回 (eligible, reasons[])，原因只陈述规则不引用竞争报价。"""
        reasons = []
        lead = self.suppliers[resp["lead_id"]]
        members = [self.suppliers[m] for m in resp["members"]]
        qual = self.qualifications[self.buyer_codes[demand["buyer_code"]]]

        if demand["industry"] not in lead["industries"] and \
                not any(demand["industry"] in m["industries"] for m in members):
            reasons.append("行业能力不匹配：供应方未覆盖需求所属行业")
        rule = demand["country_rule"]
        countries = {m["country"] for m in members}
        if rule["policy"] == REQUIRED_COUNTRIES and \
                lead["country"] not in rule["countries"]:
            reasons.append("国家准入不符：牵头方注册国不在采购方许可名单")
        if rule["policy"] == FORBIDDEN_COUNTRIES and \
                lead["country"] in rule["countries"]:
            reasons.append("国家准入不符：牵头方注册国属于受限名单")
        total_capacity = sum(m["capacity_per_round"] for m in members)
        if total_capacity < demand["quantity"]:
            reasons.append("交付能力不足：联合产能低于需求数量")
        if resp["quote"]["currency"] != demand["budget"]["currency"]:
            reasons.append("报价币种与需求预算币种不一致")
        elif resp["quote"]["amount"] > demand["budget"]["amount"]:
            reasons.append("报价超出采购预算")
        ddate = _d(resp["deliverable_date"])
        win_end = _d(demand["delivery_window"]["end"])
        win_start = _d(demand["delivery_window"]["start"])
        if ddate > win_end:
            reasons.append("可交付日期晚于交付窗口")
        if ddate < win_start:
            reasons.append("可交付日期早于交付窗口起点")
        if not set(resp["languages"]) & set(demand["languages"]):
            reasons.append("语言不匹配：无法覆盖需求要求的任一语言")
        # 利益冲突：任一成员申报与该采购方存在冲突
        if qual["buyer_id"] in lead["excludes_buyers"] or any(
                qual["buyer_id"] in m["excludes_buyers"] for m in members):
            reasons.append("利益冲突：供应方已申报与该采购方存在回避关系")
        return (len(reasons) == 0), reasons

    def _score(self, demand, resp):
        lead = self.suppliers[resp["lead_id"]]
        members = [self.suppliers[m] for m in resp["members"]]
        factors = {}
        # 价格分：预算内越接近预算下限得分越高
        headroom = max(0.0, (demand["budget"]["amount"] - resp["quote"]["amount"])
                       / demand["budget"]["amount"])
        factors["price"] = round(SCORE_WEIGHTS["price"] * headroom, 2)
        # 交付分：相对窗口提前量
        win_start = _d(demand["delivery_window"]["start"])
        win_end = _d(demand["delivery_window"]["end"])
        ddate = _d(resp["deliverable_date"])
        span = max((win_end - win_start).days, 1)
        early = min(max((win_end - ddate).days / span, 0), 1)
        factors["delivery"] = round(SCORE_WEIGHTS["delivery"] * early, 2)
        # 语言分：需求语言覆盖率
        overlap = len(set(resp["languages"]) & set(demand["languages"]))
        factors["language"] = round(
            SCORE_WEIGHTS["language"] * overlap / max(len(demand["languages"]), 1), 2)
        # 国别分：牵头方在许可名单内得满分
        rule = demand["country_rule"]
        if rule["policy"] == REQUIRED_COUNTRIES:
            factors["country"] = SCORE_WEIGHTS["country"] \
                if lead["country"] in rule["countries"] else 0
        elif rule["policy"] == FORBIDDEN_COUNTRIES:
            factors["country"] = 0 if lead["country"] in rule["countries"] \
                else SCORE_WEIGHTS["country"]
        else:
            factors["country"] = SCORE_WEIGHTS["country"]
        # 产能裕度
        total_capacity = sum(m["capacity_per_round"] for m in members)
        ratio = min(total_capacity / demand["quantity"], 2) / 2
        factors["capacity"] = round(SCORE_WEIGHTS["capacity"] * ratio, 2)
        # 联合方案完整性：成员行业对需求的覆盖（单方响应按自身覆盖计）
        covered = 1 if any(demand["industry"] in m["industries"] for m in members) else 0
        factors["joint"] = SCORE_WEIGHTS["joint"] * covered
        total = round(sum(factors.values()), 2)
        return total, factors

    def _rank(self, demand, responses):
        rows = []
        for resp in responses:
            eligible, reasons = self._hard_constraints(demand, resp)
            if eligible:
                score, factors = self._score(demand, resp)
            else:
                score, factors = 0.0, {k: 0.0 for k in SCORE_WEIGHTS}
            rows.append({
                "response_id": resp["response_id"],
                "supplier_id": resp["lead_id"],
                "members": resp["members"],
                "eligible": eligible,
                "score": score,
                "factors": factors,
                "reasons": reasons,
            })
        eligible_rows = sorted([r for r in rows if r["eligible"]],
                               key=lambda r: (-r["score"], r["response_id"]))
        for i, row in enumerate(eligible_rows, 1):
            row["rank"] = i
        for row in rows:
            if not row["eligible"]:
                row["rank"] = None
        return self._apply_manual(demand, rows, eligible_rows)

    def _apply_manual(self, demand, rows, eligible_rows):
        """套用人工排除与排名钉选；钉选占用名次，其余候选顺延。"""
        rnd = demand["round_no"]
        kept, dropped = [], []
        for row in rows:
            reason = self.exclusions.get((rnd, demand["demand_id"], row["response_id"]))
            if reason is not None:
                row["eligible"] = False
                row["rank"] = None
                row["reasons"] = [f"人工排除：{reason}"] + row["reasons"]
                row["manual"] = "excluded"
                dropped.append(row)
            else:
                kept.append(row)
        # 重新排名后应用钉选
        kept_e = sorted([r for r in kept if r["eligible"]],
                        key=lambda r: (-r["score"], r["response_id"]))
        pinned = {}
        for row in kept_e:
            pin = self.overrides.get((rnd, demand["demand_id"], row["response_id"]))
            if pin is not None:
                pinned[row["response_id"]] = max(1, int(pin))
        ordered = [None] * len(kept_e)
        for rid, pin in pinned.items():
            idx = min(pin, len(kept_e)) - 1
            if ordered[idx] is None:
                ordered[idx] = rid
            else:  # 名次冲突时顺延到最近空位
                for j in range(len(ordered)):
                    if ordered[j] is None:
                        ordered[j] = rid
                        break
        by_id = {r["response_id"]: r for r in kept_e}
        leftovers = [r for r in kept_e if r["response_id"] not in pinned]
        li = 0
        for j in range(len(ordered)):
            if ordered[j] is None:
                ordered[j] = leftovers[li]["response_id"]
                li += 1
        for rank, rid in enumerate(ordered, 1):
            row = by_id[rid]
            row["rank"] = rank
            if rid in pinned:
                row["manual"] = "override_rank"
        for row in kept:
            if not row["eligible"]:
                row["rank"] = None
        # 输出：合格者按名次，不合格（含人工排除）附后
        result = [by_id[rid] for rid in ordered] + \
                 [r for r in kept if not r["eligible"]] + dropped
        return result

    def generate_recommendations(self, round_no, event_time, demand_id=None):
        with self._lock:
            rnd = self.rounds.get(round_no)
            if not rnd:
                raise NotFoundError("轮次不存在")
            if rnd["status"] != "frozen":
                raise ConflictError("轮次尚未冻结，不能生成推荐")
            targets = [d for d in self.demands.values() if d["round_no"] == round_no]
            if demand_id:
                targets = [d for d in targets if d["demand_id"] == demand_id]
                if not targets:
                    raise NotFoundError("该轮次下无此需求")
            all_items = {}
            for demand in targets:
                resps = [r for r in self.responses.values()
                         if r["demand_id"] == demand["demand_id"] and r["status"] == "active"
                         and r["round_no"] == round_no]
                items = self._rank(demand, resps)
                seq = self.store.append(
                    "events", "recommendations_generated",
                    {"round_no": round_no, "demand_id": demand["demand_id"], "items": items},
                    event_time, now_utc())
                self.recommendations.setdefault(round_no, {})[demand["demand_id"]] = items
                all_items[demand["demand_id"]] = items
            return all_items

    def _recompute(self, demand_id, event_time):
        """增量重算：只重建受影响需求的推荐，其他需求的持有与排名不动。"""
        demand = self.demands[demand_id]
        rnd = demand["round_no"]
        resps = [r for r in self.responses.values()
                 if r["demand_id"] == demand_id and r["status"] == "active"
                 and r["round_no"] == rnd]
        items = self._rank(demand, resps)
        self.store.append("events", "recommendations_generated",
                          {"round_no": rnd, "demand_id": demand_id, "items": items,
                           "cause": "incremental_recompute"},
                          event_time, now_utc())
        self.recommendations.setdefault(rnd, {})[demand_id] = items
        return items

    # ---------------------------------------------------------- 人工调整
    def manual_adjust(self, payload, event_time):
        with self._lock:
            action = payload.get("action")
            operator = payload.get("operator_id")
            reason = payload.get("reason", "")
            rid = payload.get("response_id")
            if not isinstance(operator, str) or not operator.strip():
                raise ValidationError("operator_id 必填，人工调整必须留痕到人")
            resp = self.responses.get(rid) if isinstance(rid, str) else None
            if not resp:
                raise NotFoundError("响应不存在")
            demand = self.demands[resp["demand_id"]]
            key = (resp["round_no"], demand["demand_id"], rid)
            deal = self.deals.get(demand["demand_id"])
            if deal and deal["response_id"] == rid and deal["status"] == "confirmed":
                raise ConflictError("该候选已完成预算承诺确认，不能再人工调整")
            old_rank = self._rank_of(resp["round_no"], demand["demand_id"], rid)

            if action == "exclude":
                if not reason.strip():
                    raise ValidationError("人工排除必须填写 reason")
                self.store.append("audit", "manual_exclude", {
                    "round_no": resp["round_no"], "demand_id": demand["demand_id"],
                    "response_id": rid, "operator_id": operator, "reason": reason,
                    "old_rank": old_rank,
                }, event_time, now_utc())
                self.exclusions[key] = reason
                if deal and deal["response_id"] == rid and deal["status"] == "negotiating":
                    self._cancel_deal(demand["demand_id"], event_time, f"人工排除：{reason}")
            elif action == "reinstate":
                self.store.append("audit", "manual_reinstate", {
                    "round_no": resp["round_no"], "demand_id": demand["demand_id"],
                    "response_id": rid, "operator_id": operator, "reason": reason,
                    "old_rank": old_rank,
                }, event_time, now_utc())
                self.exclusions.pop(key, None)
                self.overrides.pop(key, None)
            elif action == "override_rank":
                new_rank = payload.get("new_rank")
                if not isinstance(new_rank, int) or isinstance(new_rank, bool) or new_rank < 1:
                    raise ValidationError("new_rank 必须是 >=1 的整数")
                current_row = next((r for r in self.recommendations.get(resp["round_no"], {})
                                    .get(demand["demand_id"], [])
                                    if r["response_id"] == rid), None)
                if current_row is None:
                    raise ConflictError("推荐尚未生成，无法钉选排名")
                if not current_row["eligible"]:
                    raise ConflictError("不合格或已排除候选不能钉选排名，请先恢复资格")
                self.store.append("audit", "manual_override_rank", {
                    "round_no": resp["round_no"], "demand_id": demand["demand_id"],
                    "response_id": rid, "operator_id": operator, "reason": reason,
                    "old_rank": old_rank, "new_rank": new_rank,
                }, event_time, now_utc())
                self.overrides[key] = new_rank
            else:
                raise ValidationError("action 必须是 exclude / reinstate / override_rank")

            items = self._recompute(demand["demand_id"], event_time) \
                if self.rounds[resp["round_no"]]["status"] == "frozen" else None
            return {"action": action, "response_id": rid,
                    "old_rank": old_rank,
                    "new_rank": self._rank_of(resp["round_no"], demand["demand_id"], rid),
                    "items": items}

    def _rank_of(self, round_no, demand_id, response_id):
        for row in self.recommendations.get(round_no, {}).get(demand_id, []):
            if row["response_id"] == response_id:
                return row.get("rank")
        return None

    # ---------------------------------------------------------- 洽谈与预算
    def _release_other_holds(self, demand_id, event_time):
        for hold in self.holds.values():
            if hold["demand_id"] == demand_id and hold["status"] == "held":
                self.store.append("events", "hold_released",
                                  {"hold_id": hold["hold_id"],
                                   "reason": "同需求转入新候选洽谈"},
                                  event_time, now_utc())
                hold["status"] = "released"
                hold["released_at"] = now_utc()

    def start_negotiation(self, payload, event_time):
        with self._lock:
            did = payload.get("demand_id")
            rid = payload.get("response_id")
            operator = payload.get("operator_id")
            demand = self.demands.get(did)
            resp = self.responses.get(rid)
            if not demand or not resp or resp["demand_id"] != did:
                raise NotFoundError("需求或响应不存在")
            if self.rounds.get(demand["round_no"], {}).get("status") != "frozen":
                raise ConflictError("轮次冻结后才能开始洽谈")
            row = next((r for r in self.recommendations.get(demand["round_no"], {})
                        .get(did, []) if r["response_id"] == rid), None)
            if not row or not row["eligible"]:
                raise ForbiddenError("该候选当前不处于合格名单，不能发起洽谈")
            existing = self.deals.get(did)
            if existing and existing["status"] == "confirmed":
                raise ConflictError("该需求已有确认承诺，预算不可重复占用")
            if not isinstance(operator, str) or not operator.strip():
                raise ValidationError("operator_id 必填")

            hold_id = "hold-" + uuid.uuid4().hex[:12]
            self._release_other_holds(did, event_time)
            self.store.append("events", "hold_granted", {
                "hold_id": hold_id, "demand_id": did, "response_id": rid,
                "amount": resp["quote"]["amount"], "currency": resp["quote"]["currency"],
            }, event_time, now_utc())
            self.holds[hold_id] = {
                "hold_id": hold_id, "demand_id": did, "response_id": rid,
                "amount": resp["quote"]["amount"], "currency": resp["quote"]["currency"],
                "status": "held", "granted_at": now_utc(),
            }
            self._append_event("negotiation_started", {
                "demand_id": did, "response_id": rid,
                "operator_id": operator, "hold_id": hold_id,
            }, event_time)
            return self._deal_view(did)

    def exchange_offer(self, payload, event_time):
        with self._lock:
            did = payload.get("demand_id")
            sender = payload.get("sender")
            terms_ref = payload.get("terms_ref")
            deal = self.deals.get(did)
            if not deal or deal["status"] != "negotiating":
                raise ConflictError("没有进行中的洽谈")
            if sender not in ("buyer", "supplier"):
                raise ValidationError("sender 必须是 buyer 或 supplier")
            self._append_event("offer_exchanged", {
                "demand_id": did, "sender": sender,
                "terms_ref": terms_ref or f"offer-{uuid.uuid4().hex[:8]}",
            }, event_time)
            return self._deal_view(did)

    def confirm_negotiation(self, payload, event_time):
        """并发确认在状态锁下串行：第二个确认必然看到已 committed 持有而失败。"""
        with self._lock:
            did = payload.get("demand_id")
            operator = payload.get("operator_id")
            deal = self.deals.get(did)
            if not deal:
                raise NotFoundError("洽谈不存在")
            if deal["status"] == "confirmed":
                raise ConflictError("该需求预算已承诺，拒绝重复确认")
            if deal["status"] != "negotiating":
                raise ConflictError("洽谈不在进行中，不能确认")
            hold = self.holds.get(deal["hold_id"])
            if not hold or hold["status"] != "held":
                raise ConflictError("预算持有已失效，不能确认")
            resp = self.responses[deal["response_id"]]
            if resp["status"] != "active":
                raise ConflictError("候选响应已退出，持有将释放")
            self.store.append("events", "budget_committed",
                              {"hold_id": hold["hold_id"], "demand_id": did,
                               "response_id": deal["response_id"],
                               "amount": hold["amount"], "currency": hold["currency"]},
                              event_time, now_utc())
            hold["status"] = "committed"
            hold["committed_at"] = now_utc()
            self._append_event("negotiation_confirmed",
                               {"demand_id": did, "operator_id": operator or ""}, event_time)
            return self._deal_view(did)

    def _cancel_deal(self, did, event_time, reason):
        deal = self.deals.get(did)
        if deal and deal.get("hold_id"):
            hold = self.holds.get(deal["hold_id"])
            if hold and hold["status"] == "held":
                self.store.append("events", "hold_released",
                                  {"hold_id": hold["hold_id"], "reason": reason},
                                  event_time, now_utc())
                hold["status"] = "released"
                hold["released_at"] = now_utc()
        self.store.append("events", "negotiation_cancelled",
                          {"demand_id": did, "reason": reason}, event_time, now_utc())
        if deal:
            deal["status"] = "cancelled"
            deal["history"].append({"at": now_utc(), "event": "cancelled", "reason": reason})

    def cancel_negotiation(self, payload, event_time):
        with self._lock:
            did = payload.get("demand_id")
            deal = self.deals.get(did)
            if not deal or deal["status"] != "negotiating":
                raise ConflictError("没有进行中的洽谈")
            self._cancel_deal(did, event_time, payload.get("reason", "人工取消洽谈"))
            return self._deal_view(did)

    def _deal_view(self, did):
        deal = self.deals[did]
        hold = self.holds.get(deal.get("hold_id", ""))
        return {**deal, "hold": None if not hold else {
            "hold_id": hold["hold_id"], "status": hold["status"],
            "amount": hold["amount"], "currency": hold["currency"]}}

    # ---------------------------------------------------------- 查询与解释
    def round_state(self, round_no):
        with self._lock:
            rnd = self.rounds.get(round_no)
            if not rnd:
                raise NotFoundError("轮次不存在")
            return {
                "round_no": round_no, "status": rnd["status"],
                "opened_at": rnd["opened_at"], "frozen_at": rnd["frozen_at"],
                "demands": [d["demand_id"] for d in self.demands.values()
                            if d["round_no"] == round_no],
            }

    def explain(self, demand_id, viewer):
        """按视角解释为何匹配/拒绝，且不泄露竞争报价。

        viewer: {"kind": "buyer", "buyer_id"} 采购方看到候选名与规则解释，无报价；
                {"kind": "supplier", "supplier_id"} 仅看到自己的得分、因素与名次；
                {"kind": "operator"} 运营看到全貌（含报价），用于调解。
        """
        with self._lock:
            demand = self.demands.get(demand_id)
            if not demand:
                raise NotFoundError("需求不存在")
            items = list(self.recommendations.get(demand["round_no"], {})
                         .get(demand_id, []))
            kind = viewer.get("kind")
            if kind == "buyer":
                qual = self.qualifications.get(viewer.get("buyer_id"))
                if not qual or qual["buyer_code"] != demand["buyer_code"]:
                    raise ForbiddenError("仅需求所属采购方可查看本方解释")
                return self._explain_buyer(demand, items)
            if kind == "supplier":
                sid = viewer.get("supplier_id")
                if sid not in self.suppliers:
                    raise ForbiddenError("供应商身份无效")
                return self._explain_supplier(demand, items, sid)
            if kind == "operator":
                return self._explain_operator(demand, items)
            raise ValidationError("viewer.kind 必须是 buyer / supplier / operator")

    def _demand_brief(self, demand):
        return {
            "demand_id": demand["demand_id"], "title": demand["title"],
            "industry": demand["industry"], "round_no": demand["round_no"],
            "revision": demand["revision"], "delivery_window": demand["delivery_window"],
            "languages": demand["languages"], "country_rule": demand["country_rule"],
        }

    def _explain_buyer(self, demand, items):
        """采购方视角：能看到候选名称、总分、规则化原因；看不到任何报价数字。"""
        candidates = []
        for row in items:
            lead = self.suppliers[row["supplier_id"]]
            candidates.append({
                "rank": row.get("rank"),
                "supplier_name": lead["name"],
                "joint_with": [self.suppliers[m]["name"] for m in row["members"]
                               if m != row["supplier_id"]],
                "eligible": row["eligible"],
                "score": row["score"],
                "score_factors": row["factors"],  # 各因素得分（0-权重），非报价
                "reasons": row["reasons"],
                "manual": row.get("manual"),
            })
        return {"demand": self._demand_brief(demand), "currency_note": "候选报价已按规则隐藏",
                "candidates": candidates,
                "deal": self._safe_deal(demand["demand_id"])}

    def _explain_supplier(self, demand, items, sid):
        """供应商视角：只返回本供应商相关行，不含任何竞争对手信息。"""
        own_resp = next((r for r in self.responses.values()
                         if r["demand_id"] == demand["demand_id"]
                         and r["round_no"] == demand["round_no"]
                         and sid in r["members"] and r["status"] == "active"), None)
        if not own_resp:
            raise ForbiddenError("贵方未参与该需求，无可展示信息")
        row = next((r for r in items if r["response_id"] == own_resp["response_id"]), None)
        if not row:
            return {"demand": self._demand_brief(demand),
                    "message": "推荐尚未生成或响应已退出"}
        return {
            "demand": self._demand_brief(demand),
            "your_result": {
                "rank": row.get("rank"),
                "eligible": row["eligible"],
                "score": row["score"],
                "score_factors": row["factors"],
                "reasons": row["reasons"],
                "quote": own_resp["quote"],
                "deliverable_date": own_resp["deliverable_date"],
                "manual": row.get("manual"),
            },
            "total_eligible": len([r for r in items if r["eligible"]]),
        }

    def _explain_operator(self, demand, items):
        resp_by_id = self.responses
        candidates = []
        for row in items:
            resp = resp_by_id[row["response_id"]]
            candidates.append({**row, "quote": resp["quote"],
                               "deliverable_date": resp["deliverable_date"]})
        return {"demand": self._demand_brief(demand), "candidates": candidates,
                "deal": self._safe_deal(demand["demand_id"], full=True)}

    def _safe_deal(self, did, full=False):
        deal = self.deals.get(did)
        if not deal:
            return None
        view = {"status": deal["status"], "response_id": deal["response_id"]}
        if full:
            view["history"] = deal["history"]
        return view

    def audit_trail(self, demand_id=None):
        with self._lock:
            rows = []
            for rec in self.store.replay("audit"):
                if demand_id is None or rec["payload"].get("demand_id") == demand_id:
                    rows.append(rec)
            return rows
