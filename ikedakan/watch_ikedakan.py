#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
富士山富士宮口八合目 池田館 空室状況 監視スクリプト（GitHub Actions対応版）
================================================================================

対象サイト: https://www.yamatan.net/hut/ikedakan/plan
このサイトはJavaScriptで描画されるSPAのため、裏側のtRPC APIから
直接JSONを取得して空室状況を計算します。

【空室判定の考え方】
・大部屋（相部屋）: 定員100名からその日に宿泊する予約人数の合計を引いた「残り人数」で判定
・2人部屋/3人部屋（個室）: 総部屋数からその日に重なる予約件数を引いた「残り部屋数」で判定
・上記いずれも、サイト側の「日付ごとの定員調整（adjustments/roomAdjustments）」を反映
・この計算はサイトの内部ロジックを推測で再現したものです。実際の表示と
  ズレがある可能性があるため、初回は必ず --debug で計算結果を確認してください。

【設定方法】
・監視したい日付・部屋タイプは、同じフォルダの config.yaml で指定します（日付は最大3件）。
・秘密情報は環境変数（ローカルは .env、GitHub ActionsではSecrets）から読み込みます。
"""

import os
import sys
import time
import json
import smtplib
import logging
import argparse
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

HUT_ID = "ikedakan"
API_BASE = "https://www.yamatan.net/api/trpc"
CHECK_INTERVAL_SEC = 3600

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = SCRIPT_DIR / "watch_ikedakan.log"

load_dotenv(SCRIPT_DIR.parent / ".env")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
TO_ADDRESS = os.environ.get("TO_ADDRESS", "")
NTFY_ENABLED = os.environ.get("NTFY_ENABLED", "true").lower() == "true"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# LINE Messaging API 設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_config = load_config()
_raw_days = _config.get("target_days", [{"month": 8, "day": 16}])
TARGET_DATES: list[tuple[int, int]] = []
for item in _raw_days[:3]:
    if isinstance(item, dict):
        TARGET_DATES.append((int(item.get("month", 8)), int(item["day"])))
TARGET_YEAR = int(_config.get("target_year", 2026))

ROOM_LABELS = {
    "大部屋": "大部屋",
    "2人部屋": "2人部屋",
    "3人部屋": "3人部屋",
}
TARGET_ROOMS = _config.get("target_rooms", ["大部屋", "2人部屋", "3人部屋"])

handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def _trpc_get(procedure: str, input_obj: dict) -> dict:
    """単一プロシージャのtRPCバッチ形式でGETリクエストする"""
    import urllib.parse
    payload = {"0": {"json": input_obj}}
    url = f"{API_BASE}/{procedure}?batch=1&input=" + urllib.parse.quote(json.dumps(payload))
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data[0]["result"]["data"]["json"]


def fetch_room_info() -> dict:
    """部屋の定義（定員・総部屋数・定員調整）を取得"""
    return _trpc_get("hut.getWithRelation", {"hutId": HUT_ID})


def fetch_month_reservations(year: int, month: str) -> list:
    """指定月周辺の予約一覧を取得（前後の週も含まれる）"""
    data = _trpc_get("hutEvent.getEvent", {"hutId": HUT_ID, "year": str(year), "month": f"{int(month):02d}"})
    return data.get("reservations", [])


def _parse_date(s: str) -> date:
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def _in_range(target: date, start_s: str, end_s: str) -> bool:
    return _parse_date(start_s) <= target <= _parse_date(end_s)


def _covers_night(target: date, start_s: str, end_s: str) -> bool:
    """宿泊予約が対象日の夜を含むか（チェックアウト日は含まない）"""
    return _parse_date(start_s) <= target < _parse_date(end_s)


def compute_availability(room_info: dict, reservations: list, target: date) -> dict:
    """各部屋タイプの空室数（人数 or 部屋数）を計算して返す"""
    result = {}
    rooms = room_info.get("rooms", [])
    adjustments = room_info.get("adjustments", [])
    room_adjustments = room_info.get("roomAdjustments", [])

    for room in rooms:
        name = room.get("name")
        if name not in TARGET_ROOMS:
            continue
        room_id = room["id"]
        is_private_room_type = bool(room.get("private_rooms"))

        # 予約禁止日チェック（DateRsvAvailabilityControlToRoom）
        prohibited = False
        for ctrl in room.get("DateRsvAvailabilityControlToRoom", []):
            c = ctrl.get("DateRsvAvailabilityControl", {})
            if c.get("prohibitNewRsvForUser") and c.get("date", "")[:10] == target.isoformat():
                prohibited = True
                break

        if is_private_room_type:
            base_total = room.get("total", 0)
            adj_sum = sum(
                a.get("adjustment_num", 0)
                for a in room_adjustments
                if a.get("room_id") == room_id and _in_range(target, a["start_date"], a["end_date"])
            )
            occupied = sum(
                1
                for r in reservations
                if r.get("room_id") == room_id and _covers_night(target, r["start_date"], r["end_date"])
            )
            available = base_total + adj_sum - occupied
        else:
            base_capacity = room.get("capacity", 0)
            adj_sum = sum(
                a.get("adjustment_num", 0)
                for a in adjustments
                if a.get("room_id") == room_id and _in_range(target, a["start_date"], a["end_date"])
            )
            occupied = sum(
                r.get("total_guest_num", 0)
                for r in reservations
                if r.get("room_id") == room_id and _covers_night(target, r["start_date"], r["end_date"])
            )
            available = base_capacity + adj_sum - occupied

        if prohibited:
            available = 0

        result[name] = available

    return result


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _fmt(month: int, day: int) -> str:
    return f"{TARGET_YEAR}/{month}/{day}"


def _format_lines(available: list[tuple[int, int, str, int]]) -> str:
    return "\n".join(f"・{_fmt(m, d)} {room}: 残り{n}" for m, d, room, n in available)


def send_email(available: list[tuple[int, int, str, int]]) -> None:
    summary = "、".join(f"{_fmt(m, d)}({room})" for m, d, room, _ in available)
    subject = f"【空室通知】池田館 に空きが出ました（{summary}）"
    body = (
        f"富士山 池田館 の以下に空きが出ました。\n\n"
        f"{_format_lines(available)}\n\n"
        f"予約ページ:\nhttps://www.yamatan.net/hut/ikedakan/plan\n\n"
        f"※この空室判定は自動計算のため、必ずページで実際の空室状況をご確認ください。\n\n"
        f"通知日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_ADDRESS
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [TO_ADDRESS], msg.as_string())
    log.info("通知メールを送信しました -> %s", TO_ADDRESS)


def send_test_email() -> None:
    subject = "【テスト】池田館 空室監視スクリプト"
    body = f"テストメールです。送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = TO_ADDRESS
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
        server.starttls()
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [TO_ADDRESS], msg.as_string())
    log.info("テストメール送信に成功しました -> %s", TO_ADDRESS)


def send_ntfy(available: list[tuple[int, int, str, int]]) -> None:
    if not NTFY_ENABLED or not NTFY_TOPIC:
        return
    summary = "、".join(f"{_fmt(m, d)}({room})" for m, d, room, _ in available)
    payload = {
        "topic": NTFY_TOPIC,
        "title": f"池田館に空きが出ました！（{summary}）",
        "message": f"{_format_lines(available)}\n\nhttps://www.yamatan.net/hut/ikedakan/plan",
        "priority": 5,
        "tags": ["mountain", "bellhop_bell"],
    }
    resp = requests.post(NTFY_SERVER, json=payload, timeout=15)
    resp.raise_for_status()
    log.info("ntfy通知を送信しました -> topic: %s", NTFY_TOPIC)


def send_test_ntfy() -> None:
    if not NTFY_TOPIC:
        log.error("NTFY_TOPICが設定されていません。")
        return
    payload = {
        "topic": NTFY_TOPIC,
        "title": "【テスト】池田館 空室監視スクリプト",
        "message": f"テスト通知です。送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "priority": 5,
        "tags": ["white_check_mark"],
    }
    resp = requests.post(NTFY_SERVER, json=payload, timeout=15)
    resp.raise_for_status()
    log.info("ntfyテスト通知の送信に成功しました。")


def send_line(available: list[tuple[int, int, str, int]]) -> None:
    """LINE Messaging API経由で空室通知を送信"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        log.warning("LINEの設定情報（TOKEN / USER_ID）が見つからないためスキップします。")
        return

    summary = "、".join(f"{_fmt(m, d)}({room})" for m, d, room, _ in available)
    message_text = (
        f"【空室通知】池田館に空きが出ました！\n"
        f"対象: {summary}\n\n"
        f"{_format_lines(available)}\n\n"
        f"▼ 予約ページ\nhttps://www.yamatan.net/hut/ikedakan/plan"
    )

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message_text}],
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    log.info("LINE通知を送信しました。")


