# 元ファイル: services/schedule_generator.py
#
# 何をするコード:
#   勤務予定・私用予定・登録シフト・食事などの固定生活時間・睡眠時間を除いた「空き時間」を計算し、
#   優先順位（受注案件 → AI学習 → 期限のある学習タスク → LP制作ドリル → Instagram制作ドリル）に
#   沿って、ルールベースで1日分の学習スケジュール案を自動生成する処理の抜粋。
#   生成AIは使用せず、Pythonのルールベース処理のみで計算する。
#   結果は保存せずプレビュー用データとして返し、ユーザーの承認後にのみDBへ保存する
#   （DB保存処理そのものは、この抜粋には含めていない）。
#
# 抜粋範囲についての注記:
#   元ファイルには、同じ考え方で複数日（最大31日）をまとめて生成する
#   generate_schedule_for_period() も存在するが、単日処理（本抜粋の
#   generate_schedule_for_date）と優先順位の考え方は同一のため、ここでは省略している。
#   また、呼び出し先の services.tasks / services.drills / services.calendar_events /
#   services.work_shifts / services.shift_types / services.goals / services.settings /
#   services.categories / services.schedule_blocks は、学習タスク・ドリル・勤務予定などを
#   DBから取得する一般的なサービス層であり、個人情報・DBの実データを含まないため
#   本リポジトリでは掲載していない。

from datetime import date, datetime, time, timedelta

from services import settings as settings_service
from services import calendar_events as calendar_event_store
from services import schedule_blocks as schedule_block_store
from services import tasks as task_service
from services import drills as drill_service
from services import categories as category_service
from services import goals as goal_service
from services import work_shifts as work_shift_service
from services import shift_types as shift_type_service


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _to_naive(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=None)


