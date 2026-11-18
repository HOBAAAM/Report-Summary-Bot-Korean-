"""3단계: 요약 결과를 Notion에 게시.

최초 1회:
    python publish_notion.py --setup https://app.notion.com/p/<page-id>
    → 해당 페이지 아래에 '📚 애널리스트 리포트' DB와 '🗓️ 데일리 브리핑' DB를 만들고
      ID를 notion_ids.json에 저장

매일:
    python publish_notion.py                  # 오늘(KST)
    python publish_notion.py --date 2026-11-18
    python publish_notion.py --dry-run        # Notion에 쓰지 않고 payload만 생성

입력: data/summaries/DATE.json (없으면 data/reports/DATE.json으로 메타데이터만 게시)
      data/summaries/DATE.digest.json (있으면 브리핑 상단에 AI 종합 분석)
같은 날 재실행해도 중복 생성하지 않음 (nid 기준). AI 요약 없이 먼저 올라간 건은 요약이 생기면 교체.
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
CATEGORY_ICON = {"company": "🏢", "industry": "🏭", "invest": "🧭",
                 "economy": "🌐", "market": "📈", "debenture": "💵"}

REPORT_DB_PROPS = {
    "제목": {"title": {}},
    "날짜": {"date": {}},
    "구분": {"select": {}},
    "섹터 대분류": {"select": {}},
    "업종": {"select": {}},
    "종목": {"rich_text": {}},
    "종목코드": {"rich_text": {}},
    "시장": {"select": {}},
    "증권사": {"select": {}},
    "애널리스트": {"rich_text": {}},
    "투자의견": {"select": {}},
    "목표가": {"number": {"format": "number_with_commas"}},
    "직전 목표가": {"number": {"format": "number_with_commas"}},
    "목표가 변동": {"number": {"format": "percent"}},
    "작성일 주가": {"number": {"format": "number_with_commas"}},
    "상승여력": {"number": {"format": "percent"}},
    "논조": {"select": {}},
    "컨센서스 대비": {"select": {}},
    "특이사항": {"multi_select": {}},
    "키워드": {"multi_select": {}},
    "한줄요약": {"rich_text": {}},
    "원문 PDF": {"url": {}},
    "AI 요약": {"checkbox": {}},
    "My View": {"rich_text": {}},
    "nid": {"rich_text": {}},
}
BRIEFING_DB_PROPS = {
    "제목": {"title": {}},
    "날짜": {"date": {}},
    "리포트 수": {"number": {}},
    "목표가 상향": {"number": {}},
    "목표가 하향": {"number": {}},
    "헤드라인": {"rich_text": {}},
}


# ─────────────────────────── helpers ───────────────────────────
def pct(v):
    return None if v is None else round(v / 100, 4)  # Notion percent 포맷은 비율값


def group_of(r: dict) -> str:
    ai = r.get("ai") or {}
    if ai.get("sector_group"):
        return ai["sector_group"]
    return {"invest": "시장전략", "market": "시장전략", "economy": "매크로·경제",
            "debenture": "채권·금리"}.get(r["category"], r.get("sector") or "미분류")


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
def report_properties(r: dict) -> dict:
    ai = r.get("ai") or {}
    earnings = ai.get("earnings") or {}
    title = f"[{subject(r)}] {r['title']}" if r["category"] in ("company", "industry") else r["title"]
    return {
        "제목": B.p_title(title),
        "날짜": B.p_date(r["write_date"]),
        "구분": B.p_select(CATEGORY_KO.get(r["category"])),
        "섹터 대분류": B.p_select(group_of(r)),
        "업종": B.p_select(r.get("sector")),
        "종목": B.p_text(r.get("item_name")),
        "종목코드": B.p_text(r.get("item_code")),
        "시장": B.p_select(r.get("market")),
        "증권사": B.p_select(r["broker"]),
        "애널리스트": B.p_text(r.get("analyst")),
        "투자의견": B.p_select(r.get("opinion")),
        "목표가": B.p_number(r.get("goal_price")),
        "직전 목표가": B.p_number(r.get("prev_goal_price")),
        "목표가 변동": B.p_number(pct(r.get("goal_change_pct"))),
        "작성일 주가": B.p_number(r.get("price_at_write")),
        "상승여력": B.p_number(pct(r.get("upside_pct"))),
        "논조": B.p_select(ai.get("tone")),
        "컨센서스 대비": B.p_select(earnings.get("vs_consensus")),
        "특이사항": B.p_multi(r.get("flags")),
        "키워드": B.p_multi(ai.get("keywords")),
        "한줄요약": B.p_text(ai.get("one_line")),
        "원문 PDF": B.p_url(r.get("pdf_url")),
        "AI 요약": B.p_check(bool(ai)),
        "nid": B.p_text(r["nid"]),
    }


def report_blocks(r: dict) -> list:
    ai = r.get("ai")
    out = []
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
def report_line(r: dict, url: str) -> list:
    ai = r.get("ai") or {}
    head = f"{subject(r)} ({r['broker']})" if r["category"] == "company" else f"{r['title']} ({r['broker']})"
    rich = B.rt(head, bold=True, link=url)
    if r["category"] == "company" and r.get("goal_price"):
        chg = r.get("goal_change_pct")
        color = "red" if chg and chg > 0 else "blue" if chg and chg < 0 else "gray"
        rich += B.rt(f"  {r.get('opinion') or ''} {r['goal_price']:,}"
                     + (f" ({chg:+.1f}%)" if chg else ""), color=color)
    line = ai.get("one_line") or (r["title"] if r["category"] == "company" else "")
    if line:
        rich += B.rt(f"  ─ {line}")
    return rich


def briefing_blocks(date: str, reports: list, digest: dict, urls: dict) -> list:
    out = []
    counts = defaultdict(int)
    for r in reports:
        counts[r["category"]] += 1
    stats = " · ".join(f"{CATEGORY_KO[c]} {n}" for c, n in counts.items())
    if digest:
        out.append(B.callout(B.rt(digest["headline"], bold=True), "📌", "yellow_background"))
    out.append(B.para(B.rt(f"총 {len(reports)}건  ({stats})", color="gray")))

    if digest:
        out.append(B.h2("🔥 오늘의 테마"))
        for t in digest.get("themes", []):
            out.append(B.bullet(B.rt(t["title"] + " ─ ", bold=True) + B.rt(t["detail"])))
        if digest.get("divergence"):
            out.append(B.h3("⚖️ 증권사 간 시각 차이"))
            out.extend(B.bullets(digest["divergence"]))
        if digest.get("watch_list"):
            out.append(B.h3("👀 주목 종목"))
            out.extend(B.bullet(B.rt(w["name"] + ": ", bold=True) + B.rt(w["reason"]))
                       for w in digest["watch_list"])
        if digest.get("risks"):
            out.append(B.h3("⚠️ 공통 리스크"))
            out.extend(B.bullets(digest["risks"]))

    # 규칙 기반 하이라이트 (AI 없이도 항상 정확한 숫자)
    comp = [r for r in reports if r["category"] == "company"]
    up = sorted([r for r in comp if (r.get("goal_change_pct") or 0) > 0], key=lambda r: -r["goal_change_pct"])
    down = sorted([r for r in comp if (r.get("goal_change_pct") or 0) < 0], key=lambda r: r["goal_change_pct"])
    non_buy = [r for r in comp if r.get("opinion_type") and r["opinion_type"] not in ("buy", "strongbuy")]
    beat = [r for r in comp if ((r.get("ai") or {}).get("earnings") or {}).get("vs_consensus") == "상회"]
    miss = [r for r in comp if ((r.get("ai") or {}).get("earnings") or {}).get("vs_consensus") == "하회"]
    out.append(B.h2("🚩 하이라이트"))
    for title, rows in [("🔺 목표가 상향", up), ("🔻 목표가 하향", down), ("⚪ 매수 외 의견", non_buy),
                        ("📈 실적 컨센서스 상회", beat), ("📉 실적 컨센서스 하회", miss)]:
        if rows:
            out.append(B.toggle(f"{title} ({len(rows)})", [B.bullet(report_line(r, urls.get(rkey(r)))) for r in rows]))

    # 섹터별
    by_group = defaultdict(list)
    for r in reports:
        by_group[group_of(r)].append(r)
    order = [g for g in SECTOR_GROUPS if g not in MACRO_GROUPS] + sorted(
        g for g in by_group if g not in SECTOR_GROUPS)
    out.append(B.h2("🏭 섹터별"))
    for g in order:
        rows = sorted(by_group.get(g, []), key=lambda r: (r["category"] != "industry", subject(r)))
        if rows:
            out.append(B.toggle(f"{g} ({len(rows)})", [B.bullet(report_line(r, urls.get(rkey(r)))) for r in rows]))
    out.append(B.h2("🌐 시장 · 매크로 · 채권"))
    for g in MACRO_GROUPS:
        rows = by_group.get(g, [])
        if rows:
            out.append(B.toggle(f"{g} ({len(rows)})", [B.bullet(report_line(r, urls.get(rkey(r)))) for r in rows]))

    out.append(B.divider())
    out.append(B.h2("✍️ My View"))
    out.append(B.quote(B.rt("오늘 리포트를 읽고 든 생각, 동의하지 않는 부분, 추적할 포인트를 기록하세요.",
                            color="gray", italic=True)))
    return out


# ─────────────────────────── commands ───────────────────────────
def cmd_setup(nc: NotionClient, page: str) -> None:
    parent = parse_page_id(page)
    nc.get_page(parent)  # 연결(Connections) 안 됐으면 여기서 404
    rdb = nc.create_database(parent, "애널리스트 리포트", "📚", REPORT_DB_PROPS)
    bdb = nc.create_database(parent, "데일리 브리핑", "🗓️", BRIEFING_DB_PROPS)
    ids = {"parent_page_id": parent, "reports_db_id": rdb["id"], "briefings_db_id": bdb["id"]}
    save_json(IDS_FILE, ids)
    log.info("생성 완료 → %s\n  리포트 DB: %s\n  브리핑 DB: %s", IDS_FILE.name, rdb["url"], bdb["url"])


def cmd_publish(nc, date: str, dry_run: bool) -> int:
    summaries = load_json(DATA / "summaries" / f"{date}.json", [])
    raw = load_json(DATA / "reports" / f"{date}.json", [])
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
        save_json(out / "reports.json", [{"properties": report_properties(r), "children": report_blocks(r)}
                                         for r in reports])
        save_json(out / "briefing.json", briefing_blocks(date, reports, digest, {}))
        log.info("dry-run: payload 저장 → %s", out.relative_to(ROOT))
        return 0

    ids = load_json(IDS_FILE, None)
    if not ids:
        log.error("notion_ids.json 없음 ─ 먼저 --setup <페이지 링크> 실행")
        return 1
    rdb, bdb = ids["reports_db_id"], ids["briefings_db_id"]

    # 이미 게시된 페이지 조회 (재실행 시 중복 방지)
    existing = {}
    for p in nc.query_all(rdb, {"property": "날짜", "date": {"equals": date}}):
        nid = "".join(t["plain_text"] for t in p["properties"]["nid"]["rich_text"])
        cat = KO_TO_CAT.get((p["properties"]["구분"]["select"] or {}).get("name"))
        existing[f"{cat}-{nid}"] = (p["id"], p["url"], p["properties"]["AI 요약"]["checkbox"])

    urls, created, replaced, failed = {}, 0, 0, 0
    for r in reports:
        prev = existing.get(rkey(r))
        if prev and (prev[2] or not r.get("ai")):
            urls[rkey(r)] = prev[1]
            continue
        try:
            if prev:  # 요약 없이 올라갔던 페이지 → 요약본으로 교체
                nc.archive_page(prev[0])
                replaced += 1
            page = nc.create_page(rdb, report_properties(r), report_blocks(r),
                                  icon=CATEGORY_ICON.get(r["category"]))
            urls[rkey(r)] = page["url"]
            created += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.warning("✗ %s %s: %s", rkey(r), r["title"], e)
    log.info("리포트 게시: 신규 %d (교체 %d) / 기존 유지 %d / 실패 %d",
             created - replaced, replaced, len(reports) - created - failed, failed)

    # 데일리 브리핑: 같은 날짜 기존 페이지는 보관 처리 후 새로 생성
    for p in nc.query_all(bdb, {"property": "날짜", "date": {"equals": date}}):
        nc.archive_page(p["id"])
    comp = [r for r in reports if r["category"] == "company"]
    wd = "월화수목금토일"[datetime.strptime(date, "%Y-%m-%d").weekday()]
    page = nc.create_page(bdb, {
        "제목": B.p_title(f"{date} ({wd}) 리서치 브리핑"),
        "날짜": B.p_date(date),
        "리포트 수": B.p_number(len(reports)),
        "목표가 상향": B.p_number(sum(1 for r in comp if (r.get("goal_change_pct") or 0) > 0)),
        "목표가 하향": B.p_number(sum(1 for r in comp if (r.get("goal_change_pct") or 0) < 0)),
        "헤드라인": B.p_text((digest or {}).get("headline")),
    }, briefing_blocks(date, reports, digest, urls), icon="📰")
    log.info("브리핑 게시: %s", page["url"])
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description="Notion 게시")
    p.add_argument("--date", default=datetime.now(KST).strftime("%Y-%m-%d"))
    p.add_argument("--setup", metavar="PAGE_URL", help="최초 1회: 부모 페이지 아래 DB 생성")
    p.add_argument("--dry-run", action="store_true")
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
    return cmd_publish(nc, args.date, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
