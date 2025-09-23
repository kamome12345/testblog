"""Generate Hugo posts from an RSS feed with OpenAI assistance.

This script fetches the most recent entries from an RSS feed, asks an LLM
for a rewritten article and image prompt, generates a cover image, and writes
out a Hugo page bundle.

It expects the following environment variables (defaults in parentheses):
    FEED_URL            – RSS feed to consume (required)
    HUGO_POSTS_DIR      – Directory containing Hugo posts ("content/posts")
    CATEGORY            – Category name to add to front matter (optional)
    LLM_MODEL           – Chat completion model name ("gpt-4o-mini")
    IMG_MODEL           – Image generation model name ("gpt-image-1")
    OPENAI_BASE         – Base URL for OpenAI compatible APIs
                          ("https://api.openai.com/v1")
    OPENAI_API_KEY      – API key for authentication (required)

The script exits early with helpful messages if the API key is missing or
contains only whitespace/newlines – a frequent cause of "Invalid header value"
errors when running in GitHub Actions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
from tenacity import retry, stop_after_attempt, wait_random_exponential

DEFAULT_FEED_URL = "https://news.yahoo.co.jp/rss/topics/entertainment.xml"
DEFAULT_POSTS_DIR = "content/posts"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
DEFAULT_IMG_MODEL = "gpt-image-1"
DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: datetime


def _env(name: str, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        return ""
    return value


def _sanitize_api_key(raw_key: str) -> str:
    """Strip whitespace/newlines and validate the result."""

    stripped = raw_key.strip()
    if not stripped:
        raise ConfigError(
            "OPENAI_API_KEY is required and cannot be blank or whitespace."
        )
    if stripped != raw_key:
        print("[rss_to_hugo_ai] Trimmed surrounding whitespace in OPENAI_API_KEY.")
    return stripped


OPENAI_API_KEY = _sanitize_api_key(_env("OPENAI_API_KEY"))
OPENAI_BASE = _env("OPENAI_BASE", DEFAULT_OPENAI_BASE).rstrip("/")
LLM_MODEL = _env("LLM_MODEL", DEFAULT_LLM_MODEL)
IMG_MODEL = _env("IMG_MODEL", DEFAULT_IMG_MODEL)
FEED_URL = _env("FEED_URL", DEFAULT_FEED_URL)
HUGO_POSTS_DIR = Path(_env("HUGO_POSTS_DIR", DEFAULT_POSTS_DIR))
CATEGORY = _env("CATEGORY").strip()

# Ensure the sanitized key is visible to child imports if any.
os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY


def openai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


@retry(wait=wait_random_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
def _request_json(method: str, url: str, *, payload: dict) -> dict:
    response = requests.request(
        method,
        url,
        headers=openai_headers(),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def fetch_feed_entries(feed_url: str, limit: int = 3) -> list[FeedItem]:
    """Fetch a small batch of items from an RSS feed."""

    resp = requests.get(feed_url, timeout=30)
    resp.raise_for_status()

    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:  # type: ignore[attr-defined]
        raise RuntimeError(f"Failed to parse RSS feed: {exc}") from exc

    channel = root.find("channel")
    items = [] if channel is None else channel.findall("item")
    entries: list[FeedItem] = []

    for item in items[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = (item.findtext("description") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()

        if not title or not link:
            continue

        published = _parse_pubdate(pub_raw)
        entries.append(FeedItem(title=title, link=link, summary=summary, published=published))

    return entries


_RFC822_FORMATS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %z",
)


def _parse_pubdate(raw: str) -> datetime:
    for fmt in _RFC822_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def _slugify(text: str, link: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z]+", "-", text).strip("-").lower()
    if not normalized:
        normalized = "post"
    digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{stamp}-{normalized[:32]}-{digest}"


@retry(wait=wait_random_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
def gen_article(title: str, link: str, summary: str) -> tuple[str, str]:
    """Ask the LLM for a rewritten article and an accompanying image prompt."""

    system_message = textwrap.dedent(
        """
        あなたは報道記者です。以下のRSS記事をもとに、
        1. 見出しと本文をMarkdownで整理し、
        2. 記事冒頭に1段落のリード文を挿入し、
        3. 300〜500語程度でニュース記事として要約してください。
        記事末尾には引用元リンクもMarkdown形式で掲載してください。
        """
    ).strip()

    user_message = textwrap.dedent(
        f"""
        タイトル: {title}
        リンク: {link}
        要約: {summary or '（要約はありません）'}

        Markdownの記事本文と、記事内容を端的に描写する日本語の画像プロンプトをJSONで返してください。
        フォーマット: {{"article": "...", "image_prompt": "..."}}
        """
    ).strip()

    payload = {
        "model": LLM_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.7,
        "max_completion_tokens": 1024,
    }

    data = _request_json("POST", f"{OPENAI_BASE}/chat/completions", payload=payload)
    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content")
    if not content:
        raise RuntimeError("OpenAI response did not include article content")

    try:
        parsed = json.loads(content)
        article = parsed["article"].strip()
        image_prompt = parsed["image_prompt"].strip()
    except (KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unexpected response format from OpenAI: {content!r}") from exc

    return article, image_prompt


@retry(wait=wait_random_exponential(multiplier=1, min=2, max=20), stop=stop_after_attempt(3))
def gen_cover_image(prompt: str) -> bytes:
    payload = {
        "model": IMG_MODEL,
        "prompt": prompt,
        "size": "1024x1024",
        "response_format": "b64_json",
    }
    data = _request_json("POST", f"{OPENAI_BASE}/images/generations", payload=payload)
    try:
        b64_data = data["data"][0]["b64_json"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected image response: {data}") from exc
    return base64.b64decode(b64_data)


def _summary_from_article(article: str, max_chars: int = 160) -> str:
    plain = re.sub(r"[\r\n]+", " ", article)
    return plain[: max_chars - 3] + "..." if len(plain) > max_chars else plain


def _toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_post(bundle_dir: Path, item: FeedItem, article_md: str, image_bytes: bytes) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    cover_name = "cover.png"
    cover_path = bundle_dir / cover_name
    cover_path.write_bytes(image_bytes)

    summary = item.summary or _summary_from_article(article_md)

    front_lines = [
        "+++",
        f"title = {_toml_str(item.title)}",
        f"date = {_toml_str(item.published.astimezone(timezone.utc).isoformat())}",
        "draft = false",
    ]

    if CATEGORY:
        front_lines.append(f"categories = {json.dumps([CATEGORY], ensure_ascii=False)}")

    front_lines.extend(
        [
            f"summary = {_toml_str(summary)}",
            "summaryLength = 30",
            "tags = []",
            "",
            "[cover]",
            f"image = {_toml_str(cover_name)}",
            f"alt = {_toml_str(item.title)}",
            "relative = true",
            "hidden = false",
            "+++",
            "",
        ]
    )

    slug_link = urlparse(item.link)
    article_with_source = article_md.rstrip() + f"\n\n> [引用元]({slug_link.geturl()})\n"

    bundle_dir.joinpath("index.md").write_text(
        "\n".join(front_lines) + article_with_source + "\n",
        encoding="utf-8",
    )


def generate_posts(items: Iterable[FeedItem]) -> None:
    HUGO_POSTS_DIR.mkdir(parents=True, exist_ok=True)

    for item in items:
        slug = _slugify(item.title, item.link)
        bundle_dir = HUGO_POSTS_DIR / slug
        if bundle_dir.exists():
            print(f"[rss_to_hugo_ai] Skipping existing bundle: {bundle_dir}")
            continue

        print(f"[rss_to_hugo_ai] Generating article for: {item.title}")
        article_md, image_prompt = gen_article(item.title, item.link, item.summary)
        print(f"[rss_to_hugo_ai] Image prompt: {image_prompt}")
        image_bytes = gen_cover_image(image_prompt)
        write_post(bundle_dir, item, article_md, image_bytes)
        print(f"[rss_to_hugo_ai] Wrote {bundle_dir / 'index.md'}")


def main() -> None:
    items = fetch_feed_entries(FEED_URL)
    if not items:
        print(f"[rss_to_hugo_ai] No entries found in feed: {FEED_URL}")
        return
    generate_posts(items)


if __name__ == "__main__":
    main()
