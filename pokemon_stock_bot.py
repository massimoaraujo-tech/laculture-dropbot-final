#!/usr/bin/env python3
"""Pokémon 🎴 — original multi-platform SA Pokémon TCG tracker -> Discord.

Same engine as Pikachu / Snorlax (levels, instant-checkout buttons, restock
debounce, quarantine, 30th stock report), with this bot's own store list
across Shopify, WooCommerce, Magento and generic sites.

Takealot is NOT in here any more — it has its own standalone bot.
Start command on Railway stays: python pokemon_stock_bot.py
"""
import json
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
try:
    from curl_cffi import requests as cffi   # looks like real Chrome to store firewalls
except Exception:
    cffi = None

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pokemon")

BOT_NAME, BOT_EMOJI = "Pokémon", "🎴"

# ---------------- Settings (all optional Railway variables) ----------------
# Old variable names still work, so the existing Railway config doesn't break.
WEBHOOK = os.getenv("DISCORD_WEBHOOK") or os.getenv("DISCORD_WEBHOOK_URL", "")
WEBHOOK_30TH = os.getenv("WEBHOOK_30TH", "")        # #30th-alerts channel (Level 1 goes ONLY there)
ROLE_30TH = os.getenv("ROLE_30TH_ID") or os.getenv("ANNIVERSARY_ROLE_ID", "")  # blank = @everyone
PROXY_URL = os.getenv("PROXY_URL", "")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))
WORKERS = int(os.getenv("WORKERS", "8"))
REPORT_HOURS = float(os.getenv("REPORT_HOURS", "6"))
OOS_CONFIRM = int(os.getenv("OOS_CONFIRM", "3"))
COOLDOWN_HOURS = float(os.getenv("RESTOCK_COOLDOWN_HOURS", "2"))
FAIL_LIMIT = int(os.getenv("FAIL_LIMIT", "5"))
RETRY_HOURS = float(os.getenv("RETRY_HOURS", "24"))
ONLY_30TH = os.getenv("ONLY_30TH", "false").lower() == "true"
ENABLE_BUTTONS = os.getenv("ENABLE_BUTTONS", "true").lower() == "true"
STATE_FILE = Path(os.getenv("STATE_DIR", ".")) / "state_pokemon.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
TIMEOUT = 30

# ---------------- Stores ----------------
STORES = [
    {"name": "Poké Store", "platform": "woocommerce",
     "url": "https://pokestore.co.za/shop/?orderby=date&per_page=-1"},
    {"name": "Toys R Us SA — Pokémon", "platform": "magento",
     "url": "https://www.toysrus.co.za/pokemon-promo?product_list_limit=100"},
    {"name": "Toys R Us SA — Trading Cards", "platform": "magento",
     "url": "https://www.toysrus.co.za/trading-cards-shop-all/pokemon?product_list_limit=100"},
    {"name": "Nintendo SA", "platform": "shopify",
     "url": "https://store.nintendo.co.za/collections/pokemon-trading-cards"},
    {"name": "Toy Kingdom", "platform": "shopify",
     "url": "https://toykingdom.co.za/collections/pokemon-cards"},
    {"name": "Level Up Store", "platform": "shopify",
     "url": "https://levelupstore.co.za/collections/pokemon-cards"},
    {"name": "Big Bang Shop", "platform": "shopify",
     "url": "https://bigbangshop.co.za/collections/pokemon-trading-card-game"},
    {"name": "ThunderBolt Gaming", "platform": "woocommerce",
     "url": "https://tbgaming.co.za/shop/?per_page=-1"},
    # Gengar Games is switched off: they rebuilt their website, so every old link
    # (including the singles page) now returns 404, and the new site loads its
    # products with JavaScript. Send Claude the link to their Pokémon sealed page.
    # {"name": "Gengar Games", "platform": "generic",
    #  "url": "PASTE-GENGAR-SEALED-OR-SEARCH-LINK-HERE"},
    {"name": "Wordsworth", "platform": "shopify",
     "url": "https://www.wordsworth.co.za/collections/pokemon-1"},
    {"name": "Legendary Loot — Preorders", "platform": "shopify",
     "url": "https://legendaryloot.co.za/collections/preorders"},
    {"name": "Rocket Grunt TCG", "platform": "woocommerce",
     "url": "https://rocketgrunttcg.co.za/shop/?per_page=-1"},
    # Geek Zone: their whole-shop page is mostly singles (and blocked bots), so we
    # watch the two pages that matter instead.
    {"name": "Geek Zone — 30th Anniversary", "platform": "woocommerce",
     "url": "https://www.geek-zone.co.za/shop/pokemon-30th-anniversary/?per_page=-1"},
    {"name": "Geek Zone — Pokémon Sealed", "platform": "woocommerce",
     "url": "https://www.geek-zone.co.za/shop/pokemon/pokemon-sealed/?per_page=-1"},
    {"name": "Comic Warehouse", "platform": "woocommerce",
     "url": "https://comicwarehouse.co.za/product-category/shop-by-franchise/pokemon/?per_page=-1"},
    {"name": "Geekstop ZA", "platform": "shopify",
     "url": "https://www.gstopza.co.za/collections/all-pokemon-cards"},
]

