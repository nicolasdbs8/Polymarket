#!/usr/bin/env python3
"""
fetch_market.py — Génère automatiquement un market_request.json
                  à partir d'une URL Polymarket.

Gère tous les types de marchés :
  - Type 1 : Binaire pur          (YES/NO sur un événement)
  - Type 2 : Binaires groupés     (plusieurs contrats YES/NO dans un événement)
  - Type 3 : Catégoriel direct    (Republican / Democrat, sans YES/NO)
  - Type 4 : Tranches scalaires   (0bps / 25bps / 50bps — refusé, hors périmètre)

Usage :
  python fetch_market.py --url "https://polymarket.com/event/..." --id MKT-007
  python fetch_market.py --url "https://polymarket.com/event/..." --id MKT-007 --market-index 2
"""

import json
import sys
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Catégories
# ─────────────────────────────────────────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "electoral_politics": [
        "election", "vote", "president", "senate", "congress", "mayor",
        "parliament", "ballot", "primary", "runoff", "polling", "elected",
        "chancellor", "prime minister", "referendum", "gubernatorial"
    ],
    "geopolitics": [
        "war", "ceasefire", "invasion", "military", "troops", "missile",
        "nuclear", "sanction", "nato", "conflict", "attack", "iran",
        "ukraine", "russia", "china", "taiwan", "israel", "gaza",
        "territory", "coup", "control", "island"
    ],
    "macroeconomics": [
        "recession", "gdp", "inflation", "fed", "rate", "cut", "hike",
        "unemployment", "economy", "economic", "cpi", "interest",
        "federal reserve", "ecb", "boe", "growth", "stagflation"
    ],
    "institutions_justice": [
        "court", "supreme", "ruling", "verdict", "trial", "impeach",
        "indicted", "arrest", "lawsuit", "conviction", "sentence",
        "justice", "legal", "law", "congress pass", "bill", "legislation"
    ],
    "tech_companies": [
        "apple", "google", "microsoft", "meta", "amazon", "tesla",
        "nvidia", "openai", "anthropic", "spacex", "x.com", "twitter",
        "ipo", "acquisition", "merger", "ceo", "launch", "product",
        "ai", "model", "chip"
    ],
}

def detect_category(title: str, tags: list = None) -> str:
    text = (title or "").lower()
    if tags:
        text += " " + " ".join(t.lower() for t in tags)
    scores = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                scores[cat] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "other"


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires de parsing API
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
    outcomes = parse_json_field(market.get("outcomes", []))
    return [str(o) for o in outcomes]

def get_prices(market: dict) -> list:
    prices = parse_json_field(market.get("outcomePrices", []))
    try:
        return [round(float(p), 4) for p in prices]
    except (ValueError, TypeError):
        return []

