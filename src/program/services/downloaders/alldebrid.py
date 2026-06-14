from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Generic, Literal, TypeVar, cast

from loguru import logger
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from program.services.downloaders.models import (
    DebridFile,
    InvalidDebridFileException,
    TorrentContainer,
    TorrentInfo,
    UserInfo,
    UnrestrictedLink,
)
from program.settings import settings_manager
from program.utils.request import CircuitBreakerOpen, SmartResponse, SmartSession
from program.media.item import ProcessedItemType

from .shared import DownloaderBase, premium_days_left


class AllDebridFile(BaseModel):
    """Represents a file in AllDebrid's torrent structure."""

    n: str  # Name
    s: int  # Size in bytes
    l: str  # Download link


class AllDebridDirectory(BaseModel):
    """Represents a directory in AllDebrid's torrent structure."""

    n: str  # Name
    e: list[AllDebridFile | AllDebridDirectory]  # Entries (files and subdirectories)


class AllDebridErrorDetail(BaseModel):
    code: str
    message: str


class AllDebridErrorResponse(BaseModel):
    """Represents an AllDebrid API error response."""

    status: Literal["error"]
    error: AllDebridErrorDetail


T = TypeVar("T", bound=BaseModel | None)


class AllDebridSuccessResponse(BaseModel, Generic[T]):
    """Represents a generic AllDebrid API success response."""

    status: Literal["success"]
    data: T


class AllDebridResponse(BaseModel, Generic[T]):
    """Union of AllDebrid success and error responses."""

    data: AllDebridErrorResponse | AllDebridSuccessResponse[T] = Field(
        discriminator="status"
    )


class AllDebridMagnet(BaseModel):
    """Represents magnet information returned by AllDebrid."""

    class MagnetInfo(BaseModel):
        id: int
        magnet: str
        hash: str
        name: str
        size: int
        ready: bool

    magnets: list[MagnetInfo]


class AllDebridUserResponse(BaseModel):
    """Represents user information returned by AllDebrid."""

    class UserData(BaseModel):
        username: str
        email: str
        is_premium: bool = Field(alias="isPremium")
        premium_until: int = Field(alias="premiumUntil")
        fidelity_points: int = Field(alias="fidelityPoints")

    user: UserData


class AllDebridLinkUnlockResponse(BaseModel):
    """Represents link unlock response from AllDebrid."""

    link: str
    filename: str
    filesize: int


class AllDebridMagnetStatusResponse(BaseModel):
    """Represents magnet status information returned by AllDebrid."""

    class MagnetInfo(BaseModel):
        id: int
        filename: str
        size: int
        status: str
        status_code: int = Field(alias="statusCode")
        upload_date: int = Field(alias="uploadDate")
        completion_date: int = Field(alias="completionDate")
        files: list[AllDebridFile | AllDebridDirectory] | None

    class MagnetErrorInfo(BaseModel):
        id: str
        error: AllDebridErrorDetail

    magnets: list[MagnetInfo | MagnetErrorInfo]

    @model_validator(mode="before")
    @classmethod
    def normalize(cls, values: Any) -> Any:
        if isinstance(values, dict):
            magnets = values.get("magnets")
            if isinstance(magnets, dict):
                values["magnets"] = [magnets]
        return values


class AllDebridError(Exception):
    """Base exception for AllDebrid related errors."""


class AllDebridAPI:
    """
    Minimal AllDebrid API client using SmartSession for retries, rate limits, and circuit breaker.
    """

    BASE_URL = "https://api.alldebrid.com/"

    def __init__(self, api_key: str, proxy_url: str | None = None) -> None:
        """
        Args:
            api_key: AllDebrid API key.
            proxy_url: Optional proxy URL used for both HTTP and HTTPS.
        """

        self.api_key = api_key
        self.proxy_url = proxy_url

        # AllDebrid rate limits: 12 req/sec and 600 req/min
        # Using conservative 10 req/sec (600 capacity)
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                "api.alldebrid.com": {
                    "rate": 10,
                    "capacity": 600,
                },
            },
            proxies=proxies,
            retries=2,
            backoff_factor=0.5,
        )

        self.session.headers.update({"Authorization": f"Bearer {api_key}"})


