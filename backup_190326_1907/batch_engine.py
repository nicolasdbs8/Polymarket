#!/usr/bin/env python3
"""
batch_engine.py — Lance le moteur sur tous les analysis_payload
                  d'un batch_payload.json produit par ChatGPT.

Produit :
  batches/BATCH-001/results/
    ├── batch_summary.json     ← tableau de toutes les décisions
    ├── MKT-001-38/
    │   └── engine_output.json
    └── ...

Usage :
  python batch_engine.py --batch BATCH-001
  python batch_engine.py --batch BATCH-001 --config schemas/system_config.json
"""

import json
import sys
import argparse
from pathlib import Path

# Import du moteur existant
sys.path.insert(0, str(Path(__file__).parent))
from moteur import run_engine, load_json, save_json, ValidationError


def check_mutual_exclusivity(summary: list, tradeable: list):
    """
    Détecte les incohérences sur les marchés mutuellement exclusifs
    (élections, tournois) où les probabilités somment à ~100%.

    Logique :
    - Si toutes les questions du batch suivent le même pattern "Will X win...",
      les contrats sont probablement mutuellement exclusifs.
    - Dans ce cas, plusieurs positions NO simultanées peuvent être
      contradictoires (elles impliquent ensemble que personne ne gagne).
    - Plusieurs positions YES simultanées sont également impossibles.
    """
    if not summary:
        return

    import re as _re

    # Détecte si le batch ressemble à un marché "winner"
    winner_pattern = _re.compile(
        r'Will .+ win (the most seats|the .+ election|the .+ tournament)',
        _re.IGNORECASE
    )
    labels = [r.get("label", "") for r in summary]
    is_winner_market = sum(1 for l in labels if winner_pattern.search(l)) >= len(labels) * 0.5

    if not is_winner_market:
        return

    # Positions tradéables (hors abstentions et vetos)
    yes_positions = [r for r in tradeable if r["side"] == "yes"]
    no_positions  = [r for r in tradeable if r["side"] == "no"]

    has_issue = False

    # Plusieurs YES simultanés = impossibles dans un marché winner
    if len(yes_positions) > 1:
        has_issue = True
        print(f"\n  {'─' * 58}")
        print(f"  ⚠  ALERTE COHÉRENCE — Marché mutuellement exclusif détecté")
        print(f"  {'─' * 58}")
        print(f"  {len(yes_positions)} positions YES simultanées sur un marché winner.")
        print(f"  Un seul gagnant est possible — ces positions sont contradictoires.")
        print(f"  → Garde seulement celle avec l'edge ajusté le plus fort :")
        best = max(yes_positions, key=lambda r: abs(r["adj_edge"]))
        print(f"    ✓ YES {best['label'][:46]}  edge {best['adj_edge']:+.1%}")
        for r in yes_positions:
            if r is not best:
                print(f"    ✗ à exclure : {r['label'][:46]}")

    # Plusieurs NO simultanés — vérifier que leur somme est cohérente
    if len(no_positions) > 1:
        # Probabilité implicite cumulée des candidats ciblés par les NO
        # Si on bet NO sur X (price 65%) et NO sur Y (price 33%), on parie
        # que ni X ni Y ne gagne — ce qui n'est possible que si ~2% restent
        sum_prices = sum(r["price_yes"] for r in no_positions)
        remaining  = round(1.0 - sum_prices, 3)

        if remaining < 0:
            has_issue = True
            print(f"\n  {'─' * 58}")
            print(f"  ⚠  ALERTE COHÉRENCE — Marché mutuellement exclusif détecté")
            print(f"  {'─' * 58}")
            print(f"  {len(no_positions)} positions NO simultanées.")
            print(f"  Somme des prix ciblés : {sum_prices:.0%} > 100%")
            print(f"  Impossible : tu paries que personne ne gagne.")
            print(f"  → Garde seulement celle avec l'edge ajusté le plus fort :")
            best = max(no_positions, key=lambda r: abs(r["adj_edge"]))
            print(f"    ✓ NO {best['label'][:46]}  edge {best['adj_edge']:+.1%}")
            for r in no_positions:
                if r is not best:
                    print(f"    ✗ à exclure : {r['label'][:46]}")

        elif remaining < 0.05 and len(no_positions) > 1:
            # Positions logiquement possibles mais qui couvrent presque tout le marché
            has_issue = True
            print(f"\n  {'─' * 58}")
            print(f"  ⚠  ATTENTION COHÉRENCE — Marché mutuellement exclusif")
            print(f"  {'─' * 58}")
            print(f"  {len(no_positions)} positions NO couvrent {sum_prices:.0%} du marché.")
            print(f"  Probabilité résiduelle pour les autres candidats : {remaining:.0%}")
            print(f"  Ces positions sont techniquement compatibles mais très corrélées.")
            print(f"  → Envisage de ne conserver que la position avec le meilleur edge :")
            best = max(no_positions, key=lambda r: abs(r["adj_edge"]))
            print(f"    Meilleur edge : NO {best['label'][:44]}  {best['adj_edge']:+.1%}")

    if not has_issue and is_winner_market and tradeable:
        # Tout va bien — juste un rappel informatif
        print(f"\n  ℹ  Marché winner détecté : positions vérifiées, aucune contradiction.")


