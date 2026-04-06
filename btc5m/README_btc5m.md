# BTC 5m — Module de trading Polymarket

Signaux directionnels UP/DOWN sur les marchés `btc-updown-5m-{timestamp}` de Polymarket.
Résolution toutes les 5 minutes, paris binaires.

---

## Scripts du module

| Script | Rôle |
|---|---|
| `btc5m_signal.py` | Générateur de signaux live + résolution automatique des outcomes |
| `btc5m_pnl.py` | Analyse P&L nette de frais par segment |
| `btc5m_projection.py` | Simulation portefeuille avec sizing + projections forward |
| `btc5m_kelly.py` | Simulation Kelly fractionnaire sur tous les trades |
| `btc5m_kelly_filtered.py` | Simulation Kelly sur trades filtrés (edge>3% & 10h–22h) |
| `btc5m_backtest.py` | Backtest sur données historiques |
| `btc5m_model.py` | Entraînement du modèle directionnel |
| `btc5m_features.py` | Construction des features (momentum, position, MA) |
| `btc5m_data.py` | Téléchargement des données OHLCV BTC |
| `btc5m_explore.py` | Exploration et diagnostic des données |
| `btc5m_phase0.py` | Calibration initiale (phase 0) |

---

## Commandes principales

### Signal live (boucle toutes les 5 min)
```bash
python btc5m/btc5m_signal.py
```

### Mesurer la friction réelle (spread Polymarket)
```bash
python btc5m/btc5m_signal.py friction
```

### Voir le journal et les stats live
```bash
python btc5m/btc5m_signal.py log
```

### Mettre à jour les graphiques P&L et projection
```bash
python btc5m/btc5m_pnl.py
python btc5m/btc5m_projection.py
```
Les deux scripts lisent `signal_log.json` à chaque exécution — **relancer suffit** après accumulation de nouveaux trades. Les PNG sont automatiquement écrasés dans `btc5m/`.

---

## Fichiers de sortie

| Fichier | Généré par | Contenu |
|---|---|---|
| `signal_log.json` | `btc5m_signal.py` | Journal de tous les signaux avec résultats |
| `btc5m_pnl.png` | `btc5m_pnl.py` | P&L cumulatif net de frais par seuil d'edge |
| `btc5m_projection.png` | `btc5m_projection.py` | Simulation portefeuille + courbes de projection |
| `btc5m_kelly_individual.png` | `btc5m_kelly.py` | 4 courbes Kelly sur tous les trades |
| `btc5m_kelly_combined.png` | `btc5m_kelly.py` | 4 stratégies superposées — tous les trades |
| `kelly_results.json` | `btc5m_kelly.py` | Métriques + equity curves Kelly (tous trades) |
| `btc5m_kelly_filtered_individual.png` | `btc5m_kelly_filtered.py` | 4 courbes Kelly sur trades filtrés |
| `btc5m_kelly_filtered_combined.png` | `btc5m_kelly_filtered.py` | 4 stratégies superposées — trades filtrés |
| `kelly_filtered_results.json` | `btc5m_kelly_filtered.py` | Métriques + equity curves Kelly (filtrés) |

---

## Logique de signal (`btc5m_signal.py`)

```
1. Modèle prédit P(UP) et P(DOWN)
2. edge_brut  = |P(direction) - 0.50|
3. edge_net   = edge_brut - friction  (spread/2 Polymarket)
4. Si edge_net <= 0 → signal absorbé, pas de trade
5. Si edge_net < 3% → SIGNAL "PETIT"   (mise conseillée : 2% du portefeuille)
6. Si edge_net ≥ 3% → SIGNAL "STANDARD" (mise conseillée : 5% du portefeuille)
```

Fenêtre de trading active : **10h–22h UTC** (phase 2b)
Seuil minimum : `EDGE_THRESHOLD = 0.02`

---

## Structure de `signal_log.json`

```json
{
  "ts":           "2026-03-20T19:21:35Z",
  "btc_price":    69930.9,
  "direction":    "DOWN",
  "decision":     "SIGNAL DOWN — PETIT",
  "edge_net":     0.0228,
  "raw_edge":     0.0228,
  "pm_slug":      "btc-updown-5m-1774034400",
  "pm_up":        0.505,
  "pm_down":      0.495,
  "pm_mins_left": 3.4,
  "result":       "down",
  "phase":        "2"
}
```

