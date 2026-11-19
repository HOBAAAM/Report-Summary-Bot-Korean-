"""하루치 요약들을 모아 '오늘의 리서치 브리핑'을 만든다 (텍스트만 사용, 1회 호출)."""
from __future__ import annotations

import json

import anthropic

from .prompts import CATEGORY_KO, _obj, _str_list

DIGEST_SYSTEM = """\
당신은 증권사 리서치센터의 데일리 모닝미팅 자료를 만드는 시니어 RA입니다. \
오늘 나온 애널리스트 리포트 요약 목록을 보고, 여러 리포트를 관통하는 흐름을 뽑아냅니다.

원칙:
- 개별 리포트를 나열하지 말고, 여러 증권사·섹터에 걸쳐 반복되는 테마와 시각 차이를 찾습니다.
- 각 주장에는 근거가 된 리포트(증권사, 종목/제목)를 괄호로 짧게 밝힙니다.
- 목록에 없는 사실이나 숫자를 만들어내지 않습니다.
- 간결한 한국어 개조식으로 씁니다.
"""

DIGEST_SCHEMA = _obj({
    "headline": {"type": "string",
                     "description": "오늘 리서치 전체를 관통하는 한 문장. 노션 페이지 제목으로 쓰이므로 40자 안팎으로 짧고 강하게"},
    "themes": {
        "type": "array",
        "description": "여러 리포트에 걸친 핵심 테마 3~5개",
        "items": _obj({
            "title": {"type": "string"},
            "detail": {"type": "string", "description": "2~3문장, 근거 리포트 포함"},
        }),
    },
    "divergence": {**_str_list, "description": "같은 종목/섹터에 대해 증권사 간 시각이 갈린 부분"},
    "watch_list": {
        "type": "array",
        "description": "오늘 리포트 기준 주목할 종목 3~5개",
        "items": _obj({"name": {"type": "string"}, "reason": {"type": "string"}}),
    },
    "risks": {**_str_list, "description": "여러 리포트가 공통으로 경고한 리스크"},
})


def _line(r: dict) -> str:
    ai = r.get("ai") or {}
    subject = r.get("item_name") or r.get("sector") or ""
    tp = ""
    if r.get("goal_price"):
        chg = f", {r['goal_change_pct']:+.1f}%" if r.get("goal_change_pct") is not None else ""
        tp = f" | {r.get('opinion')} TP {r['goal_price']:,}{chg}"
    notable = "; ".join(ai.get("notable") or [])
    return (f"- [{CATEGORY_KO.get(r['category'])}/{ai.get('sector_group', '?')}] {subject} "
            f"({r['broker']}){tp} :: {ai.get('one_line') or r['title']}"
            + (f" / 특이: {notable}" if notable else ""))


def make_digest(summaries: list, model: str, effort: str = "medium") -> dict:
    client = anthropic.Anthropic(max_retries=4)
    body = "\n".join(_line(r) for r in summaries)
    response = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        system=DIGEST_SYSTEM,
        messages=[{"role": "user", "content": f"오늘 리포트 {len(summaries)}건 요약:\n{body}"}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": DIGEST_SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"digest 생성 실패: stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)
