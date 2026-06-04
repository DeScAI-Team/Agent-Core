"""Tests for crawl_nitter helpers."""

from crawl_nitter import nitter_handle_from_url, parse_rss_tweets

SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Hello world</title>
      <link>https://nitter.net/BeeARDai/status/12345</link>
      <guid>https://nitter.net/BeeARDai/status/12345</guid>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
      <description>Hello world tweet body</description>
    </item>
    <item>
      <title>Second</title>
      <link>https://nitter.net/BeeARDai/status/67890</link>
      <pubDate>Tue, 02 Jan 2024 12:00:00 GMT</pubDate>
      <description>Second tweet</description>
    </item>
  </channel>
</rss>
"""


def test_nitter_handle_from_profile_url():
    assert nitter_handle_from_url("https://nitter.net/BeeARDai") == "BeeARDai"
    assert nitter_handle_from_url("https://nitter.net/@BeeARDai") == "BeeARDai"


def test_nitter_handle_skips_status_url():
    assert nitter_handle_from_url("https://nitter.net/BeeARDai/status/123") is None


def test_parse_rss_tweets():
    tweets = parse_rss_tweets(SAMPLE_RSS, "https://nitter.net", 20)
    assert len(tweets) == 2
    assert tweets[0]["id"] == "12345"
    assert "Hello" in tweets[0]["text"]
    assert tweets[0]["author"] == "BeeARDai"
