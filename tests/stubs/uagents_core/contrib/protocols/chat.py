from datetime import datetime
from typing import Literal, Union
from uuid import UUID

from uagents import Model


class TextContent(Model):
    type: Literal["text"]
    text: str


class MetadataContent(Model):
    type: Literal["metadata"]
    metadata: dict[str, str]


class StartSessionContent(Model):
    type: Literal["start-session"]


class EndSessionContent(Model):
    type: Literal["end-session"]


class ChatMessage(Model):
    timestamp: datetime
    msg_id: UUID
    content: list[Union[TextContent, MetadataContent, StartSessionContent, EndSessionContent]]


class ChatAcknowledgement(Model):
    timestamp: datetime
    acknowledged_msg_id: UUID


chat_protocol_spec = object()
