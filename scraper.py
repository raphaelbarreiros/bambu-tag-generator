#!/usr/bin/env python3
"""
Filament Data Extractor

Scrapes filament data from BambuLab product pages and saves to CSV (and optionally Excel).
Extracts collection names, filament categories, color names, and color codes from
Schema.org Product JSON-LD embedded in the page HTML.

Usage:
    python scraper.py --all                        # scrape all filaments (global store)
    python scraper.py --all --store us             # use US store
    python scraper.py --all --store eu             # use EU store
    python scraper.py --url-file urls.txt          # scrape specific URLs
    python scraper.py --all --export-csv both      # CSV + Excel output
"""

import argparse
import csv
import json
import logging
import os
import re
import time
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})
_REQUEST_DELAY = 2.0  # seconds between product page requests
_MAX_RETRIES = 3
_RETRY_BACKOFF = 10.0  # seconds to wait on 403, doubles each retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BAMBULAB_STORES = {
    "global": "https://store.bambulab.com",
    "us": "https://us.store.bambulab.com",
    "eu": "https://eu.store.bambulab.com",
    "uk": "https://uk.store.bambulab.com",
    "au": "https://au.store.bambulab.com",
    "ca": "https://ca.store.bambulab.com",
    "asia": "https://asia.store.bambulab.com",
    "kr": "https://kr.store.bambulab.com",
    "jp": "https://jp.store.bambulab.com",
}
BAMBULAB_BASE = BAMBULAB_STORES["global"]

# Collection pages that contain filament product links.
# The main page doesn't list support/PPS filaments, so we check those separately.
_COLLECTION_PAGES = [
    "/collections/bambu-lab-3d-printer-filament",
    "/collections/support",
    "/collections/pps",
    "/collections/pa-pet",
]

# Product slugs to skip (bundles, printers, non-filament items)
_SKIP_SLUGS = {
    "pla-basic-beginner-s-filament-pack",
    "pla-cmyk-lithophane",
    "pla-tough-upgrade",
    "h2c", "h2d", "h2s", "p2s",
}


# ── Auto-discovery ───────────────────────────────────────────────────────────

