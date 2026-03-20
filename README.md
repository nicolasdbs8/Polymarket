# Système de Paper Trading — Guide complet

**Version du moteur : 1.3 | Protocole analytique : 2.3**

---

## Table des matières

1. [Vue d'ensemble](#1-vue-densemble)
2. [Structure du projet](#2-structure-du-projet)
3. [Prérequis et installation](#3-prérequis-et-installation)
4. [Workflow complet](#4-workflow-complet)
5. [Scanner les marchés avec polymarket_scan.py](#5-scanner-les-marchés-avec-polymarket_scanpy)
6. [Générer le market_request avec fetch_market.py](#6-générer-le-market_request-avec-fetch_marketpy)
7. [Obtenir l'analysis_payload avec ChatGPT](#7-obtenir-lanalysis_payload-avec-chatgpt)
8. [Lancer le moteur](#8-lancer-le-moteur)
9. [Tenir le journal](#9-tenir-le-journal)
10. [Workflow multi-contrats batch](#10-workflow-multi-contrats-batch)
11. [Lire l'engine_output](#11-lire-lengine_output)
12. [Ce que le moteur calcule exactement](#12-ce-que-le-moteur-calcule-exactement)
13. [Marchés compatibles et incompatibles](#13-marchés-compatibles-et-incompatibles)
14. [Erreurs fréquentes](#14-erreurs-fréquentes)
15. [Modifier les paramètres](#15-modifier-les-paramètres)
16. [Prompt complet pour ChatGPT](#16-prompt-complet-pour-chatgpt)

---

## 1. Vue d'ensemble

Ce système est un moteur de **paper trading sur marchés prédictifs** (Polymarket).
Il ne joue pas d'argent réel. Il simule des décisions d'investissement pour tester une méthode analytique.

### Pipeline complet

```
polymarket_scan.py        ← trouve les marchés intéressants
        │
        ▼
fetch_market.py           ← génère le market_request depuis l'URL
        │
        ▼
ChatGPT                   ← produit l'analysis_payload (analyse structurée)
        │
        ▼
moteur.py                 ← calcule l'edge et la décision
        │
        ▼
journal.py                ← enregistre la position
```

### Workflow batch (multi-contrats)

```
polymarket_scan.py  →  batch_fetch.py  →  ChatGPT  →  batch_engine.py
```

### Séparation des rôles

| Qui                    | Fait quoi                                                    |
| ---------------------- | ------------------------------------------------------------ |
| **polymarket_scan.py** | Scanne Polymarket, filtre, classe par score d'intérêt        |
| **fetch_market.py**    | Récupère les données d'un marché via l'API                   |
| **ChatGPT**            | Recherche sur internet, produit les payloads analytiques     |
| **moteur.py**          | Calcule probabilités, edge, décision — aucune interprétation |
| **journal.py**         | Tient le journal des positions et post-mortems               |

---

## 2. Structure du projet

```
paper-trading/
│
├── polymarket_scan.py             ← scanner de marchés
├── fetch_market.py                ← génère market_request.json depuis une URL
├── moteur.py                      ← moteur de calcul
├── batch_fetch.py                 ← génère une demande d'analyse groupée
├── batch_engine.py                ← lance le moteur sur un batch complet
├── journal.py                     ← tient le journal des positions
│
├── schemas/
│   ├── market_request.schema.json
│   ├── analysis_payload.schema.json
│   └── system_config.json
│
├── markets/
│   └── MKT-00X/
│       ├── market_request.json
│       ├── analysis_payload.json
│       └── engine_output.json
│
├── batches/
│   └── BATCH-001/
│       ├── batch_request.json
│       ├── chatgpt_prompt.txt
│       ├── batch_payload.json
│       └── results/
│           ├── batch_summary.json
│           └── BATCH-001-XX/
│               └── engine_output.json
│
└── journal.json                   ← créé automatiquement au premier journal.py add
```

---

## 3. Prérequis et installation

Python 3.11+ requis : https://www.python.org/downloads/
Aucune bibliothèque externe. Pas de `pip install`.

VSCode : https://code.visualstudio.com
Extensions : **Python** (Microsoft), **Pylance**, **JSON**.

```powershell
cd C:\chemin\vers\paper-trading
code .
```

Terminal VSCode : Terminal → New Terminal.
Toutes les commandes depuis la racine du projet.

---

## 4. Workflow complet

### Marché unique

```powershell
# 1. Scanner pour trouver un marché
python polymarket_scan.py

# 2. Récupérer le marché (copie l'URL depuis le scan)
python fetch_market.py --url "https://polymarket.com/event/..." --id MKT-00X

# 3. Envoyer market_request.json à ChatGPT → placer analysis_payload.json dans markets/MKT-00X/

# 4. Lancer le moteur
python moteur.py --payload markets/MKT-00X/analysis_payload.json

# 5. Enregistrer dans le journal
python journal.py add --market MKT-00X
```

### Batch (multi-contrats)

```powershell
python batch_fetch.py --url "https://polymarket.com/event/..." --id BATCH-001
# → Colle batches/BATCH-001/chatgpt_prompt.txt dans ChatGPT
# → Sauvegarde la réponse dans batches/BATCH-001/batch_payload.json
python batch_engine.py --batch BATCH-001
```

---

## 5. Scanner les marchés avec polymarket_scan.py

Récupère les marchés actifs de Polymarket, filtre les inanalysables,
classe par score d'intérêt.

### Commandes

```powershell
# Scan général
python polymarket_scan.py

# Marchés qui se résolvent cette semaine
python polymarket_scan.py --max-days 7

# Ce mois uniquement
python polymarket_scan.py --max-days 30

# Intervalle personnalisé (2 semaines à 3 mois)
python polymarket_scan.py --min-days 14 --max-days 90

# Par catégorie
python polymarket_scan.py --category geopolitics
python polymarket_scan.py --category macroeconomics
python polymarket_scan.py --category electoral_politics

# Marchés liquides uniquement
python polymarket_scan.py --min-volume 50000

# Top 10 avec export
python polymarket_scan.py --top 10 --export scans/scan_2026-03-19.json

# Combinaisons
python polymarket_scan.py --category geopolitics --max-days 60 --min-volume 20000
```

### Paramètres

| Paramètre      | Défaut | Description                                                                                 |
| -------------- | ------ | ------------------------------------------------------------------------------------------- |
| `--category`   | tous   | `electoral_politics` `geopolitics` `macroeconomics` `institutions_justice` `tech_companies` |
| `--min-price`  | 0.15   | Prix YES minimum                                                                            |
| `--max-price`  | 0.75   | Prix YES maximum                                                                            |
| `--min-volume` | 5000   | Volume minimum en $                                                                         |
| `--min-days`   | 7      | Horizon minimum en jours                                                                    |
| `--max-days`   | 365    | Horizon maximum en jours                                                                    |
| `--top`        | 25     | Résultats affichés                                                                          |
| `--scan-limit` | 500    | Événements scannés                                                                          |
| `--export`     | —      | Export JSON des résultats                                                                   |

### Ce que le score mesure

| Composante         | Poids  | Logique                         |
| ------------------ | ------ | ------------------------------- |
| Prix proche de 50% | 40 pts | Zone d'incertitude maximale     |
| Volume (log)       | 30 pts | Marché liquide = pricing fiable |
| Horizon 30–120j    | 20 pts | Ni trop court ni trop long      |
| Spread serré       | 10 pts | Liquidité relative              |

### Ce que le scanner filtre

Exclus automatiquement : marchés O/U sportifs, résultats d'équipes, marchés "before GTA VI",
marchés mèmes, candidats anonymisés, prix cibles crypto/commodités, seuils de market cap.

### Sortie

```
  1  🔥  98  YES  52%  $1,210,960    72j  US x Iran ceasefire by June 30?
       ██████░░░░░░  [geopolitics]  résolution: 2026-05-31
       https://polymarket.com/event/us-x-iran-ceasefire-by-june-30
```

Chaque résultat affiche son URL — copie directement dans `fetch_market.py`.

---

## 6. Générer le market_request avec fetch_market.py

```powershell
python fetch_market.py --url "https://polymarket.com/event/..." --id MKT-00X
```

### Les 4 types de marchés

| Type                 | Comportement                                |
| -------------------- | ------------------------------------------- |
| **Binaire**          | Sélection automatique                       |
| **Binaires groupés** | Liste des contrats nommés, tu tapes l'index |
| **Catégoriel**       | Distribution affichée, tu tapes l'index     |
| **Scalaire**         | Refusé — hors périmètre V1                  |

### Sélection directe sans interaction

```powershell
python fetch_market.py --url "..." --id MKT-00X --market-index 38
```

### Sortie

```
══════════════════════════════════════════════════════════════
  FETCH MARKET — MKT-025  ✓
══════════════════════════════════════════════════════════════
  US x Iran ceasefire by June 30?
──────────────────────────────────────────────────────────────
  Catégorie       : geopolitics
  Prix YES        : 52%  /  NO 48%
  Résolution      : 2026-05-31  (72j)
  Volume          : $1,208,876
──────────────────────────────────────────────────────────────
  Fichier créé    : markets/MKT-025/market_request.json
══════════════════════════════════════════════════════════════

  Prochaines étapes :
  1. Envoie le market_request.json à ChatGPT → analysis_payload.json
  2. Place le payload dans markets/MKT-025/
  3. python moteur.py --payload markets/MKT-025/analysis_payload.json
  4. python journal.py add --market MKT-025
```

---

## 7. Obtenir l'analysis_payload avec ChatGPT

### Configuration du projet ChatGPT

Upload dans "Connaissances" : `analysis_payload.schema.json`, `system_config.json`,
`MKT-001_analysis_payload_exemple.json`, `batch_payload_exemple.json`.

Colle les instructions de la section 16 dans "Instructions du projet".

### Marché unique

```
Voici le market_request pour ce marché :
[colle le contenu de market_request.json]
Produis l'analysis_payload.json selon le protocole V2.3.
Recherche d'abord les informations récentes sur internet.
```

### Batch

Colle le contenu de `chatgpt_prompt.txt`. Sauvegarde la réponse dans `batch_payload.json`.

---

## 8. Lancer le moteur

```powershell
# Prix lu automatiquement depuis market_request.json
python moteur.py --payload markets/MKT-00X/analysis_payload.json

# Forcer un prix différent
python moteur.py --payload markets/MKT-00X/analysis_payload.json --market-prob 0.38
```

Syntaxe PowerShell multi-lignes : utilise le backtick `` ` `` (pas `\`).

---

## 9. Tenir le journal

```powershell
python journal.py add --market MKT-00X          ← enregistre après le moteur
python journal.py positions                      ← positions ouvertes
python journal.py summary                        ← bankroll, win rate, PnL
python journal.py history                        ← historique complet

python journal.py resolve --market MKT-00X --result no --error-type correct_analysis --lesson "..."
```

`--result` : `yes` / `no` / `cancelled` / `disputed`

`--error-type` : `correct_analysis` `correct_abstention` `base_rate_error`
`factor_weighting_error` `prerequisite_misread` `missing_information` `resolution_ambiguity`

---

## 10. Workflow multi-contrats (batch)

```powershell
# Filtrer et générer le prompt
python batch_fetch.py --url "..." --id BATCH-001 --min-price 0.03 --top 15

# Lancer le moteur sur tout le batch
python batch_engine.py --batch BATCH-001
```

`batch_engine` détecte automatiquement les contradictions sur les marchés
mutuellement exclusifs (élections, tournois) et affiche une alerte de cohérence.

---

## 11. Lire l'engine_output

| Décision                  | Signification            |
| ------------------------- | ------------------------ |
| `NO_TRADE`                | Edge insuffisant ou veto |
| `SMALL_PAPER_POSITION`    | Edge 5–12% → 20 €        |
| `STANDARD_PAPER_POSITION` | Edge > 12% → 50 €        |

**NO n'est pas une abstention.** Acheter NO à 88¢ sur un marché à 12% est une vraie position.

### Vetos

| Veto                               | Cause                           |
| ---------------------------------- | ------------------------------- |
| `resolution_ambiguity = high`      | Règle de résolution floue       |
| `grand favori avec gap structurel` | Prix ≥ 75% ET raw_edge < -12%   |
| `blocking_veto_triggered`          | Prérequis bloquant `not_filled` |

---

## 12. Ce que le moteur calcule exactement

```
# Veto prérequis bloquant
si blocking = not_filled → p_estimated = base_rate × 0.25

# Score facteurs → probabilité
adjustment_ratio       = weighted_factor_sum / max_possible_sum  → [-1, +1]
probability_adjustment = adjustment_ratio × adjustment_cap (0.30)
p_estimated            = min(0.99, max(0.01, p_raw × prerequisite_factor))

# Edge ajusté
adjusted_edge = raw_edge × confidence × liquidity × information × ambiguity × time
si uncertainty_width > 0.35 → adjusted_edge × 0.5

# Veto grand favori
si prix ≥ 75% ET raw_edge < -12% → veto

# Décision
|adjusted_edge| < 0.05         → no_trade
0.05 ≤ |adjusted_edge| < 0.12  → small_paper_position (20 €)
|adjusted_edge| ≥ 0.12         → standard_paper_position (50 €)
```

Multiplicateurs : confidence `high=1.0 / medium=0.8 / low=0.5` — liquidity `high=1.0 / medium=0.85 / low=0.6` — time `<30j=1.0 / 30-120j=0.9 / 120-365j=0.8 / >365j=0.7`

---

## 13. Marchés compatibles et incompatibles

| Type                         | Workflow                       |
| ---------------------------- | ------------------------------ |
| Binaire pur                  | `fetch_market.py`              |
| Binaires groupés — 1 contrat | `fetch_market.py` (interactif) |
| Binaires groupés — plusieurs | `batch_fetch.py`               |
| Catégoriel                   | `fetch_market.py` (interactif) |
| Scalaire                     | ❌ Non supporté V1             |

À éviter : prix > 75% ou < 25% — résolution < 48h — règle floue — aucune info publique — horizon > 365j — favori < 30% sur champ fragmenté.

---

## 14. Erreurs fréquentes

**Erreur PowerShell "opérateur unaire"** → tout sur une ligne ou backtick `` ` ``

**"Champ obligatoire manquant"** → redemande le champ à ChatGPT

**`days_to_resolution: null`** → `resolution_date` manque dans le payload

**Enum invalide (`'category': 'iran'`)** → valeurs : `electoral_politics` `geopolitics` `macroeconomics` `institutions_justice` `tech_companies` `other`

**Probabilité en pourcentage** → `"base_rate_value": 30` doit être `0.30`

**Moteur joue NO sur un grand favori** → veto grand favori (V1.3) gère ce cas automatiquement

**`batch_payload.json` non trouvé** → sauvegarde la réponse ChatGPT dans ce fichier

**Scan remonte des marchés sportifs** → signale le pattern pour ajouter le filtre

---

## 15. Modifier les paramètres

Fichier : `schemas/system_config.json` — incrémente `parameters_version` après modification.

```json
"decision_thresholds": { "min_edge": 0.05, "standard_edge": 0.12 }

"extreme_market_veto": {
  "enabled": true,
  "high_price_ceiling": 0.75,
  "low_price_ceiling": 0.25,
  "structural_edge_threshold": 0.12
}

"sizing": {
  "paper_bankroll": 1000,
  "small_position_pct": 0.02,
  "standard_position_pct": 0.05
}

"estimation": { "adjustment_cap": 0.30 }
```

---

## 16. Prompt complet pour ChatGPT

```
# Rôle

Tu es l'assistant analytique d'un système de paper trading sur marchés prédictifs (Polymarket).
Ton rôle est de produire des analysis_payload structurés et valides, conformes au protocole V2.3.
Tu ne prends pas de décisions de trading. Tu ne calcules pas les edges ni les probabilités finales.
Ces calculs sont effectués par un moteur Python séparé.

# Ce que tu fais

Quand l'utilisateur te soumet un market_request.json ou un batch prompt, tu produis :
1. Un résumé de ta recherche en langage naturel
2. Le fichier JSON complet et valide

Tu utilises ta capacité de recherche internet avant de produire tout payload.

# Protocole de production

## Étape 1 — Recherche internet (obligatoire)
Contexte récent, précédents historiques, acteurs impliqués, contraintes institutionnelles,
règle de résolution exacte sur Polymarket.

## Étape 2 — Screening
screening_status : "admissible", "conditional", "rejected"
event_clarity, resolution_clarity, liquidity_quality,
information_accessibility, market_noise_level : "high", "medium", "low"

## Étape 3 — Base rate
base_rate_value : décimale 0.01–0.99 (ex: 0.30 pour 30%, jamais 30)
base_rate_reference_class : obligatoire
base_rate_comment : limites du parallèle historique

## Étape 4 — Prérequis
Deux listes : "blocking" et "weighted"
status : "filled", "partial", "not_filled", "unknown"

## Étape 5 — Facteurs
factor_type : "accelerator" ou "brake" uniquement
factor_score : entier -2, -1, 0, 1 ou 2 (jamais décimale)
factor_weight : entier 1, 2 ou 3 uniquement
factor_comment : obligatoire si factor_weight = 3

## Étape 6 — Confiance
confidence_sources, confidence_model, confidence_context,
confidence_overall : "high", "medium", "low"

## Étape 7 — Ambiguïté
event_ambiguity, resolution_ambiguity : "high", "medium", "low"
ATTENTION : resolution_ambiguity = "high" → veto automatique.

## Étape 8 — Contradiction forcée (OBLIGATOIRE)
best_counter_thesis : string non vide
top_3_failure_reasons : array de EXACTEMENT 3 strings
market_might_be_right_because : string
thesis_invalidation_trigger : string

# Règles de format JSON

thesis_id        : market_id + "-TH1"
analysis_id      : thesis_id + "-A1"
analysis_version : "A1"
analysis_timestamp : heure actuelle ISO 8601
resolution_date    : TOUJOURS inclure au niveau racine du payload
schema_version   : "1.0"
protocol_version : "2.3"

Enums : "high"/"medium"/"low" — "accelerator"/"brake" — "filled"/"partial"/"not_filled"/"unknown"
Probabilités : décimale 0.01–0.99, jamais pourcentage
factor_score : entier -2 à 2, factor_weight : entier 1 à 3

Champs bloquants (moteur refuse si absent) :
- base_rate.base_rate_value
- factor_list non vide avec factor_score et factor_weight
- confidence.confidence_overall
- ambiguity.event_ambiguity et ambiguity.resolution_ambiguity
- contradiction_forced.best_counter_thesis
- contradiction_forced.top_3_failure_reasons (exactement 3 éléments)

# Erreurs à éviter

1. Oublier resolution_date dans le payload
2. top_3_failure_reasons avec ≠ 3 éléments
3. factor_score ou factor_weight décimaux ou hors bornes
4. Probabilités en pourcentage
5. Enums en français

# Format de livraison

Marché unique : résumé + analysis_payload.json indenté sans commentaires
Batch         : résumé + tableau JSON [ { payload1 }, { payload2 }, ... ]
               (suivre exactement batch_payload_exemple.json)
```
