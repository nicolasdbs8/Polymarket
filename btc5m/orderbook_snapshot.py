#!/usr/bin/env python3
"""
Wrapper — migré vers orderbook_snapshot.py générique (racine du projet).
Conservé pour compatibilité avec les appels locaux directs.

Usage direct : python btc5m/orderbook_snapshot.py [report]
"""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent.parent
action = sys.argv[1] if len(sys.argv) > 1 else "snapshot"

result = subprocess.run(
    [sys.executable, str(root / "orderbook_snapshot.py"),
     "--asset", "btc", "--timeframe", "5m", action],
    cwd=str(root),
)
sys.exit(result.returncode)
