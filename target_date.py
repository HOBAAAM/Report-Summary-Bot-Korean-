"""처리할 날짜 출력: 인자가 있으면 그대로, 없으면 KST 기준 오늘.

평일 10:30 KST 실행 시 그날 올라온 리포트를 정리.
공휴일은 리포트가 0건이라 자연스럽게 아무것도 게시되지 않음.
"""
import sys
from datetime import datetime, timedelta, timezone

if len(sys.argv) > 1 and sys.argv[1]:
    print(sys.argv[1])
else:
    print(datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d"))
