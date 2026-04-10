# XRP 15m — Module de trading Polymarket

Signaux directionnels UP/DOWN sur les marchés `xrp-updown-15m-{timestamp}` de Polymarket.
Résolution toutes les 15 minutes, paris binaires.

Pipeline en construction — basé sur les mêmes principes que le module btc15m.

---

## État du pipeline

| Phase | Statut | Description |
|---|---|---|
| **Phase 0** | **En cours** | Mesure orderbook 15m — friction, slippage, profil de liquidité |
| Phase 1 | — | Modélisation sur bougies 15m (walk-forward, calibration) |
| Phase 2 | — | Signal live + bot d'exécution |
| Phase 3 | — | Fusion optionnelle avec signaux XRP 5m / autres assets |

---

## Phase 0 — Orderbook

> Collecte en cours. Objectif : 48h+ de collecte.

```bash
python orderbook_snapshot.py --asset xrp --timeframe 15m report
```

---

## Scripts du module (planifiés)

| Script | Rôle | Statut |
|---|---|---|
| `xrp15m_phase0.py` | ACF, sélection features, test asymétrie UP/DOWN | À créer |
| `xrp15m_data.py` | Téléchargement bougies 15m XRP | À créer |
| `xrp15m_model.py` | Walk-forward, calibration, export | À créer |
| `xrp15m_signal.py` | Signal live | À créer |
| `xrp15m_bot.py` | Bot d'exécution automatique | À créer |
| `xrp15m_pnl.py` | Analyse P&L | À créer |
| `xrp15m_kelly.py` | Simulation Kelly | À créer |

---

## Fichiers de sortie

| Fichier | Généré par | Contenu |
|---|---|---|
| `orderbook_log.json` | `orderbook_snapshot.py` | Snapshots orderbook Phase 0 |
| `signal_log.json` | `xrp15m_signal.py` (Phase 2) | Journal des signaux avec résultats |
| `execution_log.json` | `xrp15m_bot.py` (Phase 2) | Journal des ordres passés |
