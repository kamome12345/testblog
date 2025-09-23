# scripts/rss_to_hugo_ai.py
import os, re, json, time, pathlib, datetime, base64
from urllib.parse import urlparse
import feedparser, requests
from bs4 import BeautifulSoup
from slugify import slugify
from tenacity import retry, wait_exponential, stop_after_attempt
from email.utils import parsedate_to_datetime

FEED_URL   = os.environ.get("FEED_URL", "https://news.yahoo.co.jp/rss/topics/entertainment.xml")
POSTS_DIR  = os.environ.get("HUGO_POSTS_DIR", "content/posts")
SEEN_PATH  = "data/seen_entertainment_ids.json"
CATEGORY   = os.environ.get("CATEGORY", "Entertainment")
LLM_MODEL  = os.environ.get("LLM_MODEL", "gpt-4o-mini")
IMG_MODEL  = os.environ.get("IMG_MODEL", "gpt-image-1")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY")

pathlib.Path(POSTS_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path("data").mkdir(parents=True, exist_ok=True)

def load_seen():
    if not os.path.exists(SEEN_PATH):
        return set()
    with open(SEEN_PATH, "r", encoding="utf-8") as f:
        try:
            return set(json.load(f))
        except Exception:
            return set()

def save_seen(seen:set):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, ensure_ascii=False, indent=2)

def clean_text(html_or_text):
    soup = BeautifulSoup(html_or_text or "", "html.parser")
    txt = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", txt).strip()

def build_dirname(title, published_struct):
    dt = datetime.datetime.fromtimestamp(time.mktime(published_struct)) if published_struct else datetime.datetime.now()
    yyyyMMdd = dt.strftime("%Y%m%d")
    mmdd = dt.strftime("%m%d")
    slug = slugify(title)[:60] or "untitled"
    return f"{yyyyMMdd}-{mmdd}-{slug}", dt

def jst_iso(dt):
    return (dt + datetime.timedelta(hours=9)).isoformat(timespec="seconds") + "+09:00"

LLM_SYS_PROMPT = """あなたはブログ編集者です。以下の入力（ニュースの見出しとURL）は単なる「話題のヒント」です。
著作権や虚偽報道を避けるため、記事本文は一次記事からのコピペや要約ではなく、あなた自身のオリジナル文章で、
背景説明・用語解説・影響・関連トピック紹介・過去事例比較など“付加価値のある解説記事”を日本語で作成してください。

制約:
- 事実と推測を明確に分けてください（「〜と報じられている」「可能性がある」等）。
- 出典リンクは「参考リンク」として末尾に1つだけ掲載（与えられたURL）。
- 600〜900字程度、段落分け。見出し(H2)2〜3個。箇条書き可。
- 批判や断定は避け、中立・丁寧なトーン。
- 冒頭に「※本記事はAI生成のオリジナル解説であり、一次報道の要約・転載ではありません。」と1行で明記。
"""

LLM_USER_TEMPLATE = """題名: {title}
参考URL: {url}
概要ヒント（RSSのsummaryがある場合）: {summary}
"""

IMG_PROMPT_TEMPLATE = """日本の芸能ニュースの話題に合わせたブログ用アイキャッチ。抽象的でクリーン、テキスト文字は入れない、過度な写実で人物特定をしない、ブログのヘッダーに合う横長1枚、スタジオ風ライト、シンプルなシェイプとグラデーションを主体、落ち着いた配色。テーマ: {title}"""

# --- OpenAI API (minimal)
OPENAI_BASE = "https://api.openai.com/v1"

def openai_headers():
    return {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json",
    }

def _retry_after_seconds(response):
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return max(0, int(float(header)))
    except (TypeError, ValueError):
        try:
            retry_dt = parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if retry_dt is None:
            return None
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=datetime.timezone.utc)
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (retry_dt - now).total_seconds())


