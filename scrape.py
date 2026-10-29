"""
Stylevana (Magento 2) scraper - variations, prices, and GTIN, via ZenRows.

Structured after the Moida scraper (same ZenRows client, same one-row-per-
variation CSV/JSON output) so the results drop straight into master.py for
ScanUnlimited/Keepa matching.

Targets any Stylevana US listing page - a brand page
(/en_US/brands/<slug>.html) or a category page (/en_US/skincare.html,
/en_US/makeup/lip/lip-tint.html, ...). Pass --brand <slug> as a shortcut for
the brand page.

What's confirmed vs. not (see README "What's verified"):
    CONFIRMED from the live homepage HTML: Magento 2 (theme "bs_rubix",
    Rokanthemes modules), US store code "bwsvxxb_en", USD pricing, and the
    product tile markup - every tile is a
    <div class="product-item-info" data-product-id="...">, with the name/URL
    on a.product-item-link and prices as data-price-amount attributes on
    data-price-type="rrpPrice" ("Was") and data-price-type="finalPrice".
    NOT YET CONFIRMED: the product page itself (where the barcode lives) and
    whether brand/category pages paginate with Magento's standard ?p=N.
    The product-page parser below uses standard Magento 2 sources
    (swatch-renderer/configurable jsonConfig, priceBox priceConfig, the
    "More Information" attribute table, JSON-LD) plus a labelled-barcode
    text scan, and records which source each GTIN came from in gtin_source
    so a smoke test shows immediately what actually worked.

Setup:
    pip install -r requirements.txt
    Create a .env file next to this script containing:
        ZENROWS_API_KEY=your_key_here

Run:
    python scrape.py --brand medicube --limit 3     (quick test on 3 products)
    python scrape.py --brand medicube               (full brand)
    python scrape.py --category-url https://www.stylevana.com/en_US/skincare/suncare/sunscreen.html
    python scrape.py --url <product url> --debug    (one product, show every source)

Results are written to output/stylevana_<slug>_<timestamp>.csv and .json,
one row per variation (a product with 3 shades produces 3 rows):
    product_name, product_url, variation, sku,
    original_price, sale_price, cart_price, cart_discount,
    currency, gtin, product_id, gtin_source
"""

import argparse
import csv
import json
import logging
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_URL = "https://www.stylevana.com"
STORE_PATH = "/en_US"


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    zenrows_api_key: str
    zenrows_endpoint: str = "https://api.zenrows.com/v1/"
    request_timeout_seconds: int = 230
    request_delay_seconds: float = 0.75
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    max_workers: int = 4
    proxy_country: str = "us"


def load_settings() -> Settings:
    api_key = os.getenv("ZENROWS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ZENROWS_API_KEY is not set. Create a .env file next to this script "
            "containing: ZENROWS_API_KEY=your_key_here"
        )
    return Settings(zenrows_api_key=api_key)


# --------------------------------------------------------------------------
# ZenRows client
# --------------------------------------------------------------------------

class ZenRowsError(RuntimeError):
    pass


class ZenRowsClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = requests.Session()

    def fetch(
        self,
        url: str,
        js_render: bool = True,
        premium_proxy: bool = True,
        wait_for: Optional[str] = None,
        wait_ms: Optional[int] = None,
        max_retries: Optional[int] = None,
    ) -> requests.Response:
        params = {
            "apikey": self._settings.zenrows_api_key,
            "url": url,
            "js_render": "true" if js_render else "false",
        }
        if premium_proxy:
            params["premium_proxy"] = "true"
            params["proxy_country"] = self._settings.proxy_country
        if wait_for:
            params["wait_for"] = wait_for
        if wait_ms:
            params["wait"] = str(wait_ms)

        retries = max_retries if max_retries is not None else self._settings.max_retries
        last_error: Optional[Exception] = None

        for attempt in range(1, retries + 1):
            try:
                response = self._session.get(
                    self._settings.zenrows_endpoint,
                    params=params,
                    timeout=self._settings.request_timeout_seconds,
                )
                if response.status_code == 200:
                    return response
                last_error = ZenRowsError(
                    f"ZenRows returned HTTP {response.status_code} for {url}: "
                    f"{response.text[:300]}"
                )
                logger.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, last_error)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Attempt %d/%d errored for %s: %s", attempt, retries, url, exc)

            if attempt < retries:
                time.sleep(self._settings.retry_backoff_seconds * attempt)

        raise ZenRowsError(f"Failed to fetch {url} after {retries} attempt(s)") from last_error


# --------------------------------------------------------------------------
# Listing page (brand / category): discover products
# --------------------------------------------------------------------------

