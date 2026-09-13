"""Provider-local Atlas search contracts, retrieval adapters, and pure matching."""

from app.search.errors import SearchPoolCapacityError
from app.search.service import SearchRepository, search_assets

__all__ = ["SearchPoolCapacityError", "SearchRepository", "search_assets"]
