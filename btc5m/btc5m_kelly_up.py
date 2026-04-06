"""
btc5m_kelly_up.py — Simulation Kelly sur signaux UP uniquement, fenêtre 10h-22h UTC
Filtre : direction == UP  ET  heure UTC entre 10h et 22h  ET  phase in [2b, 3, 3_watch]

SIMULATION RÉTROSPECTIVE — les résultats sont in-sample
et ne préjugent pas des performances futures.
"""

import json
import math
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent
SIGNAL_LOG     = BASE / "signal_log.json"
OUTPUT_JSON    = BASE / "kelly_up_results.json"
OUTPUT_IND     = BASE / "btc5m_kelly_up_individual.png"
OUTPUT_COMB    = BASE / "btc5m_kelly_up_combined.png"

FEES             = 0.002
INITIAL_BANKROLL = 100.0
WARMUP           = 56    # ~50% des 113 trades UP disponibles
WINDOW           = 56
RECALC_EVERY     = 10

FILTER_DESC = "UP  &  10h-22h UTC"

# ── Chargement + filtre ──────────────────────────────────────────────────────
def load_trades():
    with open(SIGNAL_LOG, "r", encoding="utf-8") as f:
        data = json.load(f)
    resolved = [e for e in data if e.get("result") not in (None, "", "null")]
    filtered = []
    for t in resolved:
        ts   = datetime.fromisoformat(t["ts"].replace("Z", "+00:00"))
        hour = ts.hour
        if t.get("direction") == "UP" and 10 <= hour < 22:
            filtered.append(t)
    filtered.sort(key=lambda e: e["ts"])
    return filtered

# ── Helpers ──────────────────────────────────────────────────────────────────
def get_pm_price(trade):
    return trade.get("pm_up") or 0.50

def is_win(trade):
    return trade.get("result", "").lower() == "up"

def trade_pnl(bankroll, size_pct, trade):
    stake = bankroll * size_pct
    pm    = get_pm_price(trade)
    if is_win(trade):
        return stake * (1.0 / pm - 1.0) * (1.0 - FEES)
    return -stake

def kelly_f(win_rate):
    return max(0.0, 2.0 * win_rate - 1.0)

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def actual_size(trade):
    """Sizing réel appliqué : STANDARD=5% si edge_net >= 3%, sinon PETIT=2%."""
    return 0.05 if trade.get("edge_net", 0.0) >= 0.03 else 0.02

# ── Stratégie A — Sizing fixe actuel (PETIT=2% / STANDARD=5%) ───────────────
def strategy_fixed(trades):
    bankroll = INITIAL_BANKROLL
    equity, sizes = [bankroll], []
    for t in trades:
        s = actual_size(t)
        sizes.append(s)
        bankroll += trade_pnl(bankroll, s, t)
        equity.append(bankroll)
    return equity, sizes

# ── Stratégie B — Kelly/4 statique ───────────────────────────────────────────
def strategy_kelly_static(trades):
    bankroll = INITIAL_BANKROLL
    equity, sizes = [bankroll], []
    wr = sum(1 for t in trades[:WARMUP] if is_win(t)) / WARMUP if WARMUP > 0 else 0.5
    f_static = clamp(kelly_f(wr) / 4.0, 0.01, 0.10)

    for i, t in enumerate(trades):
        s = actual_size(t) if i < WARMUP else f_static
        sizes.append(s)
        bankroll += trade_pnl(bankroll, s, t)
        equity.append(bankroll)
    return equity, sizes

# ── Stratégie C — Kelly/4 dynamique ──────────────────────────────────────────
def strategy_kelly_dynamic(trades):
    bankroll = INITIAL_BANKROLL
    equity, sizes = [bankroll], []
    current_f = 0.02

    for i, t in enumerate(trades):
        if i >= WINDOW and (i % RECALC_EVERY == 0 or i == WINDOW):
            recent    = trades[i - WINDOW:i]
            wr        = sum(1 for x in recent if is_win(x)) / WINDOW
            current_f = clamp(kelly_f(wr) / 4.0, 0.01, 0.10)

        s = current_f if i >= WINDOW else actual_size(t)
        sizes.append(s)
        bankroll += trade_pnl(bankroll, s, t)
        equity.append(bankroll)
    return equity, sizes

