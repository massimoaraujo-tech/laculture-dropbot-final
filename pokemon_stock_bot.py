#!/usr/bin/env python3
"""
Pokemon Stock Tracker -> Discord Webhook

Supports three store platforms:

  WOOCOMMERCE (e.g. pokestore.co.za)
    Uses the built-in `?stock_status=instock&per_page=-1` filter to fetch
    only currently in-stock items in one request. Anything appearing in that
    list that wasn't there last run = new stock (whether it's a brand-new
    listing or a restock).

  MAGENTO (e.g. toysrus.co.za)
    Category pages list ALL products, including out-of-stock/"Coming Soon"
    ones, each with a status label. We track every product's stock status
    and alert the moment it flips from unavailable -> available. This
    catches restocks the WooCommerce approach can't see coming.

  SHOPIFY (e.g. store.nintendo.co.za, toykingdom.co.za, levelupstore.co.za,
           bigbangshop.co.za)
    Same proven approach as the Lemkus/Nude Project/Denim Tears bots — reads
    the public /products.json feed Shopify exposes on every collection.
    Every variant's availability is checked, same restock detection as
    Magento above.

  GENERIC (e.g. gengargames.com — platform unconfirmed)
    Best-effort fallback: looks for any product-like link with a price
    nearby and checks for out-of-stock keywords in the surrounding text.
    Less reliable than the platform-specific fetchers — check its first-run
    logs closely; if it parses 0 products, its real markup needs inspecting.

Runs continuously with its own built-in loop (same pattern as the other
Discord alert bots in this repo) — deploy it on Railway the same way:
set DISCORD_WEBHOOK_URL as an environment variable and Start Command to
`python pokemon_stock_bot.py`.
"""

import json
import os
import re
import time
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# CONFIG — edit this section
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "PASTE_YOUR_WEBHOOK_URL_HERE")

STORES = [
    {
        "name": "Poké Store",
        "platform": "woocommerce",
        "url": "https://pokestore.co.za/shop/?orderby=date&stock_status=instock&per_page=-1",
    },
    {
        "name": "Toys R Us SA — Pokémon",
        "platform": "magento",
        "url": "https://www.toysrus.co.za/pokemon-promo?product_list_limit=100",
    },
    {
        "name": "Toys R Us SA — Trading Cards",
        "platform": "magento",
        "url": "https://www.toysrus.co.za/trading-cards-shop-all/pokemon?product_list_limit=100",
    },
    {
        "name": "Nintendo SA — Pokémon TCG",
        "platform": "shopify",
        "url": "https://store.nintendo.co.za/collections/pokemon-trading-cards",
    },
    {
        "name": "Toy Kingdom — Pokémon Cards",
        "platform": "shopify",
        "url": "https://toykingdom.co.za/collections/pokemon-cards",
    },
    {
        "name": "Level Up Store — Pokémon Cards",
        "platform": "shopify",
        "url": "https://levelupstore.co.za/collections/pokemon-cards",
    },
    {
        "name": "Big Bang Shop — Pokémon TCG",
        "platform": "shopify",
        "url": "https://bigbangshop.co.za/collections/pokemon-trading-card-game",
    },
    {
        "name": "ThunderBolt Gaming",
        "platform": "woocommerce",
        "url": "https://tbgaming.co.za/?stock_status=instock&per_page=-1",
    },
    {
        "name": "Gengar Games",
        "platform": "generic",
        "url": "https://www.gengargames.com/pokemon-single-cards",
    },
    {
        "name": "Wordsworth — Pokémon",
        "platform": "shopify",
        "url": "https://www.wordsworth.co.za/collections/pokemon-1",
    },
    # Add more stores here. Any WooCommerce, Magento, Shopify, or generic
    # shop works with zero code changes — just set "platform" and "url".
]

# How often to run a full check, in seconds.
CHECK_INTERVAL_SECONDS = 120

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

REQUEST_TIMEOUT = 20  # seconds

OOS_KEYWORDS = ("out of stock", "coming soon", "sold out", "notify me", "pre-order notify")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pokemon-bot")

# ---------------------------------------------------------------------------
# Scraping — WooCommerce
# ---------------------------------------------------------------------------

