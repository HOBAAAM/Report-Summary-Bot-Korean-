"""요약 프롬프트와 출력 스키마.

스키마를 고정하는 이유: 결과가 그대로 Notion 속성/본문 블록으로 매핑되고,
나중에 섹터별·증권사별 통계를 낼 수 있어야 하기 때문.
"""
from __future__ import annotations

SYSTEM_PROMPT = """\
당신은 한국 증권사 리서치센터의 시니어 RA입니다. 애널리스트 리포트를 읽고, \
바쁜 펀드매니저가 1분 안에 핵심을 파악할 수 있도록 정리합니다.

원칙:
- 숫자(매출, 영업이익, 성장률, 목표가, 멀티플 등)는 리포트에 적힌 값만 사용합니다. \
리포트에 없는 정보는 추측하지 말고 null 또는 빈 배열로 둡니다.
- 사실(실적 발표치)과 애널리스트의 판단(전망, 추정, 의견)을 구분해서 씁니다.
- 금액 단위는 리포트 표기를 따르되 가능하면 '억원/조원'으로 통일합니다.
- **짧게 씁니다.** 명사형 개조식(예: '3Q 영업익 컨센 하회', '27년 흑자전환 전망')으로, \
항목 하나는 40자 이내. 수식어·배경 설명·중복 표현은 뺍니다. 각 목록은 설명에 적힌 최대 개수를 넘기지 않습니다.
- 숫자는 핵심 1~2개만 (예: '영업익 -26억, 적자전환'). 세부 내역은 생략합니다.
- 'notable'(특이사항)에는 투자자가 놓치면 안 되는 것만 넣습니다: 투자의견/목표가 변경과 \
그 이유, 신규 커버리지, 컨센서스와 크게 다른 시각, 일회성 비용, 가이던스 변경, \
경영진 변화, M&A, 규제 이슈 등. 해당 사항이 없으면 빈 배열.
- 'tone'은 리포트 전체 논조이며, 매수 의견이라도 내용이 우려 중심이면 '중립' 또는 '부정'입니다.
"""

# 종목 업종(네이버 79개 세분류)과 산업 리포트 분류(기타 다수)를 하나로 묶는 공통 대분류.
# Notion에서 섹터별로 묶는 기준이 된다. (Notion select 이름엔 쉼표 불가 → '·' 사용)
# 섹터 구분: NH투자증권 리서치 분류 기준 + 철강/금속, 우주항공과 국방 (2026-11-26 결정)
SECTOR_GROUPS = [
    "반도체", "2차전지", "전자장비와 부품", "자동차", "플랫폼/게임", "통신서비스",
    "레저/엔터", "미디어/광고", "소매유통", "음식료", "화장품", "패션", "제약바이오",
    "건설", "국내 리츠", "부동산", "유틸리티", "운송", "조선", "우주항공과 국방",
    "석유와 가스/화학", "철강/금속", "지주회사", "은행", "보험", "증권/STO", "미드스몰캡",
    "ESG·정책", "시장전략", "매크로·경제", "채권·금리",
]

_nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_sector_group = {
    "type": "string",
    "enum": SECTOR_GROUPS,
    "description": "리포트가 다루는 대분류 하나. 개별 종목/산업이면 해당 산업. "
                   "전력기기(변압기·전선·배전)는 전자장비와 부품, 소프트웨어·IT서비스는 플랫폼/게임(통신 관련이면 통신서비스). "
                   "기계·장비 업체는 전방 산업으로: 반도체 장비→반도체, 2차전지 장비→2차전지, 자동차 부품·기계→자동차, "
                   "그 외 기계·로봇·부품→전자장비와 부품, 조선 기자재→조선. 방산·항공기·우주→우주항공과 국방. "
                   "어느 산업에도 뚜렷이 속하지 않는 소형주 소개 리포트는 미드스몰캡. "
                   "시황·투자전략·증권사 모닝 데일리(모닝레터/모닝브리프 등)는 시장전략, 경제는 매크로·경제, "
                   "채권은 채권·금리, ESG·지배구조·자본시장 제도/규제는 ESG·정책",
}
_str_list = {"type": "array", "items": {"type": "string"}}
_tone = {"type": "string", "enum": ["긍정", "중립", "부정"]}


