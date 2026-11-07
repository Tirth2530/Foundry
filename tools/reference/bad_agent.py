import json
import re
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, MetadataContent, TextContent, chat_protocol_spec,
)

protocol = Protocol(spec=chat_protocol_spec)


def review_card(service, env):
    payload = {
        "title": f"Deploy {service} to {env}?",
        "summary_rows": [{"label": "Service", "value": service}, {"label": "Environment", "value": env}],
        "buttons": [{"label": "Approve", "selection": {"action": "approve", "service": service}, "primary": True},
            {"label": "Reject", "selection": {"action": "reject", "service": service}}],
    }
    return MetadataContent(type="metadata", metadata={
        "card_protocol_version": "1", "requires_card_interaction": "true",
        "card_kind": "review", "card_payload": json.dumps(payload)})


async def reply(ctx, to, text, card=None):
    content = [TextContent(type="text", text=text)] + ([card] if card else [])
    await ctx.send(to, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content))


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    sel = None  # BUG: never parses the JSON selection ASI:One sends back
    if isinstance(sel, dict) and sel.get("action") in ("approve", "reject"):
        word = "approved" if sel["action"] == "approve" else "cancelled"
        return await reply(ctx, sender, f"Deployment of {sel.get('service', '?')} {word}")
    m = re.match(r"deploy\s+(\S+)\s+to\s+(\S+)", text, re.I)
    if m:
        return await reply(ctx, sender, f"Approval needed: {m.group(1)} -> {m.group(2)}", review_card(m.group(1), m.group(2)))
    await reply(ctx, sender, "Usage: deploy <service> to <env>")


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(protocol, publish_manifest=True)
