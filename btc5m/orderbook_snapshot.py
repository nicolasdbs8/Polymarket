#!/usr/bin/env python3
"""
orderbook_snapshot.py — Snapshot du carnet d'ordres Polymarket BTC 5m

Capture toutes les 10 minutes :
  - La profondeur du carnet CLOB (token UP)
  - Le slippage théorique pour 10 / 50 / 100 / 200 / 500 USDC
  - Les métriques de liquidité de base

Écrit dans btc5m/orderbook_log.json — indépendant de signal_log.json.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("[ERREUR] requests manquant. Lance : pip install requests")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL    = "https://clob.polymarket.com/book"

ORDERBOOK_LOG = Path(__file__).parent / "orderbook_log.json"

SLIPPAGE_SIZES = [10, 50, 100, 200, 500]  # USDC


# ─────────────────────────────────────────────────────────────────────────────
# 1. Trouver le marché BTC 5m actif (même logique que btc5m_signal.py)
# ─────────────────────────────────────────────────────────────────────────────

def current_5m_slugs() -> list:
    now  = datetime.now(timezone.utc)
    ts   = int(now.timestamp())
    base = (ts // 300) * 300
    return [base + i * 300 for i in range(-1, 5)]


def find_active_market() -> dict | None:
    """
    Retourne le dict du marché BTC 5m actif le plus proche de sa résolution.
    Inclut clobTokenIds parsé.
    """
    now        = datetime.now(timezone.utc)
    candidates = []

    for ts in current_5m_slugs():
        slug = f"btc-updown-5m-{ts}"
        url  = f"{GAMMA_EVENTS_URL}?slug={slug}"
        try:
            r = requests.get(url, timeout=8)
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            market = events[0].get("markets", [{}])[0]
            if not market:
                continue

            end_str = market.get("endDate", "")
            if not end_str:
                continue
            if not end_str.endswith("Z"):
                end_str += "Z"
            end_dt       = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            minutes_left = (end_dt - now).total_seconds() / 60

            if not (-1 < minutes_left < 25):
                continue

            # Parse clobTokenIds
            token_ids_raw = market.get("clobTokenIds", "[]")
            if isinstance(token_ids_raw, str):
                token_ids = json.loads(token_ids_raw)
            else:
                token_ids = token_ids_raw

            if not token_ids:
                continue

            market["_slug"]         = slug
            market["_minutes_left"] = round(minutes_left, 1)
            market["_token_id_up"]  = token_ids[0]
            market["_token_id_down"]= token_ids[1] if len(token_ids) > 1 else None
            candidates.append((minutes_left, market))

        except Exception:
            continue

    if not candidates:
        return None

    active = [(m, mk) for m, mk in candidates if m > 0]
    if active:
        active.sort(key=lambda x: x[0])
        return active[0][1]

    candidates.sort(key=lambda x: abs(x[0]))
    return candidates[0][1]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Récupérer le carnet d'ordres CLOB
# ─────────────────────────────────────────────────────────────────────────────

def fetch_orderbook(token_id: str) -> dict | None:
    """
    Appelle GET /book?token_id={token_id} sur le CLOB Polymarket.
    Retourne le dict brut avec bids/asks.
    """
    try:
        r = requests.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=8)
        if r.status_code != 200:
            print(f"[ERREUR] CLOB API status {r.status_code}")
            return None
        return r.json()
    except Exception as e:
        print(f"[ERREUR] CLOB API : {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Calcul du slippage théorique
# ─────────────────────────────────────────────────────────────────────────────

def compute_slippage(asks: list, size_usdc: float) -> float | None:
    """
    Calcule le slippage pour un achat de `size_usdc` USDC sur le token UP.
    asks : liste de {"price": float, "size": float} triée par prix croissant.
    Retourne le slippage = prix_moyen_exécution - best_ask, ou None si
    la liquidité est insuffisante.
    """
    if not asks:
        return None

    remaining   = size_usdc
    cost        = 0.0
    shares_got  = 0.0

    for level in asks:
        price = float(level["price"])
        size  = float(level["size"])   # en shares (tokens)

        # USDC disponibles à ce niveau
        usdc_available = size * price

        if usdc_available >= remaining:
            shares_got += remaining / price
            cost       += remaining
            remaining   = 0
            break
        else:
            shares_got += size
            cost       += usdc_available
            remaining  -= usdc_available

    if remaining > 0:
        # Liquidité insuffisante pour remplir l'ordre
        return None

    avg_price = cost / shares_got if shares_got > 0 else None
    if avg_price is None:
        return None

    best_ask = float(asks[0]["price"])
    return round(avg_price - best_ask, 6)


def compute_all_slippages(asks: list) -> dict:
    result = {}
    for size in SLIPPAGE_SIZES:
        s = compute_slippage(asks, size)
        result[str(size)] = round(s, 6) if s is not None else None
    return result


def total_depth_usdc(asks: list) -> float:
    """Liquidité totale disponible côté ask (en USDC)."""
    return round(sum(float(l["price"]) * float(l["size"]) for l in asks), 2)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Log
# ─────────────────────────────────────────────────────────────────────────────

def load_log() -> list:
    if ORDERBOOK_LOG.exists():
        with open(ORDERBOOK_LOG, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(log: list):
    with open(ORDERBOOK_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[{now_str}] Orderbook snapshot BTC 5m")

    # 1. Trouver le marché actif
    market = find_active_market()
    if not market:
        print("  Aucun marché BTC 5m actif trouvé — snapshot ignoré.")
        sys.exit(0)

    slug        = market["_slug"]
    token_id_up = market["_token_id_up"]
    mins_left   = market["_minutes_left"]
    best_bid    = float(market.get("bestBid",  0))
    best_ask    = float(market.get("bestAsk",  0))
    spread      = float(market.get("spread",   0))

    print(f"  Marché : {slug}  ({mins_left} min restantes)")
    print(f"  bid={best_bid}  ask={best_ask}  spread={spread}")
    print(f"  token_up={token_id_up[:12]}...")

    # 2. Récupérer le carnet CLOB
    book = fetch_orderbook(token_id_up)
    if not book:
        print("  Impossible de récupérer le carnet CLOB — snapshot ignoré.")
        sys.exit(0)

    # asks = offres de vente du token UP (ce qu'on achète)
    asks_raw = book.get("asks", [])
    # Trier par prix croissant (meilleur ask en premier)
    asks = sorted(asks_raw, key=lambda x: float(x["price"]))

    if not asks:
        print("  Carnet vide (aucun ask) — snapshot ignoré.")
        sys.exit(0)

    # 3. Calculer les métriques
    slippages   = compute_all_slippages(asks)
    depth_usdc  = total_depth_usdc(asks)
    n_levels    = len(asks)

    print(f"  Profondeur ask : {depth_usdc} USDC  ({n_levels} niveaux)")
    print(f"  Slippage : {slippages}")

    # 4. Construire l'entrée
    entry = {
        "ts":           now_str,
        "slug":         slug,
        "token_id_up":  token_id_up,
        "mins_left":    mins_left,
        "best_bid":     best_bid,
        "best_ask":     best_ask,
        "spread":       spread,
        "depth_ask_usdc": depth_usdc,
        "n_ask_levels": n_levels,
        "slippage_usdc": slippages,
    }

    # 5. Sauvegarder
    log = load_log()
    log.append(entry)
    save_log(log)
    print(f"  ✓ Snapshot enregistré ({len(log)} entrées au total)")


if __name__ == "__main__":
    main()
