from pathlib import Path

import pytest

import scrape

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- Listing tiles (real Stylevana homepage markup) -----------------------

def test_listing_tiles_parse_real_homepage_markup():
    tiles = scrape.parse_listing_tiles(_read("homepage_tiles.html"))
    assert [t["product_id"] for t in tiles] == ["28868", "19663", "89200"]

    sun_serum = tiles[0]
    assert sun_serum["product_name"] == "SKIN1004 - Madagascar Centella Hyalu-Cica Water-Fit Sun Serum SPF50 PA++++ - 50ml"
    assert sun_serum["product_url"].endswith("/en_US/skin1004-madagascar-centella-hyalu-cica-water-fit-sun-serum-spf50-pa-50ml28868.html")
    assert sun_serum["original_price"] == 19.0
    assert sun_serum["sale_price"] == 9.7


def test_listing_tile_without_was_price_uses_final_for_both():
    mask = scrape.parse_listing_tiles(_read("homepage_tiles.html"))[1]
    assert mask["original_price"] == 6.0
    assert mask["sale_price"] == 6.0


def test_listing_tile_name_is_html_unescaped():
    kumano = scrape.parse_listing_tiles(_read("homepage_tiles.html"))[2]
    assert "UV Care & Moisturizing" in kumano["product_name"]


def test_add_query_params_keeps_existing_filter():
    url = scrape._add_query_params("https://www.stylevana.com/en_US/skincare.html?price=10-20", p=2)
    assert url == "https://www.stylevana.com/en_US/skincare.html?price=10-20&p=2"


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """Serves page 1 and 2 with distinct tiles, then repeats page 2 for any
    later page - Magento's behavior for p past the last page."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, url, **kwargs):
        self.requested.append(url)
        page = int(url.rsplit("p=", 1)[1].split("&")[0])
        return _FakeResponse(self.pages[min(page, len(self.pages)) - 1])


def _tile(pid):
    return (
        f'<div class="product-item-info" data-product-id="{pid}">'
        f'<a class="product-item-link" href="https://www.stylevana.com/en_US/p{pid}.html" title="P{pid}">P{pid}</a>'
        f'<span data-price-amount="5" data-price-type="finalPrice"></span></div>'
    )


def test_collect_listing_stops_when_magento_repeats_last_page():
    pages = [_tile(1) + _tile(2), _tile(3)]
    client = _FakeClient(pages)
    settings = scrape.Settings(zenrows_api_key="x", request_delay_seconds=0)
    tiles = scrape.collect_listing(client, settings, "https://www.stylevana.com/en_US/brands/x.html")
    assert [t["product_id"] for t in tiles] == ["1", "2", "3"]
    assert len(client.requested) == 3  # page 3 repeated page 2 -> stop


def test_slug_from_url():
    assert scrape.slug_from_url("https://www.stylevana.com/en_US/brands/medicube.html") == "stylevana_medicube"
    assert scrape.slug_from_url("https://www.stylevana.com/en_US/makeup/lip/lip-tint.html") == "stylevana_makeup-lip-lip-tint"


# --- Product page (REAL Stylevana page, trimmed) ---------------------------

def test_real_configurable_uses_rrp_as_was_price():
    parsed = scrape.parse_product_page(_read("real_cosdebaha_configurable.html"))
    assert parsed["name"] == "Cos De BAHA - Azelaic Acid Serum (AZ15) - 30ml"
    assert parsed["brand"] == "Cos De BAHA"
    assert parsed["sku"] == "BSSECDB00042"
    assert parsed["product_id"] == "24288"
    assert parsed["price_source"] == "configurable_json"
    [variant] = parsed["variants"]
    assert variant["child_id"] == "24268"
    assert variant["sku"] == "BSSECDB00042NOC01M0030"
    assert variant["variation"] == "Color: N / Beauty Milliliter: 30ml"
    assert variant["sale_price"] == 11.79
    assert variant["original_price"] == 14.74  # rrp, not Magento's oldPrice (11.79)
    assert variant["stock"] == "In Stock"


def test_real_page_detail_rows_and_no_barcode():
    parsed = scrape.parse_product_page(_read("real_cosdebaha_configurable.html"))
    assert parsed["attributes"]["Product Unit"] == "30ml"
    assert parsed["attributes"]["Product Type"] == "Essence & Serums"
    assert parsed["gtin"] is None  # Stylevana doesn't publish barcodes
    [row] = scrape.build_rows("https://example/cos-de-baha.html", parsed)
    assert row["brand"] == "Cos De BAHA"
    assert row["stock"] == "In Stock"
    assert row["original_price"] == 14.74
    assert row["cart_price"] == 11.79


# --- Product page (SYNTHETIC stock-Magento fixtures, see file headers) ------

def test_configurable_product_variants_and_gtin_table():
    parsed = scrape.parse_product_page(_read("synthetic_magento_configurable.html"))
    assert parsed["name"] == "Example Brand - Example Lip Tint - 4g"
    assert parsed["price_source"] == "configurable_json"
    assert parsed["gtin"] == "8809876543210"
    assert parsed["gtin_source"] == "attribute_table:Barcode"
    by_label = {v["variation"]: v for v in parsed["variants"]}
    assert by_label["Color: 01 Rose"]["sale_price"] == 11.47
    assert by_label["Color: 01 Rose"]["original_price"] == 13.0
    assert by_label["Color: 02 Coral"]["sale_price"] == 13.0


def test_configurable_gtin_not_copied_onto_every_variant():
    parsed = scrape.parse_product_page(_read("synthetic_magento_configurable.html"))
    rows = scrape.build_rows("https://example/lip-tint.html", parsed)
    assert len(rows) == 2
    assert all(r["gtin"] is None for r in rows)
    assert {r["product_id"] for r in rows} == {"5001", "5002"}


def test_simple_product_prices_sku_and_text_barcode():
    parsed = scrape.parse_product_page(_read("synthetic_magento_simple.html"))
    assert parsed["sku"] == "SKITEXA00002"
    assert parsed["product_id"] == "6000"
    assert parsed["gtin"] == "8801234567890"
    assert parsed["gtin_source"] == "page_text"
    [row] = scrape.build_rows("https://example/sun-serum.html", parsed)
    assert row["original_price"] == 19.0
    assert row["sale_price"] == 9.7
    assert row["cart_price"] == 9.7
    assert row["gtin"] == "8801234567890"
    assert row["cart_discount"] is None


def test_text_barcode_scan_ignores_unlabelled_numbers():
    soup_html = "<html><body><p>Order 8801234567890 today. Product ID 6000.</p></body></html>"
    parsed = scrape.parse_product_page(soup_html)
    assert parsed["gtin"] is None


@pytest.mark.parametrize("pct,expected", [(0, 20.0), (20, 16.0)])
def test_cart_price_discount(pct, expected):
    parsed = {"name": "X", "sku": "S", "gtin": None, "gtin_source": None, "product_id": "1",
              "variants": [{"variation": "Default", "sku": "S", "original_price": 25.0, "sale_price": 20.0}]}
    [row] = scrape.build_rows("u", parsed, discount_pct=pct, discount_code="SVHAWS" if pct else None)
    assert row["cart_price"] == expected
