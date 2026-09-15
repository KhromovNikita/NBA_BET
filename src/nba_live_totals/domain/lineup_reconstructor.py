"""Single-pass causal provider-neutral lineup reconstruction."""

from __future__ import annotations

from nba_live_totals.domain.lineups import (
    FullLineupReset,
    InvalidTeamTransition,
    LineupReconstructionContext,
    LineupReconstructionResult,
    LineupSegment,
    LineupStatus,
    NoLineupChange,
    NormalizedGameLineups,
    NormalizedLineupEvent,
    Substitute,
    TeamLineupState,
    TeamRoster,
    _issues_from_segments,
)
from nba_live_totals.domain.possessions import BoundarySide, EventBoundary

_INVALID_REASON_ALLOWLIST = frozenset(
    {
        "invalid_initial_lineup",
        "unknown_substitution_team",
        "missing_outgoing_player_id",
        "unsupported_substitution_form",
        "outgoing_label_mismatch",
        "unknown_incoming_player",
        "ambiguous_incoming_player",
        "roster_team_mismatch",
        "ineligible_incoming_player",
        "outgoing_not_on_court",
        "incoming_already_on_court",
        "same_player_substitution",
        "invalid_full_lineup_reset",
    }
)


def _valid_state(player_ids: frozenset[int]) -> TeamLineupState:
    return TeamLineupState(LineupStatus.VALID, player_ids, None, None)


def _invalid_state(reason: str, boundary: EventBoundary) -> TeamLineupState:
    return TeamLineupState(LineupStatus.INVALID, frozenset(), reason, boundary)


def _initial_state(roster: TeamRoster, first_boundary: EventBoundary) -> TeamLineupState:
    if len(roster.starter_player_ids) == 5:
        return _valid_state(roster.starter_player_ids)
    return _invalid_state("invalid_initial_lineup", first_boundary)


def _invalidate_if_valid(
    state: TeamLineupState,
    reason: str,
    boundary: EventBoundary,
) -> TeamLineupState:
    if state.status is LineupStatus.INVALID:
        return state
    return _invalid_state(reason, boundary)


def _apply_substitute(
    state: TeamLineupState,
    roster: TeamRoster,
    transition: Substitute,
    boundary: EventBoundary,
) -> TeamLineupState:
    if state.status is LineupStatus.INVALID:
        return state
    outgoing = transition.outgoing_player_id
    incoming = transition.incoming_player_id
    if outgoing not in roster.roster_player_ids:
        return _invalid_state("roster_team_mismatch", boundary)
    if outgoing not in state.player_ids:
        return _invalid_state("outgoing_not_on_court", boundary)
    if incoming not in roster.roster_player_ids:
        return _invalid_state("roster_team_mismatch", boundary)
    if incoming not in roster.eligible_player_ids:
        return _invalid_state("ineligible_incoming_player", boundary)
    if outgoing == incoming:
        return _invalid_state("same_player_substitution", boundary)
    if incoming in state.player_ids:
        return _invalid_state("incoming_already_on_court", boundary)
    player_ids = (state.player_ids - {outgoing}) | {incoming}
    if len(player_ids) != 5:
        return _invalid_state("unsupported_substitution_form", boundary)
    return _valid_state(player_ids)


def _apply_reset(
    state: TeamLineupState,
    roster: TeamRoster,
    transition: FullLineupReset,
    boundary: EventBoundary,
) -> TeamLineupState:
    player_ids = frozenset(transition.player_ids)
    valid_reset = (
        len(transition.player_ids) == 5
        and len(player_ids) == 5
        and player_ids <= roster.eligible_player_ids
    )
    if not valid_reset:
        return _invalidate_if_valid(state, "invalid_full_lineup_reset", boundary)
    return _valid_state(player_ids)


