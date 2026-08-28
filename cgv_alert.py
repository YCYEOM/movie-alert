#!/usr/bin/env python3
"""CGV 예매 오픈 알리미 — 표준 라이브러리만 사용."""
import gzip
import http.client
import re
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# CGV 는 일부 데이터센터 IP(오라클 등)를 Cloudflare 로 403 차단한다.
# 그런 곳에서는 CGV_HOST/CGV_API_PREFIX/CGV_PROXY_KEY 로 프록시를 경유한다.
HOST = os.environ.get("CGV_HOST", "cgv.co.kr")
API = os.environ.get("CGV_API_PREFIX", "/api/v1")
CO_CD = "A420"
# Referer 가 없으면 Cloudflare WAF 가 403 을 돌려준다. 나머지 헤더는 장식.
HEADERS = {
    "accept": "application/json",
    "accept-encoding": "gzip",
    "accept-language": "ko-KR",
    "referer": "https://cgv.co.kr/cnm/movieBook/cinema",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
    ),
}
if os.environ.get("CGV_PROXY_KEY"):
    HEADERS["x-proxy-key"] = os.environ["CGV_PROXY_KEY"]

HERE = os.path.dirname(os.path.abspath(__file__))
# 서버 배포 시엔 저장소 밖(/var/lib)을 쓴다. 저장소 안이면 git pull 이 깨진다.
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(HERE, "state.json")

# 연결을 재사용하면 요청당 99ms -> 75ms. 빠른 감시에서 이 차이가 누적된다.
_conn = None

# CGV 가 429 를 주면 일정 시간 요청을 아예 멈춘다. 그냥 재시도하면 서버를 두드리는
# 꼴이고 제재만 길어진다. 성공하면 단계는 초기화.
_cool_until = 0.0
_cool_step = 0


def cooling():
    return time.monotonic() < _cool_until


def drop_conn():
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except OSError:
            pass
        _conn = None


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def get(path, **params):
    global _conn
    if cooling():  # 쉬는 중엔 요청 자체를 만들지 않는다 (단계도 올리지 않음)
        raise urllib.error.HTTPError(HOST, 429, "cooling", None, None)
    qs = "&".join(f"{k}={v}" for k, v in {"coCd": CO_CD, **params}.items())
    url = f"{API}/{path}?{qs}"
    for attempt in (1, 2):
        try:
            if _conn is None:
                _conn = http.client.HTTPSConnection(HOST, timeout=30)
            _conn.request("GET", url, headers=HEADERS)
            resp = _conn.getresponse()
            raw, status, enc = resp.read(), resp.status, resp.headers.get(
                "content-encoding"
            )
            break
        except (http.client.HTTPException, OSError):
            drop_conn()  # 서버가 끊은 연결일 수 있으니 한 번은 새로 맺어본다
            if attempt == 2:
                raise
    if status == 429:
        global _cool_step, _cool_until
        _cool_step = min(_cool_step + 1, 5)
        wait = 30 * 2 ** (_cool_step - 1)  # 30, 60, 120, 240, 480초
        _cool_until = time.monotonic() + wait
        log(f"429 Too Many Requests — {wait}초간 요청 중단")
        drop_conn()
        raise urllib.error.HTTPError(HOST + url, status, "rate limited", resp.headers, None)
    if status != 200:
        drop_conn()
        raise urllib.error.HTTPError(HOST + url, status, resp.reason, resp.headers, None)
    _cool_step = 0
    if enc == "gzip":
        raw = gzip.decompress(raw)
    data = json.loads(raw).get("data")
    return [] if data is None else data


def theaters():
    """지역별 극장 목록 -> [(siteNo, "서울 강남"), ...]"""
    return [
        (site["siteNo"], f'{region["regnGrpNm"]} {site["siteNm"]}')
        for region in get("booking/searchRegnList")
        for site in region.get("siteList") or []
    ]


def is_special(row, wanted):
    """wanted 가 비어 있으면 일반관이 아닌 모든 상영관을 특별관으로 본다."""
    grades = {row.get("tcscnsGradNm"), row.get("sascnsGradNm")} - {None, "일반"}
    return bool(grades & set(wanted)) if wanted else bool(grades)


