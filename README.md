# CGV 예매 오픈 알리미

리뉴얼된 CGV(Next.js) 예매 API를 폴링해서, **새 회차가 생기면** Discord/Telegram으로 알립니다.
의존성 없음 — 파이썬 표준 라이브러리만 씁니다.

## 사용법

```bash
python3 cgv_alert.py --theaters   # 극장 목록과 site_no 출력
python3 cgv_alert.py --selftest   # 로직 + API 연결 확인
python3 cgv_alert.py --once       # 1회만 실행
python3 cgv_alert.py              # 상시 감시
```

`config.json`에서 감시 대상을 지정합니다.

```json
{ "name": "용산 특별관", "site_no": "0013", "screens": ["아이맥스", "4DX", "SCREENX"] }
```

`screens`를 `[]`로 두면 일반관이 아닌 모든 상영관(프리미엄관·아트하우스 등 포함)을 감시합니다.

토큰은 `config.json` 대신 환경변수로 넣는 편이 안전합니다.

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
# 또는
export TELEGRAM_TOKEN="..." TELEGRAM_CHAT_ID="..."
```

첫 실행은 기준선만 저장하고 알리지 않습니다(`state.json`). 이후 폴링부터 신규 회차만 알립니다.

## 동작 원리

```
GET https://cgv.co.kr/api/v1/booking/searchMovScnInfo
    ?coCd=A420&siteNo=0013&scnYmd=20260829&rtctlScopCd=01
```

오늘부터 `days_ahead`일까지 하루씩 조회해서 `(날짜, 상영관, 영화, 시작시각)` 집합을 만들고,
직전 스냅샷에 없던 항목만 알립니다. 예매가 열리면 그 날짜에 회차가 통째로 생기므로 그대로 잡힙니다.
사라진 회차는 로그도 알림도 남기지 않습니다.

**`Referer` 헤더가 필수입니다.** 없으면 Cloudflare WAF가 403을 돌려줍니다.
`accept-encoding: gzip`으로 응답이 211KB → 18KB로 줄어듭니다(하루치 기준).

## 알림 채널: Discord vs Telegram

**Discord를 권장합니다.** 한국에서 왕복 지연이 10배 이상 차이납니다 (2026-08-28 서울 실측, 5회 평균):

| | TCP 연결 | 첫 바이트(TTFB) |
|---|---|---|
| `discord.com` | **10ms** | **~50ms** |
| `api.telegram.org` | 230ms | ~690ms |

Discord는 Cloudflare 서울 PoP를 타고, Telegram은 한국에 접점이 없어 해외로 나갑니다.
예매 오픈 경쟁에서 0.6초는 의미 있는 차이입니다.

다만 **Discord는 채널 알림 설정을 "모든 메시지"로 바꾸지 않으면 모바일 푸시가 아예 오지 않습니다.**
이게 실질적으로 가장 큰 리스크입니다. Telegram은 봇 메시지가 기본으로 푸시되고 레이트 리밋도 관대하지만,
이 용도는 알림이 드물어 레이트 리밋은 어느 쪽이든 무관합니다.

둘 다 설정하면 둘 다 보냅니다.

## 주의

- CGV와 무관한 비공식 도구입니다. 공개된 경로만 조회합니다.
- 폴링 간격(`poll_seconds`, 기본 180초)을 무리하게 줄이지 마세요.
  요청 수는 `극장 수 × days_ahead`입니다. 기본 설정(6곳 × 21일)이면 한 주기에 126건, 약 60초 걸립니다.
- 같은 극장을 상영관별로 쪼개 여러 target으로 두면 같은 데이터를 중복으로 받습니다.
  극장당 하나로 두고 `screens`에 나열하세요 — 알림 문구에 상영관 이름이 이미 들어갑니다.

## GitHub Actions로 상시 실행

`.github/workflows/watch.yml`이 5분마다 `--once`를 돌리고, 갱신된 `state.json`을 저장소에 다시 커밋합니다.
서버가 필요 없고 공개 저장소라 실행 시간도 무제한입니다.

알림 채널은 저장소 Secrets에 넣습니다 (Settings → Secrets and variables → Actions).

```bash
gh secret set DISCORD_WEBHOOK_URL   # 붙여넣기 프롬프트가 뜹니다
```

`TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`도 같은 방식이며, 설정한 채널로만 보냅니다.

### 한계

**GitHub의 cron은 정시 보장이 없습니다.** `*/5`로 적어도 실제로는 10~20분 늦게 도는 일이 흔하고,
플랫폼이 붐비면 아예 건너뛰기도 합니다. 예매 오픈을 초 단위로 노린다면 항상 켜져 있는 기계에서
`python3 cgv_alert.py`를 직접 돌리는 쪽이 확실히 빠릅니다. Actions는 "놓치지 않는 것"에 맞는 선택입니다.
