#!/usr/bin/env python3
"""
Business Listing Scraper & Email Alert System
Monitors multiple business-for-sale websites and emails matching listings.
"""

import os
import json
import time
import hashlib
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# CONFIGURATION — edit these or use env vars
# ─────────────────────────────────────────────

CONFIG = {
    # Email settings (set as GitHub Secrets)
    "email_sender":   os.environ.get("EMAIL_SENDER", "your_gmail@gmail.com"),
    "email_password": os.environ.get("EMAIL_PASSWORD", ""),   # Gmail App Password
    "email_recipient": os.environ.get("EMAIL_RECIPIENT", "your_email@gmail.com"),

    # ScraperAPI key (set as GitHub Secret: SCRAPER_API_KEY)
    "scraper_api_key": os.environ.get("SCRAPER_API_KEY", ""),

    # Search criteria
    "min_ebitda": 650_000,
    "max_ebitda": 1_500_000,
    "locations": ["san diego", "orange county", "phoenix", "scottsdale", "san diego county"],
    "excluded_industries": [
        "restaurant", "restaurants", "food", "franchise", "franchised",
        "gym", "fitness", "mail route", "postal route", "delivery route"
    ],
    "preferred_keywords": [
        "recurring revenue", "general manager", "seller financing",
        "seller note", "absentee", "established", "b2b", "service"
    ],

    # File to track already-seen listings (persisted via GitHub Actions cache or artifact)
    "seen_listings_file": "seen_listings.json",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# SEEN-LISTINGS TRACKER
# ─────────────────────────────────────────────

def load_seen(path: str) -> set:
    try:
        with open(path) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_seen(path: str, seen: set):
    with open(path, "w") as f:
        json.dump(list(seen), f)

def listing_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_soup(url: str, retries: int = 3) -> BeautifulSoup | None:
    """Fetch a URL, routing through ScraperAPI to bypass 403 blocks."""
    api_key = CONFIG.get("scraper_api_key", "")
    if api_key:
        import urllib.parse
        encoded = urllib.parse.quote(url, safe="")
        proxy_url = f"http://api.scraperapi.com?api_key={api_key}&url={encoded}&render=false"
    else:
        proxy_url = url  # fallback to direct (will likely 403)

    for attempt in range(retries):
        try:
            resp = requests.get(proxy_url, headers=HEADERS, timeout=60)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            time.sleep(3 * (attempt + 1))
    return None

def parse_price(text: str) -> int | None:
    """Extract integer dollar value from a string like '$1,200,000' or '1.2M'."""
    import re
    text = text.replace(",", "").replace("$", "").strip().lower()
    m = re.search(r"([\d.]+)\s*m", text)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    m = re.search(r"([\d.]+)\s*k", text)
    if m:
        return int(float(m.group(1)) * 1_000)
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    return None

def normalize_location(text: str) -> str:
    """Normalize location text for fuzzy matching."""
    import re
    text = text.lower().strip()
    # Expand common abbreviations
    text = re.sub(r'\bsd\b', 'san diego', text)
    text = re.sub(r'\boc\b', 'orange county', text)
    text = re.sub(r'\bphx\b', 'phoenix', text)
    text = re.sub(r'\baz\b', 'arizona', text)
    text = re.sub(r'\bca\b', 'california', text)
    # Remove punctuation noise
    text = re.sub(r'[,.]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# All location variants we accept — cast a wide net
LOCATION_VARIANTS = [
    # San Diego
    "san diego", "sandiego", "san diego county", "san diego ca",
    "san diego california", "chula vista", "el cajon", "escondido",
    "carlsbad", "oceanside", "vista", "santee", "la mesa", "el cajon",
    "national city", "poway", "encinitas", "solana beach", "del mar",
    # Orange County
    "orange county", "orange co", "irvine", "anaheim", "santa ana",
    "huntington beach", "fullerton", "garden grove", "orange ca",
    "costa mesa", "mission viejo", "newport beach", "tustin", "yorba linda",
    "laguna niguel", "lake forest", "aliso viejo", "rancho santa margarita",
    # Phoenix / Scottsdale
    "phoenix", "scottsdale", "tempe", "chandler", "gilbert", "glendale",
    "peoria", "surprise", "mesa", "maricopa county", "east valley",
    "paradise valley", "fountain hills", "cave creek", "carefree",
    "phoenix az", "scottsdale az", "phoenix arizona", "scottsdale arizona",
]

def matches_criteria(listing: dict) -> tuple[bool, list[str]]:
    """
    Returns (True, reasons) if listing matches, (False, []) otherwise.
    'listing' keys: title, url, location, cash_flow, description
    """
    reasons = []
    title_desc = (listing.get("title", "") + " " + listing.get("description", "")).lower()
    raw_location = listing.get("location", "")
    location = normalize_location(raw_location)

    # ── Location check — match against expanded variants list ──
    location_matched = any(variant in location for variant in LOCATION_VARIANTS)
    # Also check if location text appears in title/description (some sites embed it there)
    if not location_matched:
        location_matched = any(variant in title_desc for variant in LOCATION_VARIANTS)
    if not location_matched:
        return False, []

    # ── Exclude industries ──
    if any(kw in title_desc for kw in CONFIG["excluded_industries"]):
        return False, []

    # ── Cash flow / EBITDA / SDE range ──
    # If cash flow IS listed and is OUT of range → exclude
    # If cash flow IS listed and IN range → include with confirmation
    # If cash flow is NOT listed → include with a manual verify flag
    cf = listing.get("cash_flow")
    if cf:
        if not (CONFIG["min_ebitda"] <= cf <= CONFIG["max_ebitda"]):
            return False, []
        reasons.append(f"✅ Cash flow ${cf:,} is within target range ($650K–$1.5M)")
    else:
        reasons.append("⚠️ Cash flow not listed — verify manually before pursuing")

    # ── Preferred keyword bonuses ──
    found_keywords = [kw for kw in CONFIG["preferred_keywords"] if kw in title_desc]
    if found_keywords:
        reasons.append(f"🌟 Keywords matched: {', '.join(found_keywords)}")

    reasons.insert(0, f"📍 Location matched: {raw_location or 'N/A'}")
    return True, reasons


# ─────────────────────────────────────────────
# SCRAPERS — one per site
# ─────────────────────────────────────────────

def scrape_bizbuysell() -> list[dict]:
    listings = []
    location_queries = [
        ("san-diego-county-california", "San Diego County, CA"),
        ("orange-county-california",    "Orange County, CA"),
        ("phoenix-arizona",             "Phoenix, AZ"),
        ("scottsdale-arizona",          "Scottsdale, AZ"),
    ]
    for slug, label in location_queries:
        url = (
            f"https://www.bizbuysell.com/businesses-for-sale/{slug}/"
            f"?q=ebitda_gte=650000&ebitda_lte=1500000"
        )
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.result-list-item, div.listing-card, article.listing"):
            try:
                title_el = card.select_one("h2 a, h3 a, .listing-title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = f"https://www.bizbuysell.com{href}" if href.startswith("/") else href
                cf_el = card.select_one(".cash-flow, .cashflow, [data-label='Cash Flow']")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, .listing-description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "BizBuySell",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"BizBuySell parse error: {e}")
        time.sleep(2)
    return listings


def scrape_bizquest() -> list[dict]:
    listings = []
    searches = [
        ("CA/San-Diego", "San Diego County, CA"),
        ("CA/Orange-County", "Orange County, CA"),
        ("AZ/Phoenix", "Phoenix, AZ"),
        ("AZ/Scottsdale", "Scottsdale, AZ"),
    ]
    for path, label in searches:
        url = f"https://www.bizquest.com/businesses-for-sale/{path}/"
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.listing-result, div.biz-listing, .search-result-item"):
            try:
                title_el = card.select_one("h2 a, h3 a, .biz-name a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = f"https://www.bizquest.com{href}" if href.startswith("/") else href
                cf_el = card.select_one(".cash-flow, .cashflow")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "BizQuest",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"BizQuest parse error: {e}")
        time.sleep(2)
    return listings


def scrape_businessbroker() -> list[dict]:
    listings = []
    urls = [
        ("https://www.businessbroker.net/businesses/for-sale/california/san-diego.aspx", "San Diego County, CA"),
        ("https://www.businessbroker.net/businesses/for-sale/california/orange-county.aspx", "Orange County, CA"),
        ("https://www.businessbroker.net/businesses/for-sale/arizona/phoenix.aspx", "Phoenix, AZ"),
        ("https://www.businessbroker.net/businesses/for-sale/arizona/scottsdale.aspx", "Scottsdale, AZ"),
    ]
    for url, label in urls:
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.listing, div.business-listing, .result-item"):
            try:
                title_el = card.select_one("h2 a, h3 a, .title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = href if href.startswith("http") else f"https://www.businessbroker.net{href}"
                cf_el = card.select_one(".cashflow, .cash-flow, .sde")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "BusinessBroker.net",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"BusinessBroker parse error: {e}")
        time.sleep(2)
    return listings


def scrape_dealstream() -> list[dict]:
    listings = []
    url = "https://dealstream.com/s/business/usa?state=CA,AZ&cashflow_min=650000&cashflow_max=1500000"
    soup = get_soup(url)
    if not soup:
        return listings
    for card in soup.select("div.listing-card, div.deal-item, .search-item"):
        try:
            title_el = card.select_one("h2 a, h3 a, .title a, a.deal-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            full_url = href if href.startswith("http") else f"https://dealstream.com{href}"
            location_el = card.select_one(".location, .city, .state")
            location = location_el.get_text(strip=True) if location_el else ""
            cf_el = card.select_one(".cashflow, .cash-flow, .ebitda")
            cf_text = cf_el.get_text(strip=True) if cf_el else ""
            desc_el = card.select_one(".description, p")
            desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
            listings.append({
                "source": "DealStream",
                "title": title,
                "url": full_url,
                "location": location,
                "cash_flow": parse_price(cf_text) if cf_text else None,
                "description": desc,
            })
        except Exception as e:
            log.debug(f"DealStream parse error: {e}")
    return listings


def scrape_bizpen() -> list[dict]:
    """Bizpen.com scraper"""
    listings = []
    searches = [
        ("https://www.bizpen.com/listings?location=san+diego&cf_min=650000", "San Diego County, CA"),
        ("https://www.bizpen.com/listings?location=orange+county&cf_min=650000", "Orange County, CA"),
        ("https://www.bizpen.com/listings?location=phoenix&cf_min=650000", "Phoenix, AZ"),
    ]
    for url, label in searches:
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.listing, .business-card, .result"):
            try:
                title_el = card.select_one("h2 a, h3 a, a.title")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = href if href.startswith("http") else f"https://www.bizpen.com{href}"
                cf_el = card.select_one(".cashflow, .sde, .cash-flow")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "Bizpen",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"Bizpen parse error: {e}")
        time.sleep(2)
    return listings


def scrape_businessesforsale() -> list[dict]:
    listings = []
    urls = [
        ("https://www.businessesforsale.com/us/search/businesses-for-sale/california/san-diego", "San Diego County, CA"),
        ("https://www.businessesforsale.com/us/search/businesses-for-sale/california/orange-county", "Orange County, CA"),
        ("https://www.businessesforsale.com/us/search/businesses-for-sale/arizona/phoenix", "Phoenix, AZ"),
    ]
    for url, label in urls:
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.listing, article.listing, .search-result"):
            try:
                title_el = card.select_one("h2 a, h3 a, .listing-title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = href if href.startswith("http") else f"https://www.businessesforsale.com{href}"
                cf_el = card.select_one(".cashflow, .cash-flow, .profit")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "BusinessesForSale.com",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"BusinessesForSale parse error: {e}")
        time.sleep(2)
    return listings


def scrape_flippa() -> list[dict]:
    """Flippa — primarily online businesses but sometimes has physical ones"""
    listings = []
    url = "https://flippa.com/search?filter[listing_type][]=business&filter[minimum_revenue]=650000"
    soup = get_soup(url)
    if not soup:
        return listings
    for card in soup.select("div.listing-card, div.ListingCard, article"):
        try:
            title_el = card.select_one("h2 a, h3 a, .ListingCard__title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            full_url = href if href.startswith("http") else f"https://flippa.com{href}"
            location_el = card.select_one(".location, .country")
            location = location_el.get_text(strip=True) if location_el else "Online"
            cf_el = card.select_one(".profit, .net-profit, .cashflow")
            cf_text = cf_el.get_text(strip=True) if cf_el else ""
            desc_el = card.select_one(".description, p")
            desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
            listings.append({
                "source": "Flippa",
                "title": title,
                "url": full_url,
                "location": location,
                "cash_flow": parse_price(cf_text) if cf_text else None,
                "description": desc,
            })
        except Exception as e:
            log.debug(f"Flippa parse error: {e}")
    return listings


def scrape_loopnet() -> list[dict]:
    """LoopNet — primarily real estate but includes business sales"""
    listings = []
    urls = [
        ("https://www.loopnet.com/biz/california/san-diego-county/businesses-for-sale/", "San Diego County, CA"),
        ("https://www.loopnet.com/biz/california/orange-county/businesses-for-sale/", "Orange County, CA"),
        ("https://www.loopnet.com/biz/arizona/phoenix/businesses-for-sale/", "Phoenix, AZ"),
    ]
    for url, label in urls:
        soup = get_soup(url)
        if not soup:
            continue
        for card in soup.select("div.placard, div.listing-placard, article.placard"):
            try:
                title_el = card.select_one("h2 a, h3 a, .placard-title a")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                href = title_el.get("href", "")
                full_url = href if href.startswith("http") else f"https://www.loopnet.com{href}"
                cf_el = card.select_one(".cash-flow, .cashflow, .noi")
                cf_text = cf_el.get_text(strip=True) if cf_el else ""
                desc_el = card.select_one(".description, p")
                desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
                listings.append({
                    "source": "LoopNet",
                    "title": title,
                    "url": full_url,
                    "location": label,
                    "cash_flow": parse_price(cf_text) if cf_text else None,
                    "description": desc,
                })
            except Exception as e:
                log.debug(f"LoopNet parse error: {e}")
        time.sleep(2)
    return listings


def scrape_smergers() -> list[dict]:
    listings = []
    url = "https://smergers.com/businesses-for-sale/united-states/california/?cashflow_min=650000"
    soup = get_soup(url)
    if not soup:
        return listings
    for card in soup.select("div.listing, .business-card, .deal-card"):
        try:
            title_el = card.select_one("h2 a, h3 a, .title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            full_url = href if href.startswith("http") else f"https://smergers.com{href}"
            location_el = card.select_one(".location, .city")
            location = location_el.get_text(strip=True) if location_el else ""
            cf_el = card.select_one(".ebitda, .cashflow, .profit")
            cf_text = cf_el.get_text(strip=True) if cf_el else ""
            desc_el = card.select_one(".description, p")
            desc = desc_el.get_text(strip=True)[:300] if desc_el else ""
            listings.append({
                "source": "Smergers",
                "title": title,
                "url": full_url,
                "location": location,
                "cash_flow": parse_price(cf_text) if cf_text else None,
                "description": desc,
            })
        except Exception as e:
            log.debug(f"Smergers parse error: {e}")
    return listings


# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────

def build_email_html(matches: list[dict]) -> str:
    verified   = [m for m in matches if m.get("cash_flow")]
    unverified = [m for m in matches if not m.get("cash_flow")]

    def render_card(m):
        reasons_html = "".join(f"<li>{r}</li>" for r in m.get("reasons", []))
        cf_display = f"${m['cash_flow']:,}" if m.get("cash_flow") else "⚠️ Not listed"
        border_color = "#27ae60" if m.get("cash_flow") else "#f39c12"
        return f"""
        <div style="border-left:4px solid {border_color};border:1px solid #ddd;border-radius:8px;
                    padding:16px;margin-bottom:16px;font-family:Arial,sans-serif;">
          <h3 style="margin:0 0 4px 0;">
            <a href="{m['url']}" style="color:#1a73e8;text-decoration:none;">{m['title']}</a>
          </h3>
          <p style="margin:2px 0;color:#555;font-size:13px;">
            📍 {m.get('location','N/A')} &nbsp;|&nbsp;
            💰 SDE/EBITDA: <strong>{cf_display}</strong> &nbsp;|&nbsp;
            🔗 {m['source']}
          </p>
          <p style="margin:8px 0;font-size:13px;color:#333;">{m.get('description','')}</p>
          <ul style="font-size:12px;color:#555;margin:6px 0 0 0;padding-left:18px;">{reasons_html}</ul>
        </div>
        """

    verified_section = ""
    if verified:
        cards = "".join(render_card(m) for m in verified)
        verified_section = f"""
        <h3 style="color:#27ae60;">✅ Cash Flow Confirmed ({len(verified)})</h3>
        {cards}
        """

    unverified_section = ""
    if unverified:
        cards = "".join(render_card(m) for m in unverified)
        unverified_section = f"""
        <h3 style="color:#f39c12;">⚠️ Cash Flow Not Listed — Verify Manually ({len(unverified)})</h3>
        <p style="font-size:12px;color:#888;margin-top:-8px;">
          These matched on location and passed industry filters, but cash flow wasn't shown in the listing preview.
          Click through to confirm before pursuing.
        </p>
        {cards}
        """

    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px;">
      <h2 style="color:#1a73e8;">🏢 New Business Listings — {datetime.now().strftime('%b %d, %Y')}</h2>
      <p>Found <strong>{len(matches)}</strong> new listing(s) matching your criteria
         ({len(verified)} with confirmed cash flow, {len(unverified)} to verify).</p>
      <hr>
      {verified_section}
      {unverified_section}
      <hr>
      <p style="font-size:11px;color:#999;">
        Criteria: EBITDA/SDE $650K–$1.5M | San Diego County, Orange County, Phoenix/Scottsdale
        | Excluding: restaurants, gyms, mail routes, franchises
      </p>
    </body></html>
    """

def send_email(matches: list[dict]):
    cfg = CONFIG
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏢 {len(matches)} New Business Listing Match(es) — {datetime.now().strftime('%b %d')}"
    msg["From"] = cfg["email_sender"]
    msg["To"] = cfg["email_recipient"]
    html = build_email_html(matches)
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(cfg["email_sender"], cfg["email_password"])
        server.sendmail(cfg["email_sender"], cfg["email_recipient"], msg.as_string())
    log.info(f"Email sent with {len(matches)} matches.")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    log.info("Starting business listing scraper...")
    seen = load_seen(CONFIG["seen_listings_file"])

    all_scrapers = [
        scrape_bizbuysell,
        scrape_bizquest,
        scrape_businessbroker,
        scrape_dealstream,
        scrape_bizpen,
        scrape_businessesforsale,
        scrape_flippa,
        scrape_loopnet,
        scrape_smergers,
    ]

    all_listings = []
    for scraper in all_scrapers:
        name = scraper.__name__.replace("scrape_", "")
        log.info(f"Scraping {name}...")
        try:
            results = scraper()
            log.info(f"  → {len(results)} listings found")
            all_listings.extend(results)
        except Exception as e:
            log.error(f"  → Scraper {name} failed: {e}")
        time.sleep(1)

    log.info(f"Total raw listings: {len(all_listings)}")

    new_matches = []
    for listing in all_listings:
        lid = listing_id(listing["url"])
        if lid in seen:
            continue
        matched, reasons = matches_criteria(listing)
        if matched:
            listing["reasons"] = reasons
            new_matches.append(listing)
            seen.add(lid)

    log.info(f"New matches: {len(new_matches)}")
    save_seen(CONFIG["seen_listings_file"], seen)

    if new_matches:
        send_email(new_matches)
    else:
        log.info("No new matches — no email sent.")

if __name__ == "__main__":
    main()
