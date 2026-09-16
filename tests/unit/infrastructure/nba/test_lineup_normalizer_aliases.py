"""Finite NBA roster alias behavior."""

from __future__ import annotations

from nba_live_totals.infrastructure.nba import lineup_normalizer as normalizer


def test_aliases_cover_unicode_case_whitespace_punctuation_initial_and_suffix() -> None:
    aliases = normalizer._name_aliases("Nikola", "Jokić Jr.")
    keys = set(aliases)
    assert "nikola jokić jr." in keys
    assert "n jokić jr" in keys
    assert "jokić" in keys


def test_description_requires_fullmatch_and_rejects_controls_or_long_labels() -> None:
    assert normalizer._substitution_description(" SUB: Incoming FOR Outgoing ") == (
        "incoming",
        "outgoing",
    )
    assert normalizer._substitution_description("SUB: Incoming FOR Outgoing trailing") == (
        "incoming",
        "outgoing trailing",
    )
    assert normalizer._substitution_description("SUB: In\x00coming FOR Outgoing") is None
    assert normalizer._substitution_description(f"SUB: {'x' * 257} FOR Outgoing") is None


def test_alias_matching_is_finite_and_does_not_reorder_tokens() -> None:
    raw = (
        normalizer._RawPlayer(10, 1, "LeBron", "James", None, True, True, None),
        normalizer._RawPlayer(10, 2, "James", "LeBron", None, True, True, None),
    )
    aliases = normalizer._alias_map(raw)
    assert normalizer._resolve_alias(aliases, 10, "LeBron James") == frozenset({1})
    assert normalizer._resolve_alias(aliases, 10, "James LeBron") == frozenset({2})
    assert normalizer._resolve_alias(aliases, 10, "LeBron Unknown") == frozenset()
