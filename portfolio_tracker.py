#!/usr/bin/env python3
"""
portfolio_tracker.py — Suivi de portefeuille Polymarket multi-paires

Dashboard graphique :
  • Courbe valeur totale + tendance linéaire
  • P&L cumulé par paire (extensible)
  • Taux de victoire glissant
  • Drawdown depuis le pic

Sources :
  1. Logs locaux (execution_log.json + signal_log.json par paire)
  2. API Polymarket (positions ouvertes en temps réel)

Usage :
  python portfolio_tracker.py            # affiche + sauvegarde PNG
  python portfolio_tracker.py --no-api  # mode offline (logs locaux uniquement)
"""

import json
import sys
import math
import argparse
import io
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
WALLET = "0xb08d2A6083d0D3f491D30C6619A8472638015AeB"

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG : ajouter une nouvelle paire ici quand elle sera active
# ─────────────────────────────────────────────────────────────────────────────
PAIRS = {
    "btc5m": {
        "label":       "BTC 5m",
        "slug_prefix": "btc-updown-5m-",
        "color":       "#00bcd4",
        "dir":         "btc5m",
    },
    # Paires futures — décommenter et créer les dossiers correspondants
    # "btc15m": {
    #     "label":       "BTC 15m",
    #     "slug_prefix": "btc-updown-15m-",
    #     "color":       "#ff9800",
    #     "dir":         "btc15m",
    # },
    # "btcdaily": {
    #     "label":       "BTC Daily",
    #     "slug_prefix": "btc-updown-daily-",
    #     "color":       "#f44336",
    #     "dir":         "btcdaily",
    # },
    # "eth5m": {
    #     "label":       "ETH 5m",
    #     "slug_prefix": "eth-updown-5m-",
    #     "color":       "#9c27b0",
    #     "dir":         "eth5m",
    # },
    # "eth15m": {
    #     "label":       "ETH 15m",
    #     "slug_prefix": "eth-updown-15m-",
    #     "color":       "#e91e63",
    #     "dir":         "eth15m",
    # },
    # "sol5m": {
    #     "label":       "SOL 5m",
    #     "slug_prefix": "sol-updown-5m-",
    #     "color":       "#4caf50",
    #     "dir":         "sol5m",
    # },
    # "sol15m": {
    #     "label":       "SOL 15m",
    #     "slug_prefix": "sol-updown-15m-",
    #     "color":       "#8bc34a",
    #     "dir":         "sol15m",
    # },
    # "xrp5m": {
    #     "label":       "XRP 5m",
    #     "slug_prefix": "xrp-updown-5m-",
    #     "color":       "#ff5722",
    #     "dir":         "xrp5m",
    # },
    # "xrp15m": {
    #     "label":       "XRP 15m",
    #     "slug_prefix": "xrp-updown-15m-",
    #     "color":       "#ff7043",
    #     "dir":         "xrp15m",
    # },
    # "doge5m": {
    #     "label":       "DOGE 5m",
    #     "slug_prefix": "doge-updown-5m-",
    #     "color":       "#ffc107",
    #     "dir":         "doge5m",
    # },
}