def discover_product_urls(base_url=BAMBULAB_BASE):
    """Scrape collection pages to find all filament product URLs."""
    seen_slugs = set()
    product_urls = []

    for i, path in enumerate(_COLLECTION_PAGES):
        if i > 0:
            time.sleep(_REQUEST_DELAY)
        url = base_url + path
        log.info("Discovering products from %s", url)
        try:
            resp = _SESSION.get(url, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("Failed to fetch %s: %s", url, e)
            continue

        for match in re.findall(r'href="(/[^"]*?/products/([^"\\]+))"', resp.text):
            href, slug = match
            if slug in seen_slugs or slug in _SKIP_SLUGS:
                continue
            seen_slugs.add(slug)
            product_urls.append(base_url + href)

    log.info("Discovered %d product URLs", len(product_urls))
    return product_urls


# ── Collection detection ─────────────────────────────────────────────────────

# Map category prefixes to collection names
_CATEGORY_TO_COLLECTION = {
    "PLA": "PLA", "PETG": "PETG", "ABS": "ASA/ABS", "ASA": "ASA/ABS",
    "TPU": "PC/TPU", "PC": "PC/TPU",
    "PA6": "PA/PET", "PAHT": "PA/PET", "PET-CF": "PA/PET", "PPA": "PA/PET",
    "PPS": "PPS", "PVA": "SUPPORT",
    "Support": "SUPPORT",
}


def collection_from_category(category):
    """Derive collection name from the category/product type."""
    if not category:
        return None
    # Check exact prefix matches (longest first to avoid "PLA" matching before "PLA-CF")
    for prefix in sorted(_CATEGORY_TO_COLLECTION, key=len, reverse=True):
        if category.upper().startswith(prefix.upper()):
            return _CATEGORY_TO_COLLECTION[prefix]
    return None


def collection_from_url(url):
    """Extract collection name from URL path (most reliable method).

    /en-lu/collections/pla -> PLA
    /en-lu/collections/pc-tpu -> PC/TPU
    """
    parts = urlparse(url).path.split("/")
    try:
        idx = parts.index("collections")
        slug = parts[idx + 1]
    except (ValueError, IndexError):
        return None

    if "-" in slug:
        return "/".join(p.upper() for p in slug.split("-"))
    return slug.upper()


def collection_from_html(soup):
    """Fallback: extract collection from HTML elements."""
    # Try collection buttons
    for btn in soup.find_all("button", {"data-action": "toggle-collapsible"}):
        anchor = btn.find("a")
        if anchor and "/collections/" in anchor.get("href", "") and anchor.string:
            return anchor.string.strip()

    # Try breadcrumbs
    nav = soup.find("nav", class_="Breadcrumbs")
    if nav:
        for link in nav.find_all("a"):
            if "/collections/" in link.get("href", "") and link.string:
                return link.string.strip()

    return None


# ── Category / name normalization ────────────────────────────────────────────

# Patterns that should be stripped from the end of color names when they
# duplicate the category qualifier (e.g. "Iron Metallic" under "PLA Metal")
_CATEGORY_SUFFIX_STRIP = {
    "PLA Metal": "Metallic",
    "PLA Sparkle": "Sparkle",
}

# Category rename table applied once after all extraction
_CATEGORY_RENAMES = {
    "PLA Silk Dual Color": "PLA Silk 2C",
}


def normalize_category(category, offer_name=None):
    """Single-pass category normalization."""
    if not category:
        return category

    # Rename table
    for old, new in _CATEGORY_RENAMES.items():
        if old in category:
            category = category.replace(old, new)

    # Strip "(New Version)"
    category = category.replace("(New Version)", "").strip()
    category = re.sub(r"\s+", " ", category)

    # TPU hardness detection from offer name
    if "TPU" in category and offer_name and "/" in category:
        m = re.match(r"^(TPU\s+\d+A)", offer_name)
        if not m:
            m = re.search(r"TPU[\s-]*(\d+)A", offer_name, re.IGNORECASE)
            if m:
                category = f"TPU {m.group(1)}A" if m.lastindex == 1 and m.group(1).isdigit() else m.group(0).upper()
            else:
                category = "TPU 85A"  # default when ambiguous
        else:
            category = m.group(1)

    # PVA normalization — the website labels this inconsistently
    if category.upper() in ("PVA", "SUPPORT PVA", "SUPPORT FOR PVA"):
        category = "PVA"

    return category


def clean_color_name(name, category):
    """Remove redundant prefixes from color names that repeat the category.

    Examples:
        ("Translucent Blue", "PETG Translucent") -> "Blue"
        ("Matte Ivory White", "PLA Matte")       -> "Ivory White"
        ("CF Black", "PETG-CF")                   -> "Black"
    """
    if not name or not category:
        return name

    # Build list of words that might appear as redundant prefixes
    prefixes = []
    for word in category.lower().replace("-", " ").split():
        if len(word) > 2:
            prefixes.append(word)

    name_lower = name.lower()
    for prefix in prefixes:
        if name_lower.startswith(prefix + " "):
            candidate = name[len(prefix):].strip()
            if len(candidate) >= 3:
                return candidate

    # Strip known category-specific suffixes
    suffix = _CATEGORY_SUFFIX_STRIP.get(category)
    if suffix and name.endswith(suffix):
        candidate = name[: -len(suffix)].strip()
        if len(candidate) >= 3:
            return candidate

    return name


# ── Variant name parsing ─────────────────────────────────────────────────────

# New format: "PLA Basic - Jade White (10100) / Refill / 1kg"
# Old format: "Jade White (10100) / Filament with spool / 1 kg"
# Use " - " (space-dash-space) to split category from color, so "PET-CF" stays intact
_RE_NEW_VARIANT = re.compile(r"^(.+?)\s+-\s+(.+?)\s*\(([^()]+)\)")
_RE_COLOR_CODE_START = re.compile(r"^([^/(]+?)\s*\(([^()]+)\)")
_RE_COLOR_CODE_AFTER_SLASH = re.compile(r"(?:[^/]+/\s*)+([^/(]+?)\s*\(([^()]+)\)")


def parse_variant_name(name, group_name=None):
    """Parse a variant/offer name into (category, color_name, color_code).

    Handles formats like:
      "PLA Basic - Jade White (10100) / Refill / 1kg"
      "TPU 85A / TPU 90A - TPU 90A / 1 kg / Blaze (51901)"
      "PVA - Filament with spool / 0.5 kg / Clear (66400)"
      "Jade White (10100) / Filament with spool / 1 kg"  (legacy)
    """
    # New format: split on " - " first
    m = _RE_NEW_VARIANT.match(name)
    if m:
        raw_category = m.group(1).strip()
        right_side = m.group(2).strip()
        code = m.group(3).strip()

        # Right side might be "Color" (simple) or "TPU 90A / 1 kg / Color" (complex)
        # The actual color name is always the last segment before (Code)
        segments = [s.strip() for s in right_side.split("/")]
        color = segments[-1]  # last segment is the color name

        # For TPU: the first segment after " - " tells us the actual type (TPU 85A or TPU 90A)
        # Check if any segment looks like a TPU type
        category = normalize_category(raw_category)
        for seg in segments:
            tpu_m = re.match(r"^(TPU\s+\d+A)", seg, re.IGNORECASE)
            if tpu_m:
                category = tpu_m.group(1)
                break

        color = clean_color_name(color, category)
        return category, color, code

    # Legacy format: "Color (Code) / stuff" at start
    m = _RE_COLOR_CODE_START.search(name)
    if not m:
        m = _RE_COLOR_CODE_AFTER_SLASH.search(name)

    if m:
        category = normalize_category(group_name or "Unknown", name)
        color = clean_color_name(m.group(1).strip(), category)
        code = m.group(2).strip()
        return category, color, code

    return normalize_category(group_name or "Unknown"), name, ""


# ── Main extraction pipeline ─────────────────────────────────────────────────

def fetch_page(url):
    """Fetch URL and return (schema_data_list, soup) or ([], None) on failure."""
    resp = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = _SESSION.get(url, timeout=30)
            resp.raise_for_status()
            break
        except requests.exceptions.HTTPError as e:
            if resp is not None and resp.status_code == 403 and attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                log.warning("Rate limited, waiting %.0fs before retry (%d/%d)",
                            wait, attempt + 2, _MAX_RETRIES)
                time.sleep(wait)
                continue
            log.error("Failed to fetch %s: %s", url, e)
            return [], None
        except requests.RequestException as e:
            log.error("Failed to fetch %s: %s", url, e)
            return [], None

    soup = BeautifulSoup(resp.text, "html.parser")

    schema_data = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string)
            if isinstance(data, list):
                schema_data.extend(data)
            else:
                schema_data.append(data)
        except (json.JSONDecodeError, TypeError):
            continue

    return schema_data, soup


