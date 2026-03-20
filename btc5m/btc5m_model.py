#!/usr/bin/env python3
"""
btc5m_model.py — Phase 1 : modèle calibré avec walk-forward

Construit une régression logistique légère sur les 8 features stables
identifiées en Phase 0. Valide avec walk-forward mensuel strict.

Métriques principales :
  - Brier score (calibration)
  - Log-loss
  - AUC ROC
  - Comparaison systématique contre baseline naive (50%)

Usage :
    python btc5m/btc5m_model.py --days 365
    python btc5m/btc5m_model.py --days 365 --export
    python btc5m/btc5m_model.py --days 365 --plot      ← courbe de calibration

Dépendances :
    pip install requests pandas numpy scipy scikit-learn matplotlib
"""

import json
import sys
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    import pandas as pd
    import numpy as np
    from scipy import stats
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    from sklearn.calibration import calibration_curve
except ImportError as e:
    print(f"\n[ERREUR] Dépendance manquante : {e}")
    print("  Lance : pip install requests pandas numpy scipy scikit-learn matplotlib\n")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Features retenues (Phase 0 — stables et significatives)
# ─────────────────────────────────────────────────────────────────────────────

STABLE_FEATURES = [
    "ret_1",
    "ret_cum5",
    "ret_cum10",
    "n_green_5",
    "n_green_10",
    "close_pos",
    "range_pos_20",
    "close_vs_ma10",
]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Collecte des données (repris de phase0)
# ─────────────────────────────────────────────────────────────────────────────

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

def fetch_btc_5m(days: int) -> pd.DataFrame:
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    all_candles = []
    current_start = start_ms
    total = (days * 24 * 60) // 5

    print(f"  Téléchargement ~{total:,} bougies BTC/USDT 5m ({days}j)...",
          end="", flush=True)

    while current_start < end_ms:
        params = {
            "symbol": "BTCUSDT", "interval": "5m",
            "startTime": current_start, "endTime": end_ms, "limit": 1000,
        }
        try:
            r = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
            r.raise_for_status()
            batch = r.json()
        except requests.RequestException as e:
            print(f"\n[ERREUR] {e}"); sys.exit(1)
        if not batch: break
        all_candles.extend(batch)
        current_start = batch[-1][0] + 5 * 60 * 1000
        print(".", end="", flush=True)
        if len(batch) < 1000: break
        time.sleep(0.1)

    print(f" {len(all_candles):,} bougies.")

    df = pd.DataFrame(all_candles, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_volume","trades",
        "taker_buy_base","taker_buy_quote","ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col])
    return df.sort_values("open_time").drop_duplicates("open_time").reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Features (identiques à phase0 — pas de leakage)
# ─────────────────────────────────────────────────────────────────────────────

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=df.index)
    f["open_time"] = df["open_time"]

    ret   = (df["close"] - df["open"]) / df["open"]
    rng   = df["high"] - df["low"]
    body  = (df["close"] - df["open"]).abs()

    f["target"]       = (df["close"] > df["open"]).astype(int).shift(-1)
    f["ret_1"]        = ret.shift(1)
    f["ret_cum5"]     = ret.rolling(5).sum().shift(1)
    f["ret_cum10"]    = ret.rolling(10).sum().shift(1)
    f["n_green_5"]    = (np.sign(ret) > 0).rolling(5).sum().shift(1)
    f["n_green_10"]   = (np.sign(ret) > 0).rolling(10).sum().shift(1)
    f["close_pos"]    = ((df["close"] - df["low"]) / rng.replace(0, np.nan)).shift(1)
    f["body_ratio"]   = (body / rng.replace(0, np.nan)).shift(1)

    high_20 = df["high"].rolling(20).max().shift(1)
    low_20  = df["low"].rolling(20).min().shift(1)
    rng_20  = (high_20 - low_20).replace(0, np.nan)
    f["range_pos_20"] = (df["close"].shift(1) - low_20) / rng_20

    ma10 = df["close"].rolling(10).mean()
    f["close_vs_ma10"] = (df["close"].shift(1) - ma10.shift(1)) / ma10.shift(1)

    return f.dropna()


