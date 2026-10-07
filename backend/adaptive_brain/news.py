from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import aiohttp

from .schema import EvidenceItem, SourceType


@dataclass(frozen=True)
class RSSFeed:
    source_id: str
    url: str
    topic: str = "market-news"
    confidence: float = 0.5


class RSSNewsPoller:
    """Generic read-only RSS/Atom fetcher for explicitly configured feeds."""

    def __init__(self, feeds: list[RSSFeed], timeout_seconds: float = 15.0):
        self.feeds = feeds
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _text(node, names: tuple[str, ...]) -> str:
        for name in names:
            child = node.find(name)
            if child is not None and child.text:
                return child.text.strip()
        return ""

    @staticmethod
    def parse_xml(feed: RSSFeed, xml_text: str) -> list[EvidenceItem]:
        root = ET.fromstring(xml_text)
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

        out: list[EvidenceItem] = []
        for item in items:
            title = RSSNewsPoller._text(
                item, ("title", "{http://www.w3.org/2005/Atom}title")
            )
            body = RSSNewsPoller._text(
                item,
                (
                    "description",
                    "content",
                    "{http://www.w3.org/2005/Atom}summary",
                    "{http://www.w3.org/2005/Atom}content",
                ),
            )
            link = RSSNewsPoller._text(item, ("link",))
            if not link:
                atom_link = item.find("{http://www.w3.org/2005/Atom}link")
                if atom_link is not None:
                    link = atom_link.attrib.get("href", "")
            published = RSSNewsPoller._text(
                item,
                (
                    "pubDate",
                    "published",
                    "{http://www.w3.org/2005/Atom}published",
                    "{http://www.w3.org/2005/Atom}updated",
                ),
            )
            if published:
                try:
                    published = parsedate_to_datetime(published).isoformat()
                except Exception:
                    pass
            if not title and not body:
                continue
            out.append(
                EvidenceItem(
                    source_id=feed.source_id,
                    source_type=SourceType.NEWS,
                    topic=feed.topic,
                    title=title or body[:160],
                    body=body,
                    url=link,
                    published_at=published or None,
                    confidence=feed.confidence,
                    metadata={"feed_url": feed.url},
                )
            )
        return out

    async def poll(self) -> list[EvidenceItem]:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        out: list[EvidenceItem] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for feed in self.feeds:
                async with session.get(feed.url) as response:
                    response.raise_for_status()
                    out.extend(self.parse_xml(feed, await response.text()))
        return out
