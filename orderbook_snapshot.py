#!/usr/bin/env python3
"""
orderbook_snapshot.py — Snapshot générique du carnet d'ordres Polymarket

Détecte dynamiquement le marché actif pour un asset/timeframe donné,
capture la profondeur CLOB et calcule les métriques Phase 0.

Usage:
  python orderbook_snapshot.py --asset btc --timeframe 5m
  python orderbook_snapshot.py --asset eth --timeframe 15m
  python orderbook_snapshot.py --asset btc --timeframe daily
  python orderbook_snapshot.py --asset btc --timeframe 5m report

Assets supportés  : btc, eth, sol, xrp, doge
Timeframes        : 5m, 15m, daily
Log de sortie     : {asset}{timeframe}/orderbook_log.json
"""

import argparse
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
# Config
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL  = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL     = "https://clob.polymarket.com/book"

SLIPPAGE_SIZES = [50, 100, 200, 500]  # USDC

# Mots-clés de recherche par asset dans les slugs Polymarket.
# Les marchés daily utilisent le nom complet (bitcoin, ethereum...).
ASSET_KEYWORDS = {
    "btc":  ["btc", "bitcoin"],
    "eth":  ["eth", "ethereum"],
    "sol":  ["sol", "solana"],
    "xrp":  ["xrp"],
    "doge": ["doge", "dogecoin"],
}

# Mots-clés de recherche par timeframe dans les slugs.
# Les marchés daily utilisent un pattern date : "up-or-down-on-april-10"
# On utilise "up-or-down-on" pour les distinguer des marchés intraday.
TIMEFRAME_KEYWORDS = {
    "5m":    ["5m"],
    "15m":   ["15m"],
    "daily": ["up-or-down-on", "daily"],
}

# Nom utilisé dans le slug des marchés daily (pattern : {name}-up-or-down-on-{month}-{day})
DAILY_SLUG_NAMES = {
    "btc":  "bitcoin",
    "eth":  "ethereum",
    "sol":  "solana",
    "xrp":  "xrp",
    "doge": "dogecoin",
}

MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

