from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)


class Ask(Model):
    text: str


class Ans(Model):
    text: str


protocol = Protocol(spec=chat_protocol_spec)
waiting = []


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    waiting.append(sender)
    await ctx.send(PEERS["upper"], Ask(text=text))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@agent.on_message(Ans)
async def on_ans(ctx: Context, sender: str, msg: Ans):
    if waiting:
        user = waiting.pop(0)
        await ctx.send(user, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                         content=[TextContent(type="text", text="UPPER: " + msg.text)]))


agent.include(protocol, publish_manifest=True)
