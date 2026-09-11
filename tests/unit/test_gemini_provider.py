"""Gemini model selection is restricted to 3.x and above — operator policy
enforced in `GeminiLLMProvider.list_models` (spec §34)."""

from __future__ import annotations

from inumi.agent.llm.gemini_provider import (
    _DEFAULT_MODEL,
    _KNOWN_MODELS,
    _meets_min_version,
)


def test_default_and_fallback_models_are_gemini_3_or_later():
    assert _meets_min_version(_DEFAULT_MODEL)
    assert all(_meets_min_version(m) for m in _KNOWN_MODELS)


def test_meets_min_version_accepts_gemini_3_and_above():
    assert _meets_min_version("gemini-3-flash")
    assert _meets_min_version("gemini-3-pro")
    assert _meets_min_version("gemini-4-flash")


def test_meets_min_version_rejects_gemini_2_and_below():
    assert not _meets_min_version("gemini-2.5-pro")
    assert not _meets_min_version("gemini-2.0-flash")
    assert not _meets_min_version("gemini-1.5-flash")


def test_meets_min_version_rejects_unversioned_names():
    assert not _meets_min_version("gemini-pro")
    assert not _meets_min_version("text-embedding-004")