def main():
    parser = argparse.ArgumentParser(
        description="Lance le moteur sur un batch de payloads."
    )
    parser.add_argument("--batch",  required=True,
                        help="Identifiant du batch (ex: BATCH-001)")
    parser.add_argument("--config", default="schemas/system_config.json",
                        help="Chemin vers system_config.json")
    args = parser.parse_args()

    batch_dir    = Path("batches") / args.batch
    payload_path = batch_dir / "batch_payload.json"
    request_path = batch_dir / "batch_request.json"
    results_dir  = batch_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    # ── Chargements ───────────────────────────────────────────────────────────
    for p in [payload_path, request_path]:
        if not p.exists():
            print(f"[ERREUR] Fichier introuvable : {p}", file=sys.stderr)
            sys.exit(1)

    try:
        config = load_json(args.config)
    except FileNotFoundError:
        print(f"[ERREUR] Config introuvable : {args.config}", file=sys.stderr)
        sys.exit(1)

    batch_request = load_json(str(request_path))
    batch_payload = load_json(str(payload_path))

    # batch_payload peut être une liste directe ou {"analyses": [...]}
    if isinstance(batch_payload, list):
        payloads = batch_payload
    elif isinstance(batch_payload, dict):
        payloads = batch_payload.get("analyses") or batch_payload.get("payloads") or []
    else:
        print("[ERREUR] Format batch_payload.json invalide.", file=sys.stderr)
        sys.exit(1)

    if not payloads:
        print("[ERREUR] Aucun payload trouvé dans batch_payload.json.", file=sys.stderr)
        sys.exit(1)

    # Index des contrats par contract_id pour récupérer les prix marché
    contracts_index = {
        c["contract_id"]: c
        for c in batch_request.get("contracts", [])
    }

    print(f"\n{'═' * 62}")
    print(f"  BATCH ENGINE — {args.batch}")
    print(f"  {len(payloads)} payloads à traiter")
    print(f"{'═' * 62}\n")

    summary = []
    errors  = []

    for i, payload in enumerate(payloads):
        market_id = payload.get("market_id", f"unknown_{i}")

        # Retrouve le prix marché depuis batch_request
        # Le contract_id est la partie après le batch_id dans le market_id
        # ex: BATCH-001-38 → contract_id = "38"
        batch_prefix = args.batch + "-"
        contract_id  = market_id.replace(batch_prefix, "") if market_id.startswith(batch_prefix) else market_id
        contract     = contracts_index.get(contract_id, {})
        market_prob  = contract.get("price_yes", 0.5)

        # Dossier de sortie par contrat
        contract_dir = results_dir / market_id
        contract_dir.mkdir(parents=True, exist_ok=True)

        try:
            output = run_engine(payload, config, market_prob)
            save_json(output, str(contract_dir / "engine_output.json"))

            decision    = output["decision"]["decision"]
            side        = output["decision"]["position_side"]
            size        = output["decision"]["paper_position_size"]
            adj_edge    = output["edge"]["adjusted_edge"]
            p_est       = output["probability"]["p_estimated"]
            veto        = output["decision"]["veto_triggered"]
            veto_reason = output["decision"]["veto_reasons"][0] if output["decision"]["veto_reasons"] else ""

            label = payload.get("research_notes", {}).get("primary_sources", "") or contract.get("label", market_id)
            # Essaie d'extraire le nom du candidat depuis le titre de la question
            raw_label = contract.get("label", market_id)
            import re as _re
            # Pattern "Will X win..." → extrait X
            m = _re.match(r'Will (.+?) win ', raw_label, _re.IGNORECASE)
            if m:
                label = m.group(1)
            # Pattern "Will ... be X?" → extrait X (dernier mot/groupe)
            elif _re.search(r' be (.+?)\??$', raw_label, _re.IGNORECASE):
                m2 = _re.search(r' be (.+?)\??$', raw_label, _re.IGNORECASE)
                label = m2.group(1)
            else:
                label = raw_label

            row = {
                "market_id":   market_id,
                "label":       label,
                "price_yes":   market_prob,
                "p_estimated": p_est,
                "adj_edge":    adj_edge,
                "side":        side,
                "decision":    decision,
                "size":        size,
                "veto":        veto,
                "veto_reason": veto_reason,
            }
            summary.append(row)

            # Affichage ligne
            icon = "🟢" if decision != "no_trade" and not veto else ("⚪" if veto else "─")
            label_short = label[:44]
            id_short = market_id.split("-")[-1]  # juste le numéro
            print(f"  {icon} [{id_short:>3}] {label_short}")
            print(f"       marché {market_prob:.0%}  →  estimé {p_est:.0%}  "
                  f"edge {adj_edge:+.1%}  {side.upper()}  {decision}")
            if veto:
                # Coupe proprement sans couper un mot
                vr = veto_reason[:72]
                if len(veto_reason) > 72:
                    vr = vr.rsplit(" ", 1)[0] + "…"
                print(f"       ⚠ VETO : {vr}")
            print()

        except ValidationError as e:
            errors.append({"market_id": market_id, "error": str(e)})
            print(f"  ✗ [{market_id}] REFUS MOTEUR : {e}\n")

    # ── Sauvegarde du résumé ──────────────────────────────────────────────────
    summary_path = results_dir / "batch_summary.json"
    save_json({"batch_id": args.batch, "results": summary, "errors": errors}, str(summary_path))

    # ── Tableau récapitulatif ─────────────────────────────────────────────────
    tradeable = [r for r in summary if r["decision"] != "no_trade" and not r["veto"]]
    abstained = [r for r in summary if r["decision"] == "no_trade" or r["veto"]]

    print(f"{'═' * 62}")
    print(f"  RÉSUMÉ")
    print(f"{'═' * 62}")
    print(f"  Total traités  : {len(summary)}")
    print(f"  Positions      : {len(tradeable)}")
    print(f"  Abstentions    : {len(abstained)}")
    if errors:
        print(f"  Erreurs        : {len(errors)}")

    if tradeable:
        print(f"\n  Positions à ouvrir :")
        for r in sorted(tradeable, key=lambda x: abs(x["adj_edge"]), reverse=True):
            print(f"    {r['side'].upper():<4} {r['label'][:46]:<46} "
                  f"edge {r['adj_edge']:+.1%}  {r['size']} €")

    print(f"\n  ✓ Résultats dans : {results_dir}")
    print(f"  ✓ Résumé         : {summary_path}")
    print(f"{'═' * 62}\n")


if __name__ == "__main__":
    main()