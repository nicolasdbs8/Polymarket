#!/usr/bin/env python3
"""
btc5m_phase0.py — Validation statistique Phase 0

Répond à UNE seule question avant de construire quoi que ce soit :
"Les features ont-elles un pouvoir prédictif non nul et stable
 sur des sous-périodes indépendantes ?"

Si la réponse est non ou marginale → ne pas construire de modèle.

Usage :
    python btc5m/btc5m_phase0.py                    ← 90 jours par défaut
    python btc5m/btc5m_phase0.py --days 180          ← 6 mois
    python btc5m/btc5m_phase0.py --days 365          ← 1 an
    python btc5m/btc5m_phase0.py --export            ← sauvegarde les résultats

Dépendances :
    pip install requests pandas numpy scipy
"""

import json
import sys
import time
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
    import pandas as pd
    import numpy as np
    from scipy import stats
except ImportError as e:
    print(f"\n[ERREUR] Dépendance manquante : {e}")
    print("  Lance : pip install requests pandas numpy scipy\n")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Collecte des données BTC 5m via Binance API (publique, sans clé)
# ─────────────────────────────────────────────────────────────────────────────

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_CANDLES_PER_REQUEST = 1000

def fetch_btc_5m(days: int) -> pd.DataFrame:
    """
    Récupère les bougies BTC/USDT 5m depuis Binance.
    Pagine automatiquement pour couvrir toute la période demandée.
    """
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    interval = "5m"

    all_candles = []
    current_start = start_ms
    total_expected = (days * 24 * 60) // 5

    print(f"  Téléchargement de ~{total_expected:,} bougies BTC/USDT 5m "
          f"({days} jours)...", end="", flush=True)

    while current_start < end_ms:
        params = {
            "symbol":    "BTCUSDT",
            "interval":  interval,
            "startTime": current_start,
            "endTime":   end_ms,
            "limit":     MAX_CANDLES_PER_REQUEST,
        }
        try:
            r = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except requests.RequestException as e:
            print(f"\n[ERREUR] Binance API : {e}")
            sys.exit(1)

        if not batch:
            break

        all_candles.extend(batch)
        last_open_time = batch[-1][0]
        current_start  = last_open_time + 5 * 60 * 1000  # +5 minutes

        print(".", end="", flush=True)

        if len(batch) < MAX_CANDLES_PER_REQUEST:
            break

        time.sleep(0.1)  # respecte le rate limit Binance

    print(f" {len(all_candles):,} bougies récupérées.")

    # Conversion en DataFrame
    df = pd.DataFrame(all_candles, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["open_time"]  = pd.to_datetime(df["open_time"],  unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])

    df = df.sort_values("open_time").reset_index(drop=True)
    df = df.drop_duplicates("open_time").reset_index(drop=True)

    return df


