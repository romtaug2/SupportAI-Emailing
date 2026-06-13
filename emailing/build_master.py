"""
╔══════════════════════════════════════════════════════════════════╗
║          SUPPORTAI - BUILD MASTER CSV (fusion 7 bases)           ║
╠══════════════════════════════════════════════════════════════════╣
║  Fusionne les 7 exports scrapers → emailing/data/                ║
║  supportai_contacts_master.csv (1 ligne = 1 email unique).       ║
║                                                                  ║
║  MERGE-SAFE : si le master existe déjà, le tracking d'envoi      ║
║  (email_sent / sent_at / send_status...) est PRÉSERVÉ.           ║
║  Un contact déjà 'sent' ne repasse jamais en 'pending'.          ║
║  Les nouveaux emails issus du run hebdo arrivent en 'pending'.   ║
║                                                                  ║
║  Usage : python emailing/build_master.py                         ║
║  Stdlib only — zéro dépendance, exécution < 5 s.                 ║
╚══════════════════════════════════════════════════════════════════╝
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent          # emailing/
REPO_DIR = BASE_DIR.parent                          # racine repo
EXPORTS_DIR = REPO_DIR / "exports"
DATA_DIR = BASE_DIR / "data"
MASTER_PATH = DATA_DIR / "supportai_contacts_master.csv"

# ── Mapping des bases ────────────────────────────────────────────────
#  (vertical, fichier, col_email, cols_emails_secondaires, col_company, col_city, rank)
#  rank = priorité d'envoi (plus haut = envoyé en premier).
#  E-commerce d'abord : c'est la cible du pitch.
#  ⛔ EDUCATION volontairement EXCLUE de la prospection.
SOURCES = [
    ("ecommerce",      "ecommerce/annuaire_boutiques_complet.csv",      "primary_email",   ["all_emails"],      "shop_name_from_listing", None,    7),
    ("hotels",         "hotels/base_prospection_trouve_ton_hotel.csv",  "email_principal", ["emails_trouves"],  "nom",                    "ville", 6),
    ("immo",           "immo/base_prospection_immomatin.csv",           "email_principal", ["emails_trouves"],  "nom",                    None,    5),
    ("notaires",       "notaires/annuaire_notaires_france.csv",         "email",           ["emails_all"],      "office",                 "city",  4),
    ("auto_ecole",     "auto_ecole/annuaire_auto_ecoles_france.csv",    "email",           [],                  "nom",                    "ville", 2),
    ("france_travail", "france_travail/francetravail_base.csv",         "email_principal", ["emails_trouves"],  "organisme",              "ville", 1),
]

EXCLUDED_VERTICALS = {"education"}  # jamais prospectés, même si réintroduits dans SOURCES

FIELDNAMES = [
    "email", "vertical", "company", "city", "score_source_rank",
    "email_sent", "sent_at", "send_status", "send_attempts",
    "last_error", "last_subject", "created_at", "updated_at",
]

TRACKING_FIELDS = [
    "email_sent", "sent_at", "send_status", "send_attempts",
    "last_error", "last_subject", "created_at",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(v) -> str:
    return "" if v is None else str(v).strip()


import re
_EMAIL_SPLIT = re.compile(r"[;,|\s]+")


def _emails_from(value) -> list[str]:
    """Extrait tous les emails valides d'une cellule (séparés par ; , | espace)."""
    out: list[str] = []
    for tok in _EMAIL_SPLIT.split(_clean(value).lower()):
        tok = tok.strip(" ;,|")
        if _valid_email(tok) and tok not in out:
            out.append(tok)
    return out


def _valid_email(email: str) -> bool:
    return bool(email and "@" in email and "." in email.split("@")[-1]
                and " " not in email and len(email) <= 254)


