"""可解释匹配引擎。

先过硬约束（国家规则、行业、交付窗口、语言、利益冲突），
硬约束未通过直接淘汰并给出机器可读原因；
通过后按可解释的分项加权评分（满分 100）。
评估过程不读取竞争报价，评分项也不包含其他供应商价格，
因此向任一方解释时不会泄露竞争报价。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# (原因码, 人读说明) —— 人读说明不含任何竞争方信息
HARD_RULES = []


def _overlaps(a: Optional[dict], b: Optional[dict]) -> bool:
    if not a or not b:
        return True  # 任一方未声明窗口视为不受限
    a_start, a_end = a.get("start"), a.get("end")
    b_start, b_end = b.get("start"), b.get("end")
    if a_start and b_end and a_start > b_end:
        return False
    if b_start and a_end and b_start > a_end:
        return False
    return True


@dataclass
class Assessment:
    supplier_id: str
    eligible: bool = False
    reasons: list[dict] = field(default_factory=list)   # 淘汰/限制原因
    score: float = 0.0
    score_breakdown: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "supplier_id": self.supplier_id,
            "eligible": self.eligible,
            "score": round(self.score, 2),
            "reasons": self.reasons,
            "score_breakdown": self.score_breakdown,
        }


def assess(requirement_round: dict, supplier: dict) -> Assessment:
    """对单个供应商评估硬约束与评分。不依赖其他供应商数据。"""
    result = Assessment(supplier_id=supplier["supplier_id"])
    reasons: list[dict] = []

    # 1) 行业限制
    categories = supplier.get("categories", [])
    if requirement_round["category"] not in categories:
        reasons.append({
            "code": "category_mismatch",
            "detail": f"供应商不覆盖需求行业 {requirement_round['category']}",
        })

    # 2) 国家规则：原产地白名单
    allowed = requirement_round.get("allowed_origin_countries") or []
    origins = supplier.get("origin_countries", [])
    if allowed and not (set(allowed) & set(origins)):
        reasons.append({
            "code": "origin_not_allowed",
            "detail": f"供应商原产地不在准入名单 {', '.join(allowed)} 内",
        })

    # 3) 利益冲突：主办方标记的排除名单 + 供应商自报冲突
    if supplier["supplier_id"] in (requirement_round.get("excluded_supplier_ids") or []):
        reasons.append({
            "code": "conflict_of_interest_excluded",
            "detail": "该供应商在本需求的利益冲突排除名单内",
        })
    if requirement_round.get("buyer_ref") in (supplier.get("conflicts") or []):
        reasons.append({
            "code": "conflict_of_interest_declared",
            "detail": "供应商就该采购方申报了利益冲突",
        })

    # 4) 交付窗口
    supplier_windows = (supplier.get("capabilities") or {}).get("delivery_windows") or []
    req_window = requirement_round.get("delivery_window")
    if req_window and supplier_windows:
        if not any(_overlaps(req_window, w) for w in supplier_windows):
            reasons.append({
                "code": "delivery_window_unavailable",
                "detail": "供应商可交付窗口与需求交付窗口无交集",
            })

    # 5) 语言
    req_langs = set(requirement_round.get("languages") or [])
    sup_langs = set(supplier.get("languages") or [])
    shared_langs = req_langs & sup_langs
    if req_langs and not shared_langs:
        reasons.append({
            "code": "language_unsupported",
            "detail": f"供应商不支持需求要求的语言 {', '.join(sorted(req_langs))}",
        })

    # 6) 供应商能力（需求声明的硬性能力标签）
    required_caps = requirement_round.get("required_capabilities") or []
    supplier_caps = (supplier.get("capabilities") or {}).get("tags") or []
    missing_caps = sorted(set(required_caps) - set(supplier_caps))
    if missing_caps:
        reasons.append({
            "code": "capability_missing",
            "detail": f"缺少必备能力 {', '.join(missing_caps)}",
        })

    if not supplier.get("active", True):
        reasons.append({"code": "supplier_inactive", "detail": "供应商处于停用状态"})

    result.reasons = reasons
    if reasons:
        result.eligible = False
        result.score = 0.0
        return result

    result.eligible = True
    breakdown = _score(requirement_round, supplier, shared_langs, supplier_windows)
    result.score_breakdown = breakdown
    result.score = round(sum(item["points"] for item in breakdown), 2)
    return result


def _score(requirement_round, supplier, shared_langs, supplier_windows) -> list[dict]:
    """分项评分，权重固定、可逐项解释，价格不参与与他方比较。"""
    breakdown: list[dict] = []

    # 行业匹配深度（40）：主类别满分，相关能力标签按比例加分
    caps_tags = set((supplier.get("capabilities") or {}).get("tags") or [])
    required = set(requirement_round.get("required_capabilities") or [])
    if required:
        depth = len(required & caps_tags) / len(required)
        points = 20 + 20 * depth
    else:
        points = 30  # 无额外能力要求时给基础分
    breakdown.append({"item": "industry_fit", "weight": 40, "points": round(points, 2),
                      "detail": f"覆盖必备能力 {len(required & caps_tags)}/{len(required)}"})

    # 国家规则契合（20）：原产地命中准入名单的数量
    allowed = set(requirement_round.get("allowed_origin_countries") or [])
    origins = set(supplier.get("origin_countries") or [])
    if allowed:
        hit = len(allowed & origins) / len(allowed)
        points = 20 * min(1.0, hit + 0.4)  # 命中任一即有基础分
    else:
        points = 14  # 无准入限制
    breakdown.append({"item": "country_rule_fit", "weight": 20, "points": round(points, 2),
                      "detail": f"准入原产地命中 {sorted(allowed & origins)}"})

    # 语言（20）：共享语言覆盖率
    req_langs = set(requirement_round.get("languages") or [])
    if req_langs:
        points = 20 * (len(shared_langs) / len(req_langs))
        detail = f"共同语言 {sorted(shared_langs)}"
    else:
        points = 12
        detail = "需求未指定语言要求"
    breakdown.append({"item": "language_fit", "weight": 20, "points": round(points, 2),
                      "detail": detail})

    # 交付窗口裕度（20）：可交付窗口数越多越稳妥
    req_window = requirement_round.get("delivery_window")
    if req_window and supplier_windows:
        usable = sum(1 for w in supplier_windows if _overlaps(req_window, w))
        points = min(20.0, 10.0 + 5.0 * usable)
        detail = f"{usable} 个可交付窗口与需求窗口相交"
    else:
        points = 12
        detail = "未声明窗口限制，默认可达"
    breakdown.append({"item": "delivery_window_fit", "weight": 20, "points": round(points, 2),
                      "detail": detail})

    return breakdown
