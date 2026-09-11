"""Gemini model selection is restricted to 3.x and above — operator policy
enforced in `GeminiLLMProvider.list_models` (spec §34). Also covers the
automatic model-fallback behavior added after live testing repeatedly hit
the free tier's 20 req/day/model quota *and* sustained per-model capacity
shedding (503 "high demand" persisting across several same-model retries)."""

from __future__ import annotations

import pytest

from inumi.agent.llm.gemini_provider import (
    _DEFAULT_MODEL,
    _KNOWN_MODELS,
    _MODEL_FALLBACK_CHAIN,
    GeminiLLMProvider,
    _is_model_unavailable_error,
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


def test_is_model_unavailable_error_recognizes_the_real_gemini_429_shape():
    # Reproduces the live error text verbatim (trimmed).
    exc = RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You "
        "exceeded your current quota... GenerateRequestsPerDayPerProjectPerModel-FreeTier'}}"
    )
    assert _is_model_unavailable_error(exc)


def test_is_model_unavailable_error_recognizes_the_real_gemini_503_shape():
    # Reproduces the live error text verbatim — sustained across several
    # same-model retries in production, which is exactly why 503 triggers
    # a model switch too, not just 429.
    exc = RuntimeError(
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is "
        "currently experiencing high demand. Spikes in demand are usually "
        "temporary. Please try again later.', 'status': 'UNAVAILABLE'}}"
    )
    assert _is_model_unavailable_error(exc)


def test_is_model_unavailable_error_does_not_misclassify_a_request_specific_failure():
    """A malformed/empty completion is a reasoning problem the SAME model
    may well get right on the very next attempt — not a signal that this
    model itself is unavailable, so it must not trigger a model switch
    (StructuredLLMProvider's own same-model retry handles it instead)."""
    assert not _is_model_unavailable_error(ValueError("Gemini returned no function call."))


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
async def test_switches_to_the_next_model_on_sustained_high_demand_and_succeeds():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def call():
        if provider.model == _MODEL_FALLBACK_CHAIN[0]:
            raise RuntimeError("503 UNAVAILABLE: high demand")
        return "ok"

    result = await provider._with_model_fallback(call)
    assert result == "ok"
    assert provider.model == _MODEL_FALLBACK_CHAIN[1]


@pytest.mark.asyncio
async def test_a_request_specific_error_is_never_treated_as_model_unavailability():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def call():
        raise ValueError("Gemini returned no function call.")

    with pytest.raises(ValueError, match="no function call"):
        await provider._with_model_fallback(call)
    assert provider.model == _MODEL_FALLBACK_CHAIN[0]  # never switched


@pytest.mark.asyncio
async def test_raises_once_every_fallback_model_is_also_exhausted():
    provider = GeminiLLMProvider("fake-key", _MODEL_FALLBACK_CHAIN[0])

    async def call():
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded, model: " + provider.model)

    with pytest.raises(RuntimeError, match="429"):
        await provider._with_model_fallback(call)
    assert provider._unavailable_models == set(_MODEL_FALLBACK_CHAIN)


@pytest.mark.asyncio
async def test_a_model_switch_sticks_for_the_next_call_on_the_same_instance():
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
    # it already switched to — never retries the known-bad one.
    seen: list[str] = []

    async def second_call():
        seen.append(provider.model)
        return "ok"

    result = await provider._with_model_fallback(second_call)
    assert result == "ok"
    assert seen == [_MODEL_FALLBACK_CHAIN[1]]