def collect_prospects() -> dict[str, dict]:
    """Lit les 7 exports → {email_lower: row}. Dédup : le rank le plus haut gagne."""
    prospects: dict[str, dict] = {}
    for vertical, rel_path, col_email, cols_extra, col_company, col_city, rank in SOURCES:
        if vertical in EXCLUDED_VERTICALS:
            print(f"⛔ {vertical:<15} : EXCLU de la prospection")
            continue
        path = EXPORTS_DIR / rel_path
        if not path.exists():
            print(f"⚠️  {vertical:<15} : fichier absent ({rel_path}) — ignoré")
            continue
        n_rows = n_kept = 0
        with path.open("r", newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                n_rows += 1
                # email principal + tous les emails secondaires de la fiche
                emails = _emails_from(row.get(col_email))
                for c in cols_extra:
                    for e in _emails_from(row.get(c)):
                        if e not in emails:
                            emails.append(e)
                for email in emails:
                    existing = prospects.get(email)
                    if existing and int(existing["score_source_rank"]) >= rank:
                        continue  # dédup cross-bases : rank le plus haut gagne
                    prospects[email] = {
                        "email": email,
                        "vertical": vertical,
                        "company": _clean(row.get(col_company)) if col_company else "",
                        "city": _clean(row.get(col_city)) if col_city else "",
                        "score_source_rank": str(rank),
                    }
                    n_kept += 1
        print(f"📂 {vertical:<15} : {n_rows:>4} lignes → {n_kept:>4} emails retenus")
    return prospects


def load_existing_master() -> dict[str, dict]:
    """Charge le master existant → {email_lower: row} (pour préserver le tracking)."""
    if not MASTER_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    with MASTER_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            email = _clean(row.get("email")).lower()
            if email:
                out[email] = row
    return out


def main() -> int:
    print(f"\n🔧 Build master SupportAI — {_now()}")
    print(f"   Exports : {EXPORTS_DIR}")
    print(f"   Master  : {MASTER_PATH}\n")

    prospects = collect_prospects()
    if not prospects:
        print("❌ Aucun prospect collecté — exports manquants ?")
        return 1

    existing = load_existing_master()
    now = _now()
    n_new = n_kept_tracking = 0

    merged: list[dict] = []
    for email, p in prospects.items():
        row = {f: "" for f in FIELDNAMES}
        row.update(p)
        old = existing.get(email)
        if old:
            # MERGE : on préserve tout le tracking existant
            for f in TRACKING_FIELDS:
                row[f] = _clean(old.get(f))
            if _clean(old.get("send_status")):
                n_kept_tracking += 1
        if not row["send_status"]:
            row["send_status"] = "pending"
            row["email_sent"] = "false"
            row["send_attempts"] = "0"
            row["created_at"] = now
            n_new += 1
        row["updated_at"] = now
        merged.append(row)

    # Contacts présents dans l'ancien master mais disparus des exports :
    # on les CONSERVE (historique d'envoi = précieux, et un prospect reste un prospect)
    n_orphans = 0
    for email, old in existing.items():
        if email not in prospects:
            row = {f: _clean(old.get(f)) for f in FIELDNAMES}
            row["email"] = email
            merged.append(row)
            n_orphans += 1

    # Tri : rank décroissant puis email (stable, diffs git propres)
    merged.sort(key=lambda r: (-int(r.get("score_source_rank") or 0), r["email"]))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with MASTER_PATH.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(merged)

    n_pending = sum(1 for r in merged if r["send_status"] == "pending")
    n_sent = sum(1 for r in merged if r["send_status"] == "sent")
    n_error = sum(1 for r in merged if r["send_status"] == "error")

    print(f"\n💾 Master écrit : {len(merged)} contacts "
          f"({MASTER_PATH.stat().st_size / 1024:.0f} Ko)")
    print(f"   🆕 nouveaux pending : {n_new}")
    print(f"   🔒 tracking préservé : {n_kept_tracking}")
    print(f"   👻 orphelins conservés : {n_orphans}")
    print(f"   📊 état : {n_pending} pending / {n_sent} sent / {n_error} error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
