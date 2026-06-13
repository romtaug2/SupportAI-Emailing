"""
scrapers/auto_ecole.py
-----------------------

Scraper de l'annuaire auto-ecole.info — toutes les auto-écoles de France
(métropole + DOM), classées par département. Logique de parsing : on lit
directement les `data-*` attributs du bloc carte de chaque fiche
(`<div class="map-autoecole">`), qui contient `data-nom`, `data-adresse`,
`data-code-postal-ville`, `data-latitude`, `data-longitude`. C'est la source
la plus propre et la plus stable du site (utilisée par leur propre JS Leaflet).

Architecture :
1. La page de recherche `/page-38-recherche-auto-ecole-proximite.html` contient
   un `<select>` avec un `<option>` par département. On en tire le slug
   (`somme`) et le code (`80`) pour reconstruire l'URL département.
2. Chaque page département `/auto-ecole--{slug}--{code}.html` liste TOUTES
   les fiches du département dans la section "Toutes les auto-écoles du
   département" — pas besoin de naviguer par ville.
3. Chaque fiche `/info-auto-ecole--{slug}--a--{ville}--{cp}--{id}.html`
   est parsée via les data-attrs du bloc carte + l'attribut `data-email`
   pour l'email (avec fallback sur le `<canvas id="mail-canvas">`).

Clé naturelle d'upsert : `url_fiche` (URL absolue, stable car contient l'id).

Mode test : 2 départements seulement.
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import clean_text, is_social_domain


BASE_URL = "https://www.auto-ecole.info"
HOME_SEARCH_URL = f"{BASE_URL}/page-38-recherche-auto-ecole-proximite.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

TIMEOUT = 30

# Délai entre 2 requêtes (politesse) — le site n'a pas de protection
# Cloudflare agressive donc on peut être plus rapide qu'education.gouv.fr.
SLEEP_DEP_MIN = 0.6
SLEEP_DEP_MAX = 1.4
SLEEP_FICHE_MIN = 0.3
SLEEP_FICHE_MAX = 0.9

# Domaines à ignorer quand on cherche le site web officiel d'une auto-école
WEB_BLOCKLIST = (
    "auto-ecole.info", "autoecole.biz", "google.com", "googleapis.com",
    "kit.fontawesome.com", "use.typekit.net", "unpkg.com", "cdnjs.",
    "jspm.io", "orata.fr", "fontawesome", "wa.me", "whatsapp.com",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    """Session avec retry exponentiel sur 429/5xx (cohérent avec hotels.py)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _normalize_phone(raw: str) -> str:
    """Format 10 chiffres (0XXXXXXXXX), retourne '' si invalide."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("33") and len(digits) == 11:
        digits = "0" + digits[2:]
    if digits.startswith("0033") and len(digits) == 13:
        digits = "0" + digits[4:]
    return digits if len(digits) == 10 else ""


def _extract_id_from_url(url: str) -> str:
    """L'id du site se trouve à la fin : /info-auto-ecole--...--{id}.html"""
    m = re.search(r"--(\d+)\.html$", url)
    return m.group(1) if m else ""


def _extract_departement_from_postal(code_postal: str) -> str:
    """Code département depuis le CP (gère DOM 971-976)."""
    code_postal = clean_text(code_postal)
    if not code_postal:
        return ""
    if len(code_postal) >= 3 and code_postal[:3] in {"971", "972", "973", "974", "976"}:
        return code_postal[:3]
    if len(code_postal) >= 2:
        return code_postal[:2]
    return ""


def _parse_departements(home_html: str) -> list[dict]:
    """
    Liste des départements depuis le `<select>` de la page de recherche.
    On dédoublonne par code (la métropole + DOM apparaissent une seule fois).
    """
    soup = BeautifulSoup(home_html, "lxml")
    deps: list[dict] = []
    seen: set[str] = set()
    for opt in soup.find_all("option"):
        val = opt.get("value", "")
        m = re.search(r"/auto-ecole--([a-z0-9-]+)--([0-9ab]+)\.html", val, re.I)
        if not m:
            continue
        slug = m.group(1)
        code = m.group(2).upper()
        if code in seen:
            continue
        seen.add(code)
        # Les <option value> commencent par // → on préfixe https:
        if val.startswith("//"):
            url = "https:" + val
        else:
            url = urljoin(BASE_URL, val)
        deps.append({
            "code": code,
            "slug": slug,
            "label": clean_text(opt.get_text()),
            "url": url,
        })
    return deps


