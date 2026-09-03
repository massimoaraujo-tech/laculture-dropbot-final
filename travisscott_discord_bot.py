#!/usr/bin/env python3
"""
Travis Scott (Cactus Jack) -> Discord Alert Bot
--------------------------------------------------
Same proven approach as Lemkus, Nude Project, and Denim Tears: shop.travisscott.com
runs on Shopify (confirmed via server/tech-stack lookup), so this polls the public
/products.json feed on each collection and posts to Discord whenever it sees:
  1) A brand new product added to a watched collection
  2) A restock (a size/variant that was sold out becomes available again)

⚠️ Read-only monitoring only. This bot does NOT add to cart, join any queue,
or attempt checkout in any way — it only tells you the instant something is
live so you can act on it yourself. Auto-checkout/auto-queue tools violate
site terms and this script intentionally does not do that.

⚠️ Known upcoming drop to watch: the Cactus Jack x Nike "Cactus Court"
collection (Zoom Vapor 12 HC tennis shoe + Air Force 1 'Ice Blue') is
expected online at travisscott.com on September 4. Consider temporarily
lowering CHECK_INTERVAL_SECONDS (e.g. to 20-30s) for a short window around
a known drop time, then setting it back afterward to be a good citizen.

SETUP: same as the other bots.
  1. Discord: Channel Settings -> Integrations -> Webhooks -> New Webhook -> Copy URL
  2. Set DISCORD_WEBHOOK_URL as an environment variable (Railway Variables tab)
  3. pip install requests
  4. python travisscott_discord_bot.py
"""

import json
import os
import time
import logging
from datetime import datetime, timezone

import requests

# ============================== CONFIG ======================================

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_DISCORD_WEBHOOK_URL_HERE")

BASE_URL = "https://shop.travisscott.com"

# Collection handles to watch. Add/remove based on what you see browsing
# shop.travisscott.com — the handle is the part after /collections/ in the URL.
# "all" catches everything as a safety net; narrow it down once you've
# checked the real collection handles on the live site.
COLLECTIONS = [
    "all",
    "new-arrivals",
    "footwear",
]

# How often to check, in seconds. Default is a normal, polite interval.
# Around a known big drop (like Sept 4), consider temporarily lowering this
# to 20-30s for a short window, then raising it back afterward.
CHECK_INTERVAL_SECONDS = 60

STATE_FILE = "travisscott_state.json"
REQUEST_TIMEOUT = 15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TravisScottAlertBot/1.0; +personal use)"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("travisscott-bot")

# ============================== STATE ========================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"products": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ============================== DISCORD ======================================

def post_to_discord(embed):
    if "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL is not set — skipping post. %s", embed.get("title"))
        return
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=REQUEST_TIMEOUT)
        if resp.status_code >= 300:
            log.error("Discord webhook error %s: %s", resp.status_code, resp.text[:300])
    except requests.RequestException as e:
        log.error("Failed to post to Discord: %s", e)

def _size_sort_key(size_str):
    try:
        return (0, float(size_str))
    except (TypeError, ValueError):
        return (1, str(size_str))

def build_embed(product, collection_handle, kind, all_available_sizes=None, changed_sizes=None):
    product_url = f"{BASE_URL}/products/{product['handle']}"
    price = None
    try:
        price = f"$ {float(product['variants'][0]['price']):,.2f}"
    except (KeyError, IndexError, ValueError, TypeError):
        pass

    image_url = None
    if product.get("images"):
        image_url = product["images"][0].get("src")

    all_available_sizes = all_available_sizes or []
    changed_sizes = changed_sizes or []

    if kind == "new":
        title = f"🆕 New Drop: {product['title']}"
        color = 0x2ECC71
    else:
        title = f"🔁 Restock: {product['title']}"
        color = 0x3498DB

    fields = [
        {"name": "Price", "value": price or "N/A", "inline": True},
        {"name": "Collection", "value": collection_handle, "inline": True},
    ]

    if kind == "restock" and changed_sizes:
        fields.append({
            "name": "🔥 Just Restocked",
            "value": ", ".join(sorted(changed_sizes, key=_size_sort_key)),
            "inline": False,
        })

    if all_available_sizes:
        fields.append({
            "name": f"Sizes In Stock ({len(all_available_sizes)})",
            "value": ", ".join(sorted(all_available_sizes, key=_size_sort_key)),
            "inline": False,
        })
    else:
        fields.append({"name": "Stock", "value": "No sizes currently available", "inline": False})

    embed = {
        "title": title,
        "url": product_url,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Travis Scott Alert Bot • Monitoring only — does not queue or checkout for you"},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}
    return embed

# ============================== SCRAPE (JSON) ================================

def fetch_collection_products(handle):
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
        variants = product.get("variants", [])
        size_labels = {str(v["id"]): (v.get("title") or v.get("option1") or "One Size") for v in variants}
        available_variant_ids = [str(v["id"]) for v in variants if v.get("available")]
        available_sizes = [size_labels[vid] for vid in available_variant_ids]

        existing = state["products"].get(pid)

        if existing is None:
            state["products"][pid] = {"available_variants": available_variant_ids}
            post_to_discord(build_embed(product, handle, "new", all_available_sizes=available_sizes))
            log.info("New product: %s (%d size(s) in stock)", product["title"], len(available_sizes))
            continue

        prev_available = set(existing.get("available_variants", []))
        now_available = set(available_variant_ids)
        newly_available_ids = now_available - prev_available

        if newly_available_ids:
            newly_available_sizes = [size_labels[vid] for vid in newly_available_ids]
            post_to_discord(build_embed(
                product, handle, "restock",
                all_available_sizes=available_sizes,
                changed_sizes=newly_available_sizes,
            ))
            log.info("Restock: %s -> %s", product["title"], ", ".join(newly_available_sizes))

        existing["available_variants"] = available_variant_ids

def main():
    state = load_state()
    first_run = len(state["products"]) == 0
    log.info("Starting Travis Scott alert bot. Watching: %s", ", ".join(COLLECTIONS))

    post_to_discord({
        "title": "✅ Travis Scott Alert Bot is online",
        "description": f"Watching: {', '.join(COLLECTIONS)}\nChecking every {CHECK_INTERVAL_SECONDS}s.\nMonitoring only — this bot does not queue or checkout for you.",
        "color": 0x95A5A6,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    while True:
        for handle in COLLECTIONS:
            if first_run:
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
        if first_run:
            log.info("First run — baseline built. No alerts fired this pass.")
        first_run = False
        log.info("Sleeping %ss...", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
