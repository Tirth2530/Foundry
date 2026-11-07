from typing import Optional

from uagents import Model


class Funds(Model):
    currency: str
    amount: str
    payment_method: str


class RequestPayment(Model):
    accepted_funds: list[Funds]
    recipient: str
    deadline_seconds: Optional[int] = None
    reference: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[dict] = None


class CommitPayment(Model):
    funds: Funds
    recipient: str = ""
    transaction_id: str


class CompletePayment(Model):
    transaction_id: str


class RejectPayment(Model):
    reason: str = ""


class CancelPayment(Model):
    transaction_id: str = ""
    reason: str = ""


payment_protocol_spec = object()
