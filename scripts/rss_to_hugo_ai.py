# scripts/rss_to_hugo_ai.py
import os, re, json, time, pathlib, datetime, base64
import feedparser, requests
from bs4 import BeautifulSoup
from slugify import slugify

# ====== 環境変数 ======
FEED_URL    = os.environ.get("FEED_URL", "https://news.yahoo.co.jp/rss/topics/entertainment.xml")
POSTS_DIR   = os.environ.get("HUGO_POSTS_DIR", "content/posts")
CATEGORY    = os.environ.get("CATEGORY", "Entertainment")

# OpenAI互換エンドポイント（将来Groq等に差し替えたいときはここをenvで変更）
OPENAI_BASE = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")

LLM_MODEL   = os.environ.get("LLM_MODEL", "gpt-4o-mini")
IMG_MODEL   = os.environ.get("IMG_MODEL", "gpt-image-1")
SKIP_IMAGE  = os.environ.get("SKIP_IMAGE", "0") == "1"

# 429対策：1回の実行で作る件数上限（既定1件）
MAX_NEW_POSTS = int(os.environ.get("MAX_NEW_POSTS", "1"))

# OpenAIキー：前後空白を除去して形式をチェック
OPENAI_KEY  = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_KEY.startswith("sk-"):
    raise SystemExit(
        "OPENAI_API_KEY が不正です。Secrets に sk- から始まるキーを登録し、"
        "前後の空白/改行や引用符は除いてください。"
    )

# 既読管理
SEEN_PATH = "data/seen_entertainment_ids.json"

# 出力先のディレクトリを用意
pathlib.Path(POSTS_DIR).mkdir(parents=True, exist_ok=True)
pathlib.Path("data").mkdir(parents=True, exist_ok=True)

# ====== ユーティリティ ======
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
    # ディレクトリ名: YYYYMMDD-mmdd-slug
    dt = datetime.datetime.fromtimestamp(
        datetime.datetime(*published_struct[:6]).timestamp()
    ) if published_struct else datetime.datetime.utcnow()
    yyyyMMdd = dt.strftime("%Y%m%d")
    mmdd = dt.strftime("%m%d")
    slug = slugify(title)[:60] or "untitled"
    return f"{yyyyMMdd}-{mmdd}-{slug}", dt

def jst_iso(dt):
    # 与えられた naive dt を JST(+09:00) のISO表記に
    return (dt + datetime.timedelta(hours=9)).isoformat(timespec="seconds") + "+09:00"

def now_jst_iso():
    # 現在時刻を JST(+09:00) のISO表記に
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).isoformat(timespec="seconds") + "+09:00"

def openai_headers():
    return {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json",
    }

# ====== OpenAI呼び出し（429/クォータ差分を判定しつつリトライ） ======
def post_openai(url, payload, timeout=60, max_attempts=5):
    """
    429には「レート」と「残高不足（insufficient_quota）」が混在するため、
    レスポンス本文を見て分岐する。レートならRetry-Afterを尊重して再試行。
    """
    for attempt in range(1, max_attempts + 1):
        r = requests.post(url, headers=openai_headers(), json=payload, timeout=timeout)
        # 429は二種類あるので本文を見る
        if r.status_code == 429:
            try:
                body = r.json()
                err  = (body.get("error") or {})
                code = (err.get("code") or "").lower()
                msg  = (err.get("message") or "").lower()
            except Exception:
                code = msg = ""

            if "insufficient_quota" in (code + msg):
                raise SystemExit(
                    "OpenAI: 残高不足/課金未設定により拒否されました。"
                    "PlatformのBillingでプリペイド（最低$5）を追加してください。"
                )

            # レート制限 → Retry-Afterを尊重して待機
            retry_after = 0
            try:
                retry_after = int(r.headers.get("retry-after", "30"))
            except Exception:
                retry_after = 30
            wait = max(10, min(retry_after, 60))
            print(f"Rate limited (attempt {attempt}/{max_attempts}). Sleeping {wait}s...")
            time.sleep(wait)
            continue

        # その他のエラーはそのまま出す
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            if r.status_code == 401:
                raise SystemExit("OpenAI: 認証エラー（APIキーが正しいか確認してください）。") from e
            raise
        return r

    raise SystemExit("OpenAI: 429が続いたため中断しました。MAX_NEW_POSTSを下げる/実行間隔を延ばしてください。")

