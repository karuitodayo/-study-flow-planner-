# 元ファイル: services/timer.py
#
# 何をするコード:
#   学習タスク・ドリル・受注案件タスクの作業時間を計測する「作業タイマー」機能。
#   同時に起動できるタイマーは1件のみ。経過時間は「これまでに確定した秒数
#   (accumulated_seconds)」と、計測中であれば「現在の区間の開始時刻
#   (segment_started_at) からの経過」を都度計算する方式のため、画面の再描画・
#   ブラウザの再読み込みをまたいでも正しく経過時間を復元できる。
#
# 抜粋範囲についての注記:
#   このファイルは元から約150行で自己完結しているため、ほぼ全体を掲載している。
#   services.tasks / services.study_logs（実績の保存先）は、個人の学習記録・
#   SQLite実データを扱う別モジュールのため本リポジトリでは掲載していない。

from datetime import datetime
from database.db import get_connection
from services import tasks as task_service
from services import study_logs as log_service

TARGET_TYPES = ("task", "drill", "project_task")


def get_active_timer() -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM active_timer ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def elapsed_seconds(timer: dict) -> int:
    """保存されている状態から経過秒数を計算する（画面の再描画回数には依存しない）。"""
    total = timer.get("accumulated_seconds") or 0
    if timer.get("status") == "running" and timer.get("segment_started_at"):
        started = datetime.fromisoformat(timer["segment_started_at"])
        total += int((datetime.now() - started).total_seconds())
    return max(0, total)


def start_timer(target_type: str, target_id: int, label: str, category_label: str | None) -> dict:
    """新しいタイマーを開始する。既に実行中・一時停止中のタイマーがある場合は開始しない
    （「同時に1件のみ」を守るため）。
    """
    if target_type not in TARGET_TYPES:
        return {"ok": False, "message": "不正な対象種別です。"}
    if get_active_timer():
        return {"ok": False, "message": "既に実行中のタイマーがあります。先に終了（または破棄）してください。"}

    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT INTO active_timer (
                target_type, target_id, label, category_label, status,
                accumulated_seconds, segment_started_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', 0, ?, ?, ?)
            """,
            (target_type, target_id, label, category_label, now, now, now),
        )
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def pause_timer() -> None:
    """実行中のタイマーを一時停止する。経過秒数を確定し、区間の開始時刻をクリアする。"""
    timer = get_active_timer()
    if not timer or timer["status"] != "running":
        return
    seconds = elapsed_seconds(timer)
    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE active_timer SET status = 'paused', accumulated_seconds = ?, "
            "segment_started_at = NULL, updated_at = ? WHERE id = ?",
            (seconds, now, timer["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def resume_timer() -> None:
    """一時停止中のタイマーを再開する。新しい計測区間の開始時刻を記録する。"""
    timer = get_active_timer()
    if not timer or timer["status"] != "paused":
        return
    conn = get_connection()
    try:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE active_timer SET status = 'running', segment_started_at = ?, updated_at = ? WHERE id = ?",
            (now, now, timer["id"]),
        )
        conn.commit()
    finally:
        conn.close()


def discard_timer() -> None:
    """保存せずにタイマーを破棄する（実績には一切反映しない）。"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM active_timer")
        conn.commit()
    finally:
        conn.close()


def finish_and_save(minutes: int, note: str = "") -> dict:
    """確認・修正済みの分数で実績に保存し、タイマーを終了する。

    保存先は対象の種類に応じて既存の実績管理（study_logs / tasks.actual_minutes）を使い、
    既存の同日・同対象の実績があれば加算する（上書きしない）。
    """
    timer = get_active_timer()
    if not timer:
        return {"ok": False, "message": "進行中のタイマーがありません。"}
    if minutes <= 0:
        discard_timer()
        return {"ok": True, "message": "0分のため記録せずに終了しました。"}

    today_iso = datetime.now().date().isoformat()
    target_type = timer["target_type"]
    target_id = timer["target_id"]
    label = timer["label"]

    if target_type == "project_task":
        task = task_service.get_task(target_id)
        if task:
            new_actual = (task["actual_minutes"] or 0) + minutes
            task_service.update_task(target_id, actual_minutes=new_actual)
    else:
        log_service.accumulate_log_for_timer(
            log_date=today_iso,
            minutes=minutes,
            task_name=label,
            task_id=target_id if target_type == "task" else None,
            drill_id=target_id if target_type == "drill" else None,
            note=note or None,
        )

    discard_timer()
    return {"ok": True}
