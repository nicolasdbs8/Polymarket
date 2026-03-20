#!/usr/bin/env python3
"""
batch_fetch.py — Génère une demande d'analyse groupée pour un événement
                 multi-contrats (tournoi, élection multi-candidats, etc.)

Au lieu de faire N allers-retours avec ChatGPT, ce script :
1. Récupère tous les contrats nommés d'un événement Polymarket
2. Génère un fichier batch_request.json avec tous les contrats
3. Génère le prompt exact à coller dans ChatGPT
ChatGPT produit un batch_payload.json unique avec toutes les analyses.

Usage :
  python batch_fetch.py --url "https://polymarket.com/event/2026-ncaa-tournament-winner" --id BATCH-001
  python batch_fetch.py --url "..." --id BATCH-001 --min-price 0.03  (filtre les outsiders)
  python batch_fetch.py --url "..." --id BATCH-001 --top 10           (garde les N premiers prix)
"""

import json
import sys
import argparse
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# ── Imports partagés avec fetch_market ───────────────────────────────────────

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
    raise ValueError(f"Impossible d'extraire le slug depuis : {url}")

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
        raise ConnectionError(f"Impossible de contacter l'API : {e.reason}")
    if not data:
        raise ValueError(f"Aucun événement trouvé pour le slug : '{slug}'")
    return data[0]

def is_anonymized(question: str) -> bool:
    import re
    return bool(re.match(r'Will Person [A-Z]{1,3} win', question, re.IGNORECASE))

def is_yes_no_binary(outcomes: list) -> bool:
    if len(outcomes) != 2:
        return False
    return {o.strip().lower() for o in outcomes} <= {"yes", "no"}


# ─────────────────────────────────────────────────────────────────────────────
# Extraction des contrats
# ─────────────────────────────────────────────────────────────────────────────

def extract_contracts(event: dict, min_price: float, top: int) -> list:
    """
    Extrait tous les contrats tradéables de l'événement.
    Pour les marchés binaires groupés : extrait le prix YES de chaque contrat.
    Pour les marchés catégoriels : extrait chaque issue comme un contrat.
    """
    markets  = event.get("markets", [])
    contracts = []

    for i, m in enumerate(markets):
        question = m.get("question") or event.get("title") or "?"
        if is_anonymized(question):
            continue

        outcomes = get_outcomes(m)
        prices   = get_prices(m)

        # Fallback yes/no directs
        if not outcomes and m.get("yes") is not None:
            outcomes = ["Yes", "No"]
            prices   = [round(float(m["yes"]), 4),
                        round(float(m.get("no", 1 - float(m["yes"]))), 4)]

        if not outcomes or not prices:
            continue

        if is_yes_no_binary(outcomes):
            # Contrat binaire : on prend le prix YES
            price_yes = prices[0]
            price_no  = prices[1] if len(prices) > 1 else round(1 - price_yes, 4)
            contracts.append({
                "contract_id": f"{i}",
                "label":       question,
                "price_yes":   round(price_yes, 4),
                "price_no":    round(price_no, 4),
                "type":        "binary",
            })
        else:
            # Catégoriel : chaque issue devient un contrat
            for outcome, price in zip(outcomes, prices):
                contracts.append({
                    "contract_id": f"{i}_{outcome.replace(' ', '_')}",
                    "label":       outcome,
                    "price_yes":   round(price, 4),
                    "price_no":    round(max(0.01, 1 - price), 4),
                    "type":        "categorical",
                    "parent_question": question,
                })

    # Filtre par prix minimum
    if min_price > 0:
        before = len(contracts)
        contracts = [c for c in contracts if c["price_yes"] >= min_price]
        filtered = before - len(contracts)
        if filtered:
            print(f"  → {filtered} contrats filtrés (prix YES < {min_price:.0%})")

    # Trie par prix décroissant
    contracts.sort(key=lambda c: c["price_yes"], reverse=True)

    # Limite au top N
    if top and top < len(contracts):
        print(f"  → Limitation aux {top} premiers prix (sur {len(contracts)} contrats)")
        contracts = contracts[:top]

    return contracts


# ─────────────────────────────────────────────────────────────────────────────
# Génération du prompt ChatGPT
# ─────────────────────────────────────────────────────────────────────────────

