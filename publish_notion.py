"""3단계: 요약 결과를 Notion에 게시.

Notion 구조:
    부모 페이지
    └─ 🗓️ 데일리 리포트 (DB, 날짜별 1행)
       └─ 11/18 (수) 데일리 리포트   (목록 화면에는 '요약' 열에 초간단 요약 표시)
          ├─ 브리핑 (노란 헤드라인 · 건수 · 테마 · 🚩 하이라이트 표)
          ├─ 🏭 섹터별 리포트 (인라인 DB, 리포트 1건 = 1행 = 분석 페이지)
          ├─ 🌐 시장 · 매크로 · 채권 리포트 (인라인 DB)
          └─ ✍️ My View

최초 1회:
    python publish_notion.py --setup https://app.notion.com/p/<page-id>
    → 해당 페이지 아래에 '데일리 리포트' DB를 만들고 ID를 notion_ids.json에 저장

매일:
    python publish_notion.py                  # 오늘(KST)
    python publish_notion.py --date 2026-11-18
    python publish_notion.py --dry-run        # Notion에 쓰지 않고 payload만 생성
    python publish_notion.py --exists         # 그날 브리핑이 이미 있으면 'true', 없으면 'false' 출력

입력: data/summaries/DATE.json (없으면 data/reports/DATE.json으로 메타데이터만 게시)
      data/summaries/DATE.digest.json (있으면 브리핑 상단에 AI 종합 분석)
같은 날 재실행하면 그날 페이지(하위 리포트 포함)를 휴지통으로 보내고 새로 만듦 (중복 없음).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from collect import DATA, KST, ROOT, load_json, rkey, save_json
from env import load_env
from notion_pub import blocks as B
from notion_pub.client import NotionClient, parse_page_id
from summarizer.prompts import CATEGORY_KO, SECTOR_GROUPS

load_env()
log = logging.getLogger("publish")
IDS_FILE = ROOT / "notion_ids.json"
KO_TO_CAT = {v: k for k, v in CATEGORY_KO.items()}
MACRO_GROUPS = ["시장전략", "매크로·경제", "채권·금리"]
WEEKDAY = "월화수목금토일"
DB_TITLE = "데일리 리포트"
CATEGORY_ICON = {"company": "🏢", "industry": "🏭", "invest": "🧭",
                 "economy": "🌐", "market": "📈", "debenture": "💵"}

# 날짜 페이지 안의 리포트 DB (섹터별 / 시장·매크로·채권 각 1개).
# API로는 열 순서를 정할 수 없어 열 수를 최소로 유지 (구분은 아이콘, 날짜는 리포트 페이지 첫 줄에 표시)
REPORT_DB_PROPS = {
    "제목": {"title": {}},
    "섹터": {"select": {}},
    "증권사": {"select": {}},
    "의견·목표가": {"rich_text": {}},
    "한줄 요약": {"rich_text": {}},
}
MACRO_DB_PROPS = {k: v for k, v in REPORT_DB_PROPS.items() if k != "의견·목표가"}  # 목표가 없음
BRIEFING_DB_PROPS = {
    "제목": {"title": {}},
    "날짜": {"date": {}},
    "리포트 수": {"number": {}},
    "목표가 상향": {"number": {}},
    "목표가 하향": {"number": {}},
    "헤드라인": {"rich_text": {}},
    "요약": {"rich_text": {}},
}


# ─────────────────────────── helpers ───────────────────────────
def pct(v):
    return None if v is None else round(v / 100, 4)  # Notion percent 포맷은 비율값


FIXED_GROUP = {"market": "시장전략", "economy": "매크로·경제", "debenture": "채권·금리"}


def group_of(r: dict) -> str:
    """섹터 대분류. 시황/경제/채권은 카테고리로 고정 (AI가 '지주·기타'로 잘못 보내는 경우 방지),
    투자전략은 ESG·정책만 AI 판단을 존중하고 나머지는 시장전략."""
    ai = r.get("ai") or {}
    cat, g = r["category"], ai.get("sector_group")
    if cat in FIXED_GROUP:
        return FIXED_GROUP[cat]
    if cat == "invest":
        return g if g == "ESG·정책" else "시장전략"
    return g or r.get("sector") or "미분류"


def subject(r: dict) -> str:
    return r.get("item_name") or r.get("sector") or CATEGORY_KO.get(r["category"], "")


def tp_text(r: dict) -> str:
    if not r.get("goal_price"):
        return ""
    s = f"{r.get('opinion') or ''} · TP {r['goal_price']:,}원"
    if r.get("prev_goal_price"):
        chg = r["goal_change_pct"]
        s += " (유지)" if not chg else f" (직전 {r['prev_goal_price']:,}원, {chg:+.1f}%)"
    if r.get("upside_pct") is not None:
        s += f" · 상승여력 {r['upside_pct']:.1f}%"
    return s


# ─────────────────────────── report page ───────────────────────────
def weekday(date: str) -> str:
    return WEEKDAY[datetime.strptime(date, "%Y-%m-%d").weekday()]


def day_title(date: str) -> str:
    return f"{int(date[5:7])}/{int(date[8:10])} ({weekday(date)}) 데일리 리포트"


def report_title(r: dict) -> str:
    base = r["title"]
    tag = f"[{subject(r)}]"
    if r["category"] in ("company", "industry") and not base.startswith(tag):  # 원제목에 이미 있으면 중복 방지
        base = f"{tag} {base}"
    return f"{base} · {r['broker']}"


def report_meta(r: dict) -> dict:
    """리포트 페이지 맨 위 한 줄: 날짜 · 구분 · 섹터 · 증권사 · 애널리스트."""
    d = r["write_date"]
    parts = [f"{d} ({weekday(d)})", CATEGORY_KO.get(r["category"]), group_of(r),
             r["broker"], r.get("analyst")]
    return B.para(B.rt(" · ".join(x for x in parts if x), color="gray"))


def report_blocks(r: dict) -> list:
    ai = r.get("ai")
    out = [report_meta(r)]
    if not ai:
        out.append(B.callout("AI 요약 전 ─ 증권사 제공 요약만 표시됩니다.", "⏳"))
        out += [B.para(line) for line in (r.get("summary_text") or "").splitlines()[:50]]
        out.append(B.para(B.rt("📄 원문 PDF", link=r.get("pdf_url"))))
        return out

    out.append(B.callout(B.rt(ai.get("one_line", ""), bold=True), "💡", "blue_background"))
    if tp_text(r):
        out.append(B.para(B.rt(tp_text(r), color="gray")))

    def section(title, items, limit=3):
        items = [x for x in (items or []) if x][:limit]  # 프롬프트 개수 제한의 안전장치
        if items:
            out.append(B.h3(title))
            out.extend(B.bullets(items))

    def labeled(pairs):
        """'라벨: 내용' 형태의 짧은 목록 (값이 없는 줄은 생략)."""
        return [B.bullet(B.rt(f"{k}  ", bold=True) + B.rt(v)) for k, v in pairs if v]

    if r["category"] == "company":
        section("투자 논지", ai.get("thesis"))
        e = ai.get("earnings") or {}
        cons = e.get("vs_consensus")
        nums = labeled([
            (f"실적{' ' + e['period'] if e.get('period') else ''}"
             + (f" (컨센 {cons})" if cons and cons != "언급없음" else ""), e.get("summary")),
            ("추정치", ai.get("estimate_change")),
            ("밸류", ai.get("valuation")),
            ("변경 이유", ai.get("goal_price_reason")),
            ("촉매", " / ".join((ai.get("catalysts") or [])[:2])),
        ])
        if nums:
            out.append(B.h3("📊 숫자"))
            out.extend(nums)
    else:
        section("핵심 내용", ai.get("key_points"))
        picks = [f"{p['name']} ({p['reason']})" for p in (ai.get("top_picks") or [])[:3]]
        extra = labeled([
            ("방향성", ai.get("view")),
            ("Top Picks", ", ".join(picks)),
            ("언급 종목", ", ".join((ai.get("related_stocks") or [])[:5])),
        ])
        if extra:
            out.append(B.h3("📌 시각"))
            out.extend(extra)
    section("⚠️ 리스크", ai.get("risks"), limit=2)
    section("🚩 특이사항", ai.get("notable"), limit=2)

    out.append(B.divider())
    src = [B.para(line) for line in (r.get("summary_text") or "").splitlines()[:50]]
    if src:
        out.append(B.toggle("증권사 제공 요약 (원문)", src))
    meta = r.get("ai_meta") or {}
    trunc = (f" · 전체 {meta['pages_total']}p 중 {meta['pages_sent']}p 분석"
             if meta.get("pages_total") and meta["pages_sent"] < meta["pages_total"] else "")
    out.append(B.para(B.rt("📄 원문 PDF", link=r.get("pdf_url"))
                      + B.rt(f"   ·  {meta.get('model', '')} 요약{trunc}", color="gray", italic=True)))
    return out


# ─────────────────────────── daily briefing ───────────────────────────
def grouped(reports: list) -> tuple:
    """(섹터 그룹들, 매크로 그룹들) ─ 각 [(이름, 리포트들)]. 브리핑과 하위 페이지가 같은 순서를 씀."""
    by_group = defaultdict(list)
    for r in reports:
        by_group[group_of(r)].append(r)
    order = [g for g in SECTOR_GROUPS if g not in MACRO_GROUPS] + sorted(
        g for g in by_group if g not in SECTOR_GROUPS)
    sectors = [(g, sorted(by_group[g], key=lambda r: (r["category"] != "industry", subject(r))))
               for g in order if by_group.get(g)]
    macro = [(g, by_group[g]) for g in MACRO_GROUPS if by_group.get(g)]
    return sectors, macro


def tp_short(r: dict) -> str:
    """'매수 39,000 (-11.4%)' ─ 종목분석만."""
    if r["category"] != "company" or not r.get("goal_price"):
        return ""
    op = r.get("opinion") if r.get("opinion") not in (None, "", "없음", "-") else ""
    s = f"{op} {r['goal_price']:,}".strip()
    chg = r.get("goal_change_pct")
    return s + (f" ({chg:+.1f}%)" if chg else "")


def tp_color(r: dict) -> str:
    chg = r.get("goal_change_pct")
    return "red" if chg and chg > 0 else "blue" if chg and chg < 0 else "default"


def cell(text, **kw) -> list:
    return B.rt(text, **kw) if text else []


def table(header: list, rows: list) -> dict:
    trs = [{"type": "table_row", "table_row": {"cells": [cell(h, bold=True) for h in header]}}]
    trs += [{"type": "table_row", "table_row": {"cells": r}} for r in rows]
    return {"object": "block", "type": "table",
            "table": {"table_width": len(header), "has_column_header": True, "has_row_header": False,
                      "children": trs}}


def highlight_table(reports: list, urls: dict):
    """규칙 기반 하이라이트 (AI 판단 아님, 숫자 기준) → 표 1개. 해당 없으면 None."""
    comp = [r for r in reports if r["category"] == "company"]
    vs = lambda r: ((r.get("ai") or {}).get("earnings") or {}).get("vs_consensus")  # noqa: E731
    groups = [
        ("🔺 목표가 상향", sorted([r for r in comp if (r.get("goal_change_pct") or 0) > 0],
                              key=lambda r: -r["goal_change_pct"])),
        ("🔻 목표가 하향", sorted([r for r in comp if (r.get("goal_change_pct") or 0) < 0],
                              key=lambda r: r["goal_change_pct"])),
        ("⚪ 매수 외 의견", [r for r in comp if r.get("opinion_type")
                          and r["opinion_type"] not in ("buy", "strongbuy")]),
        ("📈 컨센 상회", [r for r in comp if vs(r) == "상회"]),
        ("📉 컨센 하회", [r for r in comp if vs(r) == "하회"]),
    ]
    rows = []
    for label, rs in groups:
        for i, r in enumerate(rs):
            rows.append([cell(label if i == 0 else "", bold=True),
                         cell(subject(r), bold=True, link=urls.get(rkey(r))),
                         cell(r["broker"]),
                         cell(tp_short(r), color=tp_color(r))])
    # 한줄 요약은 칸 안에서 여러 줄로 접혀 읽기 어려움 → 종목을 누르면 상세 분석으로 이동
    return table(["구분", "종목", "증권사", "의견·목표가"], rows) if rows else None


def bullet_with(rich: list, children: list) -> dict:
    b = B.bullet(rich)
    if children:
        b["bulleted_list_item"]["children"] = children
    return b


def briefing_blocks(date: str, reports: list, digest: dict, urls: dict) -> list:
    """날짜 페이지 상단: 노란 헤드라인 → 건수 → 테마 등 AI 브리핑 → 🚩 하이라이트 표."""
    out = []
    counts = defaultdict(int)
    for r in reports:
        counts[r["category"]] += 1
    stats = " · ".join(f"{CATEGORY_KO[c]} {n}" for c, n in counts.items())
    if digest:
        out.append(B.callout(B.rt(digest["headline"], bold=True), "📌", "yellow_background"))
    out.append(B.para(B.rt(f"총 {len(reports)}건  ({stats})", color="gray")))

    if digest:
        # 긴 문장이 여러 줄로 이어지지 않도록: 굵은 제목 한 줄 + 설명은 한 단계 들여서
        out.append(B.h2("🔥 오늘의 테마"))
        for t in digest.get("themes", []):
            out.append(bullet_with(B.rt(t["title"], bold=True),
                                   [B.bullet(B.rt(t["detail"], color="gray"))] if t.get("detail") else []))
        if digest.get("divergence"):
            out.append(B.h3("⚖️ 증권사 간 시각 차이"))
            out.extend(B.bullets(digest["divergence"]))
        if digest.get("watch_list"):
            out.append(B.h3("👀 주목 종목"))
            out.extend(B.bullet(B.rt(w["name"], bold=True) + B.rt("  " + w["reason"], color="gray"))
                       for w in digest["watch_list"])
        if digest.get("risks"):
            out.append(B.h3("⚠️ 공통 리스크"))
            out.extend(B.bullets(digest["risks"]))

    hl = highlight_table(reports, urls)
    if hl:
        out.append(B.h2("🚩 하이라이트"))
        out.append(hl)
    return out


def my_view_blocks() -> list:
    return [B.divider(), B.h2("✍️ My View"),
            B.quote(B.rt("오늘 리포트를 읽고 든 생각, 동의하지 않는 부분, 추적할 포인트를 기록하세요.",
                         color="gray", italic=True))]


def report_row_props(r: dict, schema: dict = REPORT_DB_PROPS) -> dict:
    ai = r.get("ai") or {}
    props = {
        "제목": B.p_title(report_title(r).rsplit(" · ", 1)[0]),  # 증권사는 별도 열
        "섹터": B.p_select(group_of(r)),
        "증권사": B.p_select(r["broker"]),
        "의견·목표가": B.p_text(tp_short(r)),
        "한줄 요약": B.p_text(ai.get("one_line")),
    }
    return {k: v for k, v in props.items() if k in schema}


# ─────────────────────────── commands ───────────────────────────
def cmd_setup(nc: NotionClient, page: str) -> None:
    parent = parse_page_id(page)
    nc.get_page(parent)  # 연결(Connections) 안 됐으면 여기서 404
    bdb = nc.create_database(parent, DB_TITLE, "🗓️", BRIEFING_DB_PROPS)
    ids = {"parent_page_id": parent, "briefings_db_id": bdb["id"]}
    save_json(IDS_FILE, ids)
    log.info("생성 완료 → %s\n  데일리 리포트 DB: %s", IDS_FILE.name, bdb["url"])


def plain_title(props: dict) -> str:
    return "".join(t["text"]["content"] for t in props["제목"]["title"])


def publish_day(nc: NotionClient, bdb: str, date: str, props: dict, reports: list, digest: dict) -> int:
    """날짜 페이지 1개 생성: 브리핑(+하이라이트 표) → 🏭 섹터별 DB → 🌐 시장·매크로·채권 DB → My View.

    인라인 DB는 항상 부모 본문 맨 끝에 붙으므로, 자리표시 블록 하나로 페이지를 만들고 → DB·행 생성
    → My View 추가 → 브리핑을 자리표시 바로 뒤에 끼워 넣은 뒤 자리표시 삭제 (하이라이트 링크가 행을 가리키도록).
    반환: 실패한 리포트 수.
    """
    for p in nc.query_all(bdb, {"property": "날짜", "date": {"equals": date}}):
        nc.archive_page(p["id"])  # 재실행: 기존 날짜 페이지(안의 리포트 DB 포함) 휴지통으로
    day = nc.create_page(bdb, props, [B.para(B.rt(f"리포트 {len(reports)}건 정리 중…", color="gray"))],
                         icon="📰")
    anchor = nc.list_children(day["id"])[0]["id"]

    sectors, macro = grouped(reports)
    urls, failed = {}, 0
    for title, icon, groups, schema in [("섹터별 리포트", "🏭", sectors, REPORT_DB_PROPS),
                                        ("시장 · 매크로 · 채권 리포트", "🌐", macro, MACRO_DB_PROPS)]:
        if not groups:
            continue
        n = sum(len(rs) for _, rs in groups)
        db = nc.create_database(day["id"], f"{title} ({n})", icon, schema)
        for _, rs in groups:  # 섹터 순서대로 생성 → 기본 보기에서 섹터끼리 모여 보임
            for r in rs:
                try:
                    row = nc.create_page(db["id"], report_row_props(r, schema), report_blocks(r),
                                         icon=CATEGORY_ICON.get(r["category"]))
                    urls[rkey(r)] = row["url"]
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log.warning("✗ %s %s: %s", rkey(r), r["title"], e)

    nc.append_children(day["id"], my_view_blocks())
    nc.append_children(day["id"], briefing_blocks(date, reports, digest, urls), after=anchor)
    nc.request("DELETE", f"/blocks/{anchor}")  # 자리표시 문구 제거
    log.info("게시: %s ─ 리포트 %d건 (실패 %d) → %s", plain_title(props), len(reports) - failed, failed, day["url"])
    return failed


def cmd_publish(nc, date: str, dry_run: bool) -> int:
    summaries = load_json(DATA / "summaries" / f"{date}.json", [])
    raw = load_json(DATA / "reports" / f"{date}.json", []) or summaries  # 결과 보관본만 있을 때도 게시 가능
    if not raw:
        log.info("%s 리포트 없음 (휴일 등) ─ 게시할 내용 없음", date)
        return 0
    by_key = {rkey(r): r for r in raw}
    by_key.update({rkey(r): r for r in summaries})  # 요약본 우선
    reports = list(by_key.values())
    digest = load_json(DATA / "summaries" / f"{date}.digest.json", None)
    log.info("%s ─ 리포트 %d건 (AI 요약 %d건), 브리핑 AI 분석 %s",
             date, len(reports), sum(1 for r in reports if r.get("ai")), "있음" if digest else "없음")

    if dry_run:
        out = DATA / "notion_preview" / date
        save_json(out / "reports.json", [{"properties": report_row_props(r), "children": report_blocks(r)}
                                         for r in reports])
        save_json(out / "briefing.json", briefing_blocks(date, reports, digest, {}) + my_view_blocks())
        log.info("dry-run: payload 저장 → %s", out.relative_to(ROOT))
        return 0

    ids = load_json(IDS_FILE, None)
    if not ids:
        log.error("notion_ids.json 없음 ─ 먼저 --setup <페이지 링크> 실행")
        return 1
    comp = [r for r in reports if r["category"] == "company"]
    props = {
        "제목": B.p_title(day_title(date)),
        "날짜": B.p_date(date),
        "리포트 수": B.p_number(len(reports)),
        "목표가 상향": B.p_number(sum(1 for r in comp if (r.get("goal_change_pct") or 0) > 0)),
        "목표가 하향": B.p_number(sum(1 for r in comp if (r.get("goal_change_pct") or 0) < 0)),
        "헤드라인": B.p_text((digest or {}).get("headline")),
        "요약": B.p_text((digest or {}).get("brief")),
    }
    failed = publish_day(nc, ids["briefings_db_id"], date, props, reports, digest)
    return 1 if failed else 0


def cmd_exists(nc, date: str) -> int:
    """그날 데일리 브리핑이 이미 게시됐는지 stdout에 true/false로 출력 (예비 실행 스킵 판단용)."""
    ids = load_json(IDS_FILE, None)
    if not ids:
        log.error("notion_ids.json 없음 ─ 먼저 --setup <페이지 링크> 실행")
        return 1
    found = nc.query_all(ids["briefings_db_id"], {"property": "날짜", "date": {"equals": date}})
    print("true" if found else "false")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Notion 게시")
    p.add_argument("--date", default=datetime.now(KST).strftime("%Y-%m-%d"))
    p.add_argument("--setup", metavar="PAGE_URL", help="최초 1회: 부모 페이지 아래 DB 생성")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--exists", action="store_true", help="그날 브리핑 존재 여부만 출력 (true/false)")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
                        datefmt="%H:%M:%S")

    token = os.environ.get("NOTION_TOKEN")
    if not token and not args.dry_run:
        log.error("NOTION_TOKEN이 없습니다 (.env 또는 환경변수)")
        return 1
    nc = NotionClient(token) if token else None
    if args.setup:
        cmd_setup(nc, args.setup)
        return 0
    if args.exists:
        return cmd_exists(nc, args.date)
    return cmd_publish(nc, args.date, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
