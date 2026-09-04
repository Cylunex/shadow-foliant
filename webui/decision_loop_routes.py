"""Research/public-private separation for decision loop views."""
from fastapi import Body, Query, Request, HTTPException
from functools import wraps


def browser_errors(fn):
    @wraps(fn)
    def checked(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            code = str(exc).strip("'") if isinstance(exc, ValueError) else "invalid_request_fields"
            conflict = any(word in code for word in ("stale", "conflict", "revision"))
            raise HTTPException(status_code=409 if conflict else 422, detail=code) from exc
    return checked


def register_decision_loop_routes(app, *, agent_result, agent_error, browser_ok):
    def result(value, summary, resource):
        from application.results import provenance, tool_result
        status = "missing" if value.get("status") == "missing" else "degraded" if value.get("status") == "stale" or value.get("blockers") else "complete"
        return agent_result(tool_result(summary=summary, resource_uri=resource, status=status,
                            provenance_value=provenance(run_id="decision-loop"), warnings=value.get("blockers") or [],
                            data=value, model_payload=value), max_bytes=131072)

    @app.get("/api/research/decision-loop")
    def browser_loop():
        from application.decision_loop import DecisionLoopService
        return browser_ok(DecisionLoopService().dashboard())

    @app.get("/api/research/cases")
    def browser_cases():
        from application.research_cases import ResearchCases
        from data.research_store import ResearchStore
        return browser_ok(ResearchCases(ResearchStore()).view(owner="portfolio-primary"))

    @app.post("/api/research/thesis/draft")
    @browser_errors
    def browser_thesis_draft(payload: dict = Body(...)):
        from application.research_cases import ResearchCases
        from data.research_store import ResearchStore
        return browser_ok(ResearchCases(ResearchStore()).draft(payload["symbol"], owner="portfolio-primary",
            text=payload["text"], claims=payload.get("claims", []), expected_revision=payload.get("expected_revision", 0)))

    @app.post("/api/research/thesis/lock")
    @browser_errors
    def browser_thesis_lock(payload: dict = Body(...)):
        from application.research_cases import ResearchCases
        from data.research_store import ResearchStore
        if payload.get("confirm") is not True:
            raise ValueError("explicit_thesis_confirmation_required")
        return browser_ok(ResearchCases(ResearchStore()).lock(payload["symbol"], owner="portfolio-primary",
            draft_revision=payload["draft_revision"], human_confirmed=True))

    @app.post("/api/research/cases/acknowledge")
    @browser_errors
    def browser_case_acknowledge(payload: dict = Body(...)):
        from application.research_cases import ResearchCases
        from data.research_store import ResearchStore
        if payload.get("confirm") is not True:
            raise ValueError("explicit_review_confirmation_required")
        return browser_ok(ResearchCases(ResearchStore(ensure_schema=False)).acknowledge(
            payload["event_id"], owner="portfolio-primary", note=payload["note"], human_confirmed=True))

    @app.get("/api/portfolio/account-facts")
    def browser_account_facts():
        from application.account_reconciliation import AccountReconciliation
        from data.research_store import ResearchStore
        return browser_ok(AccountReconciliation(ResearchStore(ensure_schema=False)).view(owner="portfolio-primary"))

    @app.get("/api/machine/v1/agent/research/cases", operation_id="get_agent_research_cases")
    def agent_cases():
        try:
            from application.research_cases import ResearchCases
            from data.research_store import ResearchStore
            return result(ResearchCases(ResearchStore()).view(), "个股长期研究与待复核问题；不含私人论点",
                          "shadow://foliant/research/cases")
        except Exception as exc:
            return agent_error(exc)

    @app.post("/api/portfolio/account-facts/preview")
    @browser_errors
    def browser_account_facts_preview(payload: dict = Body(...)):
        from application.account_reconciliation import AccountReconciliation
        from data.research_store import ResearchStore
        from portfolio_db import portfolio_db
        return browser_ok(AccountReconciliation(ResearchStore()).preview(payload["rows"], owner="portfolio-primary",
            watermark=portfolio_db.action_preview_context()["watermark"]))

    @app.post("/api/portfolio/account-facts/confirm")
    @browser_errors
    def browser_account_facts_confirm(payload: dict = Body(...)):
        from application.account_reconciliation import AccountReconciliation
        from data.research_store import ResearchStore
        from portfolio_db import portfolio_db
        if payload.get("confirm") is not True:
            raise ValueError("explicit_account_confirmation_required")
        return browser_ok(AccountReconciliation(ResearchStore()).confirm(payload["preview_id"], owner="portfolio-primary",
            watermark=portfolio_db.action_preview_context()["watermark"]))

    @app.get("/api/machine/v1/agent/decision-loop", operation_id="get_agent_decision_loop")
    def agent_loop():
        try:
            from application.decision_loop import DecisionLoopService
            return result(DecisionLoopService().dashboard(), "研究证据、实验与模拟账本；不含私人持仓",
                          "shadow://foliant/decision-loop")
        except Exception as exc:
            return agent_error(exc)

    @app.get("/api/portfolio/action-plan")
    def browser_plan(available_cash: float | None = Query(None, ge=0, le=1e10), allow_add: bool = False):
        from application.account_preview import preview_account
        return browser_ok(preview_account(owner_id="portfolio-primary", available_cash=available_cash, allow_add=allow_add))

    @app.get("/api/machine/v1/agent/portfolio/action-plan", operation_id="get_agent_account_action_plan")
    def agent_plan(request: Request, available_cash: float | None = Query(None, ge=0, le=1e10), allow_add: bool = False):
        try:
            from application.account_preview import preview_account
            owner = str(request.state.agent_identity.agent_id)
            value = preview_account(owner_id=owner, available_cash=available_cash, allow_add=allow_add)
            return result(value, value.get("summary", "账户行动预览"), "shadow://foliant/portfolios/primary/action-plan")
        except Exception as exc:
            return agent_error(exc)

    @app.get("/api/machine/v1/agent/portfolio/books", operation_id="get_agent_account_books")
    def agent_books(days: int = Query(30, ge=1, le=365)):
        try:
            from application.account_preview import account_books
            return result(account_books(days), "真实证券子账户收益，不与模型收益混算",
                          "shadow://foliant/portfolios/primary/books")
        except Exception as exc:
            return agent_error(exc)
