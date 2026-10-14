# Offline modeling fixtures

`regulation.json` and `two-overtimes.json` are synthetic NBA-shaped PBP payloads
with complete period start/end markers, cumulative scores, made-shot possessions
and terminal states. The two-OT payload ties regulation and the first OT, then
finishes the second OT. All clocks, points and counters are deliberately small.
They prove pipeline contracts, not forecast accuracy.

`tests/integration/modeling_harness.py` combines these files with the existing
local lineup schedule/box templates and retargets them to five owned games in
season 2094-95: two train games, one embargo game, one validation game and one
test game. Primary keys and analytical snapshot UUIDs are journaled before
writes; collisions are refused and only owned records are removed.

The recovery scenario adds an invalid substitution to the test game and uses
`fixture-model-reset-v1` to emit an explicit provider-neutral reset at period 3.
The production NBA lineup normalizer does not emit this reset.

The `*.expected.json` and `*.expected.jsonl` files separately freeze the numeric
projection and canonical dataset/prediction layouts. Native fitted artifacts are
verified by CPU fit, probes and save/load tests rather than a volatile CBM golden.
