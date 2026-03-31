# coding=utf-8

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config" / "ai_sources.json"
STATE_DIR = PROJECT_ROOT / "output" / ".ai_feed"
STATE_PATH = STATE_DIR / "state.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
)
TRANSLATOR = None
TRANSLATOR_READY = False


@dataclass
class Item:
    source: str
    category: str
    title: str
    url: str
    published: str = ""


class AnchorCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items: List[Dict[str, str]] = []
        self._href: Optional[str] = None
        self._chunks: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href.strip()
            self._chunks = []

    def handle_data(self, data):
        if self._href is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self._href is None:
            return
        title = re.sub(r"\s+", " ", "".join(self._chunks)).strip()
        self.items.append({"href": self._href, "title": title})
        self._href = None
        self._chunks = []


class HeadingCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items: List[str] = []
        self._tag: Optional[str] = None
        self._chunks: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3"}:
            self._tag = tag
            self._chunks = []

    def handle_data(self, data):
        if self._tag:
            self._chunks.append(data)

    def handle_endtag(self, tag):
        if self._tag == tag:
            title = re.sub(r"\s+", " ", "".join(self._chunks)).strip()
            if title:
                self.items.append(title)
            self._tag = None
            self._chunks = []


def ensure_state_dir() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> Dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> Dict:
    ensure_state_dir()
    if not STATE_PATH.exists():
        return {"sent_urls": [], "daily_seen": {}, "last_daily_date": ""}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    ensure_state_dir()
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_text(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,ja;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": url,
        },
    )
    with urlopen(req, timeout=20) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="ignore")


def clean_title(title: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()
    return title


def is_probably_english(text: str) -> bool:
    if not text:
        return False
    if re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", text):
        return False
    letters = re.findall(r"[A-Za-z]", text)
    if len(letters) < 6:
        return False
    return len(letters) / max(len(text), 1) > 0.25


def get_translator():
    global TRANSLATOR, TRANSLATOR_READY
    if TRANSLATOR_READY:
        return TRANSLATOR
    TRANSLATOR_READY = True

    if os.environ.get("AI_BILINGUAL_TRANSLATION", "").lower() not in {"1", "true", "yes"}:
        return None

    try:
        import argostranslate.package
        import argostranslate.translate
    except ImportError:
        print("AI feed: argostranslate not installed, skipping bilingual translation")
        return None

    try:
        installed_languages = argostranslate.translate.get_installed_languages()
        from_lang = next((lang for lang in installed_languages if lang.code == "en"), None)
        to_lang = next((lang for lang in installed_languages if lang.code == "zh"), None)

        if from_lang is None or to_lang is None or from_lang.get_translation(to_lang) is None:
            print("AI feed: installing Argos en->zh translation package")
            argostranslate.package.update_package_index()
            available_packages = argostranslate.package.get_available_packages()
            package_to_install = next(
                pkg for pkg in available_packages if pkg.from_code == "en" and pkg.to_code == "zh"
            )
            argostranslate.package.install_from_path(package_to_install.download())
            installed_languages = argostranslate.translate.get_installed_languages()
            from_lang = next((lang for lang in installed_languages if lang.code == "en"), None)
            to_lang = next((lang for lang in installed_languages if lang.code == "zh"), None)

        if from_lang is None or to_lang is None:
            print("AI feed: Argos en/zh languages unavailable after install")
            return None

        TRANSLATOR = from_lang.get_translation(to_lang)
        return TRANSLATOR
    except Exception as exc:
        print(f"AI feed: bilingual translation init failed: {exc}")
        return None


def maybe_translate_bilingual(title: str) -> Optional[str]:
    if not is_probably_english(title):
        return None
    translator = get_translator()
    if translator is None:
        return None
    try:
        translated = clean_title(translator.translate(title))
        if not translated or translated == title:
            return None
        return translated
    except Exception as exc:
        print(f"AI feed: translation failed for title: {exc}")
        return None


def keyword_match(text: str, keywords: List[str]) -> bool:
    haystack = (text or "").lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def fetch_rss_items(source: Dict, keywords: List[str]) -> List[Item]:
    xml_text = fetch_text(source["url"])
    root = ET.fromstring(xml_text)
    items: List[Item] = []
    for node in root.findall(".//item"):
        title = clean_title(node.findtext("title", ""))
        link = clean_title(node.findtext("link", ""))
        published = clean_title(node.findtext("pubDate", ""))
        if not title or not link:
            continue
        if not keyword_match(title, keywords):
            continue
        items.append(
            Item(
                source=source["name"],
                category=source.get("category", "未分类"),
                title=title,
                url=link,
                published=published,
            )
        )
        if len(items) >= source.get("max_items", 10):
            break
    return items


def html_link_allowed(href: str, source: Dict) -> bool:
    if not href:
        return False
    if href.startswith("mailto:") or href.startswith("javascript:"):
        return False
    for token in source.get("deny_substrings", []):
        if token and token in href:
            return False
    allow_prefixes = source.get("allow_prefixes", [])
    if href.startswith("http://") or href.startswith("https://"):
        if source.get("base_url") and not href.startswith(source["base_url"]):
            return False
        path = href.replace(source.get("base_url", ""), "", 1)
    else:
        path = href
    return any(path.startswith(prefix) for prefix in allow_prefixes)


def fetch_html_items(source: Dict, keywords: List[str]) -> List[Item]:
    html = fetch_text(source["url"])
    parser = AnchorCollector()
    parser.feed(html)
    seen = set()
    items: List[Item] = []
    for raw in parser.items:
        href = raw["href"]
        title = clean_title(raw["title"])
        if not title or len(title) < 8:
            continue
        if not html_link_allowed(href, source):
            continue
        if not keyword_match(title, keywords):
            continue
        full_url = href if href.startswith("http") else urljoin(source["base_url"], href)
        key = (title, full_url)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            Item(
                source=source["name"],
                category=source.get("category", "未分类"),
                title=title,
                url=full_url,
            )
        )
        if len(items) >= source.get("max_items", 10):
            break
    return items