def extract_filaments(urls, override_collections=None):
    """Process URLs and return ({collection: [filament_dicts]}, [failed_urls])."""
    all_data = {}
    failed_urls = []

    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(_REQUEST_DELAY)
        log.info("Processing [%d/%d] %s", i + 1, len(urls), url)
        schema_data, soup = fetch_page(url)
        if not soup:
            failed_urls.append(url)
            continue

        # Determine collection (may be overridden per-filament below)
        if override_collections and url in override_collections:
            url_collection = override_collections[url]
            log.info("Using override collection: %s", url_collection)
        else:
            url_collection = collection_from_url(url) or collection_from_html(soup)

        for obj in schema_data:
            if not isinstance(obj, dict):
                continue

            obj_type = obj.get("@type")

            # New format: ProductGroup with hasVariant[]
            if obj_type == "ProductGroup":
                group_name = obj.get("name", "")
                variants = obj.get("hasVariant", [])
                if isinstance(variants, dict):
                    variants = [variants]

                for variant in variants:
                    if not isinstance(variant, dict):
                        continue
                    name = variant.get("name", "")
                    if not name:
                        continue
                    category, color, code = parse_variant_name(name, group_name)
                    collection = url_collection or collection_from_category(category) or "Unknown"
                    all_data.setdefault(collection, []).append({
                        "Collection": collection,
                        "Category": category,
                        "Name": color,
                        "Code": code,
                    })

            # Legacy format: Product with offers[]
            elif obj_type == "Product":
                schema_category = obj.get("category", "Uncategorized")
                group_name = obj.get("name", schema_category)
                offers = obj.get("offers", [])
                if isinstance(offers, dict):
                    offers = [offers]

                for offer in offers:
                    if not isinstance(offer, dict):
                        continue
                    offer_name = offer.get("name", "")
                    if not offer_name:
                        continue
                    category, color, code = parse_variant_name(offer_name, group_name)
                    collection = url_collection or collection_from_category(category) or "Unknown"
                    all_data.setdefault(collection, []).append({
                        "Collection": collection,
                        "Category": category,
                        "Name": color,
                        "Code": code,
                    })

    # Deduplicate
    for key in all_data:
        seen = set()
        unique = []
        for f in all_data[key]:
            sig = (f["Category"], f["Name"], f["Code"])
            if sig not in seen:
                seen.add(sig)
                unique.append(f)
        all_data[key] = unique

    return all_data, failed_urls


# ── Merge ────────────────────────────────────────────────────────────────────

def load_existing_csv(path):
    """Load existing combined CSV into {collection: [filament_dicts]}."""
    if not os.path.exists(path):
        return {}
    data = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            data.setdefault(row["Collection"], []).append(row)
    total = sum(len(v) for v in data.values())
    log.info("Loaded %d existing filaments from %s", total, path)
    return data


def merge_data(existing, new):
    """Merge new data into existing, deduplicating by (Category, Name, Code)."""
    merged = {}
    # Start with existing
    for key, filaments in existing.items():
        merged[key] = list(filaments)
    # Add new
    for key, filaments in new.items():
        if key not in merged:
            merged[key] = []
        existing_sigs = {(f["Category"], f["Name"], f["Code"]) for f in merged[key]}
        added = 0
        for f in filaments:
            sig = (f["Category"], f["Name"], f["Code"])
            if sig not in existing_sigs:
                merged[key].append(f)
                existing_sigs.add(sig)
                added += 1
        if added:
            log.info("Merged %d new filaments into %s", added, key)
    return merged


