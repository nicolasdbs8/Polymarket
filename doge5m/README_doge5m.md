# DOGE 5m — Module de trading Polymarket

Signaux directionnels UP/DOWN sur les marchés `doge-updown-5m-{timestamp}` de Polymarket.
Résolution toutes les 5 minutes, paris binaires.

Pipeline en construction — basé sur les mêmes principes que le module btc5m.

---

## État du pipeline

| Phase | Statut | Description |
|---|---|---|
| **Phase 0** | **En cours** | Mesure orderbook 5m — friction, slippage, profil de liquidité |
| Phase 1 | — | Modélisation sur bougies 5m (walk-forward, calibration) |
| Phase 2 | — | Signal live + bot d'exécution |
| Phase 3 | — | Fusion optionnelle avec signaux autres assets |

---

## Phase 0 — Orderbook

> Collecte en cours. Objectif : 48h+ de snapshots toutes les 10 min.

```bash
python orderbook_snapshot.py --asset doge --timeframe 5m report
```

---

## Scripts du module (planifiés)

| Script | Rôle | Statut |
|---|---|---|
| `doge5m_phase0.py` | ACF, sélection features, test asymétrie UP/DOWN | À créer |
| `doge5m_data.py` | Téléchargement bougies 5m DOGE | À créer |
| `doge5m_model.py` | Walk-forward, calibration, export | À créer |
| `doge5m_signal.py` | Signal live | À créer |
| `doge5m_bot.py` | Bot d'exécution automatique | À créer |
| `doge5m_pnl.py` | Analyse P&L | À créer |
| `doge5m_kelly.py` | Simulation Kelly | À créer |

---

## Fichiers de sortie

| Fichier | Généré par | Contenu |
|---|---|---|
| `orderbook_log.json` | `orderbook_snapshot.py` | Snapshots orderbook Phase 0 |
| `signal_log.json` | `doge5m_signal.py` (Phase 2) | Journal des signaux avec résultats |
| `execution_log.json` | `doge5m_bot.py` (Phase 2) | Journal des ordres passés |