def _handle_rate_limit(response, context):
    if response.status_code != 429:
        return
    wait_for = _retry_after_seconds(response)
    if not wait_for:
        wait_for = 30
    wait_for = max(wait_for, 10)
    print(f"OpenAI rate limit hit during {context}. Waiting {int(wait_for)}s before retrying...", flush=True)
    time.sleep(wait_for)


@retry(wait=wait_exponential(multiplier=1, min=1, max=20), stop=stop_after_attempt(5))
def gen_article(title, url, summary):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYS_PROMPT},
            {"role": "user", "content": LLM_USER_TEMPLATE.format(title=title, url=url, summary=summary or "（なし）")}
        ],
        "temperature": 0.7,
    }
    r = requests.post(f"{OPENAI_BASE}/chat/completions", headers=openai_headers(), json=payload, timeout=60)
    _handle_rate_limit(r, "article generation")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()

@retry(wait=wait_exponential(multiplier=1, min=1, max=20), stop=stop_after_attempt(5))
def gen_image_png(title):
    prompt = IMG_PROMPT_TEMPLATE.format(title=title)
    payload = {
        "model": IMG_MODEL,
        "prompt": prompt,
        "size": "1024x576",      # 横長
        "n": 1,
    }
    r = requests.post(f"{OPENAI_BASE}/images/generations", headers=openai_headers(), json=payload, timeout=120)
    _handle_rate_limit(r, "image generation")
    r.raise_for_status()
    data = r.json()["data"][0]
    if "b64_json" in data:
        return base64.b64decode(data["b64_json"])
    # 万一URL返却型なら取得
    if "url" in data:
        img = requests.get(data["url"], timeout=60)
        img.raise_for_status()
        return img.content
    raise RuntimeError("No image data")

def build_front_matter(title, date_iso, link, summary_for_meta):
    sanitized_title = title.replace('"', "'")
    fm = [
        "---",
        f'title: "{sanitized_title}"',
        f"date: {date_iso}",
        "draft: false",
        f'categories: ["%s"]' % CATEGORY,
        'tags: ["AI記事","Entertainment"]',
        f'canonicalURL: "{link}"',
        "cover:",
        '  image: "featured.png"',
        f'  alt: "{sanitized_title}"',
        "  relative: true",
        'description: "' + summary_for_meta.replace('"', "'")[:150] + '"',
        "---",
        "",
    ]
    return "\n".join(fm)


def main():
    if not OPENAI_KEY:
        raise SystemExit("OPENAI_API_KEY is not set.")

    seen = load_seen()
    feed = feedparser.parse(FEED_URL)
    created = 0

    for e in feed.entries:
        eid = e.get("id") or e.get("link")
        if not eid or eid in seen:
            continue

        title = (e.get("title") or "").strip()
        link  = (e.get("link")  or "").strip()
        published = e.get("published_parsed") or e.get("updated_parsed")
        dirname, dt = build_dirname(title, published)
        post_dir = pathlib.Path(POSTS_DIR) / dirname
        post_dir.mkdir(parents=True, exist_ok=True)

        # 要約はメタ説明用に軽く整形（本文はAIがフル生成）
        rss_summary = clean_text(e.get("summary") or e.get("description", ""))
        date_iso = jst_iso(dt)

        # 1) 本文生成（オリジナル解説）
        article_md = gen_article(title, link, rss_summary)

        # 2) アイキャッチ生成（PNG）
        img_bytes = gen_image_png(title)
        with open(post_dir / "featured.png", "wb") as f:
            f.write(img_bytes)

        # 3) Front Matter + 本文の index.md を出力
        fm = build_front_matter(title, date_iso, link, rss_summary or title)
        body = fm + article_md + "\n\n---\n参考リンク: " + link + "\n"
        with open(post_dir / "index.md", "w", encoding="utf-8") as f:
            f.write(body)

        seen.add(eid)
        created += 1

    save_seen(seen)
    print(f"Created posts: {created}")

if __name__ == "__main__":
    main()