# ── Output ───────────────────────────────────────────────────────────────────

def write_csv_per_collection(data, output_dir):
    """Write one CSV per collection."""
    for collection, filaments in data.items():
        safe = collection.replace("/", "_").replace("\\", "_")
        path = os.path.join(output_dir, f"{safe}.csv")
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Collection", "Category", "Name", "Code"])
            w.writeheader()
            w.writerows(filaments)
        log.info("Wrote %s (%d filaments)", path, len(filaments))


def write_csv_combined(data, output_path):
    """Write all filaments to a single CSV."""
    all_rows = [f for filaments in data.values() for f in filaments]
    with open(output_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Collection", "Category", "Name", "Code"])
        w.writeheader()
        w.writerows(all_rows)
    log.info("Wrote %s (%d filaments)", output_path, len(all_rows))


def write_excel(data, output_path):
    """Write to Excel with one sheet per collection (requires pandas + openpyxl)."""
    try:
        import pandas as pd
    except ImportError:
        log.warning("pandas not installed — skipping Excel export")
        return

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        for collection, filaments in data.items():
            df = pd.DataFrame(filaments)
            sheet = re.sub(r"[\[\]*?:/\\]", "", collection)[:31]
            df.to_excel(writer, sheet_name=sheet, index=False)
    log.info("Wrote %s", output_path)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract BambuLab filament data")
    parser.add_argument("--all", action="store_true",
                        help="Auto-discover all filaments from BambuLab store")
    parser.add_argument("--store", default="global",
                        help="Store region: " + ", ".join(BAMBULAB_STORES.keys())
                             + " (default: global)")
    parser.add_argument("--merge", action="store_true",
                        help="Merge new results into existing CSV instead of overwriting")
    parser.add_argument("--urls", nargs="+", help="URLs to process")
    parser.add_argument("--url-file", help="File with one URL per line")
    parser.add_argument("--output", default="filament_data.xlsx", help="Output Excel path")
    parser.add_argument("--collection-override", help="JSON file mapping URLs to collection names")
    parser.add_argument("--export-csv", choices=["separate", "combined", "both"],
                        default="both", help="CSV export mode (default: both)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Collect URLs
    urls = list(args.urls or [])
    if args.url_file:
        try:
            with open(args.url_file) as f:
                urls.extend(line.strip() for line in f if line.strip())
        except IOError as e:
            log.error("Error reading URL file: %s", e)

    if args.all:
        store_key = args.store.lower()
        if store_key not in BAMBULAB_STORES:
            log.error("Unknown store '%s'. Use one of: %s",
                      args.store, ", ".join(BAMBULAB_STORES.keys()))
            return
        urls = discover_product_urls(BAMBULAB_STORES[store_key])

    if not urls:
        log.error("No URLs provided. Use --all, --urls, or --url-file")
        return

    # Load overrides
    overrides = None
    if args.collection_override:
        try:
            with open(args.collection_override) as f:
                overrides = json.load(f)
            log.info("Loaded %d collection overrides", len(overrides))
        except (IOError, json.JSONDecodeError) as e:
            log.error("Error loading overrides: %s", e)

    # Extract
    data, failed_urls = extract_filaments(urls, overrides)

    # Merge with existing data if requested
    output_dir = os.path.dirname(args.output) or "."
    combined_csv_path = os.path.join(
        output_dir,
        os.path.splitext(os.path.basename(args.output))[0] + "_combined.csv",
    )

    if args.merge:
        existing = load_existing_csv(combined_csv_path)
        data = merge_data(existing, data)

    if not data:
        log.warning("No filament data extracted")
        return

    total = sum(len(v) for v in data.values())
    log.info("Total: %d filaments across %d collections", total, len(data))

    # Output
    if args.export_csv in ("separate", "both"):
        write_csv_per_collection(data, output_dir)

    if args.export_csv in ("combined", "both"):
        write_csv_combined(data, combined_csv_path)

    write_excel(data, args.output)

    # Report failed URLs so user can retry just those
    if failed_urls:
        log.warning("%d URLs failed (rate limited). Re-run with --merge to fill gaps:",
                    len(failed_urls))
        for u in failed_urls:
            log.warning("  %s", u)
        # Write failed URLs to file for easy retry
        failed_path = os.path.join(output_dir, "failed_urls.txt")
        with open(failed_path, "w") as f:
            f.write("\n".join(failed_urls) + "\n")
        log.info("Failed URLs saved to %s — retry with:", failed_path)
        log.info("  python3 filament_extractor.py --url-file %s --merge", failed_path)


if __name__ == "__main__":
    main()
