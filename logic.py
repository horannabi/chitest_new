#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
알림 판단 로직. 네트워크/상태 저장과 분리해서 독립적으로 검증할 수 있게 한다.
"""


def decide(prev_ids, prev_zero, items, announced):
    """
    한 번의 조회 결과(items)를 직전 상태와 비교해서 다음 상태와 알림 목록을 정한다.

    prev_ids: 이전에 알고 있던 회차 ID 집합 (baseline 이면 빈 집합)
    prev_zero: 매진으로 기록해 둔 회차 ID 집합
    items: parser.parse_screen_plan() 결과
    announced: 이 날짜를 이미 한 번 "찾았습니다" 로 알렸는지

    반환: (new_ids, new_zero, new_announced, alerts)
    """
    alerts = []
    cur_ids = {it["screenplanid"] for it in items}

    if not items:
        # 아직 편성이 안 올라왔다. 아무것도 바꾸지 않는다.
        return prev_ids, prev_zero, announced, alerts

    if not announced:
        # 이 날짜에 처음으로 편성을 찾았다. 전체를 한 번 알리고 기준으로 삼는다.
        alerts.append(("found", list(items)))
        new_zero = {it["screenplanid"] for it in items if it["soldout"]}
        return cur_ids, new_zero, True, alerts

    added = cur_ids - prev_ids
    if added:
        new_items = [it for it in items if it["screenplanid"] in added]
        alerts.append(("added", new_items))

    opened = []
    new_zero = set(prev_zero)
    for it in items:
        sid = it["screenplanid"]
        if sid in prev_zero and not it["soldout"]:
            opened.append(it)
            new_zero.discard(sid)
        elif it["soldout"]:
            new_zero.add(sid)
    if opened:
        alerts.append(("opened", opened))

    return cur_ids, new_zero, announced, alerts
