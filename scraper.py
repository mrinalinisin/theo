"""Product scraping — extracts name, price, images, variants from product URLs.

Supports two fetching backends:
  1. requests (fast, lightweight) — used by default
  2. Playwright headless Chromium (handles JS-rendered pages, bypasses 403s)

When use_browser=False, requests is tried first. If it gets a 403 or connection
error, the scraper auto-retries with Playwright. When use_browser=True, Playwright
is used directly.
"""

import json
import re
from urllib.parse import urlparse, urlunparse

import requests as requests_lib
from bs4 import BeautifulSoup


def sanitize_url(url):
    """Strip whitespace and drop query/fragment from a URL.

    Many product URLs get copied with tracking params (utm_*, gclid, etc.)
    or stray whitespace. We keep only scheme + netloc + path so the stored
    URL is canonical and deduplicable.
    """
    if not url:
        return url
    # Remove all whitespace anywhere in the string (including accidental newlines)
    url = "".join(url.split())
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.scheme or not parsed.netloc:
        return url
    # params (the rarely-used path-level ";..." bit) is preserved; query + fragment dropped
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, "", ""))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── HTML fetching backends ────────────────────────────────────────────────────


def _fetch_html_requests(url):
    """Fetch page HTML using the requests library (fast, no JS)."""
    resp = requests_lib.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _fetch_html_playwright(url):
    """Fetch page HTML using Playwright headless Chromium (JS-rendered)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            java_script_enabled=True,
        )
        page = context.new_page()
        try:
            # Use domcontentloaded — faster and more resilient than networkidle
            # which can timeout on sites with long-polling or analytics scripts
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Give JS a moment to render product data
            page.wait_for_timeout(3000)
        except Exception:
            # Even if navigation has issues, try to grab whatever loaded
            pass
        html = page.content()
        context.close()
        browser.close()
        return html


def _fetch_html(url, use_browser=False):
    """
    Fetch page HTML with automatic fallback.

    If use_browser is True, goes straight to Playwright.
    If False, tries requests first, and falls back to Playwright on
    403/ConnectionError/Timeout.
    """
    if use_browser:
        return _fetch_html_playwright(url)

    try:
        return _fetch_html_requests(url)
    except (
        requests_lib.exceptions.HTTPError,
        requests_lib.exceptions.ConnectionError,
        requests_lib.exceptions.Timeout,
    ) as e:
        # Auto-fallback to Playwright for 403s and connection issues
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 403 or not status:
            print(f"[Scraper] requests failed ({e}), retrying with Playwright...")
            return _fetch_html_playwright(url)
        raise


# ── Public API ────────────────────────────────────────────────────────────────


def scrape_product(url, use_browser=False):
    """
    Scrape a product page and return extracted details.

    Args:
        url: Product page URL
        use_browser: If True, use Playwright directly. If False, try requests
                     first with auto-fallback to Playwright on failure.

    Returns dict with keys:
        url, name, store, price, original_price, image_url, images, variants
    """
    url = sanitize_url(url)
    html = _fetch_html(url, use_browser=use_browser)
    soup = BeautifulSoup(html, "lxml")

    # Detect Cloudflare / bot-protection block pages
    title_text = (soup.find("title").get_text(strip=True) if soup.find("title") else "").lower()
    blocked_phrases = ["access denied", "access to this page has been denied",
                       "just a moment", "attention required", "please verify"]
    is_blocked = any(phrase in title_text for phrase in blocked_phrases)
    if is_blocked and not use_browser:
        # Retry with Playwright before giving up
        print(f"[Scraper] Detected block page, retrying with Playwright...")
        html = _fetch_html_playwright(url)
        soup = BeautifulSoup(html, "lxml")
        title_text = (soup.find("title").get_text(strip=True) if soup.find("title") else "").lower()
        is_blocked = any(phrase in title_text for phrase in blocked_phrases)

    # Store name from domain
    domain = urlparse(url).netloc.replace("www.", "")
    store = domain.split(".")[0].capitalize()

    # ── Try JSON-LD first ─────────────────────────────────────────────────
    jsonld = _extract_jsonld(soup)

    name = None
    price = None
    original_price = None
    image_url = None
    images = []
    variants = {}

    if jsonld:
        name = jsonld.get("name")
        image_url = _first_str(jsonld.get("image"))
        if isinstance(jsonld.get("image"), list):
            images = [img for img in jsonld["image"] if isinstance(img, str)]

        offers = jsonld.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if isinstance(offers, dict):
            price = _to_float(offers.get("price"))
            original_price = _to_float(offers.get("highPrice")) or price

        # Variants from hasVariant
        if "hasVariant" in jsonld:
            variant_list = jsonld["hasVariant"]
            if isinstance(variant_list, list):
                sizes = set()
                colours = set()
                for v in variant_list:
                    if isinstance(v, dict):
                        for prop in v.get("additionalProperty", []):
                            if isinstance(prop, dict):
                                pname = prop.get("name", "").lower()
                                pval = prop.get("value", "")
                                if "size" in pname:
                                    sizes.add(pval)
                                elif "color" in pname or "colour" in pname:
                                    colours.add(pval)
                if sizes:
                    variants["sizes"] = sorted(sizes)
                if colours:
                    variants["colours"] = sorted(colours)

    # ── Fallback: Open Graph ──────────────────────────────────────────────
    if not name:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            name = og_title.get("content", "").strip()

    if not image_url:
        og_image = soup.find("meta", property="og:image")
        if og_image:
            image_url = og_image.get("content", "").strip()

    # ── Fallback: <title> and <h1> ────────────────────────────────────────
    if not name:
        title_tag = soup.find("title")
        if title_tag:
            name = title_tag.get_text(strip=True).split("|")[0].split("-")[0].strip()

    if not name:
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(strip=True)

    # ── Fallback: price from page ─────────────────────────────────────────
    if not price:
        price = _extract_price_from_html(soup)

    # ── Extract more images ───────────────────────────────────────────────
    if not images:
        images = _extract_images(soup, url)
    if image_url and image_url not in images:
        images.insert(0, image_url)

    # ── Extract variants from page ────────────────────────────────────────
    if not variants:
        variants = _extract_variants_from_html(soup)

    # Default missing price to 0 — user can edit in the review form
    if price is None:
        price = 0.0
    if original_price is None:
        original_price = price

    return {
        "url": url,
        "name": name or "Unknown Product",
        "store": store,
        "price": price,
        "original_price": original_price,
        "image_url": image_url or "",
        "images": images[:12],  # cap at 12
        "variants": variants,
    }


def check_price(url, use_browser=False):
    """
    Re-scrape a URL and return just the current price.

    Args:
        url: Product page URL
        use_browser: If True, use Playwright directly. If False, try requests
                     first with auto-fallback to Playwright on failure.

    Returns:
        float or None
    """
    try:
        url = sanitize_url(url)
        html = _fetch_html(url, use_browser=use_browser)
        soup = BeautifulSoup(html, "lxml")

        # Try JSON-LD first
        jsonld = _extract_jsonld(soup)
        if jsonld:
            offers = jsonld.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                p = _to_float(offers.get("price"))
                if p:
                    return p

        # Fallback to HTML
        return _extract_price_from_html(soup)
    except Exception:
        return None


# ── Private helpers ───────────────────────────────────────────────────────────


def _extract_jsonld(soup):
    """Find and parse JSON-LD Product data."""
    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        try:
            data = json.loads(script.string or "")
            # Could be a single object or a list
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("@type") in ("Product", "IndividualProduct"):
                        return item
            elif isinstance(data, dict):
                if data.get("@type") in ("Product", "IndividualProduct"):
                    return data
                # Check @graph
                if "@graph" in data:
                    for item in data["@graph"]:
                        if isinstance(item, dict) and item.get("@type") in ("Product", "IndividualProduct"):
                            return item
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _extract_price_from_html(soup):
    """Try common CSS patterns for price elements."""
    selectors = [
        '[class*="price" i][class*="current" i]',
        '[class*="price" i][class*="sale" i]',
        '[class*="price" i][class*="final" i]',
        '[class*="selling-price" i]',
        '[class*="product-price" i]',
        '[data-price]',
        '[itemprop="price"]',
        '.price',
        '#price',
    ]

    for selector in selectors:
        try:
            els = soup.select(selector)
            for el in els:
                dp = el.get("data-price")
                if dp:
                    p = _to_float(dp)
                    if p:
                        return p
                content = el.get("content")
                if content:
                    p = _to_float(content)
                    if p:
                        return p
                text = el.get_text(strip=True)
                p = _to_float(text)
                if p and p > 0.5:
                    return p
        except Exception:
            continue

    # Last resort: regex over the whole page for currency patterns
    text = soup.get_text()
    patterns = [
        r'[\u20b9$]\s*([0-9,]+(?:\.\d{2})?)',
        r'(?:Rs\.?|INR|USD)\s*([0-9,]+(?:\.\d{2})?)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text)
        if matches:
            p = _to_float(matches[0])
            if p and p > 0.5:
                return p

    return None


def _extract_images(soup, base_url):
    """Extract product images from common gallery patterns."""
    images = []
    seen = set()

    def _add(src):
        if src and src not in seen and not _is_junk_image(src):
            seen.add(src)
            images.append(src)

    # Amazon-specific: landingImage has the hero shot
    landing = soup.find("img", id="landingImage")
    if landing:
        _add(landing.get("data-old-hires") or landing.get("src"))

    # Amazon-specific: thumbnail strip images (data-old-hires on #altImages)
    for el in soup.select("#altImages img, #imageBlock img"):
        _add(el.get("data-old-hires") or el.get("src"))

    selectors = [
        '[class*="gallery"] img',
        '[class*="carousel"] img',
        '[class*="product-image"] img',
        '[class*="product__media"] img',
        '[class*="pdp"] img',
        '[class*="slider"] img',
        '[data-zoom-image]',
    ]

    for selector in selectors:
        try:
            for el in soup.select(selector):
                src = (
                    el.get("data-zoom-image")
                    or el.get("data-src")
                    or el.get("data-srcset", "").split(",")[0].split(" ")[0]
                    or el.get("src")
                    or ""
                )
                _add(src)
        except Exception:
            continue

    # Broader fallback: any large-ish product img not yet found
    if len(images) < 2:
        for el in soup.select('[class*="product"] img'):
            src = el.get("data-src") or el.get("src") or ""
            _add(src)

    return images[:12]


def _is_junk_image(src):
    """Filter out tracking pixels, icons, SVGs, and tiny spacer images."""
    if not src:
        return True
    lower = src.lower()
    junk_patterns = [
        ".svg", "icon", "transparent-pixel", "spacer", "blank.",
        "data:image", "badge", "logo", "sprite", "spinner",
        "loading", "placeholder",
    ]
    return any(p in lower for p in junk_patterns)


def _extract_variants_from_html(soup):
    """Try to extract size/colour variant data from HTML."""
    variants = {}

    # Words that indicate a non-variant element was matched
    _junk = {
        "select", "choose", "size", "color", "colour", "quick view",
        "add to bag", "add to cart", "buy now", "next", "previous",
        "notify me", "sold out", "view", "close", "apply",
    }

    def _is_variant_text(text):
        return (
            text
            and 1 <= len(text) <= 15
            and text.lower() not in _junk
            and not text.startswith("Quick")
        )

    # Scope size selectors to product forms / swatch containers
    size_selectors = [
        '[class*="size"] [class*="swatch"]',
        '[class*="size"] [class*="variant"]',
        'form [class*="size"] option',
        'form [class*="size"] button',
        '[class*="size-selector"] button',
        '[class*="size-selector"] a',
        '[class*="size-picker"] button',
        '[data-option-name*="ize"] button',
        '[data-option-name*="ize"] a',
    ]
    sizes = set()
    for sel in size_selectors:
        for el in soup.select(sel):
            text = el.get_text(strip=True)
            if _is_variant_text(text):
                sizes.add(text)
    if sizes:
        variants["sizes"] = sorted(sizes)

    # Scope colour selectors similarly
    colour_selectors = [
        '[class*="color"] [class*="swatch"]',
        '[class*="colour"] [class*="swatch"]',
        'form [class*="color"] option',
        'form [class*="colour"] option',
        '[class*="color-selector"] button',
        '[class*="colour-selector"] button',
        '[data-option-name*="olor"] button',
        '[data-option-name*="olour"] button',
    ]
    colours = set()
    for sel in colour_selectors:
        for el in soup.select(sel):
            text = el.get("title") or el.get("aria-label") or el.get_text(strip=True)
            if _is_variant_text(text):
                colours.add(text)
    if colours:
        variants["colours"] = sorted(colours)

    return variants


def _to_float(val):
    """Convert a value to float, stripping currency symbols and commas."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    cleaned = re.sub(r"[^\d.]", "", s)
    try:
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _first_str(val):
    """Return first string from a value that might be a string or list."""
    if isinstance(val, str):
        return val
    if isinstance(val, list) and val:
        return val[0] if isinstance(val[0], str) else None
    return None
