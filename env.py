"""프로젝트 루트의 .env 파일을 환경변수로 로드 (이미 설정된 값은 덮어쓰지 않음).

키 값의 앞뒤 공백/줄바꿈도 제거한다 (GitHub Secret에 복사-붙여넣기 시 줄바꿈이 섞이는 경우 대비).
"""
from __future__ import annotations

import os
from pathlib import Path

SECRET_KEYS = ("ANTHROPIC_API_KEY", "NOTION_TOKEN")


def load_env(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    for key in SECRET_KEYS:
        if key in os.environ:
            os.environ[key] = os.environ[key].strip()
