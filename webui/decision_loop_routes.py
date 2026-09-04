"""Research/public-private separation for decision loop views."""
from fastapi import Query, Request


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