class DeterministicLineupReconstructor:
    """Reconstruct full team states in one causal pass over canonical event order."""

    def reconstruct(
        self,
        context: LineupReconstructionContext,
        normalized: NormalizedGameLineups,
    ) -> LineupReconstructionResult:
        if not isinstance(context, LineupReconstructionContext):
            raise ValueError("lineup reconstruction context has an invalid type")
        if not isinstance(normalized, NormalizedGameLineups):
            raise ValueError("normalized game lineups have an invalid type")
        self._require_matching_game(context, normalized)

        first_event = normalized.events[0]
        first_boundary = EventBoundary(first_event.sequence_number, BoundarySide.BEFORE)
        home_state = _initial_state(normalized.home_roster, first_boundary)
        away_state = _initial_state(normalized.away_roster, first_boundary)
        segment_start = first_boundary
        segment_start_event = first_event
        segments: list[LineupSegment] = []

        for event in normalized.events:
            after = EventBoundary(event.sequence_number, BoundarySide.AFTER)
            next_home, next_away = self._apply_event(
                event,
                after,
                normalized.home_roster,
                normalized.away_roster,
                home_state,
                away_state,
            )
            if next_home == home_state and next_away == away_state:
                continue
            segments.append(
                self._segment(
                    len(segments) + 1,
                    segment_start,
                    EventBoundary(event.sequence_number, BoundarySide.BEFORE),
                    segment_start_event,
                    event,
                    home_state,
                    away_state,
                )
            )
            segment_start = after
            segment_start_event = event
            home_state = next_home
            away_state = next_away

        last_event = normalized.events[-1]
        segments.append(
            self._segment(
                len(segments) + 1,
                segment_start,
                EventBoundary(last_event.sequence_number, BoundarySide.AFTER),
                segment_start_event,
                last_event,
                home_state,
                away_state,
            )
        )
        frozen_segments = tuple(segments)
        status = (
            LineupStatus.VALID
            if all(
                segment.home_state.status is LineupStatus.VALID
                and segment.away_state.status is LineupStatus.VALID
                for segment in frozen_segments
            )
            else LineupStatus.INVALID
        )
        return LineupReconstructionResult(
            context=context,
            status=status,
            segments=frozen_segments,
            issues=_issues_from_segments(context, frozen_segments),
        )

    @staticmethod
    def _require_matching_game(
        context: LineupReconstructionContext,
        normalized: NormalizedGameLineups,
    ) -> None:
        if normalized.game_id != context.game.game_id:
            raise ValueError("normalized lineup game does not match reconstruction context")
        if (
            normalized.home_roster.team_id != context.game.home_team_id
            or normalized.away_roster.team_id != context.game.away_team_id
        ):
            raise ValueError("normalized lineup teams do not match reconstruction context")

    @staticmethod
    def _segment(
        number: int,
        start: EventBoundary,
        end: EventBoundary,
        start_event: NormalizedLineupEvent,
        end_event: NormalizedLineupEvent,
        home_state: TeamLineupState,
        away_state: TeamLineupState,
    ) -> LineupSegment:
        return LineupSegment(
            segment_number=number,
            start_boundary=start,
            end_boundary=end,
            start_period=start_event.period,
            end_period=end_event.period,
            start_seconds_remaining_period_ms=start_event.seconds_remaining_period_ms,
            end_seconds_remaining_period_ms=end_event.seconds_remaining_period_ms,
            home_state=home_state,
            away_state=away_state,
        )

    @staticmethod
    def _apply_event(
        event: NormalizedLineupEvent,
        boundary: EventBoundary,
        home_roster: TeamRoster,
        away_roster: TeamRoster,
        home_state: TeamLineupState,
        away_state: TeamLineupState,
    ) -> tuple[TeamLineupState, TeamLineupState]:
        transition = event.transition
        if isinstance(transition, NoLineupChange):
            return home_state, away_state
        if isinstance(transition, InvalidTeamTransition):
            if transition.code not in _INVALID_REASON_ALLOWLIST:
                raise ValueError("invalid transition reason must be allowlisted")
            if transition.team_id == home_roster.team_id:
                return _invalidate_if_valid(home_state, transition.code, boundary), away_state
            if transition.team_id == away_roster.team_id:
                return home_state, _invalidate_if_valid(away_state, transition.code, boundary)
            return (
                _invalidate_if_valid(home_state, transition.code, boundary),
                _invalidate_if_valid(away_state, transition.code, boundary),
            )
        if isinstance(transition, Substitute):
            if transition.team_id == home_roster.team_id:
                return (
                    _apply_substitute(home_state, home_roster, transition, boundary),
                    away_state,
                )
            if transition.team_id == away_roster.team_id:
                return (
                    home_state,
                    _apply_substitute(away_state, away_roster, transition, boundary),
                )
            reason = "unknown_substitution_team"
            return (
                _invalidate_if_valid(home_state, reason, boundary),
                _invalidate_if_valid(away_state, reason, boundary),
            )
        if transition.team_id == home_roster.team_id:
            return _apply_reset(home_state, home_roster, transition, boundary), away_state
        if transition.team_id == away_roster.team_id:
            return home_state, _apply_reset(away_state, away_roster, transition, boundary)
        reason = "invalid_full_lineup_reset"
        return (
            _invalidate_if_valid(home_state, reason, boundary),
            _invalidate_if_valid(away_state, reason, boundary),
        )


__all__ = ["DeterministicLineupReconstructor"]
