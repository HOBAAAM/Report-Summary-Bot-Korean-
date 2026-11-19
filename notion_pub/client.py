"""최소한의 Notion REST API 클라이언트 (rate limit 대응 포함)."""
from __future__ import annotations

import logging
import re
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_CHILDREN = 100  # 요청 1회당 블록 수 한도


def parse_page_id(url_or_id: str) -> str:
    """노션 링크나 ID에서 32자리 page id 추출."""
    m = re.findall(r"[0-9a-f]{32}", url_or_id.replace("-", "").lower())
    if not m:
        raise ValueError(f"page id를 찾을 수 없음: {url_or_id}")
    h = m[-1]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class NotionClient:
    def __init__(self, token: str, min_interval: float = 0.35):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.min_interval = min_interval  # 공식 한도: 평균 초당 3회
        self._last = 0.0

    def request(self, method: str, path: str, json=None) -> dict:
        for attempt in range(6):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            r = self.s.request(method, f"{API}{path}", json=json, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", 2 ** attempt))
                log.debug("notion %s → %s, %.1fs 후 재시도", path, r.status_code, delay)
                time.sleep(delay)
                continue
            if not r.ok:
                raise RuntimeError(f"Notion {method} {path} {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError(f"Notion {method} {path}: 재시도 한도 초과")

    # ── 편의 메서드 ──
    def get_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def create_database(self, parent_page_id: str, title: str, icon: str, properties: dict) -> dict:
        return self.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "is_inline": True,
            "icon": {"type": "emoji", "emoji": icon},
            "title": [{"type": "text", "text": {"content": title}}],
            "properties": properties,
        })

    def query_all(self, database_id: str, filter_: dict) -> list:
        results, cursor = [], None
        while True:
            body = {"filter": filter_, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self.request("POST", f"/databases/{database_id}/query", body)
            results += data["results"]
            if not data.get("has_more"):
                return results
            cursor = data["next_cursor"]

    def create_page(self, database_id: str, properties: dict, children: list, icon: str = None) -> dict:
        return self._create({"database_id": database_id}, properties, children, icon)

    def create_child_page(self, parent_page_id: str, title: list, children: list, icon: str = None) -> dict:
        """일반 페이지 아래 하위 페이지 생성 (부모 페이지 본문 끝에 링크로 붙음)."""
        return self._create({"page_id": parent_page_id}, {"title": title}, children, icon)

    def _create(self, parent: dict, properties: dict, children: list, icon: str = None) -> dict:
        body = {"parent": parent, "properties": properties, "children": children[:MAX_CHILDREN]}
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        page = self.request("POST", "/pages", body)
        self.append_children(page["id"], children[MAX_CHILDREN:])
        return page

    def list_children(self, block_id: str) -> list:
        results, cursor = [], None
        while True:
            q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self.request("GET", f"/blocks/{block_id}/children{q}")
            results += data["results"]
            if not data.get("has_more"):
                return results
            cursor = data["next_cursor"]

    def append_children(self, block_id: str, children: list, after: str = None) -> list:
        """블록 추가. after를 주면 그 블록 바로 뒤에 순서대로 삽입. 추가된 최상위 블록 목록 반환."""
        added = []
        for i in range(0, len(children), MAX_CHILDREN):
            body = {"children": children[i:i + MAX_CHILDREN]}
            if after:
                body["after"] = after
            res = self.request("PATCH", f"/blocks/{block_id}/children", body)["results"]
            added += res
            if after and res:
                after = res[-1]["id"]
        return added

    def update_database(self, database_id: str, body: dict) -> dict:
        return self.request("PATCH", f"/databases/{database_id}", body)

    def archive_page(self, page_id: str) -> None:
        self.request("PATCH", f"/pages/{page_id}", {"archived": True})
