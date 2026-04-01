from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent
BASE_CONFIG_PATH = ROOT / "config" / "config.yaml"
PROFILE_DIR = ROOT / "config" / "profiles"
KEYWORD_FILE_PATH = ROOT / "config" / "keywords" / "frequency_words.user.txt"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {path}")
    return data


def normalize_sources(profile_data: dict[str, Any]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for item in profile_data.get("platforms", []):
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("id", "")).strip()
        if not source_id:
            continue
        source_name = str(item.get("name") or source_id).strip()
        sources.append({"id": source_id, "name": source_name})

    if not sources:
        raise ValueError("No platform sources were found in the legacy profile")

    return sources


def build_runtime_config(mode: str, timezone: str) -> dict[str, Any]:
    base_config = load_yaml(BASE_CONFIG_PATH)
    legacy_profile = load_yaml(PROFILE_DIR / f"{mode}.telegram.yaml")

    app = base_config.setdefault("app", {})
    app["timezone"] = timezone

    schedule = base_config.setdefault("schedule", {})
    schedule["enabled"] = False

    report = base_config.setdefault("report", {})
    report["mode"] = mode
    report["display_mode"] = "keyword"
    report["rank_threshold"] = legacy_profile.get("report", {}).get(
        "rank_threshold",
        report.get("rank_threshold", 5),
    )

    filter_config = base_config.setdefault("filter", {})
    filter_config["method"] = "keyword"
    filter_config["priority_sort_enabled"] = False

    base_config["platforms"] = {
        "enabled": True,
        "sources": normalize_sources(legacy_profile),
    }

    rss = base_config.setdefault("rss", {})
    rss["enabled"] = False

    display = base_config.setdefault("display", {})
    regions = display.setdefault("regions", {})
    regions["rss"] = False
    regions["standalone"] = False
    regions["ai_analysis"] = False

    ai_analysis = base_config.setdefault("ai_analysis", {})
    ai_analysis["enabled"] = False

    ai_translation = base_config.setdefault("ai_translation", {})
    ai_translation["enabled"] = False

    notification = base_config.setdefault("notification", {})
    notification["enabled"] = True

    storage = base_config.setdefault("storage", {})
    storage["backend"] = "local"
    storage_formats = storage.setdefault("formats", {})
    storage_formats["sqlite"] = True
    storage_formats["txt"] = False
    storage_formats["html"] = True
    storage.setdefault("local", {})["data_dir"] = "output"

    advanced = base_config.setdefault("advanced", {})
    crawler = advanced.setdefault("crawler", {})
    legacy_crawler = legacy_profile.get("crawler", {})
    crawler["request_interval"] = legacy_crawler.get(
        "request_interval",
        crawler.get("request_interval", 1000),
    )
    crawler["use_proxy"] = False
    crawler["default_proxy"] = ""

    return base_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare a v6.6.0-compatible TrendRadar cloud config.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("incremental", "current", "daily"),
        help="TrendRadar report mode to prepare.",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Tokyo",
        help="Timezone to write into the runtime config.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path to the generated runtime YAML file.",
    )
    args = parser.parse_args()

    if not KEYWORD_FILE_PATH.exists():
        raise FileNotFoundError(f"Keyword file not found: {KEYWORD_FILE_PATH}")

    runtime_config = build_runtime_config(args.mode, args.timezone)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            runtime_config,
            handle,
            allow_unicode=True,
            sort_keys=False,
        )

    source_count = len(runtime_config.get("platforms", {}).get("sources", []))
    print(f"Prepared runtime config: {output_path}")
    print(f"Mode: {args.mode}")
    print(f"Timezone: {args.timezone}")
    print(f"Curated sources: {source_count}")
    print(f"Keyword file: {KEYWORD_FILE_PATH}")


if __name__ == "__main__":
    main()