class AllDebridDownloader(DownloaderBase):
    """
    AllDebrid downloader with lean exception handling.

    Notes on failure & breaker behavior:
    - Network/transport failures are retried by SmartSession, then counted against the per-domain
      CircuitBreaker; once OPEN, SmartSession raises CircuitBreakerOpen before the request.
    - HTTP status codes are not exceptions; we check response.ok and map to messages via _handle_error(...).
    """

    def __init__(self) -> None:
        self.key = "alldebrid"
        self.settings = settings_manager.settings.downloaders.all_debrid
        self.api: AllDebridAPI | None = None
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

        self.api = AllDebridAPI(api_key=self.settings.api_key, proxy_url=proxy_url)

        return self._validate_premium()

    def _validate_settings(self) -> bool:
        """
        Returns:
            True when enabled and API key present; otherwise False.
        """

        if not self.settings.enabled:
            return False

        if not self.settings.api_key:
            logger.warning("AllDebrid API key is not set")
            return False

        return True

    def _validate_premium(self) -> bool:
        """
        Returns:
            True if premium is active; otherwise False.
        """
        try:
            user_info = self.get_user_info()

            if not user_info:
                logger.error("Failed to get AllDebrid user info")
                return False

            if user_info.premium_status != "premium":
                logger.error("AllDebrid premium membership required")
                return False

            if user_info.premium_expires_at:
                logger.info(premium_days_left(user_info.premium_expires_at))

            return True
        except Exception as e:
            logger.error(f"Failed to validate AllDebrid premium status: {e}")
            return False

    def _handle_error(self, response: SmartResponse) -> str:
        """
        Map HTTP status codes and AllDebrid error codes to error messages.
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
                return "AllDebrid server error"
            case _:
                data = (
                    AllDebridResponse[None]
                    .model_validate({"data": response.json()})
                    .data
                )

                # AllDebrid returns errors in data.error.message format
                if isinstance(data, AllDebridErrorResponse):
                    return data.error.message

                return f"HTTP {status}"

    def _maybe_backoff(self, response: SmartResponse) -> None:
        """
        Check if we should back off based on response.
        """

        if response.status_code == 429:
            logger.warning("AllDebrid rate limit hit, backing off")

    def get_instant_availability(
        self,
        infohash: str,
        item_type: ProcessedItemType,
        **kwargs: Any,
    ) -> TorrentContainer | None:
        """
        Attempt a quick availability check by adding the magnet to AllDebrid
        and checking if it's instantly available (already cached).

        AllDebrid doesn't have a separate cache check endpoint,
        so we add the magnet and check its status.
        """

        torrent_id: int | None = None

        try:
            torrent_id = self.add_torrent(infohash)
            container, reason, info = self._process_torrent(
                torrent_id, infohash, item_type
            )

            if container is None and reason:
                logger.debug(f"Availability check failed [{infohash}]: {reason}")

                # Failed validation - delete the torrent
                if torrent_id:
                    try:
                        self.delete_torrent(torrent_id)
                    except Exception as e:
                        logger.debug(
                            f"Failed to delete failed torrent {torrent_id}: {e}"
                        )

                return None

            # Success - cache torrent_id AND info in container to avoid re-adding/re-fetching during download
            if container:
                container.torrent_id = torrent_id
                container.torrent_info = info

            return container

        except CircuitBreakerOpen:
            logger.debug(f"Circuit breaker OPEN for AllDebrid; skipping {infohash}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            raise
        except AllDebridError as e:
            logger.warning(f"Availability check failed [{infohash}]: {e}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None
        except InvalidDebridFileException as e:
            logger.debug(
                f"Availability check failed [{infohash}]: Invalid debrid file(s) - {e}"
            )

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None
        except Exception as e:
            logger.debug(f"Availability check failed [{infohash}]: {e}")

            if torrent_id:
                try:
                    self.delete_torrent(torrent_id)
                except Exception:
                    pass

            return None

    def _fetch_magnet_raw(
        self,
        torrent_id: int | str,
    ) -> "AllDebridMagnetStatusResponse.MagnetInfo":
        """
        Single v4.1/magnet/status call — shared by _process_torrent and get_torrent_info.

        Raises:
            AllDebridError: on API or parse failure.
        """

        assert self.api

        response = self.api.session.post(
            "v4.1/magnet/status",
            data={"id": str(torrent_id)},
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise AllDebridError(self._handle_error(response))

        data = (
            AllDebridResponse[AllDebridMagnetStatusResponse]
            .model_validate({"data": response.json()})
            .data
        )

        if isinstance(data, AllDebridErrorResponse):
            raise AllDebridError(
                f"Invalid response format from AllDebrid: {data.error.message}"
            )

        magnets = data.data.magnets

        if not magnets:
            raise AllDebridError(f"Magnet {torrent_id} not found")

        [magnet_data] = magnets

        if isinstance(magnet_data, AllDebridMagnetStatusResponse.MagnetErrorInfo):
            raise AllDebridError(
                f"Error getting magnet info: {magnet_data.error.message}"
            )

        return magnet_data

    def _files_from_magnet(
        self,
        files: "list[AllDebridFile | AllDebridDirectory] | None",
    ) -> list["AllDebridFile"] | None:
        """
        Flatten a magnet's file tree into a list of AllDebridFile objects.

        Handles both flat (single-file torrent) and nested (season pack) structures.
        """

        if not files:
            return None

        all_files = list[AllDebridFile]()

        for entry in files:
            if isinstance(entry, AllDebridFile):
                all_files.append(entry)
            else:
                self._add_link_to_files_recursive(entry.e, "", all_files)

        return all_files or None

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

        magnet_data = self._fetch_magnet_raw(torrent_id)

        # Status codes: 0=In Queue, 1=Downloading, 2=Compressing, 3=Uploading, 4=Ready
        if magnet_data.status != "Ready":
            return None, f"Not instantly available (status={magnet_data.status})", None

        files_data = self._files_from_magnet(magnet_data.files)

        if not files_data:
            return None, "no files present in the torrent", None

        files = list[DebridFile]()
        self._extract_files_recursive(files_data, item_type, files, infohash)

        if not files:
            return None, "no valid files after validation", None

        upload_date = magnet_data.upload_date
        completion_date = magnet_data.completion_date

        info = TorrentInfo(
            id=torrent_id,
            name=magnet_data.filename,
            status=magnet_data.status,
            infohash=None,
            bytes=magnet_data.size,
            created_at=datetime.fromtimestamp(upload_date) if upload_date else None,
            completed_at=datetime.fromtimestamp(completion_date) if completion_date else None,
            progress=100.0 if magnet_data.status_code == 4 else 0.0,
            files={},
            links=[],
        )

        return TorrentContainer(infohash=infohash, files=files), None, info

    def _add_link_to_files_recursive(
        self,
        files: list[AllDebridFile | AllDebridDirectory],
        download_link: str,
        result: list[AllDebridFile],
    ) -> None:
        """
        Recursively process files/folders and add download link to actual files.

        For season packs, AllDebrid returns nested structure:
        - files[0].n = folder name (e.g., "Show.S01.1080p")
        - files[0].e = array of episode files
        - files[0].e[0].n = episode filename
        - files[0].e[0].s = episode size

        We need to find the actual files (those with 's' field) and add the 'l' field.
        """

        for file_obj in files:
            if isinstance(file_obj, AllDebridDirectory):
                # This is a folder, recurse into it
                self._add_link_to_files_recursive(
                    files=file_obj.e,
                    download_link=download_link,
                    result=result,
                )
            else:
                result.append(
                    AllDebridFile(
                        n=file_obj.n,
                        s=file_obj.s,
                        l=(
                            # Use the file's own link if present, otherwise inherit from parent
                            file_obj.l
                            or download_link
                        ),
                    )
                )

    def _extract_files_recursive(
        self,
        file_list: list[AllDebridFile],
        item_type: ProcessedItemType,
        files: list[DebridFile],
        infohash: str,
        path_prefix: str = "",
    ) -> None:
        """
        Recursively extract files from AllDebrid's nested file structure.

        AllDebrid returns files with:
        - 'n' (name): filename or folder name
        - 's' (size): file size in bytes (only for files, not folders)
        - 'l' (link): download link (only for files, not folders)
        - 'e' (entries): array of nested files/folders (only for folders)
        """

        for file_entry in file_list:
            name = file_entry.n
            current_path = f"{path_prefix}/{name}" if path_prefix else name

            link = file_entry.l
            size = file_entry.s

            if not link:
                continue

            try:
                df = DebridFile.create(
                    path=current_path,
                    filename=name,
                    filesize_bytes=size,
                    filetype=item_type,
                    file_id=None,
                )

                df.download_url = link
                files.append(df)
            except InvalidDebridFileException:
                pass

    def add_torrent(self, infohash: str) -> int:
        """
        Add a magnet by infohash.

        Returns:
            AllDebrid magnet id.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            AllDebridError: If the API returns a failing status.
        """

        assert self.api

        magnet_url = f"magnet:?xt=urn:btih:{infohash}"

        response = self.api.session.post(
            "v4/magnet/upload",
            data={
                "magnets[]": magnet_url,
            },
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise AllDebridError(self._handle_error(response))

        # AllDebrid API returns {status: "success", data: {magnets: [{id: ...}]}}
        try:
            data = (
                AllDebridResponse[AllDebridMagnet]
                .model_validate({"data": response.json()})
                .data
            )
        except ValidationError as e:
            raise AllDebridError(f"Invalid response format from AllDebrid: {e}")

        if isinstance(data, AllDebridErrorResponse):
            raise AllDebridError(data.error.message)

        magnets = data.data.magnets

        if not magnets:
            raise AllDebridError("No magnet ID returned by AllDebrid")

        [magnet_info] = magnets

        magnet_id = magnet_info.id

        if not magnet_id:
            raise AllDebridError("No magnet ID in response")

        return int(magnet_id)

    def select_files(self, torrent_id: int | str, file_ids: list[int]) -> None:
        """
        Select which files to download from the magnet.

        Note: AllDebrid doesn't require explicit file selection.
        Files are automatically available once the magnet is ready.
        """

        pass

    def get_torrent_info(self, torrent_id: int | str) -> TorrentInfo:
        """
        Get information about a specific magnet using its ID.

        Args:
            torrent_id: ID of the magnet to get info for.

        Returns:
            TorrentInfo: Current information about the magnet.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            AllDebridError: If the API returns a failing status.
        """

        magnet_data = self._fetch_magnet_raw(torrent_id)

        upload_date = magnet_data.upload_date
        completion_date = magnet_data.completion_date

        return TorrentInfo(
            id=torrent_id,
            name=magnet_data.filename,
            status=magnet_data.status,
            infohash=None,
            bytes=magnet_data.size,
            created_at=datetime.fromtimestamp(upload_date) if upload_date else None,
            completed_at=(
                datetime.fromtimestamp(completion_date) if completion_date else None
            ),
            progress=100.0 if magnet_data.status_code == 4 else 0.0,
            files={},
            links=[],
        )

    def delete_torrent(self, torrent_id: str | int) -> None:
        """
        Delete a magnet on AllDebrid.

        Raises:
            CircuitBreakerOpen: If the per-domain breaker is OPEN.
            AllDebridError: If the API returns a failing status.
        """

        assert self.api

        # AllDebrid API expects ID as string
        response = self.api.session.post(
            url="v4/magnet/delete",
            data={
                "id": str(torrent_id),
            },
        )

        self._maybe_backoff(response)

        if not response.ok:
            raise AllDebridError(self._handle_error(response))

    def unrestrict_link(self, link: str) -> UnrestrictedLink | None:
        """
        Unrestrict a link using AllDebrid.

        Args:
            link: The link to unrestrict.

        Returns:
            UnrestrictedLink, or None on error.
        """

        try:
            assert self.api

            response = self.api.session.get(
                "v4/link/unlock",
                params={
                    "link": link,
                },
            )

            self._maybe_backoff(response)

            if not response.ok:
                return None

            data = (
                AllDebridResponse[AllDebridLinkUnlockResponse]
                .model_validate({"data": response.json()})
                .data
            )

            if isinstance(data, AllDebridErrorResponse):
                return None

            link_data = data.data
            unrestricted_url = link_data.link

            if not unrestricted_url:
                return None

            return UnrestrictedLink(
                download=unrestricted_url,
                filename=link_data.filename,
                filesize=link_data.filesize,
            )

        except Exception:
            return None

    def get_user_info(self) -> UserInfo | None:
        """
        Get normalized user information from AllDebrid.

        Returns:
            UserInfo with normalized fields, or None on error.
        """

        try:
            assert self.api

            response = self.api.session.get("v4/user")

            self._maybe_backoff(response)

            if not response.ok:
                logger.error(f"Failed to get user info: {self._handle_error(response)}")
                return None

            data = (
                AllDebridResponse[AllDebridUserResponse]
                .model_validate({"data": response.json()})
                .data
            )

            if isinstance(data, AllDebridErrorResponse):
                logger.error(f"Failed to get user info: {data.error.message}")
                return None

            user_data = data.data.user

            if not user_data:
                return None

            # Parse premium expiration
            premium_expires_at = None
            premium_days_left_val = None
            is_premium = user_data.is_premium

            if is_premium:
                premium_until = user_data.premium_until

                if premium_until > 0:
                    premium_expires_at = datetime.fromtimestamp(
                        premium_until, tz=timezone.utc
                    )
                    premium_days_left_val = max(
                        0, (premium_expires_at - datetime.now(tz=timezone.utc)).days
                    )

            return UserInfo(
                service="alldebrid",
                username=user_data.username,
                email=user_data.email,
                user_id=user_data.username,
                premium_status="premium" if is_premium else "free",
                premium_expires_at=premium_expires_at,
                premium_days_left=premium_days_left_val,
                points=user_data.fidelity_points,
            )

        except Exception as e:
            logger.error(f"Error getting AllDebrid user info: {e}")
            return None
