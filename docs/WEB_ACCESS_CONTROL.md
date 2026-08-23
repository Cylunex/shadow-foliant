# Stock Web access control

The executable route matrix is `webui/access_control.py`. Every FastAPI method/path template is
listed there exactly once and application import fails if a new route is not classified.

| Class | Identity | Current route areas |
| --- | --- | --- |
| Public | none | `GET /healthz`, safe `GET /api/health`, OIDC login and callback |
| Readiness | Agent Bearer, audience `foliant`, scope `stock.read` | detailed readiness endpoints |
| User | OIDC session with `stock-users` | quotes, stock/fund research, market data, screeners, backtests, macro, aggregate evaluation and strategy deployment views |
| Administrator | user plus `stock-admins` | all portfolio and trade data, fund holdings/plans/transactions, monitors, workflows, briefing with holdings, signals, jobs/task runs, cockpit, LLM usage and environment configuration |
| Machine read | Agent Bearer, audience `foliant`, scope `stock.read` | runtime health and Agent cockpit machine endpoints |
| Machine research | Agent Bearer, scope plus exact Foliant capability grant | versioned Agent research, selection, backtest and Run endpoints |

Unknown API and authentication paths are denied. Browser sessions are never accepted by machine
routes, and Agent Bearers never create browser sessions. Unsafe browser methods additionally require
the configured canonical Origin. Administrator writes emit an allowlisted audit event containing only
request ID, actor ID, method, route template, policy, result, and status code; bodies and route values
are never logged.

Versioned Agent routes require both their coarse `stock.read`/`stock.research` scope and the exact
`foliant.*` capability listed in the Shadow Agent registry. Proxy source addresses and loopback alone
never authorize detailed readiness or machine data.

The first integration phase does not claim multi-tenancy. Existing global financial and operational
records stay administrator-only until each resource has an ownership model and server-side
resource-level authorization.
