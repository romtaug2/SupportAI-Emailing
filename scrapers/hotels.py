"""
scrapers/hotels.py
-------------------

Scraper trouve-ton-hotel.fr. Logique de parsing identique au notebook
d'origine, en générateur qui yield un dict par hôtel.

La clé naturelle d'upsert combine nom + code postal + ville + tel, car le
site ne fournit pas d'URL stable par hôtel (les fiches sont rendues en
blocs sur la page département). Cette clé est stockée dans la colonne
technique `natural_key` créée par core/db.py — on ne la duplique PAS dans
les colonnes métier.
"""

from __future__ import annotations

import hashlib
import random
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import clean_text, extract_emails


BASE_URL = "https://www.trouve-ton-hotel.fr"
HOME_URL = BASE_URL + "/"
PARIS_URL = BASE_URL + "/hotel-paris-75/"
CMAP_JS_URL = BASE_URL + "/cmap/france_dpt.js"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TIMEOUT = 30
REQUEST_SLEEP_MIN = 0.3
REQUEST_SLEEP_MAX = 0.8


def _build_session() -> requests.Session:
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


def _get_bs4_parser() -> str:
    try:
        BeautifulSoup("<html></html>", "lxml")
        return "lxml"
    except Exception:
        return "html.parser"


def _extract_phone_lines(text: str) -> dict:
    tel = mobile = fax = ""
    if not text:
        return {"tel": "", "mobile": "", "fax": "", "phones_all": ""}

    m_tel = re.search(r"Tel\s*:\s*([0-9 .+-]{6,})", text, flags=re.I)
    if m_tel:
        tel = clean_text(m_tel.group(1))
    m_mobile = re.search(r"Mobile\s*:\s*([0-9 .+-]{6,})", text, flags=re.I)
    if m_mobile:
        mobile = clean_text(m_mobile.group(1))
    m_fax = re.search(r"Fax\s*:\s*([0-9 .+-]{6,})", text, flags=re.I)
    if m_fax:
        fax = clean_text(m_fax.group(1))

    phones = [p for p in [tel, mobile, fax] if p]
    return {"tel": tel, "mobile": mobile, "fax": fax,
            "phones_all": " | ".join(phones)}


def _discover_department_urls_from_html(soup: BeautifulSoup) -> set:
    found = set()
    if soup is None:
        return found
    for a in soup.select("a[href]"):
        href = a.get("href", "").strip()
        if not href:
            continue
        full_url = urljoin(BASE_URL, href)
        path = urlparse(full_url).path.lower().strip("/")
        if path.startswith("hotel-"):
            if not full_url.endswith("/"):
                full_url += "/"
            found.add(full_url)
    return found


def _discover_department_urls_from_js(js_text: str) -> set:
    found = set()
    if not js_text:
        return found
    patterns = [
        r"""["'](\/hotel-[^"']+)["']""",
        r"""["'](hotel-[^"']+)["']""",
        r"""https?:\/\/www\.trouve-ton-hotel\.fr\/(hotel-[^"'\s]+)""",
        r"""https?:\/\/trouve-ton-hotel\.fr\/(hotel-[^"'\s]+)""",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, js_text, flags=re.I):
            if match.startswith("http"):
                full_url = match
            elif match.startswith("/"):
                full_url = urljoin(BASE_URL, match)
            else:
                full_url = urljoin(BASE_URL + "/", match)
            if not full_url.endswith("/"):
                full_url += "/"
            path = urlparse(full_url).path.lower().strip("/")
            if path.startswith("hotel-"):
                found.add(full_url)
    return found


