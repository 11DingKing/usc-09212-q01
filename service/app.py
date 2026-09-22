"""撮合应用核心：事件驱动的状态机。

所有变更在事件存储的全局锁内"读取-判定-追加"，保证并发安全；
重启时重放事件还原全部状态（需求轮次、推荐、洽谈、预算台账）。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import matcher
from .catalog import Catalog
from .errors import (BudgetExhaustedError, ConflictError, ForbiddenError,
                     NotFoundError, ValidationError)
from .event_store import EventStore
from .vault import IdentityVault


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_id(response: "ResponseRec") -> str:
    return response.lead_supplier_id if response.kind == "solo" else f"consortium:{response.response_id}"


@dataclass
class ResponseRec:
    response_id: str
    requirement_id: str
    round_no: int
    lead_supplier_id: str
    members: list[str]
    kind: str
    quote: dict
    proposal: dict
    status: str = "active"            # active | withdrawn
    revision: int = 1
    submitted_at: str = ""

    @property
    def candidate_id(self) -> str:
        return _candidate_id(self)


@dataclass
class RecoRound:
    requirement_id: str
    round_no: int
    generations: list[dict] = field(default_factory=list)
    overrides: list[dict] = field(default_factory=list)
    recomputations: list[dict] = field(default_factory=list)
    locked: bool = False

    @property
    def latest(self) -> Optional[dict]:
        return self.generations[-1] if self.generations else None


@dataclass
class Negotiation:
    negotiation_id: str
    requirement_id: str
    round_no: int
    candidate_id: str
    kind: str
    members: list[str]
    status: str = "open"              # open | negotiating | confirmed | declined | closed
    hold_amount: float = 0.0
    committed_amount: float = 0.0
    currency: str = ""
    history: list[dict] = field(default_factory=list)
    opened_at: str = ""


class HubApplication:
    def __init__(self, store: EventStore, clock=utcnow):
        self.store = store
        self.clock = clock
        self.vault = IdentityVault()
        self.catalog = Catalog(clock=clock)
        self.responses: dict[str, ResponseRec] = {}
        self.recos: dict[tuple[str, int], RecoRound] = {}
        self.negotiations: dict[str, Negotiation] = {}
        # (requirement_id, round_no) -> {negotiation_id: amount}
        self.holds: dict[tuple[str, int], dict[str, float]] = {}
        self.committed: dict[tuple[str, int], dict[str, float]] = {}
        # 幂等键 -> 结果引用
        self.idempotency: dict[tuple[str, str], dict] = {}
        self._replayed = 0
        self._recover()

    # =========================================================
    # 事件重放
    # =========================================================
    def _recover(self) -> None:
        def handle(event_type: str, p: dict) -> None:
            self.vault.apply_event(event_type, p)
            self.catalog.apply_event(event_type, p)
            if event_type == "response.submitted":
                self.responses[p["response_id"]] = ResponseRec(**p)
            elif event_type == "response.amended":
                rec = self.responses[p["response_id"]]
                rec.quote = p["quote"]
                rec.proposal = p["proposal"]
                rec.revision = p["revision"]
            elif event_type == "response.withdrawn":
                self.responses[p["response_id"]].status = "withdrawn"
            elif event_type == "reco.generated":
                key = (p["requirement_id"], p["round_no"])
                reco = self.recos.setdefault(key, RecoRound(*key))
                reco.generations.append(p["generation"])
            elif event_type == "reco.overridden":
                reco = self.recos[(p["requirement_id"], p["round_no"])]
                reco.overrides.append(p["override"])
            elif event_type == "reco.recomputed":
                reco = self.recos[(p["requirement_id"], p["round_no"])]
                reco.recomputations.append(p["record"])
            elif event_type == "negotiation.opened":
                neg = Negotiation(
                    negotiation_id=p["negotiation_id"], requirement_id=p["requirement_id"],
                    round_no=p["round_no"], candidate_id=p["candidate_id"], kind=p["kind"],
                    members=list(p["members"]), status="open", hold_amount=p["hold_amount"],
                    currency=p["currency"], history=[p["entry"]], opened_at=p["at"])
                self.negotiations[neg.negotiation_id] = neg
                self.holds.setdefault((neg.requirement_id, neg.round_no), {})[neg.negotiation_id] = neg.hold_amount
            elif event_type == "negotiation.countered":
                neg = self.negotiations[p["negotiation_id"]]
                neg.status = "negotiating"
                neg.history.append(p["entry"])
            elif event_type == "negotiation.confirmed":
                neg = self.negotiations[p["negotiation_id"]]
                neg.status = "confirmed"
                neg.committed_amount = p["amount"]
                neg.hold_amount = 0.0
                neg.history.append(p["entry"])
                ledger_key = (neg.requirement_id, neg.round_no)
                self.holds.setdefault(ledger_key, {}).pop(neg.negotiation_id, None)
                self.committed.setdefault(ledger_key, {})[neg.negotiation_id] = p["amount"]
                self.recos[ledger_key].locked = True
            elif event_type in ("negotiation.declined", "negotiation.closed"):
                neg = self.negotiations[p["negotiation_id"]]
                neg.status = "declined" if event_type.endswith("declined") else "closed"
                neg.history.append(p["entry"])
                self.holds.get((neg.requirement_id, neg.round_no), {}).pop(neg.negotiation_id, None)
            elif event_type == "idempotency.recorded":
                self.idempotency[(p["scope"], p["key"])] = p["result"]

        self._replayed = self.store.replay(handle)

    # =========================================================
    # 采购方实名
    # =========================================================
    def register_buyer(self, buyer_ref: str, legal_name: str, contact: str,
                       credentials: Optional[dict] = None, verified: bool = False) -> dict:
        with self.store.lock:
            profile = self.vault.register(buyer_ref, legal_name, contact, credentials, verified)
            self.store.append("buyer.registered", {
                "buyer_ref": buyer_ref, "legal_name": profile.legal_name,
                "contact": profile.contact, "credentials": profile.credentials,
                "verified": profile.verified,
            })
            return {"buyer_ref": buyer_ref, "verified": profile.verified}

    def verify_buyer(self, buyer_ref: str, verified: bool = True, actor: str = "staff") -> dict:
        with self.store.lock:
            self.vault.get(buyer_ref)
            self.store.append("buyer.verified",
                              {"buyer_ref": buyer_ref, "verified": verified, "actor": actor,
                               "at": self.clock()})
            self.vault.apply_event("buyer.verified",
                                   {"buyer_ref": buyer_ref, "verified": verified})
            return {"buyer_ref": buyer_ref, "verified": verified}

    def buyer_identity(self, buyer_ref: str, role: str) -> dict:
        """实名信息仅 staff 可取。"""
        if role != "staff":
            raise ForbiddenError("实名资格信息仅主办方工作人员可查看", code="identity_restricted")
        profile = self.vault.get(buyer_ref)
        return {"buyer_ref": buyer_ref, "legal_name": profile.legal_name,
                "contact": profile.contact, "credentials": profile.credentials,
                "verified": profile.verified}

    # =========================================================
    # 需求（轮次冻结）
    # =========================================================
    def publish_requirement(self, requirement_id: str, buyer_ref: str, data: dict) -> dict:
        with self.store.lock:
            self.vault.get(buyer_ref)
            req = self.catalog.publish_requirement(requirement_id, buyer_ref, data)
            self.store.append("requirement.published", {
                "requirement_id": requirement_id, "buyer_ref": buyer_ref,
                "current_round_no": 1,
                "round": self._round_dict(req.current),
            })
            return self.requirement_view(requirement_id)

    def revise_requirement(self, requirement_id: str, patch: dict) -> dict:
        with self.store.lock:
            req, target, branched = self.catalog.revise_requirement(requirement_id, patch)
            self.store.append("requirement.revised", {
                "requirement_id": requirement_id, "branched": branched,
                "round": self._round_dict(target),
            })
            return self.requirement_view(requirement_id)

    def freeze_requirement(self, requirement_id: str, round_no: Optional[int] = None) -> dict:
        with self.store.lock:
            target = self.catalog.freeze_round(requirement_id, round_no)
            self.store.append("requirement.frozen", {
                "requirement_id": requirement_id, "round_no": target.round_no,
                "at": target.frozen_at,
            })
            return {"requirement_id": requirement_id, "round_no": target.round_no,
                    "frozen": True, "frozen_at": target.frozen_at}

    def requirement_view(self, requirement_id: str) -> dict:
        """公开视图：只有匿名代号，不含实名。"""
        req = self.catalog.get_requirement(requirement_id)
        return {
            "requirement_id": req.requirement_id,
            "buyer_ref": req.buyer_ref,
            "buyer_verified": self.vault.is_verified(req.buyer_ref),
            "current_round_no": req.current_round_no,
            "rounds": [self._round_dict(r) for r in sorted(req.rounds.values(),
                                                           key=lambda x: x.round_no)],
        }

    @staticmethod
    def _round_dict(r) -> dict:
        return {"round_no": r.round_no, "revision": r.revision, "frozen": r.frozen,
                "opened_at": r.opened_at, "frozen_at": r.frozen_at, "data": r.snapshot()}

    # =========================================================
    # 供应商
    # =========================================================
    def register_supplier(self, supplier_id: str, data: dict) -> dict:
        with self.store.lock:
            conflicts = data.get("conflicts", [])
            record = self.catalog.register_supplier(supplier_id, data)
            record["conflicts"] = list(conflicts)
            self.store.append("supplier.registered", record)
            return record

    # =========================================================
    # 供应商响应（防重复 + 联合方案）
    # =========================================================
    def submit_response(self, requirement_id: str, lead_supplier_id: str, quote: dict,
                        proposal: Optional[dict] = None, members: Optional[list[str]] = None,
                        response_id: Optional[str] = None,
                        idempotency_key: Optional[str] = None) -> dict:
        with self.store.lock:
            if idempotency_key:
                cached = self.idempotency.get(("response.submit", idempotency_key))
                if cached is not None:
                    existing = self.responses[cached["response_id"]]
                    if existing.requirement_id == requirement_id and \
                            existing.lead_supplier_id == lead_supplier_id:
                        return self.response_view(existing, replayed=True)
                    raise ConflictError("幂等键对应不同的响应内容", code="idempotency_mismatch")

            req = self.catalog.get_requirement(requirement_id)
            round_no = req.current_round_no
            member_list = self._validate_members(lead_supplier_id, members)
            kind = "solo" if len(member_list) == 1 else "joint"

            self._assert_no_duplicate_response(requirement_id, round_no, set(member_list))

            amount = quote.get("amount")
            if not isinstance(amount, (int, float)) or amount <= 0:
                raise ValidationError("报价金额必须为正数", code="bad_quote")
            if not quote.get("currency"):
                raise ValidationError("报价币种不能为空", code="bad_quote_currency")

            rid = response_id or f"resp-{uuid.uuid4().hex[:12]}"
            if rid in self.responses:
                raise ConflictError(f"响应 {rid} 已存在", code="response_exists")
            rec = ResponseRec(
                response_id=rid, requirement_id=requirement_id, round_no=round_no,
                lead_supplier_id=lead_supplier_id, members=member_list, kind=kind,
                quote={"amount": amount, "currency": quote["currency"]},
                proposal=proposal or {}, submitted_at=self.clock())
            self.responses[rid] = rec
            self.store.append("response.submitted", rec.__dict__)
            if idempotency_key:
                self._record_idempotency("response.submit", idempotency_key,
                                         {"response_id": rid})
            return self.response_view(rec)

    def _validate_members(self, lead: str, members: Optional[list[str]]) -> list[str]:
        if not members:
            members = [lead]
        members = list(dict.fromkeys(members))  # 去重保序
        if lead not in members:
            raise ValidationError("牵头供应商必须是联合方案成员", code="lead_not_member")
        for sid in members:
            supplier = self.catalog.get_supplier(sid)
            if not supplier.get("active", True):
                raise ValidationError(f"供应商 {sid} 已停用，不能参与响应", code="member_inactive")
        if len(members) < 1 or any(not isinstance(m, str) for m in members):
            raise ValidationError("成员列表非法", code="bad_members")
        return members

    def _assert_no_duplicate_response(self, requirement_id: str, round_no: int,
                                      member_set: set[str]) -> None:
        for rec in self.responses.values():
            if rec.requirement_id != requirement_id or rec.round_no != round_no \
                    or rec.status != "active":
                continue
            if rec.lead_supplier_id in member_set:
                raise ConflictError(
                    f"供应商 {rec.lead_supplier_id} 已对本需求轮次提交响应",
                    code="duplicate_response")
            overlap = member_set & set(rec.members)
            if overlap:
                raise ConflictError(
                    f"供应商 {sorted(overlap)[0]} 已在另一联合方案中，不能重复参与",
                    code="duplicate_membership")

    def amend_response(self, response_id: str, quote: dict, proposal: Optional[dict] = None):
        with self.store.lock:
            rec = self._get_response(response_id)
            self._assert_live(rec)
            amount = quote.get("amount")
            if not isinstance(amount, (int, float)) or amount <= 0:
                raise ValidationError("报价金额必须为正数", code="bad_quote")
            rec.quote = {"amount": amount, "currency": quote.get("currency", rec.quote["currency"])}
            if proposal is not None:
                rec.proposal = proposal
            rec.revision += 1
            self.store.append("response.amended", {
                "response_id": response_id, "quote": rec.quote,
                "proposal": rec.proposal, "revision": rec.revision, "at": self.clock(),
            })
            return self.response_view(rec)

    def withdraw_response(self, response_id: str, actor: str = "supplier") -> dict:
        with self.store.lock:
            rec = self._get_response(response_id)
            self._assert_live(rec)
            # 已确认或洽谈中的承诺不能单方面退出
            for neg in self.negotiations.values():
                if neg.requirement_id == rec.requirement_id and neg.round_no == rec.round_no \
                        and neg.candidate_id == rec.candidate_id and neg.status in (
                        "open", "negotiating", "confirmed"):
                    raise ConflictError(
                        "该响应存在进行中或已确认的洽谈，不能退出；请先结束洽谈",
                        code="response_locked_by_negotiation")
            before_ranking = self._ranking_if_exists(rec.requirement_id, rec.round_no)
            rec.status = "withdrawn"
            self.store.append("response.withdrawn", {
                "response_id": response_id, "actor": actor, "at": self.clock()})
            # 只重算受影响的推荐关系
            affected = self._recompute_after_withdrawal(rec, before_ranking)
            return {"response_id": response_id, "status": "withdrawn",
                    "recommendations_recomputed": affected}

    def _ranking_if_exists(self, requirement_id: str, round_no: int) -> Optional[list[str]]:
        reco = self.recos.get((requirement_id, round_no))
        return self.effective_ranking(reco) if reco and reco.latest else None

    def _recompute_after_withdrawal(self, rec: ResponseRec,
                                    ranking_before: Optional[list[str]]) -> list[dict]:
        affected = []
        reco = self.recos.get((rec.requirement_id, rec.round_no))
        if reco is None or reco.latest is None:
            return affected
        base = reco.latest
        if rec.candidate_id not in base["assessments"]:
            return affected
        record = {
            "trigger": "candidate_withdrawn",
            "candidate_id": rec.candidate_id,
            "response_id": rec.response_id,
            "at": self.clock(),
            "ranking_before": ranking_before,
            # 其他候选的评分原样保留，仅移除退出方并重排队形
            "ranking_after": self.effective_ranking(reco),
        }
        self.store.append("reco.recomputed", {
            "requirement_id": rec.requirement_id, "round_no": rec.round_no, "record": record})
        reco.recomputations.append(record)
        affected.append({"requirement_id": rec.requirement_id, "round_no": rec.round_no,
                         "candidate_id": rec.candidate_id})
        return affected

    def response_view(self, rec: ResponseRec, replayed: bool = False) -> dict:
        view = {k: getattr(rec, k) for k in (
            "response_id", "requirement_id", "round_no", "lead_supplier_id", "members",
            "kind", "quote", "proposal", "status", "revision", "submitted_at")}
        view["candidate_id"] = rec.candidate_id
        if replayed:
            view["idempotent_replay"] = True
        return view

    def _get_response(self, response_id: str) -> ResponseRec:
        rec = self.responses.get(response_id)
        if rec is None:
            raise NotFoundError(f"响应 {response_id} 不存在", code="response_not_found")
        return rec

    @staticmethod
    def _assert_live(rec: ResponseRec) -> None:
        if rec.status != "active":
            raise ConflictError(f"响应 {rec.response_id} 已退出", code="response_withdrawn")

    # =========================================================
    # 推荐生成
    # =========================================================
    def generate_recommendations(self, requirement_id: str,
                                 round_no: Optional[int] = None) -> dict:
        with self.store.lock:
            req = self.catalog.get_requirement(requirement_id)
            target = req.round(round_no)
            if not target.frozen:
                self.catalog.freeze_round(requirement_id, target.round_no)
                self.store.append("requirement.frozen", {
                    "requirement_id": requirement_id, "round_no": target.round_no,
                    "at": self.clock()})

            snapshot = {**target.snapshot(), "buyer_ref": req.buyer_ref}
            budget = snapshot["budget"]
            assessments: dict[str, dict] = {}
            for rec in self._active_responses(requirement_id, target.round_no):
                virtual = self._virtual_supplier(rec)
                assessment = matcher.assess(snapshot, virtual)
                view = assessment.as_dict()
                view["candidate_id"] = rec.candidate_id
                view["response_id"] = rec.response_id
                view["members"] = list(rec.members)
                view["quote"] = rec.quote
                # 报价层级硬约束（不与任何竞争报价比较）
                if view["eligible"]:
                    if rec.quote["currency"] != budget.get("currency"):
                        view["eligible"] = False
                        view["reasons"].append({
                            "code": "quote_currency_mismatch",
                            "detail": f"报价币种 {rec.quote['currency']} 与预算币种 "
                                      f"{budget.get('currency')} 不一致"})
                    elif rec.quote["amount"] > budget["amount"]:
                        view["eligible"] = False
                        view["reasons"].append({
                            "code": "over_budget",
                            "detail": f"报价超出预算（预算上限 {budget['amount']} "
                                      f"{budget.get('currency')}）"})
                assessments[rec.candidate_id] = view

            ranking = sorted(
                (cid for cid, a in assessments.items() if a["eligible"]),
                key=lambda cid: (-assessments[cid]["score"], cid))
            generation = {
                "generation": (reco.generations[-1]["generation"] + 1)
                if (reco := self.recos.get((requirement_id, target.round_no))) and reco.generations
                else 1,
                "at": self.clock(),
                "assessments": assessments,
                "ranking": ranking,
                "budget": budget,
            }
            reco = self.recos.setdefault((requirement_id, target.round_no),
                                         RecoRound(requirement_id, target.round_no))
            reco.generations.append(generation)
            self.store.append("reco.generated", {
                "requirement_id": requirement_id, "round_no": target.round_no,
                "generation": generation})
            return self.recommendation_view(requirement_id, target.round_no)

    def _active_responses(self, requirement_id: str, round_no: int) -> list[ResponseRec]:
        return [r for r in self.responses.values()
                if r.requirement_id == requirement_id and r.round_no == round_no
                and r.status == "active"]

    def _virtual_supplier(self, rec: ResponseRec) -> dict:
        first = self.catalog.get_supplier(rec.members[0])
        if rec.kind == "solo":
            virtual = dict(first)
        else:
            records = [self.catalog.get_supplier(m) for m in rec.members]
            virtual = {
                "supplier_id": rec.candidate_id,
                "name": "联合方案:" + "+".join(rec.members),
                "origin_countries": sorted({c for s in records for c in s["origin_countries"]}),
                "categories": sorted({c for s in records for c in s["categories"]}),
                "languages": sorted({c for s in records for c in s["languages"]}),
                "capabilities": {
                    "tags": sorted({t for s in records
                                    for t in (s.get("capabilities") or {}).get("tags", [])}),
                    "delivery_windows": [w for s in records
                                         for w in (s.get("capabilities") or {})
                                         .get("delivery_windows", [])],
                },
                "active": all(s.get("active", True) for s in records),
                "conflicts": sorted({c for s in records for c in s.get("conflicts", [])}),
            }
        return virtual

    def effective_ranking(self, reco: RecoRound) -> list[str]:
        """根据最新一代评估 + 不可篡改的人工调整计算当前名次。"""
        base = reco.latest
        if base is None:
            return []
        active_ids = {r.candidate_id for r in
                      self._active_responses(reco.requirement_id, reco.round_no)}
        pinned: dict[str, int] = {}
        excluded: set[str] = set()
        force_included: set[str] = set()
        for ov in reco.overrides:
            cid = ov["candidate_id"]
            if ov["action"] == "exclude":
                excluded.add(cid)
                force_included.discard(cid)
            elif ov["action"] == "include":
                force_included.add(cid)
                excluded.discard(cid)
            elif ov["action"] == "adjust_rank":
                pinned[cid] = ov["position"]

        def eligible_now(cid: str) -> bool:
            if cid not in active_ids or cid in excluded:
                return False
            return base["assessments"][cid]["eligible"] or cid in force_included

        candidates = [cid for cid in base["assessments"] if eligible_now(cid)]
        pinned_items = sorted((c for c in candidates if c in pinned),
                              key=lambda c: pinned[c])
        rest = sorted((c for c in candidates if c not in set(pinned_items)),
                      key=lambda cid: (-base["assessments"][cid]["score"], cid))

        # 先放自然名次，再按目标位置依次插入人工置顶项（按位置升序插入）
        ordered: list[str] = list(rest)
        for c in pinned_items:
            ordered.insert(min(pinned[c] - 1, len(ordered)), c)
        return ordered

    def recommendation_view(self, requirement_id: str, round_no: int,
                            include_quotes: bool = True) -> dict:
        reco = self._get_reco(requirement_id, round_no)
        base = reco.latest
        order = self.effective_ranking(reco)
        ranked = []
        for rank, cid in enumerate(order, start=1):
            a = dict(base["assessments"][cid])
            a["rank"] = rank
            if not include_quotes:
                a.pop("quote", None)
            ranked.append(a)
        rejected = []
        ranked_ids = set(order)
        active_ids = {r.candidate_id for r in
                      self._active_responses(requirement_id, round_no)}
        for cid, a in base["assessments"].items():
            if cid in ranked_ids:
                continue
            entry = dict(a)
            if not include_quotes:
                entry.pop("quote", None)
            if cid not in active_ids and entry["eligible"]:
                entry["eligible"] = False
                entry["reasons"] = list(entry["reasons"]) + [{
                    "code": "candidate_withdrawn",
                    "detail": "候选已退出，本轮推荐关系已做增量重算"}]
            manual = next((o for o in reco.overrides
                           if o["candidate_id"] == cid and o["action"] == "exclude"), None)
            if manual and entry["eligible"]:
                entry["eligible"] = False
                entry["reasons"] = list(entry["reasons"]) + [{
                    "code": "manually_excluded",
                    "detail": f"被人工调整排除：{manual['reason']}"}]
            rejected.append(entry)
        return {
            "requirement_id": requirement_id, "round_no": round_no,
            "generation": base["generation"], "generated_at": base["at"],
            "locked": reco.locked,
            "ranked": ranked,
            "rejected": sorted(rejected, key=lambda a: a["candidate_id"]),
            "manual_overrides": list(reco.overrides),
            "recomputations": list(reco.recomputations),
        }

    def _get_reco(self, requirement_id: str, round_no: int) -> RecoRound:
        req = self.catalog.get_requirement(requirement_id)
        req.round(round_no)
        reco = self.recos.get((requirement_id, round_no))
        if reco is None or reco.latest is None:
            raise NotFoundError(
                f"需求 {requirement_id} 第 {round_no} 轮尚未生成推荐",
                code="recommendation_not_generated")
        return reco

    # =========================================================
    # 人工调整（必须留痕）
    # =========================================================
    def apply_override(self, requirement_id: str, round_no: int, action: str,
                       candidate_id: str, reason: str, actor: str,
                       position: Optional[int] = None) -> dict:
        if not actor:
            raise ValidationError("人工调整必须记录操作人", code="actor_required")
        if not reason or not reason.strip():
            raise ValidationError("人工调整必须填写原因", code="override_reason_required")
        if action not in ("include", "exclude", "adjust_rank"):
            raise ValidationError(f"不支持的调整动作 {action}", code="bad_override_action")
        with self.store.lock:
            reco = self._get_reco(requirement_id, round_no)
            if reco.locked:
                raise ConflictError("该轮已产生确认承诺，推荐已锁定，不能再人工调整",
                                    code="recommendation_locked")
            base = reco.latest
            if candidate_id not in base["assessments"]:
                raise NotFoundError(f"候选 {candidate_id} 不在本轮推荐范围内",
                                    code="candidate_not_found")
            if action == "adjust_rank":
                if not isinstance(position, int) or position < 1:
                    raise ValidationError("调整名次需要 >=1 的 position", code="bad_position")
            ranking_before = self.effective_ranking(reco)
            override = {
                "id": f"ov-{uuid.uuid4().hex[:10]}",
                "at": self.clock(), "actor": actor, "action": action,
                "candidate_id": candidate_id, "reason": reason.strip(),
                "position": position, "ranking_before": ranking_before,
            }
            reco.overrides.append(override)
            self.store.append("reco.overridden", {
                "requirement_id": requirement_id, "round_no": round_no,
                "override": override})
            override = dict(override)
            override["ranking_after"] = self.effective_ranking(reco)
            return override

    # =========================================================
    # 洽谈与预算承诺
    # =========================================================
    def _budget_ledger(self, requirement_id: str, round_no: int) -> dict:
        req = self.catalog.get_requirement(requirement_id)
        budget = req.round(round_no).data["budget"]
        held = sum(self.holds.get((requirement_id, round_no), {}).values())
        committed = sum(self.committed.get((requirement_id, round_no), {}).values())
        return {"currency": budget["currency"], "budget": budget["amount"],
                "held": round(held, 2), "committed": round(committed, 2),
                "available": round(budget["amount"] - held - committed, 2)}

    def open_negotiation(self, requirement_id: str, candidate_id: str,
                         round_no: Optional[int] = None,
                         hold_amount: Optional[float] = None,
                         actor: str = "buyer") -> dict:
        with self.store.lock:
            req = self.catalog.get_requirement(requirement_id)
            target = req.round(round_no)
            reco = self._get_reco(requirement_id, target.round_no)
            ranking = self.effective_ranking(reco)
            if candidate_id not in ranking:
                raise ConflictError("候选不在当前有效推荐名单中，不能开启洽谈",
                                    code="candidate_not_ranked")
            response = self._response_for_candidate(requirement_id, target.round_no, candidate_id)
            for existing in self.negotiations.values():
                if existing.requirement_id == requirement_id and existing.round_no == target.round_no \
                        and existing.candidate_id == candidate_id \
                        and existing.status in ("open", "negotiating", "confirmed"):
                    raise ConflictError("该候选已存在进行中或已确认的洽谈，不能重复开启",
                                        code="negotiation_exists")
            ledger = self._budget_ledger(requirement_id, target.round_no)
            amount = hold_amount if hold_amount is not None else response.quote["amount"]
            if amount <= 0:
                raise ValidationError("预留金额必须为正数", code="bad_hold_amount")
            if amount > ledger["available"]:
                raise BudgetExhaustedError(
                    f"预算不足：可用 {ledger['available']} {ledger['currency']}，"
                    f"本次需预留 {amount}")
            neg = Negotiation(
                negotiation_id=f"neg-{uuid.uuid4().hex[:12]}",
                requirement_id=requirement_id, round_no=target.round_no,
                candidate_id=candidate_id, kind=response.kind, members=list(response.members),
                hold_amount=amount, currency=response.quote["currency"],
                opened_at=self.clock())
            entry = {"at": neg.opened_at, "actor": actor, "action": "opened",
                     "amount": amount, "note": "开启洽谈并预留预算"}
            neg.history.append(entry)
            self.negotiations[neg.negotiation_id] = neg
            self.holds.setdefault((requirement_id, target.round_no), {})[neg.negotiation_id] = amount
            self.store.append("negotiation.opened", {
                "negotiation_id": neg.negotiation_id, "requirement_id": requirement_id,
                "round_no": target.round_no, "candidate_id": candidate_id,
                "kind": response.kind, "members": response.members,
                "hold_amount": amount, "currency": neg.currency,
                "at": neg.opened_at, "entry": entry})
            return self.negotiation_view(neg.negotiation_id)

    def counter_negotiation(self, negotiation_id: str, amount: float, note: str = "",
                            actor: str = "buyer") -> dict:
        with self.store.lock:
            neg = self._get_negotiation(negotiation_id)
            if neg.status not in ("open", "negotiating"):
                raise ConflictError(f"洽谈已处于 {neg.status} 状态", code="negotiation_not_active")
            if amount <= 0:
                raise ValidationError("报价必须为正数", code="bad_counter_amount")
            entry = {"at": self.clock(), "actor": actor, "action": "countered",
                     "amount": amount, "note": note}
            neg.status = "negotiating"
            neg.history.append(entry)
            self.store.append("negotiation.countered",
                              {"negotiation_id": negotiation_id, "entry": entry})
            return self.negotiation_view(negotiation_id)

    def confirm_negotiation(self, negotiation_id: str, amount: Optional[float] = None,
                            actor: str = "buyer",
                            idempotency_key: Optional[str] = None) -> dict:
        """并发安全的预算承诺：全程持锁，幂等键重放不二次承诺。"""
        with self.store.lock:
            if idempotency_key:
                cached = self.idempotency.get(("negotiation.confirm", idempotency_key))
                if cached is not None:
                    view = self.negotiation_view(cached["negotiation_id"])
                    view["idempotent_replay"] = True
                    return view

            neg = self._get_negotiation(negotiation_id)
            if neg.status == "confirmed":
                raise ConflictError("该洽谈已确认，不能重复承诺", code="already_confirmed")
            if neg.status not in ("open", "negotiating"):
                raise ConflictError(f"洽谈已处于 {neg.status} 状态，不能确认",
                                    code="negotiation_not_active")
            final_amount = amount if amount is not None else neg.hold_amount
            if final_amount <= 0:
                raise ValidationError("确认金额必须为正数", code="bad_confirm_amount")

            ledger_key = (neg.requirement_id, neg.round_no)
            held_others = sum(v for k, v in self.holds.get(ledger_key, {}).items()
                              if k != neg.negotiation_id)
            committed = sum(self.committed.get(ledger_key, {}).values())
            ledger = self._budget_ledger(*ledger_key)
            if committed + held_others + final_amount > ledger["budget"]:
                raise BudgetExhaustedError(
                    f"预算不足：预算 {ledger['budget']}，已承诺 {committed}，"
                    f"其他洽谈预留 {held_others}，本次 {final_amount}")

            entry = {"at": self.clock(), "actor": actor, "action": "confirmed",
                     "amount": final_amount, "note": "确认承诺，预算由预留转为承诺"}
            neg.status = "confirmed"
            neg.committed_amount = final_amount
            neg.hold_amount = 0.0
            neg.history.append(entry)
            self.holds.get(ledger_key, {}).pop(neg.negotiation_id, None)
            self.committed.setdefault(ledger_key, {})[neg.negotiation_id] = final_amount
            # 预算一旦承诺，本轮推荐锁定：不允许再人工调整或二次承诺
            self.recos[ledger_key].locked = True
            self.store.append("negotiation.confirmed", {
                "negotiation_id": negotiation_id, "amount": final_amount,
                "entry": entry})
            result = self.negotiation_view(negotiation_id)
            if idempotency_key:
                self._record_idempotency("negotiation.confirm", idempotency_key,
                                         {"negotiation_id": negotiation_id})
            return result

    def close_negotiation(self, negotiation_id: str, *, declined: bool = False,
                          actor: str = "buyer", note: str = "") -> dict:
        with self.store.lock:
            neg = self._get_negotiation(negotiation_id)
            if neg.status in ("confirmed", "declined", "closed"):
                raise ConflictError(f"洽谈已处于 {neg.status} 状态", code="negotiation_final")
            released = neg.hold_amount
            neg.status = "declined" if declined else "closed"
            neg.hold_amount = 0.0
            entry = {"at": self.clock(), "actor": actor,
                     "action": "declined" if declined else "closed",
                     "released_hold": released, "note": note}
            neg.history.append(entry)
            self.holds.get((neg.requirement_id, neg.round_no), {}).pop(
                neg.negotiation_id, None)
            self.store.append(
                "negotiation.declined" if declined else "negotiation.closed",
                {"negotiation_id": negotiation_id, "entry": entry})
            return self.negotiation_view(negotiation_id)

    def negotiation_view(self, negotiation_id: str) -> dict:
        neg = self._get_negotiation(negotiation_id)
        ledger = self._budget_ledger(neg.requirement_id, neg.round_no)
        return {
            "negotiation_id": neg.negotiation_id,
            "requirement_id": neg.requirement_id, "round_no": neg.round_no,
            "candidate_id": neg.candidate_id, "kind": neg.kind, "members": neg.members,
            "status": neg.status, "hold_amount": neg.hold_amount,
            "committed_amount": neg.committed_amount, "currency": neg.currency,
            "opened_at": neg.opened_at, "history": list(neg.history),
            "budget": ledger,
        }

    def _response_for_candidate(self, requirement_id: str, round_no: int,
                                candidate_id: str) -> ResponseRec:
        for rec in self._active_responses(requirement_id, round_no):
            if rec.candidate_id == candidate_id:
                return rec
        raise NotFoundError(f"候选 {candidate_id} 无有效响应", code="candidate_response_missing")

    def _get_negotiation(self, negotiation_id: str) -> Negotiation:
        neg = self.negotiations.get(negotiation_id)
        if neg is None:
            raise NotFoundError(f"洽谈 {negotiation_id} 不存在", code="negotiation_not_found")
        return neg

    def _record_idempotency(self, scope: str, key: str, result: dict) -> None:
        self.idempotency[(scope, key)] = result
        self.store.append("idempotency.recorded",
                          {"scope": scope, "key": key, "result": result})

    # =========================================================
    # 脱敏解释
    # =========================================================
    def explain_for_supplier(self, response_id: str) -> dict:
        """供应方视角：只能看到自己的评估、评分与原因，不含任何竞争方信息。"""
        with self.store.lock:
            rec = self._get_response(response_id)
            reco = self._get_reco(rec.requirement_id, rec.round_no)
            base = reco.latest
            own = base["assessments"].get(rec.candidate_id)
            if own is None:
                raise NotFoundError("该响应未参与本轮推荐（可能已退出或推荐早于提交）",
                                    code="not_assessed")
            order = self.effective_ranking(reco)
            view = {
                "requirement_id": rec.requirement_id, "round_no": rec.round_no,
                "generation": base["generation"],
                "candidate_id": rec.candidate_id, "your_quote": rec.quote,
                "eligible": own["eligible"], "your_score": own["score"],
                "score_breakdown": own["score_breakdown"],
                "reasons": own["reasons"],
                "your_rank": order.index(rec.candidate_id) + 1
                if rec.candidate_id in order else None,
                "ranked_count": len(order),
            }
            # 预算口径只透露上限与币种，不透露任何竞争报价
            view["budget_ceiling"] = {
                "amount": base["budget"]["amount"],
                "currency": base["budget"]["currency"]}
            return view

    def explain_decision(self, requirement_id: str, round_no: int,
                         candidate_id: str, role: str) -> dict:
        """工作人员/采购方视角的决策说明；不输出其他候选报价给无关方。"""
        if role not in ("staff", "buyer"):
            raise ForbiddenError("仅工作人员或采购方可查看完整决策说明",
                                 code="explain_restricted")
        with self.store.lock:
            reco = self._get_reco(requirement_id, round_no)
            base = reco.latest
            if candidate_id not in base["assessments"]:
                raise NotFoundError("候选不在本轮评估范围内", code="candidate_not_found")
            a = base["assessments"][candidate_id]
            order = self.effective_ranking(reco)
            manual = [o for o in reco.overrides if o["candidate_id"] == candidate_id]
            return {
                "requirement_id": requirement_id, "round_no": round_no,
                "generation": base["generation"], "candidate_id": candidate_id,
                "eligible": a["eligible"], "score": a["score"],
                "score_breakdown": a["score_breakdown"], "reasons": a["reasons"],
                "rank": order.index(candidate_id) + 1 if candidate_id in order else None,
                "manual_overrides": manual,
                "decision": "ranked" if candidate_id in order else "rejected",
                # 仅返回该候选自身报价；排名列表里其他方的报价不在本说明内
                "quote": a.get("quote"),
            }
