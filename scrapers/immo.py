"""
scrapers/immo.py
-----------------

Scraper immomatin.com. Logique de parsing identique au notebook d'origine.
Clé naturelle = detail_url.
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
from core.utils import clean_text, extract_emails, extract_phones, first_or_empty


BASE_URL = "https://www.immomatin.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

TIMEOUT = 30
REQUEST_SLEEP_MIN = 0.4
REQUEST_SLEEP_MAX = 1.0


def _build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=5,
        backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_parser() -> str:
    try:
        BeautifulSoup("<html></html>", "lxml")
        return "lxml"
    except Exception:
        return "html.parser"


def _parse_label_value(text: str, label: str) -> str:
    """Extrait 'Label : valeur' jusqu'au prochain label connu."""
    if not text:
        return ""
    normalized = text.replace("\xa0", " ")
    normalized = re.sub(r"\s+", " ", normalized)

    labels = [
        "Nationalité", "Date de création", "Nom du dirigeant",
        "Nombre d\u2019employés", "Nombre d'employés", "Adresse",
        "Tel", "Téléphone", "E-mail", "Email", "Site Web", "Site",
        "Description", "OFFRE", "CONTACT", "IDENTITE",
    ]
    escaped = [re.escape(x) for x in labels if x != label]
    stop_pattern = "|".join(escaped) if escaped else "$"
    pattern = rf"{re.escape(label)}\s*:\s*(.+?)(?=(?:{stop_pattern})\s*:|$)"
    m = re.search(pattern, normalized, flags=re.I)
    return clean_text(m.group(1)) if m else ""


