# Naver Analyst Report Bot

매일 네이버 증권에 올라오는 증권사 애널리스트 리포트를 **전부 수집 → Claude로 PDF를 읽어 구조화 요약 → Notion에 섹터별로 정리**하는 자동화 파이프라인.

```
GitHub Actions (평일 10:30 KST 하루 1회 · 10:50 누락 대비 예비 실행)
   │
   ├─ 1. collect.py         네이버 증권 리서치 API → 6개 카테고리 전체 리포트 + PDF
   │                        종목 업종 조회, 목표가 변동률·상승여력 계산, 규칙 기반 특이사항 태깅
   │
   ├─ 2. summarize.py       Claude Message Batches API (50% 할인)
   │                        종목분석: Sonnet이 PDF 직접 판독 (표·차트 포함)
   │                        산업·전략·경제: Haiku / 시황·채권: 요약문 기반
   │                        JSON 스키마 고정 출력 → 하루치 종합 브리핑 생성
   │
   └─ 3. publish_notion.py  🗓️ 데일리 리포트 DB (날짜별 1행, 최신순)
                            └─ 11/18 (수) 데일리 리포트  (목록: 초간단 요약 표시)
                               ├─ 브리핑 (노란 헤드라인 · 건수 · 테마 · 🚩 하이라이트 표)
                               ├─ 🏭 섹터별 리포트 / 🌐 시장·매크로·채권 리포트 (인라인 DB, 1행 = 분석 페이지)
                               └─ ✍️ My View
```

## 요약 항목

| 종목분석 | 산업·전략·경제·시황·채권 |
|---|---|
| 한 줄 요약, 투자 논지, 실적 vs 컨센서스, 추정치 변경, 밸류에이션 근거, 목표가 변경 이유, 촉매, 리스크, 특이사항, 논조 | 한 줄 요약, 핵심 내용, 방향성 의견, Top Picks, 언급 종목, 리스크, 특이사항, 논조 |

모든 리포트는 공통 섹터 대분류(20개)로 분류되어 섹터별로 묶입니다.

## 설계 포인트
- **환각 억제**: 리포트에 없는 숫자는 null, 사실과 애널리스트 판단을 구분하도록 지시. 목표가·직전 목표가·작성일 주가는 AI가 아닌 API 원값으로 계산.
- **비용 통제**: 카테고리별 모델/페이지 수 라우팅 + 배치 API로 하루 약 $0.8 (82건 기준). 건별 실제 비용 기록, 예산 초과 시 경고.
- **멱등성**: 같은 날 재실행하면 그날 페이지를 새로 만들어 교체 (Notion 중복 없음). 배치 제출 후 중단돼도 재실행 시 결과만 이어서 수거.
- **장애 격리**: 한 건 실패가 전체를 멈추지 않음. 요약 실패 시에도 수집 데이터는 게시.

## 실행
```bash
uv venv --python 3.12 .venv && uv pip install -r requirements.txt
cp .env.example .env    # ANTHROPIC_API_KEY, NOTION_TOKEN 입력

python publish_notion.py --setup <Notion 페이지 링크>   # 최초 1회
python collect.py                # 1. 수집
python summarize.py --estimate   #    예상 비용 확인 (무료)
python summarize.py              # 2. 요약
python publish_notion.py         # 3. 게시
```
설정은 [config.yaml](config.yaml) (수집 카테고리, 카테고리별 모델·페이지 수, 일 예산).

## 유의사항
리포트 원문의 저작권은 각 증권사에 있습니다. 본 프로젝트는 개인 학습·리서치 목적이며 원문 PDF를 재배포하지 않고 링크만 보관합니다.
