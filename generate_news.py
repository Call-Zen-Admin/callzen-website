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
    {"channel_name": "Sunday Sarthak", "channel_id": "UC5fcjujOsqD-126Chn_BAuA"},
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

def is_youtube_short(video_id):
    """
    Checks if a video is a YouTube Short by calling the oEmbed endpoint
    and checking if YouTube redirects /shorts/ URL — or by inspecting
    the video page for the shorts marker.
    Returns True if it's a Short, False if it's a regular video.
    """
    try:
        # YouTube returns a 200 for shorts at /shorts/<id> and
        # redirects regular videos away from that path.
        # We use a HEAD request to check — if it stays at /shorts/ it's a Short.
        shorts_url = f"https://www.youtube.com/shorts/{video_id}"
        req = urllib.request.Request(
            shorts_url,
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        # Don't follow redirects so we can inspect the response URL
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        # Temporarily override to NOT follow redirects
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        no_redirect_opener = urllib.request.build_opener(NoRedirect)
        try:
            no_redirect_opener.open(req, timeout=8)
            # If we get a response without redirect, it's a Short
            return True
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                # Redirected away from /shorts/ — it's a regular video
                return False
            return True  # other error, assume short to be safe
    except Exception:
        # On any failure, assume not a short so we don't skip valid videos
        return False


def fetch_latest_youtube_video(channel_id, channel_name):
    """
    Fetches the latest FULL video (non-Short) from a YouTube channel
    using its public Atom RSS feed. No API key required.
    Loops through up to 15 recent entries to skip any Shorts.
    Returns a dict with video_id and title, or None values on failure.
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()

        NS = {
            "atom":  "http://www.w3.org/2005/Atom",
            "yt":    "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }
        root = ET.fromstring(xml_data)
        entries = root.findall("atom:entry", NS)  # all recent videos

        if not entries:
            raise ValueError("No entries in feed")

        for entry in entries[:15]:  # check up to 15 recent uploads
            video_id = entry.find("yt:videoId", NS).text
            title    = entry.find("atom:title",  NS).text

            # Skip if title explicitly flags it as a Short
            title_lower = (title or "").lower()
            if "#shorts" in title_lower or "#short" in title_lower:
                print(f"  ↷ {channel_name}: skipping Short (title flag) — {title}")
                continue

            # Skip if YouTube confirms it's a Short via URL check
            if is_youtube_short(video_id):
                print(f"  ↷ {channel_name}: skipping Short (URL check) — {title}")
                continue

            # This is a regular video
            print(f"  ✓ {channel_name}: {title}")
            return {
                "channel_name": channel_name,
                "channel_id":   channel_id,
                "video_id":     video_id,
                "title":        title,
            }

        # All recent entries were Shorts — fall back to the very first one
        print(f"  ⚠ {channel_name}: all recent videos are Shorts, using latest anyway")
        entry    = entries[0]
        video_id = entry.find("yt:videoId", NS).text
        title    = entry.find("atom:title",  NS).text
        return {
            "channel_name": channel_name,
            "channel_id":   channel_id,
            "video_id":     video_id,
            "title":        title,
        }

    except Exception as e:
        print(f"  ⚠ {channel_name}: failed — {e}")
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