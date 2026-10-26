"""ORM models. Importing this package registers every table on `Base.metadata`."""

from __future__ import annotations

from app.db import Base

__all__ = ["Base"]
