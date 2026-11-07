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
    """Stripe test-mode checkout, tolerant of Stripe API changes: tries the embedded checkout (current then older
    parameter names) and falls back to a hosted checkout page with a URL. Payment is always verified server-side
    with Stripe before any build starts (see verify)."""
    import stripe
    stripe.api_key = config.STRIPE_SECRET_KEY
    cents = max(50, int(round(amount_usd * 100)))
    base = dict(mode="payment", expires_at=int(time.time()) + 30 * 60,
                line_items=[{"price_data": {"currency": "usd",
                                            "product_data": {"name": "FOUNDRY verified build",
                                                             "description": description[:200]},
                                            "unit_amount": cents}, "quantity": 1}],
                metadata={"job_id": job_id, "user_address": user})
    ret = f"{config.STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}"
    variants = [
        ("embedded_page", dict(ui_mode="embedded_page", return_url=ret, redirect_on_completion="if_required")),
        ("embedded_page_noredirect", dict(ui_mode="embedded_page", return_url=ret)),
        ("embedded", dict(ui_mode="embedded", return_url=ret, redirect_on_completion="if_required")),
        ("hosted", dict(success_url=ret, cancel_url=config.STRIPE_SUCCESS_URL)),
    ]
    errors = []
    for label, extra in variants:
        try:
            session = stripe.checkout.Session.create(**base, **extra,
                                                     idempotency_key=f"foundry-checkout-v3-{label}-{job_id}-{cents}")
        except Exception as e:  # noqa: BLE001
            if "InvalidRequest" not in type(e).__name__:  # auth/network problems are not a parameter issue
                raise
            errors.append(f"{label}: {str(e)[:160]}")  # parameter not supported by this API version: next variant
            continue
        return {"client_secret": getattr(session, "client_secret", None), "id": session.id,
                "checkout_session_id": session.id, "publishable_key": config.STRIPE_PUBLISHABLE_KEY,
                "currency": "usd", "amount_cents": cents, "ui_mode": "embedded" if label != "hosted" else "hosted",
                "url": getattr(session, "url", None), "variant": label}
    raise RuntimeError("Stripe checkout could not be created: " + " | ".join(errors))


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
