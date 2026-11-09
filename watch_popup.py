#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
씨네Q 예매 팝업(/popup/ReserveMovieList, /popup/ReserveScreenPlan) 을 직접
불러서 회차·잔여석을 감시하는 봇. 메인 목록 페이지(watch.py 가 쓰던 소스)는
결제/취소가 반영되는 데 수 분씩 걸리는 것이 11-09 에 실측으로 확인됐고,
팝업은 그 즉시 반영됐다. 그래서 watch.py 는 그대로 두고 이 저장소를
완전히 분리해서 새로 만들었다.

환경변수
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   알림
    GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REF_NAME   상태 저장 + 이어받기
    THEATER_CODE   극장 코드 (기본 1001)
    KEYWORDS       쉼표 구분. 영화명에 포함되면 감시 대상
    TARGET_DATE    처음 감시할 날짜(YYYYMMDD). 비우면 /watch 로 지정
    POLL_SEC       조회 간격 초 (기본 20)
    MAX_RUN_MIN, RELAY_AFTER_MIN, RELAY_WORKFLOW   세션 길이/이어받기
"""

import base64
import json
import os
import re
import signal
import ssl
import sys
import time
from datetime import datetime, date, timedelta, timezone

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

from parser import parse_movie_list, parse_screen_plan, pick_movies
from logic import decide

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

KST = timezone(timedelta(hours=9))
OP_LEGACY_SERVER_CONNECT = 0x4
STATE_FILE = "state_popup.json"
API = "https://api.github.com/repos/%s"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def env_int(name, default):
    try:
        return int(env(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- 날짜 파싱

def parse_date(text):
    """사용자가 보낸 문자열에서 날짜를 뽑아낸다. 실패하면 None. (bot.py 와 동일 로직)"""
    s = (text or "").strip().replace(".", "-").replace("/", "-")
    today = datetime.now(KST).date()
    year_omitted = False

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.match(r"^(\d{2})(\d{2})$", s)
            if not m:
                m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
            if not m:
                return None
            mo, d = int(m.group(1)), int(m.group(2))
            y = today.year
            year_omitted = True
    try:
        result = date(y, mo, d)
    except ValueError:
        return None
    if year_omitted and result < today:
        try:
            result = date(y + 1, mo, d)
        except ValueError:
            return None
    return result


# ---------------------------------------------------------------- 텔레그램

class Telegram:
    """bot.py 의 Telegram 클래스와 동일. 저장소를 분리하면서 복사해 독립시켰다."""

    MAX_LEN = 4000

    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.enabled = bool(token and chat_id)

    def _url(self, method):
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)

    def send(self, text):
        if not self.enabled:
            log("(텔레그램 미설정) " + text.replace("\n", " / ")[:200])
            return False
        ok = True
        for part in self._split(text):
            ok = self._send_one(part) and ok
        return ok

    def _send_one(self, text):
        try:
            r = requests.post(self._url("sendMessage"),
                              data={"chat_id": self.chat_id, "text": text}, timeout=20)
        except requests.RequestException as e:
            log("텔레그램 전송 예외: %s" % str(e)[:160])
            return False
        if r.status_code != 200:
            log("텔레그램 전송 실패 %s: %s" % (r.status_code, r.text[:200]))
            return False
        return True

    @classmethod
    def _split(cls, text):
        if len(text) <= cls.MAX_LEN:
            return [text]
        parts, cur = [], ""
        for line in text.split("\n"):
            while len(line) > cls.MAX_LEN:
                if cur:
                    parts.append(cur)
                    cur = ""
                parts.append(line[:cls.MAX_LEN])
                line = line[cls.MAX_LEN:]
            if cur and len(cur) + 1 + len(line) > cls.MAX_LEN:
                parts.append(cur)
                cur = line
            else:
                cur = (cur + "\n" + line) if cur else line
        if cur:
            parts.append(cur)
        return parts

    def poll(self, offset):
        if not self.enabled:
            return []
        try:
            r = requests.get(self._url("getUpdates"),
                             params={"offset": offset + 1, "timeout": 0, "limit": 20},
                             timeout=20)
        except requests.RequestException as e:
            log("텔레그램 수신 예외: %s" % str(e)[:160])
            return []
        if r.status_code != 200:
            log("텔레그램 수신 실패 %s" % r.status_code)
            return []
        out = []
        for upd in r.json().get("result", []):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if text and chat == self.chat_id:
                out.append((upd["update_id"], text))
            elif text:
                out.append((upd["update_id"], None))
        return out


# ---------------------------------------------------------------- 상태

class State:
    """
    state_popup.json 을 GitHub Contents API 로 읽고 쓴다. watch.py 의 state.json
    과는 완전히 다른 파일이라 서로 건드리지 않는다(저장소도 다르다).
    """

    def __init__(self, gh_token, gh_repo, gh_branch):
        self.gh_token = gh_token
        self.gh_repo = gh_repo
        self.branch = gh_branch or "main"
        self._sha = None

        self.target = None            # date | None
        self.tg_offset = 0
        self.announced_date = None    # "찾았습니다" 를 이미 보낸 날짜(YYYYMMDD)
        self.known_ids = set()
        self.zero_ids = set()

        self._load()

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.gh_token,
                "Accept": "application/vnd.github+json"}

    def _load(self):
        if not (self.gh_token and self.gh_repo):
            return
        try:
            r = requests.get(
                (API % self.gh_repo) + "/contents/%s" % STATE_FILE,
                headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException as e:
            log("state 읽기 예외: %s" % str(e)[:160])
            return
        if r.status_code != 200:
            return                   # 처음 실행이면 파일이 없는 게 정상
        data = r.json()
        self._sha = data.get("sha")
        try:
            body = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
        except Exception as e:
            log("state 파싱 예외: %s" % str(e)[:160])
            return
        t = body.get("target")
        self.target = datetime.strptime(t, "%Y%m%d").date() if t else None
        self.tg_offset = body.get("tg_offset", 0)
        self.announced_date = body.get("announced_date")
        self.known_ids = set(body.get("known_ids") or [])
        self.zero_ids = set(body.get("zero_ids") or [])

    def save(self, reason):
        if not (self.gh_token and self.gh_repo):
            return
        body = {
            "target": self.target.strftime("%Y%m%d") if self.target else None,
            "tg_offset": self.tg_offset,
            "announced_date": self.announced_date,
            "known_ids": sorted(self.known_ids),
            "zero_ids": sorted(self.zero_ids),
        }
        payload = {
            "message": "state: %s" % reason,
            "content": base64.b64encode(
                json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha:
            payload["sha"] = self._sha
        try:
            r = requests.put(
                (API % self.gh_repo) + "/contents/%s" % STATE_FILE,
                headers=self._headers(), data=json.dumps(payload).encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            log("state 저장 예외: %s" % str(e)[:160])
            return
        if r.status_code in (200, 201):
            self._sha = r.json().get("content", {}).get("sha")
        elif r.status_code in (409, 422):
            self._load()          # 어긋났다. 다시 읽어서 다음 저장 때 반영
        else:
            log("state 저장 실패 %s: %s" % (r.status_code, r.text[:200]))


# ---------------------------------------------------------------- 접속

class LegacyTLSAdapter(HTTPAdapter):
    """구형 TLS 재협상 허용 + 인증서 검증 끔 (watch.py 와 동일 사유)."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._ctx()
        return super().init_poolmanager(*args, **kwargs)

    @staticmethod
    def _ctx():
        ctx = create_urllib3_context()
        ctx.options |= OP_LEGACY_SERVER_CONNECT
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            pass
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


