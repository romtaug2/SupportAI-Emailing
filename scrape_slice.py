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
║  CORRECTIF 2026-10-22 : l'état du crawl est sauvé dans           ║
║  data/notaires_crawl_state.json → reprise exacte au run suivant  ║
║  (avant : recrawl depuis la page 1, budget mangé, 0 nouveau,     ║
║  région bloquée). Garde-fou : 3 runs à 0 → région suivante.      ║
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
STATE_PATH = BASE_DIR / "data" / "notaires_crawl_state.json"
MAX_ZERO_RUNS = 3   # runs consécutifs à 0 nouveau avant de forcer la région suivante

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


def _load_state(region: str) -> dict:
    """État du crawl de la région en cours (vide si autre région ou absent)."""
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if st.get("region") == region:
                return st.get("crawl") or {}
        except Exception:
            pass
    return {}


def _save_state(region: str, crawl: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({
        "region": region,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "crawl": crawl,
    }, ensure_ascii=False), encoding="utf-8")


def _gh_output(**kv) -> None:
    """Expose le résultat de la récolte à l'étape d'alerte du workflow."""
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write(f"{k}={v}\n")


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
    scraper.crawl_state = _load_state(region_name)   # reprise du crawl
    print(f"   Reprise : {len(scraper.crawl_state.get('seen') or [])} pages déjà vues, "
          f"{len(scraper.crawl_state.get('queue') or [])} en file, "
          f"{len(scraper.crawl_state.get('pending') or [])} offices en attente\n")

    try:
        result = scraper.run(mode="create")  # additif pur : pas de stale
    except Exception as exc:
        # Échec réseau/site : on garde l'état partiel (pages vues = acquis),
        # on n'avance PAS le curseur, retentera demain.
        _save_state(region_name, scraper.crawl_state)
        cursor["zero_runs"] = int(cursor.get("zero_runs", 0)) + 1
        cursor["last_inserted"] = 0
        _save_cursor(cursor)
        _gh_output(inserted=0, zero_runs=cursor["zero_runs"], region=region_name)
        print(f"❌ Slice en erreur : {exc!r} : curseur inchangé.")
        return 1

    print(f"\n📊 Slice : +{result.inserted} nouveaux / ~{result.updated} maj "
          f"| {scraper.pages_crawled} pages d'annuaire crawlées")

    if scraper.stop_reason:
        print(f"ℹ️  Arrêt : {scraper.stop_reason}")

    zero_runs = 0 if result.inserted else int(cursor.get("zero_runs", 0)) + 1
    advance = scraper.exhausted
    if not advance and zero_runs >= MAX_ZERO_RUNS:
        print(f"🚧 {zero_runs} runs d'affilée à 0 nouveau sur '{region_name}' "
              "→ région suivante forcée (le reste sera repris au prochain tour).")
        advance = True

    if advance:
        next_idx = (idx + 1) % len(START_URLS_FULL)
        cursor["notaires_region_idx"] = next_idx
        zero_runs = 0
        _save_state(START_URLS_FULL[next_idx].rstrip("/").split("/")[-1], {})
        print(f"➡️  Région '{region_name}' terminée → curseur sur "
              f"[{next_idx + 1}/{len(START_URLS_FULL)}] "
              f"{START_URLS_FULL[next_idx].rstrip('/').split('/')[-1]}")
    else:
        _save_state(region_name, scraper.crawl_state)
        print(f"⏸️  Région '{region_name}' pas finie → reprise exacte demain.")

    cursor["zero_runs"] = zero_runs
    cursor["last_inserted"] = result.inserted
    _save_cursor(cursor)
    _gh_output(inserted=result.inserted, zero_runs=zero_runs, region=region_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