def fetch_woocommerce(store_url: str):
    """Return products from a WooCommerce shop URL filtered to in-stock only.
    Every product returned is treated as in_stock=True."""
    resp = requests.get(store_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = []
    items = soup.select("li.product") or soup.select(".products .product") or soup.select("div.product")

    for item in items:
        link_tag = item.select_one("a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link") \
            or item.find("a", href=re.compile(r"/product/"))
        if not link_tag or not link_tag.get("href"):
            continue
        link = link_tag["href"].split("?")[0]

        title_tag = item.select_one(".woocommerce-loop-product__title, h2, h3")
        name = title_tag.get_text(strip=True) if title_tag else link_tag.get_text(strip=True)
        if not name:
            continue

        price_tag = item.select_one(".price")
        price = price_tag.get_text(" ", strip=True) if price_tag else ""

        cart_btn = item.select_one("[data-product_id]")
        pid = cart_btn["data-product_id"] if cart_btn else link

        img_tag = item.select_one("img")
        image = (img_tag.get("data-src") or img_tag.get("src")) if img_tag else None

        products.append({
            "id": str(pid), "name": name, "price": price,
            "link": link, "image": image, "in_stock": True,
        })

    return products


# ---------------------------------------------------------------------------
# Scraping — Magento
# ---------------------------------------------------------------------------

def fetch_magento(store_url: str):
    """Return ALL products from a Magento category page, each tagged with
    in_stock True/False based on visible status text (Out of Stock / Coming
    Soon / etc). Unlike WooCommerce, out-of-stock items ARE included here so
    we can detect the moment they flip to available."""
    resp = requests.get(store_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = []
    items = soup.select("li.product-item") or soup.select("div.product-item-info")

    for item in items:
        link_tag = item.select_one("a.product-item-link") or item.select_one("strong.product-item-name a")
        if not link_tag or not link_tag.get("href"):
            continue
        link = link_tag["href"].split("?")[0]
        name = link_tag.get_text(strip=True)
        if not name:
            continue

        price_tag = item.select_one(".price")
        price = price_tag.get_text(" ", strip=True) if price_tag else ""

        img_tag = item.select_one("img")
        image = (img_tag.get("data-src") or img_tag.get("src")) if img_tag else None

        item_text = item.get_text(" ", strip=True).lower()
        in_stock = not any(kw in item_text for kw in OOS_KEYWORDS)

        products.append({
            "id": link, "name": name, "price": price,
            "link": link, "image": image, "in_stock": in_stock,
        })

    return products


# ---------------------------------------------------------------------------
# Scraping — Shopify (Nintendo SA, Toy Kingdom, Level Up Store)
# ---------------------------------------------------------------------------

def fetch_shopify(store_url: str):
    """store_url here is the base site URL plus the collection handle, e.g.
    'https://store.nintendo.co.za/collections/pokemon-tcg'. We convert that
    into the public /products.json feed Shopify exposes on every collection —
    the same reliable approach used for Lemkus, Nude Project, and Denim Tears.
    Every product's variants are checked for availability; a product counts
    as in_stock if ANY variant is available."""
    # Turn ".../collections/<handle>" into ".../collections/<handle>/products.json"
    base = store_url.split("?")[0].rstrip("/")
    json_url = f"{base}/products.json"

    products = []
    page = 1
    while True:
        resp = requests.get(json_url, params={"limit": 250, "page": page}, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("products", [])
        if not data:
            break

        for p in data:
            variants = p.get("variants", [])
            in_stock = any(v.get("available") for v in variants)
            price = ""
            try:
                price = f"R{float(variants[0]['price']):,.2f}"
            except (IndexError, KeyError, ValueError, TypeError):
                pass
            image = None
            if p.get("images"):
                image = p["images"][0].get("src")

            # Reconstruct the product page URL from the store's root domain
            root = "/".join(base.split("/")[:3])  # https://domain.com
            link = f"{root}/products/{p['handle']}"

            products.append({
                "id": str(p["id"]), "name": p["title"], "price": price,
                "link": link, "image": image, "in_stock": in_stock,
            })

        if len(data) < 250:
            break
        page += 1

    return products


# ---------------------------------------------------------------------------
# Scraping — Generic best-effort (unconfirmed platforms, e.g. Gengar Games)
# ---------------------------------------------------------------------------

def fetch_generic(store_url: str):
    """Best-effort fallback for stores whose exact platform isn't confirmed.
    Looks for any product-card-like link with visible text, then checks the
    surrounding container for a price and any out-of-stock keyword. Less
    reliable than the platform-specific fetchers above — if this store
    consistently parses 0 products, its real markup needs to be inspected
    and a proper selector added."""
    resp = requests.get(store_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    products = []
    seen_links = set()
    price_re = re.compile(r"R\s?[\d,]+\.\d{2}")

    for a in soup.find_all("a", href=True):
        name = a.get_text(strip=True)
        if not name or len(name) < 3:
            continue
        href = a["href"]
        if href in seen_links:
            continue

        # Walk up a few parent levels looking for a price and stock text —
        # same heuristic used for the Shelflife bot's unconfirmed markup.
        price = ""
        in_stock = True
        node = a
        for _ in range(4):
            if node.parent is None:
                break
            node = node.parent
            text_blob = node.get_text(" ", strip=True)
            price_match = price_re.search(text_blob)
            if price_match and not price:
                price = price_match.group(0)
            if any(kw in text_blob.lower() for kw in OOS_KEYWORDS):
                in_stock = False
            if price:
                break

        if not price:
            continue  # not a product card, just skip it

        seen_links.add(href)
        img_tag = a.find("img") or (a.parent.find("img") if a.parent else None)
        image = (img_tag.get("data-src") or img_tag.get("src")) if img_tag else None
        link = href if href.startswith("http") else store_url.split("/", 3)[0] + "//" + store_url.split("/", 3)[2] + href

        products.append({
            "id": link, "name": name, "price": price,
            "link": link, "image": image, "in_stock": in_stock,
        })

    return products


PLATFORM_FETCHERS = {
    "woocommerce": fetch_woocommerce,
    "magento": fetch_magento,
    "shopify": fetch_shopify,
    "generic": fetch_generic,
}


def fetch_products(store: dict):
    fetcher = PLATFORM_FETCHERS.get(store["platform"])
    if not fetcher:
        raise ValueError(f"Unknown platform: {store['platform']}")
    return fetcher(store["url"])


# ---------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_discord_alert(store_name: str, product: dict):
    if not DISCORD_WEBHOOK_URL or "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        log.warning("No Discord webhook configured — would have alerted: %s", product["name"])
        return

    embed = {
        "title": product["name"],
        "url": product["link"],
        "description": f"**{product['price']}**\nJust became available at **{store_name}**",
        "color": 0xE3350D,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if product.get("image"):
        embed["thumbnail"] = {"url": product["image"]}

    payload = {"content": f"🚨 **New stock at {store_name}!**", "embeds": [embed]}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Discord alert for %s: %s", product["name"], e)


def post_startup_message():
    if not DISCORD_WEBHOOK_URL or "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        return
    store_names = ", ".join(s["name"] for s in STORES)
    payload = {
        "embeds": [{
            "title": "✅ Pokémon Stock Bot is online",
            "description": f"Watching: {store_names}\nChecking every {CHECK_INTERVAL_SECONDS}s.",
            "color": 0x95A5A6,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.error("Failed to post startup message: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once():
    state = load_state()
    total_new = 0

    for store in STORES:
        name = store["name"]
        try:
            current = fetch_products(store)
        except requests.RequestException as e:
            log.error("Could not fetch %s: %s", name, e)
            continue
        except ValueError as e:
            log.error(str(e))
            continue

        if not current:
            log.warning("%s: parsed 0 products — the site's markup may not match "
                        "the selectors used. Send this to Claude to fix.", name)
            continue

        previous = state.get(name, {})
        first_run = name not in state
        current_by_id = {p["id"]: p for p in current}

        for pid, product in current_by_id.items():
            was_in_stock = previous.get(pid, {}).get("in_stock", False)
            became_available = product["in_stock"] and not was_in_stock

            if became_available and not first_run:
                log.info("RESTOCK/NEW — %s: %s", name, product["name"])
                send_discord_alert(name, product)
                total_new += 1

        if first_run:
            log.info("%s: first run, recorded %d products as baseline.", name, len(current_by_id))
        elif total_new == 0:
            log.info("%s: no changes (%d products tracked).", name, len(current_by_id))

        state[name] = current_by_id

    save_state(state)
    return total_new


def main():
    log.info("Starting Pokémon stock bot. Watching %d store(s).", len(STORES))
    post_startup_message()
    while True:
        try:
            run_once()
        except Exception as e:
            log.error("Unexpected error during check: %s", e)
        log.info("Sleeping %ss...", CHECK_INTERVAL_SECONDS)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