def generate_chatgpt_prompt(event: dict, contracts: list,
                             batch_id: str, url: str,
                             resolution_date: str) -> str:
    contracts_list = "\n".join(
        f'  - [{c["contract_id"]}] {c["label"]} — YES {c["price_yes"]:.0%} / NO {c["price_no"]:.0%}'
        for c in contracts
    )

    return f"""Tu dois produire une analyse groupée pour un événement Polymarket multi-contrats.

# Événement
Titre : {event.get('title', '')}
URL   : {url}
Date de résolution : {resolution_date}
Batch ID : {batch_id}

# Contrats à analyser ({len(contracts)})
{contracts_list}

# Ce que tu dois produire

Un fichier JSON unique nommé batch_payload.json contenant une liste d'objets analysis_payload,
un par contrat listé ci-dessus.

Chaque analysis_payload doit respecter exactement le schéma V2.3 habituel, avec :
- market_id      : "{batch_id}-[contract_id]"  ex: "{batch_id}-38"
- thesis_id      : market_id + "-TH1"
- analysis_id    : thesis_id + "-A1"
- analysis_version : "A1"
- resolution_date : "{resolution_date}"
- protocol_version : "2.3"
- schema_version : "1.0"
- analysis_timestamp : heure actuelle ISO 8601

Pour chaque contrat, tu dois analyser indépendamment :
- le base_rate (fréquence historique pour ce type de candidat/équipe)
- les prérequis bloquants et pondérants
- les facteurs accélérateurs et freins spécifiques à ce contrat
- la confiance
- les ambiguïtés
- la contradiction forcée obligatoire (best_counter_thesis + top_3_failure_reasons)

# Format de sortie

```json
[
  {{ analysis_payload complet pour contrat 1 }},
  {{ analysis_payload complet pour contrat 2 }},
  ...
]
```

Le JSON doit être valide, indenté, sans commentaires.
Rappel : top_3_failure_reasons doit contenir EXACTEMENT 3 éléments.
Rappel : tous les enums en anglais : high/medium/low, accelerator/brake, etc.
Rappel : tu dois impérativement te référer au modèle batch_payload_exemple.json dans les fichiers source du projet.
"""


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Génère une demande d'analyse groupée pour un événement multi-contrats."
    )
    parser.add_argument("--url",       required=True)
    parser.add_argument("--id",        required=True, dest="batch_id",
                        help="Identifiant du batch (ex: BATCH-001)")
    parser.add_argument("--min-price", type=float, default=0.0,
                        help="Prix YES minimum pour inclure un contrat (ex: 0.03 = 3%%)")
    parser.add_argument("--top",       type=int, default=0,
                        help="Garder seulement les N contrats avec le prix le plus élevé")
    args = parser.parse_args()

    print(f"\n{'═' * 62}")
    print(f"  BATCH FETCH — {args.batch_id}")
    print(f"{'═' * 62}")
    print(f"  URL : {args.url}\n")

    # ── Slug + API ────────────────────────────────────────────────────────────
    try:
        slug  = extract_slug(args.url)
        event = fetch_event(slug)
        print(f"  Événement : {event.get('title', 'N/A')}\n")
    except (ValueError, ConnectionError) as e:
        print(f"\n[ERREUR] {e}", file=sys.stderr)
        sys.exit(1)

    # ── Date de résolution ────────────────────────────────────────────────────
    end_date = event.get("endDate") or ""
    if end_date and not end_date.endswith("Z"):
        end_date += "Z"
    resolution_date = end_date or "UNKNOWN"

    # ── Extraction des contrats ───────────────────────────────────────────────
    contracts = extract_contracts(event, args.min_price, args.top)

    if not contracts:
        print("\n[ERREUR] Aucun contrat trouvé après filtrage.", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(contracts)} contrats retenus pour l'analyse groupée :")
    for c in contracts:
        print(f"    [{c['contract_id']:>4}] {c['label'][:48]:<48} YES {c['price_yes']:.0%}")

    # ── Dossier de sortie ─────────────────────────────────────────────────────
    output_dir = Path("batches") / args.batch_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── batch_request.json ────────────────────────────────────────────────────
    batch_request = {
        "batch_id":        args.batch_id,
        "event_title":     event.get("title", ""),
        "event_url":       args.url,
        "resolution_date": resolution_date,
        "snapshot_time":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contract_count":  len(contracts),
        "contracts":       contracts,
        "schema_version":  "1.0",
    }

    request_path = output_dir / "batch_request.json"
    with open(request_path, "w", encoding="utf-8") as f:
        json.dump(batch_request, f, indent=2, ensure_ascii=False)

    # ── Prompt ChatGPT ────────────────────────────────────────────────────────
    prompt = generate_chatgpt_prompt(event, contracts, args.batch_id,
                                      args.url, resolution_date)
    prompt_path = output_dir / "chatgpt_prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt)

    # ── Résumé ────────────────────────────────────────────────────────────────
    print(f"\n  {'─' * 58}")
    print(f"  ✓ batch_request.json  → {request_path}")
    print(f"  ✓ chatgpt_prompt.txt  → {prompt_path}")
    print(f"\n  Prochaine étape :")
    print(f"  1. Ouvre batches/{args.batch_id}/chatgpt_prompt.txt")
    print(f"  2. Colle son contenu dans ChatGPT")
    print(f"  3. Sauvegarde la réponse dans batches/{args.batch_id}/batch_payload.json")
    print(f"  4. Lance : python batch_engine.py --batch {args.batch_id}")
    print(f"{'═' * 62}\n")


if __name__ == "__main__":
    main()