class Fetcher:
    def __init__(self, origin):
        self.origin = origin
        self.sess = requests.Session()
        self.sess.mount("https://", LegacyTLSAdapter())
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "X-Requested-With": "XMLHttpRequest",
        })

    def _post(self, path, data):
        try:
            r = self.sess.post(self.origin + path, data=data, timeout=20, verify=False)
        except requests.RequestException as e:
            log("조회 예외 %s: %s" % (path, str(e)[:160]))
            return None
        if r.status_code != 200:
            log("조회 응답 %s %s" % (path, r.status_code))
            return None
        return r.text or ""

    def movie_list(self, theater, play_date):
        body = self._post("/popup/ReserveMovieList",
                          {"PlayDate": play_date, "TheaterCode": theater,
                           "movieCode": "", "SortType": "1"})
        return parse_movie_list(body) if body is not None else None

    def screen_plan(self, theater, play_date, movie_codes):
        body = self._post("/popup/ReserveScreenPlan",
                          {"theaterCode": theater, "playDate": play_date,
                           "movieCode": ",".join(movie_codes)})
        return parse_screen_plan(body) if body is not None else None


# ---------------------------------------------------------------- 다음 실행 요청

def request_next_run(gh_token, gh_repo, workflow, branch):
    if not (gh_token and gh_repo):
        return False
    try:
        r = requests.post(
            (API % gh_repo) + "/actions/workflows/%s/dispatches" % workflow,
            headers={"Authorization": "Bearer %s" % gh_token,
                    "Accept": "application/vnd.github+json"},
            data=json.dumps({"ref": branch}).encode("utf-8"), timeout=20)
    except requests.RequestException as e:
        log("다음 실행 요청 예외: %s" % str(e)[:160])
        return False
    ok = r.status_code == 204
    log("다음 실행 요청 %s" % ("성공" if ok else "실패 %s" % r.status_code))
    return ok


