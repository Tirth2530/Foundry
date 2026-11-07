"""ASI:One Interactive Cards: builders (used by the Foreman) and a strict validator
(used by the Card Validator agent on generated agents).

Wire format (Agentverse docs, "Agent-Driven Interactive Cards" + "Predefined Card Schemas"):
  MetadataContent.metadata is dict[str, str] with
    card_protocol_version="1", requires_card_interaction="true"|"false",
    card_kind in carousel|detail|form|review|custom, card_payload=<JSON string, <=64KB>
"""
from __future__ import annotations

import json

KINDS = {"carousel", "detail", "form", "review", "custom"}
FIELD_KINDS = {"text", "number", "email", "select", "checkbox"}
MAX_PAYLOAD = 64 * 1024


# ---------------------------------------------------------------- builders

def metadata(kind: str, payload: dict, *, interactive: bool = True, width: int | None = None) -> dict:
    md = {
        "card_protocol_version": "1",
        "requires_card_interaction": "true" if interactive else "false",
        "card_kind": kind,
        "card_payload": json.dumps(payload, separators=(",", ":")),
    }
    if not interactive:
        md["is_terminal"] = "true"
    if width:
        md["preferred_drawer_width_px"] = str(max(320, min(800, width)))
    return md


def rows(pairs) -> list[dict]:
    return [{"label": str(k), "value": str(v)} for k, v in pairs]


def review(title: str, pairs, approve: tuple[str, dict], reject: tuple[str, dict]) -> dict:
    return metadata("review", {
        "title": title,
        "summary_rows": rows(pairs),
        "approve_cta": {"label": approve[0], "selection": _sel(approve[1]), "primary": True},
        "reject_cta": {"label": reject[0], "selection": _sel(reject[1])},
    })


def detail(title: str, pairs, ctas: list[tuple[str, dict, bool]], interactive: bool = True) -> dict:
    payload = {"title": title, "summary_rows": rows(pairs),
               "ctas": [{"label": l, "selection": _sel(s), "primary": p} for l, s, p in ctas]}
    return metadata("detail", payload, interactive=interactive and bool(ctas))


def carousel(title: str, subtitle: str, items: list[dict]) -> dict:
    out = []
    for i, it in enumerate(items):
        item = {
            "id": str(it.get("id", i)),
            "title": str(it["title"]),
            "subtitle": str(it.get("subtitle", "")),
            "secondary_text": str(it.get("secondary_text", "")),
            "primary_cta": {"label": it.get("cta_label", "Open"), "selection": _sel(it.get("selection", {}))},
        }
        if it.get("badges"):
            item["badges"] = [{"label": b, "variant": "info"} for b in it["badges"]]
        if it.get("image"):
            item["image"] = it["image"]
        out.append(item)
    return metadata("carousel", {"title": title, "subtitle": subtitle, "items": out})


def form(title: str, fields: list[dict], submit_label: str, selection: dict) -> dict:
    return metadata("form", {"title": title, "fields": fields,
                             "submit_cta": {"label": submit_label, "selection": _sel(selection)}})


def _sel(d: dict) -> dict:
    return {str(k): str(v) for k, v in (d or {}).items()}


# ---------------------------------------------------------------- validator

def validate(md: dict) -> list[str]:
    """Return a list of human-readable problems ([] means valid). Messages are specific on
    purpose: they are the environment feedback the builder learns from."""
    errs: list[str] = []
    if not isinstance(md, dict):
        return ["metadata is not a dict"]
    for k, v in md.items():
        if not isinstance(v, str):
            errs.append(f"metadata['{k}'] must be a string (got {type(v).__name__}); JSON-stringify nested values")
    if md.get("card_protocol_version") != "1":
        errs.append("missing card_protocol_version='1' (ASI:One silently drops the card without it)")
    if md.get("requires_card_interaction") not in ("true", "false"):
        errs.append("requires_card_interaction must be the string 'true' or 'false'")
    kind = md.get("card_kind")
    if kind not in KINDS:
        errs.append(f"card_kind must be one of {sorted(KINDS)} (got {kind!r})")
    raw = md.get("card_payload")
    if not isinstance(raw, str):
        errs.append("card_payload must be a JSON string (json.dumps the payload)")
        return errs
    if len(raw.encode()) > MAX_PAYLOAD:
        errs.append("card_payload exceeds 64KB")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        errs.append(f"card_payload is not valid JSON: {e}")
        return errs
    if not isinstance(payload, dict):
        errs.append("card_payload must decode to a JSON object")
        return errs
    if len(payload) == 1 and next(iter(payload)) in ("data", "payload", "card"):
        errs.append("card_payload is wrapped in an extra layer; put fields at the top level")
    if md.get("is_terminal") == "true" and md.get("requires_card_interaction") == "true":
        errs.append("is_terminal='true' on an interactive card: the drawer never opens")
    checker = {"carousel": _v_carousel, "detail": _v_detail, "form": _v_form, "review": _v_review}.get(kind)
    if checker:
        checker(payload, errs)
    return errs


