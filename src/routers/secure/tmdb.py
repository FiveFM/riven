"""TMDB browsing endpoints.

Thin proxy over the TMDB API so the frontend can power a "Discover" UI
(trending / popular / search / discover-by-genre) and resolve the data it
needs to request items (TVDB id + season list for shows) without ever
holding a TMDB token itself. All responses are the raw TMDB JSON payload.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query

from program.apis.tmdb_api import TMDBApi, TMDBApiError

router = APIRouter(
    prefix="/tmdb",
    tags=["tmdb"],
    responses={404: {"description": "Not found"}},
)

# Single shared client (just holds a rate-limited session; no I/O on init).
tmdb_api = TMDBApi()


@router.get(
    "/trending",
    summary="Trending media",
    operation_id="tmdb_trending",
)
async def trending(
    media_type: Annotated[
        Literal["all", "movie", "tv"],
        Query(description="Media type"),
    ] = "all",
    window: Annotated[
        Literal["day", "week"],
        Query(description="Trending window"),
    ] = "week",
    page: Annotated[int, Query(description="Page number", ge=1)] = 1,
) -> dict[str, Any]:
    try:
        return tmdb_api.trending(media_type, window, page)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/popular",
    summary="Popular media",
    operation_id="tmdb_popular",
)
async def popular(
    media_type: Annotated[
        Literal["movie", "tv"],
        Query(description="Media type"),
    ],
    page: Annotated[int, Query(description="Page number", ge=1)] = 1,
) -> dict[str, Any]:
    try:
        return tmdb_api.popular(media_type, page)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/search",
    summary="Search movies and TV",
    operation_id="tmdb_search",
)
async def search(
    query: Annotated[str, Query(description="Search query", min_length=1)],
    page: Annotated[int, Query(description="Page number", ge=1)] = 1,
) -> dict[str, Any]:
    try:
        return tmdb_api.search_multi(query, page)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/discover",
    summary="Discover media by filters",
    operation_id="tmdb_discover",
)
async def discover(
    media_type: Annotated[
        Literal["movie", "tv"],
        Query(description="Media type"),
    ],
    page: Annotated[int, Query(description="Page number", ge=1)] = 1,
    genre: Annotated[
        str | None,
        Query(description="Comma-separated TMDB genre id(s)"),
    ] = None,
    year: Annotated[int | None, Query(description="Release/air year")] = None,
    sort_by: Annotated[
        str | None,
        Query(description="TMDB sort_by, e.g. popularity.desc"),
    ] = None,
) -> dict[str, Any]:
    try:
        return tmdb_api.discover(media_type, page, genre, year, sort_by)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/genres",
    summary="Genre list",
    operation_id="tmdb_genres",
)
async def genres(
    media_type: Annotated[
        Literal["movie", "tv"],
        Query(description="Media type"),
    ],
) -> dict[str, Any]:
    try:
        return tmdb_api.genres(media_type)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/tv/{tmdb_id}",
    summary="TV details with seasons, external IDs, cast and recommendations",
    description="Returns TV details including seasons, external_ids (TVDB id), credits and recommendations.",
    operation_id="tmdb_tv_details",
)
async def tv_details(
    tmdb_id: Annotated[str, Path(description="TMDB TV id")],
) -> dict[str, Any]:
    try:
        return tmdb_api.tv_details(tmdb_id)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/movie/{tmdb_id}",
    summary="Movie details with external IDs, cast and recommendations",
    description="Returns movie details including external_ids, credits, recommendations and belongs_to_collection.",
    operation_id="tmdb_movie_details",
)
async def movie_details(
    tmdb_id: Annotated[str, Path(description="TMDB movie id")],
) -> dict[str, Any]:
    try:
        return tmdb_api.movie_details(tmdb_id)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/collection/{collection_id}",
    summary="Collection (franchise) details",
    description="Returns a collection's movie parts — the prequels/sequels of a franchise.",
    operation_id="tmdb_collection_details",
)
async def collection_details(
    collection_id: Annotated[str, Path(description="TMDB collection id")],
) -> dict[str, Any]:
    try:
        return tmdb_api.collection_details(collection_id)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e


@router.get(
    "/find/{external_source}/{external_id}",
    summary="Resolve an external id to TMDB",
    description="Resolve an external id (e.g. tvdb_id) to its TMDB movie/tv results.",
    operation_id="tmdb_find",
)
async def find(
    external_source: Annotated[
        str,
        Path(description="External source, e.g. tvdb_id, imdb_id"),
    ],
    external_id: Annotated[str, Path(description="The external id value")],
) -> dict[str, Any]:
    try:
        return tmdb_api.find(external_source, external_id)
    except TMDBApiError as e:
        raise HTTPException(status_code=502, detail=f"TMDB error: {e}") from e
