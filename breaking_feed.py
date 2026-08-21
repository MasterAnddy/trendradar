# coding=utf-8

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "breaking_rules.json"
STATE_DIR = PROJECT_ROOT / "output" / ".breaking_feed"
STATE_PATH = STATE_DIR / "state.json"
TIMEZONE = ZoneInfo(os.environ.get("BREAKING_FEED_TIMEZONE", "Asia/Tokyo"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)


@dataclass
class AlertItem:
    source_id: str
    source_name: str
    title: str
    url: str
    rank: int
    score: int
    matched_keywords: List[str]


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def now_local_text() -> str:
    return now_local().strftime("%Y-%m-%d %H:%M:%S")


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> Dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> Dict:
    ensure_state_dir()
    if not STATE_PATH.exists():
        return {"bootstrapped": False, "sent_keys": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: Dict) -> None:
    ensure_state_dir()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip()


def normalize_key(text: str) -> str:
    text = clean_title(text).lower()
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def rank_bonus(rank: int) -> int:
    if rank == 1:
        return 20
    if rank <= 3:
        return 15
    if rank <= 5:
        return 10
    if rank <= 10:
        return 5
    return 0


def fetch_source(source: Dict) -> List[Dict]:
    response = requests.get(
        f"https://newsnow.busiyi.world/api/s?id={source['id']}&latest",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
            "Cache-Control": "no-cache",
        },
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    status = payload.get("status", "")
    if status not in {"success", "cache"}:
        raise ValueError(f"{source['id']} returned unexpected status: {status}")
    return payload.get("items", [])


def alert_key(item: AlertItem) -> str:
    return f"{item.source_id}:{normalize_key(item.title)}"


def score_item(source: Dict, title: str, rank: int, config: Dict) -> Optional[AlertItem]:
    title = clean_title(title)
    lowered = title.lower()

    if not title:
        return None
    if any(keyword.lower() in lowered for keyword in config.get("exclude_keywords", [])):
        return None

    matched_keywords: List[str] = []
    keyword_score = 0
    for keyword, weight in config.get("keyword_weights", {}).items():
        if keyword.lower() in lowered:
            matched_keywords.append(keyword)
            keyword_score += int(weight)

    if not matched_keywords:
        return None

    keyword_score = min(keyword_score, 60)
    total_score = int(source.get("source_weight", 0)) + rank_bonus(rank) + keyword_score
    if total_score < int(config.get("threshold", 60)):
        return None

    return AlertItem(
        source_id=source["id"],
        source_name=source["name"],
        title=title,
        url="",
        rank=rank,
        score=total_score,
        matched_keywords=matched_keywords[:4],
    )


def collect_alerts(config: Dict) -> List[AlertItem]:
    best_by_key: Dict[str, AlertItem] = {}

    for source in config.get("sources", []):
        try:
            items = fetch_source(source)
        except Exception as exc:
            print(f"Breaking feed: {source['id']} fetch failed: {exc}")
            continue

        for rank, raw in enumerate(items, start=1):
            alert = score_item(source, raw.get("title", ""), rank, config)
            if alert is None:
                continue

            alert.url = raw.get("mobileUrl") or raw.get("url") or ""
            key = normalize_key(alert.title)
            existing = best_by_key.get(key)
            if existing is None or (alert.score, -alert.rank) > (existing.score, -existing.rank):
                best_by_key[key] = alert

    alerts = sorted(best_by_key.values(), key=lambda item: (-item.score, item.rank, item.source_name))
    return alerts[: int(config.get("top_n", 5))]


def split_message(text: str, limit: int = 3800) -> List[str]:
    lines = text.splitlines()
    chunks: List[str] = []
    current = ""
    for line in lines:
        candidate = line if not current else current + "\n" + line
        if len(candidate.encode("utf-8")) > limit:
            if current:
                chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        response = requests.post(
            url,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": False,
                }
            ).encode("utf-8"),
            timeout=20,
        )
        response.raise_for_status()


def format_alerts(alerts: List[AlertItem], forced: bool) -> str:
    header = "特大事件测试快报" if forced else "特大事件快报"
    lines = [f"{header} {len(alerts)} 条 ({now_local_text()})", "仅推高阈值的大事，不推普通热搜。", ""]
    for index, item in enumerate(alerts, start=1):
        lines.append(f"{index}. [{item.source_name}] {item.title}")
        lines.append(f"   评分：{item.score} | 排名：#{item.rank} | 命中：{', '.join(item.matched_keywords)}")
        if item.url:
            lines.append(f"   {item.url}")
        lines.append("")
    return "\n".join(lines).strip()


def run(force_send: bool = False, dry_run: bool = False) -> int:
    config = load_config()
    state = load_state()
    alerts = collect_alerts(config)
    if not alerts:
        if not state.get("bootstrapped"):
            state["bootstrapped"] = True
            state["last_checked_at"] = now_local_text()
            save_state(state)
        print("Breaking feed: no major alerts matched")
        return 0

    sent_keys = set(state.get("sent_keys", []))
    alert_candidates = [item for item in alerts if alert_key(item) not in sent_keys]

    if not state.get("bootstrapped") and not force_send and not config.get("bootstrap_send", False):
        state["bootstrapped"] = True
        state["sent_keys"] = (state.get("sent_keys", []) + [alert_key(item) for item in alerts])[-1000:]
        save_state(state)
        print("Breaking feed: bootstrapped state without sending")
        return 0

    if force_send and not alert_candidates:
        alert_candidates = alerts

    if not alert_candidates:
        print("Breaking feed: no new major alerts")
        return 0

    message = format_alerts(alert_candidates, forced=force_send)
    if dry_run:
        print(message)
    else:
        send_telegram(message)

    state["bootstrapped"] = True
    state["sent_keys"] = (state.get("sent_keys", []) + [alert_key(item) for item in alert_candidates])[-1000:]
    state["last_checked_at"] = now_local_text()
    state["last_sent_at"] = now_local_text()
    state["last_alerts"] = [asdict(item) for item in alert_candidates]
    save_state(state)
    print(f"Breaking feed: sent {len(alert_candidates)} alert(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Major-event breaking news feed")
    parser.add_argument("--force-send", action="store_true", help="Send current alerts even on first run")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending Telegram messages")
    args = parser.parse_args()
    return run(force_send=args.force_send, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
