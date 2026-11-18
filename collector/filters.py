"""config.yaml의 카테고리별 필터 적용."""
from __future__ import annotations


def matches(item: dict, rule: dict) -> bool:
    title = item.get("title", "")
    broker = item.get("brokerName", "")

    brokers = rule.get("brokers") or []
    if brokers and broker not in brokers:
        return False

    items = [str(x) for x in (rule.get("items") or [])]
    industries = rule.get("industries") or []
    keywords = rule.get("keywords") or []
    if not (items or industries or keywords):
        return True  # 관심 필터 없음 → 전부 통과

    if items and (item.get("itemCode") in items or item.get("itemName") in items):
        return True
    if industries and item.get("industryKoreanName") in industries:
        return True
    if keywords and any(k.lower() in title.lower() for k in keywords):
        return True
    return False
