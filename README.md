# Prospection Bases

Système unifié de scraping pour construire et maintenir à jour 5 bases de prospection B2B.
Logique de scraping strictement identique à tes notebooks d'origine, mais industrialisée :
stockage SQLite, **upsert** natif avec détection de changements, exécution hebdo
automatisable.

## Architecture

```
prospection_bases/
├── core/
│   ├── db.py              # SQLite + upsert + mark_stale + scrape_runs
│   ├── utils.py           # emails, téléphones, URLs, cfemail (identique aux notebooks)
│   ├── export.py          # CSV / XLSX stylé / JSONL
│   └── scraper_base.py    # classe de base : run → iter_records → upsert → export
├── scrapers/
│   ├── auto_ecole.py     # auto-ecole.info                     (clé: url_fiche)
│   ├── ecommerce.py       # annuaire-du-ecommerce.com         (clé: shop_url)
│   ├── education.py       # education.gouv.fr/annuaire         (clé: url_fiche)
│   ├── france_travail.py  # candidat.francetravail.fr          (clé: url_detail, Playwright)
│   ├── hotels.py          # trouve-ton-hotel.fr                 (clé: hash nom+adresse+tel+email)
│   └── immo.py            # immomatin.com                       (clé: detail_url)
├── data/
│   ├── auto_ecole.db     # une base SQLite par vertical
│   ├── ecommerce.db
│   ├── education.db
│   ├── ...
│   ├── logs/              # logs par vertical + cli + weekly
│   └── reports/           # récaps JSON horodatés des runs hebdo
├── exports/
│   ├── auto_ecole/       # CSV / XLSX / JSONL par vertical
│   ├── ecommerce/
│   ├── education/
│   └── ...
├── cli.py                 # CLI unifié
├── run_weekly.py          # entrée hebdo (cron / GitHub Actions)
├── requirements.txt
└── .github/workflows/weekly.yml
```

## Installation

```bash
pip install -r requirements.txt

# Pour le scraper France Travail uniquement
python -m playwright install chromium
```

## Commandes

### CLI unifié

```bash
# Lister les verticals
python cli.py list

# Premier run (création initiale, mode test réduit)
python cli.py create hotels --test

# Run hebdo (upsert) sur un vertical
python cli.py update hotels

# Run hebdo sur TOUS les verticals
python cli.py all update

# Ré-exporter CSV/XLSX depuis la base (sans re-scraper)
python cli.py export hotels

# Statut d'un vertical (total / actives / stale / dernier run)
python cli.py status hotels

# Statut de tous les verticals (JSON)
python cli.py status
```

### Exécution hebdo

```bash
# En local (cron Linux / Task Scheduler Windows)
python run_weekly.py

# Avec variables d'environnement
PB_TEST=1 python run_weekly.py                       # mode test
PB_VERTICALS=hotels,immo python run_weekly.py        # sous-ensemble
```

Le script produit un rapport JSON horodaté dans `data/reports/weekly_<timestamp>.json`
avec le nombre d'insertions / mises à jour / inchangés par vertical.
Code de sortie `0` si tout est OK, `1` si au moins un scraper a planté.

### GitHub Actions

Le workflow `.github/workflows/weekly.yml` lance `run_weekly.py` tous les lundis à 6h UTC
et committe les exports CSV/XLSX dans le repo. À activer :

1. Push le repo sur GitHub
2. Aller dans `Settings → Actions → General` → autoriser les writes
3. Éventuellement lancer manuellement via `workflow_dispatch`

## Comment l'upsert fonctionne

Chaque vertical a sa **table SQLite** avec les colonnes métier + les colonnes techniques :

| Colonne | Rôle |
|---|---|
| `natural_key` | clé unique (URL en général) |
| `content_hash` | SHA-1 des champs métier, sert à détecter les changements |
| `first_seen_at` | timestamp du 1er insert (jamais modifié après) |
| `last_seen_at` | timestamp de la dernière fois où la fiche a été vue |
| `last_updated_at` | timestamp du dernier changement de données |
| `run_id` | id du dernier run qui a touché la ligne |
| `is_active` | 1 si vue au dernier run, 0 sinon |

À chaque run `update` :

1. Pour chaque fiche scrapée, on calcule son `content_hash`.
2. Si la fiche **n'existe pas** → `INSERT` avec `first_seen_at = now`. Compté comme **inserted**.
3. Si elle existe et le hash est **identique** → on met à jour uniquement
   `last_seen_at` et `run_id`. Compté comme **unchanged**.
