"""LLM provider/model registry (spec §34, §36).

Turns configuration ("which API keys are present") into:
  - the set of providers a DBA may choose from,
  - the live list of models each key can actually use,
  - a concrete `LLMProvider` for a chosen (provider, model),
  - the provider to use for a given conversation (its explicit choice, or
    the configured default).

The Agent never bakes in a single vendor: everything above the registry
(`agent.orchestrator`) only ever sees the vendor-neutral `LLMProvider`
interface.
"""

from __future__ import annotations

from inumi.agent.llm.anthropic_provider import AnthropicLLMProvider
from inumi.agent.llm.base import LLMProvider
from inumi.agent.llm.gemini_provider import GeminiLLMProvider
from inumi.agent.llm.mock import MockLLMProvider
from inumi.agent.llm.openai_provider import DeepSeekLLMProvider, OpenAILLMProvider
from inumi.common.config import Settings

_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicLLMProvider,
    "openai": OpenAILLMProvider,
    "gemini": GeminiLLMProvider,
    "deepseek": DeepSeekLLMProvider,
}


class LLMRegistry:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._keys = settings._llm_api_keys()
        self._cache: dict[tuple[str, str], LLMProvider] = {}
        # Set by `for_testing` — pins every conversation to one provider and
        # disables selection.
        self._fixed: LLMProvider | None = None

    # -- test seam ---------------------------------------------------------

    @classmethod
    def for_testing(cls, provider: LLMProvider) -> LLMRegistry:
        registry = cls(Settings(_env_file=None, llm_provider="mock"))
        registry._fixed = provider
        return registry

    # -- capabilities ----------------------------------------------------

    def configured_providers(self) -> list[str]:
        return self._settings.configured_llm_providers()

    def selection_enabled(self) -> bool:
        return (
            self._fixed is None
            and self._settings.allow_user_model_selection
            and not self._settings.llm_selection_locked()
        )

    def default(self) -> tuple[str, str]:
        return self._settings.effective_default_llm()

    async def list_models(self, provider: str) -> list[str]:
        if provider not in self._keys or not self._keys[provider].strip():
            return []
        return await self._construct(provider, "").list_models()

    async def describe_available(self) -> str:
        configured = self.configured_providers()
        if not configured:
            return (
                "No LLM providers are configured. Set ANTHROPIC_API_KEY / "
                "OPENAI_API_KEY / GEMINI_API_KEY / DEEPSEEK_API_KEY to enable "
                "one. Currently using the deterministic offline planner."
            )
        lines = []
        for provider in configured:
            models = await self.list_models(provider)
            preview = ", ".join(models[:8]) + ("  …" if len(models) > 8 else "")
            lines.append(f"- {provider}: {preview or '(no models returned)'}")
        current_p, current_m = self.default()
        footer = (
            f"\nCurrent default: {current_p}"
            + (f" / {current_m}" if current_m else " (provider default)")
        )
        if not self.selection_enabled():
            footer += "\n(Per-conversation switching is disabled by configuration.)"
        return "Available LLMs:\n" + "\n".join(lines) + footer

    # -- construction --------------------------------------------------

    def _construct(self, provider: str, model: str) -> LLMProvider:
        if provider == "mock":
            return MockLLMProvider()
        key = self._keys.get(provider, "")
        if not key.strip():
            raise ValueError(f"No API key configured for provider '{provider}'.")
        cls = _PROVIDER_CLASSES.get(provider)
        if cls is None:
            raise ValueError(f"Unknown LLM provider '{provider}'.")
        return cls(key, model)  # type: ignore[call-arg]

    def build(self, provider: str, model: str | None) -> LLMProvider:
        if self._fixed is not None:
            return self._fixed
        cache_key = (provider, model or "")
        if cache_key not in self._cache:
            self._cache[cache_key] = self._construct(provider, model or "")
        return self._cache[cache_key]

    def for_conversation(self, *, provider: str | None, model: str | None) -> LLMProvider:
        """Resolve the provider for a conversation given its (possibly unset)
        explicit choice."""
        if self._fixed is not None:
            return self._fixed
        default_provider, default_model = self.default()
        chosen_provider = provider or default_provider
        chosen_model = model or (default_model if not provider else None)
        try:
            return self.build(chosen_provider, chosen_model)
        except ValueError:
            # A stale/invalid selection falls back to the default rather than
            # erroring mid-conversation.
            return self.build(default_provider, default_model)

    def validate_selection(self, provider: str, model: str | None) -> str | None:
        """Return an error string if (provider, model) can't be selected, else
        None. Model membership is checked by the caller against `list_models`
        (async)."""
        if not self.selection_enabled():
            return "Model selection is disabled by this deployment's configuration."
        if provider == "mock":
            return None
        if provider not in _PROVIDER_CLASSES:
            return (
                f"Unknown provider '{provider}'. Choose one of: "
                + ", ".join(_PROVIDER_CLASSES)
            )
        if not self._keys.get(provider, "").strip():
            return f"Provider '{provider}' is not configured (no API key)."
        return None
