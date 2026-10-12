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
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(HERE, "products.json")
STATE_FILE = os.path.join(HERE, "state.json")
WATCH_FILE = os.path.join(HERE, "watch.json")

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
        if not vals:  # microdata (UltraJeux...) : <meta itemprop="availability" content="InStock">
            vals = re.findall(r'itemprop="availability"[^>]*?(?:content|href)="([^"]+)"', r.text)
            m = re.search(r'itemprop="price"[^>]*?content="(\d+(?:\.\d+)?)"', r.text)
            if m and float(m.group(1)) > 0:
                price = f"{float(m.group(1)):.2f} €"
    if vals:
        state = vals[0].rsplit("/", 1)[-1].lower()  # 1er = produit principal
        status = state in IN_STATES
        p["_avail"] = "Précommande" if state in ("preorder", "presale") else "En stock"
    else:
        status = False  # page retiree / sans offre
    img = None
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r.text)
    if m:
        img = m.group(1)
    return status, price, img


def check_woo(p):
    """Boutiques WooCommerce : API publique Store API (JSON fiable)."""
    r = session.get(p["api"], timeout=20)
    if r.status_code == 404:
        return False, None, None
    r.raise_for_status()
    d = r.json()
    pr = d.get("prices", {})
    price = None
    if pr.get("price"):
        v = int(pr["price"]) / 10 ** int(pr.get("currency_minor_unit", 2))
        price = f"{v:.2f} €" if v > 0 else None
    img = (d.get("images") or [{}])[0].get("src")
    return bool(d.get("is_in_stock") and d.get("is_purchasable")), price, img


CHECKERS = {"jsonld": check_jsonld, "shopify": check_shopify,
            "keywords": check_keywords, "woo": check_woo}


# ---------- Decouverte automatique des fiches produits ----------
# Chaque boutique de watch.json est scannee au debut de chaque run :
# toute fiche dont le titre correspond a un de ces motifs est ajoutee a la surveillance.

A30 = r"(30|anniv|c[ée]l[ée]bration)"
MATCHERS = {
    "ETB 30 ans": rf"^(?=.*(dresseur d.?[ée]lite|\betb\b))(?=.*{A30})",
    "Mini-tin 30 ans": rf"^(?=.*mini.?tin)(?=.*{A30})",
    "Bundle 30 ans": r"^(?=.*bundle)(?=.*(30 ?ans|30e|30ème|30ᵉ|anniv|c[ée]l[ée]bration))(?!.*151)",
    "UPC Mentali": r"^(?=.*(ultra.?premium|\bupc\b))(?=.*(mentali|journ[ée]e))",
    "UPC Noctali": r"^(?=.*(ultra.?premium|\bupc\b))(?=.*(noctali|soir[ée]e))",
    "Figurine Mew/Mewtwo": rf"^(?=.*figurine)(?=.*mew)(?=.*{A30})",
    "Poster 30 ans": rf"^(?=.*poster)(?=.*{A30})",
    "ETB Évolutions Prismatiques": r"^(?=.*(dresseur d.?[ée]lite|\betb\b))(?=.*prismatique)",
    "Bundle 151": r"^(?=.*bundle)(?=.*151)",
    "Display Méga-Évolution": r"^(?=.*display)(?=.*m[ée]ga.?[ée]volution)",
}
MATCHERS = {k: re.compile(v, re.I) for k, v in MATCHERS.items()}


def matches(title):
    return any(rx.search(title) for rx in MATCHERS.values())


