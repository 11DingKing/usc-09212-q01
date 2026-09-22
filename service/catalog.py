"""需求目录与供应商注册。

需求修订按轮次（round）冻结：
- 开放轮次内可以继续修订（就地更新，revision 递增）；
- 轮次冻结后再修订，会开启新的一轮，历史轮次不可变；
- 撮合与推荐只基于已冻结（或显式指定）的轮次快照。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .errors import ConflictError, NotFoundError, ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_window(window: Optional[dict]) -> Optional[dict]:
    if not window:
        return None
    start = window.get("start")
    end = window.get("end")
    if start and end and end < start:
        raise ValidationError("交付窗口结束时间早于开始时间", code="bad_delivery_window")
    return {"start": start, "end": end}


@dataclass
class Round:
    round_no: int
    data: dict
    revision: int = 1
    frozen: bool = False
    opened_at: str = field(default_factory=_now)
    frozen_at: Optional[str] = None

    def snapshot(self) -> dict:
        snap = dict(self.data)
        snap["delivery_window"] = _norm_window(self.data.get("delivery_window"))
        return snap


@dataclass
class Requirement:
    requirement_id: str
    buyer_ref: str
    rounds: dict[int, Round] = field(default_factory=dict)
    current_round_no: int = 0

    @property
    def current(self) -> Round:
        return self.rounds[self.current_round_no]

    def round(self, round_no: Optional[int] = None) -> Round:
        no = round_no if round_no is not None else self.current_round_no
        if no not in self.rounds:
            raise NotFoundError(
                f"需求 {self.requirement_id} 不存在第 {no} 轮", code="round_not_found")
        return self.rounds[no]


REQUIRED_FIELDS = ("title", "country", "category", "budget", "delivery_window")


def _validate_data(data: dict) -> dict:
    for key in REQUIRED_FIELDS:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValidationError(f"需求字段 {key} 不能为空", code=f"missing_{key}")
    budget = data["budget"]
    if not isinstance(budget, dict) or not isinstance(budget.get("amount"), (int, float)) \
            or budget["amount"] <= 0:
        raise ValidationError("预算必须为正数金额对象 {amount, currency}", code="bad_budget")
    clean = dict(data)
    clean["delivery_window"] = _norm_window(data["delivery_window"])
    clean.setdefault("languages", [])
    clean.setdefault("allowed_origin_countries", [])
    clean.setdefault("excluded_supplier_ids", [])
    return clean


class Catalog:
    def __init__(self, clock=_now):
        self._requirements: dict[str, Requirement] = {}
        self._suppliers: dict[str, dict] = {}
        self._clock = clock

    # ---------- 需求 ----------
    def publish_requirement(self, requirement_id: str, buyer_ref: str, data: dict):
        if requirement_id in self._requirements:
            raise ConflictError(f"需求 {requirement_id} 已存在", code="requirement_exists")
        clean = _validate_data(data)
        req = Requirement(requirement_id=requirement_id, buyer_ref=buyer_ref)
        req.rounds[1] = Round(round_no=1, data=clean)
        req.current_round_no = 1
        self._requirements[requirement_id] = req
        return req

    def revise_requirement(self, requirement_id: str, patch: dict,
                           round_no: Optional[int] = None):
        """修订需求。冻结轮次触发开新轮；开放轮次就地递增 revision。"""
        req = self.get_requirement(requirement_id)
        target = req.current
        branched = False
        if round_no is not None and round_no != target.round_no:
            raise ConflictError(
                f"只能修订当前轮（第 {target.round_no} 轮）", code="not_current_round")
        merged = dict(target.data)
        merged.update(patch)
        clean = _validate_data(merged)
        if target.frozen:
            new_no = target.round_no + 1
            target = Round(round_no=new_no, data=clean, revision=1)
            req.rounds[new_no] = target
            req.current_round_no = new_no
            branched = True
        else:
            target.data = clean
            target.revision += 1
        return req, target, branched

    def freeze_round(self, requirement_id: str, round_no: Optional[int] = None) -> Round:
        req = self.get_requirement(requirement_id)
        target = req.round(round_no)
        if not target.frozen:
            target.frozen = True
            target.frozen_at = self._clock()
        return target

    def get_requirement(self, requirement_id: str) -> Requirement:
        req = self._requirements.get(requirement_id)
        if req is None:
            raise NotFoundError(f"需求 {requirement_id} 不存在", code="requirement_not_found")
        return req

    def requirements_for_buyer(self, buyer_ref: str) -> list[Requirement]:
        return [r for r in self._requirements.values() if r.buyer_ref == buyer_ref]

    # ---------- 供应商 ----------
    def register_supplier(self, supplier_id: str, data: dict) -> dict:
        if supplier_id in self._suppliers:
            raise ConflictError(f"供应商 {supplier_id} 已注册", code="supplier_exists")
        name = (data.get("name") or "").strip()
        if not name:
            raise ValidationError("供应商名称不能为空", code="supplier_name_required")
        record = {
            "supplier_id": supplier_id,
            "name": name,
            "origin_countries": list(data.get("origin_countries", [])),
            "categories": list(data.get("categories", [])),
            "languages": list(data.get("languages", [])),
            "capabilities": dict(data.get("capabilities", {})),
            "active": bool(data.get("active", True)),
        }
        self._suppliers[supplier_id] = record
        return record

    def get_supplier(self, supplier_id: str) -> dict:
        supplier = self._suppliers.get(supplier_id)
        if supplier is None:
            raise NotFoundError(f"供应商 {supplier_id} 不存在", code="supplier_not_found")
        return supplier

    def list_suppliers(self) -> list[dict]:
        return list(self._suppliers.values())

    # ---------- 事件回放 ----------
    def apply_event(self, event_type: str, payload: dict) -> None:
        if event_type == "requirement.published":
            req = Requirement(requirement_id=payload["requirement_id"],
                              buyer_ref=payload["buyer_ref"])
            r = payload["round"]
            req.rounds[r["round_no"]] = Round(
                round_no=r["round_no"], data=r["data"], revision=r["revision"],
                frozen=r["frozen"], opened_at=r["opened_at"], frozen_at=r.get("frozen_at"))
            req.current_round_no = payload["current_round_no"]
            self._requirements[req.requirement_id] = req
        elif event_type == "requirement.revised":
            req = self.get_requirement(payload["requirement_id"])
            if payload["branched"]:
                r = payload["round"]
                req.rounds[r["round_no"]] = Round(
                    round_no=r["round_no"], data=r["data"], revision=r["revision"],
                    frozen=False, opened_at=r["opened_at"])
                req.current_round_no = r["round_no"]
            else:
                target = req.current
                target.data = payload["round"]["data"]
                target.revision = payload["round"]["revision"]
        elif event_type == "requirement.frozen":
            req = self.get_requirement(payload["requirement_id"])
            target = req.round(payload["round_no"])
            target.frozen = True
            target.frozen_at = payload.get("at")
        elif event_type == "supplier.registered":
            self._suppliers[payload["supplier_id"]] = payload