class HotelsScraper(ScraperBase):
    VERTICAL = "hotels"
    TABLE = "hotels"

    # La clé naturelle est un hash calculé à partir des champs métier.
    # On la place dans record["natural_key"] pour que scraper_base la lise,
    # mais on ne la met PAS dans BUSINESS_COLUMNS car la colonne technique
    # `natural_key` est déjà créée automatiquement par core/db.init_table.
    NATURAL_KEY = "natural_key"

    BUSINESS_COLUMNS = [
        "nom",
        "adresse_ligne1",
        "code_postal",
        "ville",
        "adresse_complete",
        "tel",
        "mobile",
        "fax",
        "phones_all",
        "email_principal",
        "emails_trouves",
        "site_web",
        "description",
        "image_url",
        "source_url",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/hotels/base_prospection_trouve_ton_hotel.csv"),
        xlsx_path=Path("exports/hotels/base_prospection_trouve_ton_hotel.xlsx"),
        jsonl_path=Path("exports/hotels/base_prospection_trouve_ton_hotel.jsonl"),
        email_column="email_principal",
        table_name="BaseHotels",
        sheet_name="Hôtels",
    )

    def __init__(self, data_dir=None, test_mode=False, max_departments=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        self.max_departments = max_departments or (3 if test_mode else None)
        self.session = _build_session()
        self.parser = _get_bs4_parser()

    def _polite_sleep(self) -> None:
        time.sleep(random.uniform(REQUEST_SLEEP_MIN, REQUEST_SLEEP_MAX))

    def _get_soup(self, url: str):
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, self.parser)
        except Exception as e:
            self.log.warning("Chargement HTML impossible: %s -> %s", url, e)
            return None

    def _get_text_file(self, url: str) -> str:
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            self.log.warning("Chargement texte impossible: %s -> %s", url, e)
            return ""

    def _parse_listing_block(self, block, source_url: str) -> dict | None:
        row = {
            "nom": "",
            "adresse_ligne1": "",
            "code_postal": "",
            "ville": "",
            "adresse_complete": "",
            "tel": "",
            "mobile": "",
            "fax": "",
            "phones_all": "",
            "email_principal": "",
            "emails_trouves": "",
            "site_web": "",
            "description": "",
            "image_url": "",
            "source_url": source_url,
        }

        title_p = block.select_one(".titre_nouveau p")
        if title_p:
            row["nom"] = clean_text(title_p.get_text(" ", strip=True))

        img = block.select_one(".image_nouveau img")
        if img and img.get("src"):
            row["image_url"] = urljoin(BASE_URL, img.get("src").strip())

        addr_p = block.select_one(".adresse_nouveau p")
        if addr_p:
            addr_text = addr_p.get_text("\n", strip=True)
            addr_text = addr_text.replace("â", "\n")
            addr_lines = [clean_text(x) for x in addr_text.split("\n") if clean_text(x)]

            if len(addr_lines) >= 1:
                row["adresse_ligne1"] = addr_lines[0]
            if len(addr_lines) >= 2:
                cp_city_line = addr_lines[1]
                m = re.search(r"\b(\d{5})\b\s*(.*)", cp_city_line)
                if m:
                    row["code_postal"] = clean_text(m.group(1))
                    row["ville"] = clean_text(m.group(2))
                else:
                    row["ville"] = clean_text(cp_city_line)
            row["adresse_complete"] = " | ".join(addr_lines)

        tel_p = block.select_one(".tel_nouveau p")
        if tel_p:
            tel_text = tel_p.get_text("\n", strip=True)
            row.update(_extract_phone_lines(tel_text))

        desc_p = block.select_one(".comment_nouveau p")
        if desc_p:
            row["description"] = clean_text(desc_p.get_text(" ", strip=True))

        lien_p = block.select_one(".lien_nouveau p")
        block_text = clean_text(block.get_text(" ", strip=True))
        lien_text = clean_text(lien_p.get_text(" ", strip=True)) if lien_p else ""

        emails = extract_emails(block_text + " " + lien_text, filter_bad=False)
        row["email_principal"] = emails[0] if emails else ""
        row["emails_trouves"] = " | ".join(emails)

        site_link = block.select_one(".lien_nouveau a[href]")
        if site_link and site_link.get("href"):
            row["site_web"] = urljoin(BASE_URL, site_link.get("href").strip())

        if not (row["nom"] or row["adresse_complete"] or row["email_principal"] or row["site_web"]):
            return None

        # Clé naturelle stable : hash SHA-1 des champs identifiants.
        # Stockée sous "natural_key" dans le dict yieldé → scraper_base la
        # lit via record.get(self.NATURAL_KEY), puis db.upsert_row la passe
        # dans la colonne technique natural_key uniquement (pas de doublon).
        key_src = "||".join([
            row.get("nom", "").lower(),
            row.get("adresse_complete", "").lower(),
            row.get("tel", "").lower(),
            row.get("email_principal", "").lower(),
        ])
        row["natural_key"] = hashlib.sha1(key_src.encode("utf-8")).hexdigest()
        return row

    def _scrape_department_page(self, url: str) -> Iterator[dict]:
        soup = self._get_soup(url)
        if soup is None:
            return
        blocks = soup.select("div.annonce_nouveau")
        count = 0
        for block in blocks:
            row = self._parse_listing_block(block, url)
            if row is not None:
                count += 1
                yield row
        self.log.info("[PAGE] %s -> %d hôtels", url, count)

    def iter_records(self) -> Iterator[dict]:
        self.log.info("Découverte des pages département")

        urls = set()

        home_soup = self._get_soup(HOME_URL)
        urls.update(_discover_department_urls_from_html(home_soup))
        urls.add(PARIS_URL)

        js_text = self._get_text_file(CMAP_JS_URL)
        urls.update(_discover_department_urls_from_js(js_text))

        urls = {
            u if u.endswith("/") else u + "/"
            for u in urls
            if urlparse(u).path.lower().strip("/").startswith("hotel-")
        }
        department_urls = sorted(urls)

        self.log.info("Pages département trouvées : %d", len(department_urls))
        if self.max_departments:
            department_urls = department_urls[: self.max_departments]
            self.log.info("Limite test : %d pages", len(department_urls))

        for idx, url in enumerate(department_urls, start=1):
            self.log.info("[DEP %d/%d] %s", idx, len(department_urls), url)
            yield from self._scrape_department_page(url)
            self._polite_sleep()


if __name__ == "__main__":
    HotelsScraper(test_mode=True).run(mode="update")
