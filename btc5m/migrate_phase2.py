#!/usr/bin/env python3
"""
migrate_phase2.py — Migration one-shot : ajoute "phase": "2" à toutes les
entrées de signal_log.json qui n'ont pas encore de champ "phase".

Usage :
    python btc5m/migrate_phase2.py

Sûr à relancer : ne touche pas aux entrées qui ont déjà un champ "phase".
"""

import json
from pathlib import Path

SIGNAL_LOG = Path(__file__).parent / "signal_log.json"


def main():
    if not SIGNAL_LOG.exists():
        print("  Fichier signal_log.json introuvable — rien à faire.")
        return

    with open(SIGNAL_LOG, encoding="utf-8") as f:
        log = json.load(f)

    updated = 0
    for entry in log:
        if "phase" not in entry:
            entry["phase"] = "2"
            updated += 1

    if updated == 0:
        print("  Aucune entrée à migrer (toutes ont déjà un champ 'phase').")
        return

    with open(SIGNAL_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)

    print(f"  Migration terminée : {updated} entrée(s) marquées phase='2'")
    print(f"  Total entrées dans le log : {len(log)}")


if __name__ == "__main__":
    main()
