from __future__ import annotations

import io
import json
import urllib.error

import pytest

from lumen_marketing_os.adapters.research import (
    HackerNewsClient, HttpClient, SearchClient, SourceError, parse_feed, plain_text,
)
from lumen_marketing_os.workflows.research.agents import parse_reddit_html


class Response:
    def __init__(self, body: bytes):
        self.body = io.BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.body.close()

    def read(self, n):
        return self.body.read(n)


def test_feed_and_html_extraction():
    rss = b"<rss><channel><item><title>Business data gap</title><link>https://example.com/1</link><description><![CDATA[<p>Missing contacts</p>]]></description><guid>x1</guid></item></channel></rss>"
    items = parse_feed(rss, source_key="x", source_kind="rss")
    assert len(items) == 1
    assert items[0].excerpt == "Missing contacts"
    assert plain_text("<script>secret</script><p>Useful&nbsp;data</p>") == "Useful data"
    with pytest.raises(SourceError):
        parse_feed(b"<!DOCTYPE rss><rss/>", source_key="x", source_kind="rss")


def test_reddit_public_html_parser_deduplicates():
    html = b'<a href="/r/SaaS/comments/abc/help/">Need accurate company data</a><a href="/r/SaaS/comments/abc/help/">Need accurate company data</a>'
    items = parse_reddit_html(html, community="SaaS", limit=10)
    assert len(items) == 1
    assert items[0].url == "https://www.reddit.com/r/SaaS/comments/abc/help/"


def test_brave_rotates_across_three_keys_only_on_explicit_quota_limit():
    calls = []
    def opener(request, timeout):
        calls.append(request)
        if request.get_header("X-subscription-token") in {"one", "two"}:
            raise urllib.error.HTTPError(request.full_url, 429, "quota", {},
                                         io.BytesIO(b'{"error":{"code":"QUOTA_LIMITED"}}'))
        return Response(b'{"web":{"results":[{"url":"https://example.com/a","title":"A","description":"B"}]}}')
    http = HttpClient(user_agent="test", opener=opener, host_interval=0)
    search = SearchClient(http, brave_keys=["one", "two", "three"])
    result = search.search("business data")
    assert result[0].title == "A"
    assert search.last_provider == "brave-3"
    assert [r.get_header("X-subscription-token") for r in calls] == ["one", "two", "three"]
    assert all(r.full_url.startswith("https://api.search.brave.com/") for r in calls)
    assert search.search("another business data question")[0].title == "A"
    assert [r.get_header("X-subscription-token") for r in calls] == ["one", "two", "three", "three"]

    def rate_opener(request, timeout):
        rate_calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, "rate", {},
                                     io.BytesIO(b'{"error":{"code":"RATE_LIMITED"}}'))
    rate_calls = []
    http = HttpClient(user_agent="test", opener=rate_opener, host_interval=0)
    search = SearchClient(http, brave_keys=["one", "two", "three"])
    with pytest.raises(SourceError, match="search_unavailable:rate_limited"):
        search.search("business data")
    assert search.last_provider is None
    assert len(rate_calls) == 1


def test_brave_missing_slot_and_exhausted_keys_fail_closed():
    calls = []
    def opener(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 429, "quota", {},
                                     io.BytesIO(b'{"error":{"code":"QUOTA_LIMITED"}}'))
    search = SearchClient(HttpClient(user_agent="test", opener=opener, host_interval=0),
                          brave_keys=["", "two", "three"])
    with pytest.raises(SourceError, match="search_unavailable:quota_limited,quota_limited"):
        search.search("business data")
    assert [r.get_header("X-subscription-token") for r in calls] == ["two", "three"]
    assert SearchClient(HttpClient(user_agent="test", opener=opener), brave_keys=[]).last_provider is None
    with pytest.raises(SourceError, match="search_unavailable:not_configured"):
        SearchClient(HttpClient(user_agent="test", opener=opener), brave_keys=[]).search("business data")
    assert len(calls) == 2


def test_http_bounds_and_hn_items():
    def opener(request, timeout):
        if "askstories" in request.full_url:
            return Response(b"[1]")
        if "/item/1" in request.full_url:
            return Response(json.dumps({"id": 1, "title": "Ask HN: Lead data",
                                        "text": "<p>Outdated records</p>"}).encode())
        return Response(b"x" * 20)
    http = HttpClient(user_agent="test", opener=opener, host_interval=0, max_bytes=15)
    with pytest.raises(SourceError, match="response_too_large"):
        http.get("https://example.com/large")
    http = HttpClient(user_agent="test", opener=opener, host_interval=0)
    stories = HackerNewsClient(http).stories("askstories", limit=1)
    assert stories[0].url == "https://news.ycombinator.com/item?id=1"
    assert stories[0].excerpt == "Outdated records"


def test_hn_item_context_is_bounded_and_discards_deleted_comments():
    requested = []
    values = {
        42: {"id": 42, "type": "story", "title": "Ask HN: Data?", "text": "<p>Question</p>",
             "kids": [43, 44, 45], "time": 100, "descendants": 3},
        43: {"id": 43, "type": "comment", "text": "<p>Useful answer</p>", "by": "reader"},
        44: {"id": 44, "type": "comment", "deleted": True},
        45: {"id": 45, "type": "comment", "text": "Should not be fetched"},
    }

    def opener(request, timeout):
        item_id = int(request.full_url.split("/item/")[1].split(".json")[0])
        requested.append(item_id)
        return Response(json.dumps(values[item_id]).encode())

    hn = HackerNewsClient(HttpClient(user_agent="test", opener=opener, host_interval=0))
    item = hn.item_context(42, max_comments=2)
    assert requested == [42, 43, 44]
    assert item["url"] == "https://news.ycombinator.com/item?id=42"
    assert item["text"] == "Question"
    assert [comment["id"] for comment in item["top_comments"]] == [43]
    with pytest.raises(ValueError):
        hn.item_context(42, max_comments=6)


def test_hn_comment_context_includes_parent():
    values = {
        8: {"id": 8, "type": "comment", "parent": 7, "text": "<p>Reply</p>"},
        7: {"id": 7, "type": "story", "title": "Parent story"},
    }

    def opener(request, timeout):
        item_id = int(request.full_url.split("/item/")[1].split(".json")[0])
        return Response(json.dumps(values[item_id]).encode())

    hn = HackerNewsClient(HttpClient(user_agent="test", opener=opener, host_interval=0))
    item = hn.item_context(8, max_comments=0)
    assert item["parent"]["title"] == "Parent story"
    assert item["text"] == "Reply"
