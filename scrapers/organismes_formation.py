"""
scrapers/organismes_formation.py
---------------------------------

Source : Liste publique des organismes de formation (data.gouv.fr, ministère
du Travail, mise à jour quotidienne). Environ 100 000 organismes déclarés,
avec Qualiopi et volumes d'activité, mais SANS email ni site web.

Pipeline par run (borné en temps et en volume) :
  1. Téléchargement du CSV officiel (≈ 35 Mo) dans un cache temporaire.
  2. Sélection des cibles : Qualiopi (formation ou apprentissage), au moins
     OF_MIN_STAGIAIRES stagiaires/an, hors structures publiques, jamais
     traitées (clé naturelle = NDA). Tri : les plus gros organismes d'abord.
  3. Enrichissement de chaque cible :
       - recherche web (DuckDuckGo HTML) "<dénomination> <ville> formation"
       - on écarte annuaires et réseaux sociaux, on garde 2 sites candidats
       - visite accueil + contact + mentions légales, extraction des emails
       - validation du site : SIREN / NDA présent dans les pages, OU nom de
         l'organisme dans le domaine, OU nom dans le texte (anti faux positifs)
  4. Upsert SQLite + exports (CSV lu par emailing/build_master.py).

Chaque organisme traité est enregistré (avec ou sans email) : il ne sera
plus jamais retraité. Si le moteur de recherche bloque, les organismes non
traités ne sont PAS enregistrés : ils seront retentés au run suivant.

Clé naturelle : `nda` (numéro de déclaration d'activité).

Env (toutes optionnelles) :
  OF_CSV_URL          URL du CSV (défaut : ressource data.gouv officielle)
  OF_CSV_CACHE        chemin du cache (défaut /tmp/liste_publique_of.csv)
  OF_MIN_STAGIAIRES   seuil de stagiaires/an (défaut 30)
  OF_QUALIOPI_ONLY    "true"/"false" (défaut true)
  OF_PROBE_WORKERS    visites de sites en parallèle (défaut 4)

Arrêt du run (le premier atteint) : objectif d'emails trouvés (target_emails),
plafond d'organismes (max_per_run), budget temps, moteur de recherche bloqué.
"""

from __future__ import annotations

import csv
import os
import random
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import clean_text, decode_cfemail, extract_emails, pick_best_email


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTE_OF_URL = os.getenv("OF_CSV_URL") or (
    "https://www.data.gouv.fr/api/1/datasets/r/ac59a0f5-fa83-4b82-bf12-3c5806d4f19f"
)
CSV_CACHE = Path(os.getenv("OF_CSV_CACHE") or "/tmp/liste_publique_of.csv")
CSV_CACHE_MAX_AGE_H = 20

MIN_STAGIAIRES = int(os.getenv("OF_MIN_STAGIAIRES") or 30)
QUALIOPI_ONLY = (os.getenv("OF_QUALIOPI_ONLY") or "true").strip().lower() in {
    "1", "true", "yes", "oui", "on",
}

SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_SLEEP_MIN = 2.0
SEARCH_SLEEP_MAX = 4.5
SITE_SLEEP_MIN = 0.3
SITE_SLEEP_MAX = 0.8
SITE_TIMEOUT = 12
MAX_SITES_PER_OF = 2
MAX_PAGES_PER_SITE = 4
SEARCH_FAIL_STREAK_STOP = 3   # recherches bloquées d'affilée → arrêt propre

# Les visites de sites (lentes : 4 pages × réseau) tournent en parallèle
# pendant que la recherche suivante part : le débit n'est plus limité que
# par le rythme volontairement lent des recherches (anti-blocage).
PROBE_WORKERS = max(1, int(os.getenv("OF_PROBE_WORKERS") or 4))