def extract_slug(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    parts  = [p for p in parsed.path.rstrip("/").split("/") if p]
    if "event" in parts:
        idx = parts.index("event")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    raise ValueError(
        f"Impossible d'extraire le slug depuis : {url}\n"
        "Format attendu : https://polymarket.com/event/[slug]"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Détection du type de marché
# ─────────────────────────────────────────────────────────────────────────────

TYPE_BINARY      = "binary"
TYPE_GROUPED     = "grouped"
TYPE_CATEGORICAL = "categorical"
TYPE_SCALAR      = "scalar"

def is_yes_no_binary(outcomes: list) -> bool:
    if len(outcomes) != 2:
        return False
    normalized = {o.strip().lower() for o in outcomes}
    return normalized <= {"yes", "no"}

def looks_like_scalar(outcomes: list) -> bool:
    import re
    numeric_pattern = re.compile(r'\d')
    numeric_count = sum(1 for o in outcomes if numeric_pattern.search(o))
    return len(outcomes) >= 3 and numeric_count >= len(outcomes) * 0.6

def classify_event(event: dict):
    markets   = event.get("markets", [])
    contracts = []

    for i, m in enumerate(markets):
        outcomes = get_outcomes(m)
        prices   = get_prices(m)

        if not outcomes and m.get("yes") is not None:
            outcomes = ["Yes", "No"]
            prices   = [round(float(m["yes"]), 4),
                        round(float(m.get("no", 1 - float(m["yes"]))), 4)]

        if not outcomes and prices:
            outcomes = [f"Option {j+1}" for j in range(len(prices))]

        if is_yes_no_binary(outcomes):
            mtype = TYPE_BINARY
        elif looks_like_scalar(outcomes):
            mtype = TYPE_SCALAR
        else:
            mtype = TYPE_CATEGORICAL

        contracts.append({
            "index":    i,
            "question": m.get("question") or event.get("title") or "?",
            "type":     mtype,
            "outcomes": outcomes,
            "prices":   prices,
            "raw":      m,
        })

    if not contracts:
        return TYPE_BINARY, []

    types_found = {c["type"] for c in contracts}

    if len(contracts) == 1:
        return contracts[0]["type"], contracts
    if TYPE_SCALAR in types_found and len(types_found) == 1:
        return TYPE_SCALAR, contracts
    if all(c["type"] == TYPE_BINARY for c in contracts):
        return TYPE_GROUPED, contracts
    return TYPE_CATEGORICAL, contracts


# ─────────────────────────────────────────────────────────────────────────────
# Affichage et sélection interactive
# ─────────────────────────────────────────────────────────────────────────────

import re as _re

def is_anonymized(question: str) -> bool:
    """Détecte les contrats masqués type Person BG, Person CZ, etc."""
    return bool(_re.match(r'Will Person [A-Z]{1,3} win', question, _re.IGNORECASE))


def display_contracts(contracts: list):
    named = [c for c in contracts if not is_anonymized(c["question"])
             and c["type"] != TYPE_SCALAR]
    anon_count = sum(1 for c in contracts if is_anonymized(c["question"]))

    print(f"\n  {'─' * 58}")
    print(f"  {len(named)} contrats nommés ({anon_count} candidats anonymisés masqués) :")
    print(f"  {'─' * 58}\n")

    for c in named:
        idx      = c["index"]
        question = c["question"][:52]
        outcomes = c["outcomes"]
        prices   = c["prices"]
        mtype    = c["type"]

        if mtype == TYPE_BINARY and len(outcomes) == 2 and len(prices) >= 2:
            yes_p = prices[0]
            no_p  = prices[1] if len(prices) > 1 else round(1 - yes_p, 4)
            print(f"  [{idx:>3}] {question}")
            print(f"         YES {yes_p:.0%}  /  NO {no_p:.0%}\n")
        elif mtype == TYPE_CATEGORICAL and outcomes and prices:
            print(f"  [{idx:>3}] {question}")
            for outcome, price in zip(outcomes, prices):
                bar = "█" * int(price * 20)
                print(f"         {outcome:<22} {price:.0%}  {bar}")
            print()
        else:
            print(f"  [{idx:>3}] {question}  ({len(outcomes)} issues)\n")

    if anon_count:
        print(f"  (Candidats anonymisés accessibles via --market-index N)\n")


def select_contract(contracts: list, market_index):
    tradeable = [c for c in contracts if c["type"] != TYPE_SCALAR]

    if not tradeable:
        raise ValueError(
            "Tous les contrats sont des tranches scalaires — non supporté en V1.\n"
            "Cherche une version YES/NO du même sujet."
        )

    if market_index is not None:
        match = [c for c in tradeable if c["index"] == market_index]
        if match:
            return match[0]
        if 0 <= market_index < len(tradeable):
            return tradeable[market_index]
        print(f"  [AVERTISSEMENT] --market-index {market_index} invalide.")
        return tradeable[0]

    named = [c for c in tradeable if not is_anonymized(c["question"])]
    pool  = named if named else tradeable

    if len(pool) == 1:
        c = pool[0]
        print(f"  → Contrat unique : {c['question'][:55]}")
        return c

    while True:
        try:
            choice = input(f"  → Index du contrat à analyser : ").strip()
            idx    = int(choice)
            match  = [c for c in pool if c["index"] == idx]
            if match:
                return match[0]
            print(f"  Index invalide. Entrez un index parmi les contrats listés ci-dessus.")
        except (ValueError, KeyboardInterrupt):
            print("\n  Annulé.")
            sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# API Polymarket
# ─────────────────────────────────────────────────────────────────────────────

def fetch_event(slug: str) -> dict:
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    print(f"  → Requête API : {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "paper-trading-bot/1.0", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"Erreur HTTP {e.code} : {e.reason}")
    except urllib.error.URLError as e:
        raise ConnectionError(f"Impossible de contacter l'API Polymarket : {e.reason}")
    if not data:
        raise ValueError(f"Aucun événement trouvé pour le slug : '{slug}'")
    return data[0]


# ─────────────────────────────────────────────────────────────────────────────
# Construction du market_request
# ─────────────────────────────────────────────────────────────────────────────

def build_market_request(event: dict, contract: dict,
                          market_id: str, url: str) -> dict:
    mtype    = contract["type"]
    outcomes = contract["outcomes"]
    prices   = contract["prices"]
    raw      = contract["raw"]

    if mtype == TYPE_BINARY:
        price_yes = prices[0] if prices else 0.5
        price_no  = prices[1] if len(prices) > 1 else round(1 - price_yes, 4)
    elif mtype == TYPE_CATEGORICAL:
        price_yes = prices[0] if prices else 0.5
        price_no  = round(max(0.01, min(0.99, 1 - price_yes)), 4)
    else:
        price_yes = 0.5
        price_no  = 0.5

    price_yes = max(0.01, min(0.99, round(price_yes, 4)))
    price_no  = max(0.01, min(0.99, round(price_no,  4)))

    end_date = raw.get("endDate") or event.get("endDate") or ""
    if end_date and not end_date.endswith("Z"):
        end_date += "Z"
    resolution_date = end_date or "UNKNOWN — à remplir manuellement"

    title = contract["question"]
    if mtype == TYPE_CATEGORICAL and outcomes:
        issue = outcomes[0]
        if issue.lower() not in title.lower():
            title = f"{title} — {issue}"

    volume = raw.get("volume") or event.get("volume")
    try:
        volume = round(float(volume), 2) if volume else None
    except (ValueError, TypeError):
        volume = None

    tags     = [t.get("label", "") for t in event.get("tags", [])]
    category = detect_category(title, tags)

    request = {
        "market_id":              market_id,
        "market_title":           title,
        "platform":               "polymarket",
        "market_type":            mtype,
        "market_probability_yes": price_yes,
        "price_yes":              price_yes,
        "price_no":               price_no,
        "market_probability_no":  price_no,
        "resolution_date":        resolution_date,
        "market_snapshot_time":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category":               category,
        "market_url":             url,
        "schema_version":         "1.0",
    }

    if mtype == TYPE_CATEGORICAL and outcomes and prices:
        request["categorical_context"] = {
            "all_outcomes":      outcomes,
            "all_prices":        prices,
            "analyzed_outcome":  outcomes[0] if outcomes else "",
        }

    if volume is not None:
        request["market_volume"] = volume

    return request


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    TYPE_BINARY:      "BINAIRE",
    TYPE_GROUPED:     "BINAIRES GROUPÉS",
    TYPE_CATEGORICAL: "CATÉGORIEL",
    TYPE_SCALAR:      "TRANCHES SCALAIRES (hors périmètre)",
}

def main():
    parser = argparse.ArgumentParser(
        description="Génère un market_request.json depuis une URL Polymarket."
    )
    parser.add_argument("--url",          required=True)
    parser.add_argument("--id",           required=True, dest="market_id")
    parser.add_argument("--market-index", type=int, default=None,
                        help="Index du contrat (évite la saisie interactive)")
    args = parser.parse_args()

    print(f"\n{'═' * 62}")
    print(f"  FETCH MARKET — {args.market_id}")
    print(f"{'═' * 62}")
    print(f"  URL : {args.url}\n")

    try:
        slug = extract_slug(args.url)
        print(f"  Slug : {slug}")
    except ValueError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)

    try:
        event = fetch_event(slug)
        print(f"  Événement : {event.get('title', 'N/A')}\n")
    except (ConnectionError, ValueError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)

    event_type, contracts = classify_event(event)
    print(f"  Structure détectée : {TYPE_LABELS.get(event_type, event_type)}")

    if event_type == TYPE_SCALAR:
        print("\n[REFUS] Marché entièrement scalaire — non supporté en V1.")
        print("  Cherche une version YES/NO du même sujet sur Polymarket.")
        sys.exit(1)

    if len(contracts) > 1:
        display_contracts(contracts)

    try:
        contract = select_contract(contracts, args.market_index)
    except ValueError as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"\n  ✓ Contrat sélectionné : {contract['question'][:55]}")
    print(f"    Type : {TYPE_LABELS.get(contract['type'], contract['type'])}")

    mr = build_market_request(event, contract, args.market_id, args.url)

    output_dir  = Path("markets") / args.market_id
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "market_request.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mr, f, indent=2, ensure_ascii=False)

    # Calcul des jours restants
    days_str = "?"
    try:
        end = mr["resolution_date"]
        if not end.endswith("Z"):
            end += "Z"
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        days = (end_dt - datetime.now(timezone.utc)).days
        days_str = f"{days}j"
    except Exception:
        pass

    def col(label, value):
        print(f"  {label:<16}: {value}")

    print(f"\n{'═' * 62}")
    print(f"  FETCH MARKET — {args.market_id}  ✓")
    print(f"{'═' * 62}")
    print(f"  {mr['market_title'][:58]}")
    print(f"{'─' * 62}")
    col("Catégorie",   mr["category"])
    col("Prix YES",    f"{mr['price_yes']:.0%}  /  NO {mr['price_no']:.0%}")
    col("Résolution",  f"{mr['resolution_date'][:10]}  ({days_str})")
    if mr.get("market_volume"):
        col("Volume",  f"${mr['market_volume']:,.0f}")
    if mr.get("market_type") and mr["market_type"] != "binary":
        col("Type marché", mr["market_type"])
    if mr.get("categorical_context"):
        ctx = mr["categorical_context"]
        print(f"\n  Distribution complète :")
        for o, p in zip(ctx["all_outcomes"], ctx["all_prices"]):
            bar = "█" * int(p * 16)
            marker = " ←" if o == ctx["analyzed_outcome"] else "  "
            print(f"    {marker} {o:<22} {p:.0%}  {bar}")
    print(f"{'─' * 62}")
    print(f"  Fichier créé   : {output_path}")
    print(f"{'═' * 62}")
    print(f"\n  Prochaines étapes :")
    print(f"  1. Envoie le market_request.json à ChatGPT → analysis_payload.json")
    print(f"  2. Place le payload dans markets/{args.market_id}/")
    print(f"  3. python moteur.py --payload markets/{args.market_id}/analysis_payload.json")
    print(f"  4. python journal.py add --market {args.market_id}\n")


if __name__ == "__main__":
    main()