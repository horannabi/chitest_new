# chitest_new (watch_popup)

`chitest`(watch.py)가 쓰던 메인 목록 페이지(`/Theater/MovieTable2`)는 결제/취소가
반영되는 데 수 분씩 걸리는 것이 2026-11-09 에 실측으로 확인됐다. 반면 예매
팝업이 쓰는 `/popup/ReserveMovieList` + `/popup/ReserveScreenPlan` 은 즉시
반영됐다.

이 저장소는 그 팝업 엔드포인트를 직접 불러서 감시하는 **완전히 분리된** 새
봇이다. `chitest`(watch.py)는 건드리지 않고 그대로 둔 채, 검증을 마칠 때까지
별도로 돌린다.

## 파일

- `parser.py` — 팝업 응답 파서. 실측 데이터(조조/더빙 뱃지가 낀 가장 까다로운
  경우 포함)로 검증됨.
- `logic.py` — 새 회차 발견 / 매진 해제 판단 로직. 네트워크와 분리해 7개
  시나리오로 검증됨.
- `watch_popup.py` — 메인 루프, 텔레그램, 상태 저장(`state_popup.json`),
  이어받기.

## 필요한 Secrets / Variables

| 이름 | 종류 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | Secret | 알림 받을 chat id |
| `KEYWORDS` | Secret | 쉼표 구분, 감시할 영화명 |
| `THEATER_CODE` | Variable | 극장 코드 (기본 1001) |
| `POLL_SEC` | Variable | 조회 간격 초 (기본 20) |
| `RELAY_AFTER_MIN` | Variable | 이어받기 요청 시점(분, 기본 120) |

`GITHUB_TOKEN` 은 Actions 가 자동으로 제공한다.