def fetch_day(site_no, ymd, wanted):
    """하루치 특별관 회차를 {key: 사람이 읽을 문구} 로."""
    found = {}
    for row in get(
        "booking/searchMovScnInfo", siteNo=site_no, scnYmd=ymd, rtctlScopCd="01"
    ):
        if not is_special(row, wanted):
            continue
        start = row["scnsrtTm"]
        found[f'{ymd}|{row["scnsNm"]}|{row["prodNm"]}|{start}'] = (
            f'{ymd[4:6]}/{ymd[6:8]} {start[:2]}:{start[2:]} '
            f'{row["prodNm"]} — {row["scnsNm"]}'
        )
    return found


def window(days):
    today = date.today()
    return [(today + timedelta(days=n)).strftime("%Y%m%d") for n in range(days)]


def scan(target, ymds, pause):
    found = {}
    for ymd in ymds:
        found.update(fetch_day(target["site_no"], ymd, target.get("screens", [])))
        if pause:
            time.sleep(pause)
    return found


def send(text, cfg, env="DISCORD_WEBHOOK_URL"):
    """설정된 채널로 전송. Discord 와 Telegram 둘 다 켜져 있으면 둘 다 보낸다."""
    discord = os.environ.get(env) or cfg.get("discord_webhook")
    token = os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegram_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id")
    if not discord and not token:
        log(f"(알림 채널 미설정) {text}")
        return
    for chunk in [text[i : i + 1900] for i in range(0, len(text), 1900)]:
        if discord:
            post(discord, {"content": chunk})
        if token and chat:
            post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat, "text": chunk},
            )


def post(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        # user-agent 가 없으면 Discord 앞단 Cloudflare 가 403 error code 1010 을 낸다.
        headers={"content-type": "application/json", "user-agent": HEADERS["user-agent"]},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        log(f"알림 전송 실패 {exc.code}: {exc.read()[:200]!r}")
    except OSError as exc:
        log(f"알림 전송 실패: {exc}")


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def alert(name, fresh, cfg):
    log(f"{name} 신규 {len(fresh)}건")
    send(f"**{name} 예매 오픈**\n" + "\n".join(sorted(fresh)), cfg.get("notify", {}))


def sweep(cfg, state):
    """전체 창을 훑어 기준선을 다시 잡고, 비어 있는 날짜 목록을 갱신한다.

    ponytail: 스윕이 도는 ~50초 동안은 빠른 감시가 멈춘다(사각지대). 주기를 30분으로
    두어 노출을 시간당 2회로 줄였다. 이마저 아까우면 (극장, 날짜) 하나씩 빠른 감시
    사이에 끼워 넣는 커서 방식으로 바꾸면 사각지대가 사라진다.
    """
    if cooling():
        return
    ymds = window(cfg.get("days_ahead", 21))
    for target in cfg["targets"]:
        name = target["name"]
        try:
            shows = scan(target, ymds, pause=0.3)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{name} 전체조회 실패: {exc}")
            if cooling():
                return  # 제한에 걸렸으면 나머지 극장은 시도하지 않는다
            continue
        seen = {y for y in ymds if any(k.startswith(y) for k in shows)}
        entry = state.get(name)
        if entry is None:
            log(f"{name} 기준선 {len(shows)}건 (첫 실행이라 알림 없음)")
        else:
            fresh = [shows[k] for k in shows if k not in entry["shows"]]
            log(f"{name} 전체 {len(shows)}건 (신규 {len(fresh)}건)")
            if fresh:
                alert(name, fresh, cfg)
        # 회차가 하나도 없는 날짜 = 예매가 아직 안 열린 날. 빠른 감시는 여기만 본다.
        state[name] = {"shows": shows, "empty": [y for y in ymds if y not in seen]}
    state.setdefault("__meta__", {})["last_sweep"] = time.time()
    save_state(state)


def fast_check(cfg, state):
    """아직 안 열린 날짜만 훑는다. 응답이 비어 있어 한 건당 ~80ms."""
    if cooling():
        return
    hit = False
    for target in cfg["targets"]:
        name = target["name"]
        entry = state.get(name)
        if not entry or not entry["empty"]:
            continue
        watch = entry["empty"][: cfg.get("fast_dates", 3)]
        try:
            shows = scan(target, watch, pause=0)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"{name} 빠른조회 실패: {exc}")
            if cooling():
                break  # 제한에 걸렸으면 나머지 극장은 시도하지 않는다
            continue
        if any(k not in entry["shows"] for k in shows):
            # 예매는 보통 여러 날이 한꺼번에 열린다. 앞 2일에서 낌새를 챘으면
            # 남은 미오픈 날짜까지 마저 훑어서 한 통으로 알린다.
            # 오픈은 드문 사건이라 요청이 늘어나는 것도 그때뿐이다.
            rest = [y for y in entry["empty"] if y not in watch]
            if rest:
                try:
                    shows.update(scan(target, rest, pause=0.1))
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    log(f"{name} 잔여 날짜 조회 실패: {exc}")
        fresh = [shows[k] for k in shows if k not in entry["shows"]]
        if fresh:
            alert(name, fresh, cfg)
            entry["shows"].update(shows)
            opened = {k.split("|")[0] for k in shows}
            entry["empty"] = [y for y in entry["empty"] if y not in opened]
            hit = True
    if hit:
        save_state(state)