# ─────────────────────────────────────────────────────────────────────────────
# API Polymarket — historique activité (source la plus fiable)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_activity(wallet: str) -> tuple[dict, dict]:
    """
    Récupère l'historique complet depuis l'API Polymarket.
    Retourne :
      trades_by_slug   : slug → {ts, usdc_spent, shares}   (agrégé par marché)
      redeems_by_slug  : slug → usdc_received               (agrégé par marché)
    """
    try:
        import requests
        r = requests.get(
            f"https://data-api.polymarket.com/activity?user={wallet}&limit=500",
            timeout=12,
        )
        if not r.ok:
            return {}, {}
        events = r.json()
        if not isinstance(events, list):
            return {}, {}
    except Exception as exc:
        print(f"  [API] Activité indisponible : {exc}")
        return {}, {}

    trades_by_slug:  dict[str, dict]  = {}
    redeems_by_slug: dict[str, float] = {}

    for ev in events:
        slug      = ev.get("slug", "") or ev.get("eventSlug", "")
        ev_type   = ev.get("type", "")
        usdc_size = float(ev.get("usdcSize", 0) or 0)
        size      = float(ev.get("size",     0) or 0)
        ts_epoch  = ev.get("timestamp", 0)

        if ev_type == "TRADE":
            if slug not in trades_by_slug:
                trades_by_slug[slug] = {
                    "ts":          datetime.fromtimestamp(ts_epoch, tz=timezone.utc),
                    "usdc_spent":  0.0,
                    "shares":      0.0,
                }
            trades_by_slug[slug]["usdc_spent"] += usdc_size
            trades_by_slug[slug]["shares"]     += size
            # Garder le timestamp le plus ancien pour ce marché
            t = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            if t < trades_by_slug[slug]["ts"]:
                trades_by_slug[slug]["ts"] = t

        elif ev_type == "REDEEM":
            redeems_by_slug[slug] = redeems_by_slug.get(slug, 0.0) + usdc_size

    return trades_by_slug, redeems_by_slug


# ─────────────────────────────────────────────────────────────────────────────
# Chargement données locales (métadonnées : direction, stratégie, paire)
# ─────────────────────────────────────────────────────────────────────────────

def load_pair_data(
    pair_key: str,
    cfg: dict,
    api_trades: dict,
    api_redeems: dict,
) -> tuple[list[dict], float | None]:
    """
    Combine l'API Polymarket (montants réels) avec les logs locaux (direction, stratégie).

    Priorité :
      1. api_trades  → montants dépensés réels (TRADE events)
      2. api_redeems → USDC reçus réels (REDEEM events)
      3. execution_log / signal_log → direction, paire, timestamp de placement

    Retourne (events, last_portfolio_usdc).
    """
    pair_dir    = ROOT / cfg["dir"]
    exec_path   = pair_dir / "execution_log.json"
    signal_path = pair_dir / "signal_log.json"
    prefix      = cfg["slug_prefix"]   # ex: "btc-updown-5m-"

    # ── Métadonnées locales ────────────────────────────────────────────────────
    exec_log = []
    last_portfolio = None
    if exec_path.exists():
        with open(exec_path, encoding="utf-8") as f:
            exec_log = json.load(f)
        for e in exec_log:
            if e.get("portfolio") is not None:
                last_portfolio = float(e["portfolio"])

    results_map: dict[str, str | None] = {}   # slug → "up"/"down"/None
    if signal_path.exists():
        with open(signal_path, encoding="utf-8") as f:
            signals = json.load(f)
        for s in signals:
            slug = s.get("pm_slug") or s.get("slug")
            if slug:
                results_map[slug] = s.get("result")

    # Direction par slug depuis execution_log (UP/DOWN)
    direction_map: dict[str, str] = {}
    for e in exec_log:
        slug = e.get("pm_slug", "")
        if slug and e.get("direction"):
            direction_map[slug] = e["direction"].upper()

    # ── Construire les événements ─────────────────────────────────────────────
    # Préférer l'API activity (montants réels) ; fallback sur execution_log si offline

    # Filtre slugs de cette paire depuis l'API
    pair_slugs_api = {
        slug: info
        for slug, info in api_trades.items()
        if slug.startswith(prefix)
    }

    if pair_slugs_api:
        # ── Mode API : source principale ──────────────────────────────────────
        source_slugs = pair_slugs_api
        use_api = True
    else:
        # ── Mode offline : reconstruction depuis execution_log ────────────────
        source_slugs = {}
        use_api = False
        for e in exec_log:
            if e.get("status") not in ("matched", "live"):
                continue
            slug   = e.get("pm_slug", "")
            ts_str = e.get("ts_bot") or e.get("ts_signal")
            if not slug or not ts_str or not slug.startswith(prefix):
                continue
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            sz = float(e.get("size_usdc", 0) or 0)
            ask = float(e.get("best_ask", 0) or 0)
            shares_est = sz / ask if ask > 0 else 0
            source_slugs[slug] = {"ts": ts, "usdc_spent": sz, "shares": shares_est}

    events = []
    for slug, trade_info in source_slugs.items():
        usdc_spent    = trade_info["usdc_spent"]
        shares        = trade_info["shares"]
        ts            = trade_info["ts"]
        usdc_received = api_redeems.get(slug, None) if use_api else None

        direction = direction_map.get(slug, "UP")
        result    = results_map.get(slug)

        if usdc_received is not None:
            # Tradé et settlé — montants exacts depuis l'API
            won     = (usdc_received > 0)
            gain    = round(usdc_received - usdc_spent, 4)
            pending = False
        elif result is not None:
            # Résultat connu via signal_log (REDEEM pas encore arrivé ou mode offline)
            won     = (direction == result.upper())
            gain    = round(shares - usdc_spent, 4) if won else round(-usdc_spent, 4)
            pending = False
        else:
            won     = None
            gain    = 0.0
            pending = True

        events.append({
            "ts":        ts,
            "slug":      slug,
            "pair":      pair_key,
            "direction": direction,
            "size_usdc": usdc_spent,
            "shares":    shares,
            "received":  usdc_received or 0.0,
            "result":    result,
            "won":       won,
            "gain_usdc": gain,
            "pending":   pending,
        })

    n_resolved = sum(1 for e in events if not e["pending"])
    n_wins     = sum(1 for e in events if e.get("won"))
    src_label  = "API" if use_api else "offline"
    print(f"  [{pair_key}] {len(events)} trades ({src_label}), {n_resolved} settlés, {n_wins}W/{n_resolved - n_wins}L")

    return sorted(events, key=lambda x: x["ts"]), last_portfolio


