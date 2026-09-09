"""Small provider-neutral streams for possession behavior tests."""

from nba_live_totals.domain.possessions import (
    ActionConsumption,
    FieldGoalAttempt,
    NormalizedAction,
    NormalizedGameActions,
    PeriodEnd,
    PeriodStart,
    PossessionSignal,
    ProviderReboundSide,
    Rebound,
    ReboundActor,
    ReboundDisposition,
)
from tests.unit.domain.test_possessions import GAME_ID


def action(sequence: int, *signals: PossessionSignal, period: int = 1) -> NormalizedAction:
    return NormalizedAction(
        game_id=GAME_ID,
        sequence_number=sequence,
        provider_action_number=7,
        provider_action_id=7,
        period=period,
        clock_ms=0 if any(isinstance(s, PeriodEnd) for s in signals) else 600_000,
        actor_team_id=None,
        actor_person_id=None,
        checkpoint_home_score=None,
        checkpoint_away_score=None,
        checkpoint_combined_score=None,
        consumption=ActionConsumption.TRANSITION,
        signals=signals,
    )


def game(*signals: PossessionSignal | tuple[PossessionSignal, ...]) -> NormalizedGameActions:
    """Put the scenario in Q1 and explicitly finish the remaining empty periods."""
    actions = [action(1, PeriodStart())]
    for signal in signals:
        values = signal if isinstance(signal, tuple) else (signal,)
        actions.append(action(len(actions) + 1, *values))
    actions.append(action(len(actions) + 1, PeriodEnd()))
    for period in (2, 3, 4):
        actions.append(action(len(actions) + 1, PeriodStart(), period=period))
        actions.append(action(len(actions) + 1, PeriodEnd(), period=period))
    return NormalizedGameActions(GAME_ID, tuple(actions))


def shot(team: int, *, made: bool = True) -> FieldGoalAttempt:
    return FieldGoalAttempt(team, 2, made, False)


def rebound(team: int, *, actor: ReboundActor = ReboundActor.PLAYER) -> Rebound:
    return Rebound(team, actor, ProviderReboundSide.UNKNOWN, ReboundDisposition.NORMAL_LIVE)