# ---------------------------------------------------------------- 표시

def format_items(items):
    lines = []
    by_screen = {}
    for it in items:
        by_screen.setdefault((it["movie"], it["screen"], it["capacity"]), []).append(it)
    for (movie, screen, cap), its in by_screen.items():
        lines.append("▪ %s" % movie)
        lines.append("   %s (%d석)" % (screen, cap))
        for it in sorted(its, key=lambda x: x["time"]):
            mark = " [매진]" if it["soldout"] else ""
            lines.append("     %s  %s석%s" % (it["time"], it["seats"], mark))
    return lines


HELP = """사용법 (팝업 기반 감시봇)

/watch 20261115   이 날짜에 편성이 뜨면 알림, 매진 해제도 지켜봄
/now              지금 바로 조회해서 알려주기
/status           현재 상태
/stop             감시 중지
/help             이 도움말"""


def status_text(cfg, state):
    if not state.target:
        return "감시 중인 날짜가 없습니다. /watch 20261115 처럼 지정하세요."
    out = ["%s 감시 중" % state.target.strftime("%Y%m%d")]
    out.append("찾음: %s" % ("예" if state.announced_date == state.target.strftime("%Y%m%d") else "아직"))
    out.append("기록된 회차 %d개 / 매진 %d개" % (len(state.known_ids), len(state.zero_ids)))
    return "\n".join(out)


def snapshot_text(cfg, fetcher, target_date, keywords):
    ymd = target_date.strftime("%Y%m%d")
    movies = fetcher.movie_list(cfg.theater, ymd)
    if movies is None:
        return "조회에 실패했습니다. 잠시 후 다시 시도하세요."
    cand = pick_movies(movies, keywords)
    if not cand:
        return "날짜 %s\n아직 편성이 없습니다." % ymd
    items = fetcher.screen_plan(cfg.theater, ymd, [c for _t, c in cand])
    if items is None:
        return "조회에 실패했습니다. 잠시 후 다시 시도하세요."
    if not items:
        return "날짜 %s\n아직 편성이 없습니다." % ymd
    return "\n".join(["날짜 %s / %d건" % (ymd, len(items)), ""] + format_items(items))


# ---------------------------------------------------------------- 명령 처리

def handle_command(text, state, tg, cfg, fetcher):
    cmd = text.strip()
    low = cmd.lower()
    head = low.split()[0] if low.split() else ""
    arg = cmd[len(head):].strip()

    if head in ("/help", "help", "/start"):
        tg.send(HELP)
    elif head in ("/status", "status"):
        tg.send(status_text(cfg, state))
    elif head in ("/now", "now"):
        d = parse_date(arg) if arg else state.target
        if d is None:
            tg.send("날짜를 알아듣지 못했습니다: %r" % arg)
        else:
            tg.send(snapshot_text(cfg, fetcher, d, cfg.keywords))
    elif head in ("/stop", "stop"):
        state.target = None
        state.announced_date = None
        state.known_ids = set()
        state.zero_ids = set()
        tg.send("감시를 중지했습니다.")
    elif head in ("/watch", "watch"):
        d = parse_date(arg)
        if d is None:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /watch 20261115" % arg)
        else:
            state.target = d
            state.announced_date = None
            state.known_ids = set()
            state.zero_ids = set()
            tg.send("%s 감시를 시작합니다." % d.strftime("%Y%m%d"))
    else:
        return
    state.save("command")


# ---------------------------------------------------------------- 설정

