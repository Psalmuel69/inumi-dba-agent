"""Google Gemini provider (spec §34).

Uses the unified `google-genai` SDK. Gemini's function-calling schema is a
restricted subset of OpenAPI (no `$defs`, no discriminated unions) — the
flat schema in `agent.llm.base` is written to stay inside that subset, and
the strict discriminated-union validation happens afterwards via
`agent_action_adapter`.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from inumi.agent.llm.base import StructuredLLMProvider, logger

# Gemini 2.x and earlier are deliberately excluded from selection — operator
# policy, not a technical limitation. `_meets_min_version` is what actually
# enforces this (against the live `models.list()` result); these two
# constants are only the fallback used if that call fails, so they must
# name real, currently-serving models — not a guessed/rounded id.
_MIN_MAJOR_VERSION = 3
_DEFAULT_MODEL = "gemini-3.6-flash"
_KNOWN_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.5-flash"]

# The free tier's daily quota is per (project, model) — a project easily
# burns through one model's 20 req/day during real interactive testing.
# Ranked fallback chain tried, in order, when the *current* model reports
# quota exhaustion (never for any other kind of error — that stays with
# StructuredLLMProvider's own same-model retry). Real, currently-serving
# 3.x+ models, verified live 2026-09-11; refresh via list_models() if this
# goes stale. Mutating `self.model` on exhaustion (rather than raising) is
# deliberate — the LLMRegistry caches one provider instance per (provider,
# model) key, so the switch sticks for the rest of this process's requests
# instead of rediscovering the same exhausted model every time.
_MODEL_FALLBACK_CHAIN = [
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
]

_VERSION_RE = re.compile(r"gemini-(\d+)")


def _meets_min_version(model_name: str) -> bool:
    match = _VERSION_RE.search(model_name)
    if match is None:
        return False
    return int(match.group(1)) >= _MIN_MAJOR_VERSION


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text or "quota" in text.lower()


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Drop keys Gemini's schema validator rejects (`description` on the
    root, `$schema`, `title`, `additionalProperties`)."""
    allowed = {"type", "properties", "required", "enum", "items", "nullable"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in allowed:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _clean_schema(pv) if isinstance(pv, dict) else pv for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _clean_schema(v)
        else:
            out[k] = v
    return out


class GeminiLLMProvider(StructuredLLMProvider):
    provider_name = "gemini"
    model: str

    def __init__(self, api_key: str, model: str = ""):
        super().__init__(model or _DEFAULT_MODEL)
        self._api_key = api_key
        self._client = None
        self._quota_exhausted_models: set[str] = set()

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def _next_fallback_model(self) -> str | None:
        for candidate in _MODEL_FALLBACK_CHAIN:
            if candidate != self.model and candidate not in self._quota_exhausted_models:
                return candidate
        return None

    async def _with_model_fallback(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run `call()` against `self.model`; on a quota-exhausted error,
        switch to the next untried model in the fallback chain and retry the
        SAME request, rather than surfacing the error — a daily quota is a
        property of (project, model), not of the request itself."""
        while True:
            try:
                return await call()
            except Exception as exc:
                if not _is_quota_error(exc):
                    raise
                self._quota_exhausted_models.add(self.model)
                next_model = self._next_fallback_model()
                if next_model is None:
                    raise
                logger.warning(
                    "gemini_model_quota_exhausted_switching",
                    from_model=self.model,
                    to_model=next_model,
                )
                self.model = next_model

    async def _call_tool(
        self, *, system: str, user: str, schema: dict[str, Any], tool_name: str
    ) -> dict[str, Any]:
        from google.genai import types

        declaration = types.FunctionDeclaration(
            name=tool_name,
            description="Submit your answer.",
            parameters=types.Schema(**_clean_schema(schema)),
        )
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[declaration])],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY,
                    allowed_function_names=[tool_name],
                )
            ),
        )

        async def call():
            client = self._get_client()
            response = await client.aio.models.generate_content(
                model=self.model, contents=user, config=config
            )
            for part in response.candidates[0].content.parts:
                if part.function_call is not None:
                    return dict(part.function_call.args)
            raise ValueError("Gemini returned no function call.")

        return await self._with_model_fallback(call)

    async def _call_text(self, *, system: str, user: str) -> str:
        from google.genai import types

        async def call():
            client = self._get_client()
            response = await client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(system_instruction=system),
            )
            return response.text or ""

        return await self._with_model_fallback(call)

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            models: list[str] = []
            async for m in await client.aio.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if "generateContent" in actions or not actions:
                    name = (m.name or "").removeprefix("models/")
                    if name.startswith("gemini") and _meets_min_version(name):
                        models.append(name)
            return sorted(set(models)) or list(_KNOWN_MODELS)
        except Exception:  # noqa: BLE001
            return list(_KNOWN_MODELS)
