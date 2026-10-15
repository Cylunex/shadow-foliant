"""Fund v2 routes; mounted before legacy /fund/{code}. Central auth remains authoritative."""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Any

router = APIRouter(prefix="/api/fund")


def call(fn, *args, **kwargs):
    try:
        import fund_db

        fund_db.init_db()
        from application.results import clean_json

        return {"ok": True, "data": clean_json(fn(*args, **kwargs))}
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Fund use case failed")
        return {"ok": False, "error": "服务暂不可用，请稍后重试"}


class LedgerRequest(BaseModel):
    rows: list[dict[str, Any]] = Field(max_length=2000)
    expected_version: str | None = None


@router.get("/workspace")
def workspace():
    from fund import fund_service as service, fund_ledger as ledger

    return call(
        lambda: {
            "overview": service.holdings_snapshot(),
            "experiments": [
                {
                    "code": r["code"],
                    "config": r["config"],
                    "created_at": r["created_at"],
                    "experiment_id": r["result"]["experiment_id"],
                    "result": {
                        k: r["result"].get(k)
                        for k in ("profit", "profit_pct", "max_drawdown", "status")
                    },
                }
                for r in ledger.list_saved("experiment")
            ],
            "profiles": ledger.list_saved("profile"),
            "targets": ledger.list_saved("targets"),
            "weekly_reviews": ledger.list_saved("weekly_review")
            + ledger.list_saved("weekly_review_pending"),
        }
    )


@router.post("/ledger/preview")
def ledger_preview(req: LedgerRequest):
    from fund.fund_ledger import preview

    return call(preview, req.rows)


@router.post("/ledger/commit")
def ledger_commit(req: LedgerRequest):
    from fund.fund_ledger import commit

    if not req.expected_version:
        return {"ok": False, "error": "请先预览并提交 expected_version"}
    return call(commit, req.rows, req.expected_version)


@router.post("/migration/preview")
def migration_preview(req: LedgerRequest):
    from fund.fund_ledger import migration_preview

    return call(migration_preview, req.rows)


@router.post("/migration/commit")
def migration_commit(req: LedgerRequest):
    from fund.fund_ledger import migrate

    if not req.expected_version:
        return {"ok": False, "error": "请先预览期初余额迁移"}
    return call(migrate, req.rows, req.expected_version)


@router.get("/ledger/positions")
def positions():
    from fund.fund_ledger import read

    return call(read)


@router.post("/experiments/{experiment_id}/replay")
def experiment_replay(experiment_id: str):
    from fund.fund_service import replay_experiment

    return call(replay_experiment, experiment_id)


@router.get("/research/{code}")
def research(code: str, refresh: bool = False):
    from fund.fund_service import research_context

    return call(research_context, code, refresh)


class ProfileRequest(BaseModel):
    fields: dict[str, Any]


@router.put("/profiles/{code}")
def configure_profile(code: str, req: ProfileRequest):
    from fund.fund_service import configure_profile

    return call(configure_profile, code, req.fields)


class TargetRequest(BaseModel):
    targets: dict[str, float]
    available_cash: float = Field(ge=0)
    fee_rules: dict[str, dict[str, Any]] = {}
    allow_redemptions: bool = False


@router.post("/rebalance-preview")
def rebalance(req: TargetRequest):
    from fund.fund_service import rebalance

    return call(rebalance, **req.model_dump())


@router.put("/targets")
def targets(req: TargetRequest):
    from fund.fund_service import rebalance
    from fund.fund_ledger import save

    def persist():
        result = rebalance(**req.model_dump())
        save("targets", "default", req.model_dump())
        return result

    return call(persist)


class OccurrenceRequest(BaseModel):
    status: str


@router.post("/occurrences/{occurrence_id}/status")
def occurrence_status(occurrence_id: str, req: OccurrenceRequest):
    from fund.fund_service import set_occurrence_status

    return call(set_occurrence_status, occurrence_id, req.status)


class ShareClassRequest(BaseModel):
    classes: list[dict[str, Any]]
    amount: float = Field(gt=0)
    holding_days: int = Field(ge=1)


@router.post("/share-class-costs")
def share_class_costs(req: ShareClassRequest):
    from fund.fund_fees import share_class_comparison

    return call(share_class_comparison, req.classes, req.amount, req.holding_days)


class PerformanceRequest(BaseModel):
    marks: list[dict[str, Any]] = Field(min_length=2, max_length=5000)


@router.post("/account-performance")
def account_performance(req: PerformanceRequest):
    from fund.fund_returns import portfolio_performance

    return call(portfolio_performance, req.marks)


@router.patch("/plan/{plan_id}/settings")
def plan_settings(plan_id: int, req: ProfileRequest):
    from fund.fund_service import configure_plan

    return call(configure_plan, plan_id, req.fields)
