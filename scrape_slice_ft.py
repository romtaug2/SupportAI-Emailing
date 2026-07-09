"""
╔══════════════════════════════════════════════════════════════════╗
║   SUPPORTAI - SCRAPE SLICE FRANCE TRAVAIL (récolte quotidienne)  ║
╠══════════════════════════════════════════════════════════════════╣
║  Scrape UN département de France Travail à chaque run :          ║
║   - 1 département à la fois (curseur dans data/ft_cursor.json)   ║
║   - mode "create" → AUCUN marquage stale (upsert additif pur),   ║
║     l'export re-dumpe toute la table → la base ne fait que       ║
║     grossir (cf. core.scraper_base.export_files).                ║
║   - borné par FT_MAX_PAGES (défaut 12 pages ≈ 120 fiches/run)    ║
║     pour tenir dans un budget Actions raisonnable. france_travail║
║     bufferise tout en mémoire et n'exporte qu'à la fin : un run  ║
║     doit rester assez court pour finir (sinon 0 export).         ║
║   - curseur avancé à chaque run réussi → boucle sur les ~101     ║
║     départements = refresh naturel (~3 mois par cycle complet).  ║
║   - échec réseau/site → curseur INCHANGÉ, on retentera le même   ║
║     département au prochain run.                                  ║
║                                                                  ║
║  Pourquoi séparé du weekly : france_travail (Playwright, non     ║
║  borné) cramait les 6h du weekly et faisait tout annuler         ║
║  (commit en if:success → 0 donnée sauvée). Ici il est isolé et   ║
║  borné, il ne bloque plus jamais rien.                           ║
║                                                                  ║
║  Env :                                                           ║
║    FT_MAX_PAGES   nb de pages listing max / département (12)     ║
║    FT_ENRICH      "true"/"false" enrichir emails via sites (on)  ║
║    FT_MAX_ENRICH  nb d'enrichissements site max / run (40)       ║
║  Usage : python scrape_slice_ft.py                               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from scrapers.france_travail import FranceTravailScraper, DEPARTEMENTS_FULL

BASE_DIR = Path(__file__).resolve().parent
CURSOR_PATH = BASE_DIR / "data" / "ft_cursor.json"

FT_MAX_PAGES = int(os.getenv("FT_MAX_PAGES") or 12)
FT_ENRICH = (os.getenv("FT_ENRICH") or "true").strip().lower() in {"1", "true", "yes", "on"}
FT_MAX_ENRICH = int(os.getenv("FT_MAX_ENRICH") or 40)


def _load_cursor() -> dict:
    if CURSOR_PATH.exists():
        try:
            return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"ft_dept_idx": 0}


def _save_cursor(cur: dict) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    cur["updated_at"] = datetime.now(timezone.utc).isoformat()
    CURSOR_PATH.write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    cursor = _load_cursor()
    idx = int(cursor.get("ft_dept_idx", 0)) % len(DEPARTEMENTS_FULL)
    dept = DEPARTEMENTS_FULL[idx]

    print(f"\n🔪 Scrape slice france_travail — {datetime.now(timezone.utc).isoformat()}")
    print(f"   Département : [{idx + 1}/{len(DEPARTEMENTS_FULL)}] {dept}")
    print(f"   Cap pages   : {FT_MAX_PAGES} pages listing max")
    print(f"   Enrichir    : {FT_ENRICH} (max {FT_MAX_ENRICH} sites)\n")

    scraper = FranceTravailScraper(
        test_mode=False,
        zones=[dept],                 # 1 seul département → run borné
        max_pages=FT_MAX_PAGES,       # cap temps d'exécution
        enrich_emails=FT_ENRICH,
        max_enrichments=FT_MAX_ENRICH,
    )

    try:
        # additif pur : PAS de mark_stale → l'export cumule tous les départements
        result = scraper.run(mode="create")
    except Exception as exc:
        # Échec réseau/site : on n'avance PAS le curseur, retentera au prochain run.
        print(f"❌ Slice en erreur : {exc!r} — curseur inchangé (même département demain).")
        return 1

    print(f"\n📊 Slice : +{result.inserted} nouveaux / ~{result.updated} maj / ={result.unchanged}")

    # Un département par run : on avance systématiquement au suivant (boucle).
    next_idx = (idx + 1) % len(DEPARTEMENTS_FULL)
    cursor["ft_dept_idx"] = next_idx
    _save_cursor(cursor)
    print(f"➡️  Prochain run : département "
          f"[{next_idx + 1}/{len(DEPARTEMENTS_FULL)}] {DEPARTEMENTS_FULL[next_idx]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
