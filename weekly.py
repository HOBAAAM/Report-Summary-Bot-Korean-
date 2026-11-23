"""주간 리포트: 한 주의 일간 요약(data/summaries/DATE.json)을 모아 '위클리 리포트' DB에 페이지 1개 게시.

위클리 DB는 메인 페이지(데일리 리포트 DB 아래)에 따로 있음 ─ 없으면 첫 실행 때 생성, id는 notion_ids.json.

    python weekly.py --start 2026-11-17 --end 2026-11-20            # 생성 + 게시
    python weekly.py --start 2026-11-17 --end 2026-11-20 --dry-run  # data/notion_preview/weekly-*.json

구성: 📌 한 줄 → 🔥 주간 테마 → 🎯 목표가 변동(섹터별 표 + 상·하향 Top 5) → 👀 여러 증권사가 다룬 종목
     → ⚠️ 공통 리스크 · 다음 주 체크포인트 → 📅 일간 링크.
목표가 분류는 일간과 같은 기준 (네이버 직전 목표가 = 마지막 공개 리포트 대비).
AI 호출 1회 (데일리 브리핑과 같은 모델), 나머지는 숫자 집계.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import date as Date, timedelta

import anthropic
import yaml

from collect import DATA, ROOT, load_json, rkey, save_json
from env import load_env
from notion_pub import blocks as B
from notion_pub.client import NotionClient
from publish_notion import (IDS_FILE, WEEKDAY, cell, day_filter, group_of, table, tp_group,
                            tp_short, tp_color, bullet_with)
from summarizer.digest import _line
from summarizer.prompts import _obj, _str_list

load_env()
log = logging.getLogger("weekly")
WEEKLY_DB_TITLE = "위클리 리포트"
WEEKLY_DB_PROPS = {
    "제목": {"title": {}},
    "기간": {"date": {}},
    "리포트 수": {"number": {}},
    "목표가 상향": {"number": {}},
    "목표가 하향": {"number": {}},
    "헤드라인": {"rich_text": {}},
    "요약": {"rich_text": {}},
}


def weekly_db(nc: NotionClient, ids: dict) -> str:
    if not ids.get("weekly_db_id"):
        db = nc.create_database(ids["parent_page_id"], WEEKLY_DB_TITLE, "📰", WEEKLY_DB_PROPS)
        ids["weekly_db_id"] = db["id"]
        save_json(IDS_FILE, ids)
        log.info("위클리 DB 생성 → %s", db["url"])
    return ids["weekly_db_id"]

SYSTEM = """\
당신은 증권사 리서치센터에서 주간 리서치 리뷰를 만드는 시니어 RA입니다. \
한 주 동안 나온 애널리스트 리포트 요약(날짜별)을 보고, 일간 브리핑에서는 안 보이는 '한 주의 흐름'을 뽑아냅니다.

