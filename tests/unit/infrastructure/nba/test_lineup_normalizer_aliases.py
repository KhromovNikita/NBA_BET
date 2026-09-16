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


def test_only_dot_and_comma_punctuation_variants_are_permitted() -> None:
    raw = (
        normalizer._RawPlayer(10, 1, "N.", "O'Neal", None, True, True, None),
        normalizer._RawPlayer(10, 2, "Jean", "Pierre-Dupont", None, True, True, None),
    )
    aliases = normalizer._alias_map(raw)
    assert normalizer._resolve_alias(aliases, 10, "N O'Neal") == frozenset({1})
    assert normalizer._resolve_alias(aliases, 10, "N O" + chr(0x2019) + "Neal") == frozenset({1})
    assert normalizer._resolve_alias(
        aliases, 10, "Jean Pierre" + chr(0x2013) + "Dupont"
    ) == frozenset({2})
    assert normalizer._resolve_alias(aliases, 10, "N ONeal") == frozenset()
    assert normalizer._resolve_alias(aliases, 10, "Jean Pierre Dupont") == frozenset()


def test_suffix_alias_requires_a_complete_terminal_token() -> None:
    raw = (
        normalizer._RawPlayer(10, 1, "Alex", "Petrov", None, True, True, None),
        normalizer._RawPlayer(10, 2, "Alex", "James Jr.", None, True, True, None),
    )
    aliases = normalizer._alias_map(raw)
    assert normalizer._resolve_alias(aliases, 10, "Petrov") == frozenset({1})
    assert normalizer._resolve_alias(aliases, 10, "Petro") == frozenset()
    assert normalizer._resolve_alias(aliases, 10, "James") == frozenset({2})
