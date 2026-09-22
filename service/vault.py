"""采购方实名资格与公开需求分离保存。

公开的需求/撮合状态只持有匿名化的采购方代号（buyer_ref）；
真实名称、证照、联系方式等实名信息由独立保险库保管，
默认不向供应商侧接口返回，需具备 staff 权限才能查看。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .errors import NotFoundError, ValidationError


@dataclass
class BuyerProfile:
    buyer_ref: str
    legal_name: str
    contact: str
    credentials: dict
    verified: bool = False


class IdentityVault:
    """实名资格库，逻辑上与需求存储分离（独立事件类型）。"""

    def __init__(self):
        self._profiles: dict[str, BuyerProfile] = {}

    def register(self, buyer_ref: str, legal_name: str, contact: str,
                 credentials: Optional[dict] = None, verified: bool = False) -> BuyerProfile:
        if not legal_name or not str(legal_name).strip():
            raise ValidationError("采购方实名名称不能为空", code="legal_name_required")
        if buyer_ref in self._profiles:
            raise ValidationError(f"采购方 {buyer_ref} 已完成实名登记", code="buyer_registered")
        profile = BuyerProfile(
            buyer_ref=buyer_ref,
            legal_name=legal_name.strip(),
            contact=contact,
            credentials=credentials or {},
            verified=verified,
        )
        self._profiles[buyer_ref] = profile
        return profile

    def apply_event(self, event_type: str, payload: dict) -> None:
        if event_type == "buyer.registered":
            self._profiles[payload["buyer_ref"]] = BuyerProfile(
                buyer_ref=payload["buyer_ref"],
                legal_name=payload["legal_name"],
                contact=payload["contact"],
                credentials=payload.get("credentials", {}),
                verified=payload.get("verified", False),
            )
        elif event_type == "buyer.verified":
            ref = payload["buyer_ref"]
            if ref in self._profiles:
                self._profiles[ref].verified = payload["verified"]

    def get(self, buyer_ref: str) -> BuyerProfile:
        profile = self._profiles.get(buyer_ref)
        if profile is None:
            raise NotFoundError(f"采购方 {buyer_ref} 不存在", code="buyer_not_found")
        return profile

    def is_verified(self, buyer_ref: str) -> bool:
        profile = self._profiles.get(buyer_ref)
        return bool(profile and profile.verified)

    def public_view(self, buyer_ref: str) -> dict:
        """供应商侧可见：仅代号与是否已认证，不含任何实名信息。"""
        profile = self.get(buyer_ref)
        return {"buyer_ref": buyer_ref, "verified": profile.verified}
