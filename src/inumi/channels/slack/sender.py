"""Outgoing Slack message delivery.

Separated from the webhook handler so the handler's job stays limited to
"verify, identify, authorize, forward" (spec §4) — actually posting back to
Slack's Web API is an infrastructure concern with its own retry/error
handling, kept here.
"""

from __future__ import annotations

from typing import Any

import httpx

from inumi.common.observability import get_logger

logger = get_logger(__name__)


class SlackMessageSender:
    def __init__(self, bot_token: str):
        self._bot_token = bot_token

    async def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]]) -> None:
        if not self._bot_token:
            # Local development without a real Slack app configured — log
            # instead of calling a real (and unreachable) API.
            logger.info("slack_message_dev_stub", channel=channel, text=text)
            return
        async with httpx.AsyncClient(base_url="https://slack.com/api") as client:
            response = await client.post(
                "/chat.postMessage",
                headers={"Authorization": f"Bearer {self._bot_token}"},
                json={"channel": channel, "text": text, "blocks": blocks},
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("ok"):
                logger.error("slack_post_message_failed", error=body.get("error"))
