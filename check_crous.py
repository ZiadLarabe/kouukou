#!/usr/bin/env python3
"""
Bot de veille logements CROUS - Béthune (62400)
-------------------------------------------------
Scrape trouverunlogement.lescrous.fr (année 2026-2027), parcourt toutes les
pages de résultats, et envoie une notification Discord UNIQUEMENT quand :
  - un logement à Béthune devient disponible (ping + détails)
  - un logement à Béthune n'est plus disponible (ping + durée de dispo)

Aucun message périodique / résumé n'est envoyé — silence total sinon.

Anti-saturation : si le site répond avec un nombre de résultats anormalement
bas (souvent le signe d'une page mal chargée / site surchargé), le script
réessaie plusieurs fois avant de faire confiance à la réponse. S'il n'arrive
toujours pas à obtenir une réponse fiable, il NE TOUCHE PAS à l'état existant
(pas de fausse alerte "logement disparu") et retentera au prochain cycle.

Usage:
    python check_crous.py            # une seule exécution
    python check_crous.py --loop 30  # boucle continue toutes les 30s

Variables d'environnement:
    DISCORD_WEBHOOK_URL   -> URL du webhook Discord (obligatoire pour notifier)
    DISCORD_USER_ID       -> ton ID Discord numérique (optionnel, pour le ping)

Fichier d'état:
    seen.json -> {"<id_logement>": <timestamp_epoch_premiere_detection>, ...}
"""

import os
import json
import re
import sys
import time
import argparse
import requests
from bs4 import BeautifulSoup

# ---- Config ----------------------------------------------------------

# On surveille uniquement l'année prochaine (2026-2027)
SEARCH_URLS = [
    ("2026-2027", "https://trouverunlogement.lescrous.fr/tools/47/search"),
]

# Mots-clés qui déclenchent un "match Béthune" (insensible à la casse)
KEYWORDS = ["BETHUNE", "BÉTHUNE", "62400"]

# En-dessous de ce total France, on considère la réponse comme suspecte
# (site saturé / page mal chargée) plutôt que comme un vrai changement.
MIN_EXPECTED_FRANCE = 10
MAX_FETCH_RETRIES = 4
RETRY_DELAY_SECONDS = 12

STATE_FILE = os.path.join(os.path.dirname(__file__), "seen.json")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}

# ---- Scraping ----------------------------------------------------------

def fetch_listings(label, base_url):
    """Récupère et parse TOUS les logements d'une recherche CROUS, en parcourant
    automatiquement toutes les pages de résultats."""
    listings = []
    page = 1
    max_pages = 30

    while page <= max_pages:
        cache_buster = {"page": page, "_": str(int(time.time() * 1000))}
        resp = requests.get(base_url, headers=HEADERS, params=cache_buster, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        page_cards = soup.select("li")
        found_on_page = 0

        for card in page_cards:
            link = card.select_one("h3 a[href*='/accommodations/']")
            if not link:
                continue

            name = link.get_text(strip=True)
            href = link.get("href", "")
            m = re.search(r"/accommodations/(\d+)", href)
            if not m:
                continue
            acc_id = m.group(1)

            text_block = card.get_text(" ", strip=True)

            price_match = re.search(r"([\d,\.]+)\s*€", text_block)
            price = price_match.group(0) if price_match else "prix non précisé"

            address_match = re.search(
                r"([\d].{0,60}?\d{5}\s+[A-ZÀ-ÜÇ' \-]+)", text_block
            )
            address = address_match.group(1).strip() if address_match else "adresse non détectée"

            surface_match = re.search(r"(de\s+[\d,\.]+\s+à\s+[\d,\.]+\s*m²|[\d,\.]+\s*m²)", text_block)
            surface = surface_match.group(1) if surface_match else "surface non précisée"

            full_url = href if href.startswith("http") else f"https://trouverunlogement.lescrous.fr{href}"

            listings.append({
                "id": f"{label}:{acc_id}",
                "name": name,
                "text": text_block,
                "price": price,
                "address": address,
                "surface": surface,
                "url": full_url,
                "year": label,
            })
            found_on_page += 1

        if found_on_page == 0:
            break

        page += 1

    return listings


def fetch_all_france():
    """Récupère tous les logements France, avec retry si la réponse semble
    suspecte (site saturé). Retourne None si aucune réponse fiable obtenue
    après plusieurs tentatives (le cycle appelant doit alors ne RIEN changer)."""
    for attempt in range(1, MAX_FETCH_RETRIES + 1):
        try:
            all_france = []
            for label, url in SEARCH_URLS:
                all_france.extend(fetch_listings(label, url))

            if len(all_france) >= MIN_EXPECTED_FRANCE:
                return all_france

            print(f"⚠️  Réponse suspecte ({len(all_france)} logements, "
                  f"tentative {attempt}/{MAX_FETCH_RETRIES}) — site probablement saturé.")

        except requests.RequestException as e:
            print(f"⚠️  Erreur réseau (tentative {attempt}/{MAX_FETCH_RETRIES}): {e}", file=sys.stderr)

        if attempt < MAX_FETCH_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    print("❌ Impossible d'obtenir une réponse fiable — état précédent conservé, "
          "on retentera au prochain cycle.", file=sys.stderr)
    return None


def filter_bethune(listings):
    return [item for item in listings if any(kw in item["text"].upper() for kw in KEYWORDS)]


# ---- État (déjà vu) : {id: timestamp epoch de première détection} --------

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            # Ancien format (liste d'IDs) -> migration douce
            now = time.time()
            return {item_id: now for item_id in data}
        return data
    return {}


def save_seen(seen_dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_dict, f, ensure_ascii=False, indent=2)


def format_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}j")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}min")
    return " ".join(parts)


