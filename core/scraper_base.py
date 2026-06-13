"""
core/scraper_base.py
---------------------

Classe de base partagée entre tous les scrapers. Gère :

- l'ouverture / fermeture de la base SQLite du vertical
- la création de l'entrée `scrape_runs`
- la boucle "yield une dict par fiche -> upsert" de façon uniforme
- le comptage insert / update / unchanged + marking stale en fin de run
- l'export CSV / XLSX / JSON après upsert

Chaque scraper concret fournit :
- VERTICAL         : identifiant du vertical (ex: 'ecommerce')
- TABLE            : nom de la table SQLite
- BUSINESS_COLUMNS : colonnes métier dans l'ordre de l'export
- NATURAL_KEY      : nom de la colonne qui sert de clé naturelle d'upsert
- EXPORT           : dict {csv: path, xlsx: path, jsonl: path, email_column: col}
- iter_records()   : générateur qui yield dict ligne par ligne
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from core import db
from core.export import export_csv, export_jsonl, export_xlsx
from core.utils import get_logger


DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class ExportConfig:
    csv_path: Path
    xlsx_path: Path | None = None
    jsonl_path: Path | None = None
    email_column: str | None = None
    table_name: str = "BaseProspection"
    sheet_name: str = "Base"
    extra_summary: list[tuple[str, object]] = field(default_factory=list)


class ScraperBase:
    #: Identifiant du vertical (slug)
    VERTICAL: str = "base"

    #: Nom de la table SQLite
    TABLE: str = "base"

    #: Colonnes métier, dans l'ordre d'export
    BUSINESS_COLUMNS: list[str] = []

    #: Colonne qui sert de clé naturelle (doit être unique dans le yield)
    NATURAL_KEY: str = "url"

    #: Configuration d'export par défaut
    EXPORT: ExportConfig | None = None

    def __init__(
        self,
        data_dir: Path = DATA_DIR,
        test_mode: bool = False,
    ) -> None:
        self.data_dir = data_dir
        self.test_mode = test_mode
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = data_dir / f"{self.VERTICAL}.db"
        self.log_path = data_dir / "logs" / f"{self.VERTICAL}.log"
        self.log = get_logger(f"scraper.{self.VERTICAL}", self.log_path)

        if self.EXPORT is None:
            raise ValueError(f"{self.VERTICAL}: EXPORT config manquante")

    # ------------------------------------------------------------------
    # Méthodes à implémenter par chaque scraper
    # ------------------------------------------------------------------

    def iter_records(self) -> Iterator[dict]:
        """À implémenter par chaque scraper concret."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Workflow principal : create / update passent tous les deux par ici.
    # La différence entre les deux modes est sémantique uniquement :
    # - create : on suppose la base vide (première exécution)
    # - update : on rejoue le scraping, upserte, marque stale les fiches non vues
    # Le code est identique : l'upsert se charge de la logique.
    # ------------------------------------------------------------------

    def run(self, mode: str = "update") -> db.UpsertResult:
        assert mode in {"create", "update"}, f"Mode invalide : {mode}"

        self.log.info("=" * 70)
        self.log.info("Démarrage scraper '%s' (mode=%s, test=%s)",
                      self.VERTICAL, mode, self.test_mode)
        self.log.info("DB : %s", self.db_path)

        with closing(db.connect(self.db_path)) as conn:
            db.init_table(conn, self.TABLE, self.BUSINESS_COLUMNS)
            run_id = db.start_run(conn, self.VERTICAL, mode)
            self.log.info("run_id = %s", run_id)

            result = db.UpsertResult()
            stale = 0
            error: str | None = None
            status = "ok"

            try:
                batch: list[tuple[str, dict]] = []
                BATCH_SIZE = 50  # commit toutes les 50 fiches

                for record in self.iter_records():
                    key_value = record.get(self.NATURAL_KEY)
                    if not key_value:
                        self.log.warning("Record sans clé naturelle, ignoré : %r",
                                         {k: record.get(k, '') for k in list(record)[:3]})
                        continue
                    batch.append((str(key_value), record))

                    if len(batch) >= BATCH_SIZE:
                        self._flush(conn, batch, run_id, result)
                        batch.clear()
                        self.log.info(
                            "flush : total=%d (inserted=%d updated=%d unchanged=%d)",
                            result.inserted + result.updated + result.unchanged,
                            result.inserted, result.updated, result.unchanged,
                        )

                if batch:
                    self._flush(conn, batch, run_id, result)

                if mode == "update":
                    stale = db.mark_stale(conn, self.TABLE, run_id)

            except KeyboardInterrupt:
                status = "interrupted"
                error = "KeyboardInterrupt"
                self.log.warning("Interruption clavier")
            except Exception as exc:
                status = "error"
                error = repr(exc)
                self.log.exception("Erreur pendant le scraping : %s", exc)
                raise
            finally:
                db.finish_run(conn, run_id, result, stale, status=status, error=error)
                self.log.info(
                    "Fin scraper '%s' : inserted=%d, updated=%d, unchanged=%d, stale=%d",
                    self.VERTICAL,
                    result.inserted,
                    result.updated,
                    result.unchanged,
                    stale,
                )

                # Export systématique après chaque run, même partiel
                self.export_files()

        return result

    def _flush(
        self,
        conn,
        batch: list[tuple[str, dict]],
        run_id: str,
        result: db.UpsertResult,
    ) -> None:
        with db.transaction(conn):
            for key, record in batch:
                status = db.upsert_row(
                    conn,
                    self.TABLE,
                    self.BUSINESS_COLUMNS,
                    key,
                    record,
                    run_id,
                )
                if status == "inserted":
                    result.inserted += 1
                elif status == "updated":
                    result.updated += 1
                else:
                    result.unchanged += 1

    # ------------------------------------------------------------------
    # Export (peut être appelé indépendamment du scraping)
    # ------------------------------------------------------------------

    def export_files(self) -> None:
        """Exporte la base active vers CSV/XLSX/JSONL selon la config."""
        if self.EXPORT is None:
            return

        with closing(db.connect(self.db_path)) as conn:
            db.init_table(conn, self.TABLE, self.BUSINESS_COLUMNS)
            rows = db.fetch_active_rows(conn, self.TABLE, self.BUSINESS_COLUMNS)

        cfg = self.EXPORT

        if cfg.csv_path:
            n = export_csv(rows, self.BUSINESS_COLUMNS, cfg.csv_path)
            self.log.info("CSV exporté : %s (%d lignes)", cfg.csv_path, n)

        if cfg.jsonl_path:
            n = export_jsonl(rows, cfg.jsonl_path)
            self.log.info("JSONL exporté : %s (%d lignes)", cfg.jsonl_path, n)

        if cfg.xlsx_path:
            summary = [
                ("Vertical", self.VERTICAL),
                ("Lignes actives", len(rows)),
                ("Fichier CSV", str(cfg.csv_path)),
            ] + cfg.extra_summary
            n = export_xlsx(
                rows,
                self.BUSINESS_COLUMNS,
                cfg.xlsx_path,
                sheet_name=cfg.sheet_name,
                table_name=cfg.table_name,
                email_column=cfg.email_column,
                summary=summary,
            )
            self.log.info("XLSX exporté : %s (%d lignes)", cfg.xlsx_path, n)

    # ------------------------------------------------------------------
    # Statut / inspection
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with closing(db.connect(self.db_path)) as conn:
            db.init_table(conn, self.TABLE, self.BUSINESS_COLUMNS)
            return {
                "vertical": self.VERTICAL,
                "db_path": str(self.db_path),
                **db.dump_debug(conn, self.TABLE),
            }
