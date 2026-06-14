"""TMDB API client"""

from program.utils.request import SmartSession

TMDB_READ_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJlNTkxMmVmOWFhM2IxNzg2Zjk3ZTE1NWY1YmQ3ZjY1MSIsInN1YiI6IjY1M2NjNWUyZTg5NGE2MDBmZjE2N2FmYyIsInNjb3BlcyI6WyJhcGlfcmVhZCJdLCJ2ZXJzaW9uIjoxfQ.xrIXsMFJpI1o1j5g2QpQcFP1X3AfRjFA5FlBFO5Naw8"  # noqa: S105


class TMDBApiError(Exception):
    """Base exception for TMDB API related errors"""


class TMDBApi:
    """Handles TMDB API communication"""

    def __init__(self):
        self.BASE_URL = "https://api.themoviedb.org/3"

        self.session = SmartSession(
            base_url=self.BASE_URL,
            rate_limits={
                # 40 requests per second
                # https://developer.themoviedb.org/docs/rate-limiting
                "api.themoviedb.org": {
                    "rate": 40,
                    "capacity": 1000,
                }
            },
            retries=2,
            backoff_factor=0.3,
        )

        self.session.headers.update(
            {
                "Authorization": f"Bearer {TMDB_READ_ACCESS_TOKEN}",
            }
        )

    def get_from_external_id(self, external_source: str, external_id: str):
        """Get TMDB item from external ID"""

        response = self.session.get(
            f"find/{external_id}?external_source={external_source}"
        )

        from schemas.tmdb import FindById200Response

        return FindById200Response.from_dict(response.json())

    # ------------------------------------------------------------------
    # Browse / discovery endpoints
    #
    # These return the raw TMDB JSON payload (a dict) so the frontend can
    # consume the standard TMDB shape (poster_path, title/name, etc.)
    # directly. They power the "Discover" browsing UI.
    # ------------------------------------------------------------------

    def trending(
        self, media_type: str = "all", window: str = "week", page: int = 1
    ) -> dict:
        """Trending movies/tv. media_type: all|movie|tv, window: day|week."""

        response = self.session.get(
            f"trending/{media_type}/{window}",
            params={"page": page},
        )

        return response.json()

    def popular(self, media_type: str, page: int = 1) -> dict:
        """Popular movies or tv. media_type: movie|tv."""

        response = self.session.get(
            f"{media_type}/popular",
            params={"page": page},
        )

        return response.json()

    def search_multi(self, query: str, page: int = 1) -> dict:
        """Search across movies and tv (people are filtered out client-side)."""

        response = self.session.get(
            "search/multi",
            params={"query": query, "page": page, "include_adult": "false"},
        )

        return response.json()

    def discover(
        self,
        media_type: str,
        page: int = 1,
        with_genres: str | None = None,
        year: int | None = None,
        sort_by: str | None = None,
    ) -> dict:
        """Discover movies or tv with optional genre/year/sort filters."""

        params: dict[str, str | int] = {
            "page": page,
            "include_adult": "false",
        }

        if with_genres:
            params["with_genres"] = with_genres
        if sort_by:
            params["sort_by"] = sort_by
        if year:
            # TMDB uses different year params for movie vs tv
            params["primary_release_year" if media_type == "movie" else "first_air_date_year"] = year

        response = self.session.get(f"discover/{media_type}", params=params)

        return response.json()

    def genres(self, media_type: str) -> dict:
        """Genre list for movie or tv."""

        response = self.session.get(f"genre/{media_type}/list")

        return response.json()

    def tv_details(self, tv_id: str | int) -> dict:
        """TV details with seasons, external IDs (TVDB id), cast and recommendations."""

        response = self.session.get(
            f"tv/{tv_id}",
            params={"append_to_response": "external_ids,credits,recommendations"},
        )

        return response.json()

    def movie_details(self, movie_id: str | int) -> dict:
        """Movie details with external IDs, cast and recommendations.

        ``belongs_to_collection`` in the payload links to the franchise
        (prequels/sequels), fetched separately via ``collection_details``.
        """

        response = self.session.get(
            f"movie/{movie_id}",
            params={"append_to_response": "external_ids,credits,recommendations"},
        )

        return response.json()

    def collection_details(self, collection_id: str | int) -> dict:
        """Collection (franchise) details, including its movie ``parts``."""

        response = self.session.get(f"collection/{collection_id}")

        return response.json()

    def find(self, external_source: str, external_id: str) -> dict:
        """Resolve an external id (e.g. tvdb_id) to TMDB results."""

        response = self.session.get(
            f"find/{external_id}",
            params={"external_source": external_source},
        )

        return response.json()

    def get_movie_details_with_external_ids_and_release_dates(self, movie_id: str):
        """Get movie details with external IDs and release dates appended"""

        response = self.session.get(
            f"movie/{movie_id}?append_to_response=external_ids,release_dates"
        )

        from schemas.tmdb import (
            MovieDetails200Response,
            MovieExternalIds200Response,
            MovieReleaseDates200Response,
        )

        class MovieDetailsWithExtras(MovieDetails200Response):
            external_ids: MovieExternalIds200Response
            release_dates: MovieReleaseDates200Response

        data = response.json()

        movie_details = MovieDetails200Response.from_dict(data)
        external_ids = MovieExternalIds200Response.from_dict(data.get("external_ids"))
        release_dates = MovieReleaseDates200Response.from_dict(
            data.get("release_dates")
        )

        assert movie_details
        assert external_ids
        assert release_dates

        return MovieDetailsWithExtras.model_validate(
            {
                **movie_details.model_dump(),
                "external_ids": external_ids,
                "release_dates": release_dates,
            }
        )
