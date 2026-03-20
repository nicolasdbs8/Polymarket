#!/usr/bin/env python3
"""
polymarket_scan.py — Scanne Polymarket pour détecter les marchés
                     qui présentent un intérêt analytique.

Filtre automatiquement selon des critères objectifs et produit
un rapport classé par score d'intérêt.

Usage :
  python polymarket_scan.py                        ← scan large, tous marchés
  python polymarket_scan.py --category geopolitics ← filtré par catégorie
  python polymarket_scan.py --top 20               ← affiche les 20 meilleurs
  python polymarket_scan.py --min-volume 10000     ← volume minimum en $
  python polymarket_scan.py --min-days 7 --max-days 180
  python polymarket_scan.py --export scan_results.json

Critères d'intérêt (tous configurables) :
  - Prix YES entre 15% et 75%    → zone où le moteur est fiable
  - Volume > 5000 $              → liquidité suffisante
  - Résolution dans 7 à 365 j   → horizon analytiquement utile
  - Marché binaire               → compatible moteur V1
  - Non déjà dans journal.json   → pas encore analysé
"""

import json
import sys
import argparse
import urllib.request
import urllib.parse
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
DEFAULT_PAGE_SIZE = 100

CATEGORY_KEYWORDS = {
    "electoral_politics": [
        "election", "vote", "president", "senate", "congress", "mayor",
        "parliament", "ballot", "primary", "runoff", "polling", "elected",
        "chancellor", "prime minister", "referendum", "gubernatorial", "nomination"
    ],
    "geopolitics": [
        "war", "ceasefire", "invasion", "military", "troops", "missile",
        "nuclear", "sanction", "nato", "conflict", "attack", "iran",
        "ukraine", "russia", "china", "taiwan", "israel", "gaza",
        "territory", "coup", "control", "island", "treaty", "diplomatic"
    ],
    "macroeconomics": [
        "recession", "gdp", "inflation", "fed", "rate", "cut", "hike",
        "unemployment", "economy", "economic", "cpi", "interest",
        "federal reserve", "ecb", "boe", "growth", "stagflation", "tariff",
        "trade", "debt", "deficit", "treasury"
    ],
    "institutions_justice": [
        "court", "supreme", "ruling", "verdict", "trial", "impeach",
        "indicted", "arrest", "lawsuit", "conviction", "sentence",
        "justice", "legal", "law", "congress pass", "bill", "legislation",
        "regulation", "sec", "fda", "investigation"
    ],
    "tech_companies": [
        "apple", "google", "microsoft", "meta", "amazon", "tesla",
        "nvidia", "openai", "anthropic", "spacex", "x.com", "twitter",
        "ipo", "acquisition", "merger", "ceo", "launch", "product",
        "ai", "model", "chip", "crypto", "bitcoin", "ethereum"
    ],
}

