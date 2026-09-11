"""Gemini model selection is restricted to 3.x and above — operator policy
enforced in `GeminiLLMProvider.list_models` (spec §34). Also covers the
automatic model-fallback-on-quota-exhaustion behavior added after live
testing repeatedly hit the free tier's 20 req/day/model cap."""

from __future__ import annotations

import pytest

from inumi.agent.llm.gemini_provider import (
    _DEFAULT_MODEL,
    _KNOWN_MODELS,
    _MODEL_FALLBACK_CHAIN,
    GeminiLLMProvider,
    _is_quota_error,
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


def test_is_quota_error_recognizes_the_real_gemini_429_shape():
    # Reproduces the live error text verbatim (trimmed).
    exc = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You "
        "exceeded your current quota... GenerateRequestsPerDayPerProjectPerModel-FreeTier'}}"
    )
    assert _is_quota_error(exc)


def test_is_quota_error_does_not_misclassify_a_plain_outage():
    assert not _is_quota_error(RuntimeError("503 UNAVAILABLE: high demand"))
    assert not _is_quota_error(ValueError("Gemini returned no function call."))


@pytest.mark.asyncio
async def test_switches_to_the_next_model_on_quota_exhaustion_and_succeeds():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])
    calls: list[str] = []

    async def call():
        calls.append(provider.model)
        if provider.model == _MODEL_FALLBACK_CHAIN[0]:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded, model: " + provider.model)
        return "ok"

    result = await provider._with_model_fallback(call)
    assert result == "ok"
    assert provider.model == _MODEL_FALLBACK_CHAIN[1]
    assert calls == [_MODEL_FALLBACK_CHAIN[0], _MODEL_FALLBACK_CHAIN[1]]


@pytest.mark.asyncio
async def test_a_non_quota_error_is_never_treated_as_exhaustion():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def call():
        raise RuntimeError("503 UNAVAILABLE: high demand")

    with pytest.raises(RuntimeError, match="503"):
        await provider._with_model_fallback(call)
    assert provider.model == _MODEL_FALLBACK_CHAIN[0]  # never switched


@pytest.mark.asyncio
async def test_raises_once_every_fallback_model_is_also_exhausted():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def call():
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded, model: " + provider.model)

    with pytest.raises(RuntimeError, match="429"):
        await provider._with_model_fallback(call)
    assert provider._quota_exhausted_models == set(_MODEL_FALLBACK_CHAIN)


@pytest.mark.asyncio
async def test_a_quota_switch_sticks_for_the_next_call_on_the_same_instance():
    """The LLMRegistry caches one provider instance per (provider, model)
    key, so this instance-level state is what makes the switch survive
    across separate requests in the same conversation — pinning that here
    since it's the whole reason this isn't tracked per-call instead."""
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def first_call():
        if provider.model == _MODEL_FALLBACK_CHAIN[0]:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota, model: " + provider.model)
        return "ok"

    await provider._with_model_fallback(first_call)
    assert provider.model == _MODEL_FALLBACK_CHAIN[1]

    # A later, unrelated call on the SAME instance starts from the model
    # it already switched to — never retries the known-exhausted one.
    seen: list[str] = []

    async def second_call():
        seen.append(provider.model)
        return "ok"

    result = await provider._with_model_fallback(second_call)
    assert result == "ok"
    assert seen == [_MODEL_FALLBACK_CHAIN[1]]