def _need(obj: dict, key: str, where: str, errs: list, typ=str) -> bool:
    if key not in obj:
        errs.append(f"{where} missing '{key}' (found keys: {sorted(obj)})")
        return False
    if not isinstance(obj[key], typ):
        errs.append(f"{where}.{key} must be {typ.__name__}")
        return False
    return True


def _v_cta(cta, where, errs):
    if not isinstance(cta, dict):
        errs.append(f"{where} must be an object with 'label' and 'selection'")
        return
    _need(cta, "label", where, errs)
    if _need(cta, "selection", where, errs, dict):
        for k, v in cta["selection"].items():
            if not isinstance(v, str):
                errs.append(f"{where}.selection['{k}'] should be a string")


def _v_rows(p, errs, where="payload"):
    if _need(p, "summary_rows", where, errs, list):
        for i, r in enumerate(p["summary_rows"]):
            if not isinstance(r, dict) or "label" not in r or "value" not in r:
                errs.append(f"{where}.summary_rows[{i}] needs 'label' and 'value'")


def _v_carousel(p, errs):
    _need(p, "title", "carousel", errs)
    if _need(p, "items", "carousel", errs, list):
        if not p["items"]:
            errs.append("carousel.items is empty")
        for i, it in enumerate(p["items"]):
            w = f"carousel.items[{i}]"
            if not isinstance(it, dict):
                errs.append(f"{w} must be an object")
                continue
            _need(it, "id", w, errs)
            _need(it, "title", w, errs)
            if "primary_cta" not in it:
                errs.append(f"{w} missing 'primary_cta' (found keys: {sorted(it)})")
            else:
                _v_cta(it["primary_cta"], f"{w}.primary_cta", errs)


def _v_detail(p, errs):
    _need(p, "title", "detail", errs)
    _v_rows(p, errs, "detail")
    if "ctas" in p:
        if not isinstance(p["ctas"], list):
            errs.append("detail.ctas must be a list")
        else:
            for i, c in enumerate(p["ctas"]):
                _v_cta(c, f"detail.ctas[{i}]", errs)


def _v_form(p, errs):
    _need(p, "title", "form", errs)
    if _need(p, "fields", "form", errs, list):
        if not p["fields"]:
            errs.append("form.fields is empty")
        for i, f in enumerate(p["fields"]):
            w = f"form.fields[{i}]"
            if not isinstance(f, dict):
                errs.append(f"{w} must be an object")
                continue
            for k in ("name", "kind", "label"):
                _need(f, k, w, errs)
            if f.get("kind") not in FIELD_KINDS:
                errs.append(f"{w}.kind must be one of {sorted(FIELD_KINDS)} (got {f.get('kind')!r})")
            if f.get("kind") == "select" and not (isinstance(f.get("options"), list) and f["options"]):
                errs.append(f"{w} is a select field and needs non-empty 'options' [{{value,label}}]")
    if "submit_cta" not in p:
        errs.append(f"form missing 'submit_cta' (found keys: {sorted(p)})")
    else:
        _v_cta(p["submit_cta"], "form.submit_cta", errs)


def _v_review(p, errs):
    _need(p, "title", "review", errs)
    _v_rows(p, errs, "review")
    for k in ("approve_cta", "reject_cta"):
        if k not in p:
            errs.append(f"review missing '{k}' (found keys: {sorted(p)})")
        else:
            _v_cta(p[k], f"review.{k}", errs)


def describe(md: dict) -> dict:
    """Small summary of a card for tests: kind, item/field counts, field names."""
    try:
        payload = json.loads(md.get("card_payload", "{}"))
    except (json.JSONDecodeError, TypeError):
        payload = {}
    return {
        "kind": md.get("card_kind"),
        "items": len(payload.get("items", [])) if isinstance(payload.get("items"), list) else 0,
        "fields": [f.get("name") for f in payload.get("fields", []) if isinstance(f, dict)]
        if isinstance(payload.get("fields"), list) else [],
        "text": json.dumps(payload),
    }