def _obj(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


COMPANY_SCHEMA = _obj({
    "sector_group": _sector_group,
    "one_line": {"type": "string", "description": "리포트 핵심을 한 문장으로, 60자 이내"},
    "thesis": {**_str_list, "description": "핵심 투자 논지 최대 3개, 각 40자 이내"},
    "earnings": _obj({
        "period": {**_nullable_str, "description": "다룬 실적 기간, 예: 3Q26"},
        "summary": {**_nullable_str, "description": "매출·영업이익 핵심 수치 한 줄, 60자 이내"},
        "vs_consensus": {
            "type": "string",
            "enum": ["상회", "부합", "하회", "언급없음"],
            "description": "시장 기대치/컨센서스 대비",
        },
    }),
    "estimate_change": {**_nullable_str, "description": "추정치 변경 한 줄 (변경 없으면 null)"},
    "valuation": {**_nullable_str, "description": "목표가 산정 방식, 예: 12MF EPS × PER 15배"},
    "goal_price_reason": {**_nullable_str, "description": "목표가/의견 변경 이유 한 줄 (변경 없으면 null)"},
    "catalysts": {**_str_list, "description": "주가 상승 촉매 최대 2개"},
    "risks": {**_str_list, "description": "리스크 최대 2개"},
    "notable": {**_str_list, "description": "특이사항 최대 2개 (없으면 빈 배열)"},
    "tone": _tone,
    "keywords": {**_str_list, "description": "핵심 키워드 3~5개"},
})

GENERAL_SCHEMA = _obj({
    "sector_group": _sector_group,
    "one_line": {"type": "string", "description": "리포트 핵심을 한 문장으로, 60자 이내"},
    "key_points": {**_str_list, "description": "핵심 내용 최대 3개, 각 40자 이내"},
    "view": {**_nullable_str, "description": "섹터/시장/금리 방향성 의견, 예: 비중확대, 금리 하락 전망"},
    "top_picks": {
        "type": "array",
        "description": "추천 종목 최대 3개, 이유는 20자 이내 (없으면 빈 배열)",
        "items": _obj({"name": {"type": "string"}, "reason": {"type": "string"}}),
    },
    "related_stocks": {**_str_list, "description": "언급된 주요 종목명 최대 5개"},
    "risks": {**_str_list, "description": "리스크 최대 2개"},
    "notable": {**_str_list, "description": "특이사항 최대 2개 (없으면 빈 배열)"},
    "tone": _tone,
    "keywords": {**_str_list, "description": "핵심 키워드 3~5개"},
})

CATEGORY_KO = {
    "company": "종목분석", "industry": "산업분석", "invest": "투자전략",
    "economy": "경제분석", "market": "시황", "debenture": "채권분석",
}


def schema_for(category: str) -> dict:
    return COMPANY_SCHEMA if category == "company" else GENERAL_SCHEMA


def build_user_text(report: dict, text_only: bool) -> str:
    """API에서 받은 메타데이터를 함께 줘서 목표가 변경 이유 등을 짚게 한다."""
    meta = [
        f"구분: {CATEGORY_KO.get(report['category'], report['category'])}",
        f"제목: {report['title']}",
        f"증권사: {report['broker']}",
        f"작성일: {report['write_date']}",
    ]
    if report.get("item_name"):
        meta.append(f"종목: {report['item_name']} ({report['item_code']}, {report.get('sector') or '업종 미상'})")
    if report.get("sector") and not report.get("item_name"):
        meta.append(f"산업: {report['sector']}")
    if report.get("goal_price"):
        prev = report.get("prev_goal_price")
        meta.append(
            f"투자의견: {report.get('opinion')} / 목표가: {report['goal_price']:,}원"
            + (f" (직전 {prev:,}원, {report['goal_change_pct']:+.1f}%)" if prev else " (직전 목표가 없음)")
        )
    if report.get("price_at_write"):
        meta.append(f"작성일 주가: {report['price_at_write']:,}원")

    head = "[리포트 정보]\n" + "\n".join(meta)
    if text_only:
        return (
            f"{head}\n\n[증권사 제공 요약]\n{report.get('summary_text') or '(없음)'}\n\n"
            "위 요약문을 바탕으로 정리해 주세요. 요약문에 없는 내용은 비워 두세요."
        )
    return f"{head}\n\n첨부한 리포트 PDF 전체를 읽고 정리해 주세요."