# ─────────────────────────────────────────────────────────────────────────────
# 3. Walk-forward validation
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward_validation(
    df_features: pd.DataFrame,
    train_months: int = 6,
    test_months: int = 1,
) -> pd.DataFrame:
    """
    Walk-forward mensuel strict.

    À chaque étape :
      - Entraîne sur train_months mois consécutifs
      - Prédit sur le mois suivant (jamais vu pendant l'entraînement)
      - Avance d'un mois

    Retourne un DataFrame avec les prédictions OOS et les vraies valeurs.
    """
    df = df_features.copy()
    df["year_month"] = df["open_time"].dt.to_period("M")
    months = df["year_month"].unique()
    months_sorted = sorted(months)

    if len(months_sorted) < train_months + test_months:
        print(f"\n[ERREUR] Pas assez de données pour {train_months} mois de train.")
        sys.exit(1)

    results = []
    n_folds = len(months_sorted) - train_months

    print(f"\n  Walk-forward : {n_folds} folds "
          f"({train_months} mois train → 1 mois test)", end="", flush=True)

    for i in range(n_folds):
        train_months_range = months_sorted[i : i + train_months]
        test_month         = months_sorted[i + train_months]

        train_mask = df["year_month"].isin(train_months_range)
        test_mask  = df["year_month"] == test_month

        X_train = df.loc[train_mask, STABLE_FEATURES].values
        y_train = df.loc[train_mask, "target"].values
        X_test  = df.loc[test_mask,  STABLE_FEATURES].values
        y_test  = df.loc[test_mask,  "target"].values
        times   = df.loc[test_mask,  "open_time"].values

        if len(X_train) < 100 or len(X_test) < 50:
            continue

        # Standardisation — fit sur train uniquement
        scaler  = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_test_s  = scaler.transform(X_test)

        # Régression logistique avec régularisation L2
        model = LogisticRegression(
            C=1.0,           # régularisation modérée
            max_iter=500,
            random_state=42,
            solver="lbfgs",
        )
        model.fit(X_train_s, y_train)

        # Probabilités prédites (colonne 1 = P(Up))
        proba = model.predict_proba(X_test_s)[:, 1]

        for j in range(len(y_test)):
            results.append({
                "open_time":    times[j],
                "fold":         i,
                "test_month":   str(test_month),
                "y_true":       int(y_test[j]),
                "p_model":      float(proba[j]),
                "p_baseline":   0.50,
            })

        print(".", end="", flush=True)

    print(f" OK ({len(results):,} prédictions OOS)")
    return pd.DataFrame(results)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Métriques
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(predictions: pd.DataFrame) -> dict:
    y     = predictions["y_true"].values
    p_mod = predictions["p_model"].values
    p_bas = predictions["p_baseline"].values

    brier_model    = brier_score_loss(y, p_mod)
    brier_baseline = brier_score_loss(y, p_bas)
    logloss_model  = log_loss(y, p_mod)
    logloss_base   = log_loss(y, p_bas)
    auc_model      = roc_auc_score(y, p_mod)

    # Brier Skill Score : amélioration relative par rapport à la baseline
    # BSS > 0 = modèle meilleur que baseline
    # BSS = 1 = modèle parfait
    bss = 1 - (brier_model / brier_baseline)

    # Calibration : moyenne des probabilités prédites vs fréquence réelle
    fraction_pos, mean_predicted = calibration_curve(y, p_mod, n_bins=10)

    # Simulation paper trading avec filtre d'edge
    # On trade seulement si |p_model - 0.50| > edge_threshold
    results_by_threshold = {}
    for threshold in [0.01, 0.02, 0.03, 0.04, 0.05]:
        mask   = np.abs(p_mod - 0.5) > threshold
        n_signals = mask.sum()
        if n_signals < 100:
            continue
        y_sig  = y[mask]
        p_sig  = p_mod[mask]
        # Côté : Up si p > 0.5, Down si p < 0.5
        correct = np.where(p_sig > 0.5, y_sig == 1, y_sig == 0)
        win_rate = correct.mean()
        # PnL simulé : supposons gain/perte symétriques (simplifié)
        results_by_threshold[f"edge_{threshold:.2f}"] = {
            "n_signals":   int(n_signals),
            "pct_signals": round(n_signals / len(p_mod) * 100, 1),
            "win_rate":    round(float(win_rate), 4),
            "edge_vs_50":  round(float(win_rate - 0.5), 4),
        }

    # Stabilité mensuelle du modèle
    monthly = predictions.groupby("test_month").apply(
        lambda g: pd.Series({
            "brier":    brier_score_loss(g["y_true"], g["p_model"]),
            "auc":      roc_auc_score(g["y_true"], g["p_model"]) if g["y_true"].nunique() > 1 else 0.5,
            "n":        len(g),
        })
    ).reset_index()

    return {
        "n_predictions":    len(predictions),
        "brier_model":      round(brier_model, 6),
        "brier_baseline":   round(brier_baseline, 6),
        "brier_skill_score": round(float(bss), 4),
        "logloss_model":    round(logloss_model, 6),
        "logloss_baseline": round(logloss_base, 6),
        "auc_model":        round(auc_model, 4),
        "calibration": {
            "mean_predicted": [round(float(x), 4) for x in mean_predicted],
            "fraction_pos":   [round(float(x), 4) for x in fraction_pos],
        },
        "threshold_analysis": results_by_threshold,
        "monthly_stability": monthly.to_dict(orient="records"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Coefficients du modèle final
# ─────────────────────────────────────────────────────────────────────────────

def fit_final_model(df_features: pd.DataFrame) -> dict:
    """
    Entraîne le modèle final sur toutes les données.
    Retourne les coefficients pour interprétation.
    """
    X = df_features[STABLE_FEATURES].values
    y = df_features["target"].values

    scaler = StandardScaler()
    X_s    = scaler.fit_transform(X)

    model  = LogisticRegression(C=1.0, max_iter=500, random_state=42, solver="lbfgs")
    model.fit(X_s, y)

    coefficients = dict(zip(STABLE_FEATURES, model.coef_[0]))
    return {
        "intercept":    round(float(model.intercept_[0]), 6),
        "coefficients": {k: round(float(v), 6) for k, v in coefficients.items()},
        "scaler_mean":  dict(zip(STABLE_FEATURES, [round(float(x), 8) for x in scaler.mean_])),
        "scaler_std":   dict(zip(STABLE_FEATURES, [round(float(x), 8) for x in scaler.scale_])),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 6. Affichage
# ─────────────────────────────────────────────────────────────────────────────

def display_results(metrics: dict, final_model: dict, days: int):
    bss   = metrics["brier_skill_score"]
    brier = metrics["brier_model"]
    base  = metrics["brier_baseline"]
    auc   = metrics["auc_model"]

    print(f"\n{'═' * 65}")
    print(f"  BTC 5M — PHASE 1 : RÉSULTATS WALK-FORWARD ({days} jours)")
    print(f"{'═' * 65}")

    # ── Métriques globales ────────────────────────────────────────────────────
    print(f"\n  ── Métriques globales ──")
    print(f"  Prédictions OOS  : {metrics['n_predictions']:,}")
    print(f"  Brier model      : {brier:.6f}")
    print(f"  Brier baseline   : {base:.6f}  (toujours 50%)")
    print(f"  Brier Skill Score: {bss:+.4f}  "
          f"({'✓ modèle > baseline' if bss > 0 else '✗ modèle ≤ baseline'})")
    print(f"  Log-loss model   : {metrics['logloss_model']:.6f}")
    print(f"  Log-loss baseline: {metrics['logloss_baseline']:.6f}")
    print(f"  AUC ROC          : {auc:.4f}  (baseline = 0.5000)")

    # ── Calibration ───────────────────────────────────────────────────────────
    print(f"\n  ── Calibration (proba prédite vs fréquence réelle) ──")
    cal = metrics["calibration"]
    print(f"  {'Proba prédite':>14}  {'Fréq. réelle':>13}  {'Écart':>8}")
    print(f"  {'─'*14}  {'─'*13}  {'─'*8}")
    for mp, fp in zip(cal["mean_predicted"], cal["fraction_pos"]):
        ecart = fp - mp
        icon  = "✓" if abs(ecart) < 0.02 else ("△" if abs(ecart) < 0.04 else "✗")
        print(f"  {mp:>14.3f}  {fp:>13.3f}  {ecart:>+8.3f}  {icon}")

    # ── Analyse par seuil d'edge ──────────────────────────────────────────────
    print(f"\n  ── Analyse par seuil d'edge (|p_model - 50%| > seuil) ──")
    print(f"  {'Seuil':>7}  {'N signaux':>10}  {'% du temps':>11}  "
          f"{'Win rate':>9}  {'Edge vs 50%':>12}")
    print(f"  {'─'*7}  {'─'*10}  {'─'*11}  {'─'*9}  {'─'*12}")
    for key, vals in metrics["threshold_analysis"].items():
        edge_str = f"{vals['edge_vs_50']:+.2%}"
        wr_icon  = "⭐" if vals["win_rate"] > 0.515 else (
                   "○" if vals["win_rate"] > 0.505 else "·")
        print(f"  {key:>7}  {vals['n_signals']:>10,}  "
              f"{vals['pct_signals']:>10.1f}%  "
              f"{vals['win_rate']:>9.3f}  {edge_str:>12}  {wr_icon}")

    # ── Stabilité mensuelle ───────────────────────────────────────────────────
    monthly = metrics["monthly_stability"]
    if monthly:
        briers_monthly = [m["brier"] for m in monthly if isinstance(m.get("brier"), (int, float))]
        if briers_monthly:
            print(f"\n  ── Stabilité mensuelle ──")
            print(f"  Brier mensuel : min={min(briers_monthly):.4f}  "
                  f"max={max(briers_monthly):.4f}  "
                  f"std={np.std(briers_monthly):.4f}")
            stable = np.std(briers_monthly) < 0.01
            print(f"  Stabilité     : {'✓ stable' if stable else '△ variable'}")

    # ── Coefficients ─────────────────────────────────────────────────────────
    print(f"\n  ── Coefficients du modèle final ──")
    print(f"  (tous négatifs = mean-reversion confirmée)")
    coeffs = final_model["coefficients"]
    for feat, coef in sorted(coeffs.items(), key=lambda x: x[1]):
        bar_len = int(abs(coef) * 200)
        bar = "█" * min(bar_len, 30)
        print(f"  {feat:<20} {coef:>+8.4f}  {bar}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print(f"\n{'─' * 65}")
    print(f"  VERDICT")
    print(f"{'─' * 65}")

    if bss <= 0:
        print(f"\n  ✗ MODÈLE INVALIDE : Brier Skill Score ≤ 0")
        print(f"    Le modèle ne bat pas la baseline naïve à 50%.")
        print(f"    Ne pas passer à la Phase 2.")

    elif bss < 0.001:
        print(f"\n  △ SIGNAL TRÈS MARGINAL (BSS = {bss:.4f})")
        print(f"    Le modèle bat légèrement la baseline mais l'effet est")
        print(f"    probablement trop faible pour être exploitable après friction.")
        print(f"    → Vérifie les win rates par seuil d'edge.")
        print(f"    → Si win rate > 52% sur un seuil raisonnable → Phase 2.")

    else:
        max_wr = max(
            (v["win_rate"] for v in metrics["threshold_analysis"].values()),
            default=0.5
        )
        if max_wr > 0.52:
            print(f"\n  ✓ SIGNAL EXPLOITABLE DÉTECTÉ (BSS = {bss:.4f})")
            print(f"    Win rate max : {max_wr:.1%}")
            print(f"    → Procède à la Phase 2 : intégration Polymarket")
            print(f"    → Attention aux frictions (~1-3% sur marchés 5m)")
        else:
            print(f"\n  △ SIGNAL PRÉSENT MAIS FAIBLE (BSS = {bss:.4f})")
            print(f"    Win rate max : {max_wr:.1%} — potentiellement insuffisant")
            print(f"    après friction Polymarket.")
            print(f"    → Augmente l'historique ou attends plus de données.")

    print(f"{'═' * 65}\n")


# ─────────────────────────────────────────────────────────────────────────────
# 7. Plot calibration (optionnel)
# ─────────────────────────────────────────────────────────────────────────────

def plot_calibration(metrics: dict, save_path: Path = None):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib non installé — skip plot")
        return

    cal = metrics["calibration"]
    mp  = cal["mean_predicted"]
    fp  = cal["fraction_pos"]

    fig, axes = plt.subplots(1, 2, figsize=(12, 5))

    # Courbe de calibration
    ax = axes[0]
    ax.plot([0, 1], [0, 1], "k--", label="Calibration parfaite", alpha=0.5)
    ax.plot(mp, fp, "o-", color="#2E75B6", label="Modèle", linewidth=2)
    ax.set_xlabel("Probabilité prédite")
    ax.set_ylabel("Fréquence réelle")
    ax.set_title("Courbe de calibration (OOS)")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0.44, 0.56)
    ax.set_ylim(0.44, 0.56)

    # Win rate par seuil
    ax2 = axes[1]
    thresholds  = []
    win_rates   = []
    n_signals   = []
    for key, vals in metrics["threshold_analysis"].items():
        t = float(key.split("_")[1])
        thresholds.append(t)
        win_rates.append(vals["win_rate"])
        n_signals.append(vals["n_signals"])

    ax2.axhline(0.5, color="gray", linestyle="--", alpha=0.5, label="Baseline 50%")
    ax2.plot(thresholds, win_rates, "o-", color="#1F4E79", linewidth=2, label="Win rate")
    ax2b = ax2.twinx()
    ax2b.bar(thresholds, n_signals, width=0.005, alpha=0.2,
             color="#2E75B6", label="N signaux")
    ax2b.set_ylabel("Nombre de signaux", color="#2E75B6")
    ax2.set_xlabel("Seuil d'edge minimum")
    ax2.set_ylabel("Win rate")
    ax2.set_title("Win rate par seuil d'edge (OOS)")
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches="tight")
        print(f"  ✓ Graphique sauvegardé : {save_path}")
    else:
        plt.show()
    plt.close()


# ─────────────────────────────────────────────────────────────────────────────
# 8. Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Phase 1 : modèle calibré BTC 5m avec walk-forward."
    )
    parser.add_argument("--days",        type=int, default=365)
    parser.add_argument("--train-months",type=int, default=6,
                        help="Mois d'entraînement par fold (défaut: 6)")
    parser.add_argument("--export",      action="store_true")
    parser.add_argument("--plot",        action="store_true",
                        help="Affiche la courbe de calibration")
    args = parser.parse_args()

    print(f"\n{'═' * 65}")
    print(f"  BTC 5M — PHASE 1")
    print(f"  Modèle calibré + walk-forward")
    print(f"{'═' * 65}\n")

    # 1. Données
    df = fetch_btc_5m(args.days)
    print(f"  Période : {df['open_time'].iloc[0].strftime('%Y-%m-%d')} → "
          f"{df['open_time'].iloc[-1].strftime('%Y-%m-%d')}")

    # 2. Features
    print(f"  Calcul des features...", end="", flush=True)
    features_df = compute_features(df)
    print(f" {len(features_df):,} observations.")

    # 3. Walk-forward
    predictions = walk_forward_validation(
        features_df,
        train_months=args.train_months,
    )

    if len(predictions) == 0:
        print("[ERREUR] Aucune prédiction produite.")
        sys.exit(1)

    # 4. Métriques
    print(f"  Calcul des métriques...", end="", flush=True)
    metrics = compute_metrics(predictions)
    print(f" OK.")

    # 5. Modèle final
    print(f"  Entraînement modèle final...", end="", flush=True)
    final_model = fit_final_model(features_df)
    print(f" OK.")

    # 6. Affichage
    display_results(metrics, final_model, args.days)

    # 7. Plot
    if args.plot:
        output_dir = Path(__file__).parent
        plot_calibration(metrics, save_path=output_dir / "calibration_curve.png")

    # 8. Export
    if args.export:
        output_dir = Path(__file__).parent
        output_path = output_dir / f"phase1_results_{args.days}j.json"

        def json_safe(obj):
            import numpy as np
            if isinstance(obj, (np.bool_,)): return bool(obj)
            if isinstance(obj, (np.integer,)): return int(obj)
            if isinstance(obj, (np.floating,)): return float(obj)
            if isinstance(obj, np.ndarray): return obj.tolist()
            if hasattr(obj, 'isoformat'): return str(obj)
            raise TypeError(f"Type non sérialisable : {type(obj)}")

        export_data = {
            "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days":          args.days,
            "train_months":  args.train_months,
            "features_used": STABLE_FEATURES,
            "metrics":       metrics,
            "final_model":   final_model,
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False, default=json_safe)
        print(f"  ✓ Résultats exportés : {output_path}")
        print(f"  ✓ Modèle final sauvegardé (coefficients dans le JSON)\n")


if __name__ == "__main__":
    main()