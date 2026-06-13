"""
core/db.py
-----------

Couche d'accès SQLite avec upsert natif, tracking temporel et détection de
changements par hash. Une base par vertical pour isoler les scrapers.

Schema commun à toutes les tables :

- id                 : autoincrement
- natural_key        : clé naturelle UNIQUE (URL ou hash métier)
- content_hash       : SHA-1 des champs métier, sert à détecter les changements
- first_seen_at      : timestamp du premier insert (jamais modifié après)
- last_seen_at       : timestamp de la dernière fois où la fiche a été vue
- last_updated_at    : timestamp du dernier changement de données
- run_id             : id du dernier run qui a touché la ligne
- is_active          : 1 si la fiche a été vue au dernier run, 0 sinon
- ... + les colonnes métier du vertical

En plus, une table `scrape_runs` trace chaque exécution (mode, stats, erreur).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator


# ---------------------------------------------------------------------------
# Colonnes techniques communes à toutes les tables métier
# ---------------------------------------------------------------------------

TECH_COLUMNS = [
    "natural_key",
    "content_hash",
    "first_seen_at",
    "last_seen_at",
    "last_updated_at",
    "run_id",
    "is_active",
]


RUNS_DDL = """
CREATE TABLE IF NOT EXISTS scrape_runs (
    id                  TEXT PRIMARY KEY,
    vertical            TEXT NOT NULL,
    mode                TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    ended_at            TEXT,
    status              TEXT NOT NULL,
    records_inserted    INTEGER DEFAULT 0,
    records_updated     INTEGER DEFAULT 0,
    records_unchanged   INTEGER DEFAULT 0,
    records_stale       INTEGER DEFAULT 0,
    error               TEXT
);
"""


def now_iso() -> str:
    """Timestamp UTC ISO 8601, stable pour du stockage texte."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def content_hash(values: Iterable[object]) -> str:
    """
    Hash SHA-1 d'une liste de valeurs, sert à détecter les changements lors
    d'un upsert. Les None sont normalisés en chaîne vide pour éviter les
    faux négatifs.
    """
    payload = "\u241f".join("" if v is None else str(v) for v in values)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Connexion SQLite
# ---------------------------------------------------------------------------

def connect(db_path: Path) -> sqlite3.Connection:
    """
    Ouvre une connexion SQLite avec les bons pragmas pour un run long :
    - WAL : meilleure concurrence lecture/écriture
    - synchronous=NORMAL : bon compromis sécurité / perf
    - foreign_keys=ON : au cas où
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction explicite (on est en isolation_level=None)."""
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


# ---------------------------------------------------------------------------
# Initialisation d'une table métier
# ---------------------------------------------------------------------------

def init_table(
    conn: sqlite3.Connection,
    table: str,
    business_columns: list[str],
) -> None:
    """
    Crée la table métier si elle n'existe pas, avec colonnes techniques +
    colonnes métier. Idempotent : si de nouvelles colonnes métier
    apparaissent, on fait un ALTER TABLE ADD COLUMN pour chaque nouvelle.
    """
    cols_sql = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "natural_key TEXT NOT NULL UNIQUE",
        "content_hash TEXT",
        "first_seen_at TEXT",
        "last_seen_at TEXT",
        "last_updated_at TEXT",
        "run_id TEXT",
        "is_active INTEGER DEFAULT 1",
    ]
    cols_sql.extend(f'"{c}" TEXT' for c in business_columns)

    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(cols_sql)});")

    # Migration additive : si business_columns évolue, on ajoute les manquantes
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table});").fetchall()
    }
    for col in business_columns:
        if col not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN "{col}" TEXT;')

    # Index utiles
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_last_seen "
        f"ON {table}(last_seen_at);"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{table}_is_active "
        f"ON {table}(is_active);"
    )

    conn.execute(RUNS_DDL)


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class UpsertResult:
    """Compteurs agrégés pour le rapport de run."""

    __slots__ = ("inserted", "updated", "unchanged")

    def __init__(self) -> None:
        self.inserted = 0
        self.updated = 0
        self.unchanged = 0

    def merge(self, other: "UpsertResult") -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged

    def __repr__(self) -> str:
        return (
            f"UpsertResult(inserted={self.inserted}, "
            f"updated={self.updated}, unchanged={self.unchanged})"
        )


