#!/usr/bin/env python3
"""
orderbook_snapshot_15m.py — Snapshot du carnet d'ordres Polymarket BTC 15m

Capture toutes les 5 minutes (via GitHub Actions / cron-job.org) :
  - Détection dynamique du marché BTC 15m actif (pas de construction par convention)
  - time_since_open_s : secondes écoulées depuis l'ouverture du marché
  - Profondeur du carnet CLOB (token UP)
  - Slippage théorique pour 50 / 100 / 200 / 500 USDC
  - Métriques de liquidité de base

Objectif Phase 0 : mesurer la friction effective et le profil de liquidité 15m
sur 48h+ à différentes heures pour valider la viabilité du pipeline btc15m.

Écrit dans btc15m/orderbook_log_15m.json
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

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL  = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL     = "https://clob.polymarket.com/book"

ORDERBOOK_LOG  = Path(__file__).parent / "orderbook_log_15m.json"

# Durée nominale d'un marché 15m en secondes (fallback si startDate absent)
MARKET_DURATION_S = 900

# Tailles à analyser (USDC) — mêmes seuils que 5m + 500 USDC pour comparaison
SLIPPAGE_SIZES = [50, 100, 200, 500]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Détection dynamique du marché BTC 15m actif
# ─────────────────────────────────────────────────────────────────────────────

def find_active_15m_market() -> dict | None:
    """
    Détecte le marché BTC 15m actif via l'API Gamma.
    Recherche dynamique : pas de construction de slug par convention
    (les timestamps 15m ne suivent pas toujours une grille propre).

    Retourne le dict du marché le plus proche de sa résolution,
    enrichi de champs internes (_slug, _token_id_up, _token_id_down,
    _minutes_left, _time_since_open_s).
    """
    now = datetime.now(timezone.utc)

    # Stratégie 1 : recherche via l'endpoint /markets avec tag
    candidates = _search_via_markets_api(now)

    # Stratégie 2 : fallback via /events si aucun résultat
    if not candidates:
        candidates = _search_via_events_api(now)

    if not candidates:
        return None

    # Garder uniquement les marchés non expirés
    active = [(mins, m) for mins, m in candidates if mins > 0]
    if active:
        active.sort(key=lambda x: x[0])   # le plus proche de la clôture
        return active[0][1]

    # Fallback : marché le plus récemment expiré (résolution en cours)
    candidates.sort(key=lambda x: abs(x[0]))
    return candidates[0][1]


def _search_via_markets_api(now: datetime) -> list:
    """Recherche les marchés 15m via l'endpoint /markets."""
    candidates = []
    try:
        r = requests.get(
            GAMMA_MARKETS_URL,
            params={"active": "true", "closed": "false", "limit": 50},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        markets = r.json()
        if isinstance(markets, dict):
            markets = markets.get("markets", markets.get("data", []))
    except Exception as e:
        print(f"  [WARN] Gamma markets API : {e}")
        return []

    for market in markets:
        slug = market.get("slug", "") or market.get("conditionId", "")
        # Filtre : contient "btc" et "15m" dans le slug
        if "btc" not in slug.lower() or "15m" not in slug.lower():
            continue

        enriched = _enrich_market(market, slug, now)
        if enriched is not None:
            mins_left, m = enriched
            candidates.append((mins_left, m))

    return candidates


def _search_via_events_api(now: datetime) -> list:
    """
    Fallback : recherche via l'endpoint /events avec slug partiel.
    Tente les timestamps 15m autour de l'heure actuelle.
    """
    candidates = []
    ts   = int(now.timestamp())
    base = (ts // 900) * 900  # grille de 15 min

    for offset in range(-1, 4):
        candidate_ts = base + offset * 900
        slug = f"btc-updown-15m-{candidate_ts}"
        try:
            r = requests.get(
                GAMMA_EVENTS_URL,
                params={"slug": slug},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            market = events[0].get("markets", [{}])[0]
            if not market:
                continue

            enriched = _enrich_market(market, slug, now)
            if enriched is not None:
                candidates.append(enriched)
        except Exception:
            continue

    return candidates


def _enrich_market(market: dict, slug: str, now: datetime) -> tuple | None:
    """
    Enrichit un dict de marché avec les champs internes.
    Retourne (minutes_left, market_enriched) ou None si invalide.
    """
    # endDate
    end_str = market.get("endDate", "") or market.get("end_date_iso", "")
    if not end_str:
        return None
    if not end_str.endswith("Z"):
        end_str += "Z"
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    minutes_left = (end_dt - now).total_seconds() / 60
    if not (-2 < minutes_left < 30):  # hors fenêtre utile
        return None

    # time_since_open_s : calculé depuis endDate uniquement.
    # Le champ startDate de l'API Gamma représente la création de l'event series,
    # pas l'ouverture de la fenêtre 15m actuelle — on ne l'utilise pas.
    time_since_open_s = max(0, MARKET_DURATION_S - int(minutes_left * 60))

    # clobTokenIds
    token_ids_raw = market.get("clobTokenIds", "[]")
    if isinstance(token_ids_raw, str):
        try:
            token_ids = json.loads(token_ids_raw)
        except json.JSONDecodeError:
            token_ids = []
    else:
        token_ids = token_ids_raw or []

    if not token_ids:
        return None

    market["_slug"]              = slug
    market["_minutes_left"]      = round(minutes_left, 1)
    market["_time_since_open_s"] = time_since_open_s
    market["_token_id_up"]       = token_ids[0]
    market["_token_id_down"]     = token_ids[1] if len(token_ids) > 1 else None

    return (minutes_left, market)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Carnet d'ordres CLOB
# ─────────────────────────────────────────────────────────────────────────────

def fetch_orderbook(token_id: str) -> dict | None:
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
    Slippage pour un achat de `size_usdc` USDC sur le token UP.
    asks : liste {"price": float, "size": float} triée prix croissant.
    Retourne slippage = prix_moyen_exécution - best_ask, ou None si liquidité insuffisante.
    """
    if not asks:
        return None

    remaining  = size_usdc
    cost       = 0.0
    shares_got = 0.0

    for level in asks:
        price           = float(level["price"])
        size            = float(level["size"])
        usdc_available  = size * price

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
        return None  # liquidité insuffisante

    avg_price = cost / shares_got if shares_got > 0 else None
    if avg_price is None:
        return None

    return round(avg_price - float(asks[0]["price"]), 6)


def compute_all_slippages(asks: list) -> dict:
    return {
        str(size): (round(s, 6) if (s := compute_slippage(asks, size)) is not None else None)
        for size in SLIPPAGE_SIZES
    }


def total_depth_usdc(asks: list) -> float:
    return round(sum(float(l["price"]) * float(l["size"]) for l in asks), 2)


def half_spread(best_bid: float, best_ask: float) -> float | None:
    """Friction estimée = spread / 2."""
    if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
        return round((best_ask - best_bid) / 2, 6)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Log
# ─────────────────────────────────────────────────────────────────────────────

def load_log() -> list:
    if ORDERBOOK_LOG.exists():
        with open(ORDERBOOK_LOG, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(entries: list):
    with open(ORDERBOOK_LOG, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Analyse Phase 0 (commande: python btc15m/orderbook_snapshot_15m.py report)
# ─────────────────────────────────────────────────────────────────────────────

def print_phase0_report():
    """
    Analyse le log accumulé et affiche les métriques Phase 0 :
    - Friction effective (spread/2) par tranche horaire
    - Slippage médian et moyen par taille
    - Profil de liquidité en fonction de time_since_open_s
    - Verdict go/no-go Phase 1
    """
    if not ORDERBOOK_LOG.exists():
        print("Aucun log trouvé. Lance d'abord le snapshot pendant 48h.")
        return

    log = load_log()
    if len(log) < 10:
        print(f"Données insuffisantes ({len(log)} entrées). Attendre 48h de collecte.")
        return

    try:
        import statistics
    except ImportError:
        pass

    print(f"\n{'='*60}")
    print(f"RAPPORT PHASE 0 — BTC 15m Orderbook ({len(log)} snapshots)")
    print(f"{'='*60}")

    # Friction (spread/2)
    frictions = [e["friction_half_spread"] for e in log if e.get("friction_half_spread")]
    if frictions:
        print(f"\n--- Friction (spread/2) ---")
        print(f"  Médiane : {sorted(frictions)[len(frictions)//2]*100:.3f}%")
        print(f"  Moyenne : {sum(frictions)/len(frictions)*100:.3f}%")
        print(f"  Max     : {max(frictions)*100:.3f}%")
        print(f"  Min     : {min(frictions)*100:.3f}%")

    # Slippage par taille
    print(f"\n--- Slippage par taille de mise ---")
    print(f"{'Taille':>8}  {'Médiane':>8}  {'Moyenne':>8}  {'Coût total (+ friction moy)':>28}")
    friction_med = sorted(frictions)[len(frictions)//2] if frictions else 0.005
    for size in SLIPPAGE_SIZES:
        vals = [
            e["slippage_usdc"][str(size)]
            for e in log
            if e.get("slippage_usdc", {}).get(str(size)) is not None
        ]
        if not vals:
            print(f"{size:>8} USDC  n/a")
            continue
        vals_sorted = sorted(vals)
        med = vals_sorted[len(vals)//2]
        avg = sum(vals)/len(vals)
        total = avg + friction_med
        print(f"{size:>8} USDC  {med*100:>7.2f}%  {avg*100:>7.2f}%  ~{total*100:.2f}%")

    # Profil temporel : liquidité vs time_since_open
    print(f"\n--- Profil de liquidité vs time_since_open ---")
    buckets = {"0-3min": [], "3-7min": [], "7-12min": [], "12-15min": []}
    for e in log:
        t = e.get("time_since_open_s", -1)
        d = e.get("depth_ask_usdc", 0)
        if t < 0:
            continue
        if t < 180:
            buckets["0-3min"].append(d)
        elif t < 420:
            buckets["3-7min"].append(d)
        elif t < 720:
            buckets["7-12min"].append(d)
        else:
            buckets["12-15min"].append(d)

    for label, vals in buckets.items():
        if vals:
            med = sorted(vals)[len(vals)//2]
            print(f"  {label:>10}  profondeur médiane ask : {med:>10.0f} USDC  (n={len(vals)})")

    # Verdict
    print(f"\n--- Verdict Phase 0 → Phase 1 ---")
    if frictions:
        friction_med_val = sorted(frictions)[len(frictions)//2]
        threshold = 0.03  # edge brut attendu ~4-8%, seuil = edge_min/2 ≈ 2-3%
        if friction_med_val < threshold:
            print(f"  GO — friction médiane {friction_med_val*100:.2f}% < seuil {threshold*100:.0f}%")
            print(f"       Edge minimum viable : {friction_med_val*2*100:.2f}% brut")
        else:
            print(f"  NO-GO — friction médiane {friction_med_val*100:.2f}% >= seuil {threshold*100:.0f}%")
            print(f"           Edge minimum viable : {friction_med_val*2*100:.2f}% — modèle très sélectif requis")

    print(f"\n{'='*60}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 6. Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        print_phase0_report()
        return

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[{now_str}] Orderbook snapshot BTC 15m")

    # 1. Trouver le marché actif
    market = find_active_15m_market()
    if not market:
        print("  Aucun marché BTC 15m actif trouvé — snapshot ignoré.")
        sys.exit(0)

    slug             = market["_slug"]
    token_id_up      = market["_token_id_up"]
    token_id_down    = market["_token_id_down"]
    mins_left        = market["_minutes_left"]
    time_since_open  = market["_time_since_open_s"]
    best_bid         = float(market.get("bestBid",  0) or market.get("best_bid",  0) or 0)
    best_ask         = float(market.get("bestAsk",  0) or market.get("best_ask",  0) or 0)
    spread           = float(market.get("spread",   0) or 0)

    print(f"  Marché   : {slug}")
    print(f"  Ouvert depuis : {time_since_open}s  |  Restant : {mins_left} min")
    print(f"  bid={best_bid:.4f}  ask={best_ask:.4f}  spread={spread:.4f}")
    print(f"  token_up={token_id_up[:12]}...")

    # 2. Carnet CLOB (token UP)
    book = fetch_orderbook(token_id_up)
    if not book:
        print("  Impossible de récupérer le carnet CLOB — snapshot ignoré.")
        sys.exit(0)

    asks_raw = book.get("asks", [])
    asks     = sorted(asks_raw, key=lambda x: float(x["price"]))

    if not asks:
        print("  Carnet vide (aucun ask) — snapshot ignoré.")
        sys.exit(0)

    # Récupère best_bid/ask depuis le carnet si absent dans le market
    bids_raw = book.get("bids", [])
    if best_ask == 0 and asks:
        best_ask = float(asks[0]["price"])
    if best_bid == 0 and bids_raw:
        bids_sorted = sorted(bids_raw, key=lambda x: float(x["price"]), reverse=True)
        best_bid = float(bids_sorted[0]["price"])

    # 3. Métriques
    slippages       = compute_all_slippages(asks)
    depth_usdc      = total_depth_usdc(asks)
    n_levels        = len(asks)
    friction_hs     = half_spread(best_bid, best_ask)

    print(f"  Profondeur ask : {depth_usdc} USDC  ({n_levels} niveaux)")
    print(f"  Friction (spread/2) : {f'{friction_hs*100:.3f}%' if friction_hs else 'n/a'}")
    print(f"  Slippage : {slippages}")

    # 4. Construire l'entrée
    entry = {
        "ts":                  now_str,
        "slug":                slug,
        "token_id_up":         token_id_up,
        "time_since_open_s":   time_since_open,
        "mins_left":           mins_left,
        "best_bid":            round(best_bid, 6),
        "best_ask":            round(best_ask, 6),
        "spread":              round(spread, 6) if spread else round(best_ask - best_bid, 6),
        "friction_half_spread": friction_hs,
        "depth_ask_usdc":      depth_usdc,
        "n_ask_levels":        n_levels,
        "slippage_usdc":       slippages,
    }

    # 5. Sauvegarder
    log = load_log()
    log.append(entry)
    save_log(log)
    print(f"  OK — {len(log)} entrées au total dans orderbook_log_15m.json")


if __name__ == "__main__":
    main()