4. Si elle existe et le hash est **différent** → on met à jour tous les champs
   métier + `last_updated_at = now`. Compté comme **updated**.
5. En fin de run, toute fiche non vue (run_id différent) passe en `is_active = 0`
   (**stale**). Elle reste en base pour l'historique mais n'est pas exportée.

Chaque exécution est tracée dans la table `scrape_runs` (mode, timestamps, stats, erreur).

## Exports

Après chaque run, les exports sont régénérés **depuis la DB** (donc toujours
cohérents avec l'état upserté) :

- **CSV** UTF-8 BOM → compatible Excel FR
- **XLSX** stylé → en-tête bleu LPB-like, zébrage, colonne email surlignée,
  table nommée, figé ligne 1, feuille "Résumé"
- **JSONL** → 1 ligne JSON par fiche, utile pour feed Fabric Lakehouse

Les noms de fichiers et colonnes sont **identiques à tes notebooks d'origine**
pour rester compatibles avec tes pipelines aval.

## Mode test

Chaque scraper a un `test_mode=True` qui limite le volume :
- Auto-école : 2 départements max
- Ecommerce : 2 catégories max
- Education : 2 pages max
- France Travail : 1 département (69), 2 pages max, 10 enrichissements max
- Hotels : 3 départements max
- Immo : 2 pages max

Utile pour valider une modif avant de lancer un run complet.

## Statut / observabilité

```bash
python cli.py status hotels
```

Retourne :

```json
{
  "hotels": {
    "vertical": "hotels",
    "db_path": "/.../data/hotels.db",
    "total": 4823,
    "active": 4810,
    "stale": 13,
    "last_run": {
      "id": "abc123...",
      "mode": "update",
      "started_at": "2026-04-21 06:00:12",
      "ended_at": "2026-04-21 06:47:23",
      "status": "ok",
      "records_inserted": 42,
      "records_updated": 318,
      "records_unchanged": 4450,
      "records_stale": 13
    }
  }
}
```

## FAQ

**Est-ce que je perds des données si une fiche disparaît d'un site ?**
Non. Elle passe `is_active=0` mais reste en base. Tu peux l'interroger via SQL :
```sql
SELECT * FROM hotels WHERE is_active = 0;
```

**Comment ré-activer une fiche stale ?**
Automatiquement si elle réapparaît lors d'un run suivant (son `run_id` change et
`is_active` repasse à 1).

**Et si je veux supprimer définitivement une fiche ?**
```sql
DELETE FROM hotels WHERE natural_key = '...';
```

**Comment j'inspecte une base sans CLI ?**
```bash
sqlite3 data/hotels.db
sqlite> SELECT COUNT(*) FROM hotels WHERE is_active = 1;
sqlite> SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 5;
```

**Comment j'ajoute un nouveau vertical ?**
1. Crée `scrapers/mon_vertical.py` qui hérite de `ScraperBase`
2. Définis `VERTICAL`, `TABLE`, `NATURAL_KEY`, `BUSINESS_COLUMNS`, `EXPORT`
3. Implémente `iter_records()` qui yield des dicts
4. Ajoute-le dans `scrapers/__init__.py` au `REGISTRY`

Le reste (upsert, export, logs, runs tracking) est géré automatiquement.

## Structure des données en base

Toutes les colonnes sont stockées en `TEXT` pour la simplicité. La logique métier
(comptages, pourcentages, filtres) est faite à l'export ou en SQL direct.

Indices automatiques sur `last_seen_at` et `is_active` pour les requêtes de pilotage.

## Verticals couverts

| Vertical | Source | Clé naturelle | Volume estimé |
|---|---|---|---|
| auto_ecole | auto-ecole.info | `url_fiche` | ~6 500 auto-écoles |
| ecommerce | annuaire-du-ecommerce.com | `shop_url` | ~10k boutiques |
| education | education.gouv.fr/annuaire | `url_fiche` | ~60k établissements |
| france_travail | candidat.francetravail.fr | `url_detail` | ~100k formations |
| hotels | trouve-ton-hotel.fr | hash métier | ~5k hôtels |
| immo | immomatin.com | `detail_url` | ~10k agences |

Les volumes en mode `test_mode=False` peuvent prendre plusieurs heures
(surtout France Travail, limité par le rendering Playwright).
