"""1단계: 네이버 증권 애널리스트 리포트 수집기.

사용법:
    python collect.py                     # 오늘(KST) 리포트 수집
    python collect.py --date 2026-11-17   # 특정 날짜
    python collect.py --dry-run           # 필터 결과만 확인 (상세조회/PDF/상태저장 안 함)
    python collect.py --no-pdf            # PDF 다운로드 생략

출력:
    data/reports/YYYY-MM-DD.json    ← 2단계(AI 요약)의 입력
    data/pdfs/YYYY-MM-DD/*.pdf
    data/state.json                 ← 처리한 nid 기록 (중복 방지)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from collector.filters import matches
from collector.models import Report
from collector.naver_api import NaverResearchClient

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATE_FILE = DATA / "state.json"
SECTOR_CACHE = ROOT / "cache" / "sectors.json"  # 종목코드 → 업종 (git에 커밋해 재사용)
KST = timezone(timedelta(hours=9))

log = logging.getLogger("collect")


def rkey(r: dict) -> str:
    """리포트 고유 키. nid는 카테고리별 번호라 카테고리와 묶어야 유일함."""
    return f"{r['category']}-{r['nid']}"


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def pdf_filename(r: Report) -> str:
    subject = r.item_name or r.industry or r.category
    safe = "".join(c for c in f"{subject}_{r.broker}" if c not in '\\/:*?"<>|').strip()
    return f"{r.category}_{r.nid}_{safe}.pdf"


def lookup_sector(client: NaverResearchClient, cache: dict, item_code: str) -> dict:
    """종목 업종 조회 (캐시 우선). 업종은 거의 안 바뀌므로 한 번 조회하면 재사용."""
    if item_code not in cache:
        p = client.get_item_profile(item_code)
        cache[item_code] = {
            "name": p.get("itemName"),
            "sector": p.get("industryName"),
            "sector_code": p.get("industryCode"),
            "market": p.get("marketType"),
        }
    return cache[item_code]


def run(date: str, config: dict, dry_run: bool, download_pdf: bool) -> list:
    opts = config.get("collect", {})
    client = NaverResearchClient(delay_sec=float(opts.get("request_delay_sec", 1.0)))
    state = load_json(STATE_FILE, {})
    sectors = load_json(SECTOR_CACHE, {})
    out_file = DATA / "reports" / f"{date}.json"
    collected = load_json(out_file, [])  # 같은 날 재실행 시 이어붙이기
    pdf_dir = DATA / "pdfs" / date

    stats = {"listed": 0, "matched": 0, "new": 0, "failed": 0}

    for category, rule in (config.get("categories") or {}).items():
        if not rule or not rule.get("enabled"):
            continue
        seen = set(state.get(category, []))

        try:
            items = list(client.list_reports(category, date, date))
        except Exception as e:  # noqa: BLE001
            log.error("[%s] 목록 조회 실패: %s", category, e)
            stats["failed"] += 1
            continue

        targets = [it for it in items if matches(it, rule)]
        new = [it for it in targets if str(it["nid"]) not in seen]
        stats["listed"] += len(items)
        stats["matched"] += len(targets)
        log.info("[%s] 전체 %d건 → 필터 통과 %d건 → 신규 %d건",
                 category, len(items), len(targets), len(new))

        for it in new:
            label = f"{it.get('itemName') or ''} {it['title']}".strip()
            if dry_run:
                log.info("  · (%s) %s", it.get("brokerName"), label)
                continue
            try:
                detail = client.get_detail(category, str(it["nid"]))
                report = Report.from_api(category, it, detail)
                if report.item_code:
                    try:
                        info = lookup_sector(client, sectors, report.item_code)
                        report.sector, report.market = info["sector"], info["market"]
                    except Exception as e:  # noqa: BLE001 — 업종 실패는 치명적이지 않음
                        log.warning("  업종 조회 실패 %s: %s", report.item_code, e)

                if download_pdf and report.pdf_url:
                    pdf_dir.mkdir(parents=True, exist_ok=True)
                    dest = pdf_dir / pdf_filename(report)
                    if not dest.exists():
                        client.download(report.pdf_url, dest)
                    report.pdf_path = str(dest.relative_to(ROOT))

                collected.append(report.to_dict())
                seen.add(report.nid)
                stats["new"] += 1
                flags = f" [{', '.join(report.flags)}]" if report.flags else ""
                log.info("  ✓ (%s) %s%s", report.broker, label, flags)
            except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 막지 않도록
                stats["failed"] += 1
                log.warning("  ✗ nid=%s %s: %s", it.get("nid"), label, e)

        if not dry_run:
            state[category] = sorted(seen, key=int)[-5000:]  # 무한 증가 방지
            # 건별 저장: 중간에 죽어도 진행분 보존
            save_json(out_file, collected)
            save_json(STATE_FILE, state)
            save_json(SECTOR_CACHE, sectors)

    log.info("완료 %s ─ 조회 %d / 필터 %d / 신규 저장 %d / 실패 %d",
             date, stats["listed"], stats["matched"], stats["new"], stats["failed"])
    return collected


def main() -> int:
    p = argparse.ArgumentParser(description="네이버 증권 리포트 수집기")
    p.add_argument("--date", default=datetime.now(KST).strftime("%Y-%m-%d"))
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-pdf", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    download_pdf = config.get("collect", {}).get("download_pdf", True) and not args.no_pdf
    run(args.date, config, args.dry_run, download_pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
