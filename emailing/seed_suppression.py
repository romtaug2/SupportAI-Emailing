"""
╔══════════════════════════════════════════════════════════════════╗
║      SUPPORTAI - SEED DU REGISTRE DE SUPPRESSION (one-shot)       ║
╠══════════════════════════════════════════════════════════════════╣
║  À lancer UNE FOIS pour amorcer emailing/data/suppression.csv :   ║
║   1. les plaignants connus -> bloqués par DOMAINE entier          ║
║   2. tous les contacts déjà 'sent'/'unsubscribed' du master       ║
║      -> bloqués par EMAIL (pour ne re-contacter personne)         ║
║                                                                    ║
║  Idempotent : relançable sans créer de doublons.                  ║
║  Usage : python emailing/seed_suppression.py                      ║
╚══════════════════════════════════════════════════════════════════╝
"""

import csv
import sys
from pathlib import Path

# On réutilise les helpers du script d'envoi (source de vérité unique).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_supportai import (  # noqa: E402
    MASTER_PATH, SUPPRESSION_PATH,
    load_suppression, append_suppression, _safe, _domain,
)

# ── Domaines à bannir totalement (plaintes / menaces) ────────────────
COMPLAINT_DOMAINS = {
    "renaissance-tea.com": "complaint - 4 envois",
    "eon-internet.com":    "complaint - menace AFNIC",
}

# Statuts du master considérés comme "déjà traité, ne plus contacter".
ALREADY_DONE = {"sent", "unsubscribed", "complaint", "bounce"}


def main() -> int:
    sup_emails, sup_domains = load_suppression()
    n_dom = n_mail = 0

    # 1) Plaignants -> domaine entier
    for domain, reason in COMPLAINT_DOMAINS.items():
        if domain in sup_domains:
            continue
        # append_suppression attend un email ; on forge info@<domaine> + block_domain
        append_suppression(f"info@{domain}", reason, block_domain=True)
        sup_domains.add(domain)
        n_dom += 1
        print(f"🚫 domaine bloqué : {domain}  ({reason})")

    # 2) Contacts déjà touchés dans le master -> email
    if MASTER_PATH.exists():
        with MASTER_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                email = _safe(row.get("email")).lower()
                status = _safe(row.get("send_status")).lower()
                sent = _safe(row.get("email_sent")).lower() in {"true", "1", "yes"}
                if not email:
                    continue
                if (status in ALREADY_DONE or sent) and email not in sup_emails \
                        and _domain(email) not in sup_domains:
                    append_suppression(email, f"déjà traité ({status or 'sent'})")
                    sup_emails.add(email)
                    n_mail += 1
    else:
        print(f"⚠️  Master absent ({MASTER_PATH}) — étape 2 ignorée.")

    print(f"\n✅ Seed terminé : +{n_dom} domaines, +{n_mail} emails")
    print(f"   Registre : {SUPPRESSION_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
