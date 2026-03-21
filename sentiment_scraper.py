import time
import json
import requests
import feedparser
import os
from dotenv import load_dotenv

load_dotenv()

# Free Gemini API or Groq API key needs to be set in environment
# Using Gemini for this example:
API_KEY = os.getenv("GEMINI_API_KEY", "") 
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={API_KEY}"

RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed"
]

def fetch_crypto_headlines(max_total=15):
    headlines = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                headlines.append(entry.title)
                if len(headlines) >= max_total:
                    return headlines
        except Exception as e:
            print(f"Error fetching from {feed_url}: {e}")
    return headlines[:max_total]

def get_sentiment_from_ai(headlines):
    if not headlines:
        return 0.0
    
    text_data = "\n".join(f"- {h}" for h in headlines)
    prompt = (
        "You are a quantitative analyst. Read the following crypto headlines. "
        "Reply ONLY with a single float number between -1.0 (extreme bearish fear) "
        "and 1.0 (extreme bullish greed).\n\nHeadlines:\n" + text_data
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    
    try:
        response = requests.post(API_URL, json=payload, headers={"Content-Type": "application/json"})
        response.raise_for_status()
        
        # Parse Gemini API response
        data = response.json()
        result_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        
        # Try to parse float
        return float(result_text)
    except Exception as e:
        print(f"Error querying AI API: {e}")
        return 0.0

def update_sentiment_file():
    headlines = fetch_crypto_headlines(15)
    score = get_sentiment_from_ai(headlines)
    
    output = {
        "timestamp": time.time(),
        "sentiment_score": score
    }
    
    with open("current_sentiment.json", "w") as f:
        json.dump(output, f)
    
    print(f"[{time.ctime()}] Updated sentiment score: {score}")

def run_scraper_loop():
    print("Starting Free Sentiment Scraper...")
    while True:
        update_sentiment_file()
        # Wait 4 hours (14400 seconds)
        time.sleep(14400)

if __name__ == "__main__":
    run_scraper_loop()