# ---------------- Filters: English sealed Pokémon TCG, 30th = Level 1 ----------------
POKEMON_RE = re.compile(r"pok[eé]mon", re.I)
TCG_RE = re.compile(
    r"tcg|trading card|booster|elite trainer|\betb\b|\btin\b|blister|collection|"
    r"battle deck|premium|bundle|binder|poster|display|sleeved|build (and|&) battle|"
    r"card game|scarlet|violet|mega evolution|\bupc\b", re.I)
EXCLUDE_RE = re.compile(
    r"japan|japanese|\bjp\b|chinese|\bcn\b|korean|\bkr\b|simplified|yu-?gi-?oh|"
    r"magic: the gathering|\bmtg\b|one piece|digimon|dragon ball|lorcana|flesh and blood|"
    r"plush|nintendo switch|t-shirt|hoodie|costume|lunch ?box", re.I)
PRIORITY_RE = re.compile(r"30th|\bcelebration\b|first partner", re.I)
SINGLES_TITLE_RE = re.compile(
    r"\b\d{1,3}/\d{2,3}\b|reverse holo|full art|illustration rare|secret rare|"
    r"ultra rare|holo rare|hyper rare|\bpsa ?\d|\bcgc ?\d|\bbgs ?\d|graded|slab|"
    r"single card", re.I)
SINGLES_META_RE = re.compile(r"single|graded|slab|\bpsa\b|\bcgc\b|\bbgs\b", re.I)
PREORDER_RE = re.compile(r"pre-?\s?order", re.I)
OOS_RE = re.compile(r"out of stock|sold out|coming soon|notify me", re.I)


def is_priority(title):
    return bool(PRIORITY_RE.search(title))


def is_wanted(item):
    title, meta = item["title"], item.get("meta") or ""
    if SINGLES_TITLE_RE.search(title) or (meta and SINGLES_META_RE.search(meta)):
        return False
    # Store pages are already Pokémon-only, so a TCG word is enough when the
    # title itself doesn't say "Pokémon" (common on SA stores).
    everything = f"{title} {meta} {item.get('store_hint', '')}"
    return bool(POKEMON_RE.search(everything) and TCG_RE.search(f"{title} {meta}")
                and not EXCLUDE_RE.search(title))


# ---------------- RRP check for 30th items ----------------
def _load_rrp():
    raw = os.getenv("RRP_30TH", "elite trainer=1299|booster bundle=699|sticker=499|mini tin=299")
    out = []
    for part in raw.split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out.append((k.strip().lower(), float(v)))
            except ValueError:
                pass
    return out


RRP_30TH = _load_rrp()


