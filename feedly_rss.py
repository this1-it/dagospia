#!/usr/bin/env python3
"""Dagospia RSS feed generator — scrapes m.dagospia.com and outputs dagospia.xml."""

import logging
import os
import re
import time
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — edit these as needed
# ---------------------------------------------------------------------------

BASE_URL = "https://m.dagospia.com/"

# Categories to exclude (first path segment of article URL, e.g. "politica")
BLACKLISTED_CATEGORIES: list = [
    # "politica",
    # "business",
    # "cronache",
    # "media-tv",
]

OUTPUT_FILE = "rss.xml"  # relative to CWD, or set absolute path e.g. "/tmp/dagospia.xml"


ARTICLE_LIMIT = 0       # max articles to include; 0 = no limit
FETCH_PUB_DATE = True   # set False to skip per-article requests and use now()
REQUEST_DELAY = 0.5     # seconds between article-page requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; Mobile) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Mobile Safari/537.36"
    )
}

ARTICLE_RE = re.compile(r"^/([a-z][a-z0-9\-]*)/[a-z0-9][a-z0-9\-]*-\d+$")

# ---------------------------------------------------------------------------


def fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        log.warning("fetch failed %s: %s", url, exc)
        return None


def scrape_homepage() -> list[dict]:
    log.debug("Scraping %s", BASE_URL)
    soup = fetch(BASE_URL)
    if soup is None:
        log.error("could not fetch homepage")
        sys.exit(1)

    seen: set[str] = set()
    items: list[dict] = []

    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"].split("?")[0].rstrip("/")
        match = ARTICLE_RE.match(href)
        if not match:
            continue

        category = match.group(1)
        if category in BLACKLISTED_CATEGORIES:
            continue

        full_url = urljoin(BASE_URL, href)
        if full_url in seen:
            continue
        seen.add(full_url)

        title = a_tag.get_text(strip=True)
        if not title:
            continue

        # find nearest image: check parent/siblings for <img>
        image_url: Optional[str] = None
        parent = a_tag.parent
        if parent:
            img = parent.find("img", src=True)
            if img and "static.dagospia.com" in img["src"]:
                image_url = img["src"]

        items.append(
            {
                "url": full_url,
                "title": title,
                "category": category,
                "image": image_url,
            }
        )

    log.debug("Found %d articles", len(items))
    if ARTICLE_LIMIT > 0:
        items = items[:ARTICLE_LIMIT]
        log.debug("Limit applied → %d articles", len(items))
    return items


_IT_MONTHS = {
    "gen": 1, "feb": 2, "mar": 3, "apr": 4, "mag": 5, "giu": 6,
    "lug": 7, "ago": 8, "set": 9, "ott": 10, "nov": 11, "dic": 12,
}
_ROME_TZ = ZoneInfo("Europe/Rome")


def _parse_data_ora(text: str) -> Optional[datetime]:
    """Parse Italian date like '14 apr 2026 15:20' with Europe/Rome timezone."""
    try:
        parts = text.strip().split()
        # parts: ['14', 'apr', '2026', '15:20']
        day = int(parts[0])
        month = _IT_MONTHS[parts[1].lower()]
        year = int(parts[2])
        hour, minute = (int(x) for x in parts[3].split(":"))
        return datetime(year, month, day, hour, minute, tzinfo=_ROME_TZ)
    except (KeyError, ValueError, IndexError):
        return None


def get_article_meta(url: str) -> tuple:
    """Fetch article page, return (pub_date, description)."""
    soup = fetch(url)
    if soup is None:
        return datetime.now(timezone.utc), ""

    # --- pub date ---
    pub_date = None
    time_tag = soup.find("time", class_="data-ora")
    if time_tag:
        pub_date = _parse_data_ora(time_tag.get_text())
        if not pub_date:
            log.warning("could not parse data-ora: %r", time_tag.get_text())

    if pub_date is None:
        for prop in ("article:published_time", "og:updated_time", "date"):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                try:
                    dt_str = tag["content"]
                    if dt_str.endswith("Z"):
                        dt_str = dt_str[:-1] + "+00:00"
                    pub_date = datetime.fromisoformat(dt_str)
                    break
                except ValueError:
                    pass

    if pub_date is None:
        pub_date = datetime.now(timezone.utc)

    # --- description: <p> inside .hero-section ---
    hero = soup.find(class_="hero-section")
    description = ""
    if hero:
        p = hero.find("p")
        if p:
            description = p.get_text(separator=" ", strip=True)

    # --- body: all <p> tags text joined ---
    paragraphs = [p.get_text(separator=" ", strip=True) for p in soup.find_all("p") if p.get_text(strip=True)]
    body = "\n\n".join(paragraphs)

    return pub_date, description, body


