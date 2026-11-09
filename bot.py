#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
텔레그램 명령 수신 + 감시 상태 보관.

명령
    /watch 20261115   감시할 날짜 지정 (1115, 11-15, 11/15 도 허용)
    /status           현재 상태
    /now              지금 바로 한 번 조회해서 알려주기
    /stop             감시 중지
    /help             도움말

상태는 state.json 에 보관하고, GitHub API 로 커밋해서
실행 세션이 바뀌어도 유지되게 한다. (로컬 실행 시에는 파일에만 저장)
"""

import os
import re
import json
import base64
from datetime import datetime, date, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
STATE_FILE = "state.json"

WDAY = ("월", "화", "수", "목", "금", "토", "일")

HELP = """사용법

/watch 20261115   이 날짜에 뜨면 알림
/ok               알림 확인 (이걸 보낼 때까지 5초마다 반복됩니다)
/now              찾는 것만 바로 확인
/nowall           그날 전체 목록 확인
/status           현재 감시 상태
/health           세션 생존/이어받기/감시 공백 기록
/health off       생존 기록·외부 ping 끄기 (/health on 으로 켜기)
/seatwatch on     찾은 뒤에도 자리 나는지 계속 지켜보기
/seatwatch off    끄기
/seatchangeall on   (테스트용) 잔여석이 조금이라도 바뀌면 매번 알림
/seatchangeall off  끄기
/trace on         매 조회마다 한 줄씩 보고 (5분 뒤 자동 종료)
/trace on 30      30분 동안 유지
/trace off        끄기
/stop             감시 중지
/help             이 도움말

/now 20261107 처럼 날짜를 직접 줄 수도 있습니다.

