from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from .models import Post, Source


STATUS_PATH = re.compile(r"^/([^/]+)/status/(\d+)/?$")
TRUSTED_X_HOSTS = frozenset({"x.com", "twitter.com"})
TRUSTED_QUOTED_AUTHORS = frozenset({"openai", "openaidevs", "thsottiaux", "sama"})
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_JSON_BODY_BYTES = MAX_ERROR_BODY_BYTES
AUTH_FAILURE = re.compile(
    rb"\bunauthorized\b|\bauthentication\s+failed\b|"
    rb"\bauth[_\s-]?token\b.{0,24}\b(?:expired|invalid)\b|"
    rb"\b(?:login|log\s+in)\s+(?:is\s+)?required\b|"
    rb"\b(?:x|twitter)\b.{0,40}\b(?:session|cookie)\b.{0,40}\bexpired\b",
    re.I | re.S,
)
VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class QuoteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.quote_depth: int | None = None
        self.text: list[str] = []
        self.main_text: list[str] = []
        self.author = ""

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag not in VOID_ELEMENTS:
            self.depth += 1
        values = dict(attrs)
        if tag == "div" and "rsshub-quote" in values.get("class", "").split():
            self.quote_depth = self.depth
        if self.quote_depth is not None and tag == "a":
            try:
                self.author, _post_id = _status_identity(values.get("href", ""))
            except ValueError:
                pass

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_ELEMENTS:
            return
        if self.quote_depth == self.depth:
            self.quote_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        if self.quote_depth is not None:
            self.text.append(value)
        else:
            self.main_text.append(value)


