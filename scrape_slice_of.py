"""
╔══════════════════════════════════════════════════════════════════╗
║   SUPPORTAI - SCRAPE SLICE ORGANISMES DE FORMATION (data.gouv)   ║
╠══════════════════════════════════════════════════════════════════╣
║  À chaque run :                                                  ║
║   - télécharge la liste publique des OF (data.gouv, quotidienne) ║
║   - garde les Qualiopi ≥ OF_MIN_STAGIAIRES stagiaires/an, hors   ║
║     structures publiques et hors NDA déjà traités                ║
║   - cherche le site de chacun et y récupère l'email              ║
║   - s'arrête dès OF_TARGET_EMAILS emails trouvés (objectif/jour) ║
║   - borné : OF_MAX_PER_RUN organismes / OF_TIME_BUDGET secondes  ║
║   - mode "create" : additif pur, aucun marquage stale            ║
║                                                                  ║
║  Un organisme traité (avec ou sans email) n'est jamais retraité. ║
║  Moteur de recherche bloqué → arrêt propre, le reste est retenté ║
║  au run suivant, code retour 2 si RIEN n'a pu être traité.       ║
║                                                                  ║
║  Env : OF_TARGET_EMAILS (300) · OF_MAX_PER_RUN (1500)            ║
║        OF_TIME_BUDGET (1800 s) · OF_PROBE_WORKERS (4)            ║
║        OF_MIN_STAGIAIRES (30) · OF_QUALIOPI_ONLY (true)          ║
║  Usage : python scrape_slice_of.py                               ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import sys
from datetime import datetime, timezone

from scrapers.organismes_formation import OrganismesFormationScraper

OF_TARGET_EMAILS = int(os.getenv("OF_TARGET_EMAILS") or 300)
OF_MAX_PER_RUN = int(os.getenv("OF_MAX_PER_RUN") or 1500)
OF_TIME_BUDGET = int(os.getenv("OF_TIME_BUDGET") or 1800)


def main() -> int:
    print(f"\n🎓 Slice organismes de formation — {datetime.now(timezone.utc).isoformat()}")
    print(f"   Objectif : {OF_TARGET_EMAILS} emails | plafond {OF_MAX_PER_RUN} organismes "
          f"| budget {OF_TIME_BUDGET}s\n")

    scraper = OrganismesFormationScraper(max_per_run=OF_MAX_PER_RUN)
    scraper.skip_keys = scraper.load_known_keys()     # NDA déjà traités
    scraper.time_budget = OF_TIME_BUDGET
    scraper.target_emails = OF_TARGET_EMAILS
    print(f"   Déjà traités : {len(scraper.skip_keys)} organismes")

    try:
        result = scraper.run(mode="create")
    except Exception as exc:
        print(f"❌ Slice OF en erreur : {exc!r}")
        return 1

    s = scraper.stats
    print("\n" + "=" * 66)
    print(f"  Traités ce run     : {s['traites']}")
    print(f"  ✉️  Avec email      : {s['emails']}")
    print(f"  🌐 Site sans email : {s['sans_email']}")
    print(f"  ❓ Introuvables     : {s['introuvables']}")
    print(f"  📦 Cibles restantes: ~{max(0, s['cibles_restantes'] - s['traites'])}")
    print(f"  💾 Base            : +{result.inserted} nouveaux")
    if scraper.stop_reason:
        print(f"  ⏹️  Arrêt           : {scraper.stop_reason}")
    print("=" * 66)

    if s["traites"] == 0 and "bloqué" in scraper.stop_reason:
        print("🚨 Aucun organisme traité : recherche web bloquée depuis GitHub Actions.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
