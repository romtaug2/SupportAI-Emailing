"""
╔══════════════════════════════════════════════════════════════════╗
║              SUPPORTAI - SCRIPT D'ENVOI EMAIL v1                 ║
║         L'assistant IA de votre service client                   ║
╠══════════════════════════════════════════════════════════════════╣
║  Compatible GitHub Actions                                       ║
║  Mode TEST : envoie le pitch commercial complet à toi-même       ║
║             chaque jour pour valider :                           ║
║             - Brevo SMTP + DKIM + SPF + DMARC                    ║
║             - Qualité visuelle du pitch                          ║
║             - Délivrabilité (inbox vs spam)                      ║
║  Usage : python send_supportai.py                                ║
╚══════════════════════════════════════════════════════════════════╝

Seul secret obligatoire : SMTP_PASSWORD (GitHub Secret).
Tout le reste est hardcodé (config publique non sensible).
"""

import csv
import os
import random
import ssl
import smtplib
import sys
import time
import urllib.request
from urllib.parse import urlparse, quote
from datetime import datetime, timezone, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid
from pathlib import Path

# ════════════════════════════════════════════════════════════════════
#  ①  CONFIGURATION (publique, hardcodée)
# ════════════════════════════════════════════════════════════════════

SMTP_SERVER = "smtp-relay.brevo.com"
SMTP_PORT = 587
SMTP_LOGIN = "acd893001@smtp-brevo.com"

FROM_NAME = "SupportAI - L'assistant IA de votre service client"
FROM_EMAIL = "contact@supportai.fr"
REPLY_TO = "romtaug+supportai@gmail.com"
TEST_RECIPIENT = "romtaug@gmail.com"

SITE_URL = "https://supportai.fr"
TARIF_URL = "https://supportai.fr#tarif"

# Liens de paiement Stripe (coller les liens buy.stripe.com dès qu'ils existent ;
# tant qu'ils sont vides, les boutons pointent vers la section tarif du site)
STRIPE_LINK_EXPRESS = "https://buy.stripe.com/aFafZb5PR4CB6KrbxjbII00"      # SupportAI Express - 499€
STRIPE_LINK_PREMIUM = "https://buy.stripe.com/cNi00d4LNglj5GnfNzbII01"      # SupportAI Premium - 799€
STRIPE_LINK_CODESOURCE = "https://buy.stripe.com/bJe28lguv3yx5GnfNzbII02"   # SupportAI Code Source - 1499€
EXPRESS_URL = STRIPE_LINK_EXPRESS or TARIF_URL
PREMIUM_URL = STRIPE_LINK_PREMIUM or TARIF_URL
CODESOURCE_URL = STRIPE_LINK_CODESOURCE or TARIF_URL
FONCTIONNEMENT_URL = "https://supportai.fr#fonctionnement"
CONFIGURATEUR_URL = "https://supportai.fr#configurateur"
# Espace de configuration (Streamlit) : accessible après commande,
# connexion sécurisée via Google.
STREAMLIT_URL = "https://supportai-config.streamlit.app/"
# Adresse de contact cliquable (mailto + footer). Le jour où la redirection
# contact@supportai.fr → Gmail est en place, remets "contact@supportai.fr" ici.
CONTACT_EMAIL = "romtaug+supportai@gmail.com"

# Vidéo démo Loom - laisser vide tant qu'elle n'est pas tournée.
# Dès que tu as le lien (https://www.loom.com/share/xxx), colle-le ici :
# le bloc vidéo avec miniature GIF animée s'activera automatiquement.
VIDEO_URL = os.getenv("VIDEO_URL", "https://www.loom.com/share/1409efb9abcd4f9dbdb82bea663a38eb").strip()

# ── Mode : TEST ou MASS ──────────────────────────────────────────────
#  TEST → envoie le pitch (contact fictif) à TEST_RECIPIENT
#  MASS → lit emailing/data/supportai_contacts_master.csv, prend les N
#         prochains 'pending', envoie, met à jour le CSV
#  Pauses volontairement COURTES (2-6 s) : à 300 emails/jour le run
#  GitHub Actions reste ≈ 20 min (≈ 440 min/mois, OK free tier).
SEND_MODE   = os.getenv("SEND_MODE", "TEST").strip().upper()
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT") or 300)
PAUSE_MIN   = int(os.getenv("PAUSE_MIN") or 2)
PAUSE_MAX   = int(os.getenv("PAUSE_MAX") or 6)

MASTER_PATH = Path(__file__).resolve().parent / "data" / "supportai_contacts_master.csv"

# Libellé du pill par vertical (header du mail, personnalisation légère)
VERTICAL_LABELS = {
    "ecommerce":      "E-commerce indépendant",
    "hotels":         "Hôtellerie indépendante",
    "immo":           "Professionnels de l'immobilier",
    "notaires":       "Offices notariaux",
    "education":      "Établissements de formation",
    "auto_ecole":     "Auto-écoles",
    "france_travail": "Organismes de formation",
}
DEFAULT_VERTICAL_LABEL = "Entreprises & indépendants"

# Identité légale - SIRET hardcodé (donnée publique : annuaire-entreprises.data.gouv.fr)
SIRET = "88281366000025"
SIRET_URL = f"https://annuaire-entreprises.data.gouv.fr/etablissement/{SIRET}"
SIREN = os.getenv("SIREN", "").strip()
ADRESSE_SOCIETE = os.getenv("ADRESSE_SOCIETE", "Lyon, France")
BASE_UNSUBSCRIBE_URL = "https://supportai.fr/unsubscribe"

if SIRET:
    COMPANY_ID_LABEL, COMPANY_ID_VALUE = "SIRET", SIRET
elif SIREN:
    COMPANY_ID_LABEL, COMPANY_ID_VALUE = "SIREN", SIREN
else:
    COMPANY_ID_LABEL, COMPANY_ID_VALUE = "Identifiant", "en cours d'attribution"

# Logo inline (PNG embarqué en CID, comme ThermoData)
BASE_DIR = Path(__file__).resolve().parent
SOURCE_DIR = BASE_DIR / "Source"
LOGO_WIDTH = 100


def _find_logo() -> Path | None:
    """Cherche le logo PNG : emailing/SupportAI.png ou emailing/Source/SupportAI.png."""
    for p in (BASE_DIR / "SupportAI.png", SOURCE_DIR / "SupportAI.png"):
        if p.exists():
            return p
    for d in (BASE_DIR, SOURCE_DIR):
        if d.exists():
            for f in sorted(d.glob("*.png")):
                return f
    return None


