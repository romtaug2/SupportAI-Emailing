"""
scrapers/notaires.py
---------------------

Scraper annuaire notaires.fr. Logique identique au notebook d'origine
(crawl annuaire région par région -> URLs d'offices -> scrape de chaque
fiche), mais industrialisée :

- Héritage de ScraperBase (upsert SQLite, exports CSV/XLSX/JSONL gérés
  automatiquement, run_id, mark_stale).
- iter_records() fusionne les 2 étapes du notebook (crawl_annuaire
  puis scrape_offices) en un seul générateur.
- Pas de stockage intermédiaire (SEEN_FILE / OFFICE_URLS_FILE /
  RAW_ROWS_FILE / FINAL_CSV) : l'état est porté par SQLite + dedup par
  clé naturelle (url) + content_hash pour détecter les changements.
- Sleeps randomisés pour éviter les patterns trop réguliers depuis IP
  datacenter (GitHub Actions).
- Mode test : 1 région (auvergne-rhone-alpes) + max 30 pages annuaire +
  max 20 offices, comme dans le notebook d'origine.

Clé naturelle : `url` (URL de la fiche office sur notaires.fr).
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import (
    clean_text,
    decode_cfemail,
    extract_emails,
    extract_phones,
    pick_best_email,
)


BASE_URL = "https://www.notaires.fr"

START_URLS_FULL = [
    "https://www.notaires.fr/fr/annuaire/auvergne-rhone-alpes",
    "https://www.notaires.fr/fr/annuaire/bourgogne-franche-comte",
    "https://www.notaires.fr/fr/annuaire/bretagne",
    "https://www.notaires.fr/fr/annuaire/centre-val-de-loire",
    "https://www.notaires.fr/fr/annuaire/corse",
    "https://www.notaires.fr/fr/annuaire/grand-est",
    "https://www.notaires.fr/fr/annuaire/hauts-de-france",
    "https://www.notaires.fr/fr/annuaire/ile-de-france",
    "https://www.notaires.fr/fr/annuaire/normandie",
    "https://www.notaires.fr/fr/annuaire/nouvelle-aquitaine",
    "https://www.notaires.fr/fr/annuaire/occitanie",
    "https://www.notaires.fr/fr/annuaire/pays-de-la-loire",
    "https://www.notaires.fr/fr/annuaire/provence-alpes-cote-d-azur",
    "https://www.notaires.fr/fr/annuaire/guadeloupe",
    "https://www.notaires.fr/fr/annuaire/martinique",
    "https://www.notaires.fr/fr/annuaire/guyane",
    "https://www.notaires.fr/fr/annuaire/la-reunion",
    "https://www.notaires.fr/fr/annuaire/mayotte",
]

START_URLS_TEST = [
    "https://www.notaires.fr/fr/annuaire/auvergne-rhone-alpes",
]

# Limites en mode test (identiques au notebook)
TEST_MAX_ANNUAIRE_PAGES = 30
TEST_MAX_OFFICES = 20

# Sleeps randomisés (le notebook avait 0.35s fixe, on randomise pour éviter
# les patterns reconnaissables depuis une IP datacenter)
SLEEP_CRAWL_MIN = 0.3
SLEEP_CRAWL_MAX = 0.8
SLEEP_OFFICE_MIN = 0.4
SLEEP_OFFICE_MAX = 1.0

TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}


# ---------------------------------------------------------------------------
# Helpers spécifiques notaires.fr (les helpers génériques email/phone/clean
# viennent de core.utils pour rester cohérent avec les autres scrapers).
# ---------------------------------------------------------------------------


def _normalize_url(url: str) -> str | None:
    """Force l'URL à pointer sur www.notaires.fr et nettoie le fragment."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)

    if parsed.netloc and parsed.netloc not in {"www.notaires.fr", "notaires.fr"}:
        return None

    full = urljoin(BASE_URL, parsed.path)
    if parsed.query:
        full += "?" + parsed.query
    return full.rstrip("/")


def _is_annuaire_url(url: str) -> bool:
    return urlparse(url).path.startswith("/fr/annuaire/")


def _is_office_url(url: str) -> bool:
    return urlparse(url).path.startswith("/fr/office/")


