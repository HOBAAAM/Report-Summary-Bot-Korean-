"""수집 결과 데이터 모델 ─ 2단계(AI 요약)가 이 JSON을 입력으로 받는다."""
from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"<\s*(br|/p)\s*/?>", re.I)


def html_to_text(raw: str) -> str:
    text = _BR.sub("\n", raw or "")
    text = html.unescape(_TAG.sub("", text)).replace("\xa0", " ")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _to_int(v) -> Optional[int]:
    try:
        return int(str(v).replace(",", "")) if v not in (None, "", "0") else None
    except ValueError:
        return None


def _pct(new: Optional[int], base: Optional[int]) -> Optional[float]:
    if new is None or not base:
        return None
    return round((new - base) / base * 100, 2)


@dataclass
class Report:
    nid: str
    category: str
    title: str
    broker: str
    write_date: str
    analyst: Optional[str] = None
    # 종목분석 전용
    item_code: Optional[str] = None
    item_name: Optional[str] = None
    opinion: Optional[str] = None
    opinion_type: Optional[str] = None
    goal_price: Optional[int] = None
    prev_goal_price: Optional[int] = None
    price_at_write: Optional[int] = None
    goal_change_pct: Optional[float] = None   # 목표가 변동률
    upside_pct: Optional[float] = None        # 작성일 주가 대비 상승여력
    # 섹터: 종목분석 = 네이버 종목 업종(industryName), 산업분석 = 리포트 산업 분류
    sector: Optional[str] = None
    market: Optional[str] = None              # KOSPI / KOSDAQ
    industry: Optional[str] = None
    # 공통
    summary_text: str = ""                    # 네이버 제공 요약 (HTML 제거)
    pdf_url: Optional[str] = None
    pdf_path: Optional[str] = None
    flags: list = field(default_factory=list)  # 특이사항 태그
    read_count: Optional[int] = None

    @classmethod
    def from_api(cls, category: str, item: dict, detail: dict) -> "Report":
        merged = {**item, **detail}
        goal = _to_int(merged.get("goalPrice"))
        prev = _to_int(merged.get("prevGoalPrice"))
        price = _to_int(merged.get("priceAtWriteDate"))
        r = cls(
            nid=str(merged["nid"]),
            category=category,
            title=merged.get("title", "").strip(),
            broker=merged.get("brokerName", ""),
            write_date=merged.get("writeDate", ""),
            analyst=merged.get("analystName") or merged.get("analyst"),
            item_code=merged.get("itemCode"),
            item_name=merged.get("itemName"),
            opinion=merged.get("opinionText"),
            opinion_type=merged.get("opinionType"),
            goal_price=goal,
            prev_goal_price=prev,
            price_at_write=price,
            goal_change_pct=_pct(goal, prev),
            upside_pct=_pct(goal, price),
            industry=merged.get("industryKoreanName") or merged.get("categoryIndustry"),
            summary_text=html_to_text(merged.get("content", "")),
            pdf_url=merged.get("attachUrl"),
            read_count=_to_int(merged.get("readCount")),
        )
        if category == "industry":
            r.sector = r.industry
        r.flags = r._detect_flags()
        return r

    def _detect_flags(self) -> list:
        """규칙 기반 1차 특이사항 태깅 (AI 요약 단계에서 보강 예정)."""
        flags = []
        if self.goal_change_pct is not None:
            if self.goal_change_pct > 0:
                flags.append("목표가↑")
            elif self.goal_change_pct < 0:
                flags.append("목표가↓")
        elif self.goal_price and self.prev_goal_price is None and self.category == "company":
            flags.append("신규/첫 목표가")
        if self.upside_pct is not None and self.upside_pct >= 50:
            flags.append("상승여력 50%+")
        if self.opinion_type and self.opinion_type not in ("buy", "strongbuy"):
            flags.append(f"의견:{self.opinion}")
        return flags

    def to_dict(self) -> dict:
        return asdict(self)