class Config:
    def __init__(self):
        self.theater = env("THEATER_CODE", "1001")
        self.keywords = [k.strip() for k in (env("KEYWORDS", "") or "").split(",") if k.strip()]
        if not self.keywords:
            log("오류: KEYWORDS 가 비어 있습니다.")
            sys.exit(2)
        self.site_origin = env("SITE_ORIGIN", "https://www.cineq.co.kr")
        self.poll_sec = env_int("POLL_SEC", 20)
        self.max_run_min = env_int("MAX_RUN_MIN", 340)
        self.relay_after_min = env_int("RELAY_AFTER_MIN", max(1, self.max_run_min - 15))
        self.relay_workflow = env("RELAY_WORKFLOW", "watch_popup.yml")

        raw_date = env("TARGET_DATE")
        self.initial_target = parse_date(raw_date) if raw_date else None

        self.tg_token = env("TELEGRAM_BOT_TOKEN")
        self.tg_chat = env("TELEGRAM_CHAT_ID")
        self.gh_token = env("GITHUB_TOKEN")
        self.gh_repo = env("GITHUB_REPOSITORY")
        self.gh_branch = env("GITHUB_REF_NAME", "main")


# ---------------------------------------------------------------- 메인 루프

def main():
    cfg = Config()
    start = datetime.now(KST)
    tg = Telegram(cfg.tg_token, cfg.tg_chat)
    state = State(cfg.gh_token, cfg.gh_repo, cfg.gh_branch)
    fetcher = Fetcher(cfg.site_origin)

    if state.target is None and cfg.initial_target is not None:
        state.target = cfg.initial_target
        state.save("init from env")

    log("기동 완료 / 키워드 %s / 극장 %s" % (cfg.keywords, cfg.theater))

    deadline = start + timedelta(minutes=cfg.max_run_min)
    relay_at = start + timedelta(minutes=cfg.relay_after_min)
    relay_done = False

    def on_signal(signum, _frame):
        sys.exit(0)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)

    announced = (state.target is not None
                and state.announced_date == state.target.strftime("%Y%m%d"))

    checks = 0
    while datetime.now(KST) < deadline:
        now = datetime.now(KST)
        if (not relay_done) and now >= relay_at:
            relay_done = request_next_run(cfg.gh_token, cfg.gh_repo,
                                          cfg.relay_workflow, cfg.gh_branch)

        for update_id, text in tg.poll(state.tg_offset):
            state.tg_offset = update_id
            if text is None:
                state.save("offset")
                continue
            log("명령 수신: %s" % text[:80])
            before = state.target
            handle_command(text, state, tg, cfg, fetcher)
            if state.target != before:
                announced = (state.target is not None
                            and state.announced_date == (
                                state.target.strftime("%Y%m%d") if state.target else None))

        if state.target is None:
            time.sleep(cfg.poll_sec)
            continue

        ymd = state.target.strftime("%Y%m%d")
        movies = fetcher.movie_list(cfg.theater, ymd)
        if movies is None:
            time.sleep(cfg.poll_sec)
            continue
        cand = pick_movies(movies, cfg.keywords)
        items = []
        if cand:
            items = fetcher.screen_plan(cfg.theater, ymd, [c for _t, c in cand]) or []

        checks += 1
        new_ids, new_zero, new_announced, alerts = decide(
            state.known_ids, state.zero_ids, items, announced)

        if new_ids != state.known_ids or new_zero != state.zero_ids or new_announced != announced:
            state.known_ids = new_ids
            state.zero_ids = new_zero
            if new_announced and not announced:
                state.announced_date = ymd
            announced = new_announced
            state.save("checks %d" % checks)

        for kind, its in alerts:
            if kind == "found":
                tg.send("[팝업감시] 찾았습니다!\n날짜 %s\n\n%s" %
                        (ymd, "\n".join(format_items(its))))
            elif kind == "added":
                tg.send("[팝업감시] 회차가 추가됐습니다!\n날짜 %s\n\n%s" %
                        (ymd, "\n".join(format_items(its))))
            elif kind == "opened":
                tg.send("[팝업감시] 자리가 났습니다!\n날짜 %s\n\n%s" %
                        (ymd, "\n".join(format_items(its))))

        if checks % 30 == 0:
            log("감시 중... %d회 / 회차 %d개" % (checks, len(items)))

        time.sleep(cfg.poll_sec)

    log("세션 종료 (%d분)" % cfg.max_run_min)
    return 0


if __name__ == "__main__":
    sys.exit(main())
