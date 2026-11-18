"""프로젝트 루트의 .env 파일을 환경변수로 로드 (이미 설정된 값은 덮어쓰지 않음)."""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path = Path(__file__).resolve().parent / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
