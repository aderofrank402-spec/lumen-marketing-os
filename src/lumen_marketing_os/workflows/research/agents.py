"""The four read-only Department 1 collectors.

These collectors prepare evidence for Codex review; they do not assert that a
market pain point is validated or publish/send anything externally.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from urllib.parse import urljoin

from lumen_marketing_os.adapters.research import (
    HackerNewsClient, HttpClient, SearchClient, SourceError, SourceItem,
    page_item, parse_feed, plain_text,
)
from lumen_marketing_os.workflows.research.store import FindingDraft, SourceResult, _domain


DIRECTORY_RELEVANCE_TERMS = (
    "enrich", "companies api", "company api", "crm data", "lead generation", "firmographic",
)


def directory_relevant(title: str, excerpt: str, keywords: tuple[str, ...]) -> bool:
    searchable = f"{title} {excerpt}".lower()
    return any(term.lower() in searchable for term in keywords + DIRECTORY_RELEVANCE_TERMS)


@dataclass(frozen=True)
class ResearchConfig:
    queries: tuple[str, ...]
    feeds: tuple[dict[str, str], ...]
    reddit_communities: tuple[str, ...]
    competitors: tuple[dict[str, str], ...]
    directory_sources: tuple[str, ...]
    keywords: tuple[str, ...]
    max_items: int
    directory_queries: tuple[str, ...] = ()


class _RedditLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.href:
            label = " ".join(self.parts).strip()
            if "/comments/" in self.href and len(label) >= 12:
                self.links.append((self.href, label))
            self.href = None
            self.parts = []


def parse_reddit_html(payload: bytes, *, community: str, limit: int) -> list[SourceItem]:
    parser = _RedditLinkParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    seen: set[str] = set()
    items: list[SourceItem] = []
    for href, label in parser.links:
        url = urljoin("https://www.reddit.com", href)
        if url in seen or not url.startswith("https://www.reddit.com/r/"):
            continue
        seen.add(url)
        items.append(SourceItem(f"reddit-{community}", "reddit_public", None,
                                url, plain_text(label)[:500], ""))
        if len(items) >= limit:
            break
    return items


class DepartmentAgents:
    def __init__(self, *, http: HttpClient, search: SearchClient,
                 hacker_news: HackerNewsClient, config: ResearchConfig) -> None:
        self.http = http
        self.search = search
        self.hacker_news = hacker_news
        self.config = config

    def collect(self, agent_id: str) -> list[SourceResult]:
        return {"1A": self._trends, "1B": self._forums,
                "1C": self._competitors, "1E": self._directories}[agent_id]()

    def _attempt(self, key: str, kind: str, fetch) -> SourceResult:
        try:
            return SourceResult(key, kind, tuple(fetch()))
        except SourceError as exc:
            return SourceResult(key, kind, error_class=exc.error_class)
        except (KeyError, TypeError, ValueError):
            return SourceResult(key, kind, error_class="invalid_source_response")

    def _feed(self, key: str, url: str, kind: str = "rss") -> SourceResult:
        return self._attempt(key, kind, lambda: parse_feed(
            self.http.get(url), source_key=key, source_kind=kind,
            limit=self.config.max_items))

    def _trends(self) -> list[SourceResult]:
        results = [self._feed(f"1A:{feed['id']}", feed["url"]) for feed in self.config.feeds]
        for index, query in enumerate(self.config.queries, 1):
            key = f"1A:search:{index}"
            results.append(self._attempt(key, "search", lambda q=query, k=key: [
                replace(item, source_key=k) for item in self.search.search(
                    q, limit=self.config.max_items)]))
        return results

    def _forums(self) -> list[SourceResult]:
        results = [self._attempt("1B:hn-ask", "hacker_news", lambda:
                                 self.hacker_news.stories("askstories", limit=self.config.max_items))]
        for index, query in enumerate((
            'site:reddit.com/r/SaaS "company data"',
            'site:reddit.com/r/sales "lead data"',
            'site:news.ycombinator.com "data enrichment"',
        ), 1):
            key = f"1B:search:{index}"
            results.append(self._attempt(key, "forum_search", lambda q=query, k=key: [
                replace(item, source_key=k, source_kind="forum_search")
                for item in self.search.search(q, limit=self.config.max_items)]))
        reddit_cooling_down = False
        for community in self.config.reddit_communities:
            key = f"1B:reddit:{community.lower()}"
            if reddit_cooling_down:
                results.append(SourceResult(key, "reddit_public", error_class="rate_limited_cooldown"))
                continue
            feed_url = f"https://www.reddit.com/r/{community}/new/.rss"
            public_url = f"https://www.reddit.com/r/{community}/new/"
            def read_reddit(k=key, c=community, f=feed_url, p=public_url):
                try:
                    return parse_feed(self.http.get(f), source_key=k,
                                      source_kind="reddit_public", limit=self.config.max_items)
                except SourceError as first:
                    if first.error_class in {"rate_limited", "quota_limited", "access_denied"}:
                        raise
                    return parse_reddit_html(self.http.get(p), community=c,
                                             limit=self.config.max_items)
            result = self._attempt(key, "reddit_public", read_reddit)
            results.append(result)
            if result.error_class in {"rate_limited", "quota_limited"}:
                reddit_cooling_down = True
        return results

    def _competitors(self) -> list[SourceResult]:
        return [self._attempt(f"1C:{site['id']}", "competitor_page",
                              lambda s=site: [page_item(self.http, source_key=f"1C:{s['id']}",
                                                       url=s["url"])])
                for site in self.config.competitors]

    def _directories(self) -> list[SourceResult]:
        results: list[SourceResult] = []
        feeds_by_id = {feed["id"]: feed for feed in self.config.feeds}
        for source in self.config.directory_sources:
            if source == "hacker-news-show":
                results.append(self._attempt("1E:hn-show", "hacker_news", lambda:
                                             self.hacker_news.stories("showstories",
                                                                      limit=self.config.max_items)))
            elif source in feeds_by_id:
                feed = feeds_by_id[source]
                results.append(self._feed(f"1E:{source}", feed["url"], "launch_feed"))
        for index, query in enumerate(self.config.directory_queries, 1):
            key = f"1E:search:{index}"
            results.append(self._attempt(key, "directory_search", lambda q=query, k=key: [
                replace(item, source_key=k, source_kind="directory_search")
                for item in self.search.search(q, limit=self.config.max_items)]))
        return results

    def draft(self, agent_id: str, item: SourceItem, *, previous: str | None) -> FindingDraft | None:
        title = item.title.strip()
        excerpt = item.excerpt.strip()
        searchable = f"{title} {excerpt}".lower()
        relevant = any(term.lower() in searchable for term in self.config.keywords)
        if agent_id == "1A":
            if not relevant:
                return None
            return FindingDraft("use_case_signal", title,
                                "Candidate market signal; requires Codex review, buyer identification and counter-evidence.",
                                excerpt or title, metadata={"published_at": item.published_at,
                                                           "source_key": item.source_key})
        if agent_id == "1B":
            if not relevant:
                return None
            return FindingDraft("forum_discussion", title,
                                "Relevant public discussion for review; no reply has been drafted or posted.",
                                excerpt or title, metadata={"published_at": item.published_at,
                                                           "source_key": item.source_key})
        if agent_id == "1C":
            if previous is not None and previous == excerpt:
                return None
            kind = "competitor_change" if previous is not None else "competitor_baseline"
            summary = ("Public page changed since the prior snapshot; inspect the evidence before describing the change."
                       if previous is not None else "Initial public-page baseline; no change is asserted.")
            return FindingDraft(kind, title, summary, excerpt[:3000],
                                metadata={"source_key": item.source_key,
                                          "previous_excerpt": previous[:3000] if previous else None})
        if agent_id == "1E":
            # A launch feed is not itself evidence of relevance. Without this gate,
            # every unrelated Show HN and startup headline becomes a review job.
            if not directory_relevant(title, excerpt, self.config.keywords):
                return None
            if (item.source_kind != "hacker_news" or not title.lower().startswith("show hn:")
                    or not _domain(item.subject_url)):
                return FindingDraft("launch_signal", title,
                                    "Launch or directory item; organization identity has not been resolved.",
                                    excerpt or title,
                                    metadata={"published_at": item.published_at,
                                              "source_key": item.source_key})
            name = re.sub(r"^Show HN:\s*", "", title, flags=re.I).split(" - ", 1)[0].strip()
            name = re.split(r"\s+[–—:]\s+", name, maxsplit=1)[0].strip()
            if len(name) < 3:
                return None
            return FindingDraft("organization_candidate", title,
                                "Launch/directory signal only; company identity and buyer fit are unverified.",
                                excerpt or title, subject_name=name[:250],
                                metadata={"subject_url": item.subject_url,
                                          "published_at": item.published_at,
                                          "source_key": item.source_key})
        raise ValueError("unknown agent")