def upsert_row(
    conn: sqlite3.Connection,
    table: str,
    business_columns: list[str],
    natural_key: str,
    values: dict[str, object],
    run_id: str,
    now: str | None = None,
) -> str:
    """
    Upsert une ligne dans la table. Retourne l'un de :
    - 'inserted'  : ligne nouvelle
    - 'updated'   : ligne existante avec au moins un champ métier modifié
    - 'unchanged' : ligne existante, hash identique

    Stratégie :
    1. On calcule le hash des colonnes métier.
    2. On lit l'existant (s'il y en a).
    3. Si nouveau : INSERT complet avec first_seen_at = now.
    4. Si existant + hash identique : on met à jour uniquement last_seen_at,
       is_active et run_id.
    5. Si existant + hash différent : on met à jour toutes les colonnes
       métier + last_updated_at + last_seen_at.

    Cette approche est plus lisible qu'un gros INSERT ... ON CONFLICT et
    permet un comptage exact des 3 cas.
    """
    now = now or now_iso()
    biz_values = [values.get(c, "") for c in business_columns]
    chash = content_hash(biz_values)

    existing = conn.execute(
        f"SELECT content_hash, first_seen_at FROM {table} WHERE natural_key = ?",
        (natural_key,),
    ).fetchone()

    if existing is None:
        placeholders = ", ".join(["?"] * (len(business_columns) + 7))
        cols = (
            ["natural_key", "content_hash", "first_seen_at", "last_seen_at",
             "last_updated_at", "run_id", "is_active"]
            + business_columns
        )
        cols_sql = ", ".join(f'"{c}"' for c in cols)
        row = (
            [natural_key, chash, now, now, now, run_id, 1]
            + biz_values
        )
        conn.execute(
            f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders});",
            row,
        )
        return "inserted"

    if existing["content_hash"] == chash:
        conn.execute(
            f"UPDATE {table} SET last_seen_at = ?, run_id = ?, is_active = 1 "
            f"WHERE natural_key = ?;",
            (now, run_id, natural_key),
        )
        return "unchanged"

    set_clauses = ", ".join(f'"{c}" = ?' for c in business_columns)
    params = list(biz_values) + [
        chash,
        now,
        now,
        run_id,
        natural_key,
    ]
    conn.execute(
        f"UPDATE {table} SET {set_clauses}, content_hash = ?, "
        f"last_seen_at = ?, last_updated_at = ?, run_id = ?, is_active = 1 "
        f"WHERE natural_key = ?;",
        params,
    )
    return "updated"


def mark_stale(
    conn: sqlite3.Connection,
    table: str,
    run_id: str,
) -> int:
    """
    À appeler en fin de run UPDATE : toute ligne non vue pendant ce run
    (run_id différent) passe en is_active = 0. Elle reste en base pour
    l'historique, mais ne sera pas exportée par défaut.

    Retourne le nombre de fiches marquées stale.
    """
    cur = conn.execute(
        f"UPDATE {table} SET is_active = 0 WHERE run_id IS NOT ?;",
        (run_id,),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Suivi des runs
# ---------------------------------------------------------------------------

def start_run(
    conn: sqlite3.Connection,
    vertical: str,
    mode: str,
) -> str:
    """Crée une entrée dans scrape_runs et renvoie son id."""
    conn.execute(RUNS_DDL)
    run_id = new_run_id()
    conn.execute(
        "INSERT INTO scrape_runs (id, vertical, mode, started_at, status) "
        "VALUES (?, ?, ?, ?, 'running');",
        (run_id, vertical, mode, now_iso()),
    )
    return run_id


def finish_run(
    conn: sqlite3.Connection,
    run_id: str,
    result: UpsertResult,
    stale: int,
    status: str = "ok",
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE scrape_runs SET ended_at = ?, status = ?, "
        "records_inserted = ?, records_updated = ?, records_unchanged = ?, "
        "records_stale = ?, error = ? WHERE id = ?;",
        (
            now_iso(),
            status,
            result.inserted,
            result.updated,
            result.unchanged,
            stale,
            error,
            run_id,
        ),
    )


# ---------------------------------------------------------------------------
# Lecture pour export
# ---------------------------------------------------------------------------

def fetch_active_rows(
    conn: sqlite3.Connection,
    table: str,
    business_columns: list[str],
    include_all: bool = False,
) -> list[dict]:
    """
    Retourne toutes les lignes actives (ou toutes si include_all=True) en
    conservant l'ordre des colonnes métier + colonnes techniques utiles.
    """
    cols = business_columns + [
        "first_seen_at",
        "last_seen_at",
        "last_updated_at",
        "is_active",
    ]
    cols_sql = ", ".join(f'"{c}"' for c in cols)
    where = "" if include_all else "WHERE is_active = 1"
    rows = conn.execute(
        f"SELECT {cols_sql} FROM {table} {where} ORDER BY last_seen_at DESC;"
    ).fetchall()
    return [dict(r) for r in rows]


def run_summary(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?;",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def dump_debug(conn: sqlite3.Connection, table: str) -> dict:
    """Petit helper pour inspecter une base sans SQL."""
    total = conn.execute(f"SELECT COUNT(*) AS n FROM {table};").fetchone()["n"]
    active = conn.execute(
        f"SELECT COUNT(*) AS n FROM {table} WHERE is_active = 1;"
    ).fetchone()["n"]
    stale = total - active
    last_run = conn.execute(
        "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 1;"
    ).fetchone()
    return {
        "total": total,
        "active": active,
        "stale": stale,
        "last_run": dict(last_run) if last_run else None,
    }
