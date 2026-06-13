"""
scrapers/education.py
----------------------

Scraper annuaire education.gouv.fr. Logique identique au notebook d'origine,
mais durcie pour tourner sur GitHub Actions (IP datacenter très rate-limitée
par Cloudflare sur ce domaine).

Durcissements vs le notebook :
1. impersonate="chrome146" (empreinte TLS la plus récente disponible
   dans curl_cffi 0.15, au lieu de chrome120)
2. Session curl_cffi persistante avec cookies entre les fiches
3. Warm-up : on ouvre la home avant d'attaquer les fiches détail
4. Sleeps randomisés entre 2 et 5 secondes (au lieu de 0.8s fixe)
5. Backoff plus long : 10s, 20s, 40s, 60s, 90s
6. Détection de streak 403 : si 3 fiches 403 d'affilée, pause de 90s
7. Rotation subtile de User-Agent (chrome146 vs chrome133a)
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from pathlib import Path
from time import sleep
from typing import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    import requests as std_requests
    HAS_CFFI = False

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import clean_text, decode_cfemail


BASE_URL = "https://www.education.gouv.fr"
START_URL = "https://www.education.gouv.fr/annuaire"

# Pauses plus longues + randomisées. Sur IP datacenter (GHA), les valeurs
# courtes du notebook original déclenchent du rate-limiting en quelques secs.
SLEEP_LISTE_MIN = 2.0
SLEEP_LISTE_MAX = 4.0
SLEEP_FICHE_MIN = 2.0
SLEEP_FICHE_MAX = 5.0
TIMEOUT = 60

# Backoff progressif sur 403/429, beaucoup plus long qu'avant (3/6/9/12)
BACKOFF_SCHEDULE = [10, 20, 40, 60, 90]

# Si on accumule trop de 403 d'affilée, on fait une pause plus musclée
STREAK_THRESHOLD = 3
STREAK_COOLDOWN = 90.0

HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "fr-FR,fr;q=0.9,en;q=0.8",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "upgrade-insecure-requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
}

# Rotation légère : on alterne entre deux empreintes récentes.
IMPERSONATE_TARGETS = ["chrome146", "chrome133a"]


def _extract_uai_from_url(url: str) -> str:
    match = re.search(r"/([0-9]{3,}[a-z])/", url.lower())
    return match.group(1).upper() if match else ""


def _extract_departement(code_postal: str) -> str:
    code_postal = clean_text(code_postal)
    if not code_postal:
        return ""
    if len(code_postal) >= 3 and code_postal[:3] in {"971", "972", "973", "974", "976"}:
        return code_postal[:3]
    if len(code_postal) >= 2:
        return code_postal[:2]
    return ""


def _parse_address(raw: str) -> tuple:
    raw = clean_text(raw.replace("Adresse :", ""))
    parts = [clean_text(x) for x in raw.split("-")]
    if len(parts) >= 3:
        adresse = parts[0]
        code_postal = parts[1]
        ville = " - ".join(parts[2:])
    else:
        adresse = raw
        code_postal = ""
        ville = ""
    return adresse, code_postal, _extract_departement(code_postal), ville


class EducationScraper(ScraperBase):
    VERTICAL = "education"
    TABLE = "education"
    NATURAL_KEY = "url_fiche"

    BUSINESS_COLUMNS = [
        "uai",
        "nom",
        "type",
        "statut",
        "academie",
        "zone",
        "adresse",
        "code_postal",
        "departement",
        "ville",
        "telephone",
        "email",
        "services",
        "url_fiche",
        "source_liste_page",
        "date_scraping",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/education/etablissements_education_france.csv"),
        xlsx_path=Path("exports/education/etablissements_education_france.xlsx"),
        jsonl_path=Path("exports/education/etablissements_education_france.jsonl"),
        email_column="email",
        table_name="BaseEducation",
        sheet_name="Établissements",
    )

    def __init__(self, data_dir=None, test_mode=False, max_pages=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        self.max_pages = max_pages or (2 if test_mode else None)

        # Session persistante : les cookies collectés sur la home
        # seront renvoyés aux requêtes suivantes, ce qui réduit les 403.
        if HAS_CFFI:
            self._session = cffi_requests.Session()
        else:
            self._session = std_requests.Session()
        self._session.headers.update(HEADERS)

        self._impersonate_idx = 0
        self._consecutive_403 = 0
        self._warmed_up = False

    def _next_impersonate(self) -> str:
        """Alterne entre plusieurs empreintes Chrome pour éviter les patterns."""
        target = IMPERSONATE_TARGETS[self._impersonate_idx % len(IMPERSONATE_TARGETS)]
        self._impersonate_idx += 1
        return target

    def _warm_up(self) -> None:
        """
        Ouvre la home pour récupérer les cookies de session (dont ceux de
        Cloudflare). Sans ça, les premières requêtes vers les fiches détail
        sont presque systématiquement bloquées depuis une IP datacenter.
        """
        if self._warmed_up:
            return
        try:
            self.log.info("Warm-up : GET %s", BASE_URL)
            if HAS_CFFI:
                self._session.get(
                    BASE_URL, impersonate=self._next_impersonate(),
                    timeout=TIMEOUT,
                )
            else:
                self._session.get(BASE_URL, timeout=TIMEOUT)
            time.sleep(random.uniform(2, 4))
        except Exception as e:
            self.log.warning("Warm-up échoué (non bloquant) : %s", e)
        self._warmed_up = True

    def _fetch(self, url: str, retries: int = 5) -> str:
        """
        GET avec session persistante, backoff long et détection de streak.
        Les URLs de fiches détail sont les plus sensibles : on applique le
        cooldown de streak uniquement là-dessus (pas sur la home).
        """
        self._warm_up()

        last_error = None

        for attempt in range(1, retries + 1):
            try:
                if HAS_CFFI:
                    response = self._session.get(
                        url,
                        impersonate=self._next_impersonate(),
                        timeout=TIMEOUT,
                    )
                else:
                    response = self._session.get(url, timeout=TIMEOUT)

                if response.status_code == 200:
                    self._consecutive_403 = 0
                    return response.text

                last_error = f"HTTP {response.status_code}"

                if response.status_code in (403, 429):
                    self._consecutive_403 += 1
                    if self._consecutive_403 >= STREAK_THRESHOLD:
                        self.log.warning(
                            "Streak de %d refus → cooldown %ds",
                            self._consecutive_403, STREAK_COOLDOWN,
                        )
                        time.sleep(STREAK_COOLDOWN)
                        self._consecutive_403 = 0

            except Exception as e:
                last_error = repr(e)

            wait = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
            # Petit jitter pour éviter les patterns exacts
            wait = wait + random.uniform(0, wait * 0.2)
            self.log.warning(
                "Erreur fetch %d/%d sur %s : %s. Pause %.1fs",
                attempt, retries, url, last_error, wait,
            )
            sleep(wait)

        raise RuntimeError(f"Impossible de récupérer {url} : {last_error}")

    def _find_last_page(self, html: str) -> int:
        soup = BeautifulSoup(html, "lxml")
        last = 0
        for a in soup.select("a[href*='page=']"):
            href = a.get("href", "")
            match = re.search(r"page=(\d+)", href)
            if match:
                last = max(last, int(match.group(1)))
        return last

    def _parse_listing_page(self, html: str, page_num: int) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        cards = []

        for card in soup.select(".fr-card"):
            link = card.select_one("a[href*='/annuaire/']")
            title = card.select_one(".fr-card__title")
            if not link or not title:
                continue

            href = urljoin(BASE_URL, link.get("href"))
            name = clean_text(title.get_text(" "))

            row = {col: "" for col in self.BUSINESS_COLUMNS}
            row.update({
                "uai": _extract_uai_from_url(href),
                "nom": name,
                "url_fiche": href,
                "source_liste_page": str(page_num),
            })

            for p in card.select("p"):
                txt = clean_text(p.get_text(" "))
                if "Académie de" in txt:
                    row["academie"] = txt
                elif txt.startswith("Zone "):
                    row["zone"] = txt
                elif "Adresse :" in txt:
                    adresse, cp, dep, ville = _parse_address(txt)
                    row["adresse"] = adresse
                    row["code_postal"] = cp
                    row["departement"] = dep
                    row["ville"] = ville

            cards.append(row)

        return cards

    def _parse_detail_page(self, html: str, fallback: dict) -> dict:
        soup = BeautifulSoup(html, "lxml")

        row = {col: "" for col in self.BUSINESS_COLUMNS}
        row.update(fallback)

        h1 = soup.select_one("h1")
        if h1:
            row["nom"] = clean_text(h1.get_text(" "))

        tags = [
            clean_text(tag.get_text(" "))
            for tag in soup.select(".fr-tags-group .fr-tag")
        ]
        if len(tags) >= 1:
            row["type"] = tags[0]
        if len(tags) >= 2:
            row["statut"] = tags[1]

        for p in soup.select("p"):
            txt = clean_text(p.get_text(" "))
            if not txt:
                continue

            if txt.startswith("Académie de"):
                row["academie"] = txt
            elif txt.startswith("Zone "):
                row["zone"] = txt
            elif txt.startswith("Adresse :"):
                adresse, cp, dep, ville = _parse_address(txt)
                row["adresse"] = adresse
                row["code_postal"] = cp
                row["departement"] = dep
                row["ville"] = ville
            elif txt.startswith("Tél. :"):
                row["telephone"] = txt.replace("Tél. :", "").strip()
            elif txt.startswith("Email :"):
                row["email"] = txt.replace("Email :", "").strip()

        cf_email = soup.select_one(".__cf_email__")
        if cf_email and cf_email.get("data-cfemail"):
            decoded = decode_cfemail(cf_email.get("data-cfemail"))
            if decoded:
                row["email"] = decoded

        services = []
        for p in soup.select("p.fr-icon-check-line"):
            service = clean_text(p.get_text(" "))
            if service:
                services.append(service)
        row["services"] = " | ".join(dict.fromkeys(services))

        row["uai"] = row["uai"] or _extract_uai_from_url(row["url_fiche"])
        row["departement"] = row["departement"] or _extract_departement(row["code_postal"])
        row["date_scraping"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        return row

    def iter_records(self) -> Iterator[dict]:
        self.log.info("Chargement de la première page (après warm-up)")
        first_html = self._fetch(START_URL)
        last_page = self._find_last_page(first_html)
        self.log.info("Dernière page détectée : %d", last_page)

        if self.max_pages:
            last_page = min(last_page, self.max_pages - 1)
            self.log.info("Limite pages : %d", self.max_pages)

        for page_num in range(0, last_page + 1):
            list_url = START_URL if page_num == 0 else f"{START_URL}?page={page_num}"

            try:
                html = first_html if page_num == 0 else self._fetch(list_url)
                cards = self._parse_listing_page(html, page_num)
                self.log.info("Page %d : %d fiches trouvées", page_num, len(cards))
            except Exception as e:
                self.log.error("Erreur page liste %d : %r", page_num, e)
                continue

            time.sleep(random.uniform(SLEEP_LISTE_MIN, SLEEP_LISTE_MAX))

            for card in cards:
                url = card["url_fiche"]
                try:
                    detail_html = self._fetch(url)
                    row = self._parse_detail_page(detail_html, card)
                    yield row
                except Exception as e:
                    self.log.error("Erreur fiche %s : %r", url, e)
                    # On continue — les fiches déjà vues en base seront
                    # rejouées au prochain run. Stale sera géré proprement.
                    continue
                time.sleep(random.uniform(SLEEP_FICHE_MIN, SLEEP_FICHE_MAX))


if __name__ == "__main__":
    EducationScraper(test_mode=True).run(mode="update")
