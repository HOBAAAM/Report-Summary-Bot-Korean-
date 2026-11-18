"""네이버 증권 리서치 API 클라이언트.

stock.naver.com 프론트엔드가 사용하는 JSON API를 호출한다.
(구 finance.naver.com/research/*.naver 페이지는 2026년 stock.naver.com으로 리다이렉트됨)

  목록 : GET /api/stockSecurity/researches/v2/{category}?startDate=&endDate=&index=&size=
         - index = 페이지 번호(0부터), 응답의 hasNext로 다음 페이지 여부 판단
  상세 : GET /api/stockSecurity/researches/v2/{category}/{nid}/detail-page
         - attachUrl(PDF), prevGoalPrice, priceAtWriteDate 등 목록에 없는 필드 포함
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

BASE_URL = "https://stock.naver.com/api/stockSecurity/researches/v2"
CATEGORIES = ("company", "industry", "invest", "economy", "market", "debenture")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


class NaverResearchClient:
    def __init__(self, delay_sec: float = 1.0, timeout: int = 20):
        self.delay_sec = delay_sec
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": USER_AGENT, "Referer": "https://stock.naver.com/research"}
        )
        retry = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = self.delay_sec - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, url: str, **params) -> requests.Response:
        self._throttle()
        resp = self.session.get(url, params=params or None, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    def list_reports(
        self, category: str, start_date: str, end_date: str, page_size: int = 50
    ) -> Iterator[dict]:
        """기간 내 리포트 목록을 페이지를 넘기며 전부 yield."""
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        index = 0
        while True:
            data = self._get(
                f"{BASE_URL}/{category}",
                startDate=start_date,
                endDate=end_date,
                index=index,
                size=page_size,
            ).json()
            items = data.get("items") or []
            log.debug("%s page %d: %d items", category, index, len(items))
            yield from items
            if not data.get("hasNext") or not items:
                break
            index += 1

    def get_detail(self, category: str, nid: str) -> dict:
        data = self._get(f"{BASE_URL}/{category}/{nid}/detail-page").json()
        return data.get("researchContent") or {}

    def get_item_profile(self, item_code: str) -> dict:
        """종목 프로필 (industryName = 리포트 상세 화면의 기업명 아래 업종 분류)."""
        return self._get(
            f"https://stock.naver.com/api/stockSecurity/items/v2/domestic/{item_code}/profile"
        ).json()

    def download(self, url: str, dest, attempts: int = 3) -> int:
        """PDF를 dest 경로로 저장하고 바이트 수 반환.

        CDN이 오류 페이지(HTML)를 200으로 돌려주는 경우가 있어 내용이 실제 PDF인지 확인하고,
        아니면 간격을 두고 재시도한다. 끝내 실패하면 ValueError.
        """
        tmp = dest.with_suffix(dest.suffix + ".part")
        last = ""
        for i in range(attempts):
            if i:
                time.sleep(3 * i)
            self._throttle()
            with self.session.get(url, timeout=60, stream=True) as resp:
                resp.raise_for_status()
                size = 0
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(64 * 1024):
                        f.write(chunk)
                        size += len(chunk)
            with open(tmp, "rb") as f:
                head = f.read(1024)
            if b"%PDF" in head:
                tmp.replace(dest)
                return size
            last = head[:60].decode("utf-8", "replace").replace("\n", " ")
        tmp.unlink(missing_ok=True)
        raise ValueError(f"PDF가 아닌 응답 ({last!r})")