def fetch_article_heading_items(source: Dict, keywords: List[str]) -> List[Item]:
    html = fetch_text(source["url"])
    parser = HeadingCollector()
    parser.feed(html)
    items: List[Item] = []
    seen = set()
    for title in parser.items:
        title = clean_title(title)
        if len(title) < 8 or title in seen:
            continue
        if not keyword_match(title, keywords):
            continue
        seen.add(title)
        items.append(
            Item(
                source=source["name"],
                category=source.get("category", "未分类"),
                title=title,
                url=source["url"],
            )
        )
        if len(items) >= source.get("max_items", 10):
            break
    return items


def fetch_items(config: Dict) -> List[Item]:
    all_items: List[Item] = []
    for source in config["sources"]:
        try:
            if source["type"] == "rss":
                items = fetch_rss_items(source, config["keywords"])
            elif source["type"] == "article_headings":
                items = fetch_article_heading_items(source, config["keywords"])
            else:
                items = fetch_html_items(source, config["keywords"])
            print(f"{source['name']}: fetched {len(items)} matching items")
            all_items.extend(items)
        except (URLError, HTTPError, ET.ParseError, TimeoutError, ValueError) as exc:
            print(f"{source['name']}: fetch failed: {exc}")
    deduped: List[Item] = []
    seen_urls = set()
    for item in all_items:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        deduped.append(item)
    return deduped


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

    for chunk in split_message(text):
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": False,
            }
        ).encode("utf-8")
        req = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        with urlopen(req, timeout=20) as resp:
            json.loads(resp.read().decode("utf-8", errors="ignore"))


