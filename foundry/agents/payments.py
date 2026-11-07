"""Payment Protocol (seller side) with Stripe test-mode checkout. Optional: if no Stripe key
is configured, FOUNDRY runs in demo mode and says so on the quote card."""
from __future__ import annotations

import time

from .. import config


def enabled() -> bool:
    if not (config.STRIPE_SECRET_KEY and config.STRIPE_PUBLISHABLE_KEY):
        return False
    try:
        import stripe  # noqa: F401
        return True
    except ImportError:
        return False


def create_checkout(amount_usd: float, job_id: str, user: str, description: str) -> dict:
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    cents = max(50, int(round(amount_usd * 100)))
    session = stripe.checkout.Session.create(
        ui_mode="embedded", redirect_on_completion="if_required", payment_method_types=["card"],
        mode="payment", return_url=f"{config.STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
        expires_at=int(time.time()) + 30 * 60,
        line_items=[{"price_data": {"currency": "usd", "product_data": {"name": "FOUNDRY verified build",
                                                                         "description": description[:200]},
                                    "unit_amount": cents}, "quantity": 1}],
        metadata={"job_id": job_id, "user_address": user},
        idempotency_key=f"foundry-checkout-{job_id}-{cents}",  # a retried approve never creates a 2nd session
    )
    return {"client_secret": session.client_secret, "id": session.id, "checkout_session_id": session.id,
            "publishable_key": config.STRIPE_PUBLISHABLE_KEY, "currency": "usd", "amount_cents": cents,
            "ui_mode": "embedded"}


def verify(session_id: str, job_id: str, expected_cents: int) -> tuple[bool, str]:
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    s = stripe.checkout.Session.retrieve(session_id)
    if getattr(s, "payment_status", None) != "paid":
        return False, "payment not completed yet"
    md = getattr(s, "metadata", {}) or {}
    if md.get("job_id") != job_id:
        return False, "session belongs to a different job"
    if int(getattr(s, "amount_total", 0) or 0) != expected_cents:
        return False, "amount mismatch"
    return True, "paid"