# ====== 生成プロンプト ======
# 記事本文 + ミケ記者の一言 を“同時に”JSONで返させる
LLM_SYS_PROMPT = """あなたはブログ編集者です。以下の入力（ニュースの見出しとURL）は単なる「話題のヒント」です。
著作権や虚偽報道を避けるため、記事本文は一次記事からのコピペや要約ではなく、あなた自身のオリジナル文章で、
背景説明・用語解説・影響・関連トピック紹介・過去事例比較など“付加価値のある解説記事”を日本語で作成してください。

同時に、トップ一覧の description 用として「ミケ記者（優しい三毛猫の記者）」による一言メッセージを生成してください。
- 1文だけ、やさしく共感/心配/喜びなどの感情が伝わる
- 語尾は必ず「にゃ」で終える（絵文字・顔文字は使わない）
- 30〜60文字程度
- 固有名詞や断定的表現は避け、誰かを傷つけない配慮

最終出力は JSON のみで返してください（コードブロックや説明文は禁止）:
{
  "article_md": "<Markdown本文>",
  "mike_comment": "<ミケ記者の一言（〜にゃ）>"
}

記事本文の制約:
- 冒頭に「※本記事はAI生成のオリジナル解説であり、一次報道の要約・転載ではありません。」と1行で明記
- 600〜900字程度、段落分け。見出し(H2)を2〜3個。箇条書き可。
- 事実と推測を明確に分ける（「〜と報じられている」「可能性がある」等）
- 出典リンクは末尾に1つだけ掲載（与えられたURL）
- 中立・丁寧なトーン
"""

LLM_USER_TEMPLATE = """題名: {title}
参考URL: {url}
概要ヒント（RSSのsummaryがある場合）: {summary}
"""

IMG_PROMPT_TEMPLATE = """日本の芸能ニュースの話題に合わせたブログ用アイキャッチ。抽象的でクリーン、テキスト文字は入れない、過度な写実で人物特定をしない、ブログのヘッダーに合う横長1枚、スタジオ風ライト、シンプルなシェイプとグラデーションを主体、落ち着いた配色。テーマ: {title}"""

# ====== JSONパース（堅牢化） ======
def parse_json_strict_or_slice(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
    raise ValueError("LLMから有効なJSONが取得できませんでした")

# ====== LLM/画像 生成関数 ======
def gen_article_and_comment(title, url, summary):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYS_PROMPT},
            {"role": "user", "content": LLM_USER_TEMPLATE.format(title=title, url=url, summary=summary or "（なし）")},
        ],
        "temperature": 0.7,
    }
    r = post_openai(f"{OPENAI_BASE}/chat/completions", payload, timeout=60)
    obj = parse_json_strict_or_slice(r.json()["choices"][0]["message"]["content"])
    article_md = (obj.get("article_md") or "").strip()
    mike_comment = (obj.get("mike_comment") or "").strip()
    # サニタイズ & 短すぎ/長すぎ対策
    mike_comment = re.sub(r"\s+", " ", mike_comment)
    if not mike_comment.endswith("にゃ"):
        mike_comment = (mike_comment.rstrip("。.!?、，") + "にゃ").strip()
    if len(mike_comment) > 120:
        mike_comment = mike_comment[:118].rstrip() + "にゃ"
    return article_md, mike_comment

def gen_image_png(title):
    prompt = IMG_PROMPT_TEMPLATE.format(title=title)
    payload = {
        "model": IMG_MODEL,
        "prompt": prompt,
        "size": "1024x576",   # 横長
        "n": 1,
    }
    r = post_openai(f"{OPENAI_BASE}/images/generations", payload, timeout=120)
    data = r.json()["data"][0]
    if "b64_json" in data:
        return base64.b64decode(data["b64_json"])
    if "url" in data:
        img = requests.get(data["url"], timeout=60)
        img.raise_for_status()
        return img.content
    raise RuntimeError("No image data")