# ════════════════════════════════════════════════════════════════════
#  ②  HELPERS
# ════════════════════════════════════════════════════════════════════

def _password() -> str:
    pwd = os.environ.get("SMTP_PASSWORD", "").strip()
    if not pwd:
        print("❌ SMTP_PASSWORD absent dans l'environnement.")
        print("   → GitHub Settings → Secrets and variables → Actions")
        sys.exit(2)
    return pwd


def _french_date() -> str:
    mois = {1: "janvier", 2: "février", 3: "mars", 4: "avril",
            5: "mai", 6: "juin", 7: "juillet", 8: "août",
            9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre"}
    n = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=2)))
    return f"{n.day} {mois[n.month]} {n.year}"


def _format_company_id(value: str) -> str:
    """Formate un SIRET/SIREN avec espaces : 123 456 789 [01234]."""
    v = (value or "").replace(" ", "")
    if not v.isdigit():
        return value
    if len(v) == 9:
        return f"{v[0:3]} {v[3:6]} {v[6:9]}"
    if len(v) == 14:
        return f"{v[0:3]} {v[3:6]} {v[6:9]} {v[9:14]}"
    return value


# ── Loom (clone ThermoData) : miniature GIF animée auto-téléchargée ──

def _extract_loom_id(video_url: str) -> str | None:
    try:
        path = urlparse(video_url).path.strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "share":
            return parts[1]
        return None
    except Exception:
        return None