def load_rss_cache(path: str) -> dict[str, dict]:
    """Parse existing RSS file → {url: {title, description, pub_date, category, image, mime}}."""
    cache: dict[str, dict] = {}
    if not os.path.exists(path):
        return cache
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        log.warning("cache parse error, ignoring: %s", exc)
        return cache
    ns = {"content": "http://purl.org/rss/1.0/modules/content/"}
    for item in tree.findall(".//item"):
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        pub_date_str = item.findtext("pubDate") or ""
        pub_date = None
        if pub_date_str:
            try:
                pub_date = parsedate_to_datetime(pub_date_str)
            except Exception:
                pass
        if pub_date is None:
            pub_date = datetime.now(timezone.utc)
        enclosure = item.find("enclosure")
        image = enclosure.get("url") if enclosure is not None else None
        mime = enclosure.get("type") if enclosure is not None else None
        cache[link] = {
            "title": (item.findtext("title") or "").strip(),
            "description": (item.findtext("description") or "").strip(),
            "pub_date": pub_date,
            "category": (item.findtext("category") or "").strip(),
            "image": image,
            "mime": mime,
        }
    log.debug("Loaded %d items from cache %s", len(cache), path)
    return cache


def build_feed(items: list[dict]) -> None:
    cache = load_rss_cache(OUTPUT_FILE)

    fg = FeedGenerator()
    fg.id(BASE_URL)
    fg.title("Dagospia")
    fg.link(href=BASE_URL, rel="alternate")
    fg.description("Dagospia — rassegna stampa e notizie")
    fg.language("it")
    fg.lastBuildDate(datetime.now(timezone.utc))

    entries: list[dict] = []
    for idx, item in enumerate(items):
        log.debug("[%d/%d] %s", idx + 1, len(items), item["title"][:60])

        url = item["url"]
        if url in cache:
            cached = cache[url]
            log.debug("cache hit: %s", url)
            pub_date = cached["pub_date"]
            description = cached["description"] or item["title"]
            image = cached["image"] or item["image"]
            mime = cached["mime"]
            title = cached["title"] or item["title"]
            category = cached["category"] or item["category"]
        else:
            pub_date = datetime.now(timezone.utc)
            description = item["title"]
            image = item["image"]
            mime = None
            title = item["title"]
            category = item["category"]
            if FETCH_PUB_DATE:
                pub_date, description, _body = get_article_meta(url)
                time.sleep(REQUEST_DELAY)

        # ensure timezone-aware
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)

        # resolve MIME from URL if not cached
        if image and not mime:
            ext = image.rsplit(".", 1)[-1].lower()
            mime = "image/webp" if ext == "webp" else f"image/{ext}" if ext in ("jpg", "jpeg", "png", "gif") else "image/jpeg"

        entries.append({"url": url, "title": title, "description": description,
                        "pub_date": pub_date, "category": category, "image": image, "mime": mime})

    entries.sort(key=lambda e: e["pub_date"], reverse=False)

    for entry in entries:
        fe = fg.add_entry()
        fe.id(entry["url"])
        fe.title(entry["title"])
        fe.link(href=entry["url"])
        fe.description(entry["description"])
        fe.pubDate(entry["pub_date"])
        fe.category({"term": entry["category"]})
        if entry["image"] and entry["mime"]:
            fe.enclosure(entry["image"], 0, entry["mime"])

    fg.rss_file(OUTPUT_FILE, pretty=True)
    log.debug("Feed written → %s", OUTPUT_FILE)


def main() -> None:
    items = scrape_homepage()
    if not items:
        log.error("no articles found — site structure may have changed")
        sys.exit(1)
    build_feed(items)
    try:
        ET.parse(OUTPUT_FILE)
        log.debug("XML valid ✓")
    except ET.ParseError as exc:
        log.warning("XML parse error: %s", exc)


if __name__ == "__main__":
    main()
