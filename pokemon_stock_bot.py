#!/usr/bin/env python3
"""
Pokemon Stock Tracker -> Discord Webhook

Every product is classified into one of three statuses (not just a flat
in-stock/out-of-stock flag):
  📦 STAGED    — visible on the site, nothing purchasable, no preorder
                 wording. Usually means it was just published ahead of a
                 scheduled drop — listed but sales haven't opened yet.
  🕒 PREORDER  — preorder wording detected (title/tags/page text), whether
                 or not it's currently purchasable.
  ✅ LIVE      — purchasable right now, no preorder wording — a genuine
                 immediate-stock restock or new drop.

The bot alerts on two kinds of events: a brand-new listing appearing (with
whatever status it shows up in — including STAGED, so you know something's
been loaded ahead of a drop even before it's buyable), and any status change
on a product it's already seen (e.g. staged -> preorder, preorder -> live).

Note: a product with zero public visibility (never published to the live
site at all) can't be detected by any scraper — there's nothing to see until
a store actually publishes the listing, even if sales aren't open yet.

Supports four store platforms:

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
    {
        "name": "Legendary Loot — Preorders",
        "platform": "shopify",
        "url": "https://legendaryloot.co.za/collections/preorders",
    },
    {
        "name": "Rocket Grunt TCG",
        "platform": "woocommerce",
        "url": "https://rocketgrunttcg.co.za/shop/?stock_status=instock&per_page=-1",
    },
    {
        "name": "Geek Zone",
        "platform": "woocommerce",
        "url": "https://www.geek-zone.co.za/shop/?stock_status=instock&per_page=-1",
    },
    {
        "name": "Geekstop ZA",
        "platform": "shopify",
        "url": "https://www.gstopza.co.za/collections/all-pokemon-cards",
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
PREORDER_KEYWORDS = ("pre-order", "preorder", "pre order")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pokemon-bot")

# ---------------------------------------------------------------------------
# Stock status levels
# ---------------------------------------------------------------------------
# Every product gets classified into one of three states instead of a flat
# in-stock/out-of-stock flag:
#
#   STAGED    — visible on the site, nothing purchasable, no preorder wording.
#                Usually means the listing was just published ahead of a
#                scheduled drop — the product "exists" but sales haven't
#                opened yet. This is the "loaded on the backend first"
#                signal you're looking for.
#   PREORDER  — visible on the site with preorder language detected (in the
#                title, tags, or page text), whether or not it's currently
#                purchasable. Preorders ship later even when you can pay now.
#   LIVE      — purchasable right now, no preorder wording. A genuine
#                immediate-stock restock or new drop.
#
# Note: a product with zero public visibility at all (never published to the
# live site) can't be detected by any scraper — there's nothing to see until
# a store actually publishes the listing, even if sales aren't open yet.

STATUS_STAGED = "staged"
STATUS_PREORDER = "preorder"
STATUS_LIVE = "live"

STATUS_LABELS = {
    STATUS_STAGED: "📦 Staged (not yet for sale)",
    STATUS_PREORDER: "🕒 Preorder",
    STATUS_LIVE: "✅ Live / In Stock",
}

STATUS_COLORS = {
    STATUS_STAGED: 0x95A5A6,   # grey
    STATUS_PREORDER: 0x9B59B6,  # purple
    STATUS_LIVE: 0x2ECC71,      # green
}

# ---------------------------------------------------------------------------
# Filtering & priority tiers
# ---------------------------------------------------------------------------
# Every product now gets sorted into one of three buckets:
#
#   EXCLUDED   — never alerted, never even shown in the batched summary.
#                 Non-Pokémon TCGs (Yu-Gi-Oh, MTG, etc.) and Japanese/Chinese/
#                 Korean import variants, since you only want English Pokémon.
#   ROUTINE    — a normal Pokémon product, batched into the one-message-per-
#                 store summary (unchanged from before).
#   PRIORITY   — booster boxes, ETBs, and other big-ticket items — its own
#                 spotlighted message, gold highlight, optional role ping.
#   ANNIVERSARY — anything 30th Celebration related, in ANY form (binder,
#                 poster, blister, booster, UPC, ETB, tin, whatever) — the
#                 highest tier right now. Easy to dial back down after the
#                 30th hype window passes: just trim ANNIVERSARY_KEYWORDS
#                 back to nothing, or delete the block entirely.

EXCLUDE_KEYWORDS = (
    # Non-Pokémon trading card games — you only want Pokémon
    "yu-gi-oh", "yugioh", "magic: the gathering", "magic the gathering",
    " mtg ", "digimon", "one piece card game", "disney lorcana", "lorcana",
    "dragon ball super card game", "dragon ball fusion world", "flesh and blood",
    # Import language variants — English only
    "japanese", "japan import", "(jp)", " jp ver", "chinese", "korean",
    "(cn)", "(kr)", "s-chinese", "t-chinese",
)

# General high-value product types — always worth their own spotlight,
# regardless of which set they belong to.
PRIORITY_KEYWORDS = (
    "booster box", "booster case", "elite trainer box", "etb",
    "ascended heroes", "perfect order", "prismatic evolutions", "151",
    "evolving skies", "binder", "poster", "blister", "upc",
    "ultra premium collection", "premium collection", "tin",
)

# TEMPORARY — the 30th Celebration hype window. Anything matching these
# jumps to the very top tier regardless of product type: binders, posters,
# blister packs, boosters, UPCs, ETBs, all of it. Trim this list back to
# empty once the 30th launch window has passed and routine priority rules
# (above) are enough again.
ANNIVERSARY_KEYWORDS = (
    "30th", "30th celebration", "30th anniversary", "first partner",
    "celebration 2026",
)


def is_excluded(name: str) -> bool:
    name_lower = (name or "").lower()
    return any(kw in name_lower for kw in EXCLUDE_KEYWORDS)


def is_anniversary(name: str) -> bool:
    name_lower = (name or "").lower()
    return any(kw in name_lower for kw in ANNIVERSARY_KEYWORDS)


def is_priority(name: str) -> bool:
    name_lower = (name or "").lower()
    return any(kw in name_lower for kw in PRIORITY_KEYWORDS) or is_anniversary(name_lower)


def classify_status(name: str, extra_text: str, in_stock: bool) -> str:
    """Decide STAGED / PREORDER / LIVE from whatever text signals a given
    platform's fetcher can gather (title, tags, surrounding page text)."""
    combined = f"{name or ''} {extra_text or ''}".lower()
    is_preorder = any(kw in combined for kw in PREORDER_KEYWORDS)
    if is_preorder:
        return STATUS_PREORDER
    if in_stock:
        return STATUS_LIVE
    return STATUS_STAGED

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

        item_text = item.get_text(" ", strip=True)
        status = classify_status(name, item_text, in_stock=True)

        products.append({
            "id": str(pid), "name": name, "price": price,
            "link": link, "image": image, "in_stock": True, "status": status,
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
        status = classify_status(name, item_text, in_stock)

        products.append({
            "id": link, "name": name, "price": price,
            "link": link, "image": image, "in_stock": in_stock, "status": status,
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

            # Shopify exposes tags/product_type — a strong, explicit signal
            # for preorders that most stores label consistently.
            extra_text = f"{p.get('tags', '')} {p.get('product_type', '')}"
            status = classify_status(p["title"], extra_text, in_stock)

            products.append({
                "id": str(p["id"]), "name": p["title"], "price": price,
                "link": link, "image": image, "in_stock": in_stock, "status": status,
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
        combined_text = ""
        node = a
        for _ in range(4):
            if node.parent is None:
                break
            node = node.parent
            text_blob = node.get_text(" ", strip=True)
            combined_text = text_blob  # widest blob wins, accumulates naturally going up
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
        status = classify_status(name, combined_text, in_stock)

        products.append({
            "id": link, "name": name, "price": price,
            "link": link, "image": image, "in_stock": in_stock, "status": status,
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

def send_discord_alert(store_name: str, product: dict, reason: str, previous_status: str = None):
    """reason is either 'new_listing' (first time we've ever seen this
    product) or 'status_change' (we've seen it before, but its status moved
    — e.g. staged -> preorder, preorder -> live, staged -> live)."""
    if not DISCORD_WEBHOOK_URL or "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        log.warning("No Discord webhook configured — would have alerted: %s", product["name"])
        return

    status = product.get("status", STATUS_LIVE)
    status_label = STATUS_LABELS.get(status, status)
    color = STATUS_COLORS.get(status, 0xE3350D)
    priority = is_priority(product["name"])

    if reason == "new_listing":
        header = f"🆕 New listing spotted at {store_name}"
        description = f"**{product['price']}**\nStatus: **{status_label}**"
        if status == STATUS_STAGED:
            description += "\n_Just appeared on the site but isn't purchasable yet — possibly staged ahead of a scheduled drop._"
    else:
        prev_label = STATUS_LABELS.get(previous_status, previous_status or "unknown")
        header = f"🔁 Status change at {store_name}"
        description = f"**{product['price']}**\n{prev_label} → **{status_label}**"

    if is_anniversary(product["name"]):
        header = f"🎉 30TH CELEBRATION — {header}"
        color = 0xFF69B4  # pink, distinct from the standard priority gold
        role_id = os.environ.get("ANNIVERSARY_ROLE_ID", "") or os.environ.get("PRIORITY_ROLE_ID", "")
        content_prefix = f"<@&{role_id}> " if role_id else ""
    elif priority:
        header = f"🔥 PRIORITY — {header}"
        color = 0xF1C40F  # gold, overrides the normal status color for visibility
        # Optional: ping a role for priority items only. Set PRIORITY_ROLE_ID
        # as an environment variable (the role's numeric Discord ID) to
        # enable this — leave unset to just get the gold highlight with no ping.
        role_id = os.environ.get("PRIORITY_ROLE_ID", "")
        content_prefix = f"<@&{role_id}> " if role_id else ""
    else:
        content_prefix = ""

    embed = {
        "title": product["name"],
        "url": product["link"],
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if product.get("image"):
        embed["thumbnail"] = {"url": product["image"]}

    payload = {"content": f"{content_prefix}**{header}**", "embeds": [embed]}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send Discord alert for %s: %s", product["name"], e)


def send_batched_alert(store_name: str, changes: list):
    """changes is a list of (reason, product, previous_status) tuples for
    ordinary (non-priority) items from ONE store in ONE check cycle. Instead
    of one Discord message per product, this sends a single message with
    one field per change — much less noisy when several things move at once."""
    if not DISCORD_WEBHOOK_URL or "PASTE_YOUR" in DISCORD_WEBHOOK_URL:
        return
    if not changes:
        return

    fields = []
    for reason, product, previous_status in changes[:25]:  # Discord's field cap
        status = product.get("status", STATUS_LIVE)
        status_label = STATUS_LABELS.get(status, status)
        if reason == "new_listing":
            value = f"[{product['price']}]({product['link']}) — New, status: {status_label}"
        else:
            prev_label = STATUS_LABELS.get(previous_status, previous_status or "unknown")
            value = f"[{product['price']}]({product['link']}) — {prev_label} → {status_label}"
        fields.append({"name": product["name"][:256], "value": value, "inline": False})

    overflow = len(changes) - len(fields)
    embed = {
        "title": f"📋 {len(changes)} update(s) at {store_name}",
        "color": 0x3498DB,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if overflow > 0:
        embed["footer"] = {"text": f"+{overflow} more not shown — check the site directly"}

    payload = {"embeds": [embed]}
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to send batched alert for %s: %s", store_name, e)


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

        excluded_count = sum(1 for p in current if is_excluded(p["name"]))
        current = [p for p in current if not is_excluded(p["name"])]
        if excluded_count:
            log.info("%s: filtered out %d excluded product(s) (non-Pokémon TCG or import variant).",
                      name, excluded_count)
        if not current:
            continue

        previous = state.get(name, {})
        first_run = name not in state
        current_by_id = {p["id"]: p for p in current}
        routine_changes = []  # (reason, product, previous_status) — batched together
        store_changes = 0

        for pid, product in current_by_id.items():
            product.setdefault("status", STATUS_LIVE if product.get("in_stock") else STATUS_STAGED)
            prev_entry = previous.get(pid)

            if prev_entry is None:
                # Brand new listing we've never seen before. Only alert once
                # we're past the very first run (otherwise the whole existing
                # catalog would fire as "new" the moment the bot starts).
                if not first_run:
                    log.info("NEW LISTING — %s: %s (%s)", name, product["name"], product["status"])
                    if is_priority(product["name"]):
                        send_discord_alert(name, product, reason="new_listing")
                    else:
                        routine_changes.append(("new_listing", product, None))
                    store_changes += 1
            else:
                prev_status = prev_entry.get("status", STATUS_STAGED)
                if product["status"] != prev_status:
                    log.info("STATUS CHANGE — %s: %s (%s -> %s)",
                             name, product["name"], prev_status, product["status"])
                    if is_priority(product["name"]):
                        send_discord_alert(name, product, reason="status_change", previous_status=prev_status)
                    else:
                        routine_changes.append(("status_change", product, prev_status))
                    store_changes += 1

        if routine_changes:
            send_batched_alert(name, routine_changes)

        if first_run:
            log.info("%s: first run, recorded %d products as baseline.", name, len(current_by_id))
        elif store_changes == 0:
            log.info("%s: no changes (%d products tracked).", name, len(current_by_id))

        total_new += store_changes
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
