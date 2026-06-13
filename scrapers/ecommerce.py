"""
scrapers/ecommerce.py
----------------------

Scraper annuaire-du-ecommerce.com. Logique de parsing identique au
notebook d'origine, en générateur. Clé naturelle = shop_url.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from core.scraper_base import ExportConfig, ScraperBase
from core.utils import (
    clean_text, decode_cfemail, extract_domain, extract_emails,
    extract_phones, is_asset_url, is_social_domain, normalize_url,
)


BASE = "https://www.annuaire-du-ecommerce.com"
START_URL = f"{BASE}/sites"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 30
REQUEST_DELAY = 0.35
RETRY_COUNT = 3
RETRY_SLEEP = 2


def _safe_text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def _looks_like_annuaire_domain(domain: str) -> bool:
    return "annuaire-du-ecommerce.com" in domain


def _find_domains_in_text(text: str) -> list[str]:
    matches = re.findall(
        r"\b(?:https?://)?(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s\"'<>()]*)?",
        text,
    )
    out, seen = [], set()
    for m in matches:
        m = m.strip(" .,;:!?)]}\"'")
        full = normalize_url(m)
        domain = extract_domain(full)
        if not domain or _looks_like_annuaire_domain(domain) or domain in seen:
            continue
        seen.add(domain)
        out.append(full)
    return out


def _choose_best_website(candidates: list[str]) -> str:
    if not candidates:
        return ""
    scored = []
    for c in candidates:
        full = normalize_url(c)
        domain = extract_domain(full)
        if not domain or _looks_like_annuaire_domain(domain) or is_social_domain(domain):
            continue

        score = 0
        if full.startswith("https://"):
            score += 2
        elif full.startswith("http://"):
            score += 1

        if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", domain):
            score += 3
        else:
            score += 1

        if not is_asset_url(full):
            score += 4

        parsed = urlparse(full)
        if parsed.path in ("", "/"):
            score += 3

        scored.append((score, full))

    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1]


def _extract_external_links(soup: BeautifulSoup) -> list[str]:
    links, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = normalize_url(a["href"].strip())
        if not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def _extract_social_links(links: list[str]) -> dict:
    socials = {
        "instagram": "", "facebook": "", "linkedin": "", "tiktok": "",
        "youtube": "", "pinterest": "", "x_twitter": "",
    }
    for link in links:
        low = link.lower()
        if "instagram.com" in low and not socials["instagram"]:
            socials["instagram"] = link
        elif "facebook.com" in low and not socials["facebook"]:
            socials["facebook"] = link
        elif "linkedin.com" in low and not socials["linkedin"]:
            socials["linkedin"] = link
        elif "tiktok.com" in low and not socials["tiktok"]:
            socials["tiktok"] = link
        elif ("youtube.com" in low or "youtu.be" in low) and not socials["youtube"]:
            socials["youtube"] = link
        elif "pinterest." in low and not socials["pinterest"]:
            socials["pinterest"] = link
        elif ("twitter.com" in low or "x.com" in low) and not socials["x_twitter"]:
            socials["x_twitter"] = link
    return socials


def _extract_rating_and_reviews(text: str) -> tuple:
    m_rating = re.search(r"\b(\d+(?:\.\d+)?)\s*\(\s*(\d+)\s*avis\s*\)", text, flags=re.I)
    if m_rating:
        return m_rating.group(1), m_rating.group(2)
    m_reviews = re.search(r"\b(\d+)\s+avis\b", text, flags=re.I)
    if m_reviews:
        return "", m_reviews.group(1)
    return "", ""


def _extract_added_date(text: str) -> str:
    m = re.search(r"Ajouté le\s+([0-9]{1,2}\s+\w+\s+[0-9]{4})", text, flags=re.I)
    return m.group(1) if m else ""


class EcommerceScraper(ScraperBase):
    VERTICAL = "ecommerce"
    TABLE = "ecommerce"
    NATURAL_KEY = "shop_url"

    BUSINESS_COLUMNS = [
        "category_name",
        "category_slug",
        "category_url",
        "category_count_hint",
        "listing_page_number",
        "listing_page_url",
        "shop_name_from_listing",
        "shop_url",
        "shop_slug",
        "shop_title_page",
        "listing_description",
        "listing_logo_url",
        "listing_website",
        "website",
        "website_domain",
        "primary_email",
        "all_emails",
        "email_count",
        "email_sources",
        "phones",
        "phone_count",
        "rating",
        "review_count",
        "added_date",
        "description",
        "instagram",
        "facebook",
        "linkedin",
        "tiktok",
        "youtube",
        "pinterest",
        "x_twitter",
        "status",
        "error",
    ]

    EXPORT = ExportConfig(
        csv_path=Path("exports/ecommerce/annuaire_boutiques_complet.csv"),
        xlsx_path=Path("exports/ecommerce/annuaire_boutiques_complet.xlsx"),
        jsonl_path=Path("exports/ecommerce/annuaire_boutiques_complet.jsonl"),
        email_column="primary_email",
        table_name="BaseEcommerce",
        sheet_name="Boutiques",
    )

    def __init__(self, data_dir=None, test_mode=False, max_categories=None):
        if data_dir is not None:
            super().__init__(data_dir=data_dir, test_mode=test_mode)
        else:
            super().__init__(test_mode=test_mode)
        self.max_categories = max_categories or (2 if test_mode else None)
        self.session = self._build_session()

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s

    def _fetch(self, url: str) -> str:
        last_exc = None
        for attempt in range(1, RETRY_COUNT + 1):
            try:
                r = self.session.get(url, timeout=TIMEOUT)
                r.raise_for_status()
                return r.text
            except Exception as e:
                last_exc = e
                self.log.warning("retry %d/%d erreur fetch %s -> %s",
                                 attempt, RETRY_COUNT, url, e)
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_SLEEP)
        raise last_exc

    def _find_category_links(self, index_html: str) -> list[dict]:
        soup = BeautifulSoup(index_html, "html.parser")
        categories, seen = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith("/sites/") or href == "/sites":
                continue
            full_url = urljoin(BASE, href)
            slug = href.rstrip("/").split("/")[-1]
            if "?" in href or not slug or full_url in seen:
                continue
            seen.add(full_url)

            text = _safe_text(a)
            m = re.search(r"(\d+)\s*boutiques", text, flags=re.I)
            categories.append({
                "category_name": text,
                "category_slug": slug,
                "category_url": full_url,
                "category_count_hint": int(m.group(1)) if m else None,
            })
        categories.sort(key=lambda x: x["category_slug"])
        return categories

    def _get_category_title_and_count(self, html: str) -> tuple:
        soup = BeautifulSoup(html, "html.parser")
        title = _safe_text(soup.find("h1"))
        text = soup.get_text(" ", strip=True)
        m = re.search(r"(\d+)\s+boutiques référencées", text, flags=re.I)
        total = int(m.group(1)) if m else None
        return title, total

    def _find_total_pages(self, category_url: str, html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        max_page = 1
        for a in soup.find_all("a", href=True):
            full_url = urljoin(BASE, a["href"])
            if not full_url.startswith(category_url):
                continue
            qs = parse_qs(urlparse(full_url).query)
            if "page" in qs:
                try:
                    max_page = max(max_page, int(qs["page"][0]))
                except Exception:
                    pass
        return max_page

    def _extract_shop_links_from_listing(self, html: str) -> list[dict]:
        results: dict[str, dict] = {}

        # 1) JSON-LD ItemList
        for raw in re.findall(
            r'<script type="application/ld\+json">(.*?)</script>',
            html,
            flags=re.DOTALL,
        ):
            try:
                data = json.loads(raw)
            except Exception:
                continue

            item_lists = []
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                item_lists.append(data)
            elif isinstance(data, list):
                item_lists.extend(
                    [x for x in data if isinstance(x, dict) and x.get("@type") == "ItemList"]
                )

            for item_list in item_lists:
                for item in item_list.get("itemListElement", []):
                    if not isinstance(item, dict):
                        continue
                    shop_url = item.get("url")
                    shop_name = item.get("name", "")
                    if not shop_url or "/site/" not in shop_url:
                        continue
                    if shop_url not in results:
                        results[shop_url] = {
                            "shop_name": shop_name, "shop_url": shop_url,
                            "listing_description": "", "listing_logo_url": "",
                            "listing_website": "",
                        }
                    else:
                        if shop_name and not results[shop_url]["shop_name"]:
                            results[shop_url]["shop_name"] = shop_name

        # 2) Blocs Next/React
        pattern = re.compile(
            r'"slug":"(?P<slug>[^"]+)".*?'
            r'"name":"(?P<n>[^"]+)".*?'
            r'"description":"(?P<desc>.*?)".*?'
            r'"logoUrl":"(?P<logo>.*?)"',
            flags=re.DOTALL,
        )
        for m in pattern.finditer(html):
            slug = m.group("slug")
            name = m.group("n")
            desc = m.group("desc").encode("utf-8").decode("unicode_escape")
            logo = m.group("logo").encode("utf-8").decode("unicode_escape")

            shop_url = f"{BASE}/site/{slug}"
            candidates = []
            candidates.extend(_find_domains_in_text(desc))
            if logo:
                candidates.append(logo)
            listing_website = _choose_best_website(candidates)

            if shop_url not in results:
                results[shop_url] = {
                    "shop_name": name, "shop_url": shop_url,
                    "listing_description": desc, "listing_logo_url": logo,
                    "listing_website": listing_website,
                }
            else:
                if name and not results[shop_url]["shop_name"]:
                    results[shop_url]["shop_name"] = name
                if desc:
                    results[shop_url]["listing_description"] = desc
                if logo:
                    results[shop_url]["listing_logo_url"] = logo
                if listing_website:
                    results[shop_url]["listing_website"] = listing_website

        return list(results.values())

    def _extract_emails_with_source(self, html: str, soup: BeautifulSoup) -> list[tuple]:
        found = []

        def add(email, source):
            if not email:
                return
            email = email.strip().lower()
            if email == "[email protected]":
                return
            if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email):
                return
            found.append((email, source))

        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                email = re.sub(r"^mailto:", "", href, flags=re.I).split("?")[0].strip()
                add(email, "mailto")

        for tag in soup.find_all(attrs={"data-cfemail": True}):
            try:
                add(decode_cfemail(tag["data-cfemail"]), "cloudflare_data")
            except Exception:
                pass

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "email-protection" in href.lower():
                m = re.search(r"#([0-9a-fA-F]+)", href)
                if m:
                    try:
                        add(decode_cfemail(m.group(1)), "cloudflare_href")
                    except Exception:
                        pass

        for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", html):
            add(email, "regex_html")

        text = soup.get_text(" ", strip=True)
        for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
            add(email, "regex_text")

        out, seen = [], set()
        for email, source in found:
            if email not in seen:
                seen.add(email)
                out.append((email, source))
        return out

    def _extract_shop_page_data(self, shop_url: str) -> dict:
        html = self._fetch(shop_url)
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        title = _safe_text(soup.find("h1"))
        emails = self._extract_emails_with_source(html, soup)
        links = _extract_external_links(soup)
        phones = extract_phones(text)
        socials = _extract_social_links(links)
        rating, review_count = _extract_rating_and_reviews(text)
        added_date = _extract_added_date(text)

        candidates = []
        candidates.extend(links)
        candidates.extend(_find_domains_in_text(text))
        website = _choose_best_website(candidates)

        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            description = meta_desc["content"].strip()

        return {
            "shop_title_page": title,
            "website": website,
            "website_domain": extract_domain(website) if website else "",
            "emails": emails,
            "phones": phones,
            "rating": rating,
            "review_count": review_count,
            "added_date": added_date,
            "description": description,
            **socials,
        }

    def _make_row(self, category: dict, page_num: int, page_url: str, shop: dict) -> dict:
        shop_url = shop["shop_url"]
        return {
            "category_name": category["category_name"],
            "category_slug": category["category_slug"],
            "category_url": category["category_url"],
            "category_count_hint": str(category.get("category_count_hint") or ""),
            "listing_page_number": str(page_num),
            "listing_page_url": page_url,
            "shop_name_from_listing": shop.get("shop_name", ""),
            "shop_url": shop_url,
            "shop_slug": shop_url.rstrip("/").split("/")[-1],
            "shop_title_page": "",
            "listing_description": shop.get("listing_description", ""),
            "listing_logo_url": shop.get("listing_logo_url", ""),
            "listing_website": shop.get("listing_website", ""),
            "website": "",
            "website_domain": "",
            "primary_email": "",
            "all_emails": "",
            "email_count": "0",
            "email_sources": "",
            "phones": "",
            "phone_count": "0",
            "rating": "",
            "review_count": "",
            "added_date": "",
            "description": "",
            "instagram": "",
            "facebook": "",
            "linkedin": "",
            "tiktok": "",
            "youtube": "",
            "pinterest": "",
            "x_twitter": "",
            "status": "ok",
            "error": "",
        }

    def iter_records(self) -> Iterator[dict]:
        self.log.info("Chargement index catégories")
        index_html = self._fetch(START_URL)
        categories = self._find_category_links(index_html)
        self.log.info("Catégories trouvées : %d", len(categories))

        if self.max_categories:
            categories = categories[: self.max_categories]
            self.log.info("Limite catégories : %d", self.max_categories)

        for c_idx, category in enumerate(categories, start=1):
            category_url = category["category_url"]
            self.log.info("[CATEGORIE %d/%d] %s", c_idx, len(categories), category_url)

            try:
                first_html = self._fetch(category_url)
            except Exception as e:
                self.log.error("Erreur catégorie : %s", e)
                continue

            real_title, total_hint = self._get_category_title_and_count(first_html)
            if real_title:
                category["category_name"] = real_title
            if total_hint is not None:
                category["category_count_hint"] = total_hint

            total_pages = self._find_total_pages(category_url, first_html)
            self.log.info("  titre: %s | boutiques: %s | pages: %d",
                          category["category_name"],
                          category.get("category_count_hint"),
                          total_pages)

            for page_num in range(1, total_pages + 1):
                page_url = category_url if page_num == 1 else f"{category_url}?page={page_num}"
                self.log.info("  [PAGE %d/%d] %s", page_num, total_pages, page_url)

                try:
                    listing_html = first_html if page_num == 1 else self._fetch(page_url)
                except Exception as e:
                    self.log.error("Erreur listing : %s", e)
                    continue

                shops = self._extract_shop_links_from_listing(listing_html)
                self.log.info("    fiches détectées : %d", len(shops))

                for shop in shops:
                    shop_url = shop["shop_url"]
                    row = self._make_row(category, page_num, page_url, shop)

                    try:
                        details = self._extract_shop_page_data(shop_url)
                        emails = details["emails"]
                        phones = details["phones"]
                        final_website = details["website"] or shop.get("listing_website", "")

                        row.update({
                            "shop_title_page": details["shop_title_page"],
                            "website": final_website,
                            "website_domain": extract_domain(final_website) if final_website else "",
                            "primary_email": emails[0][0] if emails else "",
                            "all_emails": " | ".join(email for email, _ in emails),
                            "email_count": str(len(emails)),
                            "email_sources": " | ".join(source for _, source in emails),
                            "phones": " | ".join(phones),
                            "phone_count": str(len(phones)),
                            "rating": details["rating"],
                            "review_count": details["review_count"],
                            "added_date": details["added_date"],
                            "description": details["description"] or shop.get("listing_description", ""),
                            "instagram": details["instagram"],
                            "facebook": details["facebook"],
                            "linkedin": details["linkedin"],
                            "tiktok": details["tiktok"],
                            "youtube": details["youtube"],
                            "pinterest": details["pinterest"],
                            "x_twitter": details["x_twitter"],
                        })
                    except Exception as e:
                        row["website"] = shop.get("listing_website", "")
                        row["website_domain"] = extract_domain(row["website"]) if row["website"] else ""
                        row["status"] = "error"
                        row["error"] = str(e)

                    yield row
                    time.sleep(REQUEST_DELAY)


if __name__ == "__main__":
    EcommerceScraper(test_mode=True).run(mode="update")
