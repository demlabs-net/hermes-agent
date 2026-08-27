"""swarm_messaging plugin — re-register the built-in send_message engine.

Hermes ships a full cross-channel send engine (tools.send_message_tool) but
deliberately keeps it OFF the model tool surface. This plugin re-registers it
as an agent-callable tool (override=True, gated by allow_tool_override in
config.yaml) so swarm agents can post to the shared Telegram home channel and
address teammates by @mention.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register send_message as a model-callable tool.

    The handler and schema are reused verbatim from the built-in engine, so all
    target formats (telegram, telegram:<chat_id>, @username resolution, threads,
    media, reactions) work exactly as documented in send_message_tool.
    """
    from tools.send_message_tool import SEND_MESSAGE_SCHEMA, send_message_tool

    ctx.register_tool(
        name="send_message",
        toolset="swarm_messaging",
        schema=SEND_MESSAGE_SCHEMA,
        handler=send_message_tool,
        override=True,
        emoji="\u2709\ufe0f",
    )
    logger.info("swarm_messaging: send_message tool registered for model use")
