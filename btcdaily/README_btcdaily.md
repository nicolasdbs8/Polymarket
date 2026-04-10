# BTC daily — Module de trading Polymarket

Signaux directionnels UP/DOWN sur les marchés `btc-updown-daily-{timestamp}` de Polymarket.
Résolution quotidienne, paris binaires.

Pipeline en construction — basé sur les mêmes principes que les modules btc5m et btc15m.

---

## État du pipeline

| Phase | Statut | Description |
|---|---|---|
| **Phase 0** | **En cours** | Mesure orderbook daily — friction, slippage, profil de liquidité sur 24h |
| Phase 1 | — | Modélisation sur bougies daily (walk-forward, calibration) |
| Phase 2 | — | Signal live + bot d'exécution |
| Phase 3 | — | Fusion optionnelle avec signaux intraday |

---

## Phase 0 — Orderbook

> Collecte en cours. Objectif : 48h+ de snapshots toutes les 10 min.

```bash
# Rapport friction / slippage / liquidité + verdict go/no-go
python orderbook_snapshot.py --asset btc --timeframe daily report
```

### Fenêtres temporelles analysées

| Fenêtre | Description |
|---|---|
| 0–2h | Ouverture du marché — liquidité initiale |
| 2–8h | Session creuse (nuit US) |
| 8–16h | Sessions EU + US — pic de volume attendu |
| 16–24h | Approche de la clôture |

---

## Scripts du module (planifiés)

| Script | Rôle | Statut |
|---|---|---|
| `btcdaily_phase0.py` | ACF, sélection features, test asymétrie UP/DOWN | À créer |
| `btcdaily_data.py` | Téléchargement bougies daily | À créer |
| `btcdaily_model.py` | Walk-forward, calibration, export | À créer |
| `btcdaily_signal.py` | Signal live | À créer |
| `btcdaily_bot.py` | Bot d'exécution automatique | À créer |
| `btcdaily_pnl.py` | Analyse P&L | À créer |
| `btcdaily_kelly.py` | Simulation Kelly | À créer |

---

## Fichiers de sortie

| Fichier | Généré par | Contenu |
|---|---|---|
| `orderbook_log.json` | `orderbook_snapshot.py` | Snapshots orderbook Phase 0 |
| `signal_log_daily.json` | `btcdaily_signal.py` (Phase 2) | Journal des signaux avec résultats |
| `execution_log_daily.json` | `btcdaily_bot.py` (Phase 2) | Journal des ordres passés |
