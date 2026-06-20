from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentInfo,
    UnrestrictedLink,
    UserInfo,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen, SmartResponse, SmartSession
from program.media.item import ProcessedItemType

from .shared import DownloaderBase, premium_days_left

# Synthetic reference stored as a DebridFile.download_url. TorBox download links
# (requestdl) expire after a few hours, so instead of baking an expiring URL into
# the MediaEntry we store this stable reference and resolve it lazily through
# unrestrict_link() whenever the VFS needs a fresh CDN URL.
REF_SCHEME = "torbox://"


class TorBoxFile(BaseModel):
    """Represents a single file inside a TorBox torrent."""

    model_config = ConfigDict(extra="ignore")

    id: int
    name: str = ""
    size: int = 0
    short_name: str | None = None


class TorBoxTorrent(BaseModel):
    """Represents a torrent as returned by /torrents/mylist."""

    model_config = ConfigDict(extra="ignore")

    id: int
    hash: str | None = None
    name: str = ""
    size: int | None = None
    download_state: str | None = None
    download_finished: bool = False
    download_present: bool = False
    progress: float | None = None
    created_at: str | None = None
    files: list[TorBoxFile] = Field(default_factory=list[TorBoxFile])


class TorBoxUser(BaseModel):
    """Represents the user account as returned by /user/me."""

    model_config = ConfigDict(extra="ignore")

    id: int | str
    email: str | None = None
    plan: int = 0
    premium_expires_at: str | None = None
    total_downloaded: int | None = None