# ====== Front Matter ======
def build_front_matter(title, date_iso, publish_iso, link, description_text, include_cover: bool):
    """
    date:        “出来事”の日時（RSS由来、JST変換）
    publishDate: 実際の公開日時（今のJST）
    lastmod:     更新日時（publishDateと同じでOK）
    description: ミケ記者の一言（〜にゃ）
    """
    sanitized_title = title.replace('"', "'")
    desc = (description_text or sanitized_title).replace('"', "'").strip()[:150]
    fm = [
        "---",
        f'title: "{sanitized_title}"',
        f"date: {date_iso}",
        f"publishDate: {publish_iso}",
        f"lastmod: {publish_iso}",
        "draft: false",
        f'categories: ["{CATEGORY}"]',
        'tags: ["AI記事","Entertainment"]',
        f'canonicalURL: "{link}"',
    ]
    if include_cover:
        fm += [
            "cover:",
            '  image: "featured.png"',
            f'  alt: "{sanitized_title}"',
            "  relative: true",
        ]
    fm += [f'description: "{desc}"', "---", ""]
    return "\n".join(fm)

# ====== メイン処理 ======
def main():
    seen = load_seen()
    feed = feedparser.parse(FEED_URL)
    created = 0
    checked = 0

    for e in feed.entries:
        if created >= MAX_NEW_POSTS:
            break

        eid = e.get("id") or e.get("link")
        if not eid:
            print("[SKIP] entry without id/link")
            continue
        if eid in seen:
            print(f"[SKIP] already seen: {eid}")
            continue

        checked += 1
        title = (e.get("title") or "").strip()
        link  = (e.get("link")  or "").strip()
        published = e.get("published_parsed") or e.get("updated_parsed")
        dirname, dt = build_dirname(title, published)
        post_dir = pathlib.Path(POSTS_DIR) / dirname
        post_dir.mkdir(parents=True, exist_ok=True)

        rss_summary = clean_text(e.get("summary") or e.get("description", ""))
        date_iso = jst_iso(dt)          # 出来事の日時（JST）
        publish_iso = now_jst_iso()     # 公開日時（今・JST）

        try:
            print(f"[TRY] create: {dirname}")
            # 1) 本文 + ミケの一言 を同時生成
            article_md, mike_comment = gen_article_and_comment(title, link, rss_summary)

            # 2) 画像生成（必要ならスキップ可）
            has_image = False
            if not SKIP_IMAGE:
                try:
                    img_bytes = gen_image_png(title)
                    with open(post_dir / "featured.png", "wb") as f:
                        f.write(img_bytes)
                    has_image = True
                except Exception as img_ex:
                    print(f"[WARN] image generation failed: {img_ex}")

            # 3) Front Matter + 本文の index.md 出力（description = ミケ記者の一言）
            fm = build_front_matter(
                title, date_iso, publish_iso, link,
                mike_comment,
                include_cover=has_image
            )
            body = fm + article_md + "\n\n---\n参考リンク: " + link + "\n"
            with open(post_dir / "index.md", "w", encoding="utf-8") as f:
                f.write(body)

            # 4) 既読登録
            seen.add(eid)
            created += 1
            print(f"[OK] created: {post_dir}")

            # 保険で少し待つ（次ループに進む前）
            time.sleep(2)

        except requests.HTTPError as http_ex:
            try:
                err = http_ex.response.json()
                print(f"[ERROR] HTTP {http_ex.response.status_code}: {err}")
            except Exception:
                print(f"[ERROR] {http_ex}")
            continue
        except Exception as ex:
            print(f"[ERROR] Unexpected: {ex}")
            continue

    save_seen(seen)
    print(f"Checked entries: {checked}")
    print(f"Created posts: {created}")

if __name__ == "__main__":
    main()
