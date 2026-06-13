#!/usr/bin/env python3
"""
cli.py
-------

CLI unifiée pour piloter toutes les bases de prospection.

Usage :

    python cli.py create <vertical> [--test]
        Premier run : initialise la base (même code que update, mais
        sémantiquement c'est l'exécution initiale).

    python cli.py update <vertical> [--test]
        Exécution récurrente (hebdo) : re-scrape et upserte. Les fiches
        non revues sont marquées stale (is_active=0) mais conservées.

    python cli.py export <vertical>
        Ré-exporte CSV/XLSX/JSONL depuis la base sans re-scraper.

    python cli.py status [<vertical>]
        Affiche un dump des stats (total, actives, stale, dernier run).

    python cli.py all <mode>
        Lance `mode` (create ou update) sur tous les verticals enregistrés.

    python cli.py list
        Liste les verticals disponibles.

Verticals disponibles :
    auto_ecole, ecommerce, education, france_travail, hotels, immo
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from core.utils import get_logger
from scrapers import REGISTRY


ROOT = Path(__file__).resolve().parent
LOG = get_logger("cli", ROOT / "data" / "logs" / "cli.log")


def _load_scraper(vertical: str, test_mode: bool):
    if vertical not in REGISTRY:
        raise SystemExit(
            f"Vertical inconnu : {vertical}\n"
            f"Disponibles : {', '.join(sorted(REGISTRY))}"
        )
    return REGISTRY[vertical](test_mode=test_mode)


def cmd_create(args):
    s = _load_scraper(args.vertical, args.test)
    result = s.run(mode="create")
    LOG.info("CREATE terminé (%s) : %r", args.vertical, result)


def cmd_update(args):
    s = _load_scraper(args.vertical, args.test)
    result = s.run(mode="update")
    LOG.info("UPDATE terminé (%s) : %r", args.vertical, result)


def cmd_export(args):
    s = _load_scraper(args.vertical, test_mode=False)
    s.export_files()


def cmd_status(args):
    verticals = [args.vertical] if args.vertical else sorted(REGISTRY)
    out = {}
    for v in verticals:
        try:
            s = REGISTRY[v](test_mode=False)
            out[v] = s.status()
        except Exception as e:
            out[v] = {"error": repr(e)}
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))


def cmd_all(args):
    summary = {}
    for v in sorted(REGISTRY):
        LOG.info("=" * 70)
        LOG.info(">>> %s %s (test=%s)", args.mode.upper(), v, args.test)
        LOG.info("=" * 70)
        try:
            s = REGISTRY[v](test_mode=args.test)
            result = s.run(mode=args.mode)
            summary[v] = {
                "status": "ok",
                "inserted": result.inserted,
                "updated": result.updated,
                "unchanged": result.unchanged,
            }
        except Exception as e:
            LOG.error("Erreur sur %s : %s\n%s",
                      v, e, traceback.format_exc())
            summary[v] = {"status": "error", "error": repr(e)}

    LOG.info("=" * 70)
    LOG.info("RÉCAPITULATIF %s :", args.mode.upper())
    for v, r in summary.items():
        LOG.info("  %-18s  %s", v, r)

    # Sauvegarde JSON du récap
    report = ROOT / "data" / "reports" / f"run_all_{args.mode}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    LOG.info("Récap écrit : %s", report)


def cmd_list(args):
    print("Verticals enregistrés :")
    for v in sorted(REGISTRY):
        cls = REGISTRY[v]
        print(f"  - {v:20s}  ({cls.__module__}.{cls.__name__})")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli",
        description="Orchestrateur des bases de prospection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Créer une base (premier run)")
    p_create.add_argument("vertical")
    p_create.add_argument("--test", action="store_true",
                          help="Mode test (volume réduit)")
    p_create.set_defaults(func=cmd_create)

    p_update = sub.add_parser("update", help="Mettre à jour une base (upsert)")
    p_update.add_argument("vertical")
    p_update.add_argument("--test", action="store_true")
    p_update.set_defaults(func=cmd_update)

    p_export = sub.add_parser("export", help="Ré-exporter CSV/XLSX depuis la DB")
    p_export.add_argument("vertical")
    p_export.set_defaults(func=cmd_export)

    p_status = sub.add_parser("status", help="Statut d'un ou tous les verticals")
    p_status.add_argument("vertical", nargs="?")
    p_status.set_defaults(func=cmd_status)

    p_all = sub.add_parser("all", help="Lancer create/update sur tous")
    p_all.add_argument("mode", choices=["create", "update"])
    p_all.add_argument("--test", action="store_true")
    p_all.set_defaults(func=cmd_all)

    p_list = sub.add_parser("list", help="Lister les verticals")
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
