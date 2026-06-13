"""
core/utils.py
--------------

Utilitaires partagés entre tous les scrapers. La logique est strictement
identique à celle des notebooks d'origine pour ne rien casser côté parsing.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Logging unifié
# ---------------------------------------------------------------------------

def get_logger(name: str, log_file: Path | None = None) -> logging.Logger:
    """
    Logger structuré, même format dans stdout et fichier. Idempotent si
    rappelé plusieurs fois avec le même name + log_file.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Nettoyage texte (identique aux notebooks)
# ---------------------------------------------------------------------------

def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------

EMAIL_REGEX = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    flags=re.IGNORECASE,
)

BAD_EMAIL_FRAGMENTS = [
    "example.", "exemple.", "email.com", "test.", "localhost",
    "sentry", "dynatrace", "w3.org", "schema.org", "javascript",
    "noreply@", "no-reply@",
]


def is_bad_email(email: str) -> bool:
    low = email.lower()
    return any(x in low for x in BAD_EMAIL_FRAGMENTS)


def extract_emails(text: str | None, filter_bad: bool = True) -> list[str]:
    """
    Extrait les emails uniques (ordre préservé) depuis un texte. Par défaut
    on filtre les emails bidons (example.com, noreply, tracking, etc.).
    """
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in EMAIL_REGEX.findall(text):
        e = raw.lower().strip(".,;:)]}>\"'")
        if not e or e in seen:
            continue
        if e == "[email protected]":
            continue
        if filter_bad and is_bad_email(e):
            continue
        seen.add(e)
        out.append(e)
    return out


def pick_best_email(emails: list[str]) -> str:
    """Priorise les emails génériques métier (formation@, contact@, ...)."""
    if not emails:
        return ""
    priority = [
        "formation@", "formations@", "contact@", "info@", "accueil@",
        "administration@", "secretariat@", "commercial@",
    ]
    for prefix in priority:
        for email in emails:
            if email.startswith(prefix):
                return email
    return emails[0]


def decode_cfemail(encoded_hex: str) -> str:
    """
    Décode les emails obfusqués Cloudflare (attribut data-cfemail).
    Retourne '' si le hex est invalide.
    """
    try:
        key = int(encoded_hex[:2], 16)
        return "".join(
            chr(int(encoded_hex[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded_hex), 2)
        )
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Téléphones (format français)
# ---------------------------------------------------------------------------

PHONE_PATTERNS = [
    re.compile(r"\b0\d(?:[ .-]?\d{2}){4}\b"),
    re.compile(r"\b\+33[ .-]?[1-9](?:[ .-]?\d{2}){4}\b"),
]


def extract_phones(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for pattern in PHONE_PATTERNS:
        for m in pattern.findall(text):
            phone = clean_text(m)
            if phone and phone not in seen:
                seen.add(phone)
                out.append(phone)
    return out


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/.*)?", url):
        return "https://" + url
    return url


def extract_domain(url: str | None) -> str:
    if not url:
        return ""
    try:
        return urlparse(normalize_url(url)).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def is_social_domain(domain: str) -> bool:
    socials = [
        "instagram.com", "facebook.com", "linkedin.com", "tiktok.com",
        "youtube.com", "youtu.be", "pinterest.", "x.com", "twitter.com",
    ]
    return any(s in domain for s in socials)


def is_asset_url(url: str) -> bool:
    low = url.lower()
    markers = [
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
        "/cdn/shop/", "/cdn/shopifycloud/", "/files/", "/images/",
    ]
    return any(m in low for m in markers)


# ---------------------------------------------------------------------------
# Helpers joins / first
# ---------------------------------------------------------------------------

def first_or_empty(values: list[str]) -> str:
    return values[0] if values else ""


def join_pipe(values: list[str]) -> str:
    """Joint une liste de chaînes par ' | ', standard utilisé partout."""
    return " | ".join(v for v in values if v)
