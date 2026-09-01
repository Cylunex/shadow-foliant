# Foliant Personal Portfolio

Use Foliant as the source of truth for the user's delegated primary securities portfolio.

## Reads

- Use `foliant.portfolio.summary` for current holdings and cost-basis context.
- Use `foliant.trades.list` for imported executions. Distinguish `import_date` from
  `trade_date`; a historical execution may be imported today.
- Keep execution facts, portfolio snapshots, and research conclusions separate.

## Trade-import capture

When the user explicitly asks to save or import executed trades, return a Nexus Proposal with:

- domain: `foliant`
- intent: `foliant.trade.import`
- risk: `medium`
- fields:
  - `tradesJson`: a JSON array of trade objects, encoded as one JSON string
  - `updatePosition`: `true` unless the user explicitly asks for ledger-only import

Each trade should preserve the available execution facts: `code`, `name`, `trade_type`
(`买入` or `卖出`), `quantity`, `price`, `trade_time`, and optional `amount`, `commission`,
`tax`, `note`, `order_id`, `broker_execution_id`, and `account_ref`. Do not invent missing
fees or broker identifiers. Foliant resolves an unambiguous name to a code in batch.

The Nexus Host performs the hidden preview and commit transaction. A clear user request to import
completed executions is the L2 authorization for this personal record import; this capability never
places, cancels, or modifies broker orders. If fields are incomplete or the preview detects an
oversell, keep the Proposal pending and ask only for the missing fact.

After commit, read the imported records back and report both the imported count and whether positions
were updated. Never claim success without a `shadow://foliant/trade-imports/...` receipt.
