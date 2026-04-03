#!/usr/bin/env python3
"""
recover_signals.py — Reconstitue les signaux manquants dans btc5m/signal_log.json
en parsant les logs des runs GitHub Actions du workflow "BTC 5m Signal".

Usage:
  python recover_signals.py [--owner ORG] [--repo REPO] [--days N]
  GITHUB_TOKEN doit être défini dans l'environnement.
"""

import argparse
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SIGNAL_LOG = Path(__file__).parent / "btc5m" / "signal_log.json"

# ── GitHub API ──────────────────────────────────────────────────────────────


def gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_runs(owner: str, repo: str, token: str, days: int) -> list:
    """Liste tous les runs du workflow signal.yml sur les N derniers jours."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/workflows/signal.yml/runs"
    )
    params = {"created": f">={since}", "per_page": 100}
    headers = gh_headers(token)

    runs = []
    page = 1
    while True:
        r = requests.get(
            url, headers=headers, params={**params, "page": page}, timeout=30
        )
        r.raise_for_status()
        data = r.json()
        batch = data.get("workflow_runs", [])
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return runs


def download_logs_zip(owner: str, repo: str, run_id: int, token: str):
    """Télécharge le zip de logs d'un run. Retourne None si indisponible."""
    url = (
        f"https://api.github.com/repos/{owner}/{repo}"
        f"/actions/runs/{run_id}/logs"
    )
    r = requests.get(
        url, headers=gh_headers(token), timeout=60, allow_redirects=True
    )
    if r.status_code in (404, 410):
        return None  # logs expirés ou run inexistant
    r.raise_for_status()
    return r.content


# ── Parsing du zip de logs ──────────────────────────────────────────────────

# Préfixe de timestamp GitHub Actions : "2026-04-01T10:00:00.1234567Z "
_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s")


def _strip_gh_timestamps(raw: str) -> str:
    """Retire les préfixes de timestamp ajoutés par GitHub Actions."""
    return "\n".join(
        _TS_PREFIX.sub("", line) for line in raw.splitlines()
    )