def money(text):
    """'R 1,299.00' -> 1299.0"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"R\s?([\d\s.,]+)", str(text)) or re.search(r"([\d][\d\s.,]*)", str(text))
    if not m:
        return None
    t = re.sub(r"[^\d.,]", "", m.group(1))
    if re.search(r",\d{2}$", t):
        t = t.replace(".", "").replace(",", ".")
    t = t.replace(",", "")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def rrp_note(item):
    if not is_priority(item["title"]) or not item.get("price_value"):
        return None
    low = item["title"].lower()
    for key, rrp in RRP_30TH:
        if key in low:
            diff = item["price_value"] - rrp
            if diff <= rrp * 0.05:
                return f"✅ At RRP (R{rrp:,.0f})"
            return f"⚠️ R{diff:,.0f} above RRP (R{rrp:,.0f})"
    return None


def fmt_price(v):
    return f"R {v:,.2f}" if v else "—"


# ---------------- Discord ----------------
def post_webhook(payload, buttons=None, flags=0, url=None):
    url = url or WEBHOOK
    if not url:
        log.error("DISCORD_WEBHOOK not set")
        return
    payload = dict(payload)
    target = url
    if flags:
        payload["flags"] = flags
    if buttons and ENABLE_BUTTONS:
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": l[:80], "url": u} for l, u in buttons[:5]]}]
        target = url + ("&" if "?" in url else "?") + "with_components=true"
    for _ in range(4):
        try:
            r = requests.post(target, json=payload, timeout=15)
        except Exception as e:
            log.warning("Discord post failed: %s", e)
            return
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.status_code >= 400 and ("components" in payload or "flags" in payload):
            payload.pop("components", None)     # fall back to a plain message
            payload.pop("flags", None)
            target = url
            continue
        if r.status_code >= 400:
            log.error("Discord error %s: %s", r.status_code, r.text[:200])
        return


def post_long(text, url=None):
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 1900:
            post_webhook({"content": chunk}, url=url)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        post_webhook({"content": chunk}, url=url)


def mention():
    return f"<@&{ROLE_30TH}>" if ROLE_30TH else "@everyone"


BADGE = {1: "🔴 LEVEL 1 · 30TH CELEBRATION", 2: "🟢 LEVEL 2 · IN STOCK",
         3: "⚪ LEVEL 3 · LISTED (not in stock yet)"}
COLOR = {1: 0xE3350D, 2: 0x2ECC71, 3: 0x95A5A6}
EVENT_ICON = {"NEW LISTING": "🆕", "RESTOCK": "🔁", "PRICE DROP": "📉", "TEST ALERT": "🧪"}


def level_of(item):
    if is_priority(item["title"]):
        return 1
    return 2 if item["in_stock"] else 3


def send_alert(store, event, item):
    lvl = level_of(item)
    fields = [
        {"name": "Price", "value": fmt_price(item.get("price_value")) if item.get("price_value")
         else (item.get("price") or "—"), "inline": True},
        {"name": "Stock", "value": "✅ In stock" if item["in_stock"] else "❌ Out of stock",
         "inline": True},
        {"name": "Store", "value": store, "inline": True},
    ]
    if item.get("preorder"):
        fields.append({"name": "Type", "value": "🕒 Preorder", "inline": True})
    if item.get("was"):
        fields.append({"name": "Was", "value": fmt_price(item["was"]), "inline": True})
    note = rrp_note(item)
    if note:
        fields.append({"name": "RRP check", "value": note, "inline": True})
    embed = {
        "title": item["title"][:256], "url": item["url"], "color": COLOR[lvl],
        "description": f"**{BADGE[lvl]}**\n{EVENT_ICON.get(event, '')} {event}",
        "fields": fields,
        "footer": {"text": f"{BOT_EMOJI} {BOT_NAME} • {store}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if item.get("image"):
        embed["image" if lvl == 1 else "thumbnail"] = {"url": item["image"]}
    if lvl == 1:
        content = f"{mention()} 🔥 **30th Celebration {event}** — {store}"
    else:
        content = f"{EVENT_ICON.get(event, '')} **{event}** — {store}"
    allowed = {"parse": ["everyone"]}
    if ROLE_30TH:
        allowed["roles"] = [ROLE_30TH]
    buttons = [(f"Open on {store}"[:80], item["url"])]
    if item.get("checkout_url") and item["in_stock"]:
        label = "🕒 Preorder now" if item.get("preorder") else (item.get("checkout_label") or "Add to cart")
        buttons.insert(0, (label, item["checkout_url"]))
    payload = {"content": content, "embeds": [embed], "allowed_mentions": allowed}
    if lvl == 1 and WEBHOOK_30TH:
        post_webhook(payload, buttons, url=WEBHOOK_30TH)                # 30th -> #30th-alerts only
    else:
        post_webhook(payload, buttons, flags=4096 if lvl == 3 else 0)   # Level 3 = silent


def short_error(e):
    t = str(e)
    if "Failed to resolve" in t or "NameResolution" in t or "Could not resolve" in t:
        return "link doesn't exist"
    if "timed out" in t or "Timeout" in t:
        return "site didn't respond (timed out)"
    if "SSL" in t or "certificate" in t:
        return "site's security certificate is broken"
    if "Expecting value" in t:
        return "didn't return product data"
    return t.split("\n")[0][:120]


# ---------------- State ----------------
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text())
    except Exception:
        s = {}
    s.setdefault("products", {})
    s.setdefault("stores", {})
    s.setdefault("seeded", [])
    return s


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        log.warning("Could not save state: %s", e)


def handle_item(state, store, key, item, seeded, now):
    old = state["products"].get(item["id"])
    pv = item.get("price_value")
    event = None
    if old is None:
        event = "NEW LISTING"
        rec = {"in_stock": item["in_stock"], "oos": 0, "last_alert": 0, "price": pv}
    else:
        rec = dict(old)
        rec.setdefault("oos", 0)
        rec.setdefault("last_alert", 0)
        if item["in_stock"]:
            if not old.get("in_stock"):
                event = "RESTOCK"
            rec["in_stock"], rec["oos"] = True, 0
            prev = old.get("price")
            if event is None and pv and prev and pv <= prev * 0.95 and prev - pv >= 20:
                event, item["was"] = "PRICE DROP", prev
        else:
            rec["oos"] += 1
            if rec["oos"] >= OOS_CONFIRM:
                rec["in_stock"] = False
        if pv:
            rec["price"] = pv
    rec["store"] = key
    if event in ("RESTOCK", "PRICE DROP") and now - rec["last_alert"] < COOLDOWN_HOURS * 3600:
        event = None
    if event and seeded and (not ONLY_30TH or is_priority(item["title"])):
        send_alert(store, event, item)
        rec["last_alert"] = now
    state["products"][item["id"]] = rec


def mark_missing(state, key, seen_ids):
    """Products that vanish from a store page (common when a sold-out item gets
    hidden) count as a sold-out check, so they can fire RESTOCK when they return."""
    for pid, rec in state["products"].items():
        if rec.get("store") == key and pid not in seen_ids and rec.get("in_stock"):
            rec["oos"] = rec.get("oos", 0) + 1
            if rec["oos"] >= OOS_CONFIRM:
                rec["in_stock"] = False


# ---------------- Main loop ----------------
def run(get_stores, fetch, parallel=True):
    state = load_state()
    if os.getenv("TEST_ALERT") == "1":
        send_alert("Test Store", "TEST ALERT", {
            "title": "Pokémon TCG: 30th Celebration Elite Trainer Box (test)",
            "url": "https://store.nintendo.co.za", "price_value": 1299.0,
            "in_stock": True, "image": None, "checkout_url": "https://store.nintendo.co.za",
            "checkout_label": "⚡ Checkout now"})
    startup, last_report = True, 0.0
    while True:
        now = time.time()
        stores = get_stores()
        active = []
        for name, key, opts in stores:
            rec = state["stores"].setdefault(key, {"fails": 0, "quarantined": False, "since": 0})
            if rec["quarantined"] and not startup and now - rec["since"] < RETRY_HOURS * 3600:
                continue
            active.append((name, key, opts))

        results = {}
        if parallel and len(active) > 1:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futs = {k: pool.submit(fetch, k, o) for _, k, o in active}
                for k, f in futs.items():
                    try:
                        results[k] = f.result()
                    except Exception as e:
                        results[k] = e
        else:
            for _, k, o in active:
                try:
                    results[k] = fetch(k, o)
                except Exception as e:
                    results[k] = e

        ok, failed, tracked, in_stock, pri, hot = 0, [], 0, 0, 0, []
        for name, key, opts in active:
            rec, res = state["stores"][key], results.get(key)
            if isinstance(res, Exception):
                reason = short_error(res)
                rec["fails"] += 1
                log.warning("%s failed (%d in a row): %s", name, rec["fails"], res)
                failed.append(f"{name} — {reason}")
                if rec["quarantined"]:
                    rec["since"] = now
                elif rec["fails"] >= FAIL_LIMIT:
                    rec.update(quarantined=True, since=now)
                    post_webhook({"content": f"🚫 **{name} quarantined** — failed {rec['fails']} checks "
                                             f"in a row ({reason}). Skipping it so the other stores "
                                             f"keep running; retrying every {RETRY_HOURS:g}h. "
                                             f"Fix its link in STORES when you have time."})
                continue
            if rec["quarantined"]:
                post_webhook({"content": f"✅ **{name} is working again** — back on the watchlist."})
            rec.update(fails=0, quarantined=False)
            ok += 1
            seeded = key in state["seeded"]
            seen = set()
            for item in res:
                if not is_wanted(item):
                    continue
                seen.add(item["id"])
                tracked += 1
                in_stock += item["in_stock"]
                if is_priority(item["title"]):
                    pri += 1
                    if item["in_stock"]:
                        hot.append((name, item))
                handle_item(state, name, key, item, seeded, now)
            if seen:                       # empty page = likely a parse glitch, don't touch stock
                mark_missing(state, key, seen)
            else:
                log.warning("%s: 0 Pokémon TCG products parsed — markup may have changed.", name)
            if not seeded:
                state["seeded"].append(key)
        save_state(state)
        log.info("Round done: %d/%d OK, %d tracked", ok, len(active), tracked)

        if startup:
            mode = "30th Celebration only" if ONLY_30TH else "all Pokémon TCG, 30th = Level 1"
            msg = (f"{BOT_EMOJI} **{BOT_NAME} online** ({mode}) — {ok}/{len(active)} stores working, "
                   f"tracking {tracked} Pokémon TCG products ({in_stock} in stock, {pri} 30th Celebration).")
            if failed:
                msg += (f"\n\n**Not working (auto-quarantined after {FAIL_LIMIT} failed checks):**\n"
                        + "\n".join(f"• {f}" for f in failed))
            post_long(msg)
            startup = False

        if now - last_report >= REPORT_HOURS * 3600:
            post_report(hot, [n for n, k, _ in stores if state["stores"].get(k, {}).get("quarantined")])
            last_report = now
        time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))


def post_report(hot, quarantined):
    if hot:
        by_store = {}
        for store, item in hot:
            by_store.setdefault(store, []).append(item)
        lines = [f"🔥 **{BOT_EMOJI} {BOT_NAME} — 30th Celebration IN STOCK right now** "
                 f"({len(hot)} products at {len(by_store)} stores)"]
        for store in sorted(by_store):
            lines.append(f"\n**{store}**")
            for it in sorted(by_store[store], key=lambda x: x["title"]):
                price = fmt_price(it.get("price_value")) if it.get("price_value") else (it.get("price") or "—")
                note = rrp_note(it)
                lines.append(f"• [{it['title'][:90]}]({it['url']}) — {price}"
                             + (f" · {note}" if note else ""))
        post_long("\n".join(lines), url=WEBHOOK_30TH or None)     # 30th list -> #30th-alerts
        status = [f"{BOT_EMOJI} **{BOT_NAME}** — still running. {len(hot)} 30th products in stock right now."]
    else:
        status = [f"{BOT_EMOJI} **{BOT_NAME}** — still running. No 30th Celebration stock right now."]
    if quarantined:
        status.append("🚫 **Quarantined (fix when you have time):** " + ", ".join(quarantined))
    post_long("\n".join(status))


# ---------------- Fetchers ----------------
def get_stores():
    return [(s["name"], s["name"], s) for s in STORES]


def proxies():
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def browser_session():
    """Chrome look-alike for store pages; helps with sites that block plain bots (403)."""
    if cffi:
        return cffi.Session(impersonate="chrome")
    return session()


def root_of(url):
    return "/".join(url.split("/")[:3])


def get_html(url):
    r = browser_session().get(url, proxies=proxies(), timeout=TIMEOUT)
    if r.status_code in (401, 403, 429):
        raise RuntimeError(f"{r.status_code} — blocked")
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def fetch_shopify(store):
    base = store["url"].split("?")[0].rstrip("/")
    root = root_of(base)
    domain = root.split("//")[1]
    s = session()
    items = []
    for page in range(1, 11):
        r = s.get(f"{base}/products.json", params={"limit": 250, "page": page},
                  proxies=proxies(), timeout=TIMEOUT)
        if r.status_code == 404:
            raise RuntimeError("404 — collection link is wrong")
        if r.status_code in (401, 403, 429):
            raise RuntimeError(f"{r.status_code} — blocked")
        r.raise_for_status()
        batch = r.json().get("products") or []
        for p in batch:
            variants = p.get("variants") or []
            prices = [float(v["price"]) for v in variants if v.get("price")]
            avail = [v for v in variants if v.get("available")]
            tags = p.get("tags")
            tags = " ".join(tags) if isinstance(tags, list) else str(tags or "")
            images = p.get("images") or []
            meta = f"{p.get('product_type') or ''} {p.get('vendor') or ''} {tags}"
            items.append({
                "id": f"{domain}:{p['id']}", "title": p.get("title") or "",
                "url": f"{root}/products/{p.get('handle')}",
                "price_value": min(prices) if prices else None,
                "in_stock": bool(avail),
                "preorder": bool(PREORDER_RE.search(f"{p.get('title', '')} {meta}")),
                "image": images[0].get("src") if images else None,
                "meta": meta, "store_hint": "pokemon",
                "checkout_url": f"{root}/cart/{avail[0]['id']}:1" if avail else None,
                "checkout_label": "⚡ Checkout now",
            })
        if len(batch) < 250:
            break
        time.sleep(1)
    return items


def fetch_woocommerce(store):
    soup = get_html(store["url"])
    root = root_of(store["url"])
    items = []
    for li in soup.select("li.product") or soup.select(".products .product"):
        a = (li.select_one("a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link")
             or li.find("a", href=re.compile(r"/product/")))
        if not a or not a.get("href"):
            continue
        link = a["href"].split("?")[0]
        t = li.select_one(".woocommerce-loop-product__title, h2, h3")
        title = t.get_text(strip=True) if t else a.get_text(strip=True)
        if not title:
            continue
        classes = " ".join(li.get("class") or [])
        text = li.get_text(" ", strip=True)
        in_stock = "outofstock" not in classes and not OOS_RE.search(text)
        btn = li.select_one("[data-product_id]")
        pid = btn["data-product_id"] if btn else None
        simple = bool(btn and "add_to_cart_button" in " ".join(btn.get("class") or [])
                      and "product_type_simple" in " ".join(btn.get("class") or []))
        img = li.select_one("img")
        price_tag = li.select_one(".price ins .amount") or li.select_one(".price .amount") \
            or li.select_one(".price")
        price = price_tag.get_text(" ", strip=True) if price_tag else ""
        items.append({
            "id": f"{root}:{pid or link}", "title": title, "url": link,
            "price": price, "price_value": money(price), "in_stock": in_stock,
            "preorder": bool(PREORDER_RE.search(text)),
            "image": (img.get("data-src") or img.get("src")) if img else None,
            "meta": classes, "store_hint": "pokemon",
            # adds 1 to cart and lands on checkout — simple products only
            "checkout_url": f"{root}/checkout/?add-to-cart={pid}" if (pid and simple and in_stock) else None,
            "checkout_label": "⚡ Checkout now",
        })
    return items


def fetch_magento(store):
    soup = get_html(store["url"])
    items = []
    for li in soup.select("li.product-item") or soup.select("div.product-item-info"):
        a = li.select_one("a.product-item-link") or li.select_one("strong.product-item-name a")
        if not a or not a.get("href"):
            continue
        link = a["href"].split("?")[0]
        title = a.get_text(strip=True)
        if not title:
            continue
        text = li.get_text(" ", strip=True)
        price_tag = li.select_one(".price")
        price = price_tag.get_text(" ", strip=True) if price_tag else ""
        img = li.select_one("img")
        items.append({
            "id": link, "title": title, "url": link, "price": price,
            "price_value": money(price), "in_stock": not OOS_RE.search(text),
            "preorder": bool(PREORDER_RE.search(text)),
            "image": (img.get("data-src") or img.get("src")) if img else None,
            "meta": "", "store_hint": "pokemon",
        })
    return items


def fetch_generic(store):
    soup = get_html(store["url"])
    root = root_of(store["url"])
    price_re = re.compile(r"R\s?\d[\d\s,.]*[.,]\d{2}")   # R 1,299.00 and R 1 300,00
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = a.get_text(strip=True)
        href = a["href"]
        if not title or len(title) < 3 or href in seen:
            continue
        price, blob, node = "", "", a
        for _ in range(4):
            if node.parent is None:
                break
            node = node.parent
            blob = node.get_text(" ", strip=True)
            m = price_re.search(blob)
            if m:
                price = m.group(0)
                break
        if not price:
            continue
        seen.add(href)
        link = href if href.startswith("http") else root + ("" if href.startswith("/") else "/") + href
        img = a.find("img") or (a.parent.find("img") if a.parent else None)
        items.append({
            "id": link, "title": title, "url": link, "price": price,
            "price_value": money(price), "in_stock": not OOS_RE.search(blob),
            "preorder": bool(PREORDER_RE.search(blob)),
            "image": (img.get("data-src") or img.get("src")) if img else None,
            "meta": "", "store_hint": "pokemon",
        })
    return items


FETCHERS = {"shopify": fetch_shopify, "woocommerce": fetch_woocommerce,
            "magento": fetch_magento, "generic": fetch_generic}


def fetch(key, store):
    return FETCHERS[store["platform"]](store)


if __name__ == "__main__":
    run(get_stores, fetch, parallel=True)