날짜는 20261115 형식입니다. (1115 처럼 월일만 보내도 됩니다)"""


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------- 날짜 파싱

def parse_date(text):
    """사용자가 보낸 문자열에서 날짜를 뽑아낸다. 실패하면 None."""
    s = (text or "").strip().replace(".", "-").replace("/", "-")
    today = datetime.now(KST).date()
    year_omitted = False

    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)              # 20261115 (기본)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)    # 2026-11-15
        if m:
            y, mo, d = (int(x) for x in m.groups())
        else:
            m = re.match(r"^(\d{2})(\d{2})$", s)             # 1115
            if not m:
                m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)    # 11-15
            if not m:
                return None
            mo, d = int(m.group(1)), int(m.group(2))
            y = today.year
            year_omitted = True

    try:
        result = date(y, mo, d)
    except ValueError:
        return None

    # 연도를 생략했고 이미 지난 날짜면 내년으로 본다
    if year_omitted and result < today:
        try:
            result = date(y + 1, mo, d)
        except ValueError:
            return None
    return result


# ---------------------------------------------------------------- 상태

class State:
    """감시 상태. state.json 에 저장하고 가능하면 저장소에도 커밋한다."""

    def __init__(self, gh_token=None, gh_repo=None, branch=None):
        self.gh_token = gh_token
        self.gh_repo = gh_repo
        self.branch = branch or "main"
        self.target = None          # date | None
        self.tg_offset = 0          # 처리한 텔레그램 update_id
        self.trace_until = None     # datetime | None. 이 시각까지 매 조회를 보고
        self.seatwatch = False      # 찾은 뒤에도 자리가 나는지 계속 지켜볼지
        # 테스트용. 켜면 매진 여부와 무관하게 잔여석이 조금이라도 바뀌면(늘든
        # 줄든) 매번 알린다. watch.py 에는 없는, chitest_new 에서만 추가된 기능.
        self.seat_change_all = False
        self.health_off = False     # True 면 생존 기록/외부 ping 을 하지 않는다 (/health off)
        # 세션이 바뀌어도 이어가야 하는 진행 상황.
        # 메모리에만 두면 이어받을 때마다 "찾았습니다" 를 다시 보내게 된다.
        self.announced = None       # "YYYYMMDD" | None. 편성 발견을 이미 알린 날짜
        self.zero_slots = set()     # 좌석 0 으로 확인된 회차 키. 여기서 자리가 나면 알린다
        # 그날 본 회차 전체. 회차 키("영화명|관|시간") -> screenplanid ("" = 모름).
        # 여기 없는 회차가 나타나면 새 회차로 본다.
        self.slot_ids = {}
        # screenplanid 가 회차 고유 키로 믿을 만한지 검증한 기록
        self.id_since = None        # 검증을 시작한 시각 문자열
        self.id_anomalies = 0       # 지금까지 발견한 이상 건수

        # 읽음 확인(/ok) 을 받을 때까지 알림을 반복한다.
        # 본문은 저장소에 남기지 않는다. 실행 중에는 메모리에 들고 있고,
        # 세션이 바뀌면 그때 한 번만 새로 조회해서 만든다.
        self.ack_date = None        # date | None. 미확인 알림이 걸린 날짜
        self.ack_count = 0          # 지금까지 보낸 횟수
        self.ack_text = None        # 메모리 전용. 저장하지 않는다.
        self.ack_last = None        # 메모리 전용. 마지막 전송 시각
        self._sha = None            # 저장소 파일 sha (커밋에 필요)
        self.load()

    def trace_on(self):
        return self.trace_until is not None and datetime.now(KST) <= self.trace_until

    # ---- 읽기

    def load(self):
        # 저장소 최신본이 우선이다. 체크아웃된 파일은 재실행 시 과거 커밋일 수
        # 있어서 신뢰할 수 없다. (Re-run 은 최초 실행 시점의 커밋을 가져온다)
        data = self._remote_load()
        if data is None:
            data = self._local_load()
            log("state: 로컬 파일에서 읽었습니다.")
        else:
            log("state: 저장소 최신본에서 읽었습니다.")

        raw = data.get("target_date")
        if raw:
            self.target = parse_date(str(raw))
        self.tg_offset = int(data.get("tg_offset") or 0)

        self.seatwatch = bool(data.get("seatwatch"))
        self.seat_change_all = bool(data.get("seat_change_all"))
        self.health_off = bool(data.get("health_off"))
        self.announced = data.get("announced") or None
        self.zero_slots = set(str(k) for k in (data.get("zero_slots") or []))
        self.slot_ids = {str(k): str(v or "") for k, v in (data.get("slot_ids") or {}).items()}
        self.id_since = data.get("id_since") or None
        self.id_anomalies = int(data.get("id_anomalies") or 0)

        raw = data.get("ack_date")
        self.ack_date = parse_date(str(raw)) if raw else None
        self.ack_count = int(data.get("ack_count") or 0)

        raw = data.get("trace_until")
        if raw:
            try:
                self.trace_until = datetime.strptime(
                    str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
            except ValueError:
                self.trace_until = None

    @staticmethod
    def _local_load():
        if not os.path.exists(STATE_FILE):
            return {}
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            log("state.json 읽기 실패(무시): %s" % e)
            return {}

    def _remote_load(self):
        """저장소의 state.json 을 읽어온다. 실패하면 None (sha 도 같이 갱신)."""
        if not (self.gh_token and self.gh_repo):
            return None
        try:
            r = requests.get(
                "https://api.github.com/repos/%s/contents/%s" % (self.gh_repo, STATE_FILE),
                headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException as e:
            log("state 원격 읽기 예외: %s" % str(e)[:160])
            return None

        if r.status_code == 404:
            return {}                      # 아직 파일이 없는 정상 상태
        if r.status_code != 200:
            log("state 원격 읽기 실패 %s" % r.status_code)
            return None

        body = r.json()
        self._sha = body.get("sha")
        try:
            return json.loads(base64.b64decode(body.get("content", "")).decode("utf-8"))
        except (ValueError, TypeError) as e:
            log("state 원격 파싱 실패: %s" % e)
            return None

    def _remote_sha(self):
        if not (self.gh_token and self.gh_repo):
            return None
        try:
            r = requests.get(
                "https://api.github.com/repos/%s/contents/%s" % (self.gh_repo, STATE_FILE),
                headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code == 200:
            return r.json().get("sha")
        return None

    # ---- 쓰기

    def save(self, reason="", commit=True):
        """
        commit=False 면 저장소에 올리지 않고 로컬 파일만 쓴다.
        offset 처럼 자주 바뀌지만 잃어도 큰일 없는 값에 쓴다.
        커밋이 잦으면 사람이 push 할 때 계속 부딪힌다.
        """
        self._write(reason, commit)

    def _write(self, reason, commit):
        payload = {
            "target_date": self.target.strftime("%Y%m%d") if self.target else None,
            "tg_offset": self.tg_offset,
            "seatwatch": self.seatwatch,
            "seat_change_all": self.seat_change_all,
            "health_off": self.health_off,
            "announced": self.announced,
            "zero_slots": sorted(self.zero_slots),
            "slot_ids": dict(sorted(self.slot_ids.items())),
            "id_since": self.id_since,
            "id_anomalies": self.id_anomalies,
            "ack_date": self.ack_date.strftime("%Y%m%d") if self.ack_date else None,
            "ack_count": self.ack_count,
            "trace_until": (self.trace_until.strftime("%Y-%m-%d %H:%M:%S")
                            if self.trace_until else None),
            "updated_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError as e:
            log("state.json 쓰기 실패: %s" % e)

        if commit:
            self._commit(body, reason)

    def _commit(self, body, reason):
        if not (self.gh_token and self.gh_repo):
            return
        payload = {
            "message": "state: %s" % (reason or "update"),
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha:
            payload["sha"] = self._sha
        try:
            r = requests.put(
                "https://api.github.com/repos/%s/contents/%s" % (self.gh_repo, STATE_FILE),
                headers=self._headers(), data=json.dumps(payload).encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            log("state 커밋 예외: %s" % str(e)[:160])
            return

        if r.status_code in (200, 201):
            self._sha = r.json().get("content", {}).get("sha")
        elif r.status_code == 409:
            # 다른 job 이 먼저 썼다. sha 를 다시 읽고 다음 저장 때 반영한다.
            log("state 커밋 충돌. sha 를 다시 읽습니다.")
            self._sha = self._remote_sha()
        else:
            log("state 커밋 실패 %s: %s" % (r.status_code, r.text[:200]))

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.gh_token,
                "Accept": "application/vnd.github+json"}


# ---------------------------------------------------------------- 텔레그램

class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.enabled = bool(token and chat_id)

    def _url(self, method):
        return "https://api.telegram.org/bot%s/%s" % (self.token, method)

    # 텔레그램 메시지 한 통의 최대 길이는 4096자. 여유를 두고 자른다.
    MAX_LEN = 4000

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
        """길면 줄 경계에서 나눈다. 한 줄이 너무 길면 그 줄은 글자 수로 자른다."""
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
        """새 명령을 [(update_id, 본문), ...] 로 반환."""
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
            # 내 대화방 메시지만 처리한다
            if text and chat == self.chat_id:
                out.append((upd["update_id"], text))
            elif text:
                log("다른 대화방(%s) 메시지는 무시합니다." % chat)
                out.append((upd["update_id"], None))
        return out


# ---------------------------------------------------------------- 명령 처리

def handle_command(text, state, tg, status_fn, now_fn, health_fn=None):
    """
    명령 하나를 처리한다.
    status_fn() -> 상태 문자열, now_fn(날짜|None) -> 즉시 조회 결과 문자열
    health_fn() -> 세션 생존/이어받기 상태 문자열
    반환값: 감시 대상 날짜가 바뀌었으면 True
    """
    cmd = text.strip()
    low = cmd.lower()
    head = low.split()[0] if low.split() else ""
    arg = cmd[len(head):].strip()

    if head in ("/help", "help", "/start"):
        tg.send(HELP)
        return False

    if head in ("/health", "health"):
        sw = (arg or "").lower().strip()
        if sw in ("on", "off"):
            state.health_off = (sw == "off")
            state.save("health %s" % sw)
            tg.send("health 기록과 외부 ping 을 %s.\n(다음 실행 요청/이어받기는 계속 동작합니다)"
                    % ("껐습니다" if state.health_off else "켰습니다"))
            return False
        tg.send(health_fn() if health_fn else "health 기록이 꺼져 있습니다.")
        return False

    if head in ("/status", "status"):
        tg.send(status_fn())
        return False

    if head in ("/now", "now", "/nowall", "nowall"):
        # 인자로 날짜를 주면 그 날짜를, 없으면 감시 중인 날짜를 본다
        d = parse_date(arg) if arg else None
        if arg and d is None:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /now 20261107" % arg)
            return False
        tg.send(now_fn(d or state.target, head.lstrip("/") == "nowall"))
        return False

    if head in ("/ok", "ok", "/ack", "ack", "확인"):
        if state.ack_date is None:
            tg.send("반복 중인 알림이 없습니다.")
            return False
        n = state.ack_count
        state.ack_date = None
        state.ack_count = 0
        state.ack_text = None
        state.ack_last = None
        state.save("ack")
        tg.send("확인했습니다. 알림을 멈춥니다. (%d번 보냈습니다)" % n)
        return False

    if head in ("/trace", "trace"):
        return _set_trace(arg, state, tg)

    if head in ("/seatwatch", "seatwatch"):
        on = (arg or "").lower().strip() not in ("off", "0", "stop")
        state.seatwatch = on
        state.save("seatwatch %s" % ("on" if on else "off"))
        tg.send("찾은 뒤에도 자리가 나는지 계속 지켜봅니다." if on
                else "자리 지켜보기를 껐습니다. 찾으면 알리고 끝냅니다.")
        return False

    if head in ("/seatchangeall", "seatchangeall"):
        on = (arg or "").lower().strip() not in ("off", "0", "stop")
        state.seat_change_all = on
        state.save("seatchangeall %s" % ("on" if on else "off"))
        tg.send("테스트용: 잔여석이 조금이라도 바뀌면(늘든 줄든) 매번 알립니다." if on
                else "잔여석 전체 변화 알림(테스트용)을 껐습니다.")
        return False

    if head in ("/stop", "stop"):
        state.target = None
        state.save("stop")
        tg.send("감시를 중지했습니다. 다시 시작하려면 /watch 날짜 를 보내세요.")
        return True

    if head in ("/watch", "watch"):
        d = parse_date(arg)
        if not d:
            tg.send("날짜를 알아듣지 못했습니다: %r\n예) /watch 20261115" % arg)
            return False
        return _set_date(d, state, tg)

    # 명령어 없이 날짜만 보낸 경우도 받아준다
    d = parse_date(cmd)
    if d:
        return _set_date(d, state, tg)

    tg.send("모르는 명령입니다.\n\n" + HELP)
    return False


def _set_trace(arg, state, tg):
    """
    /trace on        5분간 매 조회 보고
    /trace on 30     30분간
    /trace off       끄기
    """
    tokens = (arg or "").lower().split()
    if tokens and tokens[0] in ("off", "stop", "0"):
        state.trace_until = None
        state.save("trace off")
        tg.send("trace 를 껐습니다.")
        return False

    if tokens and tokens[0] in ("on", "start"):
        tokens = tokens[1:]

    minutes = 5
    if tokens:
        if not tokens[0].isdigit():
            tg.send("시간을 알아듣지 못했습니다: %r\n예) /trace on 30" % arg)
            return False
        minutes = max(1, min(180, int(tokens[0])))

    state.trace_until = datetime.now(KST) + timedelta(minutes=minutes)
    state.save("trace on %dm" % minutes)
    tg.send("trace 를 켰습니다.\n%d분 동안 매 조회마다 한 줄씩 보냅니다.\n끄려면 /trace off"
            % minutes)
    return False


def _set_date(d, state, tg):
    today = datetime.now(KST).date()
    if d < today:
        tg.send("%s 는 이미 지난 날짜입니다." % d.strftime("%Y%m%d"))
        return False
    state.target = d
    state.save("watch %s" % d.strftime("%Y%m%d"))
    tg.send("감시 시작합니다.\n날짜 %s (%s) D-%d\n뜨는 즉시 알려드릴게요."
            % (d.strftime("%Y%m%d"), WDAY[d.weekday()], (d - today).days))
    return True
