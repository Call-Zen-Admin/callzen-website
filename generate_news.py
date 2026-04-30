import feedparser
import json
import requests
from bs4 import BeautifulSoup
from readability import Document
import subprocess
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET

# ---------------------------
# CONFIG
# ---------------------------

FEEDS = {
    "international": [
        "http://feeds.bbci.co.uk/news/world/rss.xml"
    ],
    "australia": [
        "https://www.abc.net.au/news/feed/51120/rss.xml"
    ],
    "india": [
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms"
    ]
}

ARTICLES_PER_CATEGORY = {
    "international": 3,
    "australia": 3,
    "india": 4
}

# ── YouTube channels to fetch latest video from ──
YOUTUBE_CHANNELS = [
    {"channel_name": "The Deshbhakt",  "channel_id": "UCmTM_hPCeckqN3cPWtYZZcg"},
    {"channel_name": "Sunday Sarthak", "channel_id": "UC_hVYmNLOBCToJBl9IJFFNQ"},
    {"channel_name": "Mohak Mangal",   "channel_id": "UCz4a7agVFr1TxU-mpAP8hkw"},
    {"channel_name": "Abhi and Niyu",  "channel_id": "UCsDTy8jvHcwMvSZf_JGi-FA"},
    {"channel_name": "Dhruv Rathee",   "channel_id": "UC-CSyyi47VX1lD9zyeABW3w"},
    {"channel_name": "Open Letter",    "channel_id": "UCPJ_UzD4PEC-_vwN32amlIQ"},
]

OUTPUT_FILE = "news.json"

USE_OLLAMA = shutil.which("ollama") is not None

# ---------------------------
# AI SUMMARIZATION
# ---------------------------

def summarize_with_ollama(text):
    prompt = (
        "Summarize the following news article into 4 concise bullet points. "
        "Be factual and neutral:\n\n"
        f"{text}"
    )
    result = subprocess.run(
        ["ollama", "run", "llama3", prompt],
        capture_output=True,
        text=True
    )
    return result.stdout.strip()

def summarize_with_hf(text):
    from transformers import pipeline

    summarizer = pipeline(
        task="text-generation",
        model="facebook/bart-large-cnn"
    )

    chunks = [text[i:i+1000] for i in range(0, len(text), 1000)]
    summaries = []

    for chunk in chunks[:2]:
        result = summarizer(
            "Summarize this news article:\n" + chunk,
            max_length=150,
            do_sample=False
        )
        summaries.append(result[0]["generated_text"])

    return " ".join(summaries)

def summarize(text):
    if USE_OLLAMA:
        return summarize_with_ollama(text)
    return summarize_with_hf(text)

# ---------------------------
# ARTICLE EXTRACTION
# ---------------------------

def fetch_article_text(url):
    try:
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0"
        })
        doc = Document(r.text)
        html = doc.summary()
        soup = BeautifulSoup(html, "html.parser")

        # Remove junk
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        text = "\n".join(
            p.get_text(strip=True)
            for p in soup.find_all("p")
            if len(p.get_text(strip=True)) > 40
        )

        return text[:5000]
    except Exception:
        return None

# ---------------------------
# RSS PROCESSING
# ---------------------------

def fetch_news(feed_urls, limit):
    articles = []
    seen_titles = set()

    for feed_url in feed_urls:
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            if len(articles) >= limit:
                break

            title = entry.get("title", "").strip()
            if not title or title in seen_titles:
                continue

            link = entry.get("link")
            article_text = fetch_article_text(link)

            base_text = (
                article_text
                or BeautifulSoup(entry.get("summary", ""), "html.parser").get_text()
            )

            if len(base_text) < 200:
                continue

            ai_summary = summarize(base_text)

            articles.append({
                "title": title,
                "source": feed.feed.get("title", "").split("-")[0].strip(),
                "summary": ai_summary.splitlines()
            })

            seen_titles.add(title)

    return articles

# ---------------------------
# YOUTUBE FETCHING
# ---------------------------

def is_short_by_title(title):
    t = (title or "").lower()
    return "#shorts" in t or "#short" in t or t.strip() == "shorts"


def fetch_latest_youtube_video(channel_id, channel_name):
    """
    Fetches the latest non-Short video using requests + xml.etree.
    Uses requests (already in requirements.txt) to fetch the raw XML,
    avoiding feedparser's URL query-string parsing issues with YouTube RSS.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"
        })
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")

        NS = {
            "atom":  "http://www.w3.org/2005/Atom",
            "yt":    "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        root = ET.fromstring(r.content)
        entries = root.findall("atom:entry", NS)

        if not entries:
            raise ValueError("No entries in feed")

        first_video_id = entries[0].find("yt:videoId", NS).text
        first_title    = entries[0].find("atom:title", NS).text

        for entry in entries[:15]:
            video_id = entry.find("yt:videoId", NS).text
            title    = entry.find("atom:title",  NS).text

            if not video_id:
                continue

            if is_short_by_title(title):
                print(f"  ↷ {channel_name}: skip Short (title) — {title}")
                continue

            # Check duration from media:content — Shorts are ≤ 60s
            media_content = entry.find("media:group/media:content", NS)
            if media_content is None:
                media_content = entry.find("media:content", NS)
            if media_content is not None:
                duration = media_content.get("duration")
                if duration and int(duration) <= 61:
                    print(f"  ↷ {channel_name}: skip Short ({duration}s) — {title}")
                    continue

            print(f"  ✓ {channel_name}: {title}")
            return {
                "channel_name": channel_name,
                "channel_id":   channel_id,
                "video_id":     video_id,
                "title":        title,
            }

        # Fallback — nothing filtered, use very first entry
        print(f"  ⚠ {channel_name}: fallback to first entry")
        return {
            "channel_name": channel_name,
            "channel_id":   channel_id,
            "video_id":     first_video_id,
            "title":        first_title,
        }

    except Exception as e:
        print(f"  ✗ {channel_name}: FAILED — {e}")
        return {
            "channel_name": channel_name,
            "channel_id":   channel_id,
            "video_id":     None,
            "title":        None,
        }

def fetch_all_youtube():
    """Fetch latest video for every channel in YOUTUBE_CHANNELS."""
    print("\nFetching latest YouTube videos...")
    return [
        fetch_latest_youtube_video(ch["channel_id"], ch["channel_name"])
        for ch in YOUTUBE_CHANNELS
    ]

# ---------------------------
# MAIN
# ---------------------------

def main():
    output = {}

    for category, feeds in FEEDS.items():
        print(f"Fetching {category} news...")
        output[category] = fetch_news(
            feeds,
            ARTICLES_PER_CATEGORY.get(category, 3)
        )

    # ── Fetch YouTube latest videos and add to output ──
    output["youtube"] = fetch_all_youtube()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✅ News saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