CONTACT_PATHS = ["/contact", "/nous-contacter", "/mentions-legales", "/contactez-nous"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Structures publiques ou hors cible (le pitch vise des organismes privés
# qui gèrent eux-mêmes leurs demandes d'information).
EXCLUDED_NAME = re.compile(
    r"\b(lycee|college|universite|academie|rectorat|ministere|prefecture|"
    r"centre hospitalier|chu|hopital|mairie|conseil departemental|"
    r"conseil regional|ecole nationale|ehpad)\b"
)

# Domaines à ne jamais prendre pour le site de l'organisme.
DIRECTORY_DOMAINS = (
    "societe.com", "pappers.fr", "verif.com", "infogreffe", "annuaire",
    "pagesjaunes", "manageo", "lefigaro.fr", "kompass", "corporama",
    "dirigeant", "societe.ninja", "b-reputation", "118712", "infonet",
    "score3", "rubypayeur", "data.gouv.fr", "gouv.fr", "francecompetences",
    "moncompteformation", "intercariforef", "carif", "onisep", "francetravail",
    "pole-emploi", "indeed", "welcometothejungle", "hellowork", "linkedin",
    "facebook", "instagram", "twitter", "x.com", "youtube", "tiktok",
    "wikipedia", "google.", "bing.", "duckduckgo", "yelp", "mappy",
    "kelformation", "formation-continue", "trouver-une-formation",
    "orientation", "qualiopi", "certif", "doctolib", "leboncoin",
    "studyrama", "letudiant", "diplomeo", "emploi", "cadremploi",
    "monster", "jobteaser", "glassdoor", "trustpilot", "avis",
)

GENERIC_PROVIDERS = (
    "gmail.com", "orange.fr", "wanadoo.fr", "free.fr", "hotmail.fr",
    "hotmail.com", "outlook.fr", "outlook.com", "yahoo.fr", "yahoo.com",
    "laposte.net", "sfr.fr", "neuf.fr", "live.fr", "icloud.com", "aol.com",
)

# Emails de prestataires techniques, jamais ceux de l'organisme.
TECH_EMAIL_DOMAINS = (
    "wix.com", "wixpress.com", "sentry", "godaddy", "ovh.", "o2switch",
    "squarespace", "wordpress", "jimdo", "webflow", "hubspot", "mailchimp",
    "sendinblue", "brevo", "cloudflare", "gandi", "ionos", "1and1",
    "example", "domain.com", "votredomaine", "yourdomain",
)

NAME_STOPWORDS = {
    "sarl", "sas", "sasu", "eurl", "sa", "sci", "scop", "sc", "selarl",
    "association", "asso", "formation", "formations", "centre", "center",
    "institut", "ecole", "groupe", "group", "france", "conseil", "conseils",
    "organisme", "cabinet", "societe", "entreprise", "services", "service",
    "de", "du", "des", "la", "le", "les", "et", "en", "pour", "au", "aux",
    "sur", "par", "une", "un", "l", "d", "s", "ste", "the", "and",
}

# Colonnes du CSV officiel : on matche par nom normalisé (minuscules, sans
# ponctuation) pour résister aux variations de casse/séparateurs.
COLUMN_CANDIDATES = {
    "nda": ["numerodeclarationactivite", "nda", "numerodeclaration"],
    "denomination": ["denomination", "raisonsociale"],
    "siret": ["siretetablissementdeclarant", "siret"],
    "code_postal": ["adressephysiqueorganismeformationcodepostal", "codepostal"],
    "ville": ["adressephysiqueorganismeformationville", "ville"],
    "region": ["adressephysiqueorganismeformationcoderegion", "coderegion"],
    "q_formation": ["certificationsactionsdeformation", "actionsdeformation"],
    "q_apprentissage": ["certificationsactionsdeformationparapprentissage",
                        "actionsdeformationparapprentissage"],
    "q_bilan": ["certificationsbilansdecompetences", "bilansdecompetences"],
    "q_vae": ["certificationsvae"],
    "nb_stagiaires": ["informationsdeclareesnbstagiaires", "nbstagiaires"],
    "nb_formateurs": ["informationsdeclareeseffectifformateurs", "effectifformateurs"],
    "specialite": ["informationsdeclareesspecialitesdeformationlibellespecialite1",
                   "libellespecialite1"],
}
REQUIRED_COLUMNS = ("nda", "denomination")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Minuscules, sans accents."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _norm(h))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in {"true", "vrai", "1", "oui", "yes", "o", "x"}


def _to_int(v: str) -> int:
    d = _digits(v)
    return int(d) if d else 0


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().split(":")[0].removeprefix("www.")
    except Exception:
        return ""


def _name_tokens(denomination: str) -> list[str]:
    toks = re.split(r"[^a-z0-9]+", _norm(denomination))
    return [t for t in toks if len(t) >= 4 and t not in NAME_STOPWORDS]


def _same_site(email_domain: str, site_domain: str) -> bool:
    """contact@x.fr ↔ x.fr ou sous-domaine (pas de simple suffixe textuel :
    formation.fr ne doit PAS matcher abc-formation.fr)."""
    a, b = email_domain.lower(), site_domain.lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _is_directory(domain: str) -> bool:
    return any(d in domain for d in DIRECTORY_DOMAINS)


def map_columns(headers: list[str]) -> dict[str, str]:
    """{champ_interne: entête_réelle}. Égalité exacte d'abord, puis suffixe."""
    normed = {_norm_header(h): h for h in headers}
    out: dict[str, str] = {}
    for field, cands in COLUMN_CANDIDATES.items():
        for c in cands:
            if c in normed:
                out[field] = normed[c]
                break
        else:
            for c in cands:
                hit = next((orig for n, orig in normed.items() if n.endswith(c)), None)
                if hit:
                    out[field] = hit
                    break
    return out


def decode_ddg_href(href: str) -> str:
    """Les résultats DuckDuckGo HTML passent par //duckduckgo.com/l/?uddg=<url>."""
    if not href:
        return ""
    if "uddg=" in href:
        q = parse_qs(urlparse(href if "://" in href else "https:" + href).query)
        return unquote(q.get("uddg", [""])[0])
    if href.startswith("//"):
        return "https:" + href
    return href


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class OrganismesFormationScraper(ScraperBase):
    VERTICAL = "organismes_formation"
    TABLE = "organismes_formation"
    NATURAL_KEY = "nda"

    BUSINESS_COLUMNS = [
        "nda", "siret", "denomination", "code_postal", "ville", "region",
        "qualiopi", "nb_stagiaires", "nb_formateurs", "specialite",
        "site_web", "email_principal", "emails_trouves", "email_source",
        "statut", "date_scraping",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/organismes_formation/organismes_formation.csv"),
        xlsx_path=Path("exports/organismes_formation/organismes_formation.xlsx"),
        jsonl_path=Path("exports/organismes_formation/organismes_formation.jsonl"),
        email_column="email_principal",
        table_name="BaseOrganismesFormation",
        sheet_name="OrganismesFormation",
    )

    def __init__(self, data_dir=None, test_mode=False, max_per_run=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)

        self.max_per_run = max_per_run or (10 if test_mode else 150)
        self.target_emails = 0        # objectif d'emails trouvés, 0 = aucun
        self.time_budget = 0          # secondes, 0 = illimité
        self._deadline: float | None = None
        self.stop_reason = ""
        self.stats = {"traites": 0, "emails": 0, "sans_email": 0,
                      "introuvables": 0, "cibles_restantes": 0}

        # Session principale : recherche web (thread principal uniquement).
        self._session = self._new_session()
        # Une session par thread pour les visites de sites (les sessions
        # HTTP ne sont pas thread-safe).
        self._tls = threading.local()
        self._search_fail_streak = 0

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    @staticmethod
    def _new_session():
        sess = cffi_requests.Session() if HAS_CFFI else requests.Session()
        sess.headers.update(HEADERS)
        return sess

    def _site_session(self):
        sess = getattr(self._tls, "session", None)
        if sess is None:
            sess = self._tls.session = self._new_session()
        return sess

    def _get(self, url: str, timeout: int = SITE_TIMEOUT) -> str | None:
        sess = self._site_session()
        try:
            if HAS_CFFI:
                r = sess.get(url, impersonate="chrome", timeout=timeout,
                             allow_redirects=True)
            else:
                r = sess.get(url, timeout=timeout, allow_redirects=True)
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code == 200 and ("html" in ctype or not ctype):
                return r.text
        except Exception:
            pass
        return None

    def _time_left(self) -> float:
        if self._deadline is None:
            return float("inf")
        return self._deadline - time.monotonic()

    # ------------------------------------------------------------------
    # 1. Liste officielle
    # ------------------------------------------------------------------

    def _download_csv(self) -> Path:
        if CSV_CACHE.exists():
            age_h = (time.time() - CSV_CACHE.stat().st_mtime) / 3600
            if age_h < CSV_CACHE_MAX_AGE_H:
                self.log.info("Liste OF : cache réutilisé (%s, %.1f h)", CSV_CACHE, age_h)
                return CSV_CACHE
        self.log.info("Liste OF : téléchargement %s", LISTE_OF_URL)
        CSV_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(LISTE_OF_URL, headers=HEADERS, timeout=120,
                          stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            tmp = CSV_CACHE.with_suffix(".part")
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            tmp.replace(CSV_CACHE)
        self.log.info("Liste OF : %.1f Mo", CSV_CACHE.stat().st_size / 1e6)
        return CSV_CACHE

    def _read_rows(self, path: Path) -> Iterator[dict]:
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        first = text.split("\n", 1)[0]
        delim = ";" if first.count(";") >= first.count(",") else ","
        reader = csv.DictReader(text.splitlines(), delimiter=delim)
        self._colmap = map_columns(reader.fieldnames or [])
        missing = [c for c in REQUIRED_COLUMNS if c not in self._colmap]
        if missing:
            raise RuntimeError(
                f"Colonnes introuvables {missing} dans la liste OF. "
                f"Entêtes reçues : {reader.fieldnames}"
            )
        self.log.info("Colonnes mappées : %s", self._colmap)
        yield from reader

    def _field(self, row: dict, name: str) -> str:
        col = self._colmap.get(name)
        return clean_text(row.get(col, "")) if col else ""

    def select_targets(self, rows: Iterator[dict]) -> list[dict]:
        """Filtre + tri des organismes à traiter ce run."""
        targets: dict[str, dict] = {}
        n_total = 0
        for row in rows:
            n_total += 1
            nda = _digits(self._field(row, "nda"))
            name = self._field(row, "denomination")
            if not nda or not name or nda in targets:
                continue
            if self.skip_keys and nda in self.skip_keys:
                self.skipped_known += 1
                continue
            if EXCLUDED_NAME.search(_norm(name)):
                continue

            cats = []
            if _truthy(self._field(row, "q_formation")):
                cats.append("formation")
            if _truthy(self._field(row, "q_apprentissage")):
                cats.append("apprentissage")
            if _truthy(self._field(row, "q_bilan")):
                cats.append("bilan")
            if _truthy(self._field(row, "q_vae")):
                cats.append("vae")
            if QUALIOPI_ONLY and not ({"formation", "apprentissage"} & set(cats)):
                continue

            nb = _to_int(self._field(row, "nb_stagiaires"))
            if nb < MIN_STAGIAIRES:
                continue

            targets[nda] = {
                "nda": nda,
                "siret": _digits(self._field(row, "siret")),
                "denomination": name,
                "code_postal": self._field(row, "code_postal"),
                "ville": self._field(row, "ville"),
                "region": self._field(row, "region"),
                "qualiopi": ";".join(cats),
                "nb_stagiaires": str(nb),
                "nb_formateurs": self._field(row, "nb_formateurs"),
                "specialite": self._field(row, "specialite"),
            }

        ordered = sorted(targets.values(), key=lambda t: -int(t["nb_stagiaires"]))
        self.stats["cibles_restantes"] = len(ordered)
        self.log.info(
            "Liste OF : %d lignes | %d déjà traités | %d cibles restantes "
            "(Qualiopi=%s, ≥%d stagiaires)",
            n_total, self.skipped_known, len(ordered), QUALIOPI_ONLY, MIN_STAGIAIRES,
        )
        return ordered[: self.max_per_run]

    # ------------------------------------------------------------------
    # 2. Recherche du site
    # ------------------------------------------------------------------

    def _search(self, query: str) -> list[str] | None:
        """URLs de résultats, [] si aucun, None si le moteur bloque."""
        try:
            if HAS_CFFI:
                r = self._session.post(SEARCH_URL, data={"q": query, "kl": "fr-fr"},
                                       impersonate="chrome", timeout=20)
            else:
                r = self._session.post(SEARCH_URL, data={"q": query, "kl": "fr-fr"},
                                       timeout=20)
        except Exception as e:
            self.log.warning("Recherche KO (%r)", e)
            return None
        if r.status_code != 200:
            self.log.warning("Recherche HTTP %s", r.status_code)
            return None
        soup = BeautifulSoup(r.text, "lxml")
        links = [decode_ddg_href(a.get("href", "")) for a in soup.select("a.result__a")]
        links = [u for u in links if u.startswith("http")]
        if not links and ("anomaly" in r.text.lower() or "captcha" in r.text.lower()):
            self.log.warning("Recherche bloquée (anti-robot)")
            return None
        return links

    def _candidate_sites(self, of: dict) -> list[str] | None:
        query = f"{of['denomination']} {of['ville']} formation".strip()
        results = self._search(query)
        if results is None:
            self._search_fail_streak += 1
            return None
        self._search_fail_streak = 0
        sites, seen = [], set()
        for url in results:
            d = _domain(url)
            if not d or d in seen or _is_directory(d):
                continue
            seen.add(d)
            p = urlparse(url)
            sites.append(f"{p.scheme}://{p.netloc}")
            if len(sites) >= MAX_SITES_PER_OF:
                break
        return sites

    # ------------------------------------------------------------------
    # 3. Emails sur le site + validation
    # ------------------------------------------------------------------

    def _page_emails(self, html: str) -> tuple[list[str], str]:
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        emails = extract_emails(html + " " + text)
        for a in soup.select("a[href^='mailto:']"):
            e = a["href"][7:].split("?")[0].strip().lower()
            if e and e not in emails:
                emails.insert(0, e)
        for el in soup.select("[data-cfemail]"):
            dec = decode_cfemail(el.get("data-cfemail"))
            if dec and dec not in emails:
                emails.insert(0, dec)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return emails, f"{title} {text}"

    def _probe_site(self, root: str, of: dict) -> dict | None:
        site_dom = _domain(root)
        tokens = _name_tokens(of["denomination"])
        ids = [x for x in (of["siret"][:9], of["nda"]) if len(x) >= 9]

        all_emails: list[str] = []
        corpus = ""
        for i, url in enumerate([root] + [root + p for p in CONTACT_PATHS]):
            if i >= MAX_PAGES_PER_SITE:
                break
            html = self._get(url)
            if html is None:
                if i == 0:
                    return None          # site mort
                continue
            emails, text = self._page_emails(html)
            corpus += " " + text
            for e in emails:
                if e not in all_emails:
                    all_emails.append(e)
            if i >= 1 and any(_same_site(e.split("@")[-1], site_dom) for e in all_emails):
                break                     # email du domaine trouvé, assez de preuves
            time.sleep(random.uniform(SITE_SLEEP_MIN, SITE_SLEEP_MAX))

        ncorpus = _norm(corpus)
        dcorpus = _digits(corpus)
        flat_dom = re.sub(r"[^a-z0-9]", "", site_dom)
        id_match = any(x in dcorpus for x in ids)
        dom_match = any(t in flat_dom for t in tokens)
        name_hits = sum(1 for t in tokens if t in ncorpus)
        name_match = bool(tokens) and name_hits >= min(2, len(tokens))
        if not (id_match or dom_match or name_match):
            return {"site": root, "valid": False, "emails": []}

        def ok(e: str) -> bool:
            d = e.split("@")[-1]
            if any(t in d for t in TECH_EMAIL_DOMAINS):
                return False
            if _same_site(d, site_dom):
                return True
            if d in GENERIC_PROVIDERS:
                return id_match or name_match
            return any(t in re.sub(r"[^a-z0-9]", "", d) for t in tokens)

        kept = [e for e in all_emails if ok(e)]
        return {"site": root, "valid": True, "emails": kept}

    # ------------------------------------------------------------------
    # iter_records : appelé par ScraperBase.run()
    # ------------------------------------------------------------------

    def _enrich(self, of: dict, sites: list[str]) -> dict:
        """Visite les sites candidats et construit la ligne (thread worker)."""
        best = None
        for root in sites:
            res = self._probe_site(root, of)
            if res and res["valid"]:
                best = res
                if res["emails"]:
                    break

        emails = best["emails"] if best else []
        if emails:
            statut = "ok"
        elif best:
            statut = "site_sans_email"
        else:
            statut = "introuvable"

        row = {col: "" for col in self.BUSINESS_COLUMNS}
        row.update(of)
        row.update({
            "site_web": best["site"] if best else "",
            "email_principal": pick_best_email(emails),
            "emails_trouves": ";".join(emails),
            "email_source": "site" if emails else "",
            "statut": statut,
            "date_scraping": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return row

    def _collect(self, futures: set, block: bool) -> Iterator[dict]:
        """Rend les lignes terminées (thread principal : stats sans course)."""
        if not futures:
            return
        if block:
            done, _ = wait(futures)
        else:
            done = {f for f in futures if f.done()}
        for fut in done:
            futures.discard(fut)
            try:
                row = fut.result()
            except Exception as e:          # ne doit pas arriver, sécurité
                self.log.warning("[OF] visite KO : %r", e)
                continue
            statut = row["statut"]
            if statut == "ok":
                self.stats["emails"] += 1
            elif statut == "site_sans_email":
                self.stats["sans_email"] += 1
            else:
                self.stats["introuvables"] += 1
            self.stats["traites"] += 1
            self.log.info("[OF] %d | %-15s | %s | %s", self.stats["traites"], statut,
                          row["email_principal"] or "-", row["denomination"][:60])
            yield row

    def _target_reached(self) -> bool:
        return bool(self.target_emails) and self.stats["emails"] >= self.target_emails

    def iter_records(self) -> Iterator[dict]:
        if self.time_budget:
            self._deadline = time.monotonic() + self.time_budget
        targets = self.select_targets(self._read_rows(self._download_csv()))
        total = len(targets)
        self.log.info(
            "ENRICHISSEMENT : %d organismes max | objectif %s emails | budget %ss "
            "| %d visites en parallèle",
            total, self.target_emails or "∞", self.time_budget or "∞", PROBE_WORKERS,
        )

        futures: set = set()
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            for i, of in enumerate(targets, 1):
                yield from self._collect(futures, block=False)

                if self._target_reached():
                    self.stop_reason = f"objectif atteint ({self.stats['emails']} emails)"
                    break
                if self._time_left() <= 0:
                    self.stop_reason = f"budget temps épuisé après {i - 1}/{total}"
                    break

                sites = self._candidate_sites(of)
                if sites is None:             # pas enregistré → retenté plus tard
                    if self._search_fail_streak >= SEARCH_FAIL_STREAK_STOP:
                        self.stop_reason = (f"moteur de recherche bloqué après {i}/{total} "
                                            f"(les autres seront retentés au prochain run)")
                        break
                    time.sleep(random.uniform(SEARCH_SLEEP_MIN, SEARCH_SLEEP_MAX) * 3)
                    continue

                futures.add(pool.submit(self._enrich, of, sites))

                # Contre-pression : pas plus de 2 visites en attente par worker.
                while len(futures) >= PROBE_WORKERS * 2:
                    wait(futures, return_when=FIRST_COMPLETED)
                    yield from self._collect(futures, block=False)

                time.sleep(random.uniform(SEARCH_SLEEP_MIN, SEARCH_SLEEP_MAX))

            # Visites encore en cours : on les termine (≤ ~1 min) pour ne
            # perdre aucun organisme déjà cherché.
            yield from self._collect(futures, block=True)

        if self.stop_reason:
            self.log.warning("⏹️ %s", self.stop_reason)

if __name__ == "__main__":
    # Test local rapide : 10 organismes.
    sc = OrganismesFormationScraper(test_mode=True)
    sc.skip_keys = sc.load_known_keys()
    sc.run(mode="create")
    print(sc.stats, sc.stop_reason, file=sys.stderr)