def _extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    """Récupère le fil d'ariane (Accueil > Région > Département > Ville)."""
    crumbs: list[str] = []
    for el in soup.select(".breadcrumb__list span, .breadcrumb__list a"):
        t = clean_text(el.get_text(" ", strip=True))
        if t and t not in crumbs:
            crumbs.append(t)
    return crumbs


def _extract_address(soup: BeautifulSoup) -> str:
    selectors = [
        "address",
        "[class*=address]",
        "[class*=adresse]",
        ".office-sheet__address",
        ".professional__address",
        ".contact__address",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = clean_text(el.get_text(" ", strip=True))
            if len(t) > 10:
                return t
    return ""


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class NotairesScraper(ScraperBase):
    VERTICAL = "notaires"
    TABLE = "notaires"
    NATURAL_KEY = "url"

    BUSINESS_COLUMNS = [
        "office",
        "email",
        "emails_all",
        "phone",
        "phones_all",
        "address",
        "region",
        "department",
        "city",
        "url",
        "source",
        "date_scraping",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/notaires/annuaire_notaires_france.csv"),
        xlsx_path=Path("exports/notaires/annuaire_notaires_france.xlsx"),
        jsonl_path=Path("exports/notaires/annuaire_notaires_france.jsonl"),
        email_column="email",
        table_name="BaseNotaires",
        sheet_name="Notaires",
    )

    def __init__(
        self,
        data_dir=None,
        test_mode=False,
        max_pages=None,
        max_offices=None,
    ):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)

        if test_mode:
            self.start_urls = START_URLS_TEST
            self.max_pages = max_pages or TEST_MAX_ANNUAIRE_PAGES
            self.max_offices = max_offices or TEST_MAX_OFFICES
        else:
            self.start_urls = START_URLS_FULL
            self.max_pages = max_pages       # None = illimité
            self.max_offices = max_offices   # None = illimité

        # Session persistante avec retry/backoff sur 429/5xx
        self._session = requests.Session()
        self._session.headers.update(HEADERS)
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _fetch(self, url: str) -> str:
        r = self._session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text

    # ------------------------------------------------------------------
    # ÉTAPE 1 : crawl de l'annuaire
    # ------------------------------------------------------------------

    def _extract_links(self, page_html: str, current_url: str) -> set[str]:
        soup = BeautifulSoup(page_html, "lxml")
        links: set[str] = set()

        for a in soup.find_all("a", href=True):
            u = _normalize_url(urljoin(current_url, a["href"]))
            if u:
                links.add(u)

        # Capture aussi les liens en clair dans le HTML (data-attrs, JSON
        # inline, etc.) que BeautifulSoup ne voit pas comme des <a>.
        for m in re.findall(
            r"""["'](/fr/(?:annuaire|office)/[^"'?#<>\s]+)["']""",
            page_html,
        ):
            u = _normalize_url(urljoin(BASE_URL, m))
            if u:
                links.add(u)

        return links

    def _add_pagination_urls(self, url: str, html_text: str) -> set[str]:
        urls: set[str] = set()
        soup = BeautifulSoup(html_text, "lxml")

        for a in soup.select("a[href*='page=']"):
            u = _normalize_url(urljoin(url, a.get("href")))
            if u:
                urls.add(u)

        for m in re.findall(r"[?&]page=(\d+)", html_text):
            sep = "&" if "?" in url else "?"
            urls.add(f"{url}{sep}page={m}")

        return urls

    def _crawl_annuaire(self) -> list[str]:
        """BFS depuis les régions -> collecte les URLs d'offices."""
        seen: set[str] = set()
        office_urls: set[str] = set()
        queue: list[str] = []

        for u in self.start_urls:
            n = _normalize_url(u)
            if n and n not in seen and n not in queue:
                queue.append(n)

        done = 0
        max_pages = self.max_pages if self.max_pages is not None else 1_000_000

        self.log.info("=" * 60)
        self.log.info("ÉTAPE 1 : CRAWL ANNUAIRE")
        self.log.info("Start URLs    : %d", len(self.start_urls))
        self.log.info("Max pages     : %s",
                      self.max_pages if self.max_pages else "illimité")
        self.log.info("=" * 60)

        while queue and done < max_pages:
            url = queue.pop(0)
            if url in seen:
                continue

            try:
                page = self._fetch(url)
                seen.add(url)
                done += 1

                links = self._extract_links(page, url)
                links.update(self._add_pagination_urls(url, page))

                new_annuaire = 0
                new_offices = 0

                for link in links:
                    if _is_office_url(link):
                        before = len(office_urls)
                        office_urls.add(link)
                        if len(office_urls) > before:
                            new_offices += 1
                    elif (
                        _is_annuaire_url(link)
                        and link not in seen
                        and link not in queue
                    ):
                        queue.append(link)
                        new_annuaire += 1

                self.log.info(
                    "[CRAWL] pages=%d/%s | queue=%d | offices=%d "
                    "| +annuaire=%d | +offices=%d | url=%s",
                    done,
                    str(self.max_pages) if self.max_pages else "∞",
                    len(queue),
                    len(office_urls),
                    new_annuaire,
                    new_offices,
                    url,
                )

                time.sleep(random.uniform(SLEEP_CRAWL_MIN, SLEEP_CRAWL_MAX))

            except Exception as e:
                self.log.warning("[ERREUR CRAWL] %s -> %r", url, e)

        self.log.info("[CRAWL FINI] offices trouvés=%d", len(office_urls))
        return sorted(office_urls)

    # ------------------------------------------------------------------
    # ÉTAPE 2 : scraping des fiches offices
    # ------------------------------------------------------------------

    def _scrape_office(self, url: str) -> dict:
        page = self._fetch(url)
        soup = BeautifulSoup(page, "lxml")
        text = soup.get_text(" ", strip=True)

        # Titre de l'office
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True)) if h1 else ""
        if not title and soup.find("title"):
            title = clean_text(
                soup.find("title").get_text()
            ).replace("| Notaires de France", "").strip()

        # Emails : combinaison du HTML brut + texte rendu
        emails = extract_emails(page + " " + text)

        # Décodage Cloudflare au cas où l'email principal serait obfusqué
        cf_email = soup.select_one(".__cf_email__")
        if cf_email and cf_email.get("data-cfemail"):
            decoded = decode_cfemail(cf_email.get("data-cfemail"))
            if decoded and decoded not in emails:
                emails.insert(0, decoded)

        phones = extract_phones(text)
        crumbs = _extract_breadcrumbs(soup)

        row = {col: "" for col in self.BUSINESS_COLUMNS}
        row.update({
            "office": title,
            "email": pick_best_email(emails),
            "emails_all": ";".join(emails),
            "phone": phones[0] if phones else "",
            "phones_all": ";".join(phones),
            "address": _extract_address(soup),
            "region": crumbs[1] if len(crumbs) > 1 else "",
            "department": crumbs[2] if len(crumbs) > 2 else "",
            "city": crumbs[3] if len(crumbs) > 3 else "",
            "url": url,
            "source": "notaires.fr",
            "date_scraping": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        return row

    # ------------------------------------------------------------------
    # iter_records : appelé par ScraperBase.run()
    # ------------------------------------------------------------------

    def iter_records(self) -> Iterator[dict]:
        office_urls = self._crawl_annuaire()

        # Scraping incrémental : skip des offices déjà connus (passés via
        # l'attribut skip_urls par scrape_slice.py). Chaque run ne fetch
        # que des fiches NOUVELLES -> la tranche avance toute seule.
        skip = getattr(self, "skip_urls", None)
        if skip:
            before = len(office_urls)
            office_urls = [u for u in office_urls if u not in skip]
            self.log.info("Skip déjà en base : %d -> %d nouveaux", before, len(office_urls))

        if self.max_offices is not None:
            office_urls = office_urls[: self.max_offices]

        total = len(office_urls)
        self.log.info("=" * 60)
        self.log.info("ÉTAPE 2 : SCRAPING OFFICES (%d à scraper)", total)
        self.log.info("=" * 60)

        for i, url in enumerate(office_urls, 1):
            try:
                row = self._scrape_office(url)
            except Exception as e:
                self.log.warning("[ERREUR OFFICE] %s -> %r", url, e)
                continue

            self.log.info(
                "[OFFICE] %d/%d | email=%s | office=%s",
                i,
                total,
                "oui" if row.get("email") else "non",
                (row.get("office") or "")[:80],
            )
            yield row

            time.sleep(random.uniform(SLEEP_OFFICE_MIN, SLEEP_OFFICE_MAX))


if __name__ == "__main__":
    NotairesScraper(test_mode=True).run(mode="update")