def find_signal_content_in_zip(zip_bytes: bytes):
    """
    Cherche dans le zip le fichier de log du step 'Run signal'.
    Retourne le contenu nettoyé (sans préfixes GitHub) ou None.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            names = z.namelist()

            # 1. Fichier dont le nom contient "Run signal"
            candidates = [
                n for n in names
                if re.search(r"[Rr]un[\s_]signal", n)
            ]

            # 2. Fallback : n'importe quel fichier contenant la bannière du signal
            if not candidates:
                for name in names:
                    try:
                        raw = z.read(name).decode("utf-8", errors="replace")
                        if "BTC 5M" in raw and "SIGNAL" in raw:
                            candidates.append(name)
                            break
                    except Exception:
                        continue

            if not candidates:
                return None

            raw = z.read(candidates[0]).decode("utf-8", errors="replace")
            return _strip_gh_timestamps(raw)
    except zipfile.BadZipFile:
        return None


# ── Extraction des champs du signal ────────────────────────────────────────

def _search(pattern: str, text: str, group: int = 1, flags: int = 0):
    m = re.search(pattern, text, flags)
    return m.group(group) if m else None


def parse_signal(log_text: str, run_date: datetime):
    """
    Parse le texte d'un log GitHub Actions (déjà nettoyé) pour en extraire
    les données d'un signal tradeable.
    Retourne un dict compatible signal_log.json, ou None si rien à récupérer.
    """
    # ── Timestamp du signal (heure seulement) ───────────────────────────
    ts_str = _search(
        r"BTC 5M.*?SIGNAL\s+(\d{2}:\d{2}:\d{2})\s*UTC", log_text
    )
    if ts_str is None:
        return None
    h, m, s = map(int, ts_str.split(":"))
    signal_dt = run_date.replace(hour=h, minute=m, second=s, microsecond=0)
    ts = signal_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Décision ────────────────────────────────────────────────────────
    raw_decision = _search(r"D[ÉE]CISION\s*:\s*(.+?)(?:\n|$)", log_text)
    if raw_decision is None:
        return None
    # Nettoie emojis et espaces
    decision = re.sub(r"[^\x00-\x7F]", "", raw_decision).strip()

    # Seuls les signaux tradables nous intéressent
    if "PAS DE SIGNAL" in decision or "ABSORB" in decision.upper():
        return None

    dir_match = re.search(r"SIGNAL\s+(UP|DOWN)", decision)
    if dir_match is None:
        return None
    direction = dir_match.group(1)

    # ── Prix BTC ────────────────────────────────────────────────────────
    btc_str = _search(r"BTC prix actuel\s*:\s*\$([\d,]+\.?\d*)", log_text)
    if btc_str is None:
        return None
    btc_price = float(btc_str.replace(",", ""))

    # ── Probabilités modèle ─────────────────────────────────────────────
    pup_str = _search(r"P\(Up\)\s+mod[eè]le\s*:\s*([\d.]+)", log_text)
    pdown_str = _search(r"P\(Down\)\s+mod[eè]le\s*:\s*([\d.]+)", log_text)
    if pup_str is None or pdown_str is None:
        return None
    p_up = float(pup_str)
    p_down = float(pdown_str)

    # ── Edge brut ───────────────────────────────────────────────────────
    edge_str = _search(r"Edge brut\s*:\s*([\d.]+)", log_text)
    raw_edge = float(edge_str) if edge_str else round(abs(p_up - 0.5), 4)

    # ── Polymarket (obligatoire pour un signal tradeable) ───────────────
    pm_match = re.search(
        r"Polymarket\s*:\s*UP\s+([\d.]+)%\s*/\s*DOWN\s+([\d.]+)%", log_text
    )
    slug_str = _search(r"March[eé]\s*:\s*(btc-updown-5m-[\w-]+)", log_text)
    if pm_match is None or slug_str is None:
        return None  # signal sans marché Polymarket → non loggué à l'origine

    pm_up = round(float(pm_match.group(1)) / 100, 4)
    pm_down = round(float(pm_match.group(2)) / 100, 4)
    pm_slug = slug_str.strip()

    mins_str = _search(r"R[eé]solution dans\s*:\s*([\d.]+)\s*min", log_text)
    pm_mins_left = round(float(mins_str), 1) if mins_str else None

    # Edge net (avec signe)
    edge_net_str = _search(r"Edge net\s*:\s*([+-][\d.]+)", log_text)
    if edge_net_str is None:
        edge_net_str = _search(r"Edge net\s*:\s*([\d.]+)", log_text)
    edge_net = round(float(edge_net_str), 4) if edge_net_str else 0.0

    # ── Candle open (Dernière bougie HH:MM) ─────────────────────────────
    candle_hm = _search(r"Derni[eè]re bougie\s*:\s*(\d{2}:\d{2})", log_text)
    if candle_hm:
        ch, cm = map(int, candle_hm.split(":"))
        candle_dt = run_date.replace(hour=ch, minute=cm, second=0, microsecond=0)
        # Si la bougie semble postérieure au signal, c'est la veille
        if candle_dt > signal_dt:
            candle_dt -= timedelta(days=1)
        candle_open = candle_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        candle_open = None

    # ── Features ────────────────────────────────────────────────────────
    features = {}
    feat_block = re.search(
        r"Features d[eé]tail\s*:(.*?)(?:\n\s*\n|\Z)", log_text, re.DOTALL
    )
    if feat_block:
        for feat_line in feat_block.group(1).splitlines():
            fm = re.match(r"\s+([\w]+)\s+([+-]?[\d.]+(?:e[+-]?\d+)?)", feat_line)
            if fm:
                features[fm.group(1)] = round(float(fm.group(2)), 6)

    return {
        "ts":           ts,
        "btc_price":    round(btc_price, 2),
        "p_up":         round(p_up, 4),
        "p_down":       round(p_down, 4),
        "raw_edge":     round(raw_edge, 4),
        "direction":    direction,
        "decision":     decision,
        "edge_net":     edge_net,
        "pm_slug":      pm_slug,
        "pm_up":        pm_up,
        "pm_down":      pm_down,
        "pm_mins_left": pm_mins_left,
        "candle_open":  candle_open,
        "features":     features,
        "result":       None,
        "recovered":    True,
    }


# ── Signal log ──────────────────────────────────────────────────────────────

def load_signal_log() -> list:
    if SIGNAL_LOG.exists():
        with open(SIGNAL_LOG, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_signal_log(log: list):
    with open(SIGNAL_LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Reconstitue les signaux manquants depuis les logs GitHub Actions"
    )
    parser.add_argument("--owner", default="nicolasdbs8")
    parser.add_argument("--repo", default="Polymarket")
    parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN", ""),
        help="Token GitHub (ou variable GITHUB_TOKEN)"
    )
    parser.add_argument("--days", type=int, default=3,
                        help="Nombre de jours à remonter (défaut : 3)")
    args = parser.parse_args()

    if not args.token:
        print("[ERREUR] Token GitHub manquant. Définir GITHUB_TOKEN ou --token.")
        sys.exit(1)

    print(f"\nRecherche des runs 'BTC 5m Signal' sur les {args.days} derniers jours...")
    runs = list_runs(args.owner, args.repo, args.token, args.days)
    print(f"  {len(runs)} run(s) trouvé(s)")

    existing_log = load_signal_log()
    existing_ts = {e["ts"] for e in existing_log}
    # Clé composite pour éviter les doublons même si le ts diffère légèrement
    existing_keys = {
        (e.get("pm_slug"), e.get("candle_open"))
        for e in existing_log
        if e.get("pm_slug") and e.get("candle_open")
    }

    signals_found = 0
    signals_added = 0
    new_entries = []

    for run in runs:
        run_id = run["id"]
        run_created = run.get("created_at", "")

        try:
            run_date = datetime.strptime(
                run_created, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        zip_bytes = download_logs_zip(args.owner, args.repo, run_id, args.token)
        if not zip_bytes:
            continue  # logs expirés (>90 jours) ou run sans logs

        log_content = find_signal_content_in_zip(zip_bytes)
        if not log_content:
            continue  # run qui n'a pas produit de sortie signal (ex. friction, watch)

        signal = parse_signal(log_content, run_date)
        if signal is None:
            continue  # pas de signal tradeable dans ce run

        signals_found += 1

        # Anti-doublon par timestamp exact
        if signal["ts"] in existing_ts:
            continue

        # Anti-doublon par (slug, candle_open)
        key = (signal.get("pm_slug"), signal.get("candle_open"))
        if key != (None, None) and key in existing_keys:
            continue

        new_entries.append(signal)
        existing_ts.add(signal["ts"])
        if key != (None, None):
            existing_keys.add(key)
        signals_added += 1

        print(f"  + {signal['ts']}  {signal['direction']:4s}  {signal['pm_slug']}")

    if new_entries:
        combined = existing_log + new_entries
        combined.sort(key=lambda e: e["ts"])
        save_signal_log(combined)
        print(f"\n  signal_log.json mis à jour.")

    print(f"\n{'─' * 50}")
    print(f"  Runs analysés    : {len(runs)}")
    print(f"  Signaux trouvés  : {signals_found}")
    print(f"  Signaux ajoutés  : {signals_added}")
    print(f"{'─' * 50}\n")


if __name__ == "__main__":
    main()
