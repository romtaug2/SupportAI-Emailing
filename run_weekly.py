"""
╔══════════════════════════════════════════════════════════════════╗
║       SUPPORTAI - ORCHESTRATEUR HEBDOMADAIRE (run_weekly)        ║
╠══════════════════════════════════════════════════════════════════╣
║  Lance les scrapers du REGISTRY en mode FULL (update + stale),   ║
║  écrit un rapport JSON dans data/reports/.                       ║
║                                                                  ║
║  Env :                                                           ║
║    PB_TEST      "true" → mode test (volumes réduits).            ║
║                 Vide ou absent = FULL (cas du cron planifié).    ║
║    PB_VERTICALS "a,b,c" → sous-ensemble. Vide = défaut.          ║
║                                                                  ║
║  ⚠️ notaires est EXCLU par défaut : il est alimenté par tranches ║
║  quotidiennes via scrape_slice.py (voir daily_pipeline.yml).     ║
║  Pour le forcer ici : PB_VERTICALS=notaires                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from scrapers import REGISTRY

REPORTS_DIR = Path(__file__).resolve().parent / "data" / "reports"

# Gérés ailleurs (tranches quotidiennes incrémentales)
DEFAULT_EXCLUDED = {"notaires"}


def _env_bool(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> int:
    test_mode = _env_bool("PB_TEST")  # vide (cron) => FULL

    raw = (os.getenv("PB_VERTICALS") or "").strip()
    if raw:
        verticals = [v.strip() for v in raw.split(",") if v.strip()]
        unknown = [v for v in verticals if v not in REGISTRY]
        if unknown:
            print(f"❌ Verticals inconnus : {unknown}. Dispo : {sorted(REGISTRY)}")
            return 2
    else:
        verticals = sorted(v for v in REGISTRY if v not in DEFAULT_EXCLUDED)

    started = datetime.now(timezone.utc)
    print(f"\n🔄 Run hebdo — test_mode={test_mode} — verticals={verticals}\n")

    summary: dict[str, dict] = {}
    exit_code = 0

    for v in verticals:
        print(f"{'='*70}\n▶ {v}\n{'='*70}")
        try:
            scraper = REGISTRY[v](test_mode=test_mode)
            result = scraper.run(mode="update")
            summary[v] = {
                "status": "ok",
                "inserted": result.inserted,
                "updated": result.updated,
                "unchanged": result.unchanged,
            }
            print(f"✅ {v} : +{result.inserted} / ~{result.updated} / ={result.unchanged}")
        except Exception as exc:
            summary[v] = {"status": "error", "error": repr(exc)}
            exit_code = 1
            print(f"❌ {v} : {exc!r}")

    ended = datetime.now(timezone.utc)
    report = {
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "test_mode": test_mode,
        "verticals": verticals,
        "summary": summary,
        "exit_code": exit_code,
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"weekly_{started.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n📋 Rapport : {out}")
    print(f"⏱️ Durée : {(ended - started).total_seconds() / 60:.1f} min")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
