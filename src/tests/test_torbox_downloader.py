"""Unit tests for the TorBox downloader.

These tests inject a fake session into TorBoxDownloader (bypassing __init__ and the
network) so we can exercise the parsing/availability logic in isolation.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from program.services.downloaders.torbox import (
    REF_SCHEME,
    TorBoxDownloader,
    TorBoxError,
)


class FakeResponse:
    def __init__(self, json_data=None, status_code=200, ok=None):
        self._json = json_data
        self.status_code = status_code
        self.ok = (200 <= status_code < 300) if ok is None else ok

    def json(self):
        return self._json


class FakeSession:
    """Routes requests by (METHOD, url) to a pre-seeded FakeResponse and records calls."""

    def __init__(self, routes=None):
        self.headers = {}
        self.routes = routes or {}
        self.calls = []

    def _dispatch(self, method, url, params=None, data=None, json=None, **kwargs):
        self.calls.append(
            SimpleNamespace(method=method, url=url, params=params, data=data, json=json)
        )
        resp = self.routes.get((method, url))

        if resp is None:
            return FakeResponse({"success": False, "detail": "not mocked"}, 404)

        return resp

    def get(self, url, params=None, **kwargs):
        return self._dispatch("GET", url, params=params, **kwargs)

    def post(self, url, params=None, data=None, json=None, **kwargs):
        return self._dispatch("POST", url, params=params, data=data, json=json, **kwargs)

    def called(self, method, url) -> bool:
        return any(c.method == method and c.url == url for c in self.calls)

    def call_for(self, method, url):
        return next(c for c in self.calls if c.method == method and c.url == url)


def make_downloader(session: FakeSession, api_key: str = "test-key") -> TorBoxDownloader:
    """Build a TorBoxDownloader with a fake session, skipping __init__/network."""

    downloader = TorBoxDownloader.__new__(TorBoxDownloader)
    downloader.key = "torbox"
    downloader.settings = SimpleNamespace(enabled=True, api_key=api_key)
    downloader.api = SimpleNamespace(session=session, BASE_URL="https://api.torbox.app/")

    return downloader


# --- user info -------------------------------------------------------------


def test_get_user_info_premium():
    expires = (datetime.now(tz=timezone.utc) + timedelta(days=30)).isoformat()
    session = FakeSession(
        {
            ("GET", "v1/api/user/me"): FakeResponse(
                {
                    "success": True,
                    "data": {
                        "id": 99,
                        "email": "user@example.com",
                        "plan": 2,
                        "premium_expires_at": expires,
                        "total_downloaded": 1234,
                    },
                }
            )
        }
    )

    info = make_downloader(session).get_user_info()

    assert info is not None
    assert info.service == "torbox"
    assert info.email == "user@example.com"
    assert info.user_id == 99
    assert info.premium_status == "premium"
    assert info.premium_days_left is not None and info.premium_days_left >= 28
    assert info.total_downloaded_bytes == 1234


def test_get_user_info_free_plan():
    session = FakeSession(
        {
            ("GET", "v1/api/user/me"): FakeResponse(
                {"success": True, "data": {"id": 1, "email": "f@e.com", "plan": 0}}
            )
        }
    )

    info = make_downloader(session).get_user_info()

    assert info is not None
    assert info.premium_status == "free"


def test_get_user_info_error_returns_none():
    session = FakeSession(
        {("GET", "v1/api/user/me"): FakeResponse({"detail": "nope"}, 401)}
    )

    assert make_downloader(session).get_user_info() is None


# --- add / delete ----------------------------------------------------------


def test_add_torrent_returns_id():
    session = FakeSession(
        {
            ("POST", "v1/api/torrents/createtorrent"): FakeResponse(
                {"success": True, "data": {"torrent_id": 42, "hash": "abc"}}
            )
        }
    )

    assert make_downloader(session).add_torrent("abc123") == 42

    call = session.call_for("POST", "v1/api/torrents/createtorrent")
    assert "magnet:?xt=urn:btih:abc123" in call.data["magnet"]


def test_add_torrent_failure_raises():
    session = FakeSession(
        {
            ("POST", "v1/api/torrents/createtorrent"): FakeResponse(
                {"success": False, "detail": "bad magnet"}
            )
        }
    )

    with pytest.raises(TorBoxError):
        make_downloader(session).add_torrent("abc123")


def test_delete_torrent_sends_delete_operation():
    session = FakeSession(
        {
            ("POST", "v1/api/torrents/controltorrent"): FakeResponse(
                {"success": True, "data": True}
            )
        }
    )

    make_downloader(session).delete_torrent(7)

    call = session.call_for("POST", "v1/api/torrents/controltorrent")
    assert call.json == {"torrent_id": 7, "operation": "delete"}


def test_delete_torrent_failure_raises():
    session = FakeSession(
        {("POST", "v1/api/torrents/controltorrent"): FakeResponse({}, 500)}
    )

    with pytest.raises(TorBoxError):
        make_downloader(session).delete_torrent(7)


# --- cache check -----------------------------------------------------------


def test_is_cached_true_and_false():
    cached = make_downloader(
        FakeSession(
            {
                ("GET", "v1/api/torrents/checkcached"): FakeResponse(
                    {"success": True, "data": {"abc": {"hash": "abc"}}}
                )
            }
        )
    )
    not_cached = make_downloader(
        FakeSession(
            {
                ("GET", "v1/api/torrents/checkcached"): FakeResponse(
                    {"success": True, "data": {}}
                )
            }
        )
    )

    assert cached._is_cached("abc") is True
    assert not_cached._is_cached("abc") is False


# --- instant availability --------------------------------------------------


def _mylist_response(torrent_id: int):
    return FakeResponse(
        {
            "success": True,
            "data": {
                "id": torrent_id,
                "hash": "abc",
                "name": "Movie.2021.1080p",
                "size": 2_000_000_000,
                "download_finished": True,
                "download_present": True,
                "download_state": "completed",
                "progress": 1.0,
                "files": [
                    {
                        "id": 0,
                        "name": "Movie.2021.1080p/Movie.2021.1080p.mkv",
                        "short_name": "Movie.2021.1080p.mkv",
                        "size": 2_000_000_000,
                    },
                    {
                        "id": 1,
                        "name": "Movie.2021.1080p/readme.txt",
                        "short_name": "readme.txt",
                        "size": 1000,
                    },
                ],
            },
        }
    )


def test_get_instant_availability_cached_builds_container():
    session = FakeSession(
        {
            ("GET", "v1/api/torrents/checkcached"): FakeResponse(
                {"success": True, "data": {"abc": {"hash": "abc"}}}
            ),
            ("POST", "v1/api/torrents/createtorrent"): FakeResponse(
                {"success": True, "data": {"torrent_id": 55}}
            ),
            ("GET", "v1/api/torrents/mylist"): _mylist_response(55),
        }
    )

    container = make_downloader(session).get_instant_availability("abc", "movie")

    assert container is not None
    assert container.cached
    # Only the valid video file survives; the .txt is filtered out.
    assert len(container.files) == 1
    assert container.files[0].filename == "Movie.2021.1080p.mkv"
    assert container.files[0].download_url == f"{REF_SCHEME}55/0"
    assert container.torrent_id == 55
    assert container.torrent_info is not None
    assert container.torrent_info.progress == 100.0


def test_get_instant_availability_not_cached_skips_add():
    session = FakeSession(
        {
            ("GET", "v1/api/torrents/checkcached"): FakeResponse(
                {"success": True, "data": {}}
            )
        }
    )

    assert make_downloader(session).get_instant_availability("abc", "movie") is None
    # Must not add a torrent that isn't cached.
    assert not session.called("POST", "v1/api/torrents/createtorrent")


# --- unrestrict ------------------------------------------------------------


def test_unrestrict_link_resolves_reference():
    cdn = "https://store-1.torbox.app/dl/Movie.2021.1080p.mkv?token=xyz"
    session = FakeSession(
        {
            ("GET", "v1/api/torrents/requestdl"): FakeResponse(
                {"success": True, "data": cdn}
            )
        }
    )

    result = make_downloader(session).unrestrict_link(f"{REF_SCHEME}55/0")

    assert result is not None
    assert result.download == cdn
    assert result.filename == "Movie.2021.1080p.mkv"

    call = session.call_for("GET", "v1/api/torrents/requestdl")
    assert call.params["torrent_id"] == 55
    assert call.params["file_id"] == 0
    assert call.params["token"] == "test-key"


def test_unrestrict_link_rejects_non_reference():
    session = FakeSession()

    assert make_downloader(session).unrestrict_link("https://example.com/x") is None
    assert not session.called("GET", "v1/api/torrents/requestdl")