def get_fixed_life_blocks() -> list[dict]:
    import json
    raw = settings_service.get_setting("fixed_life_blocks", "[]")
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract(window: tuple[datetime, datetime], blocked: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """window（1日の活動時間帯）から、blocked（勤務・予定などで埋まっている時間帯）を取り除き、
    残った「空き時間」の区間リストを返す。
    """
    free = []
    cursor = window[0]
    for b_start, b_end in blocked:
        if b_end <= cursor or b_start >= window[1]:
            continue
        b_start = max(b_start, cursor)
        b_end = min(b_end, window[1])
        if b_start > cursor:
            free.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if cursor < window[1]:
        free.append((cursor, window[1]))
    return free


def consume_free_time(free_intervals, minutes_needed, min_block):
    """free_intervals（空き時間の区間リスト）から minutes_needed 分を、
    min_block 分以上のまとまりで消費する。
    戻り値: (消費した(start, end)のリスト, 更新後のfree_intervals, 確保できなかった残り分数)
    """
    taken = []
    updated = []
    remaining = minutes_needed
    for f_start, f_end in free_intervals:
        free_minutes = (f_end - f_start).total_seconds() / 60
        if remaining <= 0 or free_minutes < min_block:
            updated.append((f_start, f_end))
            continue
        take_minutes = min(remaining, free_minutes)
        block_end = f_start + timedelta(minutes=take_minutes)
        taken.append((f_start, block_end))
        remaining -= take_minutes
        if block_end < f_end:
            updated.append((block_end, f_end))
    return taken, updated, max(0, remaining)


def get_settings_map() -> dict:
    return settings_service.get_all_settings()


def compute_free_intervals_for_date(target_date: date, settings_map: dict | None = None) -> list[tuple[datetime, datetime]]:
    """指定日の「空き時間」を計算する中心関数。

    起床〜就寝（かつ学習終了時刻を超えない）の範囲から、以下を順に除外していく。
      1. Google Calendarの勤務予定（通勤時間・勤務後の休憩時間を加味）
      2. アプリ内に登録した勤務シフト（勤務区分が「学習可能時間を拘束する」場合のみ）
      3. 食事などの固定生活時間
      4. 既に固定済み・完了済みのスケジュールブロック（再生成時の重複配置を防ぐため）
    勤務日/休日で学習の開始時刻を変える（朝学習のON/OFF設定も考慮する）。
    """
    settings_map = settings_map if settings_map is not None else get_settings_map()
    wake = _parse_hhmm(settings_map.get("wake_time", "07:00"))
    sleep = _parse_hhmm(settings_map.get("sleep_time", "23:00"))
    commute = int(settings_map.get("commute_minutes", 30))
    post_break = int(settings_map.get("post_work_break_minutes", 30))

    day_start = datetime.combine(target_date, wake)
    day_end = datetime.combine(target_date, sleep)
    if day_end <= day_start:
        day_end += timedelta(days=1)

    # 学習終了時刻は、就寝時刻とは別に「学習スケジュールはここまでで必ず終える」境界として
    # 常に適用する（朝学習のON/OFFに関わらず有効）。
    study_end = datetime.combine(target_date, _parse_hhmm(settings_map.get("study_end_time", "22:30")))
    if study_end <= day_start:
        study_end += timedelta(days=1)
    day_end = min(day_end, study_end)

    blocked: list[tuple[datetime, datetime]] = []

    is_workday_for_window = _is_workday(target_date, settings_map)
    allow_morning_study = settings_map.get("allow_morning_study", "0") == "1"
    if is_workday_for_window:
        if not allow_morning_study:
            workday_start = datetime.combine(
                target_date, _parse_hhmm(settings_map.get("workday_study_start_time", "20:00"))
            )
            if workday_start > day_start:
                blocked.append((day_start, min(workday_start, day_end)))
    else:
        holiday_start = datetime.combine(
            target_date, _parse_hhmm(settings_map.get("holiday_study_start_time", "10:00"))
        )
        if holiday_start > day_start:
            blocked.append((day_start, min(holiday_start, day_end)))

    events = calendar_event_store.list_events(target_date.isoformat(), target_date.isoformat())
    for e in events:
        start = _to_naive(e["start_datetime"])
        end = _to_naive(e["end_datetime"])
        if e["event_type"] == "勤務":
            start -= timedelta(minutes=commute)
            end += timedelta(minutes=commute + post_break)
        blocked.append((start, end))

    # アプリ内で登録した勤務シフト（work_shifts）は、区分マスタ(shift_types)の
    # 「学習可能時間を拘束するか」フラグに従ってブロックするか決める。
    shift = work_shift_service.get_shift(target_date.isoformat())
    if shift:
        style = shift_type_service.style_for(shift["shift_type"])
        if style["restricts_study_time"]:
            s_start = datetime.combine(target_date, _parse_hhmm(shift["start_time"]))
            s_end = datetime.combine(target_date, _parse_hhmm(shift["end_time"]))
            if shift["crosses_midnight"] or s_end <= s_start:
                s_end += timedelta(days=1)
            s_start -= timedelta(minutes=commute)
            s_end += timedelta(minutes=commute + post_break)
            blocked.append((s_start, s_end))

    for fb in get_fixed_life_blocks():
        b_start = datetime.combine(target_date, _parse_hhmm(fb["start"]))
        b_end = b_start + timedelta(minutes=int(fb["duration_minutes"]))
        blocked.append((b_start, b_end))

    # 固定済み予定（is_fixed=1）・完了済み（status='完了'）のブロックも占有時間として扱う
    # （再生成時に同じ時間帯へ別タスクが重複配置されるのを防ぐ）。
    existing = schedule_block_store.list_blocks(target_date.isoformat(), target_date.isoformat())
    protected_existing = [b for b in existing if b["is_fixed"] or b["status"] == "完了"]
    for b in protected_existing:
        b_start = datetime.combine(target_date, _parse_hhmm(b["start_time"]))
        b_end = datetime.combine(target_date, _parse_hhmm(b["end_time"]))
        blocked.append((b_start, b_end))

    blocked = _merge_intervals(blocked)
    return _subtract((day_start, day_end), blocked)


def _is_workday(target_date: date, settings_map: dict | None = None) -> bool:
    """その日が「勤務日」（学習可能時間を拘束する予定がある日）かどうかを判定する。
    受注案件タスクの1日あたり配置上限を、勤務日／休日で切り分けるために使う。
    """
    events = calendar_event_store.list_events(target_date.isoformat(), target_date.isoformat())
    if any(e["event_type"] == "勤務" for e in events):
        return True
    shift = work_shift_service.get_shift(target_date.isoformat())
    if shift:
        style = shift_type_service.style_for(shift["shift_type"])
        if style["restricts_study_time"]:
            return True
    return False


def _completed_minutes_for_task(task: dict) -> int:
    """タスクがこれまでに完了した分（分）。本人入力の実績と、カレンダー上で完了にした
    スケジュールブロックの合計時間のうち、大きい方を採用する（二重計上を避けるため）。
    """
    manual = task.get("actual_minutes") or 0
    from_blocks = schedule_block_store.completed_minutes_for_task(task["id"])
    return max(manual, from_blocks)


def _completed_minutes_for_drill(drill: dict) -> int:
    manual = drill.get("minutes_spent") or 0
    from_blocks = schedule_block_store.completed_minutes_for_drill(drill["id"])
    return max(manual, from_blocks)


def _task_remaining_minutes(task: dict, fallback_minutes: int) -> int:
    """タスクの残り作業時間（分）＝見積時間－完了済み分。"""
    estimated = task.get("estimated_minutes")
    base = estimated if estimated and estimated > 0 else fallback_minutes
    return max(0, base - _completed_minutes_for_task(task))


def _drill_remaining_minutes(drill: dict, fallback_minutes: int) -> int:
    estimated = drill.get("estimated_minutes")
    base = estimated if estimated and estimated > 0 else fallback_minutes
    return max(0, base - _completed_minutes_for_drill(drill))


def _build_project_task_order(project_tasks: list[dict]) -> dict[int, list[int]]:
    """案件ID→工程タスクIDの一覧（登録順＝工程順）。"""
    order: dict[int, list[int]] = {}
    for t in sorted(project_tasks, key=lambda t: t["id"]):
        pid = t.get("project_id")
        if pid is None:
            continue
        order.setdefault(pid, []).append(t["id"])
    return order


def _is_stage_eligible(task: dict, remaining_map: dict[int, float], order_map: dict[int, list[int]]) -> bool:
    """同じ案件の前の工程がすべて終わっていなければ、次の工程は配置しない（工程順の維持）。"""
    pid = task.get("project_id")
    if pid is None:
        return True
    seq = order_map.get(pid, [])
    if task["id"] not in seq:
        return True
    idx = seq.index(task["id"])
    if idx == 0:
        return True
    prev_id = seq[idx - 1]
    return remaining_map.get(prev_id, 0) <= 0


def _is_urgent_task(task: dict, current_date: date, remaining_minutes: float, urgency_reference_cap: int) -> bool:
    """締切までに、1日あたりの配置上限のペースでは間に合わない場合に
    「締切優先（上限を無視して配置する）」と判定する。
    """
    if not task.get("due_date") or remaining_minutes <= 0:
        return False
    try:
        deadline = date.fromisoformat(task["due_date"])
    except ValueError:
        return False
    days_left = (deadline - current_date).days + 1
    if days_left <= 0:
        return True
    cap = max(1, urgency_reference_cap)
    return remaining_minutes > days_left * cap


def _spread_cap_minutes(pool_remaining_total: float, days_left: int, hard_cap: int, min_block: int) -> int:
    """期限タスク・ドリルの「1日あたり配置上限」を、残量と残り日数から計算する
    （残量を残り日数で均等割りし、特定の日に偏って配置されるのを防ぐ）。
    """
    if pool_remaining_total <= 0 or days_left <= 0 or hard_cap <= 0:
        return 0
    even_share = pool_remaining_total / days_left
    cap = min(hard_cap, even_share)
    if cap < min_block:
        cap = min(hard_cap, min_block)
    return int(cap)


def _allocate_capped_items_for_day(
    candidates: list[dict],
    remaining_map: dict[int, float],
    order_map: dict[int, list[int]],
    free_intervals: list[tuple[datetime, datetime]],
    min_block: int,
    cap_today: int,
    urgency_reference_cap: int,
    current_date: date,
):
    """1日分の項目（受注案件タスク・期限タスク・ドリル）を上限つきで配置する汎用関数。

    - 締切に間に合わないペースの項目は、1日の上限を無視して優先的に配置する。
    - それ以外は、本日まだ使っていない上限（cap_today）の範囲内でのみ配置し、
      残りは別日に回す（特定の日への集中を防ぐ）。
    """
    eligible = [
        t for t in candidates
        if remaining_map.get(t["id"], 0) > 0 and _is_stage_eligible(t, remaining_map, order_map)
    ]
    urgent_ids = {
        t["id"] for t in eligible
        if _is_urgent_task(t, current_date, remaining_map[t["id"]], urgency_reference_cap)
    }

    placements: list[tuple[dict, datetime, datetime]] = []

    urgent_placed_total = 0
    for t in eligible:
        if t["id"] not in urgent_ids:
            continue
        want = remaining_map[t["id"]]
        taken, free_intervals, not_placed = consume_free_time(free_intervals, want, min_block)
        placed = want - not_placed
        if placed > 0:
            placements.extend((t, s, e) for s, e in taken)
            remaining_map[t["id"]] -= placed
            urgent_placed_total += placed

    budget_left = max(0, cap_today - urgent_placed_total)
    normal_placed_total = 0
    for t in eligible:
        if t["id"] in urgent_ids:
            continue
        if budget_left <= 0:
            break
        remaining_now = remaining_map.get(t["id"], 0)
        if remaining_now <= 0:
            continue
        want = min(remaining_now, budget_left)
        taken, free_intervals, not_placed = consume_free_time(free_intervals, want, min_block)
        placed = want - not_placed
        if placed > 0:
            placements.extend((t, s, e) for s, e in taken)
            remaining_map[t["id"]] -= placed
            normal_placed_total += placed
            budget_left -= placed

    used_today = urgent_placed_total + normal_placed_total
    urgent_overflow = max(0, urgent_placed_total - cap_today)
    return placements, free_intervals, used_today, urgent_overflow


def generate_schedule_for_date(target_date: date) -> dict:
    """指定日の学習スケジュール案を生成する（DBへは保存しない）。

    優先順位:
      1. 勤務・私用予定・睡眠（compute_free_intervals_for_date で除外済み）
      2. 受注案件（締切が近いものから。1日の上限までで複数日へ分散し、
         締切に間に合わない場合のみ上限を超えて優先配置する）
      3. AI学習：基本目標（分/日）のうち、登録済みタスク分を先に配置
      4. 期限がある学習タスク
      5. LP制作ドリル
      6. Instagram制作ドリル
      7. AI学習の自由学習枠（4〜6で埋まらなかったAI学習目標の不足分を補完）
    """
    settings_map = get_settings_map()
    ai_goal = int(settings_map.get("ai_study_goal_minutes", 120))
    min_block = int(settings_map.get("min_study_block_minutes", 30))
    cap_workday = int(settings_map.get("project_daily_cap_workday_minutes", 90))
    cap_holiday = int(settings_map.get("project_daily_cap_holiday_minutes", 180))
    cap_other_workday = int(settings_map.get("other_daily_cap_workday_minutes", 60))
    cap_other_holiday = int(settings_map.get("other_daily_cap_holiday_minutes", 120))

    free_intervals = compute_free_intervals_for_date(target_date, settings_map)

    existing = schedule_block_store.list_blocks(target_date.isoformat(), target_date.isoformat())
    protected_existing = [b for b in existing if b["is_fixed"] or b["status"] == "完了"]

    categories = {c["name"]: c["id"] for c in category_service.list_categories()}
    generated_blocks = []
    warnings = []

    def add_block(start, end, category_name, title, task_id=None, drill_id=None):
        generated_blocks.append({
            "block_date": target_date.isoformat(),
            "start_time": start.strftime("%H:%M"),
            "end_time": end.strftime("%H:%M"),
            "category_id": categories.get(category_name),
            "category_name": category_name,
            "title": title,
            "task_id": task_id,
            "drill_id": drill_id,
            "is_fixed": 0,
        })

    all_tasks = task_service.list_tasks()

    # --- 2. 受注案件（案件タスク）。締切優先＋1日の上限で複数日に分散配置 ---
    project_tasks = sorted(
        [t for t in all_tasks if t["category_name"] == "受注案件" and t["status"] != "完了"],
        key=lambda t: (t["due_date"] is None, t["due_date"] or "", t["priority"]),
    )
    order_map = _build_project_task_order(project_tasks)
    remaining_project = {t["id"]: _task_remaining_minutes(t, min_block) for t in project_tasks}
    is_workday = _is_workday(target_date, settings_map)
    cap_today = cap_workday if is_workday else cap_holiday

    placements, free_intervals, _used_today, urgent_overflow = _allocate_capped_items_for_day(
        project_tasks, remaining_project, order_map, free_intervals, min_block, cap_today, cap_workday, target_date,
    )
    for t, s, e in placements:
        add_block(s, e, "受注案件", t["title"], task_id=t["id"])

    # --- 3. AI学習：基本目標のうち、登録済みタスク分を先に配置する ---
    goal_linked_task_ids = goal_service.active_linked_task_ids(target_date.isoformat())
    ai_tasks = sorted(
        [t for t in all_tasks if t["category_name"] == "AI学習" and t["status"] != "完了"],
        key=lambda t: (t["id"] not in goal_linked_task_ids, t["due_date"] is None, t["due_date"] or "", t["priority"]),
    )
    ai_remaining = ai_goal
    for t in ai_tasks:
        if ai_remaining <= 0:
            break
        task_remaining = _task_remaining_minutes(t, ai_goal)
        if task_remaining <= 0:
            continue
        want = min(task_remaining, ai_remaining)
        taken, free_intervals, leftover = consume_free_time(free_intervals, want, min_block)
        for s, e in taken:
            add_block(s, e, "AI学習", t["title"], task_id=t["id"])
        ai_remaining -= (want - leftover)

    # --- 4. 期限がある学習タスク（AI学習・受注案件以外）---
    due_tasks = sorted(
        [
            t for t in all_tasks
            if t["category_name"] not in ("AI学習", "受注案件") and t["status"] != "完了" and t["due_date"]
        ],
        key=lambda t: (t["id"] not in goal_linked_task_ids, t["due_date"], t["priority"]),
    )
    remaining_due = {t["id"]: _task_remaining_minutes(t, min_block) for t in due_tasks}
    other_hard_cap = cap_other_workday if is_workday else cap_other_holiday
    due_cap_today = _spread_cap_minutes(sum(remaining_due.values()), 1, other_hard_cap, min_block)
    placements, free_intervals, _used, _overflow = _allocate_capped_items_for_day(
        due_tasks, remaining_due, {}, free_intervals, min_block, due_cap_today, cap_other_workday, target_date,
    )
    for t, s, e in placements:
        add_block(s, e, t["category_name"] or "その他", t["title"], task_id=t["id"])

    # --- 5. LP制作ドリル（1日あたり上限つきで複数日に分散） ---
    lp_drills = [d for d in drill_service.list_drills() if d["category_name"] == "LP制作" and d["progress_status"] in ("未着手", "進行中")]
    remaining_lp = {d["id"]: _drill_remaining_minutes(d, min_block) for d in lp_drills}
    lp_cap_today = _spread_cap_minutes(sum(remaining_lp.values()), 1, other_hard_cap, min_block)
    placements, free_intervals, _used, _overflow = _allocate_capped_items_for_day(
        lp_drills, remaining_lp, {}, free_intervals, min_block, lp_cap_today, cap_other_workday, target_date,
    )
    for d, s, e in placements:
        add_block(s, e, "LP制作", d["title"], drill_id=d["id"])

    # --- 6. Instagram制作ドリル（同上） ---
    ig_drills = [d for d in drill_service.list_drills() if d["category_name"] == "Instagram投稿制作" and d["progress_status"] in ("未着手", "進行中")]
    remaining_ig = {d["id"]: _drill_remaining_minutes(d, min_block) for d in ig_drills}
    ig_cap_today = _spread_cap_minutes(sum(remaining_ig.values()), 1, other_hard_cap, min_block)
    placements, free_intervals, _used, _overflow = _allocate_capped_items_for_day(
        ig_drills, remaining_ig, {}, free_intervals, min_block, ig_cap_today, cap_other_workday, target_date,
    )
    for d, s, e in placements:
        add_block(s, e, "Instagram投稿制作", d["title"], drill_id=d["id"])

    # --- 7. AI学習の自由学習枠（4〜6で埋まらなかった不足分を補完） ---
    if ai_remaining > 0:
        taken, free_intervals, leftover = consume_free_time(free_intervals, ai_remaining, min_block)
        for s, e in taken:
            add_block(s, e, "AI学習", "AI学習（自由学習）")
        ai_remaining = leftover

    ai_scheduled = ai_goal - ai_remaining
    generated_blocks.sort(key=lambda b: b["start_time"])

    return {
        "date": target_date.isoformat(),
        "generated_blocks": generated_blocks,
        "fixed_blocks": protected_existing,
        "warnings": warnings,
        "ai_goal_minutes": ai_goal,
        "ai_scheduled_minutes": int(ai_scheduled),
        "remaining_free_minutes": int(sum((e - s).total_seconds() / 60 for s, e in free_intervals)),
        "is_workday": is_workday,
        "project_daily_cap_minutes": cap_today,
        "urgent_project_overflow_minutes": int(urgent_overflow),
    }
