#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
雲ノ平山荘 空室状況 監視スクリプト（GitHub Actions / LINE通知対応版）
============================================================

対象ページ: https://www.tenawan.ne.jp/lodgment/rec/002/076/pcr.asp

【設定方法】
・監視したい日付は、同じフォルダの config.yaml で指定します（最大3件）。
・LINEのアクセストークンやユーザーIDなどの秘密情報は、コードに直接書かず
  環境変数（ローカルでは .env ファイル、GitHub Actionsでは Secrets）から読み込みます。

【必要な環境変数 (.env または GitHub Secrets)】
  LINE_CHANNEL_ACCESS_TOKEN : LINE Developersで取得したチャネルアクセストークン
  LINE_USER_ID              : 通知を送るLINEのユーザーID

【ローカルでのテスト方法】
1. ルートフォルダの .env.example を .env にコピーして値を入力
2. python -m pip install -r ../requirements.txt
3. python watch_vacancy.py --test-line
4. python watch_vacancy.py --debug
"""

import os
import re
import sys
import time
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ========================= 基本設定（変更不要） =========================

TARGET_URL = "https://www.tenawan.ne.jp/lodgment/rec/002/076/pcr.asp"
TARGET_YEAR = 2026
AVAILABLE_SYMBOLS = ["○", "△"]
CHECK_INTERVAL_SEC = 3600

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
STATE_FILE = SCRIPT_DIR / "state.json"
LOG_FILE = SCRIPT_DIR / "watch_vacancy.log"

# .env（ローカル用）を読み込む。無ければ何もしない（GitHub Actionsでは実際のSecretsが使われる）
load_dotenv(SCRIPT_DIR.parent / ".env")

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")
LINE_API_URL = "https://api.line.me/v2/bot/message/push"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


_config = load_config()
# target_days: [{month: 8, day: 29}, ...] または [29, 30] のような単純リストにも対応
_raw_days = _config.get("target_days", [{"month": 8, "day": 29}])
TARGET_DATES: list[tuple[int, int]] = []
for item in _raw_days[:3]:  # 最大3件
    if isinstance(item, dict):
        TARGET_DATES.append((int(item.get("month", 8)), int(item["day"])))
    else:
        TARGET_DATES.append((8, int(item)))  # 単純な数字だけの場合は8月とみなす

# ========================= ここから下は基本的に変更不要 =========================

handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=handlers,
)
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


def _extract_month_section(html: str, month: int) -> str | None:
    headers_ = list(re.finditer(r'<th[^>]*class="month"[^>]*>\s*(\d{4})年.*?(\d{1,2})月\s*</th>', html))
    for idx, m in enumerate(headers_):
        if int(m.group(2)) == month:
            start = m.end()
            end = headers_[idx + 1].start() if idx + 1 < len(headers_) else len(html)
            return html[start:end]
    return None


def parse_status(html: str, month: int, day: int) -> str | None:
    section = _extract_month_section(html, month) or html
    for td_html in re.findall(r"<td[^>]*>(.*?)</td>", section, flags=re.DOTALL):
        day_match = re.search(
            r"<em>\s*(?:<div[^>]*>\s*(\d{1,2})\s*</div>|(\d{1,2}))\s*</em>", td_html, flags=re.DOTALL
        )
        if not day_match:
            continue
        day_num_str = day_match.group(1) or day_match.group(2)
        if not day_num_str or int(day_num_str) != day:
            continue
        status_match = re.search(r"<span>\s*([^<]*?)\s*</span>", td_html, flags=re.DOTALL)
        if status_match:
            status = status_match.group(1).strip()
            if status:
                return status
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


def _fmt(month: int, day: int) -> str:
    return f"{month}/{day}"


def _format_lines(available: list[tuple[int, int, str]]) -> str:
    return "\n".join(f"・{_fmt(m, d)}: {s}" for m, d, s in available)


def send_line_notification(text_message: str) -> None:
    """LINE Messaging API を使用してプッシュメッセージを送信"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        log.error("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が設定されていません。")
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [
            {
                "type": "text",
                "text": text_message,
            }
        ],
    }

    resp = requests.post(LINE_API_URL, headers=headers, json=payload, timeout=15)
    resp.raise_for_status()
    log.info("LINE通知を送信しました。")


def send_line_vacancy(available: list[tuple[int, int, str]]) -> None:
    date_names = "、".join(_fmt(m, d) for m, d, _ in available)
    message = (
        f"【空室通知】雲ノ平山荘に空きが出ました！（{date_names}）\n\n"
        f"{_format_lines(available)}\n\n"
        f"▼予約ページ:\n{TARGET_URL}\n\n"
        f"通知日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    send_line_notification(message)


def send_test_line() -> None:
    message = f"【テスト】雲ノ平山荘 空室監視スクリプト\n送信日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    send_line_notification(message)


def check_once(debug: bool = False) -> None:
    log.info("チェック開始: %s (対象日: %s)", TARGET_URL, ", ".join(_fmt(m, d) for m, d in TARGET_DATES))
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
    dates_state = state.setdefault("dates", {})
    newly_available: list[tuple[int, int, str]] = []

    for month, day in TARGET_DATES:
        status = parse_status(html, month, day)
        key = _fmt(month, day)

        if status is None:
            log.warning("%s の状態を特定できませんでした。", key)
            continue

        log.info("現在の状態: %s = %s", key, status)

        d_state = dates_state.setdefault(key, {})
        already_notified = d_state.get("notified_for_status") == status
        is_available = status in AVAILABLE_SYMBOLS

        if is_available:
            if already_notified:
                log.info("%s は空きが継続していますが、既に通知済みのため再送しません。", key)
            else:
                newly_available.append((month, day, status))
        else:
            d_state.pop("notified_for_status", None)

        d_state["last_status"] = status

    if newly_available:
        try:
            send_line_vacancy(newly_available)
            for month, day, status in newly_available:
                dates_state[_fmt(month, day)]["notified_for_status"] = status
        except Exception as e:
            log.error("LINE通知の送信に失敗しました: %s", e)

    state["last_checked"] = datetime.now().isoformat()
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="雲ノ平山荘 空室監視スクリプト")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test-line", action="store_true")
    args = parser.parse_args()

    if args.test_line:
        try:
            send_test_line()
        except Exception as e:
            log.error("LINEテスト通知の送信に失敗しました: %s", e)
        return

    if args.debug:
        check_once(debug=True)
        return

    if args.once:
        check_once()
        return

    log.info("監視を開始します。対象: %s", ", ".join(_fmt(m, d) for m, d in TARGET_DATES))
    while True:
        check_once()
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
