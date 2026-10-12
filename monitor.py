"""
RARE DROP - moniteur de stock maison.
Surveille une liste de produits (products.json) et poste une alerte Discord
quand un produit passe de "indisponible" a "disponible / precommande".

Usage :
  python monitor.py           -> tourne selon RUN_FOR / INTERVAL
  python monitor.py --once    -> un seul passage puis stop
  python monitor.py --test    -> envoie un message de test au webhook

Variables d'environnement :
  DISCORD_WEBHOOK  URL du webhook du salon (obligatoire)
  RUN_FOR          duree totale en secondes (0 = infini, pour un PC/VPS). Defaut 0
  INTERVAL         secondes entre deux passages. Defaut 60
  DISCORD_ROLE_ID  (optionnel) role pinge par defaut, ex. @Pokémon
"""
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(HERE, "products.json")
STATE_FILE = os.path.join(HERE, "state.json")

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
RUN_FOR = int(os.environ.get("RUN_FOR", "0"))
INTERVAL = int(os.environ.get("INTERVAL", "60"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

# Mots-cles par defaut (mode "keywords"), surchargeables produit par produit
DEFAULT_OUT = ["rupture de stock", "indisponible", "épuisé", "epuise",
               "victime de son succès", "bientôt disponible", "plus disponible"]
DEFAULT_IN = ["ajouter au panier", "précommander", "precommander", "acheter maintenant"]

session = requests.Session()
session.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- Detection ----------

def check_shopify(p):
    """Boutiques Shopify : l'URL produit + '.js' renvoie un JSON fiable."""
    url = p["url"].split("?")[0].rstrip("/") + ".js"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    price = data.get("price")
    image = data.get("featured_image")
    if image and image.startswith("//"):
        image = "https:" + image
    return bool(data.get("available")), (f"{price / 100:.2f} €" if price else None), image


def check_keywords(p):
    """Pages HTML classiques : on cherche des mots-cles dans la page."""
    r = session.get(p["url"], timeout=20)
    if r.status_code in (403, 429, 503):
        raise RuntimeError(f"HTTP {r.status_code} (probablement bloque par un anti-bot)")
    r.raise_for_status()
    html = r.text.lower()
    out_kw = [k.lower() for k in p.get("out_of_stock", DEFAULT_OUT)]
    in_kw = [k.lower() for k in p.get("in_stock", DEFAULT_IN)]

    if any(k in html for k in out_kw):
        status = False
    elif any(k in html for k in in_kw):
        status = True
    else:
        status = None  # page non reconnue -> on ne conclut rien

    price = None
    m = re.search(r'"price"\s*:\s*"?(\d+[.,]?\d*)', r.text)
    if m:
        price = m.group(1).replace(",", ".") + " €"
    img = None
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r.text)
    if m:
        img = m.group(1)
    return status, price, img


IN_STATES = {"instock", "preorder", "presale", "limitedavailability", "onlineonly"}


def check_jsonld(p):
    """Sites avec donnees schema.org (Play-in, etc.) : champ "availability".
    Page 404 ou sans offre = considere en rupture (le produit peut revenir)."""
    r = session.get(p["url"], timeout=20)
    if r.status_code == 404:
        return False, None, None
    if r.status_code in (403, 429, 503):
        raise RuntimeError(f"HTTP {r.status_code} (probablement bloque par un anti-bot)")
    r.raise_for_status()
    price = None
    # Bloc "offers" du JSON-LD du produit principal (prix + dispo)
    m = re.search(r'"offers"\s*:\s*\{[^}]*?"price"\s*:\s*"?(\d+(?:\.\d+)?)[^}]*?'
                  r'"availability"\s*:\s*"([^"]+)"', r.text)
    if m:
        vals = [m.group(2)]
        if float(m.group(1)) > 0:
            price = f"{float(m.group(1)):.2f} €"
    else:
        vals = re.findall(r'availability\\?"?\s*:\s*\\?"([^"\\]+)', r.text, re.I)
    if vals:
        status = vals[0].rsplit("/", 1)[-1].lower() in IN_STATES  # 1er = produit principal
    else:
        status = False  # page retiree / sans offre
    img = None
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r.text)
    if m:
        img = m.group(1)
    return status, price, img


CHECKERS = {"jsonld": check_jsonld, "shopify": check_shopify, "keywords": check_keywords}


# ---------- Discord ----------

def send_discord(payload):
    if not WEBHOOK:
        log("DISCORD_WEBHOOK manquant : alerte non envoyee")
        return
    for _ in range(3):
        r = requests.post(WEBHOOK, json=payload, timeout=15)
        if r.status_code == 429:  # rate limit Discord
            time.sleep(float(r.json().get("retry_after", 2)))
            continue
        if r.status_code >= 300:
            log(f"Erreur webhook {r.status_code}: {r.text[:200]}")
        return


def alert(p, price, image):
    role = p.get("role_id") or os.environ.get("DISCORD_ROLE_ID")
    embed = {
        "title": f"🚨 EN STOCK : {p['name']}",
        "url": p["url"],
        "color": 0x2ECC71,
        "fields": [
            {"name": "Boutique", "value": p.get("shop", "?"), "inline": True},
            {"name": "Prix", "value": price or "?", "inline": True},
        ],
        "footer": {"text": "RARE DROP monitor"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if image:
        embed["thumbnail"] = {"url": image}
    send_discord({
        "content": f"<@&{role}>" if role else None,
        "embeds": [embed],
        "allowed_mentions": {"roles": [str(role)] if role else []},
    })


# ---------- Boucle ----------

def run_once(products, state):
    for p in products:
        if p.get("disabled"):
            continue
        key = p["url"]
        checker = CHECKERS.get(p.get("type", "jsonld"))
        try:
            status, price, image = checker(p)
        except Exception as e:
            log(f"⚠️  {p['name']}: {e}")
            continue

        prev = state.get(key, {}).get("status")
        label = {True: "EN STOCK", False: "rupture", None: "inconnu"}[status]
        log(f"{p['name']}: {label}")

        if status is True and prev is False:
            max_price = p.get("max_price")
            value = float(price.split()[0]) if price else None
            if max_price and value and value > max_price:
                log(f"{p['name']}: en stock mais {price} > max {max_price} €, pas d'alerte")
            else:
                log(f"🚨 ALERTE {p['name']}")
                alert(p, price, image)
        if status is not None:
            state[key] = {"status": status, "checked": int(time.time())}
        time.sleep(random.uniform(1.5, 4))  # on reste poli avec les sites
    save_json(STATE_FILE, state)


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    if "--test" in sys.argv:
        send_discord({"content": "✅ Test RARE DROP monitor : le webhook fonctionne."})
        log("Message de test envoye")
        return

    if "--once" in sys.argv:
        run_once(products, state)
        return

    start = time.time()
    while True:
        run_once(products, state)
        if RUN_FOR and time.time() - start + INTERVAL > RUN_FOR:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