def discover(w):
    """Retourne les fiches (dict produit) trouvees pour une boutique de watch.json."""
    found = {}
    base = w.get("base", "").rstrip("/")
    if w["type"] == "shopify":
        for q in w["queries"]:
            r = session.get(f"{base}/search/suggest.json", timeout=20, params={
                "q": q, "resources[type]": "product", "resources[limit]": 10})
            r.raise_for_status()
            for it in r.json().get("resources", {}).get("results", {}).get("products", []):
                if matches(it["title"]) and float(it.get("price") or 0) > 5:
                    url = f"{base}/products/{it['handle']}"
                    found[url] = {"name": it["title"], "type": "shopify"}
            time.sleep(random.uniform(0.5, 1.5))
    elif w["type"] == "woo":
        for q in w["queries"]:
            r = session.get(f"{base}/wp-json/wc/store/v1/products", timeout=20,
                            params={"search": q, "per_page": 50})
            r.raise_for_status()
            for it in r.json():
                name = html_unescape(it["name"])
                v = int(it["prices"]["price"] or 0) / 10 ** int(it["prices"]["currency_minor_unit"])
                if matches(name) and v > 5:
                    found[it["permalink"]] = {"name": name, "type": "woo",
                                              "api": f"{base}/wp-json/wc/store/v1/products/{it['id']}"}
            time.sleep(random.uniform(0.5, 1.5))
    elif w["type"] == "links":  # page categorie HTML -> liens produits
        rx = re.compile(r'<a[^>]+href="([^"]*' + w["link_pattern"] + r')"[^>]*>(.*?)</a>', re.S | re.I)
        # titres courts sur certaines boutiques -> filtre propre a la boutique si fourni
        own = re.compile(w["match"], re.I) if w.get("match") else None
        matches_ = (lambda t: own.search(t)) if own else matches
        for page in w["pages"]:
            r = session.get(page, timeout=20)
            r.raise_for_status()
            for href, inner in rx.findall(r.text):
                name = html_unescape(re.sub(r"<[^>]+>|\s+", " ", inner)).strip()
                if name and matches_(name):
                    url = requests.compat.urljoin(page, href)
                    found[url] = {"name": name, "type": "jsonld"}
            time.sleep(random.uniform(0.5, 1.5))
    for u, d in found.items():
        d.update({"url": u, "shop": w["shop"]})
        if w.get("role_id"):
            d["role_id"] = w["role_id"]
    return found


def html_unescape(s):
    import html
    return html.unescape(s)


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
    name = re.sub(r"^\d+\s*·\s*", "", p["name"])  # retire le numero de priorite
    price_fr = price.replace(".", ",") if price else "?"
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M:%S")
    embed = {
        "title": "🚨 RESTOCK DÉTECTÉ",
        "url": p["url"],
        "color": 0xE74C3C,
        "description": (
            f"**{name}**\n\n"
            f"🏪 Boutique : {p.get('shop', '?')}\n"
            f"💰 Prix : {price_fr}\n"
            f"📦 Disponibilité : {p.get('_avail', 'En stock')}\n"
            f"🕒 Détection : {heure}\n"
            f"🔗 [Voir le produit]({p['url']})"
        ),
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

        if status is True and (prev is False or (prev is None and p.get("_new"))):
            max_price = p.get("max_price")
            value = float(price.split()[0]) if price else None
            if max_price and value and value > max_price:
                log(f"{p['name']}: en stock mais {price} > max {max_price} €, pas d'alerte")
            else:
                log(f"🚨 ALERTE {p['name']}")
                alert(p, price, image)
        if status is not None:
            state[key] = {"status": status, "checked": int(time.time())}
            p.pop("_new", None)
        time.sleep(random.uniform(0.5, 1.5))  # on reste poli avec les sites
    save_json(STATE_FILE, state)


def run_discovery(watch, state):
    """Scanne les boutiques et renvoie la liste des fiches decouvertes (cumulees)."""
    disc = state.setdefault("_discovered", {})
    init = state.setdefault("_init", {})
    for w in watch:
        if w.get("disabled"):
            continue
        try:
            found = discover(w)
        except Exception as e:
            log(f"⚠️  Decouverte {w['shop']}: {e}")
            continue
        new = [u for u in found if u not in disc]
        for u in new:
            found[u]["_new"] = init.get(w["shop"], False)  # 1er scan = reference, pas d'alerte
            log(f"🆕 {w['shop']}: {found[u]['name']}")
        disc.update(found)
        init[w["shop"]] = True
        log(f"Decouverte {w['shop']}: {len(found)} fiche(s), {len(new)} nouvelle(s)")
    save_json(STATE_FILE, state)
    return list(disc.values())


def main():
    products = load_json(PRODUCTS_FILE, [])
    state = load_json(STATE_FILE, {})

    if "--test-alert" in sys.argv:
        alert({"name": "ETB Pokémon 30ᵉ Anniversaire — FR", "shop": "Boutique A",
               "url": products[0]["url"] if products else "https://www.play-in.com"},
              "59.99 € (exemple)", None)
        log("Alerte de test envoyee")
        return

    if "--test" in sys.argv:
        send_discord({"content": "✅ Test RARE DROP monitor : le webhook fonctionne."})
        log("Message de test envoye")
        return

    watch = load_json(WATCH_FILE, [])
    if watch:
        products = products + run_discovery(watch, state)

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
