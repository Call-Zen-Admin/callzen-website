import feedparser
import json

feeds = {
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

def fetch_news(feed_list, count):
    articles = []
    seen_titles = set()

    for url in feed_list:
        feed = feedparser.parse(url)

        for entry in feed.entries:
            if len(articles) >= count:
                break

            title = entry.title.strip()

            if title in seen_titles:
                continue

            seen_titles.add(title)

            summary = entry.get("summary", "")
            clean_summary = summary[:180] + "..."

            articles.append({
                "title": title,
                "summary": clean_summary,
                "link": entry.link
            })

    return articles[:count]

data = {
    "international": fetch_news(feeds["international"], 3),
    "australia": fetch_news(feeds["australia"], 3),
    "india": fetch_news(feeds["india"], 4)
}

# Save JSON
with open("news.json", "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)