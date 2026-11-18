"""2단계: 수집된 리포트를 Claude로 요약.

사용법:
    python summarize.py                    # 오늘(KST) 수집분 요약 (config의 mode: batch|sync)
    python summarize.py --date 2026-11-18
    python summarize.py --sync --limit 3   # 테스트: 3건만 즉시 요약
    python summarize.py --estimate         # API 호출 없이 예상 비용만 계산
    python summarize.py --delete-pdfs      # 요약 성공한 PDF 삭제 (자동화 환경 용량 절약)

입력:  data/reports/DATE.json   (collect.py 결과)
출력:  data/summaries/DATE.json (리포트 + "ai" 요약 필드) ← 3단계(Notion)의 입력
       data/summaries/DATE.digest.json (데일리 브리핑)
배치 모드는 제출한 batch id를 data/summaries/DATE.batches.json에 기록 →
중간에 끊겨도 재실행하면 다시 제출하지 않고 결과만 이어서 받음.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import anthropic
import yaml

from collect import DATA, KST, ROOT, load_json, rkey, save_json
from env import load_env
from summarizer.claude_client import (PRICES, Route, SummarizeError, build_params,
                                      parse_message, summarize_sync)
from summarizer.digest import make_digest

load_env()
log = logging.getLogger("summarize")
BATCH_MAX_BYTES = 150 * 1024 * 1024   # 배치 1개 한도 256MB → 여유 있게 분할
TOKENS_PER_PDF_PAGE = 3200            # 예상 비용 계산용 (텍스트 + 페이지 이미지)


class Run:
    def __init__(self, date: str, cfg: dict, reports: list):
        self.date = date
        self.cfg = cfg
        self.reports = reports
        self.routes = {c: Route(**r) for c, r in (cfg.get("routes") or {}).items()}
        self.out_file = DATA / "summaries" / f"{date}.json"
        self.batch_file = DATA / "summaries" / f"{date}.batches.json"
        self.digest_file = DATA / "summaries" / f"{date}.digest.json"
        self.done = {rkey(r): r for r in load_json(self.out_file, []) if r.get("ai")}
        self.cost = 0.0
        self.ok = self.fail = 0
        self.client = anthropic.Anthropic(max_retries=4)

    def todo(self) -> list:
        return [r for r in self.reports if rkey(r) not in self.done and r["category"] in self.routes]

    def save(self) -> None:
        save_json(self.out_file, [self.done[rkey(r)] for r in self.reports if rkey(r) in self.done])

    def record(self, r: dict, res, delete_pdf: bool) -> None:
        self.done[rkey(r)] = {**r, "ai": res.data, "ai_meta": {
            "model": res.model,
            "mode": "batch" if res.batch else "sync",
            "source": "pdf" if res.pages_total else "text",
            "pages_total": res.pages_total,
            "pages_sent": res.pages_sent,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
            "cost_usd": round(res.cost_usd, 5),
            "summarized_at": datetime.now(KST).isoformat(timespec="seconds"),
        }}
        self.ok += 1
        self.cost += res.cost_usd
        label = f"[{r['category']}] {r.get('item_name') or r.get('sector') or ''} {r['title']}".strip()
        log.info("✓ %s ─ %s", label[:60], res.data.get("one_line", ""))
        if delete_pdf and r.get("pdf_path"):
            (ROOT / r["pdf_path"]).unlink(missing_ok=True)

    # ── 예상 비용 ──
    def estimate(self, todo: list) -> float:
        from pypdf import PdfReader
        total = 0.0
        for r in todo:
            route = self.routes[r["category"]]
            tin = 1500 + len(r.get("summary_text") or "") * 2
            if route.source == "pdf" and r.get("pdf_path"):
                try:
                    pages = min(len(PdfReader(ROOT / r["pdf_path"]).pages), route.max_pages)
                    tin = pages * TOKENS_PER_PDF_PAGE + 1500
                except Exception:  # noqa: BLE001 — 깨진 PDF는 요약문 기준으로 추정
                    pass
            tout = 1500 if route.effort == "low" else 3000
            p_in, p_out = PRICES[route.model]
            total += (tin * p_in + tout * p_out) / 1e6
        return total * (0.5 if self.cfg.get("mode") == "batch" else 1.0)

    # ── 동기 모드 ──
    def run_sync(self, todo: list, delete_pdf: bool) -> None:
        def work(r):
            params, pages = build_params(r, self.routes[r["category"]], ROOT)
            return summarize_sync(self.client, params, pages)

        with ThreadPoolExecutor(max_workers=int(self.cfg.get("max_workers", 4))) as pool:
            futures = {pool.submit(work, r): r for r in todo}
            for fut in as_completed(futures):
                r = futures[fut]
                try:
                    self.record(r, fut.result(), delete_pdf)
                    self.save()  # 건별 저장
                except Exception as e:  # noqa: BLE001
                    self.fail += 1
                    log.warning("✗ %s %s: %s", rkey(r), r["title"][:40], e)

    # ── 배치 모드 ──
    def submit_batches(self, todo: list) -> list:
        chunks, cur, size = [], [], 0
        pages_by_key = {}
        for r in todo:
            try:
                params, pages = build_params(r, self.routes[r["category"]], ROOT)
            except SummarizeError as e:
                self.fail += 1
                log.warning("✗ %s: %s", rkey(r), e)
                continue
            req = {"custom_id": rkey(r), "params": params}
            n = len(json.dumps(req))
            if cur and size + n > BATCH_MAX_BYTES:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(req)
            size += n
            pages_by_key[rkey(r)] = pages
        if cur:
            chunks.append(cur)

        submitted = []
        for reqs in chunks:
            b = self.client.messages.batches.create(requests=reqs)
            submitted.append({"id": b.id, "keys": [q["custom_id"] for q in reqs]})
            log.info("배치 제출 %s (%d건)", b.id, len(reqs))
        state = {"batches": submitted, "pages": pages_by_key}
        save_json(self.batch_file, state)
        return state

    def collect_batches(self, state: dict, max_wait: int, delete_pdf: bool) -> bool:
        """완료될 때까지 대기 후 결과 반영. 시간 초과면 False (재실행 시 이어서)."""
        by_key = {rkey(r): r for r in self.reports}
        pending = [b for b in state["batches"] if not b.get("collected")]
        deadline = time.monotonic() + max_wait
        for b in pending:
            while True:
                info = self.client.messages.batches.retrieve(b["id"])
                if info.processing_status == "ended":
                    break
                if time.monotonic() > deadline:
                    log.warning("대기 시간 초과 ─ 배치 %s 아직 처리 중. 나중에 다시 실행하면 이어서 받습니다.", b["id"])
                    return False
                c = info.request_counts
                log.info("배치 %s 처리 중… (완료 %d / 진행 %d)", b["id"][-8:], c.succeeded + c.errored, c.processing)
                time.sleep(30)

            for item in self.client.messages.batches.results(b["id"]):
                r = by_key.get(item.custom_id)
                if r is None:
                    continue
                res_type = item.result.type
                if res_type != "succeeded":
                    self.fail += 1
                    detail = getattr(getattr(item.result, "error", None), "error", None)
                    log.warning("✗ %s: 배치 결과 %s %s", item.custom_id, res_type, detail or "")
                    continue
                try:
                    pages = tuple(state["pages"].get(item.custom_id) or (None, None))
                    self.record(r, parse_message(item.result.message, batch=True, pages=pages), delete_pdf)
                except Exception as e:  # noqa: BLE001
                    self.fail += 1
                    log.warning("✗ %s: %s", item.custom_id, e)
            b["collected"] = True
            self.save()
            save_json(self.batch_file, state)
        self.batch_file.unlink(missing_ok=True)  # 모두 수거 완료
        return True

    def write_digest(self) -> None:
        dcfg = self.cfg.get("digest") or {}
        try:
            digest = make_digest(list(self.done.values()), dcfg.get("model", "claude-sonnet-5-5"),
                                 dcfg.get("effort", "medium"))
            save_json(self.digest_file, digest)
            log.info("데일리 브리핑 생성: %s", digest["headline"])
        except Exception as e:  # noqa: BLE001 — 브리핑 실패가 개별 요약을 무효화하진 않음
            log.warning("데일리 브리핑 생성 실패: %s", e)


def main() -> int:
    p = argparse.ArgumentParser(description="리포트 AI 요약")
    p.add_argument("--date", default=datetime.now(KST).strftime("%Y-%m-%d"))
    p.add_argument("--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--limit", type=int, help="최대 요약 건수 (테스트용, 브리핑 생략)")
    p.add_argument("--sync", action="store_true", help="config와 무관하게 즉시 호출 모드")
    p.add_argument("--estimate", action="store_true", help="예상 비용만 계산")
    p.add_argument("--max-wait", type=int, default=3 * 3600, help="배치 최대 대기 초")
    p.add_argument("--delete-pdfs", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")).get("summarize", {})
    if args.sync:
        cfg["mode"] = "sync"
    reports = load_json(DATA / "reports" / f"{args.date}.json", None)
    if reports is None:
        log.error("수집 결과가 없습니다: data/reports/%s.json (collect.py 먼저 실행)", args.date)
        return 1

    run = Run(args.date, cfg, reports)
    state = load_json(run.batch_file, None)
    todo = run.todo()
    if args.limit:
        todo = todo[: args.limit]
    est = run.estimate(todo)
    log.info("%s ─ 전체 %d건, 요약 완료 %d건, 이번 대상 %d건 · 모드 %s · 예상 비용 $%.2f",
             args.date, len(reports), len(run.done), len(todo), cfg.get("mode"), est)
    if args.estimate:
        return 0

    if state:
        log.info("이전에 제출한 배치 결과를 이어서 받습니다.")
        if not run.collect_batches(state, args.max_wait, args.delete_pdfs):
            return 2
    elif todo:
        if cfg.get("mode") == "batch":
            state = run.submit_batches(todo)
            if state["batches"] and not run.collect_batches(state, args.max_wait, args.delete_pdfs):
                return 2
        else:
            run.run_sync(todo, args.delete_pdfs)

    log.info("요약 ─ 성공 %d / 실패 %d · 이번 실행 비용 $%.3f", run.ok, run.fail, run.cost)
    day_cost = sum((r.get("ai_meta") or {}).get("cost_usd", 0) for r in run.done.values())
    budget = float(cfg.get("daily_budget_usd", 0) or 0)
    if budget and day_cost > budget:
        log.warning("⚠️ %s 누적 비용 $%.2f 가 일 예산 $%.2f 를 넘었습니다. config.yaml routes 조정 필요",
                    args.date, day_cost, budget)

    # 브리핑: 새 요약이 생겼거나 아직 없으면 생성 (일부 실패분은 다음 실행에서 재시도)
    if not args.limit and run.done and (run.ok or not run.digest_file.exists()):
        run.write_digest()
    return 1 if run.fail and not run.ok else 0


if __name__ == "__main__":
    sys.exit(main())
