# Trade Outcomes Memory

Persistent log of every closed trade + Gate 4 reasoning that approved it.
Read by the LLM gate before each new decision on the same ticker.
Appended by PositionManager when a trade closes (WIN/LOSS/EOD).

Format per entry:
  ## TICKER | OUTCOME | DATE
  - Session, regime, P&L, gate reasoning (truncated to 120 chars)

---
