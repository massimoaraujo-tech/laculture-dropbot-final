#!/usr/bin/env python3
"""
Lemkus -> Discord Alert Bot
----------------------------
Polls Lemkus's public Shopify product feed (no HTML scraping needed — Shopify
exposes /products.json on every collection) and posts to a Discord webhook
whenever it sees:
  1) A brand new product added to a watched collection
  2) A restock (a size/variant that was sold out becomes available again)

SETUP (takes ~2 minutes):
  1. In Discord: Channel Settings -> Integrations -> Webhooks -> New Webhook
     -> copy the Webhook URL.
  2. Paste that URL into DISCORD_WEBHOOK_URL below.
  3. pip install requests
  4. python lemkus_discord_bot.py
  5. Leave it running (on your PC, a VPS, Railway, Render, or a Raspberry Pi).
     It checks every CHECK_INTERVAL_SECONDS and posts new activity.

State (what it's already seen) is stored in lemkus_state.json next to this
script, so restarting the script won't spam you with everything as "new".
"""

import json
import os
import time
import logging
from datetime import datetime, timezone

import requests

# ============================== CONFIG ======================================

# Paste your Discord webhook URL here (Server Settings > Integrations > Webhooks)
DISCORD_WEBHOOK_URL = "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE"

# Which Lemkus collections to watch. Add/remove as you like.
# Find more collection handles by browsing lemkus.com and copying the
# part after /collections/ in the URL.
COLLECTIONS = [
    "male-sneakers",
    "womens-sneakers",
    "sneaker-launches",   # "coming soon" / upcoming drops
    "just-dropped-sneakers",
]

BASE_URL = "https://lemkus.com"
CHECK_INTERVAL_SECONDS = 120          # how often to poll (2 min). Don't go below ~60s.
STATE_FILE = "lemkus_state.json"
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LemkusAlertBot/1.0; +personal use)"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("lemkus-bot")

# ============================== STATE ========================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"products": {}}  # product_id (str) -> {"available_variants": [ids...]}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================== DISCORD ======================================

def post_to_discord(embed):
    if "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not set — skipping post. %s", embed.get("title"))
        return
    payload = {"embeds": [embed]}
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 300:
            log.error("Discord webhook error %s: %s", resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        log.error("Failed to post to Discord: %s", e)

def build_embed(product, collection_handle, kind, variant_title=None):
    product_url = f"{BASE_URL}/products/{product['handle']}"
    price = None
    try:
        price = f"R {float(product['variants'][0]['price']):,.2f}"
    except (KeyError, IndexError, ValueError, TypeError):
        pass

    image_url = None
    if product.get("images"):
        image_url = product["images"][0].get("src")

    if kind == "new":
        title = f"🆕 New Drop: {product['title']}"
        color = 0x2ECC71  # green
    else:
        title = f"🔁 Restock: {product['title']}"
        if variant_title:
            title += f" ({variant_title})"
        color = 0x3498DB  # blue

    embed = {
        "title": title,
        "url": product_url,
        "color": color,
        "fields": [
            {"name": "Price", "value": price or "N/A", "inline": True},
            {"name": "Collection", "value": collection_handle, "inline": True},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Lemkus Alert Bot"},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed

# ============================== SCRAPE (JSON) ================================

def fetch_collection_products(handle):
    """Shopify exposes a public JSON feed for every collection."""
    products = []
    page = 1
    while True:
        url = f"{BASE_URL}/collections/{handle}/products.json"
        params = {"limit": 250, "page": page}
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("products", [])
        if not data:
            break
        products.extend(data)
        if len(data) < 250:
            break
        page += 1
    return products

# ============================== CORE LOGIC ===================================

def check_collection(handle, state):
    try:
        products = fetch_collection_products(handle)
    except requests.RequestException as e:
        log.error("Failed to fetch collection '%s': %s", handle, e)
        return

    for product in products:
        pid = str(product["id"])
        available_variant_ids = [
            str(v["id"]) for v in product.get("variants", []) if v.get("available")
        ]

        existing = state["products"].get(pid)

        if existing is None:
            # Brand new product we've never seen before
            state["products"][pid] = {"available_variants": available_variant_ids}
            post_to_discord(build_embed(product, handle, "new"))
            log.info("New product: %s", product["title"])
            continue

        # Check for restocked variants (previously unavailable, now available)
        prev_available = set(existing.get("available_variants", []))
        now_available = set(available_variant_ids)
        newly_available = now_available - prev_available

        if newly_available:
            variant_lookup = {str(v["id"]): v.get("title") for v in product.get("variants", [])}
            for vid in newly_available:
                variant_title = variant_lookup.get(vid)
                post_to_discord(build_embed(product, handle, "restock", variant_title))
            log.info("Restock: %s (%d size(s))", product["title"], len(newly_available))

        existing["available_variants"] = available_variant_ids

def main():
    state = load_state()
    log.info("Starting Lemkus alert bot. Watching: %s", ", ".join(COLLECTIONS))
    first_run = len(state["products"]) == 0
    if first_run:
        log.info("First run — building baseline (no Discord alerts will fire this pass).")

    while True:
        for handle in COLLECTIONS:
            if first_run:
                # On the very first run, just record state silently so you don't
                # get 300+ "new product" alerts for the entire existing catalog.
                try:
                    products = fetch_collection_products(handle)
                    for product in products:
                        pid = str(product["id"])
                        available_variant_ids = [
                            str(v["id"]) for v in product.get("variants", []) if v.get("available")
                        ]
                        state["products"][pid] = {"available_variants": available_variant_ids}
                except requests.RequestException as e:
                    log.error("Baseline fetch failed for '%s': %s", handle, e)
            else:
                check_collection(handle, state)
            save_state(state)
        first_run = False
        log.info("Sleeping %ss...", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
