"""PostgreSQL-only compatibility facade for 龙虎榜 persistence."""

from database_pg import LonghubangDatabasePG, longhubang_db


def get_longhubang_db(db_path=None):
    """Return the process-wide PostgreSQL repository; ``db_path`` is obsolete."""
    del db_path
    return longhubang_db


LonghubangDatabase = LonghubangDatabasePG

__all__ = ["LonghubangDatabase", "LonghubangDatabasePG", "get_longhubang_db"]