def _download_loom_gif(video_url: str, timeout: int = 5) -> bytes | None:
    loom_id = _extract_loom_id(video_url)
    if not loom_id:
        return None
    thumb_url = f"https://cdn.loom.com/sessions/thumbnails/{loom_id}-with-play.gif"
    try:
        req = urllib.request.Request(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content_type = (r.headers.get("Content-Type") or "").lower()
            data = r.read()
        if not data:
            return None
        if "gif" not in content_type and not data.startswith((b"GIF87a", b"GIF89a")):
            return None
        return data
    except Exception:
        return None


# ── Mailto pré-remplis (modèle de demande, comme ThermoData) ──

MAILTO_TEST_BODY = """Bonjour,

Je souhaite tester SupportAI pour mon site.

Mon site web : ...
Mon secteur : ...
Nombre de questions clients par jour (environ) : ...

Question ou précision : ...

Merci d'avance pour votre retour."""

MAILTO_QUESTION_BODY = """Bonjour,

J'ai une question avant de me lancer :

..."""


def _mailto(subject: str, body: str) -> str:
    return f"mailto:{CONTACT_EMAIL}?subject={quote(subject)}&body={quote(body)}"


# ════════════════════════════════════════════════════════════════════
#  ③  PERSONNALISATION (contact fake en mode test)
# ════════════════════════════════════════════════════════════════════

FAKE_CONTACT = {
    "company": "",
    "city": "",
    "department_code": "69",
    "vertical": "ecommerce",
    "email": TEST_RECIPIENT,
}


def _format_subject(contact: dict) -> str:
    company = contact.get("company", "").strip()
    if company:
        return f"🤖 {company} : un chatbot IA 24/7 clé en main"
    return "🤖 Un chatbot IA 24/7 clé en main pour votre service client"


def _format_greeting(contact: dict) -> str:
    company = contact.get("company", "").strip()
    city = contact.get("city", "").strip()
    if company and city:
        return (
            f"<strong style=\"color:#0A1F3D;\">{company}</strong>, à {city} : vos clients vous posent "
            "chaque jour les mêmes questions - <strong style=\"color:#0A1F3D;\">délais, retours, paiement, "
            "tailles, livraison</strong>... Et chaque question restée sans réponse immédiate, "
            "c'est un client qui hésite, puis qui part."
        )
    if company:
        return (
            f"<strong style=\"color:#0A1F3D;\">{company}</strong> : vos clients posent chaque jour "
            "les mêmes questions - délais, retours, paiement... Chaque question sans réponse "
            "immédiate, c'est un client qui hésite, puis qui part."
        )
    return ("Vos clients posent chaque jour les mêmes questions - délais, retours, paiement... "
            "Chaque question sans réponse immédiate, c'est un client qui hésite, puis qui part.")


# ════════════════════════════════════════════════════════════════════
#  ④  PLAIN TEXT (fallback)
# ════════════════════════════════════════════════════════════════════

def _build_plain_text(contact: dict, unsubscribe_url: str) -> str:
    company = contact.get("company", "")
    city = contact.get("city", "")
    greeting = f"Bonjour{' ' + company if company else ''},"

    return f"""{greeting}

SupportAI - L'assistant IA de votre service client

{f'Votre boutique {company} à {city} reçoit chaque jour les mêmes questions : "Quels sont vos délais ?", "Comment retourner un article ?", "Acceptez-vous Apple Pay ?".' if company else ''}

Aujourd'hui, vos clients tombent sur 2 options :
1) Un chatbot classique qui fait de la reconnaissance de mots-clés
   → 60% de questions sans réponse, frustration, panier abandonné
2) Vous (ou votre équipe) qui répondez à la main
   → Vous perdez 1-2h/jour à répéter les 20 mêmes questions

SupportAI installe sur votre site un vrai chatbot IA (propulsé par Gemini, Google)
qui comprend les questions formulées en langage naturel et y répond à partir
de votre FAQ Google Sheets.

DIFFÉRENCE CLÉ :
- Chatbots classiques : arbres de décision basés sur des mots-clés
- SupportAI : vraie compréhension du langage (LLM)

TROIS FORMULES, ZÉRO ABONNEMENT :
- Express 499€ : FAQ Q/R illimitées, vos couleurs, snippet à coller vous-même, formation visio + support email
- Premium 799€ (le plus populaire) : tout Express + installation par nos soins (visio ou sur place) + support prolongé
- Code Source 1499€ : repo GitHub complet, white-label, revente autorisée

COMMENT ÇA MARCHE - 4 ÉTAPES :
1) Vous nous envoyez votre FAQ (Excel, mail, oral, peu importe)
2) On structure la FAQ et on configure l'IA Gemini sur votre Google Sheet
3) Démo vidéo du chatbot, vous validez
4) Installation sur votre site + formation en visio. C'est en ligne.

Solutions concurrentes : 30 à 200€/mois en abonnement (jusqu'à 2 400€/an).
SupportAI : à partir de 499€, une fois. C'est tout.

Voir la démo : {SITE_URL}
Tarif : {TARIF_URL}
Comment ça marche : {FONCTIONNEMENT_URL}
Connexion Google disponible - Accédez à votre espace SupportAI en un clic, sans compte à créer : {STREAMLIT_URL}

Répondez à cet email pour démarrer : {CONTACT_EMAIL}
Désinscription : {unsubscribe_url}

---
SupportAI · {COMPANY_ID_LABEL} {_format_company_id(COMPANY_ID_VALUE)} · {ADRESSE_SOCIETE}
Vérifier notre entreprise : {SIRET_URL}
IA : Google Gemini · Conforme RGPD
Prospection B2B · Intérêt légitime art. 6.1.f RGPD
"""


# ════════════════════════════════════════════════════════════════════
#  ⑤  HTML COMPLET (le pitch commercial, branding SupportAI officiel)
# ════════════════════════════════════════════════════════════════════

def _vertical_label(contact: dict) -> str:
    return VERTICAL_LABELS.get((contact.get("vertical") or "").strip().lower(),
                               DEFAULT_VERTICAL_LABEL)


def _build_html(contact: dict, unsubscribe_url: str, has_logo: bool = False, has_thumb: bool = False) -> str:
    date_str = _french_date()
    greeting_html = _format_greeting(contact)
    vertical_label = _vertical_label(contact)

    # Logo : PNG inline (CID) si dispo, sinon fallback texte stylisé
    if has_logo:
        logo_html = f"""<a href="{SITE_URL}" style="text-decoration:none;">
        <img src="cid:supportai_logo" alt="SupportAI"
             width="{LOGO_WIDTH}" style="max-width:{LOGO_WIDTH}px;height:auto;display:block;margin:0 auto;"></a>"""
    else:
        logo_html = f"""<table cellpadding="0" cellspacing="0" style="margin:0 auto;">
    <tr><td style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:14px;padding:12px 24px;">
      <a href="{SITE_URL}" style="text-decoration:none;">
        <span style="font-size:22px;font-weight:bold;color:#0A1F3D;font-family:Arial,sans-serif;letter-spacing:-0.5px;">Support</span><span style="font-size:22px;font-weight:bold;color:#2196F3;font-family:Arial,sans-serif;letter-spacing:-0.5px;">AI</span>
      </a>
    </td></tr>
    </table>"""

    # Bloc démo : vidéo Loom (avec miniature GIF) si dispo, sinon CTA site
    if VIDEO_URL:
        thumb_tag = ""
        if has_thumb:
            thumb_tag = f"""
        <a href="{VIDEO_URL}" style="display:block;text-decoration:none;">
          <img src="cid:loom_thumb" alt="Voir la démo SupportAI" width="520"
               style="max-width:100%;height:auto;border-radius:10px;border:2px solid #BBDEFB;display:block;margin:0 auto 14px;">
        </a>"""
        demo_block = f"""
      <p style="font-size:18px;font-weight:bold;color:#0A1F3D;margin:0 0 14px;font-family:Arial,sans-serif;letter-spacing:-0.3px;">
        Voir SupportAI en action
      </p>
      {thumb_tag}
      <a href="{VIDEO_URL}"
         style="display:inline-block;background:#0A1F3D;background-image:linear-gradient(135deg,#0A1F3D 0%,#2196F3 100%);
                color:#ffffff;font-size:14px;font-weight:bold;padding:13px 28px;border-radius:999px;
                text-decoration:none;font-family:Arial,sans-serif;">
        ▶ Regarder la démo vidéo →
      </a>
      <p style="margin:14px 0 0;font-size:12px;line-height:1.6;font-family:Arial,sans-serif;">
        <a href="{CONFIGURATEUR_URL}" style="color:#1976D2;text-decoration:underline;font-weight:bold;">
          Tester le configurateur en direct
        </a>
      </p>"""
    else:
        demo_block = f"""
      <p style="font-size:18px;font-weight:bold;color:#0A1F3D;margin:0 0 6px;font-family:Arial,sans-serif;letter-spacing:-0.3px;">
        Voir SupportAI en action
      </p>
      <p style="font-size:13px;color:#4A5A6E;margin:0 0 16px;font-family:Arial,sans-serif;line-height:1.5;">
        Configurateur live + démo du chatbot directement sur le site.
      </p>
      <a href="{SITE_URL}"
         style="display:inline-block;background:#0A1F3D;background-image:linear-gradient(135deg,#0A1F3D 0%,#2196F3 100%);
                color:#ffffff;font-size:14px;font-weight:bold;padding:13px 28px;border-radius:999px;
                text-decoration:none;font-family:Arial,sans-serif;">
        Découvrir sur supportai.fr →
      </a>
      <p style="margin:14px 0 0;font-size:12px;line-height:1.6;font-family:Arial,sans-serif;">
        <a href="{CONFIGURATEUR_URL}" style="color:#1976D2;text-decoration:underline;font-weight:bold;">
          Voir le configurateur en direct
        </a>
      </p>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta name="color-scheme" content="light">
<meta name="x-apple-disable-message-reformatting">
<title>SupportAI - L'assistant IA de votre service client</title>
</head>
<body style="margin:0;padding:0;background:#F4F9FE;-webkit-text-size-adjust:100%;">

<table width="100%" cellpadding="0" cellspacing="0" role="presentation"
       style="background:#F4F9FE;padding:28px 14px 40px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" role="presentation"
       style="max-width:600px;width:100%;">

  <!-- Preheader caché (visible en preview Gmail/Outlook) -->
  <tr><td style="display:none;max-height:0;overflow:hidden;font-size:1px;color:#F4F9FE;">
    Les chatbots classiques font de la reconnaissance de mots-clés. SupportAI utilise une vraie IA Gemini. 499€ one-shot, zéro abonnement.
  </td></tr>

  <!-- Logo SupportAI -->
  <tr><td style="padding-bottom:16px;text-align:center;">
    {logo_html}
  </td></tr>

  <!-- Bandeau gradient navy → blue -->
  <tr><td>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#0A1F3D;background-image:linear-gradient(135deg,#0A1F3D 0%,#2196F3 100%);border-radius:10px;">
    <tr><td style="padding:12px 18px;text-align:center;">
      <p style="font-size:12px;font-weight:bold;color:#ffffff;letter-spacing:1px;
                text-transform:uppercase;margin:0;font-family:Arial,sans-serif;">
        Votre chatbot actuel comprend-il vraiment vos clients ?
      </p>
    </td></tr>
    </table>
  </td></tr>

  <!-- Bloc principal -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;">
    <tr><td style="padding:26px 22px 22px;">

      <!-- Pill date -->
      <table cellpadding="0" cellspacing="0">
      <tr><td style="background:#E3F2FD;border:1px solid #BBDEFB;border-radius:100px;padding:5px 14px;">
        <span style="font-size:11px;font-weight:bold;color:#0D47A1;letter-spacing:1px;
                     text-transform:uppercase;font-family:Arial,sans-serif;">
          {vertical_label} &middot; {date_str}
        </span>
      </td></tr>
      </table>

      <!-- H1 -->
      <h1 style="font-size:24px;font-weight:bold;color:#0A1F3D;line-height:1.25;
                margin:16px 0 8px;font-family:Arial,sans-serif;letter-spacing:-0.5px;">
        60% des questions clients<br>restent sans réponse.<br>
        <span style="color:#2196F3;">Et 1 panier sur 4 est abandonné<br>pour ça.</span>
      </h1>

      <!-- Bloc greeting personnalisé -->
      <p style="font-size:13px;color:#4A5A6E;line-height:1.7;margin:14px 0;
                font-family:Arial,sans-serif;background:#F4F9FE;border-left:3px solid #2196F3;
                padding:10px 14px;border-radius:0 8px 8px 0;">
        {greeting_html}
      </p>

      <p style="font-size:13px;color:#6B7B8E;font-style:italic;margin:0 0 18px;font-family:Arial,sans-serif;">
        Vos clients tapent "délais Apple Pay" - votre chatbot répond "désolé je n'ai pas compris".
      </p>

      <p style="color:#4A5A6E;font-size:13px;line-height:1.7;margin:0 0 14px;font-family:Arial,sans-serif;">
        Les chatbots classiques fonctionnent par <strong style="color:#0A1F3D;">arbres de décision
        et reconnaissance de mots-clés</strong>.
        Si votre client ne pose pas la question dans les mots EXACTS prévus, le bot fail.
        Et il fail dans <strong style="color:#0A1F3D;">60% des cas réels</strong>.
      </p>

      <p style="color:#4A5A6E;font-size:13px;line-height:1.7;margin:0;font-family:Arial,sans-serif;">
        <strong style="color:#0A1F3D;">SupportAI utilise une vraie IA</strong> (Google Gemini)
        qui comprend les questions formulées en langage naturel - n'importe quel phrasing,
        n'importe quelle faute de frappe, n'importe quelle langue. Le bot répond à partir
        de votre FAQ Google Sheets que vous modifiez quand vous voulez.
      </p>

    </td></tr>
    </table>
  </td></tr>

  <!-- Comparaison side-by-side -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <!-- Card "Chatbots classiques" -->
      <td width="50%" valign="top" style="padding-right:6px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#FFF1F2;border:1px solid #FECDD3;border-radius:12px;">
        <tr><td style="padding:16px 14px;">
          <p style="font-size:11px;font-weight:bold;color:#9F1239;margin:0 0 4px;
                    text-transform:uppercase;letter-spacing:0.8px;font-family:Arial,sans-serif;">
            Chatbots classiques
          </p>
          <p style="font-size:14px;font-weight:bold;color:#7F1D1D;margin:0 0 10px;font-family:Arial,sans-serif;">
            À base de mots-clés
          </p>
          <p style="font-size:12px;color:#7F1D1D;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            &#10005; Arbres de décision rigides<br>
            &#10005; Reconnaissance de mots-clés<br>
            &#10005; 50-200€/mois récurrent<br>
            &#10005; 60% questions sans réponse
          </p>
        </td></tr>
        </table>
      </td>
      <!-- Card "SupportAI" -->
      <td width="50%" valign="top" style="padding-left:6px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#ECFDF5;border:1px solid #A7F3D0;border-radius:12px;">
        <tr><td style="padding:16px 14px;">
          <p style="font-size:11px;font-weight:bold;color:#065F46;margin:0 0 4px;
                    text-transform:uppercase;letter-spacing:0.8px;font-family:Arial,sans-serif;">
            SupportAI
          </p>
          <p style="font-size:14px;font-weight:bold;color:#064E3B;margin:0 0 10px;font-family:Arial,sans-serif;">
            IA Google Gemini
          </p>
          <p style="font-size:12px;color:#064E3B;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            &#10003; Vraie compréhension du langage<br>
            &#10003; Toute question, tout phrasing<br>
            &#10003; <strong>Dès 499€ one-shot, zéro abonnement</strong><br>
            &#10003; FAQ modifiable en 5 min via Google Sheets
          </p>
        </td></tr>
        </table>
      </td>
    </tr>
    </table>
  </td></tr>

  <!-- KPIs -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="50%" style="padding-right:6px;padding-bottom:6px;" valign="top">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;text-align:center;">
            <tr><td style="padding:18px 8px;">
              <div style="font-size:24px;font-weight:bold;color:#2196F3;font-family:Georgia,serif;letter-spacing:-1px;">dès 499€</div>
              <div style="font-size:11px;color:#6B7B8E;margin-top:4px;font-family:Arial,sans-serif;">Paiement unique, 3 formules</div>
            </td></tr>
          </table>
        </td>
        <td width="50%" style="padding-left:6px;padding-bottom:6px;" valign="top">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;text-align:center;">
            <tr><td style="padding:18px 8px;">
              <div style="font-size:24px;font-weight:bold;color:#0A1F3D;font-family:Georgia,serif;letter-spacing:-1px;">4 étapes</div>
              <div style="font-size:11px;color:#6B7B8E;margin-top:4px;font-family:Arial,sans-serif;">De votre FAQ à la mise en ligne</div>
            </td></tr>
          </table>
        </td>
      </tr>
      <tr>
        <td width="50%" style="padding-right:6px;padding-top:6px;" valign="top">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;text-align:center;">
            <tr><td style="padding:18px 8px;">
              <div style="font-size:24px;font-weight:bold;color:#0D47A1;font-family:Georgia,serif;letter-spacing:-1px;">0€</div>
              <div style="font-size:11px;color:#6B7B8E;margin-top:4px;font-family:Arial,sans-serif;">Abonnement mensuel</div>
            </td></tr>
          </table>
        </td>
        <td width="50%" style="padding-left:6px;padding-top:6px;" valign="top">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;text-align:center;">
            <tr><td style="padding:18px 8px;">
              <div style="font-size:24px;font-weight:bold;color:#06B6D4;font-family:Georgia,serif;letter-spacing:-1px;">&#8734;</div>
              <div style="font-size:11px;color:#6B7B8E;margin-top:4px;font-family:Arial,sans-serif;">Q/R illimitées</div>
            </td></tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Bloc démo : vidéo Loom si dispo, sinon CTA site -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#F4F9FE;border:1px solid #BBDEFB;border-radius:14px;">
    <tr><td style="padding:22px 24px;text-align:center;">
{demo_block}
    </td></tr>
    </table>
  </td></tr>

  <!-- Connexion Google (style ThermoData) -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;">
    <tr><td style="padding:14px 20px;text-align:center;">
      <p style="margin:0;font-size:13px;line-height:1.6;font-family:Arial,sans-serif;color:#4A5A6E;">
        <a href="{STREAMLIT_URL}" style="color:#1976D2;text-decoration:none;font-weight:bold;">Connexion Google disponible</a>
        &mdash; Accédez à votre espace SupportAI en un clic, sans compte à créer.
      </p>
    </td></tr>
    </table>
  </td></tr>

  <!-- Features list -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;">
    <tr><td style="padding:24px 22px;">

      <p style="font-size:11px;font-weight:bold;text-transform:uppercase;
                letter-spacing:1.2px;color:#6B7B8E;margin:0 0 18px;font-family:Arial,sans-serif;">
        Ce qui est inclus
      </p>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="36" valign="top" style="font-size:20px;line-height:1;">&#128221;</td>
        <td style="padding-left:10px;padding-bottom:12px;">
          <p style="font-size:13px;font-weight:bold;color:#0A1F3D;margin:0 0 3px;font-family:Arial,sans-serif;">
            Création de votre FAQ (Q/R illimitées)
          </p>
          <p style="font-size:12px;color:#6B7B8E;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            Vous nous donnez vos questions/réponses via Google Sheet. On structure et optimise, sans limite de questions.
          </p>
        </td>
      </tr>
      </table>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="36" valign="top" style="font-size:20px;line-height:1;">&#127912;</td>
        <td style="padding-left:10px;padding-bottom:12px;">
          <p style="font-size:13px;font-weight:bold;color:#0A1F3D;margin:0 0 3px;font-family:Arial,sans-serif;">
            Personnalisation aux couleurs de votre marque
          </p>
          <p style="font-size:12px;color:#6B7B8E;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            20 couleurs au choix, ou votre HEX exact. Titre, message d'accueil, questions suggérées.
          </p>
        </td>
      </tr>
      </table>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="36" valign="top" style="font-size:20px;line-height:1;">&#9881;</td>
        <td style="padding-left:10px;padding-bottom:12px;">
          <p style="font-size:13px;font-weight:bold;color:#0A1F3D;margin:0 0 3px;font-family:Arial,sans-serif;">
            Compatible avec tous les sites
          </p>
          <p style="font-size:12px;color:#6B7B8E;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            WordPress, Shopify, Wix, Squarespace, Webflow, HTML, React. Snippet prêt à coller - ou installation par nos soins en formule Premium.
          </p>
        </td>
      </tr>
      </table>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="36" valign="top" style="font-size:20px;line-height:1;">&#127891;</td>
        <td style="padding-left:10px;padding-bottom:12px;">
          <p style="font-size:13px;font-weight:bold;color:#0A1F3D;margin:0 0 3px;font-family:Arial,sans-serif;">
            Formation en visio + support email inclus
          </p>
          <p style="font-size:12px;color:#6B7B8E;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            Vous savez modifier votre FAQ vous-même via Google Sheets. Le bot voit la nouvelle version en 5 min.
          </p>
        </td>
      </tr>
      </table>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="36" valign="top" style="font-size:20px;line-height:1;">&#128274;</td>
        <td style="padding-left:10px;">
          <p style="font-size:13px;font-weight:bold;color:#0A1F3D;margin:0 0 3px;font-family:Arial,sans-serif;">
            Conformité RGPD &amp; clé API chiffrée
          </p>
          <p style="font-size:12px;color:#6B7B8E;margin:0;line-height:1.7;font-family:Arial,sans-serif;">
            Aucun stockage des conversations. Tier Gemini payant activable (zero training data).
          </p>
        </td>
      </tr>
      </table>

    </td></tr>
    </table>
  </td></tr>

  <!-- Tarifs : 3 formules (aligné sur le site) -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;overflow:hidden;">
    <tr><td style="background:#0A1F3D;background-image:linear-gradient(135deg,#0A1F3D 0%,#2196F3 100%);padding:7px;text-align:center;">
      <span style="font-size:10px;font-weight:bold;color:#fff;letter-spacing:0.8px;
                  text-transform:uppercase;font-family:Arial,sans-serif;">
        Trois formules &middot; Zéro abonnement
      </span>
    </td></tr>
    <tr><td style="padding:16px 10px 18px;">

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>

        <!-- EXPRESS 499 -->
        <td width="33%" valign="top" style="padding:0 3px;">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;">
          <tr><td style="padding:14px 9px;text-align:center;">
            <p style="font-size:9px;font-weight:bold;color:#6B7B8E;text-transform:uppercase;letter-spacing:0.8px;margin:0 0 3px;font-family:Arial,sans-serif;">Autonome</p>
            <p style="font-size:14px;font-weight:bold;color:#0A1F3D;margin:0 0 6px;font-family:Arial,sans-serif;">Express</p>
            <p style="margin:0;font-family:Georgia,serif;"><span style="font-size:26px;font-weight:bold;color:#0A1F3D;letter-spacing:-1px;">499</span><span style="font-size:16px;color:#2196F3;font-weight:bold;">€</span></p>
            <p style="font-size:9px;color:#9CA3AF;margin:2px 0 10px;font-family:Arial,sans-serif;">une fois</p>
            <p style="font-size:10.5px;color:#4A5A6E;margin:0 0 12px;line-height:1.7;text-align:left;font-family:Arial,sans-serif;">
              &#10003; FAQ Q/R illimitées<br>
              &#10003; Vos couleurs<br>
              &#10003; Snippet à coller vous-même<br>
              &#10003; Formation visio + support
            </p>
            <a href="{EXPRESS_URL}"
               style="display:inline-block;background:#F4F9FE;border:1px solid #2196F3;color:#1976D2;font-size:11px;font-weight:bold;padding:8px 14px;border-radius:999px;text-decoration:none;font-family:Arial,sans-serif;">
              Choisir
            </a>
          </td></tr>
          </table>
        </td>

        <!-- PREMIUM 799 (populaire) -->
        <td width="34%" valign="top" style="padding:0 3px;">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:2px solid #2196F3;border-radius:12px;overflow:hidden;">
          <tr><td style="background:#2196F3;padding:4px;text-align:center;">
            <span style="font-size:8px;font-weight:bold;color:#fff;letter-spacing:0.6px;text-transform:uppercase;font-family:Arial,sans-serif;">Le plus populaire</span>
          </td></tr>
          <tr><td style="padding:11px 9px 14px;text-align:center;">
            <p style="font-size:9px;font-weight:bold;color:#6B7B8E;text-transform:uppercase;letter-spacing:0.8px;margin:0 0 3px;font-family:Arial,sans-serif;">Clé en main</p>
            <p style="font-size:14px;font-weight:bold;color:#0A1F3D;margin:0 0 6px;font-family:Arial,sans-serif;">Premium</p>
            <p style="margin:0;font-family:Georgia,serif;"><span style="font-size:26px;font-weight:bold;color:#0A1F3D;letter-spacing:-1px;">799</span><span style="font-size:16px;color:#2196F3;font-weight:bold;">€</span></p>
            <p style="font-size:9px;color:#9CA3AF;margin:2px 0 10px;font-family:Arial,sans-serif;">une fois</p>
            <p style="font-size:10.5px;color:#4A5A6E;margin:0 0 12px;line-height:1.7;text-align:left;font-family:Arial,sans-serif;">
              &#10003; Tout Express<br>
              &#10003; <strong>Installation par nos soins</strong><br>
              &#10003; Tests mobile + navigateurs<br>
              &#10003; Support prolongé
            </p>
            <a href="{PREMIUM_URL}"
               style="display:inline-block;background:#0A1F3D;background-image:linear-gradient(135deg,#0A1F3D 0%,#2196F3 100%);color:#ffffff;font-size:11px;font-weight:bold;padding:8px 14px;border-radius:999px;text-decoration:none;font-family:Arial,sans-serif;">
              Choisir
            </a>
          </td></tr>
          </table>
        </td>

        <!-- CODE SOURCE 1499 -->
        <td width="33%" valign="top" style="padding:0 3px;">
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;">
          <tr><td style="padding:14px 9px;text-align:center;">
            <p style="font-size:9px;font-weight:bold;color:#6B7B8E;text-transform:uppercase;letter-spacing:0.8px;margin:0 0 3px;font-family:Arial,sans-serif;">Pro / Agence</p>
            <p style="font-size:14px;font-weight:bold;color:#0A1F3D;margin:0 0 6px;font-family:Arial,sans-serif;">Code Source</p>
            <p style="margin:0;font-family:Georgia,serif;"><span style="font-size:26px;font-weight:bold;color:#0A1F3D;letter-spacing:-1px;">1499</span><span style="font-size:16px;color:#2196F3;font-weight:bold;">€</span></p>
            <p style="font-size:9px;color:#9CA3AF;margin:2px 0 10px;font-family:Arial,sans-serif;">une fois</p>
            <p style="font-size:10.5px;color:#4A5A6E;margin:0 0 12px;line-height:1.7;text-align:left;font-family:Arial,sans-serif;">
              &#10003; Repo GitHub complet<br>
              &#10003; White-label<br>
              &#10003; Revente autorisée<br>
              &#10003; Modification libre
            </p>
            <a href="{CODESOURCE_URL}"
               style="display:inline-block;background:#F4F9FE;border:1px solid #2196F3;color:#1976D2;font-size:11px;font-weight:bold;padding:8px 14px;border-radius:999px;text-decoration:none;font-family:Arial,sans-serif;">
              Choisir
            </a>
          </td></tr>
          </table>
        </td>

      </tr>
      </table>

      <p style="margin:14px 0 0;font-size:10px;color:#9CA3AF;font-family:Arial,sans-serif;line-height:1.6;text-align:center;">
        Solutions concurrentes : de 30 à 200€/mois en abonnement, soit 360 à 2 400€ par an, chaque année.<br>
        <a href="{TARIF_URL}" style="color:#1976D2;text-decoration:underline;">Comparer les formules en détail →</a>
      </p>

    </td></tr>
    </table>
  </td></tr>

  <!-- Comment ça marche -->
  <tr><td style="padding-top:14px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FFFFFF;border:1px solid #E2E8F0;border-radius:16px;">
    <tr><td style="padding:22px 22px 18px;">

      <p style="font-size:11px;font-weight:bold;text-transform:uppercase;
                letter-spacing:1.2px;color:#6B7B8E;margin:0 0 16px;font-family:Arial,sans-serif;">
        Comment ça marche &mdash; 4 étapes simples
      </p>

      <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="32" valign="top" style="padding-bottom:10px;">
          <div style="width:24px;height:24px;background:#2196F3;border-radius:50%;color:#fff;font-size:12px;font-weight:bold;line-height:24px;text-align:center;font-family:Arial,sans-serif;">1</div>
        </td>
        <td style="padding-left:8px;padding-bottom:10px;">
          <p style="font-size:13px;color:#0A1F3D;margin:0;font-family:Arial,sans-serif;line-height:1.5;">
            <strong>Étape 1 :</strong> Vous nous envoyez votre FAQ (Excel, mail, oral, peu importe).
          </p>
        </td>
      </tr>
      <tr>
        <td width="32" valign="top" style="padding-bottom:10px;">
          <div style="width:24px;height:24px;background:#2196F3;border-radius:50%;color:#fff;font-size:12px;font-weight:bold;line-height:24px;text-align:center;font-family:Arial,sans-serif;">2</div>
        </td>
        <td style="padding-left:8px;padding-bottom:10px;">
          <p style="font-size:13px;color:#0A1F3D;margin:0;font-family:Arial,sans-serif;line-height:1.5;">
            <strong>Étape 2 :</strong> On structure la FAQ et on configure l'IA Gemini sur votre Google Sheet.
          </p>
        </td>
      </tr>
      <tr>
        <td width="32" valign="top" style="padding-bottom:10px;">
          <div style="width:24px;height:24px;background:#2196F3;border-radius:50%;color:#fff;font-size:12px;font-weight:bold;line-height:24px;text-align:center;font-family:Arial,sans-serif;">3</div>
        </td>
        <td style="padding-left:8px;padding-bottom:10px;">
          <p style="font-size:13px;color:#0A1F3D;margin:0;font-family:Arial,sans-serif;line-height:1.5;">
            <strong>Étape 3 :</strong> Démo vidéo du chatbot, vous validez. Ajustements à la marge si besoin.
          </p>
        </td>
      </tr>
      <tr>
        <td width="32" valign="top">
          <div style="width:24px;height:24px;background:#16A34A;border-radius:50%;color:#fff;font-size:12px;font-weight:bold;line-height:24px;text-align:center;font-family:Arial,sans-serif;">4</div>
        </td>
        <td style="padding-left:8px;">
          <p style="font-size:13px;color:#0A1F3D;margin:0;font-family:Arial,sans-serif;line-height:1.5;">
            <strong>Étape 4 :</strong> Installation sur votre site + formation en visio. C'est en ligne.
          </p>
        </td>
      </tr>
      </table>

    </td></tr>
    </table>
  </td></tr>

  <!-- CTA final : mailto pré-remplis (modèle de demande, comme ThermoData) -->
  <tr><td style="padding-top:18px;text-align:center;">
    <p style="font-size:14px;color:#4A5A6E;margin:0 0 14px;font-family:Arial,sans-serif;line-height:1.6;">
      Intéressé ? Cliquez ci-dessous : <strong style="color:#0A1F3D;">le mail est déjà pré-rempli</strong>,<br>vous n'avez qu'à compléter et envoyer.
    </p>
    <a href="{_mailto('Je teste SupportAI', MAILTO_TEST_BODY)}"
       style="display:inline-block;background:#16A34A;color:#ffffff;
              font-size:14px;font-weight:bold;padding:13px 30px;border-radius:999px;
              text-decoration:none;font-family:Arial,sans-serif;">
      ✓ OK je teste SupportAI →
    </a>
    <p style="margin:14px 0 0;font-size:12px;line-height:1.6;font-family:Arial,sans-serif;">
      Une simple question d'abord ?
      <a href="{_mailto('Question SupportAI', MAILTO_QUESTION_BODY)}"
         style="color:#1976D2;text-decoration:underline;font-weight:bold;">
        Écrivez-nous, réponse sous 24h
      </a>
    </p>
  </td></tr>

  <!-- Mentions légales -->
  <tr><td style="padding-top:24px;">
    <table width="100%" cellpadding="0" cellspacing="0"
           style="background:#FAFBFC;border:1px solid #E2E8F0;border-radius:12px;">
    <tr><td style="padding:18px 22px;">

      <p style="font-size:11px;color:#6B7B8E;margin:0 0 6px;font-family:Arial,sans-serif;line-height:1.6;text-align:center;">
        <strong style="color:#0A1F3D;">SupportAI</strong> &middot; <a href="{SIRET_URL}" style="color:#6B7B8E;text-decoration:underline;">{COMPANY_ID_LABEL} {_format_company_id(COMPANY_ID_VALUE)}</a> &middot; {ADRESSE_SOCIETE}
      </p>
      <p style="font-size:11px;color:#6B7B8E;margin:0 0 6px;font-family:Arial,sans-serif;line-height:1.6;text-align:center;">
        Site : <a href="{SITE_URL}" style="color:#1976D2;text-decoration:none;">supportai.fr</a> &middot;
        Contact : <a href="mailto:{CONTACT_EMAIL}" style="color:#1976D2;text-decoration:none;">{CONTACT_EMAIL}</a>
      </p>
      <p style="font-size:10px;color:#9CA3AF;margin:0;font-family:Arial,sans-serif;line-height:1.6;text-align:center;">
        IA : Google Gemini &middot; Conforme RGPD &middot; Prospection B2B &middot; Intérêt légitime art. 6.1.f RGPD
      </p>

    </td></tr>
    </table>
  </td></tr>

  <!-- Unsubscribe -->
  <tr><td style="padding-top:14px;text-align:center;">
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ════════════════════════════════════════════════════════════════════
#  ⑥  CONSTRUCTION DU MESSAGE MIME
# ════════════════════════════════════════════════════════════════════

def build_message(contact: dict, recipient: str, subject: str) -> MIMEMultipart:
    unsubscribe_url = f"{BASE_UNSUBSCRIBE_URL}?email={recipient}"
    logo_path = _find_logo()
    loom_thumb_data = _download_loom_gif(VIDEO_URL) if VIDEO_URL else None

    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Reply-To"] = REPLY_TO
    msg["Message-ID"] = make_msgid(domain="supportai.fr")
    msg.add_header("List-Unsubscribe", f"<{unsubscribe_url}>")
    msg.add_header("List-Unsubscribe-Post", "List-Unsubscribe=One-Click")
    msg["X-SupportAI-Pipeline"] = "mass" if SEND_MODE == "MASS" else "daily-test"

    related = MIMEMultipart("related")
    alternative = MIMEMultipart("alternative")

    plain_body = _build_plain_text(contact, unsubscribe_url)
    html_body = _build_html(
        contact, unsubscribe_url,
        has_logo=bool(logo_path),
        has_thumb=bool(loom_thumb_data),
    )

    alternative.attach(MIMEText(plain_body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    related.attach(alternative)

    # Miniature vidéo Loom inline CID (comme ThermoData)
    if loom_thumb_data:
        thumb_img = MIMEImage(loom_thumb_data, _subtype="gif")
        thumb_img.add_header("Content-ID", "<loom_thumb>")
        thumb_img.add_header("Content-Disposition", "inline", filename="loom_thumb.gif")
        related.attach(thumb_img)

    # Logo inline CID (comme ThermoData)
    if logo_path:
        with open(logo_path, "rb") as f:
            img = MIMEImage(f.read())
        img.add_header("Content-ID", "<supportai_logo>")
        img.add_header("Content-Disposition", "inline", filename=logo_path.name)
        related.attach(img)

    msg.attach(related)
    return msg


# ════════════════════════════════════════════════════════════════════
#  ⑦  ENVOI SMTP
# ════════════════════════════════════════════════════════════════════

def smtp_send(msg: MIMEMultipart, recipient: str) -> None:
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(SMTP_LOGIN, _password())
        server.sendmail(FROM_EMAIL, [recipient], msg.as_string())


# ════════════════════════════════════════════════════════════════════
#  ⑧  MASTER CSV - LECTURE / ÉCRITURE / TRACKING (mode MASS)
# ════════════════════════════════════════════════════════════════════

def load_master_csv() -> tuple[list[str], list[dict]]:
    if not MASTER_PATH.exists():
        print(f"❌ Master CSV introuvable : {MASTER_PATH}")
        print("   → Lance d'abord : python emailing/build_master.py")
        sys.exit(1)
    with MASTER_PATH.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    print(f"📂 Master chargé : {len(rows)} contacts")
    return fieldnames, rows


def save_master_csv(fieldnames: list[str], rows: list[dict]) -> None:
    with MASTER_PATH.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _safe(v) -> str:
    return "" if v is None else str(v).strip()


def _is_valid_email(email: str) -> bool:
    email = _safe(email)
    return bool(email and "@" in email and "." in email.split("@")[-1])


def pick_pending_contacts(rows: list[dict], limit: int) -> list[dict]:
    """N prochains pending, triés par rank source décroissant (e-commerce d'abord)."""
    pending = [
        r for r in rows
        if _safe(r.get("send_status")).lower() == "pending"
        and _safe(r.get("email_sent")).lower() not in {"true", "1", "yes"}
    ]

    def _key(r):
        try:
            rank = int(r.get("score_source_rank") or 0)
        except (ValueError, TypeError):
            rank = 0
        return (-rank, 0 if _safe(r.get("company")) else 1)

    pending.sort(key=_key)
    return pending[:limit]


def mark_contact_sent(row: dict, subject: str, status: str = "sent", error: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    if status == "sent":
        row["email_sent"] = "true"
    row["sent_at"] = now
    row["send_status"] = status
    row["send_attempts"] = str(int(_safe(row.get("send_attempts")) or "0") + 1)
    row["last_error"] = error[:200]
    row["last_subject"] = subject
    row["updated_at"] = now


# ════════════════════════════════════════════════════════════════════
#  ⑨  MODE TEST - pitch complet à TEST_RECIPIENT
# ════════════════════════════════════════════════════════════════════

def run_test(dry_run: bool) -> int:
    print(f"  Mode    : TEST → {TEST_RECIPIENT}\n{'='*70}\n")

    subject = _format_subject(FAKE_CONTACT)
    print(f"📧 Objet : {subject}")

    msg = build_message(FAKE_CONTACT, TEST_RECIPIENT, subject)
    size_kb = len(msg.as_string().encode("utf-8")) / 1024
    print(f"📦 Taille mail : {size_kb:.1f} Ko")

    if dry_run:
        print("🟡 DRY_RUN actif → mail NON envoyé.")
        return 0

    try:
        print("📤 Envoi en cours...")
        smtp_send(msg, TEST_RECIPIENT)
        print("✅ Email envoyé avec succès.")
        return 0
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ Échec auth SMTP : {e}")
        print("   → Vérifie SMTP_PASSWORD dans GitHub Secrets.")
        return 1
    except smtplib.SMTPException as e:
        print(f"❌ Erreur SMTP : {type(e).__name__}: {e}")
        return 1
    except Exception as e:
        print(f"❌ Erreur inattendue : {type(e).__name__}: {e}")
        return 1


# ════════════════════════════════════════════════════════════════════
#  ⑩  MODE MASS - N prochains pending du master CSV
# ════════════════════════════════════════════════════════════════════

def run_mass(dry_run: bool) -> int:
    est_min = DAILY_LIMIT * (PAUSE_MIN + PAUSE_MAX) / 2 / 60
    print(f"  Mode    : MASS - limite {DAILY_LIMIT} emails")
    print(f"  Pauses  : {PAUSE_MIN}-{PAUSE_MAX}s (durée estimée ~{est_min:.0f} min)")
    print(f"  Master  : {MASTER_PATH}")
    print(f"{'='*70}\n")

    fieldnames, all_rows = load_master_csv()
    contacts = pick_pending_contacts(all_rows, DAILY_LIMIT)

    total_pending = sum(
        1 for r in all_rows
        if _safe(r.get("send_status")).lower() == "pending"
        and _safe(r.get("email_sent")).lower() not in {"true", "1", "yes"}
    )
    if not contacts:
        print("ℹ️  Aucun contact pending. Relance les scrapers (weekly.yml) pour réalimenter.")
        return 0

    print(f"📋 {len(contacts)} sélectionnés / {total_pending} pending / {len(all_rows)} total")

    sent_count = error_count = 0

    for i, contact in enumerate(contacts, 1):
        email = _safe(contact.get("email"))
        subject = _format_subject(contact)
        print(f"\n── [{i}/{len(contacts)}] {email or '-'}")
        print(f"   Vertical   : {_safe(contact.get('vertical')) or '-'}")
        print(f"   Entreprise : {_safe(contact.get('company')) or '-'}")
        print(f"   Objet      : {subject}")

        if dry_run:
            print("   🟡 DRY_RUN → non envoyé, tracking non modifié")
            continue

        if not _is_valid_email(email):
            mark_contact_sent(contact, subject, status="error", error="invalid email")
            save_master_csv(fieldnames, all_rows)
            error_count += 1
            print("   ❌ Email invalide")
            continue

        try:
            msg = build_message(contact, email, subject)
            smtp_send(msg, email)
            mark_contact_sent(contact, subject, status="sent")
            save_master_csv(fieldnames, all_rows)
            sent_count += 1
            print("   ✅ Envoyé")
        except smtplib.SMTPAuthenticationError as e:
            # Auth KO = tous les envois suivants échoueraient aussi → on STOPPE
            # sans marquer le contact en error (il reste pending, retenté demain).
            print(f"   ❌ Auth SMTP KO : {e}")
            print("   🛑 Arrêt immédiat - les contacts restants restent 'pending'.")
            save_master_csv(fieldnames, all_rows)
            return 1
        except Exception as exc:
            mark_contact_sent(contact, subject, status="error", error=str(exc))
            save_master_csv(fieldnames, all_rows)
            error_count += 1
            print(f"   ❌ Erreur : {exc}")

        if i < len(contacts):
            pause = random.randint(PAUSE_MIN, PAUSE_MAX)
            print(f"   ⏳ Pause {pause}s...")
            time.sleep(pause)

    print(f"\n{'='*70}")
    print(f"  RÉSULTAT : {sent_count} envoyés / {error_count} erreurs / {len(contacts)} tentés")
    print(f"  Pending restant : ~{total_pending - sent_count - error_count}")
    print(f"{'='*70}")
    return 0


# ════════════════════════════════════════════════════════════════════
#  ⑪  MAIN
# ════════════════════════════════════════════════════════════════════

def main() -> int:
    print(f"\n{'='*70}")
    print(f"  SupportAI - Email v2 (TEST + MASS)")
    print(f"  {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*70}")
    print(f"  SMTP    : {SMTP_SERVER}:{SMTP_PORT}")
    print(f"  From    : {FROM_NAME} <{FROM_EMAIL}>")

    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}

    if SEND_MODE == "MASS":
        return run_mass(dry_run)
    return run_test(dry_run)


if __name__ == "__main__":
    sys.exit(main())
