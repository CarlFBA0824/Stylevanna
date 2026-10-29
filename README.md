# Stylevana Scraper

Scrapes [stylevana.com/en_US](https://www.stylevana.com/en_US/) (Magento 2)
product **variations**, **prices**, **SKUs**, and **brand** using
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

**Confirmed from real product pages** (SKIN1004 28868, Cos De BAHA 24288):

- **Stylevana publishes no barcode.** There is no GTIN, EAN, UPC or JAN
  anywhere on the page: no Product JSON-LD (only Organization), and nothing
  in the attribute rows, the dataLayer, `spConfig` or the description. So
  `gtin` will be blank. The parser still checks JSON-LD, the attribute rows
  and labelled `Barcode: 880…` text, in case that changes.
- **Configurable products:** the `spConfig` x-magento-init block under
  `#product_addtocart_form` has a `sku` map (child id → child SKU),
  `optionPrices` (`finalPrice` is the current price; `rrp` is the "Was"
  price; `oldPrice` just repeats the final price), `optionStock`, and
  `productInfo.brand`.
- **Detail tab:** `p.attribute-row` holds `span.col.label` and
  `span.col.value`, e.g. Product Unit : 30ml.
- **Brand:** `a.product-brand`.
- `tests/fixtures/real_cosdebaha_configurable.html` is a trimmed copy of a
  real page.

**Not yet confirmed:** whether listing pagination is Magento's standard
`?p=N`. Discovery stops as soon as a page adds no new product ids, which also
covers Magento re-serving the last page.

**Before a full run**, do a smoke test:

```bash
python scrape.py --url https://www.stylevana.com/en_US/cos-de-baha-azelaic-acid-serum-az15-30ml24288.html --debug
python scrape.py --brand medicube --limit 3
```

## Pricing

- `original_price` is the "Was" price (the theme's `rrp` / `rrpPrice`), or
  the sale price when the product isn't discounted.
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

`product_name, product_url, variation, sku, original_price, sale_price, cart_price, cart_discount, currency, gtin, product_id, gtin_source, brand, stock`

`sku` is the child SKU for each variation, e.g. `BSSECDB00042NOC01M0030`.
`stock` is Stylevana's label, e.g. "In Stock".

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
