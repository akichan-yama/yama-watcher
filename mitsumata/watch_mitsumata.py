#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三俣山荘グループ 予約サイト 空室状況 監視スクリプト（GitHub Actions対応版）
================================================================================

対象ページ: https://mitsumatasanso.hutlify.com/lodges/mitsumata?calendar_month=2026-08-01&room=lodge

【設定方法】
・監視したい日付は、同じフォルダの config.yaml で指定します（最大3件）。
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

TARGET_URL = "https://mitsumatasanso.hutlify.com/lodges/mitsumata?calendar_month=2026-08-01&room=lodge"
FULL_STATUSES = {"満", "満室", "/", "休", "期間外", ""}
CHECK_INTERVAL_SEC = 3600

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = SCRIPT_DIR / "watch_mitsumata.log"

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
TARGET_DAYS = [int(d) for d in _config.get("target_days", [28])][:3]  # 最大3件


handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}


def fetch_page() -> str:
    resp = requests.get(TARGET_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding
    return resp.text


def parse_status(html: str, day: int) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[int, tuple[str, int]] = {}
    for tag in soup.find_all(True):
        text = tag.get_text(" ", strip=True)
        m = re.fullmatch(r"(\d{1,2})\s*(満室?|残\s?\d+|休|期間外|予約不要|/)?", text)
        if not m:
            continue
        day_num = int(m.group(1))
        if not (1 <= day_num <= 31):
            continue
        status = (m.group(2) or "").replace(" ", "")
        descendant_count = len(tag.find_all(True))
        if day_num not in candidates or descendant_count > candidates[day_num][1]:
            candidates[day_num] = (status, descendant_count)

    if len(candidates) < 15:
        return None
    if day in candidates:
        return candidates[day][0]
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


def _display(status: str) -> str:
    return status if status else "○（空室あり）"


def _format_lines(available: list[tuple[int, str]]) -> str:
    return "\n".join(f"・8/{d}: {_display(s)}" for d, s in available)


def send_email(available: list[tuple[int, str]]) -> None:
    summary = "、".join(f"8/{d}" for d, _ in available)
    subject = f"【空室通知】三俣山荘 に空きが出ました（{summary}）"
    body = (
        f"三俣山荘グループ予約サイトで、三俣山荘の以下の日程に空きが出ました。\n\n"
        f"{_format_lines(available)}\n\n"
        f"予約ページ:\n{TARGET_URL}\n\n"
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
    subject = "【テスト】三俣山荘 空室監視スクリプト"
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


def send_ntfy(available: list[tuple[int, str]]) -> None:
    if not NTFY_ENABLED or not NTFY_TOPIC:
        return
    summary = "、".join(f"8/{d}" for d, _ in available)
    payload = {
        "topic": NTFY_TOPIC,
        "title": f"三俣山荘に空きが出ました！（{summary}）",
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
        "title": "【テスト】三俣山荘 空室監視スクリプト",
        "message": f"テスト通知です。送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "priority": 5,
        "tags": ["white_check_mark"],
    }
    resp = requests.post(NTFY_SERVER, json=payload, timeout=15)
    resp.raise_for_status()
    log.info("ntfyテスト通知の送信に成功しました。")


def check_once(debug: bool = False) -> None:
    log.info("チェック開始: %s (対象日: %s)", TARGET_URL, TARGET_DAYS)
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
    days_state = state.setdefault("days", {})
    newly_available: list[tuple[int, str]] = []

    for day in TARGET_DAYS:
        status = parse_status(html, day)
        key = str(day)

        if status is None:
            log.warning("三俣山荘 / 8月%d日 の状態を特定できませんでした。", day)
            continue

        log.info("現在の状態: 三俣山荘 / 8/%d = %s", day, _display(status))

        d_state = days_state.setdefault(key, {})
        already_notified = d_state.get("notified_for_status") == status
        is_available = status not in FULL_STATUSES

        if is_available:
            if already_notified:
                log.info("8/%d は空きが継続していますが、既に通知済みのため再送しません。", day)
            else:
                newly_available.append((day, status))
        else:
            d_state.pop("notified_for_status", None)

        d_state["last_status"] = status

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
            for day, status in newly_available:
                days_state[str(day)]["notified_for_status"] = status

    state["last_checked"] = datetime.now().isoformat()
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="三俣山荘 空室監視スクリプト")
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

    log.info("監視を開始します。対象: 三俣山荘 / %s", TARGET_DAYS)
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
