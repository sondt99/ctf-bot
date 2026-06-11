"""Tests for bot.services.ctfd — normalization and utility functions."""
from __future__ import annotations

from bot.services.ctfd import (
    _authorization_values,
    _clean_text,
    _normalize_challenge,
    _normalize_files,
    _normalize_tags,
    _to_number,
)


# ── _clean_text ────────────────────────────────────────────────────────────────

def test_clean_text_strips_html_tags():
    assert _clean_text("<p>Hello world</p>") == "Hello world"


def test_clean_text_br_becomes_newline():
    result = _clean_text("line1<br/>line2") or ""
    assert "line1" in result
    assert "line2" in result


def test_clean_text_none_returns_none():
    assert _clean_text(None) is None


def test_clean_text_empty_returns_none():
    assert _clean_text("   ") is None


def test_clean_text_unescapes_html_entities():
    assert "&amp;" not in (_clean_text("Hello &amp; World") or "")


# ── _to_number ─────────────────────────────────────────────────────────────────

def test_to_number_integer():
    assert _to_number(100) == 100
    assert isinstance(_to_number(100), int)


def test_to_number_whole_float_becomes_int():
    assert _to_number(100.0) == 100
    assert isinstance(_to_number(100.0), int)


def test_to_number_fractional_stays_float():
    result = _to_number(99.5)
    assert result == 99.5
    assert isinstance(result, float)


def test_to_number_none_returns_none():
    assert _to_number(None) is None


def test_to_number_bad_string_returns_none():
    assert _to_number("notanumber") is None


# ── _normalize_tags ────────────────────────────────────────────────────────────

def test_normalize_tags_dict_list():
    raw = [{"value": "web"}, {"value": "easy"}]
    assert _normalize_tags(raw) == ["web", "easy"]


def test_normalize_tags_string_list():
    assert _normalize_tags(["rev", "pwn"]) == ["rev", "pwn"]


def test_normalize_tags_not_list_returns_empty():
    assert _normalize_tags("notalist") == []
    assert _normalize_tags(None) == []


# ── _normalize_files ───────────────────────────────────────────────────────────

def test_normalize_files_absolute_url_unchanged():
    files = _normalize_files(["http://example.com/file.zip"], "http://base/")
    assert "http://example.com/file.zip" in files


def test_normalize_files_relative_url_joined():
    files = _normalize_files(["/files/challenge.zip"], "http://ctf.example.com/")
    assert files[0].startswith("http://ctf.example.com")


def test_normalize_files_none_returns_empty():
    assert _normalize_files(None, "http://base/") == []


# ── _authorization_values ──────────────────────────────────────────────────────

def test_authorization_values_with_token_prefix():
    vals = _authorization_values("Token abc123")
    assert vals == ["Token abc123"]


def test_authorization_values_with_bearer_prefix():
    vals = _authorization_values("Bearer xyz")
    assert vals == ["Bearer xyz"]


def test_authorization_values_without_prefix_returns_both():
    vals = _authorization_values("mysecret")
    assert "Token mysecret" in vals
    assert "Bearer mysecret" in vals


def test_authorization_values_none_returns_none_only():
    vals = _authorization_values(None)
    assert vals == [None]


# ── _normalize_challenge ───────────────────────────────────────────────────────

def test_normalize_challenge_missing_id_returns_none():
    assert _normalize_challenge({}, "http://base/") is None


def test_normalize_challenge_minimal():
    raw = {"id": 1, "name": "Test Challenge", "category": "web"}
    result = _normalize_challenge(raw, "http://ctf.example.com/")
    assert result is not None
    assert result.id == 1
    assert result.name == "Test Challenge"
    assert result.category == "web"


def test_normalize_challenge_empty_name_uses_fallback():
    raw = {"id": 42, "name": "", "category": "misc"}
    result = _normalize_challenge(raw, "http://base/")
    assert result is not None
    assert "42" in result.name
