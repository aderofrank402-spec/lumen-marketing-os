"""Bounded, read-only public-source adapters for Department 1."""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable


class SourceError(Exception):
    def __init__(self, error_class: str, *, retryable: bool = False,
                 status: int | None = None) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class SourceItem:
    source_key: str
    source_kind: str
    external_id: str | None
    url: str
    title: str
    excerpt: str
    subject_url: str | None = None
    published_at: str | None = None


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.suppressed += 1
        elif tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.suppressed:
            self.suppressed -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    return re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()


class HttpClient:
    """One-at-a-time host pacing; no cookies, browser disguise, or unbounded bodies."""

    def __init__(self, *, user_agent: str, timeout: int = 12,
                 max_bytes: int = 2_000_000, host_interval: float = 2.0,
                 opener: Callable[..., Any] | None = None,
                 sleeper: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.host_interval = host_interval
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.monotonic = monotonic
        self._last_by_host: dict[str, float] = {}

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise SourceError("invalid_public_url")
        host = parsed.hostname.lower()
        last = self._last_by_host.get(host)
        if last is not None:
            wait = self.host_interval - (self.monotonic() - last)
            if wait > 0:
                self.sleeper(wait)
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            **(headers or {}),
        })
        self._last_by_host[host] = self.monotonic()
        try:
            with self.opener(request, timeout=self.timeout) as response:
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise SourceError("response_too_large")
                return content
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    body = json.loads(exc.read(4096))
                    code = str(body.get("error", {}).get("code", ""))
                except (ValueError, AttributeError):
                    code = ""
                error_class = "quota_limited" if code == "QUOTA_LIMITED" else "rate_limited"
                raise SourceError(error_class, retryable=True, status=429) from None
            if exc.code in {401, 403}:
                raise SourceError("access_denied", status=exc.code) from None
            if 500 <= exc.code < 600:
                raise SourceError("provider_unavailable", retryable=True, status=exc.code) from None
            raise SourceError("http_error", status=exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceError("network_error", retryable=True) from None

    def json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        try:
            return json.loads(self.get(url, headers=headers))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError("invalid_json") from exc


class SearchClient:
    def __init__(self, http: HttpClient, *, brave_keys: list[str]) -> None:
        self.http = http
        self.brave_keys = tuple(brave_keys)
        self._active_brave_index = 0
        self.last_provider: str | None = None

    def search(self, query: str, *, limit: int = 10) -> list[SourceItem]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query and limit 1..20 required")
        self.last_provider = None
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": limit})
        errors: list[str] = []
        for slot in range(self._active_brave_index, len(self.brave_keys)):
            key = self.brave_keys[slot]
            if not key:
                continue
            try:
                data = self.http.json(url, headers={"X-Subscription-Token": key,
                                                    "Accept": "application/json"})
                self.last_provider = f"brave-{slot + 1}"
                return [SourceItem("search", "search", None, item["url"],
                                   plain_text(item.get("title", "")),
                                   plain_text(item.get("description", "")))
                        for item in data.get("web", {}).get("results", [])[:limit]
                        if item.get("url")]
            except SourceError as exc:
                errors.append(exc.error_class)
                # A quota limit may be key-specific. Temporary rate limiting is not
                # a reason to rotate credentials and increase request pressure.
                if exc.error_class == "quota_limited":
                    self._active_brave_index = slot + 1
                    continue
                break
        raise SourceError("search_unavailable:" + ",".join(errors or ["not_configured"]),
                          retryable=True)

def parse_feed(payload: bytes, *, source_key: str, source_kind: str,
               limit: int = 15) -> list[SourceItem]:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise SourceError("unsafe_xml")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SourceError("invalid_feed") from exc
    items: list[SourceItem] = []
    atom = root.tag.endswith("feed")
    nodes = list(root) if atom else root.findall("./channel/item")
    for node in nodes:
        if len(items) >= limit:
            break
        if atom and not node.tag.endswith("entry"):
            continue
        def get(name: str) -> str:
            child = next((c for c in node if c.tag.rsplit("}", 1)[-1] == name), None)
            return (child.text or "").strip() if child is not None else ""
        link = get("link")
        if atom:
            link_node = next((c for c in node if c.tag.rsplit("}", 1)[-1] == "link"
                              and c.attrib.get("rel", "alternate") == "alternate"), None)
            link = link_node.attrib.get("href", "") if link_node is not None else link
        title = plain_text(get("title"))
        excerpt = plain_text(get("description") or get("summary") or get("content"))
        if not link or not title:
            continue
        items.append(SourceItem(source_key, source_kind, get("guid") or get("id") or None,
                                link, title, excerpt[:3000], published_at=get("pubDate") or get("updated") or None))
    return items


class HackerNewsClient:
    BASE = "https://hacker-news.firebaseio.com/v0"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def stories(self, group: str, *, limit: int = 15) -> list[SourceItem]:
        if group not in {"askstories", "showstories", "topstories"}:
            raise ValueError("unsupported HN group")
        if not 1 <= limit <= 50:
            raise ValueError("HN story limit must be 1..50")
        ids = self.http.json(f"{self.BASE}/{group}.json")
        if not isinstance(ids, list):
            raise SourceError("invalid_hn_response")
        items: list[SourceItem] = []
        for item_id in ids[:limit]:
            data = self.http.json(f"{self.BASE}/item/{int(item_id)}.json")
            if not isinstance(data, dict) or data.get("dead") or data.get("deleted"):
                continue
            title = plain_text(data.get("title", ""))
            if not title:
                continue
            canonical = f"https://news.ycombinator.com/item?id={int(item_id)}"
            items.append(SourceItem(f"hn-{group}", "hacker_news", str(item_id), canonical,
                                    title, plain_text(data.get("text", ""))[:3000],
                                    subject_url=data.get("url"),
                                    published_at=str(data.get("time", "")) or None))
        return items

    def item_context(self, item_id: int, *, max_comments: int = 3) -> dict[str, Any]:
        """Fetch one public item and a bounded sample of its direct comments."""
        if item_id <= 0 or not 0 <= max_comments <= 5:
            raise ValueError("positive HN item ID and comment limit 0..5 required")
        data = self.http.json(f"{self.BASE}/item/{item_id}.json")
        if (not isinstance(data, dict) or data.get("id") != item_id
                or data.get("deleted") or data.get("dead")):
            raise SourceError("hn_item_unavailable")
        if data.get("type") not in {"story", "comment"}:
            raise SourceError("hn_item_unsupported")

        def summary(value: dict[str, Any]) -> dict[str, Any]:
            ident = value["id"]
            return {
                "id": ident,
                "url": f"https://news.ycombinator.com/item?id={ident}",
                "type": value.get("type"),
                "title": plain_text(str(value.get("title") or ""))[:500],
                "text": plain_text(str(value.get("text") or ""))[:3000],
                "author": value.get("by"),
                "created_at_unix": value.get("time"),
                "comment_count": value.get("descendants"),
            }

        result = summary(data)
        parent_id = data.get("parent")
        if data.get("type") == "comment" and isinstance(parent_id, int) and parent_id > 0:
            parent = self.http.json(f"{self.BASE}/item/{parent_id}.json")
            result["parent"] = (summary(parent) if isinstance(parent, dict)
                                and parent.get("id") == parent_id
                                and not parent.get("deleted") and not parent.get("dead") else None)
        comments: list[dict[str, Any]] = []
        kids = data.get("kids")
        for child_id in (kids if isinstance(kids, list) else [])[:max_comments]:
            if not isinstance(child_id, int) or child_id <= 0:
                continue
            child = self.http.json(f"{self.BASE}/item/{child_id}.json")
            if (isinstance(child, dict) and child.get("id") == child_id
                    and child.get("type") == "comment"
                    and not child.get("deleted") and not child.get("dead")):
                comments.append(summary(child))
        result["top_comments"] = comments
        return result


def page_item(http: HttpClient, *, source_key: str, url: str) -> SourceItem:
    payload = http.get(url)
    text = plain_text(payload.decode("utf-8", errors="replace"))
    if not text:
        raise SourceError("empty_page")
    return SourceItem(source_key, "competitor_page", None, url, source_key,
                      text[:20_000], subject_url=url)
