from datetime import UTC, datetime
from http.client import IncompleteRead, RemoteDisconnected
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

import pytest

from codex_quota_monitor.classifier import classify
from codex_quota_monitor.feed import FeedError, RssHubClient
from codex_quota_monitor.models import Source


RSS_FIXTURE = Path(__file__).parent / "fixtures" / "twitter-user.xml"


class Response:
    status = 200

    def __init__(self, body: bytes, content_type: str | None = None) -> None:
        self.body = body
        self.headers = (
            {"Content-Type": content_type} if content_type is not None else {}
        )

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class ReadFailureResponse(Response):
    def __init__(self, error: Exception) -> None:
        super().__init__(b"")
        self.error = error

    def read(self) -> bytes:
        raise self.error


def test_fetches_posts_from_exact_lightweight_route() -> None:
    response = Response(RSS_FIXTURE.read_bytes())
    client = RssHubClient("http://rsshub:1200/", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response) as mocked:
        posts = client.fetch(Source("thsottiaux", True))

    request = mocked.call_args.args[0]
    assert request.full_url == (
        "http://rsshub:1200/twitter/user/thsottiaux/"
        "includeReplies=1&includeRts=0&count=20&readable=1&showQuotedInTitle=0"
    )
    assert mocked.call_args.kwargs == {"timeout": 20}
    assert request.headers["Accept"] == (
        "application/json, application/rss+xml, application/atom+xml"
    )
    assert request.headers["User-agent"] == "codex-quota-monitor/0.1"
    assert posts[0].post_id == "2066956441173323943"
    assert posts[0].author == "thsottiaux"


def _json_response(items: list[dict[str, str]]) -> Response:
    return Response(
        json.dumps({"title": "trusted timeline", "item": items}).encode(),
        "application/json; charset=utf-8",
    )


