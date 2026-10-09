# Design

The monitor classifies a critical exit only from a final `hard_risk` sell at the current authoritative stop. It requires a current provider quote, a same run manifest plan with an explicit unexpired validity timestamp, matching price adjustment basis, a broker sourced current sellable quantity, and verified tradeability. Missing evidence closes delivery. No source currently establishes a highest severity buy, so buys stay closed.

A versioned state in the decision snapshot records each symbol's plan, active breach, recovery count, episode and random epoch. First observations and plan replacements establish a silent baseline. Two distinct quotes above 101% of the unchanged stop rearm an episode; invalid and boundary quotes reset recovery progress. One crossing produces one event key tied to symbol, hard stop, final sell, epoch and episode. A removed holding retains its episode state. The archive claim is mandatory: an unavailable archive stops outbound delivery, and the router selects one channel without fallback.

Routine route calls and legacy direct portfolio transports return silently before contacting a sender. Scheduled slots do not claim a delivery or post a summary; their protected snapshot and bounded receipt report policy version, level, gate, eligible count and reason. This preserves the existing protected launcher and single slot contract while removing routine HTTP attempts.

Rejected alternatives: treating all sells as critical, sending cooldown reminders, guessing sellability from position size, comparing adjusted plan prices with unverified live quote basis, and sending a scheduled summary after the monitor has already observed the event.