class FeedError(RuntimeError):
    def __init__(self, message: str, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


class RssHubClient:
    def __init__(self, base_url: str, timeout: int, retries: int, count: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.count = count

    def _url(self, source: Source) -> str:
        replies = "1" if source.include_replies else "0"
        route_parameters = (
            f"includeReplies={replies}&includeRts=0&count={self.count}"
            "&readable=1&showQuotedInTitle=0"
        )
        return f"{self.base_url}/twitter/user/{source.handle}/{route_parameters}"

    def fetch(self, source: Source) -> list[Post]:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = Request(
                    self._url(source),
                    headers={
                        "Accept": (
                            "application/json, application/rss+xml, "
                            "application/atom+xml"
                        ),
                        "User-Agent": "codex-quota-monitor/0.1",
                    },
                )
                with urlopen(request, timeout=self.timeout) as response:
                    response_type = _response_type(response)
                    if response_type == "json":
                        body = response.read(MAX_JSON_BODY_BYTES)
                    elif response_type == "xml":
                        body = response.read()
                    else:
                        body = response.read(MAX_ERROR_BODY_BYTES)
                if response_type == "json":
                    payload = json.loads(body)
                    items = _json_items(payload)
                    if items is None:
                        if _has_auth_failure(body):
                            raise FeedError(
                                "X authentication failed", auth_failed=True
                            )
                        raise ValueError("RSSHub returned a structured error response")
                    if not items:
                        raise FeedError(f"empty feed for @{source.handle}")
                    return [self._json_post(source, item) for item in items]
                if response_type != "xml":
                    if _has_auth_failure(body):
                        raise FeedError("X authentication failed", auth_failed=True)
                    raise ValueError("RSSHub returned a structured error response")
                root = ElementTree.fromstring(body)
                entries = self._entries(root)
                if not entries:
                    raise FeedError(f"empty feed for @{source.handle}")
                return [self._post(source, entry) for entry in entries]
            except HTTPError as exc:
                if exc.code in (401, 403):
                    raise FeedError("X authentication failed", auth_failed=True) from exc
                if 500 <= exc.code < 600 and _has_auth_failure(_read_error_body(exc)):
                    raise FeedError("X authentication failed", auth_failed=True) from exc
                last_error = exc
            except FeedError:
                raise
            except (
                URLError,
                TimeoutError,
                OSError,
                HTTPException,
                ElementTree.ParseError,
                ValueError,
            ) as exc:
                last_error = exc
            if attempt + 1 < self.retries:
                time.sleep(2**attempt)
        raise FeedError(f"feed fetch failed for @{source.handle}")

    @staticmethod
    def _entries(root: ElementTree.Element) -> list[ElementTree.Element]:
        if _local_name(root.tag) == "feed":
            return [child for child in root if _local_name(child.tag) == "entry"]
        return root.findall("./channel/item")

    @staticmethod
    def _post(source: Source, entry: ElementTree.Element) -> Post:
        link = _entry_link(entry)
        link_author, post_id = _status_identity(link)
        if link_author.casefold() != source.handle.casefold():
            raise ValueError(
                f"entry author does not match trusted route @{source.handle}: {link}"
            )

        title = _child_text(entry, "title")
        feed_content = _child_text(entry, "description", "content")
        quote = QuoteParser()
        quote.feed(feed_content)
        text = _primary_text(title, quote.main_text)
        text = re.sub(r"^Re\s+", "", text)
        published_at = _published_at(
            _child_text(entry, "pubDate", "published", "updated")
        )
        return Post(
            post_id=post_id,
            author=source.handle,
            text=text,
            published_at=published_at,
            url=link,
            quoted_text=(
                " ".join(quote.text)
                if quote.author.casefold() in TRUSTED_QUOTED_AUTHORS
                else ""
            ),
            quoted_author=quote.author,
            is_retweet=text.startswith("RT "),
        )

    @staticmethod
    def _json_post(source: Source, item: object) -> Post:
        if not isinstance(item, dict):
            raise ValueError("RSSHub JSON item must be an object")
        values: dict[str, str] = {}
        for name in ("title", "description", "pubDate", "link", "guid"):
            value = item.get(name, "")
            if not isinstance(value, str):
                raise ValueError(f"RSSHub JSON item field {name} must be text")
            values[name] = value

        link = values["link"] or values["guid"]
        link_author, post_id = _status_identity(link)
        if (
            link_author.casefold() != source.handle.casefold()
            or not _json_author_matches(item.get("author"), source.handle)
        ):
            raise ValueError(
                f"entry author does not match trusted route @{source.handle}: {link}"
            )

        quote = QuoteParser()
        quote.feed(values["description"])
        text = _primary_text(values["title"], quote.main_text)
        text = re.sub(r"^Re\s+", "", text)
        return Post(
            post_id=post_id,
            author=source.handle,
            text=text,
            published_at=_published_at(values["pubDate"]),
            url=link,
            quoted_text=(
                " ".join(quote.text)
                if quote.author.casefold() in TRUSTED_QUOTED_AUTHORS
                else ""
            ),
            quoted_author=quote.author,
            is_retweet=text.startswith("RT "),
        )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_error_body(error: HTTPError) -> bytes:
    try:
        return error.read(MAX_ERROR_BODY_BYTES)
    except (AttributeError, HTTPException, OSError, ValueError):
        return b""


def _has_auth_failure(body: bytes) -> bool:
    return bool(AUTH_FAILURE.search(body[:MAX_ERROR_BODY_BYTES]))


def _response_type(response: object) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return "xml"
    try:
        content_type = headers.get_content_type()
    except AttributeError:
        try:
            content_type = headers.get("Content-Type", "").partition(";")[0].strip()
        except AttributeError:
            return "xml"
    content_type = content_type.casefold()
    if not content_type:
        return "xml"
    if content_type == "application/json" or content_type.endswith("+json"):
        return "json"
    if (
        content_type
        in {
            "application/rss+xml",
            "application/atom+xml",
            "application/xml",
            "text/xml",
        }
        or content_type.endswith("+xml")
    ):
        return "xml"
    return "other"


def _json_items(payload: object) -> list[object] | None:
    if not isinstance(payload, dict) or "item" not in payload:
        return None
    items = payload["item"]
    if not isinstance(items, list):
        return None
    return items


def _json_author_matches(value: object, expected_handle: str) -> bool:
    if isinstance(value, str):
        # RSSHub's public API emits a mutable display name here (for example,
        # "Sam Altman" for @sama), not a handle. Identity remains anchored to
        # the strictly validated status URL and configured route above.
        return bool(value.strip())
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    url = value[0].get("url")
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    match = re.fullmatch(r"/([^/]+)/?", parsed.path)
    return bool(
        parsed.scheme.casefold() == "https"
        and hostname in TRUSTED_X_HOSTS
        and not parsed.query
        and not parsed.fragment
        and match is not None
        and match.group(1).casefold() == expected_handle.casefold()
    )


def _child_text(entry: ElementTree.Element, *names: str) -> str:
    for name in names:
        for child in entry:
            if _local_name(child.tag) == name:
                return "".join(child.itertext())
    return ""


def _entry_link(entry: ElementTree.Element) -> str:
    for child in entry:
        if _local_name(child.tag) == "link":
            return child.attrib.get("href", "") or (child.text or "")
    return _child_text(entry, "guid", "id")


def _status_identity(url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").casefold()
    except ValueError as exc:
        raise ValueError(f"invalid X status URL: {url}") from exc
    if hostname.startswith("www."):
        hostname = hostname[4:]
    path_match = STATUS_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme.casefold() != "https"
        or hostname not in TRUSTED_X_HOSTS
        or path_match is None
    ):
        raise ValueError(f"invalid X status URL: {url}")
    return path_match.group(1), path_match.group(2)


def _plain_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", unescape(value))
    return " ".join(without_tags.split())


def _primary_text(title: str, description_parts: list[str]) -> str:
    title_text = _plain_text(title)
    description_text = " ".join(description_parts)
    if len(description_text) > len(title_text):
        return description_text
    return title_text or description_text


def _published_at(value: str) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        published_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        published_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    return published_at.astimezone(UTC)
