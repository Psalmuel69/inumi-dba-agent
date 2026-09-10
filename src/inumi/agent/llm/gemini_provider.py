"""Google Gemini provider (spec §34).

Uses the unified `google-genai` SDK. Gemini's function-calling schema is a
restricted subset of OpenAPI (no `$defs`, no discriminated unions) — the
flat schema in `agent.llm.base` is written to stay inside that subset, and
the strict discriminated-union validation happens afterwards via
`agent_action_adapter`.
"""

from __future__ import annotations

from typing import Any

from inumi.agent.llm.base import StructuredLLMProvider

_DEFAULT_MODEL = "gemini-2.5-pro"
_KNOWN_MODELS = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]


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

    def __init__(self, api_key: str, model: str = ""):
        super().__init__(model or _DEFAULT_MODEL)
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def _call_tool(
        self, *, system: str, user: str, schema: dict[str, Any], tool_name: str
    ) -> dict[str, Any]:
        from google.genai import types

        client = self._get_client()
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
        response = await client.aio.models.generate_content(
            model=self.model, contents=user, config=config
        )
        for part in response.candidates[0].content.parts:
            if part.function_call is not None:
                return dict(part.function_call.args)
        raise ValueError("Gemini returned no function call.")

    async def _call_text(self, *, system: str, user: str) -> str:
        from google.genai import types

        client = self._get_client()
        response = await client.aio.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system),
        )
        return response.text or ""

    async def list_models(self) -> list[str]:
        try:
            client = self._get_client()
            models: list[str] = []
            async for m in await client.aio.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if "generateContent" in actions or not actions:
                    name = (m.name or "").removeprefix("models/")
                    if name.startswith("gemini"):
                        models.append(name)
            return sorted(set(models)) or list(_KNOWN_MODELS)
        except Exception:  # noqa: BLE001
            return list(_KNOWN_MODELS)
