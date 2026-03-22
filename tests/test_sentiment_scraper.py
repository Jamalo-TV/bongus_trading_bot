import unittest
import os
import json
import time
from bongus.strategies import sentiment_scraper

class TestSentimentScraper(unittest.TestCase):
    def test_fetch_crypto_headlines(self):
        headlines = sentiment_scraper.fetch_crypto_headlines(5)
        self.assertIsInstance(headlines, list)
        self.assertGreater(len(headlines), 0)
        self.assertIsInstance(headlines[0], str)

    def test_get_sentiment_from_ai(self):
        # Use mock headlines to avoid API call limits
        headlines = ["Bitcoin surges to new high", "Crypto market sees correction"]
        score = sentiment_scraper.get_sentiment_from_ai(headlines)
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, -1.0)
        self.assertLessEqual(score, 1.0)

    def test_update_sentiment_file(self):
        sentiment_scraper.update_sentiment_file()
        self.assertTrue(os.path.exists("current_sentiment.json"))
        with open("current_sentiment.json", "r") as f:
            data = json.load(f)
        self.assertIn("timestamp", data)
        self.assertIn("sentiment_score", data)
        self.assertIsInstance(data["sentiment_score"], float)

if __name__ == "__main__":
    unittest.main()
