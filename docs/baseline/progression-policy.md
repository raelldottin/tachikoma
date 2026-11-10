# Progression policy (R2 / AUTO-005)

`sdk/policy/` turns one [account snapshot](account-snapshot.md) plus static design data into a `Plan`: an ordered list of [catalog](action-catalog.md) `ActionRequest`s, and every candidate it rejected with the reason. Policies are pure and versioned by name. Design data (`DesignData.from_records` parses the client's `RoomDesign` and `ResearchDesign` records) is passed in, never fetched.

## baseline-v1

`baseline-v1` reproduces `run.py`'s current daily order as far as the catalog can express it. That gives automated runs today's behaviour as their comparison baseline, not an invented strategy.

1. **Research** (`Client.upgradeResearches`): for each research line (`@RootResearchDesignId`), the first design that is neither researching nor completed. A line with research already in progress is skipped. Starbux-priced research is rejected unless `allow_starbux_spend=True`.
2. **Room upgrades** (`Client.upgradeRooms`): each room whose design has an upgrade (`@UpgradeFromRoomDesignId`) and is not already upgrading. Every admitted upgrade is followed by `collect_all_resources`, as `upgradeRooms` collects after each request.
3. **Daily reward** (`collect_daily_reward`).

Rules applied to every candidate:

- The action's catalog preconditions must hold on the snapshot. A section that wasn't observed yields no action, only a rejection.
- Costs (`mineral:N` or `gas:N`) are checked against the observed balance minus `mineral_reserve` / `gas_reserve`, and deducted cumulatively across the plan. An unobserved balance blocks priced actions.
- `max_requests` truncates the plan, and the dropped requests are listed as rejections.

## Differences from run.py

- **Training is not modelled.** `Client.manageTraining` chooses training tiers from research, fatigue and role data that the catalog doesn't express. Every plan carries an `add_training` rejection saying so.
- **Spend is deducted cumulatively.** `run.py` re-checks its last collected totals, while baseline-v1 subtracts planned spend, so it is slightly more conservative within one plan.
- **No concurrency cap.** As in `run.py`, the server's concurrent-construction limit is discovered at execution time; the R2 scheduler stops on it.
- Steps outside the catalog (flying starbux, task rewards, mining drones, market and messages, crew upgrades) are not part of the plan.