class ImmoScraper(ScraperBase):
    VERTICAL = "immo"
    TABLE = "immo"
    NATURAL_KEY = "detail_url"

    BUSINESS_COLUMNS = [
        "nom",
        "categorie",
        "adresse",
        "telephone_principal",
        "telephones",
        "email_principal",
        "emails_trouves",
        "site_web",
        "nationalite",
        "date_creation",
        "dirigeant",
        "nombre_employes",
        "description_listing",
        "description",
        "listing_page",
        "listing_page_url",
        "detail_url",
        "status_scrape",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/immo/base_prospection_immomatin.csv"),
        xlsx_path=Path("exports/immo/base_prospection_immomatin.xlsx"),
        jsonl_path=Path("exports/immo/base_prospection_immomatin.jsonl"),
        email_column="email_principal",
        table_name="BaseImmo",
        sheet_name="Immobilier",
    )

    def __init__(self, data_dir=None, test_mode=False, start_page=1, max_pages=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        self.start_page = start_page
        self.max_pages = max_pages or (2 if test_mode else None)
        self.session = _build_session()
        self.parser = _get_parser()

    def _polite_sleep(self):
        time.sleep(random.uniform(REQUEST_SLEEP_MIN, REQUEST_SLEEP_MAX))

    def _get_soup(self, url: str):
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, self.parser)
        except Exception as e:
            self.log.warning("Chargement impossible: %s -> %s", url, e)
            return None

    def _parse_detail_page(self, detail_url: str) -> dict:
        row = {
            "nom": "",
            "categorie": "",
            "adresse": "",
            "telephone_principal": "",
            "telephones": "",
            "email_principal": "",
            "emails_trouves": "",
            "site_web": "",
            "nationalite": "",
            "date_creation": "",
            "dirigeant": "",
            "nombre_employes": "",
            "description": "",
            "detail_url": detail_url,
            "status_scrape": "ok",
        }

        soup = self._get_soup(detail_url)
        if soup is None:
            row["status_scrape"] = "erreur_chargement"
            return row

        article = soup.select_one("article.annuaire")
        if article is None:
            row["status_scrape"] = "article_introuvable"
            return row

        h1 = article.select_one("h1")
        if h1:
            row["nom"] = clean_text(h1.get_text(" ", strip=True))

        cats = article.select("ul.rubr li a")
        if cats:
            row["categorie"] = " | ".join(
                clean_text(a.get_text(" ", strip=True)) for a in cats
            )

        article_text = clean_text(article.get_text(" ", strip=True))

        mailto_emails = []
        for a in article.select('a[href^="mailto:"]'):
            href = a.get("href", "").strip()
            email = re.sub(r"^mailto:", "", href, flags=re.I).split("?")[0].strip().lower()
            if email:
                mailto_emails.append(email)

        regex_emails = extract_emails(article_text, filter_bad=False)
        all_emails, seen = [], set()
        for e in mailto_emails + regex_emails:
            if e not in seen:
                seen.add(e)
                all_emails.append(e)

        site_candidates = []
        for a in article.select("a[href]"):
            href = a.get("href", "").strip()
            if href.startswith("http") and "google.com/maps" not in href:
                if href not in site_candidates:
                    site_candidates.append(href)

        phones = extract_phones(article_text)

        adresse = _parse_label_value(article_text, "Adresse")
        if not adresse:
            p_tags = article.select("p")
            if p_tags:
                first_p = clean_text(p_tags[0].get_text(" ", strip=True))
                tmp = first_p
                for ph in phones:
                    tmp = tmp.replace(ph, " ")
                for em in all_emails:
                    tmp = tmp.replace(em, " ")
                for site in site_candidates:
                    tmp = tmp.replace(site, " ")
                tmp = re.sub(r"\s+", " ", tmp).strip(" -|,")
                tmp = re.sub(r"Description\s*:.*$", "", tmp, flags=re.I).strip()
                adresse = clean_text(tmp)

        description = _parse_label_value(article_text, "Description")
        if not description:
            p_texts = [clean_text(p.get_text(" ", strip=True)) for p in article.select("p")]
            long_ps = [p for p in p_texts if len(p) > 80]
            description = long_ps[-1] if long_ps else ""

        row.update({
            "adresse": adresse,
            "telephone_principal": first_or_empty(phones),
            "telephones": " | ".join(phones),
            "email_principal": first_or_empty(all_emails),
            "emails_trouves": " | ".join(all_emails),
            "site_web": first_or_empty(site_candidates),
            "nationalite": _parse_label_value(article_text, "Nationalité"),
            "date_creation": _parse_label_value(article_text, "Date de création"),
            "dirigeant": _parse_label_value(article_text, "Nom du dirigeant"),
            "nombre_employes": (
                _parse_label_value(article_text, "Nombre d\u2019employés")
                or _parse_label_value(article_text, "Nombre d'employés")
            ),
            "description": description,
        })

        return row

    def _scrape_listing_page(self, page_num: int) -> list[dict]:
        url = f"{BASE_URL}/annuaires/{page_num}.html"
        soup = self._get_soup(url)
        if soup is None:
            return []

        results = []
        for card in soup.select("a.annuaire__listing__itemlisting"):
            href = card.get("href", "").strip()
            if not href:
                continue
            name_el = card.select_one("h2")
            desc_el = card.select_one("p")
            results.append({
                "nom_listing": clean_text(name_el.get_text(" ", strip=True)) if name_el else "",
                "description_listing": clean_text(desc_el.get_text(" ", strip=True)) if desc_el else "",
                "detail_url": urljoin(BASE_URL, href),
                "listing_page": str(page_num),
                "listing_page_url": url,
            })
        self.log.info("[PAGE %03d] %d fiches trouvées", page_num, len(results))
        return results

    def iter_records(self) -> Iterator[dict]:
        page_num = self.start_page
        seen_urls: set[str] = set()

        while True:
            listing = self._scrape_listing_page(page_num)
            if not listing:
                self.log.info("[STOP] Plus de résultats page %d", page_num)
                break

            for item in listing:
                if item["detail_url"] in seen_urls:
                    continue
                seen_urls.add(item["detail_url"])

                detail = self._parse_detail_page(item["detail_url"])

                merged = {
                    "nom": detail.get("nom") or item.get("nom_listing", ""),
                    "categorie": detail.get("categorie", ""),
                    "adresse": detail.get("adresse", ""),
                    "telephone_principal": detail.get("telephone_principal", ""),
                    "telephones": detail.get("telephones", ""),
                    "email_principal": detail.get("email_principal", ""),
                    "emails_trouves": detail.get("emails_trouves", ""),
                    "site_web": detail.get("site_web", ""),
                    "nationalite": detail.get("nationalite", ""),
                    "date_creation": detail.get("date_creation", ""),
                    "dirigeant": detail.get("dirigeant", ""),
                    "nombre_employes": detail.get("nombre_employes", ""),
                    "description_listing": item.get("description_listing", ""),
                    "description": detail.get("description", ""),
                    "listing_page": str(item.get("listing_page", "")),
                    "listing_page_url": item.get("listing_page_url", ""),
                    "detail_url": item.get("detail_url", ""),
                    "status_scrape": detail.get("status_scrape", ""),
                }
                yield merged
                self._polite_sleep()

            self._polite_sleep()

            if self.max_pages and page_num >= self.start_page + self.max_pages - 1:
                self.log.info("Limite atteinte : %d pages", self.max_pages)
                break
            page_num += 1


if __name__ == "__main__":
    ImmoScraper(test_mode=True).run(mode="update")