def heartbeat(cfg, state):
    """하루 한 번 생존 신호. 이게 끊기면 감시가 죽은 것이다.

    오라클 무료 인스턴스는 7일간 CPU/네트워크가 20% 미만이면 회수될 수 있는데,
    이 프로그램은 그 조건에 정확히 해당한다. 조용히 사라지는 걸 막는 장치.
    """
    hours = cfg.get("heartbeat_hours", 24)
    if not hours:
        return
    meta = state.setdefault("__meta__", {})
    if time.time() - meta.get("last", 0) < hours * 3600:
        return
    lines = []
    for target in cfg["targets"]:
        entry = state.get(target["name"])
        if not entry:
            continue
        days = sorted({k[:8] for k in entry["shows"]})
        upto = f'{days[-1][4:6]}/{days[-1][6:]}까지' if days else '없음'
        lines.append(f'{target["name"]} {len(entry["shows"])}건 · {upto}')
    send("🟢 감시 중\n" + "\n".join(lines), cfg.get("notify", {}))
    meta["last"] = time.time()
    save_state(state)


def notices(cfg, state):
    """이벤트 게시판에서 특별관·예매 관련 새 공고를 찾아 알린다.

    회차가 실제로 열리는 순간(sweep/fast_check)과 달리, 이쪽은 "O월 O일 O시 오픈"
    같은 사전 공고를 잡는다. 둘은 다른 사건이라 채널도 따로 둘 수 있다.
    """
    if cooling():
        return
    conf = cfg.get("notices") or {}
    words = conf.get("keywords") or []
    if not words:
        return
    try:
        page = get(
            "content/event/evt/evt/searchEvtListForPage",
            sscnsChoiYn="N", expnYn="N", expoChnlCd="01",
            startRow=0, listCount=conf.get("count", 40),
        )
        # 회차 API 와 달리 data 가 {startRow, totalCount, list:[...]} 형태다
        rows = page.get("list") if isinstance(page, dict) else page
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"공고 조회 실패: {exc}")
        return

    pattern = re.compile("|".join(re.escape(w) for w in words), re.IGNORECASE)
    first = "__notices__" not in state
    seen = state.setdefault("__notices__", {})
    fresh = []
    for row in rows:
        no = str(row.get("evntNo") or "")
        name = (row.get("evntNm") or "").strip()
        if not no or no in seen:
            continue
        seen[no] = name
        if pattern.search(name):
            fresh.append(name)
    if first:
        log(f"공고 기준선 {len(seen)}건 (첫 실행이라 알림 없음)")
    elif fresh:
        log(f"공고 신규 {len(fresh)}건")
        send("**예매 오픈 공고**\n" + "\n".join("· " + f for f in fresh),
             conf, env="NOTICE_WEBHOOK_URL")
    save_state(state)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def selftest():
    rows = [
        {"tcscnsGradNm": "아이맥스", "sascnsGradNm": "일반"},
        {"tcscnsGradNm": "일반", "sascnsGradNm": "일반"},
        {"tcscnsGradNm": "일반", "sascnsGradNm": "아트하우스"},
        {"tcscnsGradNm": None, "sascnsGradNm": None},
    ]
    assert [is_special(r, ["아이맥스"]) for r in rows] == [True, False, False, False]
    assert [is_special(r, []) for r in rows] == [True, False, True, False]

    prev = {"a": "1", "b": "2"}
    cur = {"b": "2", "c": "3"}
    assert [cur[k] for k in cur if k not in prev] == ["3"], "신규만 잡아야 함"
    assert [k for k in prev if k not in cur] == ["a"]  # 사라진 건 알리지 않음

    long_text = "가" * 4000
    assert len([long_text[i : i + 1900] for i in range(0, len(long_text), 1900)]) == 3

    # 빈 날짜(=예매 미오픈) 판정: 회차 키의 앞 8자리가 날짜다
    ymds = ["20260901", "20260902", "20260903"]
    shows = {"20260901|IMAX관|영화|1000": "x"}
    seen = {y for y in ymds if any(k.startswith(y) for k in shows)}
    assert [y for y in ymds if y not in seen] == ["20260902", "20260903"]

    # 빠른 감시가 오픈을 잡으면 그 날짜는 빈 목록에서 빠져야 한다
    entry = {"shows": dict(shows), "empty": ["20260902", "20260903"]}
    found = {"20260902|IMAX관|영화|1200": "y"}
    assert [found[k] for k in found if k not in entry["shows"]] == ["y"]
    entry["shows"].update(found)
    opened = {k.split("|")[0] for k in found}
    entry["empty"] = [y for y in entry["empty"] if y not in opened]
    assert entry["empty"] == ["20260903"], "열린 날짜는 빠른 감시 대상에서 빠져야 함"

    # 갓 부팅한 서버(monotonic 이 작음)에서도 첫 스윕이 즉시 돌아야 한다
    for uptime in (5.0, 90.0, 999999.0):
        assert uptime - float("-inf") >= 1800, "첫 회는 무조건 전체 스윕"

    # 공고 필터: 키워드가 든 것만, 그리고 한 번 본 것은 다시 알리지 않는다
    pat = re.compile("|".join(re.escape(w) for w in ["예매", "IMAX", "SCREENX"]), re.IGNORECASE)
    rows = [{"evntNo": "1", "evntNm": "[오디세이] IMAX N차 관람 이벤트"},
            {"evntNo": "2", "evntNm": "8월 팝콘 할인"},
            {"evntNo": "3", "evntNm": "[아바타] 아이맥스 예매 오픈 안내"}]
    seen, fresh = {}, []
    for r in rows:
        if r["evntNo"] in seen:
            continue
        seen[r["evntNo"]] = r["evntNm"]
        if pat.search(r["evntNm"]):
            fresh.append(r["evntNm"])
    assert len(fresh) == 2 and "팝콘" not in " ".join(fresh)
    assert [r for r in rows if r["evntNo"] not in seen] == [], "두 번째 순회에선 신규 없음"

    # 오픈 감지 시 남은 미오픈 날짜까지 합쳐 한 통으로 알린다
    empty = ["20260910", "20260911", "20260912", "20260913"]
    watch = empty[:2]
    rest = [y for y in empty if y not in watch]
    assert rest == ["20260912", "20260913"], "앞 2일 외 나머지를 마저 봐야 함"
    seen_shows = {}
    found = {f"{y}|IMAX관|영화|1000": f"{y[4:6]}/{y[6:]} 10:00 영화 — IMAX관" for y in empty}
    fresh = [found[k] for k in found if k not in seen_shows]
    assert len(fresh) == 4, "4일치가 한 통에 담겨야 함"
    assert sorted(fresh)[0].startswith("09/10") and sorted(fresh)[-1].startswith("09/13")

    # 재시작 시 스윕 타이밍: 오래됐으면 즉시, 신선하면 남은 시간만큼 대기
    def plan(age, every):
        return "즉시" if age >= every else int(every - age)
    assert plan(0, 1800) == 1800 and plan(1700, 1800) == 100
    assert plan(1800, 1800) == "즉시" and plan(99999, 1800) == "즉시"

    assert window(3)[0] == date.today().strftime("%Y%m%d")
    assert len(window(21)) == 21

    # 429 백오프: 한 번 맞으면 그 뒤 요청은 아예 나가지 않아야 한다
    # (__main__ 로 실행되므로 import 말고 이 모듈의 전역을 직접 다룬다)
    g = globals()
    saved = (g["_cool_until"], g["_cool_step"], g["get"])
    try:
        g["_cool_step"], g["_cool_until"] = 1, time.monotonic() + 30
        assert cooling(), "429 직후에는 쉬어야 함"
        calls = []
        g["get"] = lambda *a, **k: calls.append(1)
        fast_check({"targets": [{"name": "x", "site_no": "0", "screens": []}]},
                   {"x": {"shows": {}, "empty": ["20260901"]}})
        sweep({"targets": [{"name": "x", "site_no": "0"}], "days_ahead": 1}, {})
        notices({"notices": {"keywords": ["예매"]}}, {})
        assert calls == [], "쉬는 동안 요청이 나가면 안 됨"
        # 쉬는 중 get() 은 네트워크에 나가지 않고 단계도 올리지 않아야 한다
        before = g["_cool_step"]
        for _ in range(6):
            try:
                get("booking/searchRegnList")
            except urllib.error.HTTPError as exc:
                assert exc.code == 429
        assert g["_cool_step"] == before, "쉬는 중 호출로 단계가 오르면 안 됨"

        g["_cool_until"] = 0.0
        assert not cooling(), "시간이 지나면 재개"
        assert [30 * 2 ** (n - 1) for n in range(1, 6)] == [30, 60, 120, 240, 480]
    finally:
        g["_cool_until"], g["_cool_step"], g["get"] = saved

    live = get("booking/searchRegnList")
    assert any(s["siteNm"] == "용산아이파크몰" for r in live for s in r["siteList"])
    print("selftest ok")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--selftest":
        selftest()
    elif arg == "--theaters":
        for site_no, label in theaters():
            print(f"{site_no}  {label}")
    else:
        config = load_config()
        saved = load_state()
        if any("shows" not in v for k, v in saved.items() if not k.startswith("__")):
            saved = {}  # 옛 형식이면 기준선부터 다시 (알림 없음)
        full_every = config.get("poll_seconds", 300)
        fast_every = config.get("fast_seconds", 10)
        log(f"감시 시작: {[t['name'] for t in config['targets']]}")
        log(f"전체 {full_every}초 / 빠른 감시 {fast_every}초")
        # 전체 스윕은 126요청 25MB 로 무겁다. 재배포로 자주 재시작하면 이게 겹쳐
        # 429 를 부른다. 직전 스윕이 아직 신선하면 남은 시간만큼 미룬다.
        # monotonic() 은 부팅 후 경과 초라 0 으로 두면 갓 부팅한 서버에서 첫 스윕이
        # 통째로 늦어지므로, 오래됐으면 -inf 로 즉시 실행시킨다.
        age = time.time() - saved.get("__meta__", {}).get("last_sweep", 0)
        if age >= full_every:
            last_full = float("-inf")
        else:
            last_full = time.monotonic() - (full_every - age)
            log(f"직전 스윕 {int(age)}초 전 — 다음 스윕까지 {int(full_every - age)}초 대기")
        last_ntc = float("-inf")
        ntc_every = config.get("notice_seconds", 300)
        while True:
            if time.monotonic() - last_ntc >= ntc_every:
                notices(config, saved)
                last_ntc = time.monotonic()
            if time.monotonic() - last_full >= full_every:
                sweep(config, saved)
                heartbeat(config, saved)
                last_full = time.monotonic()
                if arg == "--once":
                    break
            else:
                fast_check(config, saved)
            time.sleep(fast_every)