**Champs clés pour l'analyse P&L :**
- `direction` + `pm_up`/`pm_down` → prix d'entrée effectif
- `edge_net` → seuil de filtrage
- `result` → outcome réel (présent = trade résolu)
- `decision` → contient "PETIT" ou "STANDARD" (détermine le sizing)

---

## Calcul des frais (taker fee dynamique Polymarket)

Effectif à partir du 30 mars 2026 sur les marchés crypto 5m :

```
fee = p × 0.018 × (4 × p × (1-p))
```

- `p` = prix d'entrée (pm_up si direction=UP, pm_down si direction=DOWN)
- Peak à **1.80%** pour p = 0.50
- Décroît vers 0% aux extrêmes (p→0 ou p→1)

---

## Résultats observés (505 trades — 2026-03-20 → 2026-04-06)

> Snapshot `btc5m_pnl.py` + `btc5m_projection.py` + `btc5m_kelly_filtered.py` — 2026-04-06.
> Note : 15 signaux suspects historiques (bug `abs()` corrigé le 2026-04-06) légèrement surestiment les stats STANDARD.

### Win rate et P&L net de frais — 505 trades

| Segment | N | Win% | P&L net | ROI/trade | Sharpe |
|---|---|---|---|---|---|
| Global | 505 | 55.0% | +24.9 | +4.9% | 4.24 |
| **10h–22h UTC** | **281** | **59.4%** | **+37.0** | **+13.2%** | **9.46** |
| Hors fenêtre | 224 | 49.6% | -12.1 | -5.4% | — |
| **10h–22h UP seul** | **113** | **68.1%** | **+34.0** | **+30.1%** | **14.03** |
| 10h–22h DOWN seul | 168 | 53.6% | +3.1 | +1.8% | 1.48 |
| edge>3% + 10h–22h | 39 | 56.4% | +2.6 | +6.6% | 1.99 |
| edge>4% + 10h–22h | 18 | 61.1% | +2.4 | +13.2% | 2.69 |
| edge>5% + 10h–22h | 14 | 64.3% | +2.7 | +19.5% | — |

> Le signal UP 10h–22h est le moteur principal de la performance (Sharpe 14, win rate 68.1%).
> Le signal DOWN 10h–22h reste rentable mais marginal (win rate 53.6%, ROI +1.8%/trade).
> Le fort edge seul (>3%, >4%) ne produit pas d'alpha sans la fenêtre horaire.

### Simulation portefeuille (base 100 USDC, sizing PETIT=2% / STANDARD=5%)

| Segment | N | Final | ROI | MaxDD | Sharpe |
|---|---|---|---|---|---|
| Global (tous) | 505 | 141.68$ | +41.7% | -57.68$ | 4.24 |
| **10h–22h UTC** | **281** | **212.90$** | **+112.9%** | -37.52$ | **9.46** |
| **10h–22h UP seul** | **113** | **202.99$** | **+103.0%** | -15.32$ | **14.03** |
| 10h–22h DOWN seul | 168 | 104.88$ | +4.9% | -40.85$ | 1.48 |
| edge>3% + 10h–22h | 39 | 108.56$ | +8.6% | -40.41$ | 1.99 |
| edge>4% + 10h–22h | 18 | 110.25$ | +10.2% | -18.90$ | 2.69 |

### Filtre edge>3% + 10h–22h — 39 trades (`btc5m_kelly_filtered.py`, 2026-04-06)

| Stratégie | Final | ROI | MaxDD | Sharpe |
|---|---|---|---|---|
| **Fixe 5% (STANDARD)** | **112.05$** | **+12.1%** | 27.5% | **1.34** |
| Kelly/4 statique | 89.36$ | -10.6% | 49.4% | 0.16 |
| Kelly/4 dynamique | 102.74$ | +2.7% | 31.9% | 0.64 |
| Kelly/4 incertitude | 107.14$ | +7.1% | 33.2% | 0.95 |

> Sharpe en baisse vs snapshot précédent (3.0 → 1.34) avec 39 vs 26 trades — le segment se normalise.
> Sizing fixe 5% reste le meilleur. Kelly sur-mise toujours — revoir à 100+ trades filtrés.

### Projections forward — 10h–22h UP seul (sizing réel, compound)

| Horizon | Portefeuille | ROI total |
|---|---|---|
| 1 semaine | ~141 | +40.6% |
| 2 semaines | ~199 | +98.9% |
| 1 mois | ~436 | +336% |