def test_parses_rsshub_json_post_and_trusted_quote() -> None:
    response = _json_response(
        [
            {
                "title": "Codex limits will reset tomorrow.",
                "author": "OpenAI",
                "description": (
                    "Codex limits will reset tomorrow."
                    '<div class="rsshub-quote">'
                    '<a href="https://x.com/OpenAIDevs/status/111"></a>'
                    "<p>Usage limits are being expanded.</p></div>"
                ),
                "pubDate": "Tue, 16 Jun 2026 12:00:00 GMT",
                "link": "https://x.com/OpenAI/status/123456789",
                "guid": "https://x.com/OpenAI/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        post = client.fetch(Source("OpenAI", False))[0]

    assert post.post_id == "123456789"
    assert post.author == "OpenAI"
    assert post.text == "Codex limits will reset tomorrow."
    assert post.published_at == datetime(2026, 6, 16, 12, tzinfo=UTC)
    assert post.url == "https://x.com/OpenAI/status/123456789"
    assert post.quoted_author == "OpenAIDevs"
    assert post.quoted_text == "Usage limits are being expanded."
    assert post.is_retweet is False


def test_parses_rsshub_public_api_author_array() -> None:
    response = _json_response(
        [
            {
                "title": "Codex limits reset.",
                "author": [
                    {
                        "name": "OpenAI",
                        "url": "https://x.com/OpenAI",
                        "avatar": "https://pbs.twimg.com/profile_images/example.jpg",
                    }
                ],
                "description": "Codex limits reset.",
                "pubDate": "2026-06-16T12:00:00Z",
                "link": "https://x.com/OpenAI/status/123456789",
                "guid": "https://twitter.com/OpenAI/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        post = client.fetch(Source("OpenAI", False))[0]

    assert post.author == "OpenAI"


@pytest.mark.parametrize(
    ("handle", "display_name"),
    [
        ("OpenAIDevs", "OpenAI Developers"),
        ("thsottiaux", "Tibo"),
        ("sama", "Sam Altman"),
    ],
)
def test_parses_rsshub_display_name_when_status_url_matches_route(
    handle: str, display_name: str
) -> None:
    response = _json_response(
        [
            {
                "title": "Codex limits reset.",
                "author": display_name,
                "description": "Codex limits reset.",
                "pubDate": "2026-06-16T12:00:00Z",
                "link": f"https://x.com/{handle}/status/123456789",
                "guid": f"https://twitter.com/{handle}/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        post = client.fetch(Source(handle, False))[0]

    assert post.author == handle


def test_rejects_rsshub_public_api_author_array_for_another_account() -> None:
    response = _json_response(
        [
            {
                "title": "Codex limits reset.",
                "author": [
                    {"name": "OpenAI", "url": "https://x.com/attacker"}
                ],
                "description": "Codex limits reset.",
                "pubDate": "2026-06-16T12:00:00Z",
                "link": "https://x.com/OpenAI/status/123456789",
                "guid": "https://twitter.com/OpenAI/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        with pytest.raises(FeedError, match="feed fetch failed for @OpenAI"):
            client.fetch(Source("OpenAI", False))


def test_parses_rsshub_json_retweet_marker() -> None:
    response = _json_response(
        [
            {
                "title": "RT @someone unrelated update",
                "author": "OpenAI",
                "description": "RT @someone unrelated update",
                "pubDate": "2026-06-16T12:00:00Z",
                "link": "https://x.com/OpenAI/status/123456789",
                "guid": "https://x.com/OpenAI/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        post = client.fetch(Source("OpenAI", False))[0]

    assert post.is_retweet is True


def test_rejects_empty_rsshub_json_feed() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch(
        "codex_quota_monitor.feed.urlopen", return_value=_json_response([])
    ):
        with pytest.raises(FeedError, match="empty feed for @OpenAI"):
            client.fetch(Source("OpenAI", False))


def test_rejects_rsshub_json_item_with_mismatched_author_or_status_url() -> None:
    response = _json_response(
        [
            {
                "title": "Codex limits reset.",
                "author": "attacker",
                "description": "Codex limits reset.",
                "pubDate": "2026-06-16T12:00:00Z",
                "link": "https://x.com/attacker/status/123456789",
                "guid": "https://x.com/attacker/status/123456789",
            }
        ]
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response):
        with pytest.raises(FeedError, match="feed fetch failed for @OpenAI"):
            client.fetch(Source("OpenAI", False))


def test_json_error_object_preserves_bounded_authentication_semantics() -> None:
    response = StructuredErrorResponse(b'{"error":"auth_token invalid"}')
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response) as mocked:
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI", False))

    assert raised.value.auth_failed is True
    assert response.read_sizes == [65_536]
    assert mocked.call_count == 1


def test_parses_rss_post_fields() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch(
        "codex_quota_monitor.feed.urlopen",
        return_value=Response(RSS_FIXTURE.read_bytes()),
    ):
        post = client.fetch(Source("thsottiaux"))[0]

    assert post.text == "Give us 24 hours to reset the Codex rate limits across all plans."
    assert post.published_at == datetime(2026, 6, 16, 12, tzinfo=UTC)
    assert post.url == "https://x.com/thsottiaux/status/2066956441173323943"
    assert post.is_retweet is False


def test_parses_atom_entry() -> None:
    atom = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>We have reset Codex usage limits.</title>
        <link href="https://x.com/OpenAI/status/123456789" />
        <id>https://x.com/OpenAI/status/123456789</id>
        <published>2026-06-16T12:00:00Z</published>
        <content type="html">We have reset Codex usage limits.</content>
      </entry>
    </feed>"""
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(atom)):
        post = client.fetch(Source("OpenAI"))[0]

    assert post.post_id == "123456789"
    assert post.author == "OpenAI"
    assert post.published_at == datetime(2026, 6, 16, 12, tzinfo=UTC)
    assert post.url == "https://x.com/OpenAI/status/123456789"


@pytest.mark.parametrize("status", [401, 403])
def test_marks_authentication_failure(status: int) -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)
    error = HTTPError("http://rsshub", status, "Unauthorized", {}, None)

    with patch("codex_quota_monitor.feed.urlopen", side_effect=error):
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is True


@pytest.mark.parametrize(
    "body",
    [
        b"Unauthorized",
        b'{"message":"authentication failed"}',
        b'{"error":"auth_token expired"}',
        b'{"error":"auth token invalid"}',
        b'{"message":"login required"}',
        b'{"error":"X session cookie expired"}',
    ],
)
def test_marks_explicit_authentication_semantics_in_5xx_body(body: bytes) -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)
    error = HTTPError(
        "http://rsshub",
        503,
        "Service Unavailable",
        {},
        BytesIO(body),
    )

    with patch("codex_quota_monitor.feed.urlopen", side_effect=error) as mocked:
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is True
    assert str(raised.value) == "X authentication failed"
    assert mocked.call_count == 1


def test_plain_5xx_body_is_not_misclassified_as_authentication_failure() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)
    error = HTTPError(
        "http://rsshub",
        503,
        "Service Unavailable",
        {},
        BytesIO(b"upstream temporarily unavailable"),
    )

    with patch("codex_quota_monitor.feed.urlopen", side_effect=error):
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is False


def test_non_5xx_body_is_not_used_to_infer_authentication_failure() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)
    error = HTTPError(
        "http://rsshub",
        404,
        "Not Found",
        {},
        BytesIO(b"Unauthorized"),
    )

    with patch("codex_quota_monitor.feed.urlopen", side_effect=error):
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is False


class BoundedErrorBody(BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0 or size > 65_536:
            raise AssertionError("error response read must be bounded to 64 KiB")
        return super().read(size)


class StructuredErrorResponse(BoundedErrorBody):
    status = 200
    headers = {"Content-Type": "application/json; charset=utf-8"}

    def __enter__(self) -> "StructuredErrorResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_marks_bounded_structured_error_feed_with_explicit_auth_semantics() -> None:
    response = StructuredErrorResponse(b'{"error":"auth_token invalid"}')
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=response) as mocked:
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is True
    assert str(raised.value) == "X authentication failed"
    assert response.read_sizes == [65_536]
    assert mocked.call_count == 1


def test_5xx_body_is_bounded_and_never_leaked_in_feed_error() -> None:
    secret_marker = b"private-rsshub-response-secret"
    body = BoundedErrorBody(
        b"temporary upstream failure: "
        + secret_marker
        + b"x" * 70_000
        + b" auth_token expired"
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)
    error = HTTPError(
        "http://rsshub",
        503,
        "Service Unavailable",
        {},
        body,
    )

    with patch("codex_quota_monitor.feed.urlopen", side_effect=error):
        with pytest.raises(FeedError) as raised:
            client.fetch(Source("OpenAI"))

    assert body.read_sizes == [65_536]
    assert raised.value.auth_failed is False
    assert secret_marker.decode() not in str(raised.value)


@pytest.mark.parametrize(
    "transient_error",
    [
        HTTPError("http://rsshub", 503, "Unavailable", {}, None),
        URLError("network unreachable"),
        TimeoutError("timed out"),
    ],
)
def test_retries_transient_fetch_failures(transient_error: Exception) -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)

    with (
        patch(
            "codex_quota_monitor.feed.urlopen",
            side_effect=[transient_error, Response(RSS_FIXTURE.read_bytes())],
        ) as mocked,
        patch("codex_quota_monitor.feed.time.sleep") as sleep,
    ):
        posts = client.fetch(Source("thsottiaux"))

    assert posts[0].post_id == "2066956441173323943"
    assert mocked.call_count == 2
    sleep.assert_called_once_with(1)


def test_reports_exhausted_fetch_failure_without_auth_flag() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)

    with (
        patch("codex_quota_monitor.feed.urlopen", side_effect=TimeoutError("timed out")),
        patch("codex_quota_monitor.feed.time.sleep"),
        pytest.raises(FeedError, match="feed fetch failed for @OpenAI") as raised,
    ):
        client.fetch(Source("OpenAI"))

    assert raised.value.auth_failed is False


def test_rejects_entry_whose_author_does_not_match_trusted_route() -> None:
    body = RSS_FIXTURE.read_text(encoding="utf-8").replace(
        "x.com/thsottiaux/status/", "x.com/randomuser/status/"
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(body.encode())):
        with pytest.raises(FeedError, match="feed fetch failed for @thsottiaux"):
            client.fetch(Source("thsottiaux"))


def test_rejects_allowlisted_url_hidden_inside_untrusted_query() -> None:
    body = RSS_FIXTURE.read_text(encoding="utf-8").replace(
        "https://x.com/thsottiaux/status/2066956441173323943",
        "https://evil.invalid/?next=https://x.com/thsottiaux/status/2066956441173323943",
        1,
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(body.encode())):
        with pytest.raises(FeedError, match="feed fetch failed for @thsottiaux"):
            client.fetch(Source("thsottiaux"))


def test_rejects_empty_feed() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch(
        "codex_quota_monitor.feed.urlopen",
        return_value=Response(b"<rss><channel /></rss>"),
    ):
        with pytest.raises(FeedError, match="empty feed for @OpenAI"):
            client.fetch(Source("OpenAI"))


def test_route_disables_replies_when_source_requests_it() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch(
        "codex_quota_monitor.feed.urlopen",
        return_value=Response(RSS_FIXTURE.read_bytes()),
    ) as mocked:
        client.fetch(Source("thsottiaux", False))

    assert "includeReplies=0" in mocked.call_args.args[0].full_url


def test_extracts_quote_from_trusted_author_and_removes_reply_prefix() -> None:
    body = RSS_FIXTURE.read_text(encoding="utf-8").replace(
        "<title>Give us 24 hours",
        "<title>Re Give us 24 hours",
    ).replace(
        "Give us 24 hours to reset the Codex rate limits across all plans.]]>",
        "Original reply<div class=\"rsshub-quote\">"
        "<a href=\"https://x.com/OpenAI/status/111\"></a>"
        "<p>We have reset Codex usage limits.</p></div>]]>",
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(body.encode())):
        post = client.fetch(Source("thsottiaux"))[0]

    assert post.text.startswith("Give us 24 hours")
    assert post.quoted_author == "OpenAI"
    assert post.quoted_text == "We have reset Codex usage limits."


def test_does_not_trust_quoted_text_from_an_unlisted_author() -> None:
    body = RSS_FIXTURE.read_text(encoding="utf-8").replace(
        "Give us 24 hours to reset the Codex rate limits across all plans.]]>",
        "Original reply<div class=\"rsshub-quote\">"
        "<a href=\"https://x.com/randomuser/status/111\"></a>"
        "<p>We have reset Codex usage limits.</p></div>]]>",
    )
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(body.encode())):
        post = client.fetch(Source("thsottiaux"))[0]

    assert post.quoted_author == "randomuser"
    assert post.quoted_text == ""


def test_description_fallback_keeps_quote_out_of_original_post_text() -> None:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel><item>
      <link>https://x.com/thsottiaux/status/999</link>
      <pubDate>Tue, 16 Jun 2026 12:00:00 GMT</pubDate>
      <description><![CDATA[
        A product update is available.
        <div class="rsshub-quote">
          <a href="https://x.com/OpenAI/status/111"></a>
          <p>We have reset Codex usage limits.</p>
        </div>
      ]]></description>
    </item></channel></rss>"""
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch("codex_quota_monitor.feed.urlopen", return_value=Response(body)):
        parsed = client.fetch(Source("thsottiaux"))[0]

    assert parsed.text == "A product update is available."
    assert parsed.quoted_text == "We have reset Codex usage limits."
    assert classify(parsed, frozenset({"thsottiaux"})).matched is False


@pytest.mark.parametrize(
    "read_error",
    [
        ConnectionResetError("connection reset"),
        OSError("socket read failed"),
        RemoteDisconnected("remote closed connection"),
        IncompleteRead(b"partial", 100),
    ],
)
def test_retries_failures_raised_while_reading_response(read_error: Exception) -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=2, count=20)

    with (
        patch(
            "codex_quota_monitor.feed.urlopen",
            side_effect=[
                ReadFailureResponse(read_error),
                Response(RSS_FIXTURE.read_bytes()),
            ],
        ) as mocked,
        patch("codex_quota_monitor.feed.time.sleep") as sleep,
    ):
        posts = client.fetch(Source("thsottiaux"))

    assert posts[0].post_id == "2066956441173323943"
    assert mocked.call_count == 2
    sleep.assert_called_once_with(1)


def test_quote_parser_ignores_text_after_void_tags_and_quote_container() -> None:
    client = RssHubClient("http://rsshub:1200", timeout=20, retries=1, count=20)

    with patch(
        "codex_quota_monitor.feed.urlopen",
        return_value=Response(RSS_FIXTURE.read_bytes()),
    ):
        quote_post = client.fetch(Source("thsottiaux"))[1]

    assert quote_post.quoted_author == "OpenAI"
    assert quote_post.quoted_text == "@OpenAI We have reset Codex usage limits."
    assert "Outside timeline text." not in quote_post.quoted_text
