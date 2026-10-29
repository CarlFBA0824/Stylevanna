# Stylevana Scraper

Scrapes [stylevana.com/en_US](https://www.stylevana.com/en_US/) (Magento 2)
product **variations**, **prices**, and **GTIN**, using
[ZenRows](https://www.zenrows.com/) for proxying/JS rendering. Built from the
same layout as the `Moida-Scraper` repo (one row per variation, same CSV
columns), so the output drops straight into `master.py` for
ScanUnlimited/Keepa matching.

## What's verified (and what isn't yet)

The scraper was written without live access to stylevana.com (the dev
environment blocks the site). It was built against the live **homepage**
HTML, pasted in by hand.

**Confirmed from the real homepage HTML:**

- **Platform:** Magento 2, theme `bs_rubix`, with Rokanthemes modules. The US
  store code is `bwsvxxb_en` and prices are in USD.
- **Product tiles:** every product tile is a
  `<div class="product-item-info" data-product-id="...">`. The name and URL
  are on `a.product-item-link`. Prices are `data-price-amount` attributes:
  `data-price-type="rrpPrice"` is the "Was" price (only shown when
  discounted) and `data-price-type="finalPrice"` is the current price.
  `tests/fixtures/homepage_tiles.html` holds real tiles copied from that page.
- **Brand pages** live at `/en_US/brands/<slug>.html`. Some brands are
  nested, e.g. `/en_US/brands/shiseido/anessa.html`; pass those with
  `--category-url`.
- **Cloudflare** analytics are present. A bot check hasn't been seen yet but
  is likely, so every fetch goes through ZenRows' premium proxy.

**Not yet confirmed:**

1. **The product page itself**, which is where the barcode lives. The parser
   uses stock Magento 2 sources, in this order:
   - the configurable `jsonConfig`/`spConfig`, for per-shade or per-size prices
   - the `priceBox` `priceConfig`, plus the theme's `rrpPrice` span, for
     simple products
   - JSON-LD `gtin*`
   - the "More Information" attribute table (`#product-attribute-specs-table`),
     using a Barcode/EAN/UPC/JAN label
   - as a last resort, a labelled `Barcode: 880…` pattern in the page text

   Each row's `gtin_source` column records which one matched.
   `tests/fixtures/synthetic_magento_*.html` are **synthetic** stock-Magento
   pages. They test the parsing logic, not Stylevana's real markup.
2. **Listing pagination:** whether brand and category pages use Magento's
   standard `?p=N`. Discovery stops as soon as a page adds no new product ids,
   which also covers Magento re-serving the last page.

**Before a full run**, do a smoke test and look at `gtin_source` and the
prices:

```bash
python scrape.py --url https://www.stylevana.com/en_US/skin1004-madagascar-centella-hyalu-cica-water-fit-sun-serum-spf50-pa-50ml28868.html --debug
python scrape.py --brand medicube --limit 3 --save-html html_dump
```

If GTINs come back blank, send one of the saved `html_dump/` pages so the
parser can be pointed at the right spot.

## Pricing

- `original_price` is the "Was" price (the theme's `rrpPrice`), or the sale
  price when the product isn't discounted.
- `sale_price` is the current product-page price.
- `cart_price` is `sale_price * (1 - --discount-pct/100)`. The default
  discount is 0, so `cart_price` equals `sale_price`.

Stylevana's site-wide codes aren't flat like Moida's welcome30. The live
homepage advertises **SVHAWS**: an extra 18% off orders over $59 and 20% off
orders over $99, **members only**, and not combinable with other offers. That
discount depends on cart total and login, so it isn't applied by default. To
model it anyway:

```bash
python scrape.py --brand medicube --discount-pct 20 --discount-code SVHAWS
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then edit .env and paste your ZenRows API key
```

## Usage

```bash
python scrape.py --brand medicube --limit 3      # smoke test
python scrape.py --brand medicube                # whole brand
python scrape.py --category-url https://www.stylevana.com/en_US/brands/shiseido/anessa.html
python scrape.py --category-url "https://www.stylevana.com/en_US/skincare.html?price=10-20"
python scrape.py --url <product url> --debug     # one product, show every parser source
```

| Flag | Default | Description |
|---|---|---|
| `--brand` | none | Brand slug, which becomes `/en_US/brands/<slug>.html` |
| `--category-url` | none | Any listing page URL. Existing filters are kept when paginating |
| `--url` | none | Debug mode: scrape one product page |
| `--debug` | off | With `--url`, also print every parsed source |
| `--max-pages` | 50 | Most listing pages to walk |
| `--page-size` | none | Sends `product_list_limit` if set |
| `--workers` | 4 | Product pages fetched at the same time |
| `--limit N` | none | Only scrape the first N products |
| `--no-js-render` | off | Skip ZenRows JS rendering, which is cheaper if the site allows it |
| `--save-html DIR` | none | Save every fetched product page's HTML |
| `--discount-pct` | 0 | Percent off `sale_price` used for `cart_price` |
| `--discount-code` | none | Label recorded in `cart_discount` |

Results are written to `output/stylevana_<slug>_<timestamp>.csv` and `.json`,
one row per **variation**:

`product_name, product_url, variation, sku, original_price, sale_price, cart_price, cart_discount, currency, gtin, product_id, gtin_source`

A configurable product's page-level barcode can't be tied to a specific shade,
so on a multi-variant product `gtin` is left blank instead of being copied
onto every row.

## Matching against Amazon: master.py

`master.py` is a copy of Moida's `master.py`, pointed at
`output/stylevana_*.csv`. It reads `cart_price` as the cost basis.

```bash
python master.py --vitacost output/stylevana_medicube_<timestamp>.csv --scanunlimited ScanUnlimited_export.csv --keepa KeepaExport.csv --output output/master_medicube.xlsx
```

To combine several brand runs into one batch:

```bash
python merge_csv.py output/stylevana_medicube_*.csv output/stylevana_celimax_*.csv --output output/stylevana_combined.csv
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Notes

- **ZenRows credits:** one rendered request per listing page, plus one per
  product. Use `--limit` while tuning.
- Respect the site's `robots.txt` and terms of service, and keep concurrency
  reasonable (`--workers`).
