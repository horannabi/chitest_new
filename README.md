# chitest_new (watch_popup)

`chitest`(watch.py)가 쓰던 메인 목록 페이지(`/Theater/MovieTable2`)는 결제/취소가
반영되는 데 수 분씩 걸리는 것이 2026-11-09 에 실측으로 확인됐다. 반면 예매
팝업이 쓰는 `/popup/ReserveMovieList` + `/popup/ReserveScreenPlan` 은 즉시
반영됐다 (좌석 취소 직후 팝업 146 vs 목록 145, 좌석 예매 직후 팝업 138 vs
목록 140 - 둘 다 팝업이 먼저 맞았다).

이 저장소는 `watch.py` 를 포크해서, **시간표를 가져오는 부분만** 팝업
엔드포인트로 바꾼 것이다. `chitest`(watch.py)는 건드리지 않고 그대로 둔다.

## watch.py 와 비교

| | watch.py | watch_popup.py |
|---|---|---|
| 시간표 소스 | `/Theater/MovieTable2` (메인 목록) | `/popup/ReserveMovieList` + `/popup/ReserveScreenPlan` |
| 잔여석 | 목록 숫자 그대로, 매진/5석 이하만 좌석 배치도로 재확인(`seats.py`) | 팝업이 이미 정확한 숫자를 줘서 좌석 배치도 재확인이 불필요 - 그래서 `seats.py` 가 없다 |
| 직접 접속 막히면 | `r.jina.ai` 로 우회 | **우회 없음** - 우회로는 GET 렌더링 전용이라 이 두 엔드포인트처럼 POST 가 필요한 호출은 못 넘긴다 |
| 그 외 전부 (State/Telegram/명령/Health/Watchdog/`/ok` 반복/`/seatwatch`/`/trace`/회차 ID 이상 검증/이어받기) | 동일 | **코드 그대로** (`bot.py`, `health.py` 는 byte 단위로 동일하게 복사) |

## 파일

- `parser.py` — 팝업 두 엔드포인트의 응답 파서. `to_items()` 가 watch.py 의
  `parse_html()` 과 완전히 같은 모양(`[{name, groups:[{name, slots:[...]}]}]`)
  으로 변환해 주기 때문에, 그 아래 로직(`match_keywords`, `slot_key`,
  `observe_slots`, `raise_alert` 등)을 전부 그대로 가져올 수 있었다.
- `watch_popup.py` — `watch.py` 의 포크. `Fetcher`/`parse`/`contains_keyword`/
  `check_seats` 만 새로 짰고, 나머지(Config 나머지 필드, main() 전체 흐름,
  명령 처리 연결)는 동일하다.
- `bot.py`, `health.py` — `chitest` 의 것과 완전히 동일(State, Telegram,
  handle_command, Health, Watchdog). 데이터 소스와 무관한 부분이라 그대로
  복사했다.

검증: 실제로 받았던 응답(조조+더빙 뱃지가 낀 가장 까다로운 회차 포함)을
픽스처로 저장해 파싱부터 `observe_slots`/`check_seats`/매진 해제 감지/신규
회차 감지까지 네트워크 없이 로컬에서 통과를 확인한 뒤 올렸다.

## 필요한 Secrets / Variables

watch.yml 과 거의 동일하되, `TARGET_URL` 대신 `THEATER_CODE` 를 쓴다.

| 이름 | 종류 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Secret | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | Secret | 알림 받을 chat id |
| `KEYWORDS` | Secret | 쉼표 구분, 감시할 영화명 |
| `SITE_URL` | Secret (선택) | 알림에 같이 보낼 사람이 볼 주소 |
| `HEALTHCHECK_URL` | Secret (선택) | healthchecks.io 등 외부 감시 ping 주소 |
| `THEATER_CODE` | Variable | 극장 코드 (기본 1001) |
| `POLL_SEC`, `RELAY_AFTER_MIN`, `STALL_SEC` 등 | Variable | watch.yml 과 동일한 기본값 |

`GITHUB_TOKEN` 은 Actions 가 자동으로 제공한다.