def _to_price(raw: Any) -> Optional[float]:
    if raw in (None, ""):
        return None
    try:
        return round(float(raw), 2)
    except (TypeError, ValueError):
        return None


def _tile_price(tile, price_type: str) -> Optional[float]:
    node = tile.select_one(f'[data-price-type="{price_type}"][data-price-amount]')
    return _to_price(node["data-price-amount"]) if node else None


def parse_listing_tiles(html: str) -> List[Dict[str, Any]]:
    """Every product tile on a Stylevana listing page, in page order, deduped
    by product id (the homepage repeats products across carousels).

    Tile shape confirmed against the live homepage:
        <div class="product-item-info" data-product-id="28868" ...>
          <a class="product-item-link" href=".../skin1004-...-50ml28868.html" title="SKIN1004 - ...">
          <span data-price-amount="19" data-price-type="rrpPrice">     ("Was", only when discounted)
          <span data-price-amount="9.7" data-price-type="finalPrice">  (current price)
    Stock Magento's data-price-type="oldPrice" is kept as a fallback for the
    "Was" price in case a page uses the default price template."""
    soup = BeautifulSoup(html, "lxml")
    tiles: Dict[str, Dict[str, Any]] = {}
    for tile in soup.select(".product-item-info[data-product-id]"):
        product_id = tile["data-product-id"].strip()
        if not product_id or product_id in tiles:
            continue
        link = tile.select_one("a.product-item-link[href]")
        if not link:
            continue
        sale_price = _tile_price(tile, "finalPrice")
        was_price = _tile_price(tile, "rrpPrice") or _tile_price(tile, "oldPrice")
        tiles[product_id] = {
            "product_id": product_id,
            "product_name": (link.get("title") or link.get_text()).strip(),
            "product_url": link["href"].split("?")[0],
            "original_price": was_price or sale_price,
            "sale_price": sale_price,
        }
    return list(tiles.values())


def _add_query_params(url: str, **params: Any) -> str:
    """Merges params into url's existing query string, so a filtered listing
    like skincare.html?price=10-20 keeps its filter when paginated."""
    split = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(split.query))
    query.update({k: str(v) for k, v in params.items()})
    return urllib.parse.urlunsplit(split._replace(query=urllib.parse.urlencode(query)))


