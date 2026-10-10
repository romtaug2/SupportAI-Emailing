"""
╔══════════════════════════════════════════════════════════════════╗
║      SUPPORTAI - SCRAPE SLICE (récolte quotidienne notaires)     ║
╠══════════════════════════════════════════════════════════════════╣
║  Scrape une TRANCHE de l'annuaire notaires.fr à chaque run :     ║
║   - 1 région à la fois (curseur dans data/scrape_cursor.json)    ║
║   - skip automatique des offices déjà en base (la DB EST le      ║
║     curseur fin : chaque run ne fetch que du NOUVEAU)            ║
║   - cap SLICE_SIZE offices/run (défaut 300 ≈ 9-12 min)           ║
║   - mode "create" → AUCUN marquage stale (upsert additif pur),   ║
║     les exports et le master ne font que grossir                 ║
║   - région épuisée → curseur passe à la suivante (boucle sur     ║
║     les 18 régions = refresh annuel naturel des nouveautés)      ║
║                                                                  ║
║  Env : SLICE_SIZE (défaut 300)                                   ║
║  Usage : python scrape_slice.py                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from scrapers.notaires import NotairesScraper, START_URLS_FULL

BASE_DIR = Path(__file__).resolve().parent
CURSOR_PATH = BASE_DIR / "data" / "scrape_cursor.json"
DB_PATH = BASE_DIR / "data" / "notaires.db"

SLICE_SIZE = int(os.getenv("SLICE_SIZE") or 300)
# Budget temps de la récolte (s). Le step GitHub coupe à 18 min sans export :
# on s'arrête à 15 min pour laisser le temps d'écrire la base et les exports.
TIME_BUDGET = int(os.getenv("NOTAIRES_TIME_BUDGET") or 900)


def _load_cursor() -> dict:
    if CURSOR_PATH.exists():
        try:
            return json.loads(CURSOR_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"notaires_region_idx": 0}


def _save_cursor(cur: dict) -> None:
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    cur["updated_at"] = datetime.now(timezone.utc).isoformat()
    CURSOR_PATH.write_text(json.dumps(cur, indent=1, ensure_ascii=False), encoding="utf-8")


def _known_urls() -> set[str]:
    """URLs d'offices déjà en base (clé naturelle) → skip au prochain run."""
    if not DB_PATH.exists():
        return set()
    try:
        con = sqlite3.connect(DB_PATH)
        # Canonicalise (sans query) : les anciennes lignes stockées avec
        # ?search_params=... ne doivent plus être re-scrapées.
        urls = {r[0].split("?", 1)[0].rstrip("/")
                for r in con.execute("SELECT url FROM notaires") if r[0]}
        con.close()
        return urls
    except sqlite3.Error:
        return set()


def main() -> int:
    cursor = _load_cursor()
    idx = int(cursor.get("notaires_region_idx", 0)) % len(START_URLS_FULL)
    region_url = START_URLS_FULL[idx]
    region_name = region_url.rstrip("/").split("/")[-1]

    known = _known_urls()
    print(f"\n🔪 Scrape slice notaires — {datetime.now(timezone.utc).isoformat()}")
    print(f"   Région  : [{idx + 1}/{len(START_URLS_FULL)}] {region_name}")
    print(f"   En base : {len(known)} offices connus (skip auto)")
    print(f"   Tranche : {SLICE_SIZE} offices max | budget {TIME_BUDGET}s\n")

    scraper = NotairesScraper(test_mode=False)
    scraper.start_urls = [region_url]      # 1 région par run (listing borné)
    scraper.max_offices = SLICE_SIZE       # cap temps d'exécution
    scraper.skip_urls = known              # ne fetch que du nouveau
    scraper.time_budget = TIME_BUDGET      # arrêt propre avant le kill GitHub

    try:
        result = scraper.run(mode="create")  # additif pur : pas de stale
    except Exception as exc:
        # Échec réseau/site : on n'avance PAS le curseur, retentera demain.
        print(f"❌ Slice en erreur : {exc!r} — curseur inchangé.")
        return 1

    print(f"\n📊 Slice : +{result.inserted} nouveaux / ~{result.updated} maj")

    if scraper.stop_reason:
        print(f"ℹ️  Arrêt : {scraper.stop_reason}")

    # On n'avance le curseur QUE si la région a été couverte en entier.
    # (Avant : "moins de 300 insérés" = épuisée, faux dès qu'un run est coupé.)
    if scraper.exhausted:
        next_idx = (idx + 1) % len(START_URLS_FULL)
        cursor["notaires_region_idx"] = next_idx
        print(f"➡️  Région '{region_name}' épuisée → curseur sur "
              f"[{next_idx + 1}/{len(START_URLS_FULL)}] "
              f"{START_URLS_FULL[next_idx].rstrip('/').split('/')[-1]}")
    else:
        print(f"⏸️  Région '{region_name}' pas finie → on continue demain (skip auto).")

    _save_cursor(cursor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
