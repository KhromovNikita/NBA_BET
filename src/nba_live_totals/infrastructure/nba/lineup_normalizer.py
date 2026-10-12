"""Structural NBA box-score/PBP validation for lineup reconstruction.

This module validates the provider envelope and classifies the small, explicit
substitution vocabulary supported by NBA V1. Raw names remain private to this
module; normalized transitions contain only stable player IDs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, fields
from typing import NoReturn, cast

from nba_live_totals.application.lineups import (
    GameLineupInputSnapshot,
    LineupNormalizationError,
)
from nba_live_totals.domain import (
    PlayByPlayEvent,
    SourceDocument,
    canonical_json_checksum,
)
from nba_live_totals.domain.lineups import (
    InvalidTeamTransition,
    NoLineupChange,
    NormalizedGameLineups,
    NormalizedLineupEvent,
    Substitute,
    TeamRoster,
)
from nba_live_totals.infrastructure.nba.errors import ProviderSchemaError
from nba_live_totals.infrastructure.nba.mappers import parse_play_by_play
from nba_live_totals.infrastructure.nba.mappers.common import provider_flag

_PLAYER_STATISTIC_FIELDS = (
    "minutes",
    "fieldGoalsMade",
    "fieldGoalsAttempted",
    "fieldGoalsPercentage",
    "threePointersMade",
    "threePointersAttempted",
    "threePointersPercentage",
    "freeThrowsMade",
    "freeThrowsAttempted",
    "freeThrowsPercentage",
    "reboundsOffensive",
    "reboundsDefensive",
    "reboundsTotal",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "foulsPersonal",
    "points",
    "plusMinusPoints",
)
NORMALIZER_VERSION = "nba-pbp-v3-lineup-v1"


@dataclass(frozen=True, slots=True)
class _RawPlayer:
    """Private box-score identity used for normalization and future aliases."""

    team_id: int
    player_id: int
    first_name: str
    family_name: str
    position: str | None
    starter: bool
    played: bool
    comment: str | None


_MAX_LABEL_LENGTH = 256
_SUBSTITUTION_DESCRIPTION = re.compile(r"sub:\s+(.+?)\s+for\s+(.+)")
_SUFFIX_TOKENS = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})


def _has_control(text: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in text)


def _text_forms(value: str, *, bounded: bool = False) -> tuple[str, ...]:
    """Return deterministic spelling variants, never a fuzzy/reordered match."""
    if _has_control(value):
        return ()
    value = unicodedata.normalize("NFKC", value)
    if _has_control(value):
        return ()
    value = value.translate(
        str.maketrans(
            cast(
                dict[int, str | int | None],
                {
                    0x2019: "'",
                    0x2018: "'",
                    0x02BC: "'",
                    0x2010: "-",
                    0x2011: "-",
                    0x2012: "-",
                    0x2013: "-",
                    0x2014: "-",
                    0x2015: "-",
                },
            )
        )
    )
    value = " ".join(value.split())
    if bounded and len(value) > _MAX_LABEL_LENGTH:
        return ()
    value = value.casefold()
    base = value
    # Only periods and commas are ignorable provider punctuation. Apostrophes
    # and hyphens remain in the key (after equivalent Unicode glyphs are
    # canonicalized above), so O'Neal is not ONeal and Jean-Pierre is not
    # Jean Pierre.
    dot_comma_free = value.replace(".", "").replace(",", "")
    forms = {base, dot_comma_free}
    return tuple([base, *sorted(form for form in forms if form and form != base)])


def _name_aliases(first_name: str, family_name: str) -> tuple[str, ...]:
    family_variants = [family_name]
    family_key = _text_forms(family_name)
    if family_key:
        tokens = family_key[0].split()
        if len(tokens) > 1 and tokens[-1].rstrip(".,") in _SUFFIX_TOKENS:
            family_variants.append(" ".join(tokens[:-1]))
    labels: set[str] = set()
    for family in family_variants:
        labels.update(_text_forms(family))
        labels.update(_text_forms(f"{first_name} {family}"))
        initial = first_name[:1]
        if initial:
            labels.update(_text_forms(f"{initial}. {family}"))
            labels.update(_text_forms(f"{initial} {family}"))
    return tuple(sorted(labels))


def _alias_map(raw_players: tuple[_RawPlayer, ...]) -> dict[int, dict[str, frozenset[int]]]:
    aliases: dict[int, dict[str, set[int]]] = {}
    for player in raw_players:
        team_aliases = aliases.setdefault(player.team_id, {})
        for alias in _name_aliases(player.first_name, player.family_name):
            team_aliases.setdefault(alias, set()).add(player.player_id)
    return {
        team_id: {alias: frozenset(values[alias]) for alias in sorted(values)}
        for team_id, values in sorted(aliases.items())
    }


def _resolve_alias(
    aliases: dict[int, dict[str, frozenset[int]]], team_id: int, label: str
) -> frozenset[int]:
    matches: set[int] = set()
    for key in _text_forms(label, bounded=True):
        matches.update(aliases.get(team_id, {}).get(key, ()))
    return frozenset(matches)


def _substitution_description(description: str) -> tuple[str, str] | None:
    """Parse a complete normalized SUB description or return no match."""
    forms = _text_forms(description)
    if not forms:
        return None
    # The first form retains punctuation in captured labels. Alias lookup
    # applies the finite punctuation variants separately.
    normalized = forms[0]
    match = _SUBSTITUTION_DESCRIPTION.fullmatch(normalized)
    if match is None:
        return None
    incoming, outgoing = match.groups()
    if not incoming or not outgoing:
        return None
    if len(incoming) > _MAX_LABEL_LENGTH or len(outgoing) > _MAX_LABEL_LENGTH:
        return None
    return incoming, outgoing


def _fail(code: str) -> NoReturn:
    raise LineupNormalizationError(code)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _fail("unsupported_document_schema")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        _fail("unsupported_document_schema")
    return cast(list[object], value)


def _string(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str:
        _fail("unsupported_document_schema")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        _fail("unsupported_document_schema")
    return value


def _provider_flag(value: object) -> bool:
    try:
        return provider_flag(value)
    except ProviderSchemaError:
        _fail("unsupported_document_schema")
    raise AssertionError("unreachable")


def _display_text(value: object) -> str | None:
    text = _string(value, optional=True)
    return text if text else None


def _required(mapping: dict[str, object], key: str) -> object:
    if key not in mapping:
        _fail("unsupported_document_schema")
    return mapping[key]


def _check_document_identity(
    document: SourceDocument,
    *,
    endpoint: str,
    resource_key: str,
    applied_checksum: str,
) -> dict[str, object]:
    """Check one immutable document without conflating its two identities."""
    if document.source != "nba.com" or document.endpoint != endpoint:
        _fail("input_document_identity_mismatch")
    if document.resource_key != resource_key:
        _fail("input_resource_not_applied")
    if document.checksum_sha256 != applied_checksum:
        _fail("input_document_checksum_mismatch")
    try:
        payload = document.serializable_payload()
        if canonical_json_checksum(payload) != document.checksum_sha256:
            _fail("input_document_checksum_mismatch")
    except LineupNormalizationError:
        raise
    except Exception:
        _fail("input_document_checksum_mismatch")
    return _mapping(payload)


def _raw_box_players(
    payload: dict[str, object], snapshot: GameLineupInputSnapshot
) -> tuple[_RawPlayer, ...]:
    meta = _mapping(_required(payload, "meta"))
    if type(meta.get("version")) is not int or meta.get("version") != 1:
        _fail("unsupported_document_schema")
    box = _mapping(_required(payload, "boxScoreTraditional"))
    raw_game_id = _string(_required(box, "gameId"))
    if raw_game_id != snapshot.game.game_id.value:
        _fail("input_document_identity_mismatch")
    if _integer(_required(box, "homeTeamId")) != snapshot.game.home_team_id:
        _fail("input_document_identity_mismatch")
    if _integer(_required(box, "awayTeamId")) != snapshot.game.away_team_id:
        _fail("input_document_identity_mismatch")

    rows: list[_RawPlayer] = []
    for key, expected_team_id in (
        ("homeTeam", snapshot.game.home_team_id),
        ("awayTeam", snapshot.game.away_team_id),
    ):
        team = _mapping(_required(box, key))
        for team_key in (
            "teamId",
            "teamCity",
            "teamName",
            "teamTricode",
            "teamSlug",
            "players",
            "statistics",
        ):
            _required(team, team_key)
        for team_text_key in ("teamCity", "teamName", "teamTricode", "teamSlug"):
            _string(_required(team, team_text_key))
        if _integer(_required(team, "teamId")) != expected_team_id:
            _fail("raw_canonical_roster_mismatch")
        team_statistics = _mapping(_required(team, "statistics"))
        for statistic in _PLAYER_STATISTIC_FIELDS:
            _required(team_statistics, statistic)
        for value in _list(_required(team, "players")):
            player = _mapping(value)
            for player_key in (
                "personId",
                "firstName",
                "familyName",
                "jerseyNum",
                "position",
                "comment",
                "statistics",
            ):
                _required(player, player_key)
            player_id = _integer(_required(player, "personId"))
            first_name = _string(_required(player, "firstName"))
            family_name = _string(_required(player, "familyName"))
            position = _display_text(_required(player, "position"))
            comment = _display_text(_required(player, "comment"))
            statistics = _mapping(_required(player, "statistics"))
            if statistics and any(key not in statistics for key in _PLAYER_STATISTIC_FIELDS):
                _fail("unsupported_document_schema")
            explicit_starter = player.get("starter")
            legacy_starter = position is not None
            if explicit_starter is None:
                starter = legacy_starter
            else:
                starter = _provider_flag(explicit_starter)
                if starter != legacy_starter and "position" in player and position is not None:
                    _fail("raw_canonical_roster_mismatch")
            played_value = player.get("played")
            played = bool(statistics) if played_value is None else _provider_flag(played_value)
            rows.append(
                _RawPlayer(
                    team_id=expected_team_id,
                    player_id=player_id,
                    first_name=cast(str, first_name),
                    family_name=cast(str, family_name),
                    position=position,
                    starter=starter,
                    played=played,
                    comment=comment,
                )
            )
    return tuple(rows)


def _validate_box_canonical(
    snapshot: GameLineupInputSnapshot,
    raw_players: tuple[_RawPlayer, ...],
) -> tuple[TeamRoster, TeamRoster]:
    raw_keys = tuple((row.team_id, row.player_id) for row in raw_players)
    raw_player_ids = tuple(row.player_id for row in raw_players)
    if len(set(raw_keys)) != len(raw_keys) or len(set(raw_player_ids)) != len(raw_player_ids):
        _fail("raw_canonical_roster_mismatch")
    canonical = tuple(snapshot.canonical_player_boxes)
    canonical_keys = tuple((row.team_id, row.player_id) for row in canonical)
    canonical_player_ids = tuple(row.player_id for row in canonical)
    if len(set(canonical_keys)) != len(canonical_keys) or len(set(canonical_player_ids)) != len(
        canonical_player_ids
    ):
        _fail("raw_canonical_roster_mismatch")
    if set(raw_keys) != set(canonical_keys):
        _fail("raw_canonical_roster_mismatch")

    raw_by_key = {key: row for key, row in zip(raw_keys, raw_players, strict=True)}
    for row in canonical:
        if row.game_id != snapshot.game.game_id:
            _fail("raw_canonical_roster_mismatch")
        raw = raw_by_key.get((row.team_id, row.player_id))
        if raw is None:
            _fail("raw_canonical_roster_mismatch")
        if (
            row.position,
            row.starter,
            row.played,
            row.comment,
        ) != (
            raw.position,
            raw.starter,
            raw.played,
            raw.comment,
        ):
            _fail("raw_canonical_roster_mismatch")

    rosters: list[TeamRoster] = []
    for team_id in (snapshot.game.home_team_id, snapshot.game.away_team_id):
        team_rows = tuple(row for row in raw_players if row.team_id == team_id)
        rosters.append(
            TeamRoster(
                team_id=team_id,
                roster_player_ids=frozenset(row.player_id for row in team_rows),
                eligible_player_ids=frozenset(
                    row.player_id for row in team_rows if row.played and row.comment is None
                ),
                starter_player_ids=frozenset(
                    row.player_id
                    for row in team_rows
                    if row.starter and row.played and row.comment is None
                ),
            )
        )
    return rosters[0], rosters[1]


def _validate_pbp(
    snapshot: GameLineupInputSnapshot,
    payload: dict[str, object],
) -> tuple[PlayByPlayEvent, ...]:
    meta = _mapping(_required(payload, "meta"))
    if type(meta.get("version")) is not int or meta.get("version") != 1:
        _fail("unsupported_document_schema")
    game = _mapping(_required(payload, "game"))
    raw_game_id = _string(_required(game, "gameId"))
    if raw_game_id != snapshot.game.game_id.value:
        _fail("input_document_identity_mismatch")
    actions = _list(_required(game, "actions"))
    if not actions:
        _fail("empty_completed_game_pbp")
    try:
        parsed = parse_play_by_play(snapshot.pbp_source_document)
    except ProviderSchemaError:
        _fail("unsupported_document_schema")
    except Exception:
        _fail("unsupported_document_schema")
    canonical = tuple(snapshot.canonical_events)
    if len(actions) != len(parsed) or len(parsed) != len(canonical):
        _fail("raw_canonical_cardinality_mismatch")
    names = tuple(field.name for field in fields(PlayByPlayEvent))
    for sequence_number, (raw_value, raw_event, canonical_event) in enumerate(
        zip(actions, parsed, canonical, strict=True), start=1
    ):
        raw_action = _mapping(raw_value)
        raw_action_number = _integer(_required(raw_action, "actionNumber"))
        raw_action_id_value = _required(raw_action, "actionId")
        if raw_action_id_value is not None and type(raw_action_id_value) is not int:
            _fail("unsupported_document_schema")
        if (
            sequence_number != raw_event.sequence_number
            or sequence_number != canonical_event.sequence_number
            or raw_action_number != raw_event.action_number
            or raw_action_number != canonical_event.action_number
            or raw_action_id_value != raw_event.action_id
            or raw_action_id_value != canonical_event.action_id
        ):
            _fail("raw_canonical_event_mismatch")
        if any(getattr(raw_event, name) != getattr(canonical_event, name) for name in names):
            _fail("raw_canonical_event_mismatch")
        if raw_event.game_id != snapshot.game.game_id:
            _fail("raw_canonical_event_mismatch")
        if raw_event.team_id not in {
            None,
            snapshot.game.home_team_id,
            snapshot.game.away_team_id,
        }:
            _fail("raw_canonical_event_mismatch")
    return parsed


def _classify_event(
    event: PlayByPlayEvent,
    raw_action: dict[str, object],
    aliases: dict[int, dict[str, frozenset[int]]],
    raw_by_id: dict[int, _RawPlayer],
) -> NoLineupChange | Substitute | InvalidTeamTransition:
    """Classify one action using only the finite, same-team raw roster map."""
    if event.action_type.strip().casefold() != "substitution":
        return NoLineupChange()
    if event.sub_type.strip().casefold() not in {"", "out"}:
        return InvalidTeamTransition(event.team_id, "unsupported_substitution_form")
    if event.team_id is None:
        return InvalidTeamTransition(None, "unknown_substitution_team")

    person_id = event.person_id
    if person_id is None:
        return InvalidTeamTransition(event.team_id, "missing_outgoing_player_id")
    raw_player = raw_by_id.get(person_id)
    if raw_player is None or raw_player.team_id != event.team_id:
        return InvalidTeamTransition(event.team_id, "roster_team_mismatch")

    raw_name = raw_action.get("playerName")
    if type(raw_name) is not str:
        return InvalidTeamTransition(event.team_id, "outgoing_label_mismatch")
    if _resolve_alias(aliases, event.team_id, raw_name) != frozenset({person_id}):
        return InvalidTeamTransition(event.team_id, "outgoing_label_mismatch")

    labels = _substitution_description(event.description)
    if labels is None:
        return InvalidTeamTransition(event.team_id, "unsupported_substitution_form")
    incoming_label, outgoing_label = labels
    outgoing_matches = _resolve_alias(aliases, event.team_id, outgoing_label)
    if outgoing_matches != frozenset({person_id}):
        return InvalidTeamTransition(event.team_id, "outgoing_label_mismatch")

    incoming_matches = _resolve_alias(aliases, event.team_id, incoming_label)
    if not incoming_matches:
        return InvalidTeamTransition(event.team_id, "unknown_incoming_player")
    eligible_matches = frozenset(
        player_id
        for player_id in incoming_matches
        if (incoming_player := raw_by_id.get(player_id)) is not None
        and incoming_player.played
        and incoming_player.comment is None
    )
    if len(eligible_matches) > 1:
        return InvalidTeamTransition(event.team_id, "ambiguous_incoming_player")
    if not eligible_matches:
        return InvalidTeamTransition(event.team_id, "ineligible_incoming_player")
    incoming_id = next(iter(eligible_matches))
    roster = raw_player.team_id
    incoming_raw = raw_by_id.get(incoming_id)
    if incoming_raw is None or incoming_raw.team_id != roster:
        return InvalidTeamTransition(event.team_id, "roster_team_mismatch")
    if not incoming_raw.played or incoming_raw.comment is not None:
        return InvalidTeamTransition(event.team_id, "ineligible_incoming_player")
    return Substitute(event.team_id, person_id, incoming_id)


class NBAPlayByPlayV3LineupNormalizer:
    """Validate one immutable snapshot and build starter/event envelopes."""

    version = NORMALIZER_VERSION

    def normalize(self, snapshot: GameLineupInputSnapshot) -> NormalizedGameLineups:
        if not isinstance(snapshot, GameLineupInputSnapshot):
            raise ValueError("snapshot must be a GameLineupInputSnapshot")
        box_payload = _check_document_identity(
            snapshot.box_score_source_document,
            endpoint="boxscoretraditionalv3",
            resource_key=f"box_score:{snapshot.game.game_id.value}",
            applied_checksum=snapshot.applied_box_score_sha256,
        )
        pbp_payload = _check_document_identity(
            snapshot.pbp_source_document,
            endpoint="playbyplayv3",
            resource_key=f"play_by_play:{snapshot.game.game_id.value}",
            applied_checksum=snapshot.applied_pbp_sha256,
        )
        try:
            raw_players = _raw_box_players(box_payload, snapshot)
            home_roster, away_roster = _validate_box_canonical(snapshot, raw_players)
            parsed_events = _validate_pbp(snapshot, pbp_payload)
        except TypeError, ValueError:
            _fail("raw_canonical_roster_mismatch")
        aliases = _alias_map(raw_players)
        raw_by_id = {player.player_id: player for player in raw_players}
        actions = _list(_required(_mapping(_required(pbp_payload, "game")), "actions"))
        try:
            events = tuple(
                NormalizedLineupEvent(
                    sequence_number=event.sequence_number,
                    period=event.period,
                    seconds_remaining_period_ms=event.seconds_remaining_period_ms,
                    transition=_classify_event(
                        event,
                        _mapping(raw_action),
                        aliases,
                        raw_by_id,
                    ),
                )
                for event, raw_action in zip(parsed_events, actions, strict=True)
            )
        except TypeError, ValueError:
            _fail("unsupported_document_schema")
        try:
            return NormalizedGameLineups(
                game_id=snapshot.game.game_id,
                home_roster=home_roster,
                away_roster=away_roster,
                events=events,
            )
        except TypeError, ValueError:
            _fail("raw_canonical_roster_mismatch")
        raise AssertionError("unreachable")


__all__ = ["NORMALIZER_VERSION", "NBAPlayByPlayV3LineupNormalizer"]