> Basé sur ROI/trade +30.1% observé sur 113 trades (10h–22h UP). Ordre de grandeur indicatif.
> À actualiser via `btc5m_projection.py`.

---

## Tension fenêtre × seuil d'edge

|  | edge>2% | edge>3% | edge>4% | edge>5% |
|---|---|---|---|---|
| 10h–22h | 107 tr · 58% · +0.123 | 39 tr · 56% · +0.066 | 18 tr · 61% · +0.132 | 14 tr · 64% · +0.195 |
| Hors fenêtre | 74 tr · 47% · -0.078 | 34 tr · 47% · -0.085 | 16 tr · 44% · -0.150 | 11 tr · 36% · -0.314 |

Format : `N trades · win rate · ROI/trade net`

Le fort edge seul ne suffit pas — c'est la fenêtre 10h–22h qui porte l'alpha.
Hors fenêtre, même edge>5% est négatif (-31.4% ROI/trade).

---

## Phases du modèle

| Phase | Description |
|---|---|
| `2` | Phase courante (edge_threshold=0.02, fenêtre libre) |
| `2b` | Phase 2 avec filtre fenêtre 10h–22h UTC activé |

---

## Simulation Kelly fractionnaire (rétrospective)

> **⚠ SIMULATION RÉTROSPECTIVE — résultats in-sample uniquement.**
> Ne préjugent pas des performances futures.

### Tous les trades (272 trades, win rate 55.1%)

```bash
python btc5m/btc5m_kelly.py
```

| Stratégie | Final | ROI | MaxDD | Sharpe | Mise moy |
|---|---|---|---|---|---|
| A — Fixe actuel (PETIT 2% / STANDARD 5%) | 142.39$ | +42.4% | 28.3% | 0.988 | 2.5% |
| B — Kelly/4 statique (warmup 50 trades) | 117.27$ | +17.3% | 15.2% | 0.870 | 1.2% |
| C — Kelly/4 dynamique (fenêtre 50, recalc/20) | 143.92$ | +43.9% | 34.0% | 0.946 | 2.5% |
| D — Kelly/4 ajusté incertitude | 113.51$ | +13.5% | 22.7% | 0.590 | 1.5% |

### Filtre edge>3% & 10h–22h UTC (26 trades, win rate 61.5%)

```bash
python btc5m/btc5m_kelly_filtered.py
```

| Stratégie | Final | ROI | MaxDD | Sharpe |
|---|---|---|---|---|
| A — Fixe 5% (STANDARD uniquement) | 122.95$ | +22.9% | 15.9% | **3.035** |
| B — Kelly/4 statique (warmup 13) | 111.17$ | +11.2% | 27.1% | 1.453 |
| C — Kelly/4 dynamique (fenêtre 13) | 115.58$ | +15.6% | 25.8% | 1.922 |
| D — Kelly/4 incertitude | 117.72$ | +17.7% | 27.7% | 1.966 |

**Conclusions :**
- Sur l'ensemble des trades, le sizing fixe actuel est déjà quasi-optimal (ROI +42.4%, Sharpe 0.99).
- Sur le segment filtré, le Sharpe de la stratégie A monte à **3.0** — signe que le sizing 5% STANDARD
  est bien calibré pour ce segment.
- Kelly dynamique et statique sur-misent légèrement sur 26 trades (variance élevée, win rate 61.5%
  donne f/4 ≈ 5.75%), ce qui dégrade le MaxDD sans améliorer le ROI.
- **Conclusion opérationnelle** : le sizing actuel (PETIT 2% / STANDARD 5%) est conservatoire et sain.
  Revoir le Kelly une fois 100+ trades filtrés accumulés.

---

## Paramètres configurables

Dans `btc5m_signal.py` :
```python
EDGE_THRESHOLD   = 0.02       # edge minimum pour émettre un signal
DEFAULT_FRICTION = 0.005      # spread/2 estimé (mettre à jour avec 'friction')
TRADE_WINDOW_UTC = (10, 22)   # fenêtre active en heures UTC
```

Dans `btc5m_projection.py` :
```python
PORTFOLIO_INIT  = 100.0   # capital de départ en USDC
SIZE_SMALL      = 0.02    # 2% — mise pour signaux PETIT  (edge_net < 3%)
SIZE_STANDARD   = 0.05    # 5% — mise pour signaux STANDARD (edge_net ≥ 3%)
```
