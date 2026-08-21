"""Slack Block Kit rendering for Agent replies (spec §16).

Purely presentational — the `action_id`/`value` encoding here carries an
`approval_id` for the button click handler to forward, but clicking a
button is never itself trusted as an approval; see
`channels.api.app`'s interactive-action handler, which re-verifies
everything through the Agent -> Gateway path exactly like any other request.
"""

from __future__ import annotations

from typing import Any

from inumi.agent.reply import AgentReply


def render_reply_blocks(reply: AgentReply) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": reply.text}}
    ]
    if reply.approval_card is not None:
        card = reply.approval_card
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*AI DBA ACTION REQUIRES APPROVAL*\n"
                        f"*Operation:* `{card.tool_id}`\n"
                        f"*Target:* {card.target_summary}\n"
                        f"*Reason:* {card.reason}\n"
                        f"*Risk:* {card.risk_level}\n"
                        f"*Blast radius:* {card.blast_radius}\n"
                        f"*Expires in:* {card.expires_in_seconds // 60} minutes"
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "block_id": f"inumi_approval_{card.approval_id}",
                "elements": [
                    {
                        "type": "button",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "action_id": "inumi_approve",
                        "value": card.approval_id,
                    },
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Reject"},
                        "action_id": "inumi_reject",
                        "value": card.approval_id,
                    },
                ],
            }
        )
    return blocks
