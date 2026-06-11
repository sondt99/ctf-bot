"""Tests for bot.services.scoreboard_fetcher — normalization and hashing."""
from __future__ import annotations

from bot.services.scoreboard_fetcher import (
    _extract_rctf_leaderboard,
    _looks_like_ctfd_scoreboard,
    _normalize_entries,
    make_payload_hash,
)


# ── _normalize_entries ─────────────────────────────────────────────────────────

def test_normalize_entries_basic():
    raw = [
        {"name": "TeamA", "score": 1000, "pos": 1},
        {"name": "TeamB", "score": 500, "pos": 2},
    ]
    result = _normalize_entries(raw)
    assert len(result) == 2
    assert result[0]["name"] == "TeamA"
    assert result[0]["score"] == 1000.0
    assert result[0]["pos"] == 1


def test_normalize_entries_uses_index_when_no_pos():
    raw = [{"name": "T", "score": 100}]
    result = _normalize_entries(raw)
    assert result[0]["pos"] == 1


def test_normalize_entries_skips_missing_name_or_score():
    raw = [{"name": "T"}, {"score": 100}, {"name": "OK", "score": 50}]
    result = _normalize_entries(raw)
    assert len(result) == 1
    assert result[0]["name"] == "OK"


def test_normalize_entries_sorted_by_pos():
    raw = [
        {"name": "B", "score": 50, "pos": 2},
        {"name": "A", "score": 100, "pos": 1},
    ]
    result = _normalize_entries(raw)
    assert result[0]["name"] == "A"
    assert result[1]["name"] == "B"


# ── _extract_rctf_leaderboard ──────────────────────────────────────────────────

def test_extract_rctf_leaderboard_standard():
    payload = {"data": {"leaderboard": [{"name": "team1", "score": 800}]}}
    result = _extract_rctf_leaderboard(payload)
    assert result is not None
    assert result[0]["name"] == "team1"
    assert result[0]["pos"] == 1


def test_extract_rctf_leaderboard_empty():
    payload = {"data": {"leaderboard": []}}
    result = _extract_rctf_leaderboard(payload)
    assert result == []


def test_extract_rctf_leaderboard_missing_fields_skipped():
    payload = {"data": {"leaderboard": [{"name": "t"}, {"name": "ok", "score": 100}]}}
    result = _extract_rctf_leaderboard(payload)
    assert result is not None
    assert len(result) == 1
    assert result[0]["name"] == "ok"


def test_extract_rctf_leaderboard_wrong_shape_returns_none():
    assert _extract_rctf_leaderboard({}) is None
    assert _extract_rctf_leaderboard({"data": []}) is None


# ── make_payload_hash ─────────────────────────────────────────────────────────

def test_make_payload_hash_deterministic():
    entries = [{"pos": 1, "name": "Team", "score": 500}]
    h1 = make_payload_hash(entries)
    h2 = make_payload_hash(entries)
    assert h1 == h2


def test_make_payload_hash_different_for_different_data():
    e1 = [{"pos": 1, "name": "Team", "score": 500}]
    e2 = [{"pos": 1, "name": "Team", "score": 501}]
    assert make_payload_hash(e1) != make_payload_hash(e2)


def test_make_payload_hash_is_string():
    entries = [{"pos": 1, "name": "T", "score": 1}]
    assert isinstance(make_payload_hash(entries), str)


# ── _looks_like_ctfd_scoreboard ───────────────────────────────────────────────

def test_looks_like_ctfd_scoreboard_true():
    payload = {"data": [{"name": "TeamX", "score": 100}]}
    assert _looks_like_ctfd_scoreboard(payload) is True


def test_looks_like_ctfd_scoreboard_empty_data():
    payload = {"data": []}
    assert _looks_like_ctfd_scoreboard(payload) is False


def test_looks_like_ctfd_scoreboard_no_data_key():
    assert _looks_like_ctfd_scoreboard({}) is False
