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

# ★追加：ミケ記者の一言の重複回避用
MIKE_SEEN_PATH = "data/seen_mike_comments.json"

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
    # ディレクトリ名: YYYYMMDD-hhmmss-スラッグ（時間情報も含める）
    dt = datetime.datetime.fromtimestamp(
        datetime.datetime(*published_struct[:6]).timestamp()
    ) if published_struct else datetime.datetime.utcnow()
    yyyyMMdd = dt.strftime("%Y%m%d")
    hhmmss = dt.strftime("%H%M%S")
    # ★日本語をそのまま許可（ピンイン化しない）
    slug = slugify(title, allow_unicode=True)[:60] or "無題"
    # 先頭/末尾のハイフンを整える
    slug = slug.strip("-")
    return f"{yyyyMMdd}-{hhmmss}-{slug}", dt

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

# ====== ミケ重複管理 ======
def load_seen_mike():
    p = MIKE_SEEN_PATH
    if not os.path.exists(p):
        return set()
    try:
        with open(p, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return set(arr if isinstance(arr, list) else [])
    except Exception:
        return set()

def save_seen_mike(seen:set, keep_last=500):
    arr = list(seen)
    if len(arr) > keep_last:
        arr = arr[-keep_last:]
    with open(MIKE_SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False, indent=2)

def _norm_mike(s: str) -> str:
    # 空白・句読点などを落として近似重複も抑える（簡易）
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[。、．，!！?？・…~〜\-—_（）\(\)「」『』\"'“”’`]", "", s)
    return s


# ====== OpenAI呼び出し（429/クォータ差分を判定しつつリトライ） ======
def post_openai(url, payload, timeout=60, max_attempts=5):
    """
    429には「レート」と「残高不足（insufficient_quota）」が混在するため、
    レスポンス本文を見て分岐する。レートならRetry-Afterを尊重して再試行。
    """
    for attempt in range(1, max_attempts + 1):
        r = requests.post(url, headers=openai_headers(), json=payload, timeout=timeout)
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
            retry_after = 0
            try:
                retry_after = int(r.headers.get("retry-after", "30"))
            except Exception:
                retry_after = 30
            wait = max(10, min(retry_after, 60))
            print(f"Rate limited (attempt {attempt}/{max_attempts}). Sleeping {wait}s...")
            time.sleep(wait)
            continue
        try:
            r.raise_for_status()
        except requests.HTTPError as e:
            if r.status_code == 401:
                raise SystemExit("OpenAI: 認証エラー（APIキーが正しいか確認してください）。") from e
            # エラー本文も出す
            try:
                print("[ERROR] OpenAI response:", r.json())
            except Exception:
                print("[ERROR] OpenAI response (non-JSON):", r.text[:500])
            raise
        return r
    raise SystemExit("OpenAI: 429が続いたため中断しました。MAX_NEW_POSTSを下げる/実行間隔を延ばしてください。")

# ====== 生成プロンプト ======
# 記事本文 + ミケ記者の一言 + タグ配列 を“同時に”JSONで返させる
LLM_SYS_PROMPT = """あなたはブログ編集者です。以下の入力（ニュースの見出しとURL）は単なる「話題のヒント」です。
本文は一次記事の要約・転載ではなく、背景説明・用語解説・影響・関連トピック・過去事例比較など付加価値のある解説を日本語で作成してください。

同時に、トップ一覧の description 用として「ミケ記者（優しい三毛猫の記者）」の一言“候補を6つ”生成してください。
- 1文だけ／30〜60字／語尾は必ず「にゃ」／語り口を変えて重複回避／誰かを傷つけない配慮

さらに、記事に付与するタグを抽出してください（一般名詞のみ、人名・団体名・固有作品名は避ける、3〜5個、10文字以内）。

そして、**オリジナルの日本語見出し（headline_ja）**を作ってください。
- 与えられた見出しと同一/ほぼ同一は不可
- 20〜40文字程度、キャッチーだが煽りすぎない
- 内容を正しく示す

最終出力は JSON のみ:
{
  "headline_ja": "<オリジナル見出し>",
  "article_md": "<Markdown本文>",
  "mike_candidates": ["候補1","候補2","候補3","候補4","候補5","候補6"],
  "tags": ["タグ1","タグ2","タグ3"]
}

記事本文の制約:
- 冒頭に「※本記事はAI生成のオリジナル解説であり、一次報道の要約・転載ではありません。」と1行で明記
- 600〜900字、段落分け、H2を2〜3個、箇条書き可
- 事実と推測を明確化（「〜と報じられている」「可能性がある」等）
- 出典リンクは末尾に1つ（与えられたURL）
- 中立・丁寧なトーン
"""



LLM_USER_TEMPLATE = """題名: {title}
参考URL: {url}
概要ヒント（RSSのsummaryがある場合）: {summary}
"""

IMG_PROMPT_TEMPLATE = """ブログ用の横長アイキャッチ。テキストは入れない。
主役: 3頭身の元気な三毛猫キャラクター「ミケ記者」（擬人化）。
表現: 実在の芸能人を想起させるが、写真の複製や本人そのものの精密再現は避ける。髪型・衣装・小道具・配色・ポーズで“なりきり感”を出す。ブランド/ロゴ/ユニフォームは使わない。
スタイル: ベクター/フラット、太めのアウトライン、やわらかな配色、シンプルな図形とグラデーション。写真風/過度な写実は不可。
出力: 横長1枚。{size_hint}
題名ヒント: {title}
キーワード: {keywords}"""

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

# ====== タグ整形 ======
def sanitize_tags(raw):
    out = []
    if isinstance(raw, list):
        for t in raw:
            if not isinstance(t, str): continue
            s = t.strip().replace("　", " ")
            s = re.sub(r"\s+", "", s)              # 空白除去（短い名詞想定）
            if not s: continue
            if len(s) > 10: s = s[:10]
            if re.search(r"[#/,.\[\]{}()!?:;\"'<>\\|@^~`+=*&%$]", s):
                continue
            # 人名っぽい（カタカナ+姓っぽい等）は簡易に除外（完璧ではない）
            if re.search(r"[A-Za-z]", s):  # 英単語は今回は弾く（必要なら許可）
                continue
            out.append(s)
    # 重複除去の順序維持
    uniq = []
    for x in out:
        if x not in uniq:
            uniq.append(x)
    # 3〜5個に調整
    if len(uniq) < 3:
        return uniq
    return uniq[:5]

# ====== LLM/画像 生成関数 ======
def gen_article_comment_tags(title, url, summary, seen_mike_normed):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": LLM_SYS_PROMPT},
            {"role": "user", "content": LLM_USER_TEMPLATE.format(title=title, url=url, summary=summary or "（なし）")},
        ],
        "temperature": 0.8,
    }
    r = post_openai(f"{OPENAI_BASE}/chat/completions", payload, timeout=60)
    obj = parse_json_strict_or_slice(r.json()["choices"][0]["message"]["content"])

    headline = (obj.get("headline_ja") or "").strip()
    article_md = (obj.get("article_md") or "").strip()
    cands = obj.get("mike_candidates") or []
    tags = sanitize_tags(obj.get("tags"))

    # ミケ候補→未使用を選抜（あなたの既存ロジックを流用）
    picked = None; picked_norm = None
    for raw in cands:
        if not isinstance(raw, str): continue
        line = re.sub(r"\s+", " ", raw.strip())
        if not line: continue
        if not line.endswith("にゃ"):
            line = (line.rstrip("。.!?、，") + "にゃ").strip()
        normed = _norm_mike(line)
        if normed and normed not in seen_mike_normed:
            picked, picked_norm = line, normed
            break
    if not picked and cands:
        line = re.sub(r"\s+", " ", str(cands[0]).strip())
        if not line.endswith("にゃ"):
            line = (line.rstrip("。.!?、，") + "にゃ").strip()
        picked = line
        picked_norm = _norm_mike(line)

    return headline, article_md, picked, picked_norm, tags



