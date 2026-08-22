"""Compatibility import for the PostgreSQL portfolio repository.

The former SQLite implementation was retired. Call sites keep importing
``portfolio_db`` while all reads and writes use the single PostgreSQL backend.
"""

from portfolio_db_pg import PortfolioDBPG, portfolio_db

PortfolioDB = PortfolioDBPG

__all__ = ["PortfolioDB", "PortfolioDBPG", "portfolio_db"]
