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

**일부 데이터센터 IP는 통째로 차단됩니다.** Oracle Cloud 도쿄에서는 홈페이지까지 403이었고
(같은 서버에서 네이버는 정상), GitHub Actions와 Cloudflare Workers에서는 200이었습니다.
막히는 곳에서는 Cloudflare Worker를 프록시로 두고 아래 환경변수로 경유시킵니다.

```bash
CGV_HOST=<worker>.workers.dev
CGV_API_PREFIX=/p/api/v1
CGV_PROXY_KEY=<공유키>
```

실측상 오라클 도쿄 → Worker(NRT) → CGV 왕복이 104ms로, 서울에서 직접 부르는 84ms와 큰 차이가 없습니다.
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

## 서버에 상시 실행 (권장)

리눅스 서버(Oracle Cloud 무료 티어 등)에서 한 줄이면 됩니다.

```bash
curl -fsSL https://raw.githubusercontent.com/YCYEOM/movie-alert/main/setup.sh \
  | sudo bash -s -- '<디스코드 웹훅 URL>'
```

전용 계정으로 systemd 서비스를 등록하고 바로 켭니다. 죽으면 systemd가 5초 뒤 되살립니다.

```bash
journalctl -u movie-alert -f          # 로그
systemctl restart movie-alert         # 재시작
sudo vi /etc/movie-alert.env          # 웹훅/토큰 변경
```

상태 파일은 `/var/lib/movie-alert/state.json`에 둡니다. 저장소 안에 두면 `git pull`이 깨집니다.

### 하트비트

하루 한 번 감시 현황을 보냅니다.

```
🟢 감시 중
용산아이파크몰 168건 · 09/08까지
여의도 28건 · 09/01까지
```

**이게 안 오면 감시가 죽은 것입니다.** 오라클 무료 인스턴스는 7일간 CPU·네트워크 사용률이
20% 미만이면 회수될 수 있는데([공식 문서](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)),
이 프로그램은 그 조건에 정확히 해당합니다. 조용히 사라지는 걸 알아채기 위한 장치입니다.

`heartbeat_hours`를 `0`으로 두면 끕니다. 부재를 직접 알아차려야 하는 게 부담이면
[healthchecks.io](https://healthchecks.io) 같은 데드맨 스위치를 붙이는 편이 확실합니다 —
핑이 끊기면 그쪽에서 먼저 알려줍니다.

### 감지 속도

서울에서 잰 값입니다. 연결을 재사용하면 요청당 84ms이고, 빠른 감시 1주기(6극장 × 2일 = 12요청)가 약 0.8초입니다.

| 설정 | 1주기 | 최대 감지 지연 | CGV 요청 |
|---|---|---|---|
| `fast_seconds: 3` (기본) | 3.8초 | **약 4초** | 3.3 req/s |
| `fast_seconds: 10` | 10.8초 | 약 11초 | 1.2 req/s |

단, 30분마다 도는 전체 스윕이 약 53초 걸리고 그동안은 빠른 감시가 멈춥니다.
전체 시간의 2.9%는 감지 지연이 최대 53초라는 뜻입니다. `poll_seconds`를 늘리면
노출이 더 줄지만, 이미 열린 날짜의 회차 추가를 놓칠 창이 길어집니다.

`fast_seconds`를 줄이면 빨라지지만 CGV 서버 부담이 커집니다. 실측으로 지속 3 req/s까지는 429가 나지 않았고, 무거운 전체 스윕(211KB × 126요청)을 연달아 돌렸을 때만 429가 났습니다.

## GitHub Actions로 실행 (대안)

`.github/workflows/watch.yml`이 5분마다 `--once`를 돌리고, 갱신된 `state.json`을 저장소에 다시 커밋합니다.
서버가 필요 없고 공개 저장소라 실행 시간도 무제한입니다.

알림 채널은 저장소 Secrets에 넣습니다 (Settings → Secrets and variables → Actions).

```bash
gh secret set DISCORD_WEBHOOK_URL   # 붙여넣기 프롬프트가 뜹니다
```

`TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`도 같은 방식이며, 설정한 채널로만 보냅니다.

### 한계

**GitHub의 cron은 정시 보장이 없습니다.** 정시(`:00`, `:05`)에 부하가 몰려 10~20분 늦거나 아예
건너뛰는 일이 흔합니다. 초 단위 경쟁에는 맞지 않습니다.

**서버와 동시에 돌리지 마세요.** 상태 파일이 따로 놀아서 같은 알림이 두 번 옵니다.
서버로 옮긴 뒤에는 `gh workflow disable watch.yml`로 꺼주세요.