def collect_listing(
    client: ZenRowsClient,
    settings: Settings,
    listing_url: str,
    max_pages: int = 50,
    page_size: Optional[int] = None,
    js_render: bool = True,
) -> List[Dict[str, Any]]:
    """Walks ?p=1,2,3... until a page adds no new product ids. That stop rule
    also covers Magento's habit of serving the last page again for any p past
    the end, instead of an empty page."""
    found: Dict[str, Dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        params: Dict[str, Any] = {"p": page}
        if page_size:
            params["product_list_limit"] = page_size
        page_url = _add_query_params(listing_url, **params)
        response = client.fetch(page_url, js_render=js_render, premium_proxy=True)
        tiles = parse_listing_tiles(response.text)
        new = [t for t in tiles if t["product_id"] not in found]
        for tile in new:
            found[tile["product_id"]] = tile
        logger.info("Listing page %d: %d tiles (%d new, %d total)", page, len(tiles), len(new), len(found))
        if not new:
            break
        time.sleep(settings.request_delay_seconds)
    return list(found.values())


# --------------------------------------------------------------------------
# Product page: variations, price, SKU, GTIN
# --------------------------------------------------------------------------

GTIN_KEYS = ("gtin13", "gtin14", "gtin12", "gtin8", "gtin")
# Attribute-table labels / page text that introduce a barcode. Kept to explicit
# barcode words so an unrelated number (a product id, a phone number) is never
# picked up as a GTIN.
GTIN_LABEL_PATTERN = re.compile(r"\b(barcode|bar code|ean|upc|gtin|jan)\b", re.IGNORECASE)
GTIN_TEXT_PATTERN = re.compile(
    r"\b(?:barcode|bar code|ean(?:-?13)?|upc|gtin(?:-?1[234])?|jan(?: code)?)\b\s*(?:no\.?|number|code)?\s*[:#]?\s*(\d{8,14})\b",
    re.IGNORECASE,
)


def _magento_init_blocks(soup: BeautifulSoup) -> List[Any]:
    blocks = []
    for tag in soup.find_all("script", {"type": "text/x-magento-init"}):
        raw = tag.string or tag.get_text()
        try:
            blocks.append(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return blocks


def _walk(obj: Any) -> Iterator[Dict[str, Any]]:
    """Every dict nested anywhere inside obj (depth-first)."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def find_configurable_config(init_blocks: List[Any]) -> Optional[Dict[str, Any]]:
    """Magento's configurable-product config: the "jsonConfig" passed to
    Magento_Swatches/js/swatch-renderer, or the "spConfig" passed to the
    plain configurable widget. Both carry "attributes" and "optionPrices"."""
    for block in init_blocks:
        for node in _walk(block):
            for key in ("jsonConfig", "spConfig"):
                config = node.get(key)
                if isinstance(config, dict) and "attributes" in config and "optionPrices" in config:
                    return config
    return None


def find_price_config(init_blocks: List[Any]) -> Optional[Dict[str, Any]]:
    """The "priceConfig" Magento passes to the product page's priceBox widget:
    {"productId": ..., "prices": {"oldPrice": {"amount": ..}, "finalPrice": {"amount": ..}}}."""
    for block in init_blocks:
        for node in _walk(block):
            config = node.get("priceConfig")
            if isinstance(config, dict) and isinstance(config.get("prices"), dict):
                return config
    return None


def variants_from_configurable(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One variant per child product in a Magento configurable jsonConfig.
    The variation label joins every attribute's option label for that child,
    e.g. "Color: 01 Clear / Size: 50ml"."""
    labels: Dict[str, List[str]] = {}
    for attribute in (config.get("attributes") or {}).values():
        attr_label = attribute.get("label") or attribute.get("code") or "Option"
        for option in attribute.get("options") or []:
            for child_id in option.get("products") or []:
                labels.setdefault(str(child_id), []).append(f"{attr_label}: {option.get('label')}")

    sku_map = config.get("sku") if isinstance(config.get("sku"), dict) else {}
    variants = []
    for child_id, prices in (config.get("optionPrices") or {}).items():
        sale = _to_price((prices.get("finalPrice") or {}).get("amount"))
        was = _to_price((prices.get("oldPrice") or {}).get("amount"))
        variants.append(
            {
                "child_id": str(child_id),
                "variation": " / ".join(labels.get(str(child_id), [])) or "Default",
                "sku": sku_map.get(str(child_id)),
                "original_price": was or sale,
                "sale_price": sale,
            }
        )
    return variants


def _extract_jsonld_blocks(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    blocks = []
    for tag in soup.find_all("script", {"type": "application/ld+json"}):
        raw = tag.string or tag.get_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        blocks.extend(_walk(data))
    return blocks


def _jsonld_product(blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for block in blocks:
        block_type = block.get("@type")
        types = block_type if isinstance(block_type, list) else [block_type]
        if "Product" in types or "ProductGroup" in types:
            return block
    return None


def _first_gtin(obj: Dict[str, Any]) -> Optional[str]:
    for key in GTIN_KEYS:
        value = obj.get(key)
        if value and re.fullmatch(r"\d{8,14}", str(value).strip()):
            return str(value).strip()
    return None


def _attribute_table(soup: BeautifulSoup) -> Dict[str, str]:
    """Label -> value pairs from Magento's "More Information" table
    (#product-attribute-specs-table: <th class="col label"> / <td class="col data">)."""
    pairs: Dict[str, str] = {}
    for row in soup.select("#product-attribute-specs-table tr, .additional-attributes tr"):
        label = row.find("th")
        value = row.find("td")
        if label and value:
            pairs[label.get_text(" ", strip=True)] = value.get_text(" ", strip=True)
    return pairs


def find_gtin(soup: BeautifulSoup, jsonld: Optional[Dict[str, Any]], attributes: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """(gtin, source) from, in order: JSON-LD, the attribute table, then any
    "Barcode: 880..." style label in the page text."""
    if jsonld:
        gtin = _first_gtin(jsonld)
        if gtin:
            return gtin, "jsonld"
        for offer in _walk(jsonld.get("offers")):
            gtin = _first_gtin(offer)
            if gtin:
                return gtin, "jsonld_offer"

    for label, value in attributes.items():
        if GTIN_LABEL_PATTERN.search(label):
            digits = re.sub(r"\D", "", value)
            if 8 <= len(digits) <= 14:
                return digits, f"attribute_table:{label}"

    for script in soup(["script", "style", "noscript"]):
        script.extract()
    match = GTIN_TEXT_PATTERN.search(soup.get_text(" ", strip=True))
    if match:
        return match.group(1), "page_text"
    return None, None


def _page_price(soup: BeautifulSoup, price_type: str) -> Optional[float]:
    node = soup.select_one(
        f'.product-info-main [data-price-type="{price_type}"][data-price-amount], '
        f'.product-info-price [data-price-type="{price_type}"][data-price-amount]'
    )
    return _to_price(node["data-price-amount"]) if node else None


def parse_product_page(html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    init_blocks = _magento_init_blocks(soup)
    jsonld = _jsonld_product(_extract_jsonld_blocks(soup))
    attributes = _attribute_table(soup)

    name_node = soup.select_one("h1.page-title span, h1.page-title, h1")
    name = name_node.get_text(" ", strip=True) if name_node else (jsonld or {}).get("name")

    sku_node = soup.select_one(".product.attribute.sku .value, [itemprop=sku]")
    sku = sku_node.get_text(strip=True) if sku_node else None
    if not sku:
        form = soup.select_one("form[data-product-sku]")
        sku = form["data-product-sku"] if form else (jsonld or {}).get("sku")

    product_id = None
    id_input = soup.select_one('#product_addtocart_form input[name="product"], input[name="product"][value]')
    if id_input:
        product_id = id_input.get("value")

    configurable = find_configurable_config(init_blocks)
    price_config = find_price_config(init_blocks)
    if configurable:
        variants = variants_from_configurable(configurable)
        price_source = "configurable_json"
    else:
        sale = was = None
        price_source = None
        if price_config:
            prices = price_config["prices"]
            sale = _to_price((prices.get("finalPrice") or {}).get("amount"))
            was = _to_price((prices.get("oldPrice") or {}).get("amount"))
            product_id = product_id or price_config.get("productId")
            price_source = "price_config"
        if sale is None:
            sale = _page_price(soup, "finalPrice")
            price_source = "page_price" if sale is not None else None
        # The theme's custom "Was" price (rrpPrice) is what listing tiles show;
        # prefer it over Magento's oldPrice, which is often just equal to the
        # final price when the discount comes from a catalog rule.
        was = _page_price(soup, "rrpPrice") or was
        variants = [
            {
                "child_id": product_id,
                "variation": "Default",
                "sku": sku,
                "original_price": was if was and sale and was > sale else sale,
                "sale_price": sale,
            }
        ]

    gtin, gtin_source = find_gtin(soup, jsonld, attributes)
    return {
        "name": name,
        "sku": sku,
        "product_id": str(product_id) if product_id else None,
        "gtin": gtin,
        "gtin_source": gtin_source,
        "price_source": price_source,
        "variants": variants,
        "attributes": attributes,
        "jsonld_found": jsonld is not None,
    }


def build_rows(
    product_url: str,
    parsed: Dict[str, Any],
    tile: Optional[Dict[str, Any]] = None,
    discount_pct: float = 0.0,
    discount_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Final CSV row(s) for one product. A configurable product's single page
    barcode can't be attributed to a specific shade/size, so it's only
    applied when the product has exactly one variant - otherwise gtin is left
    blank rather than wrongly copied onto every variant."""
    tile = tile or {}
    variants = parsed["variants"] or [{"variation": "Unknown", "sku": None, "original_price": None, "sale_price": None}]
    single = len(variants) == 1
    rows = []
    for v in variants:
        sale = v.get("sale_price") if v.get("sale_price") is not None else tile.get("sale_price")
        original = v.get("original_price") or tile.get("original_price") or sale
        cart_price = round(sale * (1 - discount_pct / 100), 2) if sale is not None else None
        rows.append(
            {
                "product_name": parsed.get("name") or tile.get("product_name") or product_url,
                "product_url": product_url,
                "variation": v.get("variation") or "Default",
                "sku": v.get("sku") or (parsed.get("sku") if single else None),
                "original_price": original,
                "sale_price": sale,
                "cart_price": cart_price,
                "cart_discount": f"{discount_code} ({discount_pct:g}% off, assumed)" if discount_pct else None,
                "currency": "USD",
                "gtin": parsed.get("gtin") if single else None,
                "product_id": v.get("child_id") or parsed.get("product_id") or tile.get("product_id"),
                "gtin_source": parsed.get("gtin_source") if single else None,
            }
        )
    return rows


def scrape_product(
    client: ZenRowsClient,
    product_url: str,
    tile: Optional[Dict[str, Any]] = None,
    js_render: bool = True,
    discount_pct: float = 0.0,
    discount_code: Optional[str] = None,
    save_html_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    response = client.fetch(product_url, js_render=js_render, premium_proxy=True, wait_ms=3000 if js_render else None)
    if save_html_dir:
        save_html_dir.mkdir(parents=True, exist_ok=True)
        slug = urllib.parse.urlsplit(product_url).path.rstrip("/").rsplit("/", 1)[-1]
        (save_html_dir / slug).write_text(response.text, encoding="utf-8")
    parsed = parse_product_page(response.text)
    if not parsed["gtin"]:
        logger.warning("No GTIN found on %s", product_url)
    return build_rows(product_url, parsed, tile, discount_pct, discount_code)


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

FIELDNAMES = [
    "product_name", "product_url", "variation", "sku",
    "original_price", "sale_price", "cart_price", "cart_discount",
    "currency", "gtin", "product_id", "gtin_source",
]


def export_csv(rows: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def export_json(rows: List[Dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def slug_from_url(url: str) -> str:
    """Short, filesystem-safe label from a listing URL, e.g.
    .../en_US/brands/medicube.html -> stylevana_medicube,
    .../en_US/makeup/lip/lip-tint.html -> stylevana_makeup-lip-lip-tint."""
    path = urllib.parse.urlsplit(url).path
    if path.startswith(STORE_PATH):
        path = path[len(STORE_PATH):]
    path = re.sub(r"\.html$", "", path.strip("/"))
    if path.startswith("brands/"):
        path = path[len("brands/"):]
    segment = path.replace("/", "-") or "listing"
    return re.sub(r"[^a-zA-Z0-9_-]", "-", f"stylevana_{segment}")[:80]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape a Stylevana US brand/category listing's product variations, prices, and GTINs via ZenRows."
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--brand", type=str, help="Brand slug, e.g. 'medicube' for /en_US/brands/medicube.html")
    target.add_argument("--category-url", type=str, help="Full URL of any Stylevana listing page")
    target.add_argument("--url", type=str, help="Debug mode: scrape a single product URL and print the result")
    parser.add_argument("--debug", action="store_true", help="With --url, also print every source the parser found")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=None, help="Optional product_list_limit per listing page")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None, help="Only scrape the first N products")
    parser.add_argument("--output-dir", type=str, default="output")
    parser.add_argument("--no-js-render", action="store_true", help="Fetch without JS rendering (cheaper, if the site allows it)")
    parser.add_argument("--save-html", type=str, default=None, help="Directory to save every fetched product page's HTML")
    parser.add_argument(
        "--discount-pct", type=float, default=0.0,
        help="Percent off sale_price to compute cart_price (default 0 - Stylevana's codes are tiered and members-only)",
    )
    parser.add_argument("--discount-code", type=str, default=None, help="Label recorded in cart_discount")
    args = parser.parse_args()

    settings = load_settings()
    client = ZenRowsClient(settings)
    js_render = not args.no_js_render
    save_dir = Path(args.save_html) if args.save_html else None

    if args.url:
        response = client.fetch(args.url, js_render=js_render, premium_proxy=True, wait_ms=3000 if js_render else None)
        parsed = parse_product_page(response.text)
        if args.debug:
            print("--- Parsed sources ---")
            print(json.dumps({k: v for k, v in parsed.items() if k != "variants"}, indent=2, ensure_ascii=False))
            print("--- Variants ---")
            print(json.dumps(parsed["variants"], indent=2, ensure_ascii=False))
            print("--- Final scraped row(s) ---")
        print(json.dumps(build_rows(args.url, parsed, None, args.discount_pct, args.discount_code), indent=2, ensure_ascii=False))
        return

    if args.brand:
        listing_url = f"{BASE_URL}{STORE_PATH}/brands/{args.brand.strip().lower()}.html"
    elif args.category_url:
        listing_url = args.category_url
    else:
        parser.error("pass --brand, --category-url, or --url")

    logger.info("Collecting products from %s", listing_url)
    tiles = collect_listing(client, settings, listing_url, args.max_pages, args.page_size, js_render)
    logger.info("Found %d products", len(tiles))
    if not tiles:
        logger.error("No product tiles found. The listing page markup may differ from the homepage's.")
        return
    if args.limit:
        tiles = tiles[: args.limit]

    all_rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                scrape_product, client, tile["product_url"], tile, js_render,
                args.discount_pct, args.discount_code, save_dir,
            ): tile["product_url"]
            for tile in tiles
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                rows = future.result()
                all_rows.extend(rows)
                logger.info("Scraped %d variation row(s) from %s", len(rows), url)
            except Exception:
                logger.exception("Failed to scrape %s", url)

    with_gtin = sum(1 for r in all_rows if r["gtin"])
    logger.info("GTIN found on %d of %d rows", with_gtin, len(all_rows))

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(args.output_dir)
    slug = slug_from_url(listing_url)
    csv_path = output_dir / f"{slug}_{timestamp}.csv"
    json_path = output_dir / f"{slug}_{timestamp}.json"
    export_csv(all_rows, csv_path)
    export_json(all_rows, json_path)
    logger.info("Wrote %d rows to %s and %s", len(all_rows), csv_path, json_path)


if __name__ == "__main__":
    main()
