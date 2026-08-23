"""Process-local composition root for application services."""

from __future__ import annotations

import os
import threading

from application.run_repository import RunRepository
from application.services import (
    BacktestRunService,
    DataQualityService,
    MarketOverviewService,
    ResearchRunQueryService,
    RunCoordinator,
    SecurityResearchService,
    SelectionRunService,
    TradeEntryService,
)


class ApplicationServices:
    def __init__(self, repository: RunRepository | None = None, *, recover: bool = False) -> None:
        self.runs = repository or RunRepository(
            ensure_schema=os.getenv("FOLIANT_RUNTIME_DDL", "false").lower() == "true"
        )
        self.coordinator = RunCoordinator(self.runs, recover=recover)
        self.market = MarketOverviewService()
        self.data_quality = DataQualityService()
        self.security_research = SecurityResearchService(self.coordinator)
        self.selection = SelectionRunService(self.coordinator)
        self.backtest = BacktestRunService(self.coordinator)
        self.run_query = ResearchRunQueryService(self.runs)
        self.trade_entry = TradeEntryService(self.runs)


_services: ApplicationServices | None = None
_lock = threading.Lock()


def get_application_services() -> ApplicationServices:
    global _services
    if _services is not None:
        return _services
    with _lock:
        if _services is None:
            _services = ApplicationServices()
    return _services


def set_application_services(value: ApplicationServices | None) -> None:
    """Test seam; production composition remains lazy and process-local."""
    global _services
    _services = value
