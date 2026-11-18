"""Notion 블록/속성 JSON 빌더."""
from __future__ import annotations

from typing import Optional

TEXT_LIMIT = 2000  # rich_text 객체 1개당 글자 수 한도


def rt(text: str, bold: bool = False, link: Optional[str] = None,
       color: str = "default", italic: bool = False) -> list:
    text = text or ""
    out = []
    for i in range(0, max(len(text), 1), TEXT_LIMIT):
        t = {"content": text[i:i + TEXT_LIMIT]}
        if link:
            t["link"] = {"url": link}
        out.append({"type": "text", "text": t,
                    "annotations": {"bold": bold, "italic": italic, "color": color}})
    return out


def _block(kind: str, rich: list, **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich, **extra}}


def h2(text):            return _block("heading_2", rt(text))
def h3(text):            return _block("heading_3", rt(text))
def para(rich):          return _block("paragraph", rich if isinstance(rich, list) else rt(rich))
def bullet(rich):        return _block("bulleted_list_item", rich if isinstance(rich, list) else rt(rich))
def quote(rich):         return _block("quote", rich if isinstance(rich, list) else rt(rich))
def divider():           return {"object": "block", "type": "divider", "divider": {}}


def callout(rich, emoji: str = "💡", color: str = "gray_background") -> dict:
    rich = rich if isinstance(rich, list) else rt(rich)
    return _block("callout", rich, icon={"type": "emoji", "emoji": emoji}, color=color)


def toggle(title: str, children: list) -> dict:
    return _block("toggle", rt(title, bold=True), children=children)


def bullets(items) -> list:
    return [bullet(x) for x in (items or []) if x]


# ── 속성(property) 값 ──
def p_title(text):      return {"title": rt(text[:2000])}
def p_text(text):       return {"rich_text": rt(text) if text else []}
def p_number(v):        return {"number": v}
def p_date(d):          return {"date": {"start": d} if d else None}
def p_url(u):           return {"url": u or None}
def p_check(b):         return {"checkbox": bool(b)}


def _opt(name: str) -> str:
    # select 옵션 이름: 쉼표 불가, 100자 제한
    return str(name).replace(",", " ").strip()[:100]


def p_select(name):
    return {"select": {"name": _opt(name)} if name else None}


def p_multi(names):
    seen, out = set(), []
    for n in names or []:
        n = _opt(n)
        if n and n not in seen:
            seen.add(n)
            out.append({"name": n})
    return {"multi_select": out}
