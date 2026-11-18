"""처리할 날짜 출력: 인자가 있으면 그대로, 없으면 KST 기준 직전 영업일(주말 제외).

아침 실행 시 '전날 하루치'를 빠짐없이 모으기 위함 (월요일 → 금요일분).
공휴일은 리포트가 0건이라 자연스럽게 아무것도 게시되지 않음.
"""
import sys
from datetime import datetime, timedelta, timezone

if len(sys.argv) > 1 and sys.argv[1]:
    print(sys.argv[1])
else:
    d = datetime.now(timezone(timedelta(hours=9))) - timedelta(days=1)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    print(d.strftime("%Y-%m-%d"))
