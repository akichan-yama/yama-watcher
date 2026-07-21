#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双六小屋グループ 予約サイト 空室状況 監視スクリプト（GitHub Actions対応版）
================================================================================

対象ページ: https://www.sugorokugoya.com/reservation/selectdate?month=2026-08&type=1

【設定方法】
・監視したい日付・小屋・部屋タイプは、同じフォルダの config.yaml で指定します（日付は最大3件）。
・秘密情報は環境変数（ローカルは .env、GitHub ActionsではSecrets）から読み込みます。
"""

import os
import re
import sys
import time
import json
import smtplib
import logging
import argparse
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

TARGET_URL = "https://www.sugorokugoya.com/reservation/selectdate?month=2026-08&type=1"
FULL_STATUSES = {"満", "満室", "/", "期間外", "", "-"}
CHECK_INTERVAL_SEC = 3600

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = SCRIPT_DIR / "watch_futamata.log"

load_dotenv(SCRIPT_DIR.parent / ".env")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
TO_ADDRESS = os.environ.get("TO_ADDRESS", "")
NTFY_ENABLED = os.environ.get("NTFY_ENABLED", "true").lower() == "true"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = "https://ntfy.sh"
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_config = load_config()
TARGET_HUT = _config.get("target_hut", "双六小屋")
TARGET_ROOMS = _config.get("target_rooms", ["一般室"])
TARGET_DAYS = [int(d) for d in _config.get("target_days", [26])][:3]  # 最大3件


handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def fetch_page() -> str:
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def _build_grid(table) -> list[list[str]]:
    grid: list[list[str]] = []
    rowspan_tracker: dict[int, list] = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        row: list[str] = []
        col = 0
        cell_iter = iter(cells)
        current = next(cell_iter, None)
        while current is not None or col in rowspan_tracker:
            if col in rowspan_tracker and rowspan_tracker[col][0] > 0:
                row.append(rowspan_tracker[col][1])
                rowspan_tracker[col][0] -= 1
                if rowspan_tracker[col][0] == 0:
                    del rowspan_tracker[col]
                col += 1
                continue
            if current is None:
                break
            text = current.get_text(strip=True)
            colspan = int(current.get("colspan", 1) or 1)
            rowspan = int(current.get("rowspan", 1) or 1)
            for i in range(colspan):
                row.append(text)
                if rowspan > 1:
                    rowspan_tracker[col + i] = [rowspan - 1, text]
                col += 1
            current = next(cell_iter, None)
        grid.append(row)
    return grid


def parse_status(html: str, hut: str, room: str, day: int) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        if hut not in table.get_text():
            continue
        grid = _build_grid(table)
        if not grid:
            continue

        header_row_idx = None
        ordered_days: list[int] = []
        for r_idx, row in enumerate(grid):
            days_in_order = []
            for cell in row:
                m = re.match(r"^(\d{1,2})\D*$", cell)
                if m and 1 <= int(m.group(1)) <= 31:
                    days_in_order.append(int(m.group(1)))
            if len(days_in_order) >= 15 and day in days_in_order:
                header_row_idx = r_idx
                ordered_days = days_in_order
                break
        if header_row_idx is None or not ordered_days:
            continue

        day_pos = ordered_days.index(day)
        num_days = len(ordered_days)

        for row in grid[header_row_idx + 1:]:
            row_text = " ".join(row[:3])
            if hut in row_text and room in row_text:
                status_cells = row[-num_days:]
                if day_pos < len(status_cells):
                    return status_cells[day_pos].strip()
    return None


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_lines(available: list[tuple[int, str, str]]) -> str:
    return "\n".join(f"・8/{d} {room}: {status}" for d, room, status in available)


def send_email(available: list[tuple[int, str, str]]) -> None:
    summary = "、".join(f"8/{d}({room})" for d, room, _ in available)
    subject = f"【空室通知】{TARGET_HUT} に空きが出ました（{summary}）"
    body = (
        f"双六小屋グループ予約サイトで、{TARGET_HUT} の以下に空きが出ました。\n\n"
        f"{_format_lines(available)}\n\n"
        f"予約ページ:\n{TARGET_URL}\n\n"
        f"※「後日開始」等、まだ正式に予約できない表示の場合もあります。必ずページで内容をご確認ください。\n\n"
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
    subject = f"【テスト】{TARGET_HUT} 空室監視スクリプト"
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


def send_ntfy(available: list[tuple[int, str, str]]) -> None:
    if not NTFY_ENABLED or not NTFY_TOPIC:
        return
    summary = "、".join(f"8/{d}({room})" for d, room, _ in available)
    payload = {
        "topic": NTFY_TOPIC,
        "title": f"{TARGET_HUT} に空きが出ました！（{summary}）",
        "message": f"{_format_lines(available)}\n\n{TARGET_URL}",
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
        "title": f"【テスト】{TARGET_HUT} 空室監視スクリプト",
        "message": f"テスト通知です。送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "priority": 5,
        "tags": ["white_check_mark"],
    }
    resp = requests.post(NTFY_SERVER, json=payload, timeout=15)
    resp.raise_for_status()
    log.info("ntfyテスト通知の送信に成功しました。")


def check_once(debug: bool = False) -> None:
    log.info("チェック開始: %s (対象日: %s / 部屋: %s)", TARGET_URL, TARGET_DAYS, TARGET_ROOMS)
    try:
        html = fetch_page()
    except Exception as e:
        log.error("ページ取得に失敗しました: %s", e)
        return

    if debug:
        debug_path = SCRIPT_DIR / "debug_page.html"
        debug_path.write_text(html, encoding="utf-8", errors="ignore")
        log.info("デバッグ用にHTMLを保存しました -> %s", debug_path)

    state = load_state()
    cells_state = state.setdefault("cells", {})
    newly_available: list[tuple[int, str, str]] = []

    for day in TARGET_DAYS:
        for room in TARGET_ROOMS:
            status = parse_status(html, TARGET_HUT, room, day)
            key = f"{day}_{room}"

            if status is None:
                log.warning("%s / %s / 8月%d日 の状態を特定できませんでした。", TARGET_HUT, room, day)
                continue

            log.info("現在の状態: %s / %s / 8/%d = %s", TARGET_HUT, room, day, status)

            c_state = cells_state.setdefault(key, {})
            already_notified = c_state.get("notified_for_status") == status
            is_available = status not in FULL_STATUSES

            if is_available:
                if already_notified:
                    log.info("「%s」は空きが継続していますが、既に通知済みのため再送しません。", key)
                else:
                    newly_available.append((day, room, status))
            else:
                c_state.pop("notified_for_status", None)

            c_state["last_status"] = status

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
        if notified_ok:
            for day, room, status in newly_available:
                cells_state[f"{day}_{room}"]["notified_for_status"] = status

    state["last_checked"] = datetime.now().isoformat()
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="双六小屋 空室監視スクリプト")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-email", action="store_true")
    parser.add_argument("--test-ntfy", action="store_true")
    args = parser.parse_args()

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

    log.info("監視を開始します。対象: %s / %s / %s", TARGET_HUT, TARGET_ROOMS, TARGET_DAYS)
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
