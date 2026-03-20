#!/usr/bin/env python3
"""
journal.py — Gestion du journal de paper trading.

Commandes disponibles :

  Enregistrer une position après le moteur :
    python journal.py add --market MKT-005

  Voir toutes les positions ouvertes :
    python journal.py positions

  Résoudre un marché (post-mortem) :
    python journal.py resolve --market MKT-005 --result yes
    python journal.py resolve --market MKT-005 --result no

  Voir le résumé du portefeuille :
    python journal.py summary

  Voir l'historique complet :
    python journal.py history
"""

import json
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_FILE = "journal.json"
MARKETS_DIR  = "markets"


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def load_journal() -> dict:
    p = Path(JOURNAL_FILE)
    if not p.exists():
        return {
            "created_at": now_iso(),
            "schema_version": "1.0",
            "bankroll_initial": 1000.0,
            "bankroll_current": 1000.0,
            "positions": []
        }
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def save_journal(journal: dict):
    with open(JOURNAL_FILE, "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2, ensure_ascii=False)

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def find_position(journal: dict, market_id: str) -> dict | None:
    for p in journal["positions"]:
        if p["market_id"] == market_id:
            return p
    return None

def sep(char="═", n=62):
    print(char * n)

def col(label: str, value, width=22):
    print(f"  {label:<{width}}: {value}")


# ─────────────────────────────────────────────────────────────────────────────
# Commande : add
# ─────────────────────────────────────────────────────────────────────────────

def cmd_add(market_id: str):
    """
    Lit engine_output.json + market_request.json du marché
    et ajoute une entrée dans le journal.
    """
    market_dir = Path(MARKETS_DIR) / market_id
    output_path  = market_dir / "engine_output.json"
    request_path = market_dir / "market_request.json"

    for p in [output_path, request_path]:
        if not p.exists():
            print(f"[ERREUR] Fichier introuvable : {p}", file=sys.stderr)
            sys.exit(1)

    engine  = load_json(str(output_path))
    request = load_json(str(request_path))
    journal = load_journal()

    # Vérifie si déjà enregistré
    if find_position(journal, market_id):
        print(f"[AVERTISSEMENT] {market_id} est déjà dans le journal.")
        print("  Utilisez 'resolve' pour clôturer cette position.")
        sys.exit(0)

    decision = engine["decision"]["decision"]

    # Pas de position si no_trade
    if decision == "no_trade":
        entry = {
            "market_id":             market_id,
            "market_title":          request.get("market_title", ""),
            "category":              request.get("category", ""),
            "resolution_date":       request.get("resolution_date", ""),
            "added_at":              now_iso(),
            "market_probability":    engine["inputs_summary"]["market_probability_yes"],
            "p_estimated":           engine["probability"]["p_estimated"],
            "uncertainty_low":       engine["probability"]["uncertainty_low"],
            "uncertainty_high":      engine["probability"]["uncertainty_high"],
            "raw_edge":              engine["edge"]["raw_edge"],
            "adjusted_edge":         engine["edge"]["adjusted_edge"],
            "confidence_overall":    engine["inputs_summary"]["confidence_overall"],
            "decision":              "no_trade",
            "position_side":         "none",
            "paper_position_size":   0.0,
            "market_url":            request.get("market_url", ""),
            "status":                "no_trade",
            "final_result":          None,
            "paper_pnl":             0.0,
            "post_mortem":           None,
        }
        journal["positions"].append(entry)
        save_journal(journal)
        sep()
        print(f"  JOURNAL — {market_id} ajouté (NO TRADE)")
        sep()
        col("Titre",      entry["market_title"][:45])
        col("Décision",   "NO TRADE — abstention")
        col("Raison",     f"edge ajusté {entry['adjusted_edge']:+.1%} insuffisant")
        sep()
        return

    # Position ouverte
    side = engine["decision"]["position_side"]
    size = engine["decision"]["paper_position_size"]

    # Calcul du gain potentiel
    if side == "no":
        market_prob_no   = 1 - engine["inputs_summary"]["market_probability_yes"]
        potential_gain   = round(size / market_prob_no * engine["inputs_summary"]["market_probability_yes"], 2)
        potential_return = round(engine["inputs_summary"]["market_probability_yes"] / market_prob_no * 100, 1)
    else:
        market_prob_yes  = engine["inputs_summary"]["market_probability_yes"]
        potential_gain   = round(size / market_prob_yes * (1 - market_prob_yes), 2)
        potential_return = round((1 - market_prob_yes) / market_prob_yes * 100, 1)

    entry = {
        "market_id":             market_id,
        "market_title":          request.get("market_title", ""),
        "category":              request.get("category", ""),
        "resolution_date":       request.get("resolution_date", ""),
        "added_at":              now_iso(),
        "market_probability":    engine["inputs_summary"]["market_probability_yes"],
        "p_estimated":           engine["probability"]["p_estimated"],
        "uncertainty_low":       engine["probability"]["uncertainty_low"],
        "uncertainty_high":      engine["probability"]["uncertainty_high"],
        "raw_edge":              engine["edge"]["raw_edge"],
        "adjusted_edge":         engine["edge"]["adjusted_edge"],
        "confidence_overall":    engine["inputs_summary"]["confidence_overall"],
        "decision":              decision,
        "position_side":         side,
        "paper_position_size":   size,
        "potential_gain":        potential_gain,
        "potential_return_pct":  potential_return,
        "market_url":            request.get("market_url", ""),
        "status":                "open",
        "final_result":          None,
        "paper_pnl":             None,
        "post_mortem":           None,
    }

    # Débite la bankroll
    journal["bankroll_current"] = round(journal["bankroll_current"] - size, 2)
    journal["positions"].append(entry)
    save_journal(journal)

    sep()
    print(f"  JOURNAL — {market_id} ajouté")
    sep()
    col("Titre",           entry["market_title"][:45])
    col("Position",        f"{side.upper()} — {decision}")
    col("Mise paper",      f"{size} €")
    col("Gain potentiel",  f"+{potential_gain} € ({potential_return}%)")
    col("Bankroll restante", f"{journal['bankroll_current']} €")
    col("Résolution",      entry["resolution_date"][:10])
    sep()
    print(f"  Position enregistrée dans {JOURNAL_FILE}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Commande : positions
# ─────────────────────────────────────────────────────────────────────────────

def cmd_positions():
    """Affiche toutes les positions ouvertes."""
    journal = load_journal()
    open_pos = [p for p in journal["positions"] if p["status"] == "open"]

    sep()
    print(f"  POSITIONS OUVERTES ({len(open_pos)})")
    sep()

    if not open_pos:
        print("  Aucune position ouverte.\n")
        return

    total_exposed = sum(p["paper_position_size"] for p in open_pos)
    total_potential = sum(p.get("potential_gain", 0) for p in open_pos)

    for p in open_pos:
        side  = p["position_side"].upper()
        size  = p["paper_position_size"]
        gain  = p.get("potential_gain", 0)
        ret   = p.get("potential_return_pct", 0)
        edge  = p["adjusted_edge"]
        mprob = p["market_probability"]
        pest  = p["p_estimated"]
        title = p["market_title"][:48]
        resol = p["resolution_date"][:10]

        print(f"\n  [{p['market_id']}] {title}")
        print(f"  {'─' * 55}")
        col("Côté",          f"{side}")
        col("Mise",          f"{size} €  →  gain potentiel +{gain} € ({ret}%)")
        col("Marché / estimé", f"{mprob:.0%} / {pest:.0%}  (edge ajusté {edge:+.1%})")
        col("Résolution",    resol)

    sep("─")
    col("Total exposé",      f"{total_exposed} €")
    col("Gain potentiel max", f"+{total_potential} €")
    col("Bankroll disponible", f"{journal['bankroll_current']} €")
    sep()
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Commande : resolve
# ─────────────────────────────────────────────────────────────────────────────

def cmd_resolve(market_id: str, result: str, error_type: str,
                lesson: str, process_fix: str):
    """
    Clôture une position après résolution du marché.
    Calcule le PnL paper et enregistre le post-mortem.
    """
    if result not in ("yes", "no", "cancelled", "disputed"):
        print(f"[ERREUR] --result doit être : yes / no / cancelled / disputed", file=sys.stderr)
        sys.exit(1)

    journal = load_journal()
    pos = find_position(journal, market_id)

    if not pos:
        print(f"[ERREUR] {market_id} introuvable dans le journal.", file=sys.stderr)
        sys.exit(1)

    if pos["status"] in ("resolved", "no_trade"):
        print(f"[AVERTISSEMENT] {market_id} est déjà clôturé (status: {pos['status']}).")
        sys.exit(0)

    side   = pos["position_side"]
    size   = pos["paper_position_size"]
    mprob  = pos["market_probability"]

    # ── Calcul du PnL ─────────────────────────────────────────────────────────
    # Sur Polymarket : si tu achètes NO à (1-p), tu récupères 1.0 si NO résout
    # Si tu achètes YES à p, tu récupères 1.0 si YES résout
    # Mise = size euros de parts, donc nb_parts = size / prix_achat

    if result == "cancelled" or result == "disputed":
        pnl = 0.0
        outcome = "remboursé"
    elif side == "no":
        price_no = round(1 - mprob, 4)
        nb_parts = size / price_no if price_no > 0 else 0
        if result == "no":
            pnl = round(nb_parts * 1.0 - size, 2)   # gain
        else:
            pnl = round(-size, 2)                    # perte totale
        outcome = "gagné" if pnl > 0 else "perdu"
    else:  # side == "yes"
        price_yes = mprob
        nb_parts  = size / price_yes if price_yes > 0 else 0
        if result == "yes":
            pnl = round(nb_parts * 1.0 - size, 2)
        else:
            pnl = round(-size, 2)
        outcome = "gagné" if pnl > 0 else "perdu"

    # ── Mise à jour de la bankroll ────────────────────────────────────────────
    if result in ("cancelled", "disputed"):
        journal["bankroll_current"] = round(journal["bankroll_current"] + size, 2)
    elif pnl > 0:
        journal["bankroll_current"] = round(journal["bankroll_current"] + size + pnl, 2)
    else:
        pass  # mise déjà débitée, perte = ne rien remettre

    # ── Post-mortem ───────────────────────────────────────────────────────────
    valid_error_types = [
        "base_rate_error", "factor_weighting_error", "prerequisite_misread",
        "missing_information", "resolution_ambiguity",
        "correct_analysis", "correct_abstention"
    ]
    if error_type and error_type not in valid_error_types:
        print(f"[AVERTISSEMENT] error_type '{error_type}' non standard. Valeurs recommandées :")
        for et in valid_error_types:
            print(f"  {et}")

    post_mortem = {
        "post_mortem_id":    f"{market_id}-PM",
        "resolved_at":       now_iso(),
        "final_result":      result,
        "paper_pnl":         pnl,
        "error_type":        error_type or "non_renseigné",
        "main_lesson":       lesson or "",
        "process_fix":       process_fix or "",
    }

    pos["status"]       = "resolved"
    pos["final_result"] = result
    pos["paper_pnl"]    = pnl
    pos["post_mortem"]  = post_mortem

    save_journal(journal)

    # ── Affichage ─────────────────────────────────────────────────────────────
    sep()
    print(f"  RÉSOLUTION — {market_id}")
    sep()
    col("Titre",       pos["market_title"][:45])
    col("Résultat",    result.upper())
    col("Position",    f"{side.upper()} à {mprob:.0%}")
    col("Mise",        f"{size} €")
    col("PnL paper",   f"{pnl:+.2f} €  ({outcome})")
    col("Bankroll",    f"{journal['bankroll_current']} €")
    if lesson:
        col("Leçon",   lesson[:50])
    sep()
    print(f"  Post-mortem enregistré dans {JOURNAL_FILE}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Commande : summary
# ─────────────────────────────────────────────────────────────────────────────

def cmd_summary():
    """Affiche le résumé global du portefeuille."""
    journal = load_journal()
    positions = journal["positions"]

    resolved   = [p for p in positions if p["status"] == "resolved"]
    open_pos   = [p for p in positions if p["status"] == "open"]
    no_trades  = [p for p in positions if p["status"] == "no_trade"]

    won   = [p for p in resolved if p.get("paper_pnl", 0) > 0]
    lost  = [p for p in resolved if p.get("paper_pnl", 0) < 0]
    flat  = [p for p in resolved if p.get("paper_pnl", 0) == 0]

    total_pnl      = sum(p.get("paper_pnl", 0) for p in resolved)
    bankroll_init  = journal["bankroll_initial"]
    bankroll_curr  = journal["bankroll_current"]
    total_return   = round((bankroll_curr - bankroll_init) / bankroll_init * 100, 2)
    total_exposed  = sum(p["paper_position_size"] for p in open_pos)

    win_rate = round(len(won) / len(resolved) * 100, 1) if resolved else 0

    sep()
    print("  RÉSUMÉ DU PORTEFEUILLE")
    sep()
    col("Bankroll initiale",   f"{bankroll_init} €")
    col("Bankroll actuelle",   f"{bankroll_curr} €")
    col("Performance totale",  f"{total_return:+.2f}%  ({total_pnl:+.2f} €)")
    sep("─")
    col("Marchés analysés",    len(positions))
    col("Positions ouvertes",  len(open_pos))
    col("Positions résolues",  len(resolved))
    col("Abstentions",         len(no_trades))
    col("Capital exposé",      f"{total_exposed} €")
    sep("─")

    if resolved:
        col("Taux de réussite",   f"{win_rate}%  ({len(won)}W / {len(lost)}L / {len(flat)}=)")
        col("PnL réalisé",        f"{total_pnl:+.2f} €")

        # Performance par catégorie
        categories = {}
        for p in resolved:
            cat = p.get("category", "other")
            if cat not in categories:
                categories[cat] = {"count": 0, "pnl": 0.0, "wins": 0}
            categories[cat]["count"] += 1
            categories[cat]["pnl"]   += p.get("paper_pnl", 0)
            if p.get("paper_pnl", 0) > 0:
                categories[cat]["wins"] += 1

        if len(categories) > 1:
            sep("─")
            print("  Performance par catégorie :")
            for cat, stats in sorted(categories.items(), key=lambda x: -x[1]["pnl"]):
                wr = round(stats["wins"] / stats["count"] * 100)
                print(f"    {cat:<25} {stats['count']} trades  "
                      f"PnL {stats['pnl']:+.2f} €  WR {wr}%")
    else:
        print("  Aucune position résolue pour l'instant.")

    sep()
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Commande : history
# ─────────────────────────────────────────────────────────────────────────────

def cmd_history():
    """Affiche l'historique complet de toutes les positions."""
    journal = load_journal()
    positions = journal["positions"]

    if not positions:
        print("  Aucune position enregistrée.\n")
        return

    sep()
    print(f"  HISTORIQUE COMPLET ({len(positions)} entrées)")
    sep()

    status_icons = {
        "open":     "🔵 OUVERT  ",
        "resolved": "",
        "no_trade": "⚪ NO TRADE",
    }

    for p in positions:
        status = p["status"]
        icon   = status_icons.get(status, status)

        if status == "resolved":
            pnl  = p.get("paper_pnl", 0)
            icon = "🟢 GAGNÉ   " if pnl > 0 else ("🔴 PERDU   " if pnl < 0 else "⚪ FLAT    ")

        title  = p["market_title"][:48]
        side   = p["position_side"].upper() if p["position_side"] != "none" else "—"
        size   = p["paper_position_size"]
        resol  = p["resolution_date"][:10] if p.get("resolution_date") else "?"
        pnl_str = f"{p['paper_pnl']:+.2f} €" if p.get("paper_pnl") is not None else "en cours"

        print(f"\n  {icon}  [{p['market_id']}] {title}")
        print(f"           {side:<6} {size} €  →  {pnl_str}  (résolution: {resol})")

        if status == "resolved" and p.get("post_mortem"):
            pm = p["post_mortem"]
            if pm.get("main_lesson"):
                print(f"           Leçon : {pm['main_lesson'][:60]}")

    sep()
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Interface CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Journal de paper trading.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python journal.py add --market MKT-005
  python journal.py positions
  python journal.py resolve --market MKT-005 --result no --error-type correct_analysis --lesson "Base rate solide, freins bien pondérés"
  python journal.py summary
  python journal.py history
        """
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # add
    p_add = sub.add_parser("add", help="Enregistre une position depuis engine_output.json")
    p_add.add_argument("--market", required=True, help="Identifiant du marché (ex: MKT-005)")

    # positions
    sub.add_parser("positions", help="Affiche les positions ouvertes")

    # resolve
    p_res = sub.add_parser("resolve", help="Clôture un marché après résolution")
    p_res.add_argument("--market",      required=True, help="Identifiant du marché")
    p_res.add_argument("--result",      required=True,
                       choices=["yes", "no", "cancelled", "disputed"],
                       help="Résultat final du marché")
    p_res.add_argument("--error-type",  default="",
                       help="Type d'erreur ou de réussite (voir protocole section 17.2)")
    p_res.add_argument("--lesson",      default="",
                       help="Leçon principale du post-mortem")
    p_res.add_argument("--process-fix", default="",
                       help="Correctif de processus proposé")

    # summary
    sub.add_parser("summary", help="Résumé global du portefeuille")

    # history
    sub.add_parser("history", help="Historique complet")

    args = parser.parse_args()

    if args.command == "add":
        cmd_add(args.market)
    elif args.command == "positions":
        cmd_positions()
    elif args.command == "resolve":
        cmd_resolve(
            args.market,
            args.result,
            args.error_type,
            args.lesson,
            args.process_fix,
        )
    elif args.command == "summary":
        cmd_summary()
    elif args.command == "history":
        cmd_history()


if __name__ == "__main__":
    main()