def format_realtime(items: List[Item]) -> str:
    grouped: Dict[str, List[Item]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    lines = [f"AI专线新增 {len(items)} 条 ({now_local()})", ""]
    idx = 1
    for category in ["研究", "产品发布", "公司动态", "媒体补充", "未分类"]:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        lines.append(f"{category}: {len(category_items)} 条")
        for item in category_items[:8]:
            lines.append(f"{idx}. [{item.source}] {item.title}")
            translation = maybe_translate_bilingual(item.title)
            if translation:
                lines.append(f"   中文: {translation}")
            lines.append(item.url)
            if item.published:
                lines.append(f"时间: {item.published}")
            lines.append("")
            idx += 1
    return "\n".join(lines).strip()


def format_daily(items: List[Item]) -> str:
    grouped: Dict[str, List[Item]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    lines = [f"AI专线日报 {datetime.now().strftime('%Y-%m-%d')}", ""]
    for category in ["研究", "产品发布", "公司动态", "媒体补充", "未分类"]:
        category_items = grouped.get(category, [])
        if not category_items:
            continue
        lines.append(f"{category}: {len(category_items)} 条")
        for item in category_items[:6]:
            lines.append(f"- [{item.source}] {item.title}")
            translation = maybe_translate_bilingual(item.title)
            if translation:
                lines.append(f"  中文: {translation}")
            lines.append(f"  {item.url}")
        lines.append("")
    return "\n".join(lines).strip()


def filter_by_feed(items: List[Item], feed: str) -> List[Item]:
    if feed == "research":
        allowed = {"研究"}
    elif feed == "company":
        allowed = {"产品发布", "公司动态"}
    else:
        allowed = {"研究", "产品发布", "公司动态", "媒体补充", "未分类"}
    return [item for item in items if item.category in allowed]


def _state_key(feed: str, suffix: str) -> str:
    return f"{feed}_{suffix}"


def run_realtime(feed: str) -> int:
    config = load_config()
    state = load_state()
    items = filter_by_feed(fetch_items(config), feed)
    sent_urls = set(state.get(_state_key(feed, "sent_urls"), []))
    new_items = [item for item in items if item.url not in sent_urls]
    if not new_items:
        print("AI feed: no new items")
        return 0

    message = format_realtime(new_items)
    send_telegram(message)
    state[_state_key(feed, "sent_urls")] = (
        state.get(_state_key(feed, "sent_urls"), []) + [item.url for item in new_items]
    )[-500:]
    today = datetime.now().strftime("%Y-%m-%d")
    daily_seen = state.get(_state_key(feed, "daily_seen"), {})
    today_items = daily_seen.get(today, [])
    today_items.extend(
        {
            "source": item.source,
            "category": item.category,
            "title": item.title,
            "url": item.url,
            "published": item.published,
        }
        for item in new_items
    )
    daily_seen[today] = today_items[-200:]
    state[_state_key(feed, "daily_seen")] = {today: daily_seen[today]}
    save_state(state)
    print(f"AI feed: sent {len(new_items)} new items")
    return 0


def run_daily(feed: str) -> int:
    state = load_state()
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get(_state_key(feed, "last_daily_date")) == today:
        print("AI feed: daily digest already sent today")
        return 0
    items = [Item(**item) for item in state.get(_state_key(feed, "daily_seen"), {}).get(today, [])]
    if not items:
        print("AI feed: no daily items to summarize")
        return 0
    send_telegram(format_daily(items))
    state[_state_key(feed, "last_daily_date")] = today
    save_state(state)
    print(f"AI feed: sent daily digest with {len(items)} items")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AI-focused Telegram feed")
    parser.add_argument("--mode", choices=["realtime", "daily"], default="realtime")
    parser.add_argument("--feed", choices=["all", "research", "company"], default="all")
    args = parser.parse_args()

    if args.mode == "daily":
        return run_daily(args.feed)
    return run_realtime(args.feed)


if __name__ == "__main__":
    sys.exit(main())