TIMEFRAME_CONFIG = {
    "5m": {
        "duration_s":       300,
        "grid_s":           300,
        "window_min":       25,
        "fallback_offsets": range(-1, 5),
        "report_buckets": [
            ("0-2min", 0,   120),
            ("2-4min", 120, 240),
            ("4-5min", 240, 300),
        ],
    },
    "15m": {
        "duration_s":       900,
        "grid_s":           900,
        "window_min":       30,
        "fallback_offsets": range(-1, 4),
        "report_buckets": [
            ("0-3min",   0,   180),
            ("3-7min",   180, 420),
            ("7-12min",  420, 720),
            ("12-15min", 720, 900),
        ],
    },
    "daily": {
        "duration_s":       86400,
        "grid_s":           86400,
        "window_min":       1500,  # 25h de fenêtre de recherche
        "fallback_offsets": range(0, 0),  # pas de fallback events API pour daily (slug non-standard)
        "report_buckets": [
            ("0-2h",   0,     7200),
            ("2-8h",   7200,  28800),
            ("8-16h",  28800, 57600),
            ("16-24h", 57600, 86400),
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Détection dynamique du marché actif
# ─────────────────────────────────────────────────────────────────────────────

def find_active_market(asset: str, timeframe: str) -> dict | None:
    """
    Détecte le marché actif pour l'asset/timeframe donné.
    Stratégie 1 : /markets (liste active) → filtrage par keywords
    Stratégie 2 : /events (fallback avec slug construit)

    Retourne le marché le plus proche de sa résolution,
    enrichi des champs internes _slug, _token_id_up, _minutes_left, etc.
    """
    cfg = TIMEFRAME_CONFIG[timeframe]
    now = datetime.now(timezone.utc)

    candidates = _search_via_markets_api(asset, timeframe, cfg, now)
    if not candidates:
        candidates = _search_via_events_api(asset, timeframe, cfg, now)

    if not candidates:
        return None

    active = [(mins, m) for mins, m in candidates if mins > 0]
    if active:
        active.sort(key=lambda x: x[0])
        return active[0][1]

    candidates.sort(key=lambda x: abs(x[0]))
    return candidates[0][1]


def _search_via_markets_api(asset: str, timeframe: str, cfg: dict, now: datetime) -> list:
    """Recherche via /markets en filtrant par asset et timeframe dans le slug."""
    candidates = []
    try:
        r = requests.get(
            GAMMA_MARKETS_URL,
            params={"active": "true", "closed": "false", "limit": 100},
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

    asset_kws     = ASSET_KEYWORDS.get(asset, [asset])
    timeframe_kws = TIMEFRAME_KEYWORDS.get(timeframe, [timeframe])

    for market in markets:
        slug = (market.get("slug", "") or market.get("conditionId", "")).lower()
        if not any(kw in slug for kw in asset_kws):
            continue
        if not any(kw in slug for kw in timeframe_kws):
            continue
        enriched = _enrich_market(market, slug, cfg, now)
        if enriched is not None:
            candidates.append(enriched)

    return candidates


def _search_via_events_api(asset: str, timeframe: str, cfg: dict, now: datetime) -> list:
    """
    Fallback via /events avec slug construit.
    - intraday (5m, 15m) : pattern {asset}-updown-{timeframe}-{timestamp}
    - daily              : pattern {full_name}-up-or-down-on-{month}-{day}
                           testé sur J-1, J, J+1, J+2 (fenêtre de transition)
    """
    if timeframe == "daily":
        return _search_daily_via_events(asset, cfg, now)

    candidates = []
    ts   = int(now.timestamp())
    base = (ts // cfg["grid_s"]) * cfg["grid_s"]

    for offset in cfg["fallback_offsets"]:
        candidate_ts = base + offset * cfg["grid_s"]
        slug = f"{asset}-updown-{timeframe}-{candidate_ts}"
        try:
            r = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=8)
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            market = events[0].get("markets", [{}])[0]
            if not market:
                continue
            enriched = _enrich_market(market, slug, cfg, now)
            if enriched is not None:
                candidates.append(enriched)
        except Exception:
            continue

    return candidates


def _search_daily_via_events(asset: str, cfg: dict, now: datetime) -> list:
    """
    Recherche les marchés daily via /events en construisant les slugs par date.
    Pattern Polymarket : {full_name}-up-or-down-on-{month}-{day}
    Ex : bitcoin-up-or-down-on-april-10

    Teste J-1 à J+2 pour couvrir la fenêtre de transition autour de midi ET.
    """
    from datetime import timedelta

    name       = DAILY_SLUG_NAMES.get(asset, asset)
    candidates = []

    for offset in range(-1, 3):
        candidate_date = (now + timedelta(days=offset)).date()
        month = MONTH_NAMES[candidate_date.month - 1]
        day   = candidate_date.day
        slug  = f"{name}-up-or-down-on-{month}-{day}"
        try:
            r = requests.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=8)
            if r.status_code != 200:
                continue
            events = r.json()
            if not events:
                continue
            market = events[0].get("markets", [{}])[0]
            if not market:
                continue
            enriched = _enrich_market(market, slug, cfg, now)
            if enriched is not None:
                candidates.append(enriched)
        except Exception:
            continue

    return candidates


def _enrich_market(market: dict, slug: str, cfg: dict, now: datetime) -> tuple | None:
    """
    Enrichit un dict marché avec les champs internes.
    Retourne (minutes_left, market_enriched) ou None si hors fenêtre ou invalide.
    """
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
    window       = cfg["window_min"]

    # Garde les marchés actifs ou très récemment expirés (résolution en cours)
    if not (-(window * 0.02) < minutes_left < window):
        return None

    duration_s        = cfg["duration_s"]
    time_since_open_s = max(0, duration_s - int(minutes_left * 60))

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
# 3. Calcul du slippage
# ─────────────────────────────────────────────────────────────────────────────

def compute_slippage(asks: list, size_usdc: float) -> float | None:
    """
    Slippage pour un achat de size_usdc USDC sur le token UP.
    Retourne avg_price - best_ask, ou None si liquidité insuffisante.
    """
    if not asks:
        return None
    remaining  = size_usdc
    cost       = 0.0
    shares_got = 0.0
    for level in asks:
        price          = float(level["price"])
        size           = float(level["size"])
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
        return None
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
    if best_bid > 0 and best_ask > 0 and best_ask > best_bid:
        return round((best_ask - best_bid) / 2, 6)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 4. Log
# ─────────────────────────────────────────────────────────────────────────────

def get_log_path(asset: str, timeframe: str) -> Path:
    return Path(__file__).parent / f"{asset}{timeframe}" / "orderbook_log.json"


def load_log(log_path: Path) -> list:
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_log(log_path: Path, entries: list):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Rapport Phase 0
# ─────────────────────────────────────────────────────────────────────────────

def print_phase0_report(asset: str, timeframe: str):
    """
    Analyse le log accumulé et affiche les métriques Phase 0 :
    - Friction effective (spread/2)
    - Slippage médian/moyen par taille
    - Profil de liquidité par fenêtre temporelle (adapté au timeframe)
    - Verdict go/no-go Phase 1
    """
    log_path = get_log_path(asset, timeframe)
    if not log_path.exists():
        print(f"Aucun log trouvé ({log_path}). Lance d'abord le snapshot pendant 48h.")
        return

    log = load_log(log_path)
    if len(log) < 10:
        print(f"Données insuffisantes ({len(log)} entrées). Attendre 48h de collecte.")
        return

    label = f"{asset.upper()} {timeframe}"
    print(f"\n{'='*60}")
    print(f"RAPPORT PHASE 0 — {label} Orderbook ({len(log)} snapshots)")
    print(f"{'='*60}")

    # Friction
    frictions = [e["friction_half_spread"] for e in log if e.get("friction_half_spread")]
    if frictions:
        print(f"\n--- Friction (spread/2) ---")
        print(f"  Médiane : {sorted(frictions)[len(frictions)//2]*100:.3f}%")
        print(f"  Moyenne : {sum(frictions)/len(frictions)*100:.3f}%")
        print(f"  Max     : {max(frictions)*100:.3f}%")
        print(f"  Min     : {min(frictions)*100:.3f}%")

    # Slippage par taille
    print(f"\n--- Slippage par taille de mise ---")
    print(f"{'Taille':>8}  {'Médiane':>8}  {'Moyenne':>8}  {'Coût total (frais inclus)':>26}")
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
        med   = vals_sorted[len(vals)//2]
        avg   = sum(vals) / len(vals)
        total = avg + friction_med
        print(f"{size:>8} USDC  {med*100:>7.2f}%  {avg*100:>7.2f}%  ~{total*100:.2f}%")

    # Profil temporel
    cfg     = TIMEFRAME_CONFIG[timeframe]
    buckets = {lbl: [] for lbl, _, _ in cfg["report_buckets"]}
    for e in log:
        t = e.get("time_since_open_s", -1)
        d = e.get("depth_ask_usdc", 0)
        if t < 0:
            continue
        for lbl, start, end in cfg["report_buckets"]:
            if start <= t < end:
                buckets[lbl].append(d)
                break

    print(f"\n--- Profil de liquidité vs time_since_open ---")
    for lbl, vals in buckets.items():
        if vals:
            med = sorted(vals)[len(vals)//2]
            print(f"  {lbl:>10}  profondeur médiane ask : {med:>12.0f} USDC  (n={len(vals)})")
        else:
            print(f"  {lbl:>10}  —  (n=0)")

    # Verdict
    print(f"\n--- Verdict Phase 0 → Phase 1 ---")
    if frictions:
        friction_med_val = sorted(frictions)[len(frictions)//2]
        threshold = 0.03
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

def discover_slugs(asset: str):
    """
    Liste tous les marchés actifs correspondant à l'asset (tous keywords confondus).
    Utile pour identifier les patterns de nommage réels sur Polymarket.

    Usage : python orderbook_snapshot.py --asset btc discover
    """
    try:
        r = requests.get(
            GAMMA_MARKETS_URL,
            params={"active": "true", "closed": "false", "limit": 100},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[ERREUR] API status {r.status_code}")
            return
        markets = r.json()
        if isinstance(markets, dict):
            markets = markets.get("markets", markets.get("data", []))
    except Exception as e:
        print(f"[ERREUR] {e}")
        return

    asset_kws = ASSET_KEYWORDS.get(asset, [asset])
    matches = [
        m.get("slug", "") or m.get("conditionId", "")
        for m in markets
        if any(kw in (m.get("slug", "") or "").lower() for kw in asset_kws)
    ]

    print(f"\nMarchés actifs pour '{asset}' (keywords: {asset_kws}) — {len(matches)} trouvés :")
    for slug in sorted(matches):
        print(f"  {slug}")


def parse_args():
    parser = argparse.ArgumentParser(description="Polymarket orderbook snapshot générique")
    parser.add_argument("--asset",     required=True, choices=["btc", "eth", "sol", "xrp", "doge"],
                        help="Asset cible")
    parser.add_argument("--timeframe", required=False, choices=["5m", "15m", "daily"],
                        help="Durée du marché (non requis pour 'discover')")
    parser.add_argument("action", nargs="?", default="snapshot",
                        choices=["snapshot", "report", "discover"],
                        help="'snapshot' (défaut), 'report' ou 'discover'")
    return parser.parse_args()


def main():
    args      = parse_args()
    asset     = args.asset
    timeframe = args.timeframe

    if args.action == "discover":
        discover_slugs(asset)
        return

    if not timeframe:
        print("[ERREUR] --timeframe requis pour snapshot et report.")
        sys.exit(1)

    if args.action == "report":
        print_phase0_report(asset, timeframe)
        return

    log_path = get_log_path(asset, timeframe)
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[{now_str}] Orderbook snapshot {asset.upper()} {timeframe}")

    # 1. Trouver le marché actif
    market = find_active_market(asset, timeframe)
    if not market:
        print(f"  Aucun marché {asset.upper()} {timeframe} actif trouvé — snapshot ignoré.")
        sys.exit(0)

    slug            = market["_slug"]
    token_id_up     = market["_token_id_up"]
    mins_left       = market["_minutes_left"]
    time_since_open = market["_time_since_open_s"]
    best_bid        = float(market.get("bestBid", 0) or market.get("best_bid", 0) or 0)
    best_ask        = float(market.get("bestAsk", 0) or market.get("best_ask", 0) or 0)
    spread          = float(market.get("spread",  0) or 0)

    print(f"  Marché        : {slug}")
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

    # Récupère best_bid/ask depuis le carnet si absent dans market
    bids_raw = book.get("bids", [])
    if best_ask == 0 and asks:
        best_ask = float(asks[0]["price"])
    if best_bid == 0 and bids_raw:
        bids_sorted = sorted(bids_raw, key=lambda x: float(x["price"]), reverse=True)
        best_bid    = float(bids_sorted[0]["price"])

    # 3. Métriques
    slippages   = compute_all_slippages(asks)
    depth_usdc  = total_depth_usdc(asks)
    n_levels    = len(asks)
    friction_hs = half_spread(best_bid, best_ask)

    print(f"  Profondeur ask      : {depth_usdc} USDC  ({n_levels} niveaux)")
    print(f"  Friction (spread/2) : {f'{friction_hs*100:.3f}%' if friction_hs else 'n/a'}")
    print(f"  Slippage            : {slippages}")

    # 4. Construire l'entrée
    entry = {
        "ts":                   now_str,
        "slug":                 slug,
        "token_id_up":          token_id_up,
        "time_since_open_s":    time_since_open,
        "mins_left":            mins_left,
        "best_bid":             round(best_bid, 6),
        "best_ask":             round(best_ask, 6),
        "spread":               round(spread, 6) if spread else round(best_ask - best_bid, 6),
        "friction_half_spread": friction_hs,
        "depth_ask_usdc":       depth_usdc,
        "n_ask_levels":         n_levels,
        "slippage_usdc":        slippages,
    }

    # 5. Sauvegarder
    log = load_log(log_path)
    log.append(entry)
    save_log(log_path, log)
    print(f"  OK — {len(log)} entrées dans {log_path}")


if __name__ == "__main__":
    main()
