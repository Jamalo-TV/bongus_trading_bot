import json
import unittest
from unittest.mock import MagicMock, mock_open, patch

from bongus.strategies import sentiment_scraper


class TestSentimentScraper(unittest.TestCase):
    @patch('bongus.strategies.sentiment_scraper.feedparser.parse')
    def test_fetch_crypto_headlines(self, mock_parse):
        # Mock feedparser response
        mock_feed = MagicMock()
        mock_entry = MagicMock()
        mock_entry.title = "Test headline"
        mock_feed.entries = [mock_entry]
        mock_parse.return_value = mock_feed

        headlines = sentiment_scraper.fetch_crypto_headlines(5)
        self.assertIsInstance(headlines, list)
        self.assertGreater(len(headlines), 0)
        self.assertIsInstance(headlines[0], str)

    def test_get_sentiment_from_ai(self):
        # Use mock headlines to avoid API call limits
        headlines = ["Bitcoin surges to new high", "Crypto market sees correction"]
        with patch('bongus.strategies.sentiment_scraper.requests.post') as mock_post:
            # Mock successful response
            mock_post.return_value.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": "0.5"}]}}]
            }
            score = sentiment_scraper.get_sentiment_from_ai(headlines)
            self.assertIsInstance(score, float)
            self.assertEqual(score, 0.5)

    @patch('bongus.strategies.sentiment_scraper.requests.post')
    def test_get_sentiment_from_ai_error(self, mock_post):
        mock_post.return_value.raise_for_status.side_effect = Exception("API Error")
        headlines = ["Market crashes"]
        score = sentiment_scraper.get_sentiment_from_ai(headlines)
        self.assertIsInstance(score, float)
        self.assertEqual(score, 0.0)

    @patch('bongus.strategies.sentiment_scraper.get_sentiment_from_ai')
    @patch('bongus.strategies.sentiment_scraper.fetch_crypto_headlines')
    @patch('builtins.open', new_callable=mock_open)
    def test_update_sentiment_file(self, mock_file, mock_fetch, mock_get_sentiment):
        mock_fetch.return_value = ["Test headline"]
        mock_get_sentiment.return_value = 0.5
        sentiment_scraper.update_sentiment_file()

        # Verify file was opened correctly
        mock_file.assert_called_with(sentiment_scraper._SENTIMENT_FILE, "w", encoding="utf-8")

        # Verify JSON was written correctly
        # Extract the written data from the mock
        written_data = "".join(call.args[0] for call in mock_file().write.call_args_list)
        data = json.loads(written_data)

        self.assertIn("timestamp", data)
        self.assertEqual(data["sentiment_score"], 0.5)

if __name__ == "__main__":
    unittest.main()