# Patterns de marchés non analysables avec notre protocole
NON_ANALYZABLE_PATTERNS = [
    # Sports props O/U
    r'\bO/U\b',
    r'\bover/under\b',
    r':\s*(points|rebounds|assists|blocks|steals|threes|turnovers)\s+o/u',
    r'points o/u', r'rebounds o/u', r'assists o/u',
    r'\b(pts|reb|ast)\s+o/u',
    r'total\s+points', r'player\s+prop',

    # Résultats sportifs collectifs (win the ... finals/championship/series)
    r'win the\s+\w+\s+(nba|nfl|nhl|mlb|mls)\b',
    r'win the (nba|nfl|nhl|mlb|nba)\s+',
    r'\b(nba|nfl|nhl|mlb)\s+(finals|championship|playoffs|eastern|western)',
    r'win more than \d+\.?\d*\s*(games|wins)',
    r'\b(conference finals|division series|world series)\b',
    r'timberwolves|celtics|thunder|lakers|warriors|nets|bulls|heat|bucks',

    # Marchés "before GTA VI" — Polymarket les utilise comme référence absurde
    r'before gta vi',
    r'before gta 6',

    # Marchés mèmes / culturels sans valeur analytique
    r'jesus christ (return|come back|second coming)',
    r'alien(s)? (contact|invasion|land)',
    r'zombie apocalypse',
    r'(album|song|single|tour|concert)\s+(before|by|in)',
    r'new\s+\w+\s+album',

    # Candidats anonymisés
    r'\bcandidate\s+[a-z]\b',
    r'\bperson\s+[a-z]{1,3}\b',

    # Crypto prix fixes
    r'dip to \$\d',
    r'reach \$\d+',
    r'hit \$\d+',
    r'above \$\d+k?\b',
    r'below \$\d+k?\b',
    r'price (above|below|over|under)\s+\$',
    r'to \$\d+,?\d*\s+in',

    # Paris entertainment / célébrités
    r'next james bond',
    r'pizza hut',
    r'acquired before',
    r'(kardashian|kanye|taylor swift|beyoncé)\b',

    # Prix cibles sur commodités et crypto (market cap, FDV, prix fixes)
    r'hit\s*\(?low\)?\s*\$\d',
    r'hit\s*\(?high\)?\s*\$\d',
    r'\(low\)\s*\$\d',
    r'\(high\)\s*\$\d',
    r'\b(gold|silver|oil|copper|wheat|corn|gas)\s+\(?\w*\)?\s+hit',
    r'\b(gc|si|cl|ng|zc|zw)\b.{0,20}\$\d',
    r'market cap.{0,20}>\s*\$\d',
    r'market cap.{0,20}<\s*\$\d',
    r'\bfdv\b.{0,20}>\s*\$\d',
    r'one day after launch',

    # Résultats sportifs collectifs supplémentaires
    r'win the (atlantic|pacific|central|metro|northeast|southeast|northwest|southwest) (division|conference)',
    r'(lightning|bruins|rangers|panthers|maple leafs|canadiens|flyers|penguins|capitals|hurricanes|senators)\b',
    r'win the\s+\w+\s+(stanley cup|super bowl|world series|nba finals|mlb)',

]

import re as _re
NON_ANALYZABLE_RE = [_re.compile(p, _re.IGNORECASE) for p in NON_ANALYZABLE_PATTERNS]

def is_non_analyzable(title: str) -> bool:
    """Détecte les marchés structurellement inanalysables avec le protocole."""
    if not title:
        return False
    for pattern in NON_ANALYZABLE_RE:
        if pattern.search(title):
            return True
    return False


def detect_category(title: str) -> str:
    text = (title or "").lower()
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

def api_get(url: str, params: dict = None) -> list | dict:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "paper-trading-scanner/1.0", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ConnectionError(f"Connexion impossible: {e.reason}")


