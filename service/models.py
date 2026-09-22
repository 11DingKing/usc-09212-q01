"""领域输入解析与约定常量。

业务身份均为调用方提供的稳定标识（字符串）。
金额统一为 {"amount": 数字, "currency": 三字母币种}；日期为 YYYY-MM-DD。
"""
from datetime import date

REQUIRED_COUNTRIES = "required"  # 白名单语义：仅允许列出的国家
FORBIDDEN_COUNTRIES = "forbidden"  # 黑名单语义：禁止列出的国家


def parse_money(payload, key, errors):
    raw = payload.get(key)
    if not isinstance(raw, dict):
        errors.append(f"{key} 必须是金额对象 {{'amount','currency'}}")
        return None
    amount = raw.get("amount")
    currency = raw.get("currency")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount <= 0:
        errors.append(f"{key}.amount 必须是正数")
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        errors.append(f"{key}.currency 必须是三字母币种代码")
        return None
    return {"amount": amount, "currency": currency.strip().upper()}


def parse_date(payload, key, errors, required=False):
    value = payload.get(key)
    if value in (None, ""):
        if required:
            errors.append(f"{key} 必填，格式 YYYY-MM-DD")
        return None
    if not isinstance(value, str):
        errors.append(f"{key} 必须是 YYYY-MM-DD 字符串")
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        errors.append(f"{key} 不是合法日期 (YYYY-MM-DD)")
        return None
    return value


def parse_country_rule(payload, errors):
    """解析国家准入规则，缺省表示不限制。"""
    rule = payload.get("country_rule")
    if rule in (None, {}):
        return {"policy": "open", "countries": []}
    if not isinstance(rule, dict):
        errors.append("country_rule 必须是对象")
        return None
    policy = rule.get("policy")
    countries = rule.get("countries", [])
    if policy not in (REQUIRED_COUNTRIES, FORBIDDEN_COUNTRIES):
        errors.append("country_rule.policy 必须是 required 或 forbidden")
    if not isinstance(countries, list) or not all(isinstance(c, str) and c.strip() for c in countries):
        errors.append("country_rule.countries 必须是非空字符串列表")
        return None
    return {"policy": policy, "countries": [c.strip().upper() for c in countries]}
