#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
세션 생존 기록 + 다음 세션 띄우기(relay).

기록은 main 과 커밋이 부딪히지 않도록 별도 브랜치(health)에 남긴다.

    health.json   마지막 생존 시각, 세션 정보, 조회 횟수, 최근 오류 (자주 덮어씀)
    health.log    세션 시작/요청/종료/감시 공백 같은 사건 기록 (최근 500줄 유지)

새 세션은 시작할 때 이전 세션의 마지막 생존 시각을 읽어, 그 사이에
감시가 비어 있던 시간(공백)을 계산해 남긴다.

HEALTHCHECK_URL 이 있으면 생존 기록 때마다 그 주소로 GET 을 보낸다.
(healthchecks.io 같은 외부 감시 서비스에 연결하면, ping 이 끊겼을 때
 GitHub 와 무관하게 따로 알림을 받을 수 있다)
"""

import json
import base64
import faulthandler
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
FMT = "%Y-%m-%d %H:%M:%S"
API = "https://api.github.com/repos/%s"
LOG_KEEP = 500


def log(msg):
    print("[%s] %s" % (datetime.now(KST).strftime("%m-%d %H:%M:%S"), msg), flush=True)


def now_kst():
    return datetime.now(KST)


def fmt(dt):
    return dt.strftime(FMT) if dt else None


def parse(s):
    try:
        return datetime.strptime(str(s), FMT).replace(tzinfo=KST)
    except (TypeError, ValueError):
        return None


class Health:
    def __init__(self, gh_token, gh_repo, base_branch, run_id, run_event,
                 ping_url=None, beat_sec=60, branch="health"):
        self.token = gh_token
        self.repo = gh_repo
        self.base_branch = base_branch or "main"
        self.branch = branch
        self.run_id = run_id or "local"
        self.run_event = run_event or "local"
        self.ping_url = ping_url
        self.beat_sec = max(10, beat_sec)
        self.enabled = bool(gh_token and gh_repo)
        # False 면 기록(health 브랜치)과 외부 ping 을 하지 않는다. /health off 로 끈다.
        # 다음 workflow run 요청(request_next)은 이 값과 무관하게 동작한다.
        self.active = True
        # /health 응답 끝에 덧붙일 줄을 돌려주는 함수 (회차 ID 검증 현황 등)
        self.extra_fn = None

        self.started = now_kst()
        self.last_beat = None          # 마지막으로 health.json 을 쓴 시각
        self.last_ping = None
        self.last_error = None
        self.error_count = 0
        self.gap_sec = None            # 이번 세션 시작 시 잰 감시 공백
        self.prev_run = None
        self.relay = {"requested_at": None, "ok": None, "tries": 0}
        self._gap_retry = 0            # 이전 실행 종료 시각을 다시 조회할 남은 횟수
        self._sha = {}                 # 파일별 sha
        # 기록은 메인 루프와 Watchdog 스레드 양쪽에서 쓴다. 파일 sha 가 엇갈리지
        # 않도록 쓰기를 직렬화한다. 실패 시 _put 이 스스로 다시 부르므로 RLock 이다.
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 시작/종료

    def start(self):
        """
        브랜치를 준비하고, 이전 세션의 마지막 생존 시각으로 공백을 잰다.
        반환: 공백(초) 또는 None (이전 기록이 없을 때)
        """
        if not self.active:
            log("health: 꺼져 있어 기록하지 않습니다.")
            return None
        if not self.enabled:
            log("health: GitHub 정보가 없어 기록하지 않습니다.")
            return None
        self._ensure_branch()

        prev = self._read_json("health.json") or {}
        prev_seen = parse(prev.get("last_seen"))
        self.prev_run = prev.get("run_id")

        # 1순위: Actions 기록에서 이전 실행의 watch.py 단계가 실제로 끝난 시각.
        # 이어받기로 끊길 때는 종료 기록을 남기지 못해서(11-05 실측) 생존 기록만으로는
        # 최대 beat_sec 만큼 공백이 부풀려진다.
        ended, conclusion = self._prev_step_end(self.prev_run)

        if ended:
            self.gap_sec = int((self.started - ended).total_seconds())
            msg = ("세션 시작 run=%s (%s). 이전 run=%s 종료 %s (%s) -> 공백 %d초 (Actions 기록 기준)"
                   % (self.run_id, self.run_event, self.prev_run,
                      fmt(ended), conclusion or "?", self.gap_sec))
        elif prev_seen:
            # 2순위: 마지막 생존 기록
            self.gap_sec = int((self.started - prev_seen).total_seconds())
            how = prev.get("status") or "?"
            msg = ("세션 시작 run=%s (%s). 이전 run=%s 마지막 생존 %s (%s) -> 공백 %d초"
                   % (self.run_id, self.run_event, self.prev_run,
                      prev.get("last_seen"), how, self.gap_sec))
            if how == "running":
                # 이전 세션이 끝 인사 없이 끊겼다. 실제 공백은 이보다 최대 beat_sec 짧다.
                msg += (" (Actions 기록 아직 없음, 생존 기록 기준. 오차 최대 %d초. 나중에 재계산)"
                        % self.beat_sec)
            self._gap_retry = 5              # 이후 생존 기록 때 Actions 기록을 다시 본다
        else:
            msg = "세션 시작 run=%s (%s). 이전 기록 없음" % (self.run_id, self.run_event)
        self.event(msg)
        self.beat(force=True)
        return self.gap_sec

    def stop(self, status, checks=0):
        """세션이 끝날 때. status: 'ended'(시간 다 됨) / 'cancelled'(이어받기로 끊김) 등."""
        self.beat(force=True, status=status, checks=checks)
        self.event("세션 종료 run=%s (%s)" % (self.run_id, status))

    # ------------------------------------------------------------ 생존 기록

    def beat(self, force=False, status="running", checks=0, extra=None, ping=True):
        """
        beat_sec 마다 health.json 을 갱신하고 외부 ping 을 보낸다.

        ping=False 면 기록만 남기고 외부 ping 은 건너뛴다. 멈춰 있을 때 쓴다.
        ping 이 계속 나가면 바깥 감시에는 정상으로 보이기 때문이다.
        """
        if not self.active:
            return
        now = now_kst()
        # 간격 검사와 갱신은 한 묶음이어야 한다. 메인과 Watchdog 이 거의 같은 때에
        # 들어오면 둘 다 검사를 통과해서 기록이 두 번 올라간다.
        # 실제 쓰기(_put)는 이 블록 밖에서 하므로 잠금을 오래 쥐지 않는다.
        with self._lock:
            if (not force and self.last_beat
                    and (now - self.last_beat).total_seconds() < self.beat_sec):
                return
            self.last_beat = now
        if ping:
            self._ping()
        if not self.enabled:
            return
        self._retry_gap()
        data = {
            "status": status,
            "run_id": self.run_id,
            "run_event": self.run_event,
            "started": fmt(self.started),
            "last_seen": fmt(now),
            "checks": checks,
            "gap_at_start_sec": self.gap_sec,
            "prev_run": self.prev_run,
            "relay": self.relay,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }
        if extra:
            data.update(extra)
        body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self._put("health.json", body, "beat")

    def error(self, msg):
        self.error_count += 1
        self.last_error = "%s %s" % (fmt(now_kst()), msg[:200])

    def ping_fail(self, reason=""):
        """
        외부 감시에 "지금 문제가 있다" 를 즉시 알린다 (ping 주소 + /fail).

        유예 시간을 기다리지 않고 바로 알림이 나가므로, 스스로 풀 수 없어서
        사람이 손을 대야 하는 상황에서만 쓴다. 스스로 복구되는 공백까지
        여기로 보내면 알림이 잦아져서 아무도 보지 않게 된다.
        이미 실패로 잡혀 있는 상태면 중복 알림은 가지 않는다.
        """
        if not (self.active and self.ping_url):
            return False
        try:
            requests.get(self.ping_url.rstrip("/") + "/fail", timeout=10)
        except requests.RequestException as e:
            log("외부 감시 실패 보고 예외: %s" % str(e)[:160])
            return False
        log("외부 감시에 실패를 보고했습니다%s" % ((" - " + reason) if reason else ""))
        return True

    def _ping(self):
        if not self.ping_url:
            return
        try:
            requests.get(self.ping_url, timeout=10)
            self.last_ping = now_kst()
        except requests.RequestException as e:
            log("health ping 실패: %s" % str(e)[:120])

    # ------------------------------------------------------------ 사건 기록

    def event(self, msg):
        """health.log 에 한 줄 덧붙인다. (실행 로그에도 같이 남긴다)"""
        line = "[%s] %s" % (fmt(now_kst()), msg)
        log("health: " + msg)
        if not (self.enabled and self.active):
            return
        old = self._read_text("health.log") or ""
        lines = old.splitlines()[-(LOG_KEEP - 1):] + [line]
        self._put("health.log", "\n".join(lines) + "\n", "event")

    def summary(self):
        """/health 명령 응답."""
        now = now_kst()
        up = int((now - self.started).total_seconds() // 60)
        out = ["health 기록: %s (끄기 /health off, 켜기 /health on)"
               % ("켜짐" if self.active else "꺼짐"),
               "세션 run=%s (%s)" % (self.run_id, self.run_event),
               "시작 %s (%d분째)" % (self.started.strftime("%m-%d %H:%M:%S"), up)]
        if self.gap_sec is not None:
            out.append("시작 시 감시 공백 %d초 (이전 run=%s)" % (self.gap_sec, self.prev_run))
        r = self.relay
        if r["requested_at"]:
            out.append("다음 workflow run 요청: %s %s (%d회 시도)"
                       % (r["requested_at"], "성공" if r["ok"] else "실패", r["tries"]))
        else:
            out.append("다음 workflow run 요청: 아직")
        out.append("오류 %d건%s" % (self.error_count,
                                   (" / 최근: " + self.last_error) if self.last_error else ""))
        out.append("외부 ping: %s" % (
            "꺼짐" if not self.ping_url
            else (self.last_ping.strftime("%H:%M:%S") if self.last_ping else "아직")))
        if self.extra_fn:
            try:
                out.extend(self.extra_fn())
            except Exception as e:            # 부가 정보 때문에 응답이 막히면 안 된다
                out.append("(부가 정보 오류: %s)" % str(e)[:100])
        if self.enabled:
            out.append("기록: https://github.com/%s/tree/%s" % (self.repo, self.branch))
        return "\n".join(out)

    # ------------------------------------------------------------ 다음 세션 띄우기

    def request_next(self, workflow, ref, inputs=None):
        """
        workflow_dispatch 로 다음 실행을 요청한다. 성공하면 True.
        새 실행이 뜨면 concurrency 설정에 따라 지금 세션은 끊기고 이어받는다.
        """
        self.relay["tries"] += 1
        self.relay["requested_at"] = fmt(now_kst())
        if not self.enabled:
            self.relay["ok"] = False
            return False
        try:
            r = requests.post(
                (API % self.repo) + "/actions/workflows/%s/dispatches" % workflow,
                headers=self._headers(),
                data=json.dumps({"ref": ref, "inputs": inputs or {}}).encode("utf-8"),
                timeout=20)
        except requests.RequestException as e:
            self.relay["ok"] = False
            self.event("다음 workflow run 요청 예외 (%d번째): %s" % (self.relay["tries"], str(e)[:160]))
            return False
        ok = r.status_code == 204
        self.relay["ok"] = ok
        if ok:
            self.event("다음 workflow run 요청 성공 (%d번째)" % self.relay["tries"])
        else:
            self.event("다음 workflow run 요청 실패 %s (%d번째): %s"
                       % (r.status_code, self.relay["tries"], r.text[:200]))
        return ok

    # ------------------------------------------------------------ GitHub API

    def _prev_step_end(self, run_id):
        """
        이전 실행에서 'python watch.py' 단계가 끝난 시각(KST)과 결과를 돌려준다.
        아직 종료 처리 전이거나 조회에 실패하면 (None, None).
        기다리지 않는다. 감시 시작이 늦어지면 그만큼 공백이 늘기 때문이다.
        """
        if not run_id or not str(run_id).isdigit() or str(run_id) == str(self.run_id):
            return None, None
        try:
            r = requests.get((API % self.repo) + "/actions/runs/%s/jobs" % run_id,
                             headers=self._headers(), timeout=20)
            if r.status_code != 200:
                log("health: 이전 실행 조회 실패 %s" % r.status_code)
                return None, None
            for job in r.json().get("jobs", []):
                for step in job.get("steps", []):
                    if "watch.py" not in (step.get("name") or ""):
                        continue
                    done = step.get("completed_at")
                    if done:
                        t = datetime.strptime(done, "%Y-%m-%dT%H:%M:%SZ")
                        return (t.replace(tzinfo=timezone.utc).astimezone(KST),
                                step.get("conclusion"))
        except (requests.RequestException, ValueError) as e:
            log("health: 이전 실행 조회 예외: %s" % str(e)[:120])
        return None, None

    def _retry_gap(self):
        """시작 때 못 구한 이전 실행 종료 시각을 다시 조회해 공백을 고쳐 기록한다."""
        if self._gap_retry <= 0:
            return
        self._gap_retry -= 1
        ended, conclusion = self._prev_step_end(self.prev_run)
        if not ended:
            return
        self._gap_retry = 0
        old = self.gap_sec
        self.gap_sec = int((self.started - ended).total_seconds())
        self.event("공백 재계산: 이전 run=%s 종료 %s (%s) -> %d초 (Actions 기록 기준, 처음 계산 %s초)"
                   % (self.prev_run, fmt(ended), conclusion or "?", self.gap_sec, old))

    def _headers(self):
        return {"Authorization": "Bearer %s" % self.token,
                "Accept": "application/vnd.github+json"}

    def _ensure_branch(self):
        """health 브랜치가 없으면 기준 브랜치에서 만든다."""
        try:
            r = requests.get((API % self.repo) + "/git/ref/heads/%s" % self.branch,
                             headers=self._headers(), timeout=20)
            if r.status_code == 200:
                return
            if r.status_code != 404:
                log("health 브랜치 확인 실패 %s" % r.status_code)
                return
            b = requests.get((API % self.repo) + "/git/ref/heads/%s" % self.base_branch,
                             headers=self._headers(), timeout=20)
            if b.status_code != 200:
                log("health: 기준 브랜치 조회 실패 %s" % b.status_code)
                return
            c = requests.post((API % self.repo) + "/git/refs", headers=self._headers(),
                              data=json.dumps({"ref": "refs/heads/%s" % self.branch,
                                               "sha": b.json()["object"]["sha"]}),
                              timeout=20)
            log("health 브랜치 생성 %s" % c.status_code)
        except requests.RequestException as e:
            log("health 브랜치 준비 예외: %s" % str(e)[:160])

    def _get(self, path):
        try:
            r = requests.get((API % self.repo) + "/contents/%s" % path,
                             headers=self._headers(), params={"ref": self.branch}, timeout=20)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            if r.status_code == 404:
                self._sha.pop(path, None)
            return None
        body = r.json()
        self._sha[path] = body.get("sha")
        try:
            return base64.b64decode(body.get("content", "")).decode("utf-8")
        except (ValueError, TypeError):
            return None

    def _read_text(self, path):
        return self._get(path)

    def _read_json(self, path):
        t = self._get(path)
        if not t:
            return None
        try:
            return json.loads(t)
        except ValueError:
            return None

    def _put(self, path, body, reason, retry=True):
        with self._lock:
            self._put_one(path, body, reason, retry)

    def _put_one(self, path, body, reason, retry=True):
        payload = {
            "message": "health: %s" % reason,
            "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self._sha.get(path):
            payload["sha"] = self._sha[path]
        try:
            r = requests.put((API % self.repo) + "/contents/%s" % path,
                             headers=self._headers(),
                             data=json.dumps(payload).encode("utf-8"), timeout=20)
        except requests.RequestException as e:
            log("health 기록 예외: %s" % str(e)[:160])
            return
        if r.status_code in (200, 201):
            self._sha[path] = r.json().get("content", {}).get("sha")
        elif r.status_code in (409, 422) and retry:
            # sha 가 어긋났다 (이전 세션이 마지막에 썼거나 처음 쓰는 파일). 다시 읽고 한 번 더.
            self._get(path)
            self._put_one(path, body, reason, retry=False)
        else:
            log("health 기록 실패 %s: %s" % (r.status_code, r.text[:200]))


# ---------------------------------------------------------------- 멈춤 감시

class Watchdog:
    """
    메인 루프가 멈추지 않았는지 별도 스레드에서 지켜본다.

    생존 기록을 메인 루프 안에서 남기면, 루프가 멈출 때 기록도 같이 멈춘다.
    11-06 16:01 에 실제로 그렇게 멈췄는데, 오류 한 줄 없이 로그와 생존 기록이
    동시에 끊겨서 어느 단계였는지조차 특정하지 못했다. 기록을 이 스레드로
    옮기면 메인이 멈춰도 "어느 단계에서 몇 초째 진행이 없는지" 가 계속 남는다.

    임계를 넘기면 메인 스레드의 스택을 떠서 텔레그램으로 보낸다. 표준 출력이
    막힌 것이 원인인 경우에도 단서가 빠져나가도록, 로그가 아니라 네트워크로
    내보내고 health.json 에도 같이 남긴다.
    """

    def __init__(self, health, tg=None, stall_sec=180,
                 exit_sec=0, relay_fn=None, min_uptime_sec=600):
        self.health = health
        self.tg = tg
        # 0 이하면 멈춤 알림만 끈다. 생존 기록은 이 스레드가 계속 남겨야 하므로
        # 스레드 자체는 그대로 돌린다.
        self.stall_sec = max(30, stall_sec) if stall_sec > 0 else float("inf")
        # 이만큼 멈춰 있으면 다음 세션을 띄우고 스스로 끝낸다. 0 이면 안 끝낸다.
        # 알림 임계(stall_sec)보다 뒤여야 스택을 먼저 남기고 나간다.
        self.exit_sec = max(stall_sec, exit_sec) if exit_sec > 0 else float("inf")
        self.relay_fn = relay_fn
        # 기동하고 이 시간이 지나기 전에 멈춘 경우에는 스스로 끝내지 않는다.
        # 올라오자마자 또 멈추는 상황이면 재기동이 끝없이 돌 수 있다.
        self.min_uptime_sec = min_uptime_sec
        self.started = now_kst()
        self._last_bail = None                   # 마지막으로 끝내기를 시도한 시각
        self._warned_uptime = False              # 기동 직후 멈춤을 이미 알렸는지
        self._warned_relay = False               # workflow run 요청 실패를 이미 알렸는지
        self._lock = threading.Lock()
        self._stage = "기동"
        self._at = now_kst()
        self._checks = 0
        self._main_tid = threading.get_ident()   # 이 객체를 만든 스레드가 메인이다
        self._stalled = False                    # 멈춤을 이미 알렸는지
        self._stop = threading.Event()
        self._thread = None
        # 같은 멈춤은 한 번만 알리지만, 임계 근처를 오가면 멈춤/해제가 번갈아
        # 반복될 수 있다. 그 경우 알림이 쌓이지 않도록 세션당 횟수를 묶어 둔다.
        # 넘어서면 로그에만 남긴다 (진단 자체는 계속한다).
        self.max_alerts = 5
        self._alerts = 0

    # ------------------------------------------------------------ 메인이 부른다

    def mark(self, stage, checks=None):
        """메인 루프가 단계를 지날 때마다 부른다. 여기서는 기록만 하고 멈추지 않는다."""
        with self._lock:
            self._stage = stage
            self._at = now_kst()
            if checks is not None:
                self._checks = checks

    def start(self):
        if self._thread:
            return
        # 데몬으로 둬서 메인이 끝나면 같이 내려간다
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()
        if self.stall_sec == float("inf"):
            log("생존 기록을 별도 스레드로 남깁니다. (멈춤 알림은 꺼져 있음)")
        else:
            log("멈춤 감시 시작 (%d초 이상 진행이 없으면 스택을 남깁니다)" % self.stall_sec)

    def stop(self):
        self._stop.set()

    def summary(self):
        """/health 에 붙일 현재 단계."""
        stage, at, _checks = self._snapshot()
        return "현재 단계: %s (%d초 경과)" % (stage, int((now_kst() - at).total_seconds()))

    # ------------------------------------------------------------ 감시 스레드

    def _snapshot(self):
        with self._lock:
            return self._stage, self._at, self._checks

    def _main_stack(self):
        """메인 스레드가 지금 어느 줄에 있는지."""
        frame = sys._current_frames().get(self._main_tid)
        if frame is None:
            return "(메인 스레드를 찾지 못했습니다)"
        try:
            return "".join(traceback.format_stack(frame))
        except Exception as e:                   # 스택을 뜨다 실패해도 감시는 계속한다
            return "(스택을 뜨지 못했습니다: %s)" % str(e)[:120]

    def _run(self):
        # 생존 기록 간격보다 촘촘히 깨어나야 멈춤을 제때 잡는다.
        # beat() 는 안에서 간격을 보고 걸러내므로 자주 불러도 기록은 간격대로 남는다.
        tick = max(5, min(10, self.health.beat_sec))
        while not self._stop.wait(tick):
            try:
                self._tick()
            except Exception as e:
                # 이 스레드가 죽으면 생존 기록도 멈춤 감지도 같이 사라진다.
                # 메인은 멀쩡히 돌고 있어서 아무도 눈치채지 못하므로,
                # 무슨 일이 나든 루프만은 지킨다.
                log("멈춤 감시: 예외가 났지만 계속합니다 - %s" % str(e)[:200])

    def _tick(self):
        stage, at, checks = self._snapshot()
        idle = int((now_kst() - at).total_seconds())
        try:
            # 멈춰 있는 동안에는 외부 ping 을 보내지 않는다. 이 스레드는 메인이
            # 멈춰도 살아 있어서, 그대로 ping 하면 밖에서는 정상으로 보인다.
            # 자가 복구까지 실패한 경우를 외부 감시가 잡으려면 여기서 끊어야 한다.
            self.health.beat(checks=checks, ping=(idle < self.stall_sec),
                             extra={"stage": stage, "stage_idle_sec": idle})
        except Exception as e:                   # 기록이 실패해도 감시는 멈추지 않는다
            log("멈춤 감시: 생존 기록 예외 %s" % str(e)[:160])

        if idle >= self.stall_sec and not self._stalled:
            self._stalled = True
            self._report(stage, idle, checks)
        elif idle < self.stall_sec and self._stalled:
            self._stalled = False
            self._recovered(stage)

        # 알림만으로는 감시가 돌아오지 않는다. 더 오래 멈춰 있으면
        # 다음 세션을 띄우고 이 세션을 끝낸다. 요청이 실패하면 1분 뒤 다시 시도한다.
        if idle >= self.exit_sec and (
                self._last_bail is None
                or (now_kst() - self._last_bail).total_seconds() >= 60):
            self._last_bail = now_kst()
            self._bail_out(stage, idle, checks)

    def _report(self, stage, idle, checks=0):
        self._alerts += 1
        # 상한을 넘으면 밖으로 내보내는 것(텔레그램/기록/스택 덤프)을 멈추고
        # 로그만 남긴다. 알림이 쌓여서 그 자체가 부담이 되는 일을 막는다.
        quiet = self._alerts > self.max_alerts

        stack = self._main_stack()
        log("[멈춤] '%s' 단계에서 %d초째 진행이 없습니다.%s 메인 스레드 스택:\n%s"
            % (stage, idle, " (알림 상한 초과, 로그만 남깁니다)" if quiet else "", stack))
        if quiet:
            return

        # 표준 출력이 막혀 위 로그가 안 보이는 경우를 대비해 stderr 로도 한 번 더 남긴다
        try:
            faulthandler.dump_traceback(all_threads=True)
        except Exception:
            pass
        try:
            self.health.error("멈춤: %s 단계에서 %d초" % (stage, idle))
            self.health.event("[멈춤] %s 단계에서 %d초째 진행 없음" % (stage, idle))
            self.health.beat(force=True, checks=checks,
                             extra={"stage": stage, "stage_idle_sec": idle,
                                    "stall_stack": stack[-3000:]})
        except Exception as e:
            log("멈춤 감시: 기록 예외 %s" % str(e)[:160])
        if self.tg:
            tail = ("\n\n이번이 %d번째입니다. %d번을 넘으면 로그에만 남깁니다."
                    % (self._alerts, self.max_alerts)) if self._alerts > 1 else ""
            self.tg.send("감시 루프가 멈춰 있습니다.\n단계: %s\n%d초째 진행 없음\n\n%s%s"
                         % (stage, idle, stack[-1200:], tail))

    def _bail_out(self, stage, idle, checks):
        """멈춘 채로 너무 오래 있다. 다음 세션을 띄우고 이 세션을 끝낸다."""
        up = int((now_kst() - self.started).total_seconds())
        if up < self.min_uptime_sec:
            # 올라온 지 얼마 안 돼서 멈췄다. 여기서 새 세션을 띄우면 그 세션도
            # 같은 자리에서 멈춰 재기동이 끝없이 돌 수 있다. 알리고 기다린다.
            if self._warned_uptime:
                return
            self._warned_uptime = True
            log("[멈춤] 기동 %d초 만에 멈췄습니다. 재기동이 반복될 수 있어 끝내지 않고 기다립니다." % up)
            # 스스로 풀 수 없고 사람이 봐야 하는 상황이다. 유예를 기다리지 말고 바로 알린다.
            self.health.ping_fail("기동 직후 멈춤")
            if self.tg:
                self.tg.send("기동 %d분 만에 멈췄습니다.\n재기동이 끝없이 도는 것을 막기 위해 "
                             "스스로 끝내지 않고 기다립니다.\n단계: %s (%d초째)"
                             % (up // 60, stage, idle))
            return

        if not self.relay_fn:
            return

        log("[멈춤] %s 단계에서 %d초째입니다. 다음 세션을 띄우고 끝냅니다." % (stage, idle))
        try:
            ok = bool(self.relay_fn())
        except Exception as e:
            ok = False
            log("멈춤 감시: 다음 workflow run 요청 예외 %s" % str(e)[:160])

        if not ok:
            # 이어받을 세션이 없는데 끝내면 감시가 통째로 빈다. 버티면서 다시 시도한다.
            log("[멈춤] 다음 workflow run 요청이 실패했습니다. 끝내지 않고 1분 뒤 다시 시도합니다.")
            # 이어받을 세션이 없어서 감시가 통째로 비어 있다. 사람이 직접 띄워야 하므로
            # 유예를 기다리지 말고 바로 알린다. 이미 실패로 잡혀 있으면 중복 알림은 안 간다.
            self.health.ping_fail("다음 workflow run 요청 실패")
            if self.tg and not self._warned_relay:
                self._warned_relay = True
                self.tg.send("멈춰 있는데 다음 workflow run 요청이 실패했습니다.\n"
                             "감시가 비지 않도록 끝내지 않고 1분 간격으로 다시 시도합니다.\n"
                             "계속 실패하면 GitHub Actions 에서 watch 를 직접 실행해 주세요.")
            return

        try:
            self.health.event("[멈춤] %s 단계 %d초째. 다음 workflow run 을 요청하고 끝냅니다."
                              % (stage, idle))
            self.health.beat(force=True, status="stalled(exit)", checks=checks,
                             extra={"stage": stage, "stage_idle_sec": idle})
        except Exception as e:
            log("멈춤 감시: 종료 기록 예외 %s" % str(e)[:160])
        if self.tg:
            self.tg.send("멈춰 있어 이 세션을 끝냅니다. 새 workflow run 이 이어받습니다.\n"
                         "단계: %s (%d초째)" % (stage, idle))

        # 메인이 멈춰 있어서 정상 종료 경로(시그널 처리, health.stop)를 탈 수 없다.
        # 기록과 알림을 끝냈으니 여기서 바로 나간다. 멈춰서 끝낸 것이므로 0 이 아니다.
        log("[멈춤] 종료합니다.")
        time.sleep(2)                            # 알림이 빠져나갈 틈을 준다
        os._exit(1)

    def _recovered(self, stage):
        log("[멈춤 해제] 다시 진행합니다. 현재 단계 %s" % stage)
        if self._alerts > self.max_alerts:
            return                               # 멈춤을 조용히 넘겼으면 해제도 조용히
        try:
            self.health.event("[멈춤 해제] 다시 진행 (현재 %s 단계)" % stage)
        except Exception:
            pass
        if self.tg:
            self.tg.send("감시 루프가 다시 돌기 시작했습니다. (현재 단계: %s)" % stage)
