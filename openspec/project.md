# shadow-foliant specification context

`shadow-foliant` is an Agent-first A-share research system. Browser access is an auxiliary control
surface, while MCP and scheduled jobs remain first-class process-local capabilities.

Cross-cutting invariants:

- External market data is accessed through `data/datahub.py`.
- Browser identity, service identity, and local stdio MCP identity are separate trust domains.
- Global portfolio, trade, environment, job, and signal state is administrator-only until resources
  are explicitly partitioned by `shadow_user_id`.
- Production values live outside Git. Repository examples use `example.com` and replacement markers.
- LLM prompts, responses, financial data, credentials, cookies, and tokens are not telemetry.