def send_test_line() -> None:
    """LINE送信用テスト"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        log.error("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定です。")
        return

    message_text = f"【テスト】池田館 空室監視スクリプト\n送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message_text}],
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    log.info("LINEテスト通知の送信に成功しました。")


def check_once(debug: bool = False) -> None:
    log.info("チェック開始 (対象: %s)", ", ".join(_fmt(m, d) for m, d in TARGET_DATES))
    try:
        room_info = fetch_room_info()
        months_needed = sorted({m for m, d in TARGET_DATES})
        reservations = []
        seen_ids = set()
        for m in months_needed:
            for r in fetch_month_reservations(TARGET_YEAR, str(m)):
                if r["id"] not in seen_ids:
                    reservations.append(r)
                    seen_ids.add(r["id"])
    except Exception as e:
        log.error("データ取得に失敗しました: %s", e)
        return

    if debug:
        debug_path = SCRIPT_DIR / "debug_data.json"
        debug_path.write_text(
            json.dumps({"room_info": room_info, "reservations": reservations}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("デバッグ用にデータを保存しました -> %s", debug_path)

    state = load_state()
    cells_state = state.setdefault("cells", {})
    newly_available: list[tuple[int, int, str, int]] = []

    for month, day in TARGET_DATES:
        target = date(TARGET_YEAR, month, day)
        availability = compute_availability(room_info, reservations, target)

        for room_name, avail_num in availability.items():
            key = f"{month}-{day}_{room_name}"
            log.info("現在の状態: %s %s = 残り%d", _fmt(month, day), room_name, avail_num)

            c_state = cells_state.setdefault(key, {})
            already_notified = c_state.get("notified_for_avail") == avail_num
            is_available = avail_num > 0

            if is_available:
                if already_notified:
                    log.info("「%s」は空きが継続していますが、既に通知済みのため再送しません。", key)
                else:
                    newly_available.append((month, day, room_name, avail_num))
            else:
                c_state.pop("notified_for_avail", None)

            c_state["last_avail"] = avail_num

    if newly_available:
        notified_ok = False
        try:
            send_email(newly_available)
            notified_ok = True
        except Exception as e:
            log.error("メール送信に失敗しました: %s", e)
        try:
            send_ntfy(newly_available)
            notified_ok = True
        except Exception as e:
            log.error("ntfy通知の送信に失敗しました: %s", e)
        try:
            send_line(newly_available)
            notified_ok = True
        except Exception as e:
            log.error("LINE通知の送信に失敗しました: %s", e)

        if notified_ok:
            for month, day, room_name, avail_num in newly_available:
                cells_state[f"{month}-{day}_{room_name}"]["notified_for_avail"] = avail_num

    state["last_checked"] = datetime.now().isoformat()
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="池田館 空室監視スクリプト")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--test-ntfy", action="store_true")
    parser.add_argument("--test-line", action="store_true")
    args = parser.parse_args()

    if args.test_line:
        try:
            send_test_line()
        except Exception as e:
            log.error("LINEテスト通知の送信に失敗しました: %s", e)
        return

    if args.test_ntfy:
        try:
            send_test_ntfy()
        except Exception as e:
            log.error("ntfyテスト通知の送信に失敗しました: %s", e)
        return

    if args.test_email:
        try:
            send_test_email()
        except Exception as e:
            log.error("テストメール送信に失敗しました: %s", e)
        return

    if args.debug:
        check_once(debug=True)
        return

    if args.once:
        check_once()
        return

    log.info("監視を開始します。対象: %s", [_fmt(m, d) for m, d in TARGET_DATES])
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