원칙:
- 하루만 나온 이야기보다 여러 날·여러 증권사에 걸쳐 반복된 흐름을 우선합니다.
- 각 주장에는 근거(증권사, 요일)를 괄호로 짧게 밝힙니다.
- 목록에 없는 사실이나 숫자를 만들어내지 않습니다.
- 독자는 금융권 취업 준비생입니다. 간결한 한국어 개조식, 항목마다 정해진 글자 수를 지킵니다.
"""

SCHEMA = _obj({
    "headline": {"type": "string", "description": "이번 주 리서치 전체를 한 문장으로 (70자 이내)"},
    "brief": {"type": "string", "description": "headline을 25자 안팎으로 압축 (목록 화면 표시용)"},
    "themes": {
        "type": "array",
        "description": "여러 날에 걸쳐 반복된 주간 테마 3~5개",
        "items": _obj({
            "title": {"type": "string", "description": "테마 이름, 25자 이내"},
            "detail": {"type": "string", "description": "한 문장 60자 이내, 근거 증권사 괄호로"},
            "days": {"type": "string", "description": "언급된 요일, 예: '화·수·금'"},
        }),
    },
    "coverage": {
        "type": "array",
        "description": "입력의 '여러 증권사가 다룬 종목' 목록 각각에 대해 한 줄 (목록에 있는 종목만, 순서 유지)",
        "items": _obj({
            "name": {"type": "string"},
            "stance": {"type": "string", "enum": ["일치", "엇갈림"]},
            "view": {"type": "string", "description": "증권사들의 시각 요약, 50자 이내"},
        }),
    },
    "risks": {**_str_list, "description": "여러 리포트가 공통으로 경고한 리스크 3개 이내, 항목당 50자 이내"},
    "next_week": {**_str_list, "description": "다음 주 체크포인트(이벤트·지표·확인할 것) 3개 이내, 항목당 50자 이내"},
})


def kdate(d: str) -> str:
    y, m, dd = map(int, d.split("-"))
    return f"{m}/{dd} ({WEEKDAY[Date(y, m, dd).weekday()]})"


def week_title(start: str, end: str) -> str:
    s = Date.fromisoformat(start)
    monday = s - timedelta(days=s.weekday())
    nth = (monday.day - 1) // 7 + 1
    return f"{monday.month}월 {nth}주차 위클리 ({start[5:].replace('-', '/').lstrip('0')}~{end[5:].replace('-', '/').lstrip('0')})"


def load_week(start: str, end: str) -> tuple[list, list]:
    """(리포트 목록[각 r에 'day' 추가], 데이터가 있는 날짜들)."""
    out, days = [], []
    d = Date.fromisoformat(start)
    while d <= Date.fromisoformat(end):
        ds = d.isoformat()
        raw = load_json(DATA / "summaries" / f"{ds}.json", [])
        rows = list(raw.values()) if isinstance(raw, dict) else raw
        if rows:
            days.append(ds)
            seen = {}
            for r in rows:
                seen[rkey(r)] = {**r, "day": ds}
            out.extend(seen.values())
        d += timedelta(days=1)
    return out, days


def multi_covered(reports: list, limit: int = 10) -> list:
    """2곳 이상 증권사가 다룬 종목 [(종목, [리포트...])], 다룬 증권사 수 내림차순."""
    by = defaultdict(list)
    for r in reports:
        if r["category"] == "company" and r.get("item_name"):
            by[r["item_name"]].append(r)
    multi = [(n, rs) for n, rs in by.items() if len({r["broker"] for r in rs}) >= 2]
    multi.sort(key=lambda x: (-len({r["broker"] for r in x[1]}), x[0]))
    return multi[:limit]


def make_weekly(reports: list, multi: list, model: str, effort: str) -> dict:
    body = "\n".join(f"[{kdate(r['day'])}] {_line(r)[2:]}" for r in reports)
    cov = "\n".join(f"- {n}: " + ", ".join(f"{r['broker']}({kdate(r['day'])}, {tp_short(r) or '목표가 없음'})"
                                            for r in rs) for n, rs in multi)
    client = anthropic.Anthropic(max_retries=4)
    resp = client.beta.messages.create(
        model=model, max_tokens=16000, system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"이번 주 리포트 {len(reports)}건 요약:\n{body}\n\n여러 증권사가 다룬 종목:\n{cov or '(없음)'}"}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"위클리 생성 실패: stop_reason={resp.stop_reason}")
    log.info("AI 사용량: 입력 %d / 출력 %d 토큰", resp.usage.input_tokens, resp.usage.output_tokens)
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def tp_tables(comp: list) -> list:
    """섹터별 목표가 변동 표 + 상·하향 Top 5 표."""
    by = defaultdict(Counter)
    for r in comp:
        by[group_of(r)][tp_group(r)] += 1
    rows = sorted(by.items(), key=lambda x: -sum(x[1].values()))
    sector_rows = [[cell(g, bold=True), cell(str(sum(c.values()))),
                    cell(str(c["상향"] or ""), color="red"), cell(str(c["하향"] or ""), color="blue"),
                    cell(str(c["유지"] or ""))] for g, c in rows]
    tot = Counter(tp_group(r) for r in comp)
    sector_rows.append([cell("합계", bold=True), cell(str(len(comp)), bold=True),
                        cell(str(tot["상향"]), bold=True, color="red"),
                        cell(str(tot["하향"]), bold=True, color="blue"), cell(str(tot["유지"]), bold=True)])
    out = [B.h3("섹터별"), table(["섹터", "리포트", "🔺 상향", "🔻 하향", "➖ 유지"], sector_rows)]

    up = sorted([r for r in comp if tp_group(r) == "상향"], key=lambda r: -r["goal_change_pct"])[:5]
    down = sorted([r for r in comp if tp_group(r) == "하향"], key=lambda r: r["goal_change_pct"])[:5]
    top = []
    for label, rs in [("🔺 상향 Top 5", up), ("🔻 하향 Top 5", down)]:
        for i, r in enumerate(rs):
            top.append([cell(label if i == 0 else "", bold=True), cell(r["item_name"], bold=True),
                        cell(r["broker"]), cell(tp_short(r), color=tp_color(r)), cell(kdate(r["day"]))])
    if top:
        out += [B.h3("변동 폭 큰 종목"), table(["구분", "종목", "증권사", "의견·목표가", "날짜"], top)]
    return out


def weekly_blocks(w: dict, comp: list, multi: list, daily_links: list, n: int) -> list:
    out = [B.callout(B.rt(w["headline"], bold=True), "📌", "yellow_background"),
           B.para(B.rt(f"리포트 {n}건 · 종목분석 {len(comp)}건 · {len(daily_links)}일", color="gray")),
           B.h2("🔥 이번 주 테마")]
    for t in w.get("themes", []):
        out.append(bullet_with(B.rt(t["title"], bold=True) + B.rt(f"  {t.get('days', '')}", color="gray"),
                               [B.bullet(B.rt(t["detail"], color="gray"))]))
    out.append(B.h2("🎯 목표가 변동"))
    out += tp_tables(comp)

    views = {c["name"]: c for c in w.get("coverage", [])}
    if multi:
        out.append(B.h2("👀 여러 증권사가 다룬 종목"))
        for name, rs in multi:
            v = views.get(name, {})
            brokers = len({r["broker"] for r in rs})
            head = B.rt(name, bold=True) + B.rt(f"  {brokers}곳", color="gray")
            if v.get("stance"):
                head += B.rt(f"  {v['stance']}", color="orange" if v["stance"] == "엇갈림" else "green")
            out.append(bullet_with(head, [B.bullet(B.rt(v["view"], color="gray"))] if v.get("view") else []))
    if w.get("risks"):
        out += [B.h2("⚠️ 공통 리스크")] + B.bullets(w["risks"])
    if w.get("next_week"):
        out += [B.h2("🗓️ 다음 주 체크포인트")] + B.bullets(w["next_week"])
    if daily_links:
        out.append(B.h2("📅 일간 리포트"))
        for title, url, brief in daily_links:
            out.append(B.bullet(B.rt(title, bold=True, link=url) + (B.rt(f"  {brief}", color="gray") if brief else [])))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="주간 리포트 생성·게시")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

    reports, days = load_week(args.start, args.end)
    if not reports:
        log.info("기간 내 요약 없음")
        return 0
    comp = [r for r in reports if r["category"] == "company"]
    multi = multi_covered(reports)
    title = week_title(args.start, args.end)
    log.info("%s ─ %d일 · 리포트 %d건 · 종목분석 %d건 · 복수 커버 종목 %d개",
             title, len(days), len(reports), len(comp), len(multi))

    cache = DATA / "summaries" / f"weekly-{args.start}_{args.end}.json"
    w = load_json(cache, None)
    if not w or w.get("_based_on") != len(reports):
        dcfg = (yaml.safe_load(open(ROOT / "config.yaml"))["summarize"].get("digest") or {})
        w = make_weekly(reports, multi, dcfg.get("model", "claude-sonnet-5-5"), dcfg.get("effort", "medium"))
        w["_based_on"] = len(reports)
        save_json(cache, w)
    log.info("헤드라인: %s", w["headline"])

    nc = None if args.dry_run else NotionClient(os.environ["NOTION_TOKEN"])
    ids = load_json(IDS_FILE, {})
    links = []
    for d in days:
        pages = nc.query_all(ids["briefings_db_id"], day_filter(d)) if nc else []
        if pages:
            pg = pages[0]
            t = "".join(x["plain_text"] for x in pg["properties"]["제목"]["title"])
            brief = "".join(x["plain_text"] for x in pg["properties"]["요약"]["rich_text"])
            links.append((t, pg["url"], brief))
        elif not nc:
            links.append((kdate(d), None, None))
    blocks = weekly_blocks(w, comp, multi, links, len(reports))

    if args.dry_run:
        out = DATA / "notion_preview" / f"weekly-{args.start}_{args.end}.json"
        save_json(out, blocks)
        log.info("dry-run: %s", out.relative_to(ROOT))
        return 0

    wdb = weekly_db(nc, ids)
    for old in nc.query_all(wdb, {"property": "제목", "title": {"equals": title}}):
        nc.archive_page(old["id"])  # 재실행: 같은 주 위클리 교체
    props = {
        "제목": B.p_title(title),
        "기간": {"date": {"start": args.start, "end": args.end}},
        "리포트 수": B.p_number(len(reports)),
        "목표가 상향": B.p_number(sum(1 for r in comp if tp_group(r) == "상향")),
        "목표가 하향": B.p_number(sum(1 for r in comp if tp_group(r) == "하향")),
        "헤드라인": B.p_text(w["headline"]),
        "요약": B.p_text(w.get("brief")),
    }
    page = nc.create_page(wdb, props, blocks, icon="📰")
    log.info("게시: %s → %s", title, page["url"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
