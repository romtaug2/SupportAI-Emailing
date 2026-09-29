"""
╔══════════════════════════════════════════════════════════════════╗
║   SUPPORTAI - SCRAPE SLICE FRANCE TRAVAIL (récolte quotidienne)  ║
╠══════════════════════════════════════════════════════════════════╣
║  Scrape UN département de France Travail à chaque run :          ║
║   - N départements par run (FT_DEPTS_PER_RUN, défaut 3), curseur ║
║     dans data/ft_cursor.json, sauvé après CHAQUE département     ║
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
import time
from datetime import datetime, timezone
from pathlib import Path

from scrapers.france_travail import FranceTravailScraper, DEPARTEMENTS_FULL

BASE_DIR = Path(__file__).resolve().parent
CURSOR_PATH = BASE_DIR / "data" / "ft_cursor.json"

FT_MAX_PAGES = int(os.getenv("FT_MAX_PAGES") or 12)
FT_ENRICH = (os.getenv("FT_ENRICH") or "true").strip().lower() in {"1", "true", "yes", "on"}
FT_MAX_ENRICH = int(os.getenv("FT_MAX_ENRICH") or 40)
# Plusieurs départements par run (chacun est court en incrémental) + garde-fou
# temps : on n'ENTAME pas un nouveau département au-delà du budget.
FT_DEPTS_PER_RUN = max(1, int(os.getenv("FT_DEPTS_PER_RUN") or 3))
FT_TIME_BUDGET_MIN = float(os.getenv("FT_TIME_BUDGET_MIN") or 20)


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
    t0 = time.monotonic()
    cursor = _load_cursor()
    idx = int(cursor.get("ft_dept_idx", 0)) % len(DEPARTEMENTS_FULL)

    print(f"\n🔪 Scrape slice france_travail — {datetime.now(timezone.utc).isoformat()}")
    print(f"   Départements : {FT_DEPTS_PER_RUN} max par run (budget {FT_TIME_BUDGET_MIN:.0f} min)")
    print(f"   Cap pages    : {FT_MAX_PAGES} pages listing max / département")
    print(f"   Enrichir     : {FT_ENRICH} (max {FT_MAX_ENRICH} sites)")
    print(f"   Incrémental  : fiches déjà en base non refetchées\n")

    total_inserted = total_updated = 0
    done = 0

    for _ in range(FT_DEPTS_PER_RUN):
        elapsed_min = (time.monotonic() - t0) / 60
        if done and elapsed_min >= FT_TIME_BUDGET_MIN:
            print(f"⏱️  Budget temps atteint ({elapsed_min:.1f} min) → on s'arrête ici.")
            break

        dept = DEPARTEMENTS_FULL[idx]
        print(f"── Département [{idx + 1}/{len(DEPARTEMENTS_FULL)}] {dept}")

        scraper = FranceTravailScraper(
            test_mode=False,
            zones=[dept],                 # 1 département à la fois → borné
            max_pages=FT_MAX_PAGES,       # cap temps d'exécution
            enrich_emails=FT_ENRICH,
            max_enrichments=FT_MAX_ENRICH,
        )
        scraper.skip_keys = scraper.load_known_keys()   # incrémental
        print(f"   En base : {len(scraper.skip_keys)} fiches connues (skip auto)")

        try:
            # additif pur : PAS de mark_stale → l'export cumule tous les départements
            result = scraper.run(mode="create")
        except Exception as exc:
            # Échec réseau/site : curseur INCHANGÉ sur ce département, on garde
            # ce qui a déjà été fait dans ce run (curseur déjà sauvé par dept).
            print(f"❌ Slice en erreur sur {dept} : {exc!r} — curseur inchangé "
                  f"(même département au prochain run).")
            return 1 if not done else 0

        print(f"   📊 {dept} : +{result.inserted} nouveaux / ~{result.updated} maj / "
              f"={result.unchanged} / {scraper.skipped_known} sautés")
        total_inserted += result.inserted
        total_updated += result.updated
        done += 1

        # Curseur avancé et SAUVÉ après chaque département réussi.
        idx = (idx + 1) % len(DEPARTEMENTS_FULL)
        cursor["ft_dept_idx"] = idx
        _save_cursor(cursor)

    print(f"\n📊 Run : {done} département(s), +{total_inserted} nouveaux / ~{total_updated} maj "
          f"en {(time.monotonic() - t0) / 60:.1f} min")
    print(f"➡️  Prochain run : département [{idx + 1}/{len(DEPARTEMENTS_FULL)}] {DEPARTEMENTS_FULL[idx]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
