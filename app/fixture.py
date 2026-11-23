"""Offline provider double. Sees only messages. No access to eval rows or labels."""
import asyncio
import json
import os
import re

from .settings import CATALOG

KEYWORDS = {
    "REFUND": ("refund", "money back", "reimbursement"),
    "INVOICE": ("invoice", "bill copy"),
    "SHIPPING": ("shipping", "shipment", "shipping address"),
    "DELIVERY": ("delivery", "arrival", "deliver"),
    "SUBSCRIPTION": ("newsletter", "subscribe"),
    "PAYMENT": ("payment", "pay ", "credit card"),
    "FEEDBACK": ("feedback", "complaint", "review"),
    "CONTACT": ("agent", "contact", "support team"),
    "CANCEL": ("cancellation fee", "cancelation fee"),
    "ACCOUNT": ("account", "password", "profile"),
    "ORDER": ("order", "purchase"),
}


async def answer(text, version):
    await asyncio.sleep(0.025)
    if version == "v2" and os.getenv("FIXTURE_SLOW_CANARY") == "1":
        await asyncio.sleep(0.18)
    lower = text.casefold()
    cat = next((c for c, keys in KEYWORDS.items() if any(k in lower for k in keys)), "CONTACT")
    if os.getenv("FIXTURE_REGRESSION") == "1":
        cat = "CONTACT"
    priority = "low" if re.search(r"how|information|feedback", lower) else "normal"
    if re.search(r"blocked|twice|cannot log", lower):
        priority = "high"
    if re.search(r"fraud|compromised", lower):
        priority = "urgent"
    return json.dumps(dict(category=cat, priority=priority, product=CATALOG[cat], summary=text[:160],
                           draft_reply=f"I can help with your {cat.lower()} request. Could you share the relevant details?"))