# ─────────────────────────────────────────────────────────────────────────────
# API Polymarket — positions ouvertes
# ─────────────────────────────────────────────────────────────────────────────

def fetch_open_positions(wallet: str) -> dict:
    """Retourne dict asset→info pour les positions ouvertes."""
    try:
        import requests
        r = requests.get(
            f"https://data-api.polymarket.com/positions"
            f"?user={wallet}&sizeThreshold=.01",
            timeout=10,
        )
        if not r.ok:
            return {}
        data = r.json()
        if not isinstance(data, list):
            return {}
        return {
            p.get("asset", ""): {
                "value":   round(float(p.get("currentValue", 0)), 4),
                "size":    round(float(p.get("size", 0)), 4),
                "title":   p.get("title", "")[:50],
                "outcome": p.get("outcome", ""),
                "slug":    p.get("conditionId", ""),
            }
            for p in data
        }
    except Exception as exc:
        print(f"  [API] Positions indisponibles : {exc}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Construction timeline globale
# ─────────────────────────────────────────────────────────────────────────────

def build_timeline(all_events: list[dict], open_positions: dict, last_portfolio_usdc: float | None = None) -> dict:
    """
    Construit la timeline P&L globale et par paire.
    Retourne un dict prêt pour le graphique.
    """
    if not all_events:
        return {}

    # Capital de départ : last_portfolio_usdc (argumento) ou portfolio_before si disponible
    first_portfolio = all_events[0].get("portfolio_before", last_portfolio_usdc or 0.0)

    # ── P&L cumulé global et par paire ──────────────────────────────────────
    resolved  = [e for e in all_events if not e["pending"]]
    pending   = [e for e in all_events if e["pending"]]

    # Timeline globale (résolu uniquement pour la courbe)
    global_ts  = []
    global_pnl = []
    cum = 0.0
    for e in resolved:
        cum += e["gain_usdc"]
        global_ts.append(e["ts"])
        global_pnl.append(round(cum, 4))

    # Timeline par paire
    pair_timelines = {}
    for pair_key in PAIRS:
        sub = [e for e in resolved if e["pair"] == pair_key]
        if not sub:
            continue
        cum_p = 0.0
        ts_p, pnl_p = [], []
        for e in sub:
            cum_p += e["gain_usdc"]
            ts_p.append(e["ts"])
            pnl_p.append(round(cum_p, 4))
        pair_timelines[pair_key] = {"ts": ts_p, "pnl": pnl_p}

    # ── Rolling win rate (fenêtre glissante N trades) ────────────────────────
    WIN_WINDOW = min(10, max(3, len(resolved) // 3)) if resolved else 10
    rolling_wr_ts, rolling_wr = [], []
    for i in range(len(resolved)):
        window = resolved[max(0, i - WIN_WINDOW + 1): i + 1]
        wr = sum(1 for e in window if e["won"]) / len(window)
        rolling_wr_ts.append(resolved[i]["ts"])
        rolling_wr.append(wr)

    # ── Drawdown ─────────────────────────────────────────────────────────────
    peak = 0.0
    drawdowns = []
    for pnl in global_pnl:
        if pnl > peak:
            peak = pnl
        dd = pnl - peak   # négatif ou nul
        drawdowns.append(dd)

    # ── Valeur de portefeuille ────────────────────────────────────────────────
    open_val    = sum(p["value"] for p in open_positions.values())
    current_pnl = global_pnl[-1] if global_pnl else 0.0

    # P&L réalisé = somme des gains nets par trade (montants réels API)
    total_pnl_real = sum(e["gain_usdc"] for e in resolved)

    # Solde USDC actuel = dernier solde lu par le bot (source la plus fiable)
    if last_portfolio_usdc is not None:
        portfolio_val   = last_portfolio_usdc + open_val
        # Capital initial = solde actuel - P&L net réalisé (à partir des gains réels)
        first_portfolio = last_portfolio_usdc - total_pnl_real
    else:
        portfolio_val   = first_portfolio + total_pnl_real + open_val

    # ── Stats globales ────────────────────────────────────────────────────────
    n_total    = len(all_events)
    n_resolved = len(resolved)
    n_pending  = len(pending)
    n_wins     = sum(1 for e in resolved if e["won"])
    n_losses   = n_resolved - n_wins
    total_mis  = sum(e["size_usdc"] for e in all_events)
    total_pnl  = total_pnl_real

    return {
        "global_ts":       global_ts,
        "global_pnl":      global_pnl,
        "pair_timelines":  pair_timelines,
        "rolling_wr_ts":   rolling_wr_ts,
        "rolling_wr":      rolling_wr,
        "win_window":      WIN_WINDOW,
        "drawdowns":       drawdowns,
        "first_portfolio": first_portfolio,
        "portfolio_val":   portfolio_val,
        "open_val":        open_val,
        "open_positions":  open_positions,
        "n_total":         n_total,
        "n_resolved":      n_resolved,
        "n_pending":       n_pending,
        "n_wins":          n_wins,
        "n_losses":        n_losses,
        "total_mis":       total_mis,
        "total_pnl":       total_pnl,
        "resolved_events": resolved,
        "pending_events":  pending,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Graphique
# ─────────────────────────────────────────────────────────────────────────────

def plot_dashboard(data: dict, save_path: Path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.gridspec as gridspec
        import numpy as np
    except ImportError:
        print("  matplotlib / numpy non disponibles — graphique ignoré")
        return

    if not data or not data.get("global_ts"):
        print("  Pas assez de trades résolus pour tracer un graphique.")
        return

    # ── Style dark ────────────────────────────────────────────────────────────
    plt.style.use("dark_background")
    BG    = "#0d1117"
    GRID  = "#1e2633"
    ACCENT = "#58a6ff"

    fig = plt.figure(figsize=(16, 10), facecolor=BG)
    fig.suptitle(
        f"Portfolio Polymarket — {WALLET[:10]}…   "
        f"({data['n_resolved']} trades résolus · {data['n_pending']} en attente)",
        fontsize=13, color="white", fontweight="bold", y=0.98,
    )

    gs = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.45, wspace=0.35,
        top=0.93, bottom=0.07,
        left=0.07, right=0.97,
    )

    ax_main = fig.add_subplot(gs[0:2, 0])  # P&L cumulé (grand, gauche)
    ax_pair = fig.add_subplot(gs[0:2, 1])  # P&L par paire (grand, droite)
    ax_wr   = fig.add_subplot(gs[2, 0])    # Win rate glissant (bas gauche)
    ax_dd   = fig.add_subplot(gs[2, 1])    # Drawdown (bas droite)

    ts_all = data["global_ts"]
    pnl_all = data["global_pnl"]
    ts_num  = mdates.date2num(ts_all)

    # ── Panel 1 : P&L cumulé global + tendance ───────────────────────────────
    ax_main.set_facecolor(BG)
    ax_main.set_title("P&L cumulé (USDC)", color="white", fontsize=11, pad=8)

    # Zone verte/rouge sous la courbe
    ax_main.fill_between(ts_all, pnl_all, 0,
                          where=[p >= 0 for p in pnl_all],
                          color="#238636", alpha=0.25, interpolate=True)
    ax_main.fill_between(ts_all, pnl_all, 0,
                          where=[p < 0 for p in pnl_all],
                          color="#da3633", alpha=0.25, interpolate=True)

    ax_main.plot(ts_all, pnl_all, color=ACCENT, lw=2, zorder=3, label="P&L cumulé")

    # Marqueurs win / loss
    wins_ts   = [e["ts"] for e in data["resolved_events"] if e["won"]]
    losses_ts = [e["ts"] for e in data["resolved_events"] if not e["won"]]
    wins_pnl   = [pnl_all[data["global_ts"].index(t)] for t in wins_ts  if t in data["global_ts"]]
    losses_pnl = [pnl_all[data["global_ts"].index(t)] for t in losses_ts if t in data["global_ts"]]
    ax_main.scatter(wins_ts,   wins_pnl,   color="#3fb950", s=40, zorder=4, alpha=0.8, label="Win")
    ax_main.scatter(losses_ts, losses_pnl, color="#f85149", s=40, zorder=4, alpha=0.8, marker="v", label="Loss")

    # Tendance linéaire (si ≥3 points)
    if len(ts_num) >= 3:
        z   = np.polyfit(ts_num, pnl_all, 1)
        p   = np.poly1d(z)
        trend_xs = np.linspace(ts_num[0], ts_num[-1], 100)
        trend_ys = p(trend_xs)
        tcolor    = "#3fb950" if z[0] >= 0 else "#f85149"
        ax_main.plot(
            mdates.num2date(trend_xs), trend_ys,
            color=tcolor, lw=1.5, ls="--", alpha=0.7, label=f"Tendance ({'+' if z[0]>=0 else ''}{z[0]:.4f} USDC/j)",
        )

    # Ligne zéro
    ax_main.axhline(0, color="#8b949e", lw=0.8, ls=":")

    # Annotation valeur actuelle
    current_pnl = pnl_all[-1]
    ax_main.annotate(
        f" {current_pnl:+.2f} USDC",
        xy=(ts_all[-1], current_pnl),
        color="#3fb950" if current_pnl >= 0 else "#f85149",
        fontsize=10, fontweight="bold", va="center",
    )

    # Portefeuille total en haut à droite
    portf_txt = (
        f"Portefeuille : {data['portfolio_val']:.2f} USDC\n"
        f"Capital init : {data['first_portfolio']:.2f} USDC\n"
        f"Positions    : {data['open_val']:.2f} USDC"
    )
    ax_main.text(
        0.98, 0.97, portf_txt,
        transform=ax_main.transAxes, ha="right", va="top",
        fontsize=8, color="#8b949e",
        bbox=dict(facecolor=GRID, edgecolor="#30363d", boxstyle="round,pad=0.4"),
    )

    ax_main.set_ylabel("USDC", color="#8b949e", fontsize=9)
    ax_main.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    ax_main.tick_params(colors="#8b949e", labelsize=8)
    for spine in ax_main.spines.values():
        spine.set_edgecolor(GRID)
    ax_main.grid(True, color=GRID, alpha=0.7)
    ax_main.legend(fontsize=8, loc="upper left", facecolor=GRID, edgecolor="#30363d",
                   labelcolor="white")
    plt.setp(ax_main.get_xticklabels(), rotation=30, ha="right")

    # ── Panel 2 : P&L par paire ───────────────────────────────────────────────
    ax_pair.set_facecolor(BG)
    ax_pair.set_title("P&L par paire", color="white", fontsize=11, pad=8)
    ax_pair.axhline(0, color="#8b949e", lw=0.8, ls=":")

    has_pair_data = False
    for pair_key, cfg in PAIRS.items():
        tl = data["pair_timelines"].get(pair_key)
        if not tl:
            continue
        has_pair_data = True
        ax_pair.plot(
            tl["ts"], tl["pnl"],
            color=cfg["color"], lw=2,
            label=f"{cfg['label']} ({tl['pnl'][-1]:+.2f})",
        )
        # Dernier point
        ax_pair.scatter([tl["ts"][-1]], [tl["pnl"][-1]], color=cfg["color"], s=50, zorder=4)

    if not has_pair_data:
        ax_pair.text(0.5, 0.5, "Aucune donnée", transform=ax_pair.transAxes,
                     ha="center", va="center", color="#8b949e")

    ax_pair.set_ylabel("USDC", color="#8b949e", fontsize=9)
    ax_pair.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
    ax_pair.tick_params(colors="#8b949e", labelsize=8)
    for spine in ax_pair.spines.values():
        spine.set_edgecolor(GRID)
    ax_pair.grid(True, color=GRID, alpha=0.7)
    if has_pair_data:
        ax_pair.legend(fontsize=8, facecolor=GRID, edgecolor="#30363d", labelcolor="white")
    plt.setp(ax_pair.get_xticklabels(), rotation=30, ha="right")

    # ── Panel 3 : Win rate glissant ───────────────────────────────────────────
    ax_wr.set_facecolor(BG)
    ax_wr.set_title(f"Win rate glissant ({data['win_window']} trades)", color="white", fontsize=10, pad=6)

    if data["rolling_wr"]:
        ax_wr.plot(data["rolling_wr_ts"], [w * 100 for w in data["rolling_wr"]],
                   color="#d29922", lw=1.8)
        ax_wr.fill_between(data["rolling_wr_ts"], [w * 100 for w in data["rolling_wr"]], 50,
                           where=[w >= 0.5 for w in data["rolling_wr"]],
                           color="#238636", alpha=0.25)
        ax_wr.fill_between(data["rolling_wr_ts"], [w * 100 for w in data["rolling_wr"]], 50,
                           where=[w < 0.5 for w in data["rolling_wr"]],
                           color="#da3633", alpha=0.25)
    ax_wr.axhline(50, color="#8b949e", lw=0.8, ls=":")
    ax_wr.set_ylim(0, 100)
    ax_wr.set_ylabel("%", color="#8b949e", fontsize=9)
    ax_wr.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax_wr.tick_params(colors="#8b949e", labelsize=8)
    for spine in ax_wr.spines.values():
        spine.set_edgecolor(GRID)
    ax_wr.grid(True, color=GRID, alpha=0.7)
    plt.setp(ax_wr.get_xticklabels(), rotation=30, ha="right")

    # Stats globales dans le coin
    if data["n_resolved"] > 0:
        wr_global = data["n_wins"] / data["n_resolved"] * 100
        roi = data["total_pnl"] / data["total_mis"] * 100 if data["total_mis"] else 0
        stats_txt = (
            f"{data['n_wins']}W / {data['n_losses']}L  WR={wr_global:.0f}%\n"
            f"Misé : {data['total_mis']:.2f} USDC\n"
            f"ROI  : {roi:+.1f}%"
        )
        ax_wr.text(
            0.02, 0.97, stats_txt,
            transform=ax_wr.transAxes, ha="left", va="top",
            fontsize=8, color="#8b949e",
            bbox=dict(facecolor=GRID, edgecolor="#30363d", boxstyle="round,pad=0.3"),
        )

    # ── Panel 4 : Drawdown ───────────────────────────────────────────────────
    ax_dd.set_facecolor(BG)
    ax_dd.set_title("Drawdown depuis le pic (USDC)", color="white", fontsize=10, pad=6)

    if data["drawdowns"]:
        ax_dd.fill_between(data["global_ts"], data["drawdowns"], 0,
                           color="#da3633", alpha=0.6)
        ax_dd.plot(data["global_ts"], data["drawdowns"],
                   color="#f85149", lw=1.2, drawstyle="steps-post")
        max_dd = min(data["drawdowns"])
        ax_dd.annotate(
            f" Max DD: {max_dd:.2f}",
            xy=(data["global_ts"][data["drawdowns"].index(max_dd)], max_dd),
            color="#f85149", fontsize=8, va="top",
        )

    ax_dd.axhline(0, color="#8b949e", lw=0.8, ls=":")
    ax_dd.set_ylabel("USDC", color="#8b949e", fontsize=9)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax_dd.tick_params(colors="#8b949e", labelsize=8)
    for spine in ax_dd.spines.values():
        spine.set_edgecolor(GRID)
    ax_dd.grid(True, color=GRID, alpha=0.7)
    plt.setp(ax_dd.get_xticklabels(), rotation=30, ha="right")

    # ── Sauvegarde ────────────────────────────────────────────────────────────
    plt.savefig(save_path, dpi=150, facecolor=BG, bbox_inches="tight")
    print(f"\n  Dashboard sauvegardé : {save_path}")

    try:
        import subprocess, os
        os.startfile(str(save_path))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Affichage console
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(data: dict):
    if not data:
        print("  Aucune donnée disponible.")
        return

    W = 72
    print()
    print("╔" + "═" * W + "╗")
    print("  PORTFOLIO TRACKER — Polymarket")
    print(f"  Wallet : {WALLET}")
    print("╚" + "═" * W + "╝")
    print()

    # Résumé financier
    pnl = data["total_pnl"]
    sign = "+" if pnl >= 0 else ""
    roi  = pnl / data["total_mis"] * 100 if data["total_mis"] else 0

    print(f"  Capital initial     : {data['first_portfolio']:.2f} USDC")
    print(f"  Positions ouvertes  : {data['open_val']:.2f} USDC")
    print(f"  Valeur portefeuille : {data['portfolio_val']:.2f} USDC")
    print(f"  P&L net réalisé     : {sign}{pnl:.2f} USDC")
    if data["first_portfolio"]:
        portfolio_roi = pnl / data["first_portfolio"] * 100
        sign_pr = "+" if portfolio_roi >= 0 else ""
        print(f"  ROI (portefeuille)  : {sign_pr}{portfolio_roi:.2f}%")
    if data["total_mis"]:
        print(f"  ROI (capital misé)  : {sign}{roi:.1f}%")
    print()

    # Tableau par paire
    print(f"  {'PAIRE':<12} {'TRADES':>6} {'WINS':>5} {'LOSSES':>7} {'WR':>6} {'P&L':>10} {'MISÉ':>8}")
    print("  " + "─" * 58)
    for pair_key, cfg in PAIRS.items():
        events = data.get("resolved_events", [])
        pair_events = [e for e in events if e["pair"] == pair_key]
        if not pair_events:
            print(f"  {cfg['label']:<12} {'—':>6}")
            continue
        n   = len(pair_events)
        w   = sum(1 for e in pair_events if e["won"])
        l   = n - w
        wr  = w / n * 100
        pnl_p = sum(e["gain_usdc"] for e in pair_events)
        mis_p = sum(e["size_usdc"]  for e in pair_events)
        sign_p = "+" if pnl_p >= 0 else ""
        print(f"  {cfg['label']:<12} {n:>6} {w:>5} {l:>7} {wr:>5.0f}% {sign_p}{pnl_p:>8.2f} {mis_p:>8.2f}")
    print("  " + "─" * 58)

    # Positions ouvertes
    if data["open_positions"]:
        print()
        print(f"  POSITIONS OUVERTES ({len(data['open_positions'])})")
        print(f"  {'VALEUR':>7}  {'TAILLE':>7}  {'STATUT':<26}  MARCHÉ")
        print("  " + "─" * 68)
        for asset, p in data["open_positions"].items():
            v, sz = p["value"], p["size"]
            ratio = v / max(sz, 0.001)
            if ratio > 0.85:
                status = "⏳ résolu, attente UMA"
            elif ratio > 0.40:
                status = "🔄 marché ouvert"
            else:
                status = "❌ perdant probable"
            print(f"  {v:>7.2f}  {sz:>7.2f}  {status:<26}  {p['title']}")
        print("  " + "─" * 68)
        print(f"  Total positions : {data['open_val']:.2f} USDC")

    # Trades en attente
    if data["pending_events"]:
        print()
        print(f"  TRADES EN ATTENTE DE RÉSOLUTION ({len(data['pending_events'])})")
        print("  " + "─" * 42)
        for e in data["pending_events"]:
            print(f"  {e['ts'].strftime('%d/%m %H:%M UTC')}  {e['pair']:<8}  "
                  f"{e['direction']:<4}  {e['size_usdc']:.2f} USDC  → {e['slug'][-10:]}")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot historique (pour les graphiques long terme)
# ─────────────────────────────────────────────────────────────────────────────

HISTORY_PATH = ROOT / "portfolio_history.json"

def save_snapshot(data: dict):
    """Ajoute un snapshot daté à portfolio_history.json."""
    if not data:
        return
    snapshot = {
        "ts":            datetime.now(timezone.utc).isoformat(),
        "portfolio_val": round(data["portfolio_val"], 4),
        "pnl":           round(data["total_pnl"], 4),
        "open_val":      round(data["open_val"], 4),
        "n_resolved":    data["n_resolved"],
        "n_wins":        data["n_wins"],
        "n_losses":      data["n_losses"],
    }
    history = []
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            history = []
    history.append(snapshot)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    print(f"  Snapshot historique ajouté : {HISTORY_PATH.name}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Portfolio tracker Polymarket")
    parser.add_argument("--no-api",   action="store_true", help="Mode offline (pas d'appel API)")
    parser.add_argument("--no-chart", action="store_true", help="Ne pas générer le graphique")
    args = parser.parse_args()

    # ── Chargement des données locales ────────────────────────────────────────
    all_events    = []
    # ── Activité globale depuis l'API (si disponible) ────────────────────────
    api_trades, api_redeems = {}, {}
    if not args.no_api:
        print("  Récupération de l'historique activité...")
        api_trades, api_redeems = fetch_activity(WALLET)

    last_portfolio = None
    for pair_key, cfg in PAIRS.items():
        events, lp = load_pair_data(pair_key, cfg, api_trades, api_redeems)
        all_events.extend(events)
        if lp is not None:
            last_portfolio = lp

    all_events.sort(key=lambda x: x["ts"])

    # ── Positions ouvertes (API) ───────────────────────────────────────────────
    open_positions = {}
    if not args.no_api:
        print("  Récupération des positions ouvertes...")
        open_positions = fetch_open_positions(WALLET)

    # ── Construction timeline ─────────────────────────────────────────────────
    data = build_timeline(all_events, open_positions, last_portfolio)

    # ── Affichage console ─────────────────────────────────────────────────────
    print_summary(data)

    # ── Snapshot historique ───────────────────────────────────────────────────
    save_snapshot(data)

    # ── Graphique ─────────────────────────────────────────────────────────────
    if not args.no_chart:
        save_path = ROOT / "portfolio_overview.png"
        plot_dashboard(data, save_path)


if __name__ == "__main__":
    main()
