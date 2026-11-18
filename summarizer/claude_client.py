"""Claude API 호출: PDF(또는 요약문) → 구조화된 JSON 요약.

요청 생성(build_params)과 응답 해석(parse_message)을 분리해서
동기 호출(sync)과 배치 API(batch, 50% 할인) 양쪽에서 같은 로직을 쓴다.
"""
from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
from pypdf import PdfReader, PdfWriter

from .prompts import SYSTEM_PROMPT, build_user_text, schema_for

# 1M 토큰당 USD (입력, 출력) ─ 비용 계산용. 배치는 50% 할인.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
# 서버측 거절 대체(fallbacks)를 지원하는 모델 (동기 호출에서만 사용 가능)
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
MAX_PDF_BYTES = 24 * 1024 * 1024
MAX_TOKENS = 16000


class SummarizeError(Exception):
    pass


@dataclass
class Route:
    """카테고리별 처리 방식 (config.yaml summarize.routes)."""
    model: str
    source: str = "pdf"          # pdf | text
    max_pages: int = 20
    effort: str = "low"


@dataclass
class SummaryResult:
    data: dict
    model: str
    input_tokens: int
    output_tokens: int
    batch: bool
    pages_total: Optional[int] = None
    pages_sent: Optional[int] = None

    @property
    def cost_usd(self) -> float:
        p_in, p_out = PRICES.get(self.model, (0.0, 0.0))
        cost = (self.input_tokens * p_in + self.output_tokens * p_out) / 1e6
        return cost * (0.5 if self.batch else 1.0)


def load_pdf(path: Path, max_pages: int):
    """PDF 바이트 반환. max_pages 초과 시 앞부분만 잘라서 보냄 (결과에 기록됨)."""
    raw = path.read_bytes()
    reader = PdfReader(io.BytesIO(raw))
    total = len(reader.pages)
    if total <= max_pages and len(raw) <= MAX_PDF_BYTES:
        return raw, total, total
    writer = PdfWriter()
    for page in reader.pages[:max_pages]:
        writer.add_page(page)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    if len(data) > MAX_PDF_BYTES:
        raise SummarizeError(f"PDF가 너무 큼 ({len(data) / 1e6:.1f}MB, {max_pages}p 기준)")
    return data, total, min(total, max_pages)


def build_params(report: dict, route: Route, root: Path):
    """Messages API 요청 파라미터와 (전체 페이지, 보낸 페이지) 반환."""
    content, pages = [], (None, None)
    text_only = route.source != "pdf" or not report.get("pdf_path")
    if not text_only:
        pdf_path = root / report["pdf_path"]
        if not pdf_path.exists():
            raise SummarizeError(f"PDF 파일 없음: {report['pdf_path']}")
        pdf, total, sent = load_pdf(pdf_path, route.max_pages)
        pages = (total, sent)
        content.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(pdf).decode("ascii")},
        })
    user_text = build_user_text(report, text_only)
    if pages[0] and pages[1] < pages[0]:
        user_text += f"\n(참고: 전체 {pages[0]}페이지 중 앞 {pages[1]}페이지만 첨부됨)"
    content.append({"type": "text", "text": user_text})

    params = {
        "model": route.model,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "effort": route.effort,
            "format": {"type": "json_schema", "schema": schema_for(report["category"])},
        },
    }
    return params, pages


def parse_message(msg, batch: bool, pages=(None, None)) -> SummaryResult:
    if msg.stop_reason == "refusal":
        cat = msg.stop_details.category if getattr(msg, "stop_details", None) else None
        raise SummarizeError(f"모델이 요약을 거절함 (category={cat})")
    if msg.stop_reason == "max_tokens":
        raise SummarizeError("출력이 max_tokens에서 잘림")
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        raise SummarizeError("응답에 텍스트 블록이 없음")
    u = msg.usage
    return SummaryResult(
        data=json.loads(text),
        model=msg.model,
        input_tokens=(u.input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        + (u.cache_read_input_tokens or 0),
        output_tokens=u.output_tokens or 0,
        batch=batch,
        pages_total=pages[0],
        pages_sent=pages[1],
    )


def summarize_sync(client: anthropic.Anthropic, params: dict, pages) -> SummaryResult:
    if params["model"] in FALLBACK_MODELS:
        # 안전 분류기가 거절하면 서버가 다른 모델로 자동 재시도
        msg = client.beta.messages.create(
            **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        msg = client.messages.create(**params)
    return parse_message(msg, batch=False, pages=pages)