def gen_image_png(title, extra_hint_tags=None):
    # タグからキーワードを作る（最大4語）
    kw = ""
    if extra_hint_tags:
        kw = "、".join(extra_hint_tags[:4])
    if not kw:
        kw = "芸能ニュース, イベント, ステージ, インタビュー"

    prompt = IMG_PROMPT_TEMPLATE.format(
        title=title,
        keywords=kw,
        size_hint="1536x1024 推奨"
    )

    payload = {
        "model": IMG_MODEL,
        "prompt": prompt,
        "size": "1536x1024",  # 許可サイズ
        "n": 1,
    }
    r = post_openai(f"{OPENAI_BASE}/images/generations", payload, timeout=120)
    data = r.json().get("data", [{}])[0]
    if "b64_json" in data:
        return base64.b64decode(data["b64_json"])
    if "url" in data:
        img = requests.get(data["url"], timeout=60)
        img.raise_for_status()
        return img.content
    raise RuntimeError("No image data in response")

# ====== Front Matter ======
def build_front_matter(title, date_iso, publish_iso, link, description_text, include_cover, extra_tags, source_title=None):
    sanitized_title = title.replace('"', "'")
    desc = (description_text or sanitized_title).replace('"', "'").strip()[:150]

    base_tags = ["AI記事", "Entertainment"]
    for t in extra_tags or []:
        if t not in base_tags:
            base_tags.append(t)

    def yq(x): return '"' + str(x).replace('"', "'") + '"'
    tags_line = "tags: [" + ",".join(yq(t) for t in base_tags) + "]"

    fm = [
        "---",
        f'title: "{sanitized_title}"',
        f"date: {date_iso}",
        f"publishDate: {publish_iso}",
        f"lastmod: {publish_iso}",
        "draft: false",
        f'categories: ["{CATEGORY}"]',
        tags_line,
        f'canonicalURL: "{link}"',
    ]
    if source_title:
        safe_source_title = source_title.replace('"', "'")
        fm.append(f'sourceTitle: "{safe_source_title}"')

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
    seen_mike_normed = load_seen_mike() 
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
            # 1) 本文 + ミケ候補→ユニーク選抜 + タグ を生成
            headline, article_md, mike_comment, mike_norm, extra_tags = gen_article_comment_tags(
            title, link, rss_summary, seen_mike_normed
            )       

            # 2) 画像生成（必要ならスキップ可）
            has_image = False
            if not SKIP_IMAGE:
                try:
                    img_bytes = gen_image_png(headline or title, extra_hint_tags=extra_tags)
                    with open(post_dir / "featured.png", "wb") as f:
                        f.write(img_bytes)
                    has_image = True
                except Exception as img_ex:
                    print(f"[WARN] image generation failed: {img_ex}")

            # 3) Front Matter + 本文の index.md 出力
            fm = build_front_matter(
                headline or title,   # ← 表示タイトルはオリジナル優先
                date_iso, publish_iso, link,
                mike_comment, include_cover=has_image, extra_tags=extra_tags,
                source_title=title   # ← 追加引数
            )
            body = fm + article_md + "\n\n---\n参考リンク: " + link + "\n"
            with open(post_dir / "index.md", "w", encoding="utf-8") as f:
                f.write(body)

            # 4) 既読登録
            seen.add(eid)
            if mike_norm:
                seen_mike_normed.add(mike_norm)   # ★追加
            created += 1
            print(f"[OK] created: {post_dir}")

            time.sleep(2)  # 保険

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
    save_seen_mike(seen_mike_normed) 
    print(f"Checked entries: {checked}")
    print(f"Created posts: {created}")

if __name__ == "__main__":
    main()
