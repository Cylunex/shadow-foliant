# Notification requirements

## Critical only monitor outbound policy

- The system MUST keep ordinary high-frequency monitoring silent when no certified critical or market-risk event exists. This restriction MUST NOT suppress native morning, selection or portfolio reports; normal reports retain channel routing, message archiving and idempotency. Monitoring sources MUST NOT bypass silence by relabeling messages as reports.
- Protected scheduled reports MUST retain their separate slot certification and delivery contract; disabling their external scheduler MUST NOT disable native task reports.
- An exit alert MUST require a final hard risk sell, a new crossing of the unchanged stop after two distinct verified recovery quotes, same batch current market data, a current same run plan with an explicit expiry, compatible quote and plan price basis, current broker sellable quantity, and verified tradeability.
- A buy alert MUST remain closed until an authoritative highest severity buy gate exists with current budget, risk, plan, tradeability and price evidence.
- The system MUST baseline already breached positions on rollout and plan replacement. Invalid or boundary quotes MUST NOT advance recovery. A guarded final hold MUST NOT become a sell alert.
- A critical event MUST carry a stable episode key independent of minor price or body changes, use the message archive before delivery, target only QQ, suppress attempts to move the same event to another channel, and never retry when the outcome is uncertain.
- A critical exit MUST use a provider timestamp for its quote; a batch retrieval timestamp alone MUST NOT arm or trigger the event.
- Protected snapshots MUST expose bounded policy version, active level, gate status, eligible new event count, criteria and silent reasons without requiring an outbound message.

### Scenarios

1. No verified event: monitoring remains silent while native morning, selection and portfolio reports can deliver and archive normally; repeated calls with the same idempotency key do not send twice.
2. One newly crossed verified hard stop: exactly one bounded alert contains name and code, final action, current price, stop, reason, quote time and invalidation condition.
3. Repeated polling, changed quote text or another channel: no second attempt for the same event episode.
4. Existing breach at rollout, a revised stop, a guarded hold, a missing sellable balance or an unknown price adjustment: no alert.
