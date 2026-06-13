"""
Package scrapers.

Chaque scraper est une classe qui hérite de `core.ScraperBase` et expose :
- VERTICAL, TABLE, BUSINESS_COLUMNS, NATURAL_KEY, EXPORT
- iter_records() -> Iterator[dict]

Enregistrement central ci-dessous : ajoute ici tout nouveau scraper pour
qu'il soit découvert automatiquement par le CLI et l'orchestrateur.
"""

from scrapers.auto_ecole import AutoEcoleScraper
from scrapers.ecommerce import EcommerceScraper
from scrapers.education import EducationScraper
from scrapers.france_travail import FranceTravailScraper
from scrapers.hotels import HotelsScraper
from scrapers.immo import ImmoScraper
from scrapers.notaires import NotairesScraper


REGISTRY = {
    "auto_ecole": AutoEcoleScraper,
    "ecommerce": EcommerceScraper,
    "education": EducationScraper,
    "france_travail": FranceTravailScraper,
    "hotels": HotelsScraper,
    "immo": ImmoScraper,
    "notaires": NotairesScraper,
}


__all__ = [
    "REGISTRY",
    "AutoEcoleScraper",
    "EcommerceScraper",
    "EducationScraper",
    "FranceTravailScraper",
    "HotelsScraper",
    "ImmoScraper",
    "NotairesScraper",
]
