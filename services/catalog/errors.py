"""Catalog-layer exceptions.

All failures are explicit: missing records raise NotFoundError, uniqueness
violations raise ConflictError. There is no tenant context to fail closed
on — New_sage is single-user with no auth (spec §13).
"""


class CatalogError(Exception):
    """Base class for all catalog errors."""


class NotFoundError(CatalogError):
    """Raised when a requested record does not exist."""


class ConflictError(CatalogError):
    """Raised on uniqueness violations (duplicate library name, etc.)."""