# ─────────────────────────────────────────────────────────────────────────────
# 2. Calcul des features
# ─────────────────────────────────────────────────────────────────────────────

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule les features sur les bougies fermées.
    RÈGLE CRITIQUE : aucune feature n'utilise de données de la bougie cible.
    Toutes les features sont décalées d'au moins 1 bougie (shift).
    """
    f = pd.DataFrame(index=df.index)

    # ── Variable cible ────────────────────────────────────────────────────────
    # 1 si la bougie SUIVANTE est verte (close > open), 0 sinon
    f["target"] = (df["close"] > df["open"]).astype(int).shift(-1)

    # ── Rendements ────────────────────────────────────────────────────────────
    ret = (df["close"] - df["open"]) / df["open"]

    f["ret_1"]  = ret.shift(1)                           # rendement dernière bougie
    f["ret_2"]  = ret.shift(2)
    f["ret_3"]  = ret.shift(3)
    f["ret_cum5"]  = ret.rolling(5).sum().shift(1)       # rendement cumulé 5 bougies
    f["ret_cum10"] = ret.rolling(10).sum().shift(1)

    # ── Momentum / persistance ────────────────────────────────────────────────
    direction = np.sign(ret)
    f["n_green_5"]  = (direction > 0).rolling(5).sum().shift(1)   # nb vertes sur 5
    f["n_green_10"] = (direction > 0).rolling(10).sum().shift(1)  # nb vertes sur 10
    f["streak"]     = direction.groupby(
        (direction != direction.shift()).cumsum()
    ).cumcount().shift(1)  # longueur de la série en cours

    # ── Structure du chandelier (dernière bougie) ─────────────────────────────
    body   = (df["close"] - df["open"]).abs()
    rng    = df["high"] - df["low"]
    wick_h = df["high"]  - df[["open", "close"]].max(axis=1)
    wick_l = df[["open", "close"]].min(axis=1) - df["low"]

    f["body_ratio"]  = (body / rng.replace(0, np.nan)).shift(1)    # corps / range
    f["close_pos"]   = (
        (df["close"] - df["low"]) / rng.replace(0, np.nan)
    ).shift(1)   # position du close dans le range [0, 1]
    f["wick_asymm"]  = (
        (wick_h - wick_l) / rng.replace(0, np.nan)
    ).shift(1)   # asymétrie mèches (positif = mèche haute > basse)

    # ── Volatilité / régime ───────────────────────────────────────────────────
    # Volatilité réalisée = std des rendements sur fenêtre glissante
    f["vol_12"]  = ret.rolling(12).std().shift(1)    # 1h
    f["vol_36"]  = ret.rolling(36).std().shift(1)    # 3h
    f["vol_72"]  = ret.rolling(72).std().shift(1)    # 6h

    # ATR simplifié
    tr = pd.DataFrame({
        "hl": df["high"] - df["low"],
        "hc": (df["high"] - df["close"].shift(1)).abs(),
        "lc": (df["low"]  - df["close"].shift(1)).abs(),
    }).max(axis=1)
    f["atr_12"] = tr.rolling(12).mean().shift(1)

    # Régime vol : percentile de la vol actuelle dans les 72 dernières bougies
    f["vol_percentile"] = (
        f["vol_12"].rolling(72).rank(pct=True)
    )

    # Expansion ou compression
    f["vol_ratio"] = (f["vol_12"] / f["vol_72"].replace(0, np.nan))

    # ── Position dans le range récent ─────────────────────────────────────────
    high_20 = df["high"].rolling(20).max().shift(1)
    low_20  = df["low"].rolling(20).min().shift(1)
    rng_20  = (high_20 - low_20).replace(0, np.nan)
    f["range_pos_20"] = (df["close"].shift(1) - low_20) / rng_20   # [0, 1]

    # ── Momentum de la moyenne mobile ────────────────────────────────────────
    ma10 = df["close"].rolling(10).mean()
    f["close_vs_ma10"] = (
        (df["close"].shift(1) - ma10.shift(1)) / ma10.shift(1)
    )  # % au-dessus/dessous de la MA10

    # ── Contexte intraday ─────────────────────────────────────────────────────
    f["hour_utc"]   = df["open_time"].dt.hour
    f["minute_utc"] = df["open_time"].dt.minute

    # Suppression des lignes avec NaN (début de série, lookback insuffisant)
    f = f.dropna()

    return f


# ─────────────────────────────────────────────────────────────────────────────
# 3. Analyse statistique
# ─────────────────────────────────────────────────────────────────────────────

FEATURE_NAMES = [
    "ret_1", "ret_2", "ret_3", "ret_cum5", "ret_cum10",
    "n_green_5", "n_green_10", "streak",
    "body_ratio", "close_pos", "wick_asymm",
    "vol_12", "vol_36", "vol_72", "atr_12",
    "vol_percentile", "vol_ratio", "range_pos_20",
    "close_vs_ma10", "hour_utc",
]

def analyze_feature(series: pd.Series, target: pd.Series, name: str) -> dict:
    """
    Analyse la relation entre une feature et la variable cible.
    Retourne : corrélation, p-value, point-biserial, et splitting informatif.
    """
    # Aligne les indices
    valid = pd.concat([series, target], axis=1).dropna()
    if len(valid) < 100:
        return {"name": name, "n": len(valid), "valid": False}

    x = valid.iloc[:, 0].values
    y = valid.iloc[:, 1].values

    # Corrélation de Pearson
    r_pearson, p_pearson = stats.pearsonr(x, y)

    # Corrélation point-bisériale (plus adaptée pour cible binaire)
    r_pb, p_pb = stats.pointbiserialr(y, x)

    # Test Mann-Whitney (non paramétrique)
    group1 = x[y == 1]
    group0 = x[y == 0]
    if len(group1) > 0 and len(group0) > 0:
        mw_stat, mw_p = stats.mannwhitneyu(group1, group0, alternative="two-sided")
        auc = mw_stat / (len(group1) * len(group0))  # AUC implicite
    else:
        mw_p, auc = 1.0, 0.5

    return {
        "name":       name,
        "n":          len(valid),
        "valid":      True,
        "r_pearson":  round(r_pearson, 4),
        "p_pearson":  round(p_pearson, 4),
        "r_pb":       round(r_pb, 4),
        "p_pb":       round(p_pb, 4),
        "mw_p":       round(mw_p, 4),
        "auc":        round(auc, 4),
        "mean_up":    round(float(np.mean(group1)), 5),
        "mean_down":  round(float(np.mean(group0)), 5),
        "significant": p_pb < 0.05 and abs(r_pb) > 0.01,
    }


def stability_test(df_features: pd.DataFrame, n_periods: int = 4) -> dict:
    """
    Teste la stabilité des corrélations sur des sous-périodes indépendantes.
    Si la corrélation d'une feature est instable (change de signe ou disparaît),
    c'est un signal fort qu'elle sera inutile hors échantillon.
    """
    n = len(df_features)
    period_size = n // n_periods
    results = {}

    for feat in FEATURE_NAMES:
        if feat not in df_features.columns:
            continue
        period_corrs = []
        for i in range(n_periods):
            start = i * period_size
            end   = start + period_size
            sub   = df_features.iloc[start:end]
            valid = sub[[feat, "target"]].dropna()
            if len(valid) < 50:
                continue
            r, p = stats.pointbiserialr(valid["target"], valid[feat])
            period_corrs.append(r)

        if len(period_corrs) >= 2:
            # Stabilité = faible variance des corrélations entre périodes
            # et cohérence de signe
            signs = [np.sign(r) for r in period_corrs if r != 0]
            sign_consistency = len(set(signs)) == 1  # tous mêmes signe
            corr_std = float(np.std(period_corrs))
            results[feat] = {
                "period_corrs": [round(r, 4) for r in period_corrs],
                "sign_consistent": sign_consistency,
                "corr_std": round(corr_std, 4),
                "stable": sign_consistency and corr_std < 0.05,
            }

    return results


def baseline_analysis(target: pd.Series) -> dict:
    """Analyse la variable cible : est-elle proche de 50/50 ?"""
    n      = len(target.dropna())
    n_up   = int(target.sum())
    p_up   = n_up / n if n > 0 else 0.5

    # Test binomial : est-ce significativement différent de 50% ?
    binom  = stats.binomtest(n_up, n, p=0.5, alternative="two-sided")

    return {
        "n_total":      n,
        "n_up":         n_up,
        "n_down":       n - n_up,
        "p_up":         round(p_up, 4),
        "p_down":       round(1 - p_up, 4),
        "binom_p":      round(binom.pvalue, 4),
        "baseline_bias": abs(p_up - 0.5) > 0.01 and binom.pvalue < 0.05,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Affichage
# ─────────────────────────────────────────────────────────────────────────────

def display_results(baseline: dict, feature_results: list, stability: dict,
                    days: int):
    print(f"\n{'═' * 65}")
    print(f"  BTC 5M — PHASE 0 : VALIDATION STATISTIQUE ({days} jours)")
    print(f"{'═' * 65}")

    # ── Baseline ──────────────────────────────────────────────────────────────
    print(f"\n  ── Baseline (variable cible) ──")
    print(f"  N total bougies : {baseline['n_total']:,}")
    print(f"  % vertes        : {baseline['p_up']:.1%}  "
          f"({'≠ 50% significatif' if baseline['baseline_bias'] else '≈ 50% non significatif'})")
    if baseline["baseline_bias"]:
        print(f"  → Biais de base détecté : p-value binomiale = {baseline['binom_p']:.4f}")
        print(f"    Ce biais est exploitable directement sans modèle.")

    # ── Features ──────────────────────────────────────────────────────────────
    print(f"\n  ── Analyse des features (tri par |r_pb|) ──\n")
    print(f"  {'Feature':<20} {'r_pb':>7} {'p-val':>8} {'AUC':>7} "
          f"{'Stable':>8}  {'Signal'}") 
    print(f"  {'─'*20} {'─'*7} {'─'*8} {'─'*7} {'─'*8}  {'─'*20}")

    # Tri par abs(r_pb) décroissant
    valid_results = [r for r in feature_results if r.get("valid")]
    valid_results.sort(key=lambda x: abs(x.get("r_pb", 0)), reverse=True)

    significant_count = 0
    stable_significant = 0

    for r in valid_results:
        stab = stability.get(r["name"], {})
        is_stable  = stab.get("stable", False)
        is_sig     = r["significant"]

        if is_sig:
            significant_count += 1
        if is_sig and is_stable:
            stable_significant += 1

        sig_icon   = "✓" if is_sig else "·"
        stable_str = "✓ stable" if is_stable else ("✗ instable" if stab else "?")
        signal_str = "⭐ SIGNAL POSSIBLE" if (is_sig and is_stable) else (
                     "△ sig. mais instable" if is_sig else "")

        print(f"  {r['name']:<20} {r['r_pb']:>+7.4f} {r['p_pb']:>8.4f} "
              f"{r['auc']:>7.4f} {stable_str:>10}  {signal_str}")

    # ── Stabilité détail ──────────────────────────────────────────────────────
    print(f"\n  ── Stabilité par sous-périodes ──\n")
    for feat, s in stability.items():
        if not any(f["name"] == feat and f.get("significant") for f in feature_results):
            continue
        periods_str = "  ".join(f"{r:+.3f}" for r in s["period_corrs"])
        sign_str = "✓ signe stable" if s["sign_consistent"] else "✗ signe instable"
        print(f"  {feat:<20} [{periods_str}]  {sign_str}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"  VERDICT")
    print(f"{'─' * 65}")
    print(f"  Features significatives (p < 0.05, |r| > 0.01) : {significant_count}")
    print(f"  Dont stables sur sous-périodes                  : {stable_significant}")

    if stable_significant == 0:
        print(f"\n  ✗ RECOMMANDATION : NE PAS CONSTRUIRE DE MODÈLE")
        print(f"    Aucune feature ne montre un signal stable hors échantillon.")
        print(f"    Construire un modèle sur ces données produirait très probablement")
        print(f"    un backtest flatteur et des performances live nulles.")
    elif stable_significant <= 2:
        print(f"\n  △ RECOMMANDATION : SIGNAL TRÈS FAIBLE")
        print(f"    {stable_significant} feature(s) montrent un signal marginal et stable.")
        print(f"    Proceed avec extrême prudence. Valide sur une période plus longue.")
        print(f"    Ne pas trader avant la Phase 1 complète avec walk-forward strict.")
    else:
        print(f"\n  ✓ RECOMMANDATION : SIGNAL DÉTECTÉ — Procède à la Phase 1")
        print(f"    {stable_significant} features stables. Construire un modèle léger")
        print(f"    (régression logistique) avec walk-forward sur ces features.")

    print(f"{'═' * 65}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Phase 0 : validation statistique BTC 5m avant tout modèle."
    )
    parser.add_argument("--days",   type=int, default=90,
                        help="Nombre de jours d'historique (défaut: 90)")
    parser.add_argument("--export", action="store_true",
                        help="Exporte les résultats en JSON dans btc5m/")
    args = parser.parse_args()

    print(f"\n{'═' * 65}")
    print(f"  BTC 5M — PHASE 0")
    print(f"  Validation statistique avant construction de modèle")
    print(f"{'═' * 65}\n")

    # 1. Données
    df = fetch_btc_5m(args.days)
    print(f"  Période : {df['open_time'].iloc[0].strftime('%Y-%m-%d')} → "
          f"{df['open_time'].iloc[-1].strftime('%Y-%m-%d')}")

    # 2. Features
    print(f"  Calcul des features...", end="", flush=True)
    features_df = compute_features(df)
    print(f" {len(features_df):,} observations avec features complètes.")

    # 3. Baseline
    baseline = baseline_analysis(features_df["target"])

    # 4. Analyse feature par feature
    print(f"  Analyse statistique des features...", end="", flush=True)
    feature_results = []
    for feat in FEATURE_NAMES:
        if feat in features_df.columns:
            result = analyze_feature(
                features_df[feat], features_df["target"], feat
            )
            feature_results.append(result)
    print(f" OK.")

    # 5. Test de stabilité
    print(f"  Test de stabilité sur sous-périodes...", end="", flush=True)
    stability = stability_test(features_df)
    print(f" OK.")

    # 6. Affichage
    display_results(baseline, feature_results, stability, args.days)

    # 7. Export optionnel
    if args.export:
        output_dir = Path(__file__).parent
        output_path = output_dir / f"phase0_results_{args.days}j.json"
        export_data = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days":         args.days,
            "n_candles":    len(features_df),
            "baseline":     baseline,
            "features":     feature_results,
            "stability":    stability,
        }
        def json_safe(obj):
            """Convertit les types numpy/pandas non sérialisables."""
            import numpy as np
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            raise TypeError(f"Type non sérialisable : {type(obj)}")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False, default=json_safe)
        print(f"  ✓ Résultats exportés : {output_path}\n")


if __name__ == "__main__":
    main()