def fetch_all_active_events(limit: int = 500) -> list:
    """Récupère les événements actifs paginés."""
    events = []
    offset = 0
    page_size = min(DEFAULT_PAGE_SIZE, limit)

    while len(events) < limit:
        params = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
            "order": "volume",
            "ascending": "false",
        }
        batch = api_get(GAMMA_EVENTS_URL, params)
        if not batch:
            break
        events.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    return events[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# Parsing des marchés
# ─────────────────────────────────────────────────────────────────────────────

def parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return []

def get_outcomes(market: dict) -> list:
    return [str(o) for o in parse_json_field(market.get("outcomes", []))]

def get_prices(market: dict) -> list:
    prices = parse_json_field(market.get("outcomePrices", []))
    try:
        return [round(float(p), 4) for p in prices]
    except (ValueError, TypeError):
        return []

def is_yes_no_binary(outcomes: list) -> bool:
    if len(outcomes) != 2:
        return False
    return {o.strip().lower() for o in outcomes} <= {"yes", "no"}

def is_anonymized(question: str) -> bool:
    return bool(re.match(r'Will Person [A-Z]{1,3} win', question, re.IGNORECASE))

def looks_like_scalar(outcomes: list) -> bool:
    numeric_pattern = re.compile(r'\d')
    numeric_count = sum(1 for o in outcomes if numeric_pattern.search(o))
    return len(outcomes) >= 3 and numeric_count >= len(outcomes) * 0.6


# ─────────────────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────────────────

def score_market(market_data: dict, cfg: dict) -> dict | None:
    """
    Évalue un contrat individuel et retourne un dict enrichi avec score,
    ou None si le contrat est à rejeter.
    """
    title     = market_data.get("title", "")
    question  = market_data.get("question", title)
    outcomes  = get_outcomes(market_data)
    prices    = get_prices(market_data)

    # Fallback yes/no directs
    if not outcomes and market_data.get("yes") is not None:
        outcomes = ["Yes", "No"]
        prices   = [round(float(market_data["yes"]), 4),
                    round(float(market_data.get("no", 1 - float(market_data["yes"]))), 4)]

    if not outcomes or not prices:
        return None

    # Filtre : marchés non analysables (sports O/U, candidats anonymisés, etc.)
    if is_non_analyzable(question or title):
        return None

    # Filtre : binaire pur uniquement
    if not is_yes_no_binary(outcomes):
        return None
    if looks_like_scalar(outcomes):
        return None
    if is_anonymized(question):
        return None

    price_yes = prices[0]
    price_no  = prices[1] if len(prices) > 1 else round(1 - price_yes, 4)

    # ── Filtres disqualifiants ────────────────────────────────────────────────

    # Prix hors zone fiable
    if price_yes < cfg["min_price"] or price_yes > cfg["max_price"]:
        return None

    # Volume insuffisant
    volume = 0
    try:
        volume = float(market_data.get("volume") or 0)
    except (ValueError, TypeError):
        pass
    if volume < cfg["min_volume"]:
        return None

    # Horizon
    end_date_str = market_data.get("endDate") or ""
    days_to_resolution = None
    if end_date_str:
        try:
            if not end_date_str.endswith("Z"):
                end_date_str += "Z"
            end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            days_to_resolution = (end_date - datetime.now(timezone.utc)).days
        except ValueError:
            pass

    if days_to_resolution is not None:
        if days_to_resolution < cfg["min_days"] or days_to_resolution > cfg["max_days"]:
            return None

    # ── Score d'intérêt (0 à 100) ────────────────────────────────────────────
    score = 0

    # 1. Prix dans la zone optimale (35-65% = max, dégradé vers les bords)
    center_distance = abs(price_yes - 0.50)
    price_score = max(0, 40 - int(center_distance * 120))  # 40 pts max au centre
    score += price_score

    # 2. Volume (log-normalisé, 30 pts max)
    import math
    if volume > 0:
        vol_score = min(30, int(math.log10(volume / cfg["min_volume"] + 1) * 15))
        score += vol_score

    # 3. Horizon optimal (30-120j = max, dégradé)
    if days_to_resolution is not None:
        if 30 <= days_to_resolution <= 120:
            horizon_score = 20
        elif 7 <= days_to_resolution < 30:
            horizon_score = 15
        elif 120 < days_to_resolution <= 180:
            horizon_score = 12
        elif 180 < days_to_resolution <= 365:
            horizon_score = 8
        else:
            horizon_score = 3
        score += horizon_score

    # 4. Liquidité relative (spread implicite)
    # Sur Polymarket, price_yes + price_no devrait = ~1.0
    # Un spread élevé (somme < 0.95) indique un marché illiquide
    spread = round(price_yes + price_no, 4)
    if spread >= 0.99:
        score += 10
    elif spread >= 0.97:
        score += 5

    return {
        "question":          question,
        "title":             title,
        "price_yes":         price_yes,
        "price_no":          price_no,
        "volume":            round(volume, 0),
        "days_to_resolution": days_to_resolution,
        "end_date":          end_date_str[:10] if end_date_str else "?",
        "spread":            spread,
        "score":             score,
        "category_detected": detect_category(question or title),
        "slug":              market_data.get("slug") or market_data.get("conditionId", ""),
        "market_id":         market_data.get("id") or market_data.get("conditionId", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dédoublonnage via journal
# ─────────────────────────────────────────────────────────────────────────────

def load_already_analyzed() -> set:
    """Charge les URLs/titres déjà dans le journal pour éviter les doublons."""
    already = set()
    journal_path = Path("journal.json")
    if journal_path.exists():
        try:
            with open(journal_path) as f:
                journal = json.load(f)
            for pos in journal.get("positions", []):
                url = pos.get("market_url", "")
                title = pos.get("market_title", "")
                if url:
                    already.add(url.lower())
                if title:
                    already.add(title.lower())
        except Exception:
            pass
    return already


# ─────────────────────────────────────────────────────────────────────────────
# Affichage
# ─────────────────────────────────────────────────────────────────────────────

SCORE_ICONS = {
    (70, 100): "🔥",
    (50, 70):  "⭐",
    (30, 50):  "○",
    (0,  30):  "·",
}

def score_icon(score: int) -> str:
    for (lo, hi), icon in SCORE_ICONS.items():
        if lo <= score < hi:
            return icon
    return "·"

def bar(value: float, width: int = 12) -> str:
    filled = int(value * width)
    return "█" * filled + "░" * (width - filled)

def display_results(results: list, cfg: dict):
    total_scanned = cfg.get("total_scanned", 0)
    total_filtered = len(results)

    print(f"\n{'═' * 65}")
    print(f"  POLYMARKET SCAN — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'═' * 65}")
    print(f"  Marchés scannés  : {total_scanned}")
    print(f"  Après filtrage   : {total_filtered}")
    if cfg.get("category"):
        print(f"  Catégorie        : {cfg['category']}")
    print(f"  Critères         : prix {cfg['min_price']:.0%}–{cfg['max_price']:.0%}  "
          f"volume ≥ ${cfg['min_volume']:,.0f}  "
          f"horizon {cfg['min_days']}–{cfg['max_days']}j")
    print(f"{'─' * 65}\n")

    if not results:
        print("  Aucun marché ne correspond aux critères actuels.")
        print("  Essaie d'assouplir --min-volume ou d'élargir l'horizon.\n")
        return

    print(f"  {'#':<3} {'Score':>5}  {'Prix YES':>9}  {'Volume':>10}  "
          f"{'Jours':>6}  Titre")
    print(f"  {'─'*3} {'─'*5}  {'─'*9}  {'─'*10}  {'─'*6}  {'─'*40}")

    for i, r in enumerate(results, 1):
        icon   = score_icon(r["score"])
        vol    = f"${r['volume']:>8,.0f}" if r["volume"] else "    N/A   "
        days   = f"{r['days_to_resolution']:>5}j" if r["days_to_resolution"] is not None else "   ?  "
        title  = r["question"][:50] if r["question"] else r["title"][:50]
        cat    = r["category_detected"][:12]

        print(f"  {i:<3} {icon} {r['score']:>3}  "
              f"YES {r['price_yes']:>5.0%}  {vol}  {days}  {title}")
        url_display = r.get("market_url", "")
        print(f"       {bar(r['price_yes'])}  [{cat}]  résolution: {r['end_date']}")
        if url_display:
            print(f"       {url_display}")
        print()

    print(f"{'─' * 65}")
    print(f"  🔥 = score ≥ 70  ⭐ = score ≥ 50  ○ = score ≥ 30")
    print(f"{'═' * 65}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scanne Polymarket pour trouver les marchés à analyser.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python polymarket_scan.py
  python polymarket_scan.py --category geopolitics
  python polymarket_scan.py --min-volume 50000 --max-days 90
  python polymarket_scan.py --top 10 --export scan.json
        """
    )
    parser.add_argument("--category",   default=None,
                        choices=["electoral_politics", "geopolitics", "macroeconomics",
                                 "institutions_justice", "tech_companies"],
                        help="Filtre par catégorie")
    parser.add_argument("--min-price",  type=float, default=0.15,
                        help="Prix YES minimum (défaut: 0.15)")
    parser.add_argument("--max-price",  type=float, default=0.75,
                        help="Prix YES maximum (défaut: 0.75)")
    parser.add_argument("--min-volume", type=float, default=5000,
                        help="Volume minimum en $ (défaut: 5000)")
    parser.add_argument("--min-days",   type=int,   default=7,
                        help="Horizon minimum en jours (défaut: 7)")
    parser.add_argument("--max-days",   type=int,   default=365,
                        help="Horizon maximum en jours (défaut: 365)")
    parser.add_argument("--top",        type=int,   default=25,
                        help="Nombre de résultats à afficher (défaut: 25)")
    parser.add_argument("--scan-limit", type=int,   default=500,
                        help="Nombre max d'événements à scanner (défaut: 500)")
    parser.add_argument("--export",     default=None,
                        help="Exporte les résultats en JSON")
    parser.add_argument("--no-dedup",   action="store_true",
                        help="Ne pas exclure les marchés déjà dans le journal")
    args = parser.parse_args()

    cfg = {
        "min_price":  args.min_price,
        "max_price":  args.max_price,
        "min_volume": args.min_volume,
        "min_days":   args.min_days,
        "max_days":   args.max_days,
        "category":   args.category,
    }

    print(f"\n  Scan en cours... (limite: {args.scan_limit} événements)", end="", flush=True)

    # ── Récupération ──────────────────────────────────────────────────────────
    try:
        events = fetch_all_active_events(limit=args.scan_limit)
    except ConnectionError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f" {len(events)} événements récupérés.")

    # ── Marchés déjà analysés ─────────────────────────────────────────────────
    already_analyzed = set() if args.no_dedup else load_already_analyzed()

    # ── Scoring ───────────────────────────────────────────────────────────────
    candidates = []
    total_contracts = 0

    for event in events:
        markets = event.get("markets", [])

        # Le slug de l'événement parent → URL de la page générale
        event_slug = (
            event.get("slug") or
            event.get("url", "").split("/event/")[-1].split("?")[0] or
            ""
        )

        for market in markets:
            total_contracts += 1

            # Enrichit le market avec les données de l'event si manquantes
            if not market.get("endDate"):
                market["endDate"] = event.get("endDate")
            if not market.get("volume"):
                market["volume"] = event.get("volume")
            if not market.get("question"):
                market["question"] = event.get("title")

            result = score_market(market, cfg)
            if result is None:
                continue

            # Filtre catégorie
            if args.category and result["category_detected"] != args.category:
                continue

            # Filtre doublons
            q_lower = result["question"].lower()
            t_lower = result["title"].lower()
            if q_lower in already_analyzed or t_lower in already_analyzed:
                continue

            # URL : toujours la page générale de l'événement parent
            # (pas le contrat individuel)
            market_slug = result.get("slug") or ""
            slug = event_slug or market_slug
            result["market_url"] = f"https://polymarket.com/event/{slug}" if slug else ""

            candidates.append(result)

    # ── Tri et sélection ──────────────────────────────────────────────────────
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_results = candidates[:args.top]

    cfg["total_scanned"] = total_contracts

    # ── Affichage ─────────────────────────────────────────────────────────────
    display_results(top_results, cfg)

    # ── Export optionnel ──────────────────────────────────────────────────────
    if args.export:
        export_data = {
            "scan_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config": cfg,
            "total_scanned": total_contracts,
            "results_count": len(top_results),
            "results": top_results,
        }
        with open(args.export, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False)
        print(f"  ✓ Résultats exportés : {args.export}\n")

    # ── Suggestion finale ─────────────────────────────────────────────────────
    if top_results:
        best = top_results[0]
        print(f"  Marché le plus intéressant détecté :")
        print(f"  → {best['question'][:60]}")
        print(f"     YES {best['price_yes']:.0%}  |  score {best['score']}  "
              f"|  {best['days_to_resolution']}j")
        if best.get("market_url"):
            print(f"     {best['market_url']}")
        print(f"\n  Pour l'analyser :")
        if best.get("market_url"):
            print(f"  python fetch_market.py --url \"{best['market_url']}\" --id MKT-00X\n")
        else:
            print(f"  Recherche ce titre sur Polymarket.com\n")


if __name__ == "__main__":
    main()