# ── Stratégie D — Kelly/4 ajusté par incertitude ─────────────────────────────
def strategy_kelly_uncertainty(trades):
    bankroll = INITIAL_BANKROLL
    equity, sizes = [bankroll], []
    wins_so_far = 0

    for i, t in enumerate(trades):
        if i < 10:
            s = 0.01
        else:
            wr        = wins_so_far / i
            f_raw     = kelly_f(wr) / 4.0
            uncertainty = 1.0 - 1.0 / math.sqrt(i + 1)
            s         = clamp(f_raw * uncertainty, 0.01, 0.10)

        sizes.append(s)
        bankroll += trade_pnl(bankroll, s, t)
        equity.append(bankroll)
        if is_win(t):
            wins_so_far += 1
    return equity, sizes

# ── Métriques ─────────────────────────────────────────────────────────────────
def compute_metrics(equity, sizes):
    arr     = np.array(equity)
    final   = arr[-1]
    roi     = (final - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100.0
    peak, max_dd = arr[0], 0.0
    for v in arr:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    returns = np.diff(arr) / arr[:-1]
    sharpe  = (returns.mean() / returns.std() * math.sqrt(252)) if returns.std() > 0 else 0.0
    return {
        "final_bankroll":   round(final, 4),
        "roi_pct":          round(roi, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "sharpe_ratio":     round(sharpe, 3),
        "avg_size_pct":     round(np.mean(sizes) * 100.0, 2),
        "n_trades":         len(sizes),
    }

# ── Plots ──────────────────────────────────────────────────────────────────────
STRATEGY_META = {
    "A_fixed":   ("#2196F3", "A - Sizing fixe (PETIT=2%/STANDARD=5%)"),
    "B_static":  ("#4CAF50", f"B - Kelly/4 statique (warmup={WARMUP})"),
    "C_dynamic": ("#FF9800", f"C - Kelly/4 dynamique (fenetre={WINDOW})"),
    "D_uncert":  ("#9C27B0", "D - Kelly/4 incertitude"),
}

def plot_individual(strategies, path):
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle(
        f"BTC 5m — Kelly fractionnaire — Filtre : {FILTER_DESC}\n"
        "[!] SIMULATION RETROSPECTIVE — resultats in-sample uniquement",
        fontsize=11, fontweight="bold"
    )
    for ax, key in zip(axes.flat, STRATEGY_META):
        color, label = STRATEGY_META[key]
        eq = strategies[key]["equity"]
        m  = strategies[key]["metrics"]
        ax.plot(range(len(eq)), eq, color=color, linewidth=1.8)
        ax.axhline(INITIAL_BANKROLL, color="gray", linewidth=0.8, linestyle="--")
        ax.set_title(
            f"{label}\n"
            f"Final: {m['final_bankroll']:.2f}$ | ROI: {m['roi_pct']:+.1f}%  "
            f"MaxDD: {m['max_drawdown_pct']:.1f}%  Sharpe: {m['sharpe_ratio']:.2f}  "
            f"Mise: {m['avg_size_pct']:.1f}%",
            fontsize=8.5
        )
        ax.set_xlabel("Trades")
        ax.set_ylabel("USDC")
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
        ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  -> Saved: {path}")

def plot_combined(strategies, path):
    fig, ax = plt.subplots(figsize=(12, 6))
    for key, (color, label) in STRATEGY_META.items():
        eq = strategies[key]["equity"]
        m  = strategies[key]["metrics"]
        ax.plot(eq, color=color, linewidth=2.0,
                label=f"{label}  ROI {m['roi_pct']:+.1f}%  MaxDD {m['max_drawdown_pct']:.1f}%")
    ax.axhline(INITIAL_BANKROLL, color="gray", linewidth=0.8, linestyle="--", label="Bankroll initiale")
    ax.set_title(
        f"BTC 5m — Comparaison Kelly — Filtre : {FILTER_DESC}\n"
        "[!] SIMULATION RETROSPECTIVE — resultats in-sample uniquement",
        fontsize=10, fontweight="bold"
    )
    ax.set_xlabel("Trades")
    ax.set_ylabel("Bankroll (USDC)")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(True, alpha=0.3)
    ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f"))
    plt.tight_layout()
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  -> Saved: {path}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"=== BTC 5m Kelly — Filtre : {FILTER_DESC} ===")
    trades = load_trades()
    if not trades:
        print("Aucun trade UP résolu dans la fenêtre 10h-22h.")
        return

    wins = sum(1 for t in trades if is_win(t))
    print(f"Trades filtres : {len(trades)}  |  Win rate : {wins/len(trades)*100:.1f}%  ({wins}/{len(trades)})")
    print(f"Parametres     : warmup={WARMUP}  window={WINDOW}  recalc_every={RECALC_EVERY}\n")

    eq_a, sz_a = strategy_fixed(trades)
    eq_b, sz_b = strategy_kelly_static(trades)
    eq_c, sz_c = strategy_kelly_dynamic(trades)
    eq_d, sz_d = strategy_kelly_uncertainty(trades)

    strategies = {
        "A_fixed":   {"equity": eq_a, "sizes": sz_a, "metrics": compute_metrics(eq_a, sz_a)},
        "B_static":  {"equity": eq_b, "sizes": sz_b, "metrics": compute_metrics(eq_b, sz_b)},
        "C_dynamic": {"equity": eq_c, "sizes": sz_c, "metrics": compute_metrics(eq_c, sz_c)},
        "D_uncert":  {"equity": eq_d, "sizes": sz_d, "metrics": compute_metrics(eq_d, sz_d)},
    }

    sep = "-" * 70
    names = {
        "A_fixed":   "A - Fixe (PETIT=2%/STANDARD=5%)",
        "B_static":  f"B - Kelly/4 statique (w={WARMUP})",
        "C_dynamic": f"C - Kelly/4 dynamique (w={WINDOW})",
        "D_uncert":  "D - Kelly/4 incertitude",
    }
    print(sep)
    print(f"{'Strategie':<32} {'Final':>8} {'ROI':>8} {'MaxDD':>8} {'Sharpe':>8} {'MiseMoy':>8}")
    print(sep)
    for key, name in names.items():
        m = strategies[key]["metrics"]
        print(
            f"{name:<32} "
            f"{m['final_bankroll']:>7.2f}$ "
            f"{m['roi_pct']:>+7.1f}% "
            f"{m['max_drawdown_pct']:>7.1f}% "
            f"{m['sharpe_ratio']:>8.3f} "
            f"{m['avg_size_pct']:>7.1f}%"
        )
    print(sep)

    # IC win rate à 95%
    import math as _math
    p = wins / len(trades)
    se = _math.sqrt(p * (1 - p) / len(trades))
    print(f"\n  IC win rate 95% : [{p - 1.96*se:.1%}  —  {p + 1.96*se:.1%}]  (n={len(trades)})")
    print(f"  Kelly recommande (f = 2p-1) : {kelly_f(p):.3f}  =>  Kelly/4 = {kelly_f(p)/4:.3f}")
    print(f"\n  [!] SIMULATION RETROSPECTIVE — resultats in-sample uniquement\n")

    print("Generation des graphiques...")
    plot_individual(strategies, OUTPUT_IND)
    plot_combined(strategies, OUTPUT_COMB)

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warning": "SIMULATION RETROSPECTIVE — resultats in-sample uniquement",
        "filter": FILTER_DESC,
        "n_trades": len(trades),
        "win_rate_pct": round(p * 100, 2),
        "win_rate_ci_95": [round((p - 1.96*se) * 100, 1), round((p + 1.96*se) * 100, 1)],
        "kelly_full": round(kelly_f(p), 4),
        "kelly_quarter": round(kelly_f(p) / 4, 4),
        "initial_bankroll": INITIAL_BANKROLL,
        "params": {"warmup": WARMUP, "window": WINDOW, "recalc_every": RECALC_EVERY},
        "strategies": {
            key: {
                "name": names[key],
                "metrics": strategies[key]["metrics"],
                "equity_curve": [round(v, 4) for v in strategies[key]["equity"]],
            }
            for key in strategies
        },
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"  -> Saved: {OUTPUT_JSON}")
    print("\nTermine.")

if __name__ == "__main__":
    main()