class TorBoxError(Exception):
    """Base exception for TorBox related errors."""


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse a TorBox timestamp leniently into a UTC-aware datetime."""

    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        logger.debug(f"Failed to parse TorBox datetime: {value}")
        return None

    # TorBox sometimes returns timestamps without an offset; assume UTC so arithmetic
    # against timezone-aware "now" never raises.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


class TorBoxAPI:
    """
    Minimal TorBox API client using SmartSession for retries, rate limits, and circuit breaker.
    """

    BASE_URL = "https://api.torbox.app/"

    def __init__(self, api_key: str, proxy_url: str | None = None) -> None:
        """
        Args:
            api_key: TorBox API key.
            proxy_url: Optional proxy URL used for both HTTP and HTTPS.
        """

        self.api_key = api_key
        self.proxy_url = proxy_url

        # TorBox allows 300 req/min for cached actions; stay conservative.
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                "api.torbox.app": {
                    "rate": 5,
                    "capacity": 300,
                },
            },
            proxies=proxies,
            retries=2,
            backoff_factor=0.5,
        )

        self.session.headers.update({"Authorization": f"Bearer {api_key}"})


class TorBoxDownloader(DownloaderBase):
    """
    TorBox downloader.

    Notes on failure & breaker behavior:
    - Network/transport failures are retried by SmartSession, then counted against the per-domain
      CircuitBreaker; once OPEN, SmartSession raises CircuitBreakerOpen before the request.
    - HTTP status codes are not exceptions; we check response.ok and map to messages via _handle_error(...).

    Notes on download URLs:
    - TorBox does not expose a persistent file link. We add the torrent, store a synthetic
      reference (REF_SCHEME) per file, and resolve it to a fresh CDN URL through unrestrict_link()
      via /torrents/requestdl whenever the VFS asks for one.
    """

    def __init__(self) -> None:
        self.key = "torbox"
        self.settings = settings_manager.settings.downloaders.tor_box
        self.api: TorBoxAPI | None = None
        self.initialized = self.validate()

    def validate(self) -> bool:
        """
        Validate settings and current premium status.

        Returns:
            True if ready, else False.
        """

        if not self._validate_settings():
            return False

        proxy_url = self.PROXY_URL or None

        self.api = TorBoxAPI(api_key=self.settings.api_key, proxy_url=proxy_url)

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        """
        Returns:
            True when enabled and API key present; otherwise False.
        """

        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.warning("TorBox API key is not set")
            return False

        return True

    def _validate_premium(self) -> bool:
        """
        Returns:
            True if a paid plan is active; otherwise False.
        """

        try:
            user_info = self.get_user_info()

            if not user_info:
                logger.error("Failed to get TorBox user info")
                return False

            if user_info.premium_status != "premium":
                logger.error("TorBox paid plan required")
                return False

            if user_info.premium_expires_at:
                logger.info(premium_days_left(user_info.premium_expires_at))

            return True
        except Exception as e:
            logger.error(f"Failed to validate TorBox premium status: {e}")
            return False

    def _handle_error(self, response: SmartResponse) -> str:
        """
        Map HTTP status codes and TorBox error payloads to error messages.
        """

        status = response.status_code

        match status:
            case 400:
                return "Bad request"
            case 401:
                return "Unauthorized - check API key"
            case 403:
                return "Forbidden"
            case 404:
                return "Not found"
            case 429:
                return "Rate limit exceeded"
            case _ if status >= 500:
                return "TorBox server error"
            case _:
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        return payload.get("detail") or payload.get("error") or f"HTTP {status}"
                except Exception:
                    pass

                return f"HTTP {status}"

    def _maybe_backoff(self, response: SmartResponse) -> None:
        """
        Check if we should back off based on response.
        """

        if response.status_code == 429:
            logger.warning("TorBox rate limit hit, backing off")

    @staticmethod
    def _build_ref(torrent_id: int | str, file_id: int) -> str:
        """Build the synthetic download_url reference stored on a DebridFile."""

        return f"{REF_SCHEME}{torrent_id}/{file_id}"

    @staticmethod
    def _parse_ref(link: str) -> tuple[int, int] | None:
        """Parse a synthetic reference back into (torrent_id, file_id)."""

        if not link.startswith(REF_SCHEME):
            return None

        try:
            torrent_part, file_part = link[len(REF_SCHEME):].split("/", 1)
            return int(torrent_part), int(file_part)
        except (ValueError, AttributeError):
            return None

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
        **kwargs: Any,
    ) -> TorrentContainer | None:
        """
        Check whether a hash is cached on TorBox.

        TorBox exposes a dedicated cache check, so we query it first and only add the
        torrent (to obtain a torrent id and file list) when it is already cached. This
        avoids adding non-cached torrents to the user's account.
        """

        torrent_id: int | None = None

        try:
            if not self._is_cached(infohash):
                return None

            torrent_id = self.add_torrent(infohash)
            container, reason, info = self._process_torrent(
                torrent_id, infohash, item_type
            )

            if container is None:
                if reason:
                    logger.debug(f"Availability check failed [{infohash}]: {reason}")

                self._safe_delete(torrent_id)

                return None

            # Success - cache torrent_id AND info in container to avoid re-adding/re-fetching during download
            container.torrent_id = torrent_id
            container.torrent_info = info

            return container

        except CircuitBreakerOpen:
            logger.debug(f"Circuit breaker OPEN for TorBox; skipping {infohash}")
            self._safe_delete(torrent_id)
            raise
        except TorBoxError as e:
            logger.warning(f"Availability check failed [{infohash}]: {e}")
            self._safe_delete(torrent_id)
            return None
        except InvalidDebridFileException as e:
            logger.debug(
                f"Availability check failed [{infohash}]: Invalid debrid file(s) - {e}"
            )
            self._safe_delete(torrent_id)
            return None
        except Exception as e:
            logger.debug(f"Availability check failed [{infohash}]: {e}")
            self._safe_delete(torrent_id)
            return None

    def _safe_delete(self, torrent_id: int | str | None) -> None:
        """Best-effort delete used during cleanup paths."""

        if not torrent_id:
            return

        try:
            self.delete_torrent(torrent_id)
        except Exception as e:
            logger.debug(f"Failed to delete torrent {torrent_id}: {e}")

    def _is_cached(self, infohash: str) -> bool:
        """
        Query /torrents/checkcached to determine if a hash is instantly available.
        """

        assert self.api

        response = self.api.session.get(
            "v1/api/torrents/checkcached",
            params={"hash": infohash, "format": "object", "list_files": "false"},
        )

        self._maybe_backoff(response)

        if not response.ok:
            return False

        try:
            payload = response.json()
        except Exception:
            return False

        if not isinstance(payload, dict) or not payload.get("success"):
            return False

        data = payload.get("data")

        if isinstance(data, dict):
            return bool(data)

        if isinstance(data, list):
            return len(data) > 0

        return False

    def _fetch_torrent_raw(self, torrent_id: int | str) -> TorBoxTorrent:
        """
        Single /torrents/mylist call - shared by _process_torrent and get_torrent_info.

        Raises:
            TorBoxError: on API or parse failure.
        """

        assert self.api

        response = self.api.session.get(
            "v1/api/torrents/mylist",
            params={"id": str(torrent_id), "bypass_cache": "true"},
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise TorBoxError(self._handle_error(response))

        payload = response.json()

        if not isinstance(payload, dict) or not payload.get("success"):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise TorBoxError(detail or "Invalid response from TorBox")

        data = payload.get("data")

        # /mylist with an id returns a single object, but tolerate a list response.
        if isinstance(data, list):
            data = next(
                (t for t in data if str(t.get("id")) == str(torrent_id)),
                data[0] if data else None,
            )

        if not data:
            raise TorBoxError(f"Torrent {torrent_id} not found")

        return TorBoxTorrent.model_validate(data)

    def _build_info(self, torrent_id: int | str, torrent: TorBoxTorrent) -> TorrentInfo:
        """Build a normalized TorrentInfo from a TorBox torrent."""

        return TorrentInfo(
            id=torrent_id,
            name=torrent.name,
            status=torrent.download_state,
            infohash=torrent.hash,
            bytes=torrent.size,
            created_at=_parse_datetime(torrent.created_at),
            progress=(torrent.progress * 100) if torrent.progress is not None else None,
            files={},
            links=[],
        )

    def _process_torrent(
        self,
        torrent_id: int,
        infohash: str,
        item_type: ProcessedItemType,
    ) -> tuple[TorrentContainer | None, str | None, TorrentInfo | None]:
        """
        Process a single torrent and return (container, reason, info).

        Returns:
            (TorrentContainer or None, human-readable reason string if None, TorrentInfo or None)
        """

        torrent = self._fetch_torrent_raw(torrent_id)

        if not torrent.files:
            if not (torrent.download_finished or torrent.download_present):
                return None, f"Not ready (state={torrent.download_state})", None

            return None, "no files present in the torrent", None

        files = list[DebridFile]()

        for file in torrent.files:
            filename = file.short_name or file.name.split("/")[-1]

            try:
                df = DebridFile.create(
                    path=file.name or filename,
                    filename=filename,
                    filesize_bytes=file.size,
                    filetype=item_type,
                    file_id=file.id,
                )
            except InvalidDebridFileException:
                continue

            df.download_url = self._build_ref(torrent_id, file.id)
            files.append(df)

        if not files:
            return None, "no valid files after validation", None

        return TorrentContainer(infohash=infohash, files=files), None, self._build_info(
            torrent_id, torrent
        )

    def add_torrent(self, infohash: str) -> int:
        """
        Add a magnet by infohash via /torrents/createtorrent.

        Returns:
            TorBox torrent id.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            TorBoxError: If the API returns a failing status.
        """

        assert self.api

        magnet_url = f"magnet:?xt=urn:btih:{infohash}"

        response = self.api.session.post(
            "v1/api/torrents/createtorrent",
            data={"magnet": magnet_url, "seed": 3, "allow_zip": "false"},
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise TorBoxError(self._handle_error(response))

        payload = response.json()

        if not isinstance(payload, dict) or not payload.get("success"):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise TorBoxError(detail or "Failed to add torrent to TorBox")

        data = payload.get("data") or {}
        torrent_id = data.get("torrent_id")

        if torrent_id is None:
            raise TorBoxError("No torrent_id returned by TorBox")

        return int(torrent_id)

    def select_files(self, torrent_id: int | str, file_ids: list[int]) -> None:
        """
        Select which files to download.

        Note: TorBox makes every file in a cached torrent available immediately, so no
        explicit selection step is required.
        """

        pass

    def get_torrent_info(self, torrent_id: int | str) -> TorrentInfo:
        """
        Get information about a specific torrent using its ID.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            TorBoxError: If the API returns a failing status.
        """

        torrent = self._fetch_torrent_raw(torrent_id)

        return self._build_info(torrent_id, torrent)

    def delete_torrent(self, torrent_id: int | str) -> None:
        """
        Delete a torrent on TorBox via /torrents/controltorrent.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            TorBoxError: If the API returns a failing status.
        """

        assert self.api

        response = self.api.session.post(
            "v1/api/torrents/controltorrent",
            json={"torrent_id": int(torrent_id), "operation": "delete"},
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise TorBoxError(self._handle_error(response))

    def unrestrict_link(self, link: str) -> UnrestrictedLink | None:
        """
        Resolve a synthetic TorBox reference into a fresh CDN download URL via
        /torrents/requestdl.

        Args:
            link: A synthetic reference produced by _build_ref (torbox://<torrent_id>/<file_id>).

        Returns:
            UnrestrictedLink, or None on error.
        """

        try:
            assert self.api

            parsed = self._parse_ref(link)

            if not parsed:
                logger.debug(f"Not a TorBox reference, cannot unrestrict: {link}")
                return None

            torrent_id, file_id = parsed

            response = self.api.session.get(
                "v1/api/torrents/requestdl",
                params={
                    "token": self.settings.api_key,
                    "torrent_id": torrent_id,
                    "file_id": file_id,
                    "redirect": "false",
                },
            )

            self._maybe_backoff(response)

            if not response.ok:
                return None

            payload = response.json()

            if not isinstance(payload, dict) or not payload.get("success"):
                return None

            url = payload.get("data")

            if not url or not isinstance(url, str):
                return None

            filename = url.split("/")[-1].split("?")[0]

            return UnrestrictedLink(
                download=url,
                filename=filename,
                filesize=0,
            )

        except Exception as e:
            logger.debug(f"TorBox unrestrict_link failed for {link}: {e}")
            return None

    def get_user_info(self) -> UserInfo | None:
        """
        Get normalized user information from TorBox.

        Returns:
            UserInfo with normalized fields, or None on error.
        """

        try:
            assert self.api

            response = self.api.session.get(
                "v1/api/user/me", params={"settings": "false"}
            )

            self._maybe_backoff(response)

            if not response.ok:
                logger.error(f"Failed to get user info: {self._handle_error(response)}")
                return None

            payload = response.json()

            if not isinstance(payload, dict) or not payload.get("success"):
                detail = payload.get("detail") if isinstance(payload, dict) else None
                logger.error(f"Failed to get user info: {detail or 'unknown error'}")
                return None

            user = TorBoxUser.model_validate(payload.get("data") or {})

            is_premium = user.plan > 0
            premium_expires_at = _parse_datetime(user.premium_expires_at)
            premium_days_left_val = None

            if is_premium and premium_expires_at:
                premium_days_left_val = max(
                    0, (premium_expires_at - datetime.now(tz=timezone.utc)).days
                )

            return UserInfo(
                service="torbox",
                username=user.email,
                email=user.email,
                user_id=user.id,
                premium_status="premium" if is_premium else "free",
                premium_expires_at=(
                    premium_expires_at.replace(tzinfo=None)
                    if premium_expires_at
                    else None
                ),
                premium_days_left=premium_days_left_val,
                total_downloaded_bytes=user.total_downloaded,
            )

        except Exception as e:
            logger.error(f"Error getting TorBox user info: {e}")
            return None