# ---- Discord --------------------------------------------------------------

def _post(payload):
    if not WEBHOOK_URL:
        print(f"⚠️  DISCORD_WEBHOOK_URL non défini — message non envoyé: {payload.get('content','')}")
        return
    r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
    if r.status_code >= 300:
        print(f"Erreur envoi Discord ({r.status_code}): {r.text}", file=sys.stderr)


def notify_became_available(item):
    ping = f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""
    _post({
        "username": "CROUS Béthune Watcher",
        "content": f"{ping}🚨 Nouveau logement disponible à Béthune !",
        "embeds": [{
            "title": f"🏠 Disponible ({item['year']})",
            "description": item["name"],
            "url": item["url"],
            "color": 0x2ECC71,
            "fields": [
                {"name": "Adresse", "value": item["address"], "inline": False},
                {"name": "Prix", "value": item["price"], "inline": True},
                {"name": "Surface", "value": item["surface"], "inline": True},
                {"name": "Lien", "value": item["url"], "inline": False},
            ],
        }],
    })


def notify_became_unavailable(item, duration_seconds):
    ping = f"<@{DISCORD_USER_ID}> " if DISCORD_USER_ID else ""
    _post({
        "username": "CROUS Béthune Watcher",
        "content": f"{ping}❌ Logement à Béthune plus disponible (resté dispo {format_duration(duration_seconds)}).",
        "embeds": [{
            "title": f"Logement retiré ({item.get('year','?')})",
            "description": item.get("name", "?"),
            "color": 0xE74C3C,
            "fields": [
                {"name": "Était disponible pendant", "value": format_duration(duration_seconds), "inline": False},
                {"name": "Adresse", "value": item.get("address", "?"), "inline": False},
            ],
        }],
    })


# ---- Main -----------------------------------------------------------------

def print_details(items, title):
    print(f"\n=== {title} ({len(items)}) ===")
    if not items:
        print("  (aucun)")
    for item in items:
        print(f"- {item['name']} [{item['year']}] | {item['address']} | {item['price']} | {item['surface']} | {item['url']}")


def run_once():
    all_france = fetch_all_france()

    if all_france is None:
        # Réponse non fiable -> on ne touche à rien, pas de fausse alerte.
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Cycle ignoré (réponse non fiable).")
        return

    bethune = filter_bethune(all_france)
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Total France: {len(all_france)} | Total Béthune: {len(bethune)}")
    print_details(bethune, "Logements à BÉTHUNE actuellement")

    seen = load_seen()  # {id: first_seen_epoch}
    current_by_id = {item["id"]: item for item in bethune}

    new_ids = set(current_by_id) - set(seen)
    gone_ids = set(seen) - set(current_by_id)

    now = time.time()

    for item_id in new_ids:
        item = current_by_id[item_id]
        print(f"NOUVEAU: {item['name']} ({item_id})")
        notify_became_available(item)
        seen[item_id] = now

    for item_id in gone_ids:
        first_seen = seen.get(item_id, now)
        duration = now - first_seen
        print(f"DISPARU: {item_id} — resté dispo {format_duration(duration)}")
        # On n'a plus le détail complet (le logement n'est plus dans la page),
        # on envoie ce qu'on sait à minima.
        notify_became_unavailable({"id": item_id, "year": item_id.split(":")[0]}, duration)
        del seen[item_id]

    save_seen(seen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop", type=int, default=0,
        help="Si fourni, tourne en continu toutes les N secondes. "
             "Sans cet argument, une seule exécution."
    )
    args = parser.parse_args()

    if args.loop and args.loop > 0:
        print(f"Mode boucle continue activé — check toutes les {args.loop} secondes.")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"Erreur inattendue: {e}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        run_once()


if __name__ == "__main__":
    main()
