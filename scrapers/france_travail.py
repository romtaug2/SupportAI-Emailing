"""
scrapers/france_travail.py
---------------------------

Scraper France Travail (formations). Utilise Playwright car le site est
rendu en JS. Logique de parsing identique au notebook d'origine.

Clé naturelle = url_detail.

L'interface publique expose `iter_records()` synchrone comme les autres
scrapers : on exécute le pipeline async en interne via asyncio et on
yield les fiches via une queue.

Installation Playwright requise :
    pip install playwright
    python -m playwright install chromium
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import threading
from pathlib import Path
from queue import Queue
from typing import Iterator
from urllib.parse import urlparse, urlunparse

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import (
    clean_text, extract_emails, extract_phones, pick_best_email,
)


BASE_SEARCH_URL = (
    "https://candidat.francetravail.fr/formations/recherche"
    "?filtreEstFormationEnCoursOuAVenir=formEnCours"
    "&filtreEstFormationTerminee=formEnCours"
    "&ou=DEPARTEMENT-{zone}"
    "&range={start}-{end}"
    "&tri=0"
)

DEPARTEMENTS_FULL = [
    "01", "02", "03", "04", "05", "06", "07", "08", "09",
    "10", "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "23", "24", "25", "26", "27", "28", "29", "2A", "2B",
    "30", "31", "32", "33", "34", "35", "36", "37", "38", "39",
    "40", "41", "42", "43", "44", "45", "46", "47", "48", "49",
    "50", "51", "52", "53", "54", "55", "56", "57", "58", "59",
    "60", "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "70", "71", "72", "73", "74", "75", "76", "77", "78", "79",
    "80", "81", "82", "83", "84", "85", "86", "87", "88", "89",
    "90", "91", "92", "93", "94", "95",
    "971", "972", "973", "974", "976",
]

DEPARTEMENTS_TEST = ["69"]


DETAIL_SLEEP_MIN = 0.7
DETAIL_SLEEP_MAX = 1.7
SITE_SLEEP_MIN = 0.5
SITE_SLEEP_MAX = 1.2


def _normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", p.query, ""))
    except Exception:
        return url


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def _email_confidence(source: str) -> int:
    return {
        "france_travail": 100,
        "site_contact": 90,
        "site_mentions": 80,
        "site_public": 70,
        "introuvable": 0,
    }.get(source, 0)


def _build_candidate_contact_urls(site_url: str) -> list[str]:
    if not site_url:
        return []
    parsed = urlparse(site_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    paths = [
        "", "/contact", "/contacts", "/nous-contacter", "/contactez-nous",
        "/mentions-legales", "/mentions-l\u00e9gales",
        "/qui-sommes-nous", "/a-propos",
    ]
    urls, seen = [], set()
    for path in paths:
        u = root + path
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


async def _sleep_between(min_s, max_s):
    await asyncio.sleep(random.uniform(min_s, max_s))


class FranceTravailScraper(ScraperBase):
    VERTICAL = "france_travail"
    TABLE = "france_travail"
    NATURAL_KEY = "url_detail"

    BUSINESS_COLUMNS = [
        "zone",
        "formation",
        "organisme",
        "ville",
        "adresse",
        "duree",
        "prochaine_session",
        "site_web",
        "email_principal",
        "emails_trouves",
        "email_source",
        "email_confiance",
        "telephone_principal",
        "telephones_trouves",
        "certifiante",
        "financement",
        "type_formation",
        "objectif",
        "contenu",
        "validation",
        "url_detail",
        "url_listing",
        "status_scrape",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/france_travail/francetravail_base.csv"),
        xlsx_path=Path("exports/france_travail/francetravail_base.xlsx"),
        jsonl_path=Path("exports/france_travail/francetravail_base.jsonl"),
        email_column="email_principal",
        table_name="BaseFranceTravail",
        sheet_name="Formations",
    )

    def __init__(
        self,
        data_dir=None,
        test_mode=False,
        zones=None,
        max_pages=None,
        enrich_emails=True,
        max_enrichments=None,
    ):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        self.zones = zones or (DEPARTEMENTS_TEST if test_mode else DEPARTEMENTS_FULL)
        self.max_pages = max_pages or (2 if test_mode else 2000)
        self.enrich_emails = enrich_emails
        self.max_enrichments = max_enrichments or (10 if test_mode else 100000)

    # ------------------------------------------------------------------
    # Pipeline async
    # ------------------------------------------------------------------

    async def _extract_listing_items(self, page, listing_url):
        links = await page.locator("a[href*='/formations/detail/']").evaluate_all(
            """els => els.map(a => ({
                href: a.href,
                text: a.innerText || ''
            }))"""
        )
        items, seen = [], set()
        for link in links:
            href = _normalize_url(link.get("href", ""))
            if not href or href in seen:
                continue
            seen.add(href)
            items.append({
                "url_detail": href,
                "listing_text": clean_text(link.get("text", "")),
                "url_listing": listing_url,
            })
        return items

    async def _extract_visible_external_sites(self, page):
        links = await page.locator("a[href]").evaluate_all(
            """els => els.map(a => ({
                href: a.href,
                text: a.innerText || ''
            }))"""
        )
        excluded = [
            "francetravail.fr", "pole-emploi.fr", "facebook.com",
            "linkedin.com", "twitter.com", "x.com", "instagram.com",
            "youtube.com", "google.", "gouv.fr",
        ]
        sites, seen = [], set()
        for link in links:
            href = link.get("href", "")
            domain = _get_domain(href)
            if not href.startswith("http"):
                continue
            if any(x in domain for x in excluded):
                continue
            if href not in seen:
                seen.add(href)
                sites.append(href)
        return sites

    async def _parse_detail_page(self, context, url_detail, url_listing="", zone=""):
        page = await context.new_page()

        row = {c: "" for c in self.BUSINESS_COLUMNS}
        row.update({
            "zone": zone,
            "url_detail": url_detail,
            "url_listing": url_listing,
            "email_source": "introuvable",
            "email_confiance": "0",
            "status_scrape": "ok",
        })

        try:
            await page.goto(url_detail, wait_until="networkidle", timeout=90000)
            await page.wait_for_timeout(1500)

            text_raw = await page.locator("body").inner_text()
            text = clean_text(text_raw)
            lines = [clean_text(x) for x in text_raw.split("\n") if clean_text(x)]

            emails = extract_emails(text, filter_bad=True)
            phones = extract_phones(text)

            row["emails_trouves"] = " | ".join(emails)
            row["email_principal"] = pick_best_email(emails)
            if row["email_principal"]:
                row["email_source"] = "france_travail"
                row["email_confiance"] = str(_email_confidence("france_travail"))

            row["telephones_trouves"] = " | ".join(phones)
            row["telephone_principal"] = phones[0] if phones else ""

            h1 = page.locator("h1")
            if await h1.count():
                row["formation"] = clean_text(await h1.first.inner_text())

            def after_label(label, max_lines=1):
                label_clean = clean_text(label).lower().rstrip(":")
                for i, line in enumerate(lines):
                    if clean_text(line).lower().rstrip(":") == label_clean and i + 1 < len(lines):
                        vals = []
                        for j in range(i + 1, min(i + 1 + max_lines, len(lines))):
                            vals.append(lines[j])
                        return " | ".join(vals)
                return ""

            def contains_line(prefix):
                for line in lines:
                    if prefix.lower() in line.lower():
                        return line
                return ""

            row["duree"] = contains_line("Durée de")
            row["objectif"] = after_label("Objectif général", 2)
            row["contenu"] = after_label("Contenu", 5)
            row["validation"] = after_label("Validation", 4)
            row["type_formation"] = after_label("Type de formation", 1)
            row["certifiante"] = after_label("La formation est-elle certifiante ?", 1)
            row["financement"] = contains_line("Formation financée")
            row["prochaine_session"] = contains_line("Prochaine session")
            row["organisme"] = after_label("Organisme", 1)

            lieu_idx = None
            for i, line in enumerate(lines):
                if line.lower().rstrip(":") == "lieu de la formation":
                    lieu_idx = i
                    break

            if lieu_idx is not None:
                stop_labels = [
                    "type de formation", "la formation est-elle certifiante ?",
                    "pré-requis", "prérequis", "financement",
                    "rémunérations et aides",
                ]
                addr_parts = []
                for j in range(lieu_idx + 1, min(lieu_idx + 8, len(lines))):
                    if lines[j].lower().rstrip(":") in stop_labels:
                        break
                    addr_parts.append(lines[j])
                row["adresse"] = " | ".join(addr_parts)
                for part in addr_parts:
                    if re.search(r"\b\d{5}\b", part):
                        row["ville"] = part

            sites = await self._extract_visible_external_sites(page)
            if sites:
                row["site_web"] = sites[0]

        except Exception as e:
            row["status_scrape"] = f"erreur: {e}"
        finally:
            await page.close()

        return row

    async def _enrich_organism_from_site(self, context, site_url):
        result = {
            "email_principal": "",
            "emails_trouves": "",
            "email_source": "introuvable",
            "email_confiance": 0,
            "telephone_principal": "",
            "telephones_trouves": "",
        }
        if not site_url:
            return result

        page = await context.new_page()
        all_emails, all_phones = [], []
        source = "introuvable"

        try:
            for url in _build_candidate_contact_urls(site_url):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(800)
                    try:
                        text = await page.locator("body").inner_text(timeout=8000)
                    except Exception:
                        text = await page.content()

                    emails = extract_emails(text)
                    phones = extract_phones(text)

                    for e in emails:
                        if e not in all_emails:
                            all_emails.append(e)
                    for p in phones:
                        if p not in all_phones:
                            all_phones.append(p)

                    if emails:
                        low = url.lower()
                        if "contact" in low:
                            source = "site_contact"
                        elif "mention" in low:
                            source = "site_mentions"
                        else:
                            source = "site_public"
                        break

                    await _sleep_between(SITE_SLEEP_MIN, SITE_SLEEP_MAX)
                except Exception:
                    continue
        finally:
            await page.close()

        result["emails_trouves"] = " | ".join(all_emails)
        result["email_principal"] = pick_best_email(all_emails)
        result["telephones_trouves"] = " | ".join(all_phones)
        result["telephone_principal"] = all_phones[0] if all_phones else ""

        if result["email_principal"]:
            result["email_source"] = source
            result["email_confiance"] = _email_confidence(source)

        return result

    async def _scrape_async(self, queue: Queue):
        from playwright.async_api import async_playwright

        organism_cache: dict = {}

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="fr-FR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            listing_page = await context.new_page()
            enriched_count = 0

            # On stocke localement les rows pour l'étape d'enrichissement
            buffered_rows: list[dict] = []

            for zone in self.zones:
                self.log.info("=" * 40)
                self.log.info("ZONE %s", zone)
                empty = 0

                for page_index in range(self.max_pages):
                    start = page_index * 10
                    end = start + 9
                    url = BASE_SEARCH_URL.format(zone=zone, start=start, end=end)
                    self.log.info("[%s] listing page %d range %d-%d",
                                  zone, page_index + 1, start, end)

                    try:
                        await listing_page.goto(url, wait_until="networkidle", timeout=90000)
                        await listing_page.wait_for_timeout(2000)
                    except Exception as e:
                        self.log.warning("Listing impossible : %s", e)
                        empty += 1
                        if empty >= 3:
                            break
                        continue

                    items = await self._extract_listing_items(listing_page, url)
                    self.log.info("[%s] %d fiches détectées", zone, len(items))

                    if not items:
                        empty += 1
                        if empty >= 3:
                            break
                        continue
                    empty = 0

                    for idx, item in enumerate(items, start=1):
                        row = await self._parse_detail_page(
                            context=context,
                            url_detail=item["url_detail"],
                            url_listing=url,
                            zone=zone,
                        )
                        buffered_rows.append(row)
                        await _sleep_between(DETAIL_SLEEP_MIN, DETAIL_SLEEP_MAX)

            await listing_page.close()

            # Étape d'enrichissement : on complète les rows sans email en
            # visitant le site web de l'organisme. Le cache évite de visiter
            # plusieurs fois le même domaine.
            if self.enrich_emails:
                self.log.info("Enrichissement emails organismes")
                for row in buffered_rows:
                    if row.get("email_principal") or not row.get("site_web"):
                        continue
                    key = f"{clean_text(row.get('organisme', '')).lower()}||{_get_domain(row.get('site_web', ''))}"

                    if key in organism_cache:
                        enrich = organism_cache[key]
                    else:
                        if enriched_count >= self.max_enrichments:
                            break
                        self.log.info("[ENRICH] %s -> %s",
                                      row.get("organisme", ""), row.get("site_web", ""))
                        enrich = await self._enrich_organism_from_site(
                            context, row.get("site_web", "")
                        )
                        organism_cache[key] = enrich
                        enriched_count += 1

                    if enrich.get("email_principal"):
                        row["email_principal"] = enrich["email_principal"]
                        row["emails_trouves"] = enrich["emails_trouves"]
                        row["email_source"] = enrich["email_source"]
                        row["email_confiance"] = str(enrich["email_confiance"])
                    if not row.get("telephone_principal") and enrich.get("telephone_principal"):
                        row["telephone_principal"] = enrich["telephone_principal"]
                        row["telephones_trouves"] = enrich["telephones_trouves"]

            await browser.close()

            for row in buffered_rows:
                queue.put(row)
            queue.put(None)  # sentinelle de fin

    def iter_records(self) -> Iterator[dict]:
        """
        Exécute le pipeline async dans un thread dédié (compat Windows/notebook),
        yield les fiches au fur et à mesure. La sentinelle None signale la fin.
        """
        queue: Queue = Queue(maxsize=500)
        error_box: list = []

        def runner():
            try:
                if sys.platform.startswith("win"):
                    loop = asyncio.ProactorEventLoop()
                else:
                    loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._scrape_async(queue))
                loop.close()
            except Exception as e:
                error_box.append(e)
                queue.put(None)

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()

        while True:
            item = queue.get()
            if item is None:
                break
            yield item

        thread.join()
        if error_box:
            raise error_box[0]


if __name__ == "__main__":
    FranceTravailScraper(test_mode=True).run(mode="update")