def _parse_fiches_urls(dep_html: str) -> list[str]:
    """Toutes les URLs `/info-auto-ecole--...html` de la page département."""
    soup = BeautifulSoup(dep_html, "lxml")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/info-auto-ecole--" not in href or not href.endswith(".html"):
            continue
        full = urljoin(BASE_URL, href)
        if full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class AutoEcoleScraper(ScraperBase):
    VERTICAL = "auto_ecole"
    TABLE = "auto_ecole"
    NATURAL_KEY = "url_fiche"

    # NOTE — pourquoi pas de `date_scraping` dans BUSINESS_COLUMNS :
    # core/db.py calcule un content_hash sur ces colonnes pour détecter
    # les vrais changements. Inclure un timestamp `now()` ferait basculer
    # toutes les fiches en "updated" à chaque run même sans modification
    # réelle (bug latent dans education.py, qu'on évite ici). La date du
    # dernier scrape est déjà tracée via les colonnes techniques
    # `last_seen_at` et `last_updated_at` (cf core/db.fetch_active_rows).
    BUSINESS_COLUMNS = [
        "site_id",
        "nom",
        "email",
        "telephone",
        "telephone_2",
        "adresse",
        "code_postal",
        "ville",
        "departement",
        "departement_nom",
        "latitude",
        "longitude",
        "site_web",
        "formations",
        "url_fiche",
        "source_dep_url",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/auto_ecole/annuaire_auto_ecoles_france.csv"),
        xlsx_path=Path("exports/auto_ecole/annuaire_auto_ecoles_france.xlsx"),
        jsonl_path=Path("exports/auto_ecole/annuaire_auto_ecoles_france.jsonl"),
        email_column="email",
        table_name="BaseAutoEcole",
        sheet_name="Auto-Écoles",
    )

    def __init__(self, data_dir=None, test_mode=False, max_departments=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        # En mode test, on ne fait que 2 départements (cohérent avec hotels.py / immo.py)
        self.max_departments = max_departments or (2 if test_mode else None)
        self.session = _build_session()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch(self, url: str, retries: int = 3) -> str | None:
        """GET avec retries internes en plus de ceux de la Session adapter."""
        for attempt in range(1, retries + 1):
            try:
                resp = self.session.get(url, timeout=TIMEOUT)
                if resp.status_code == 404:
                    self.log.warning("404 sur %s", url)
                    return None
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                wait = (2 ** attempt) + random.uniform(0, 1)
                self.log.warning(
                    "Erreur fetch %d/%d sur %s : %s. Pause %.1fs",
                    attempt, retries, url, e, wait,
                )
                time.sleep(wait)
        self.log.error("Abandon sur %s", url)
        return None

    # ------------------------------------------------------------------
    # Parsing détail
    # ------------------------------------------------------------------

    def _parse_detail(self, url: str, html: str, source_dep_url: str) -> dict:
        """
        Parse une fiche d'auto-école. Source de vérité :
        `<div class="map-autoecole" data-nom=... data-adresse=...
        data-code-postal-ville=... data-latitude=... data-longitude=...>`
        """
        soup = BeautifulSoup(html, "lxml")
        row = {col: "" for col in self.BUSINESS_COLUMNS}
        row["url_fiche"] = url
        row["source_dep_url"] = source_dep_url
        row["site_id"] = _extract_id_from_url(url)

        # Nom (h1, fallback data-nom)
        h1 = soup.find("h1")
        if h1:
            row["nom"] = clean_text(h1.get_text(" "))

        # Bloc carte → adresse / cp / ville / lat / lng
        map_div = soup.find(class_="map-autoecole")
        if map_div:
            if not row["nom"]:
                row["nom"] = clean_text(map_div.get("data-nom", ""))
            row["adresse"] = clean_text(map_div.get("data-adresse", ""))
            cp_ville = clean_text(map_div.get("data-code-postal-ville", ""))
            row["latitude"] = clean_text(map_div.get("data-latitude", ""))
            row["longitude"] = clean_text(map_div.get("data-longitude", ""))
            m = re.match(r"^\s*(\d{5})\s+(.+)$", cp_ville)
            if m:
                row["code_postal"] = m.group(1)
                row["ville"] = m.group(2).strip()
            else:
                row["ville"] = cp_ville

        # Fallback lat/lng depuis #map
        if not row["latitude"]:
            map_el = soup.find(id="map")
            if map_el:
                row["latitude"] = clean_text(map_el.get("data-latitude", ""))
                row["longitude"] = clean_text(map_el.get("data-longitude", ""))

        # Email : data-email (clean), fallback canvas, sanity-check par regex
        email = ""
        email_tag = soup.find(attrs={"data-email": True})
        if email_tag:
            email = clean_text(email_tag.get("data-email"))
        if not email:
            canvas = soup.find("canvas", id="mail-canvas")
            if canvas:
                email = clean_text(canvas.get_text())
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            email = ""
        row["email"] = email

        # Téléphones (depuis le bloc Coordonnées) — dedup par version normalisée
        phones_raw: list[str] = []
        seen_norm: set[str] = set()
        for li in soup.select("ul.fa-ul li"):
            txt = clean_text(li.get_text(" "))
            if not any(k in txt for k in ("Téléphone", "Tél", "Tel ")):
                continue
            for raw in re.findall(
                r"(?:\+33\s?|0)\s?[1-9](?:[\s.\-]?\d{2}){4}", txt
            ):
                norm = _normalize_phone(raw)
                if not norm or norm in seen_norm:
                    continue
                seen_norm.add(norm)
                phones_raw.append(norm)
        if phones_raw:
            row["telephone"] = phones_raw[0]
        if len(phones_raw) > 1:
            row["telephone_2"] = phones_raw[1]

        # Site web : on cherche dans le <li> qui contient "Site web"
        site_web = ""
        for li in soup.select("ul.fa-ul li"):
            if "Site web" not in li.get_text():
                continue
            a = li.find("a", href=True)
            if a and a["href"].startswith("http"):
                href = a["href"].strip()
                if not any(b in href.lower() for b in WEB_BLOCKLIST):
                    site_web = href
                    break
        # Fallback : title="Visitez le site de ..."
        if not site_web:
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                title = (a.get("title") or "").lower()
                if not href.startswith("http"):
                    continue
                if "visitez le site" in title and not any(
                    b in href.lower() for b in WEB_BLOCKLIST
                ):
                    site_web = href
                    break
        # On ignore les réseaux sociaux (utilise le helper core)
        if site_web and is_social_domain(site_web.lower()):
            site_web = ""
        row["site_web"] = site_web

        # Formations (alt text des pictos)
        formations: list[str] = []
        for img in soup.select("img[alt]"):
            src = img.get("src", "")
            if "/formations/pictos/" not in src:
                continue
            alt = clean_text(img.get("alt"))
            if alt and alt not in formations and alt.lower() not in ("...", "permis"):
                formations.append(alt)
        row["formations"] = " | ".join(formations)

        # Département : breadcrumb d'abord, fallback CP
        for a in soup.select("ol.breadcrumb a[href]"):
            m = re.search(
                r"/auto-ecole--([a-z0-9-]+)--([0-9ab]+)\.html",
                a.get("href", ""),
                re.I,
            )
            if m:
                row["departement_nom"] = m.group(1)
                row["departement"] = m.group(2).upper()
                break
        if not row["departement"]:
            row["departement"] = _extract_departement_from_postal(row["code_postal"])

        return row

    # ------------------------------------------------------------------
    # iter_records
    # ------------------------------------------------------------------

    def iter_records(self) -> Iterator[dict]:
        # 1. Liste des départements depuis la page de recherche
        self.log.info("Chargement de la page de recherche : %s", HOME_SEARCH_URL)
        home_html = self._fetch(HOME_SEARCH_URL)
        if not home_html:
            raise RuntimeError("Impossible de récupérer la page d'accueil")

        deps = _parse_departements(home_html)
        self.log.info("Départements identifiés : %d", len(deps))

        if self.max_departments:
            deps = deps[: self.max_departments]
            self.log.info("Mode test : limité à %d départements", len(deps))

        # 2. Pour chaque département : liste des fiches
        for dep_idx, dep in enumerate(deps, start=1):
            self.log.info(
                "[DEP %d/%d] %s (%s) → %s",
                dep_idx, len(deps), dep["code"], dep["label"], dep["url"],
            )
            dep_html = self._fetch(dep["url"])
            if not dep_html:
                self.log.error("Page département %s inaccessible", dep["code"])
                continue

            fiches_urls = _parse_fiches_urls(dep_html)
            self.log.info(
                "[DEP %s] %d fiches détectées",
                dep["code"], len(fiches_urls),
            )

            time.sleep(random.uniform(SLEEP_DEP_MIN, SLEEP_DEP_MAX))

            # 3. Pour chaque fiche : on yield le dict complet
            for f_idx, fiche_url in enumerate(fiches_urls, start=1):
                try:
                    fiche_html = self._fetch(fiche_url)
                    if not fiche_html:
                        continue
                    row = self._parse_detail(fiche_url, fiche_html, dep["url"])
                    yield row
                except Exception as e:
                    self.log.error("Erreur fiche %s : %r", fiche_url, e)
                    continue

                time.sleep(random.uniform(SLEEP_FICHE_MIN, SLEEP_FICHE_MAX))

                if f_idx % 25 == 0:
                    self.log.info(
                        "[DEP %s] progression %d/%d fiches",
                        dep["code"], f_idx, len(fiches_urls),
                    )


if __name__ == "__main__":
    AutoEcoleScraper(test_mode=True).run(mode="update")
