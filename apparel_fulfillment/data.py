"""A source-linked apparel subset and separately identified merchant simulations."""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re

from commerce_lab.catalog import Catalog, DATASET

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/apparel_fulfillment_v1.json"
SIZES = {"X-Small": "XS", "Small": "S", "Medium": "M", "Large": "L", "X-Large": "XL", "XX-Large": "XXL"}
STYLES = {
    "LAB-GT-CREW": ("Goodthreads", "Goodthreads Men's Short-Sleeve Crewneck Cotton T-Shirt,"),
    "LAB-GT-VNECK": ("Goodthreads", 'Amazon Brand - Goodthreads Men\'s Slim-Fit "The Perfect V-Neck T-Shirt" Short-Sleeve Cotton,'),
    "LAB-AE-CREW-2PACK": ("Amazon Essentials", "Amazon Essentials Men's 2-Pack Regular-Fit Short-Sleeve Crewneck T-Shirt,"),
}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def load_world(path: Path = DATA) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build(path: Path = DATA) -> dict:
    if path.exists():
        raise FileExistsError("An apparel snapshot already exists; create a separately versioned dataset to change it")
    catalog = Catalog()
    variants, skipped = {}, Counter()
    for style_id, (brand, prefix) in STYLES.items():
        with closing(catalog.connect()) as db:
            rows = db.execute("SELECT * FROM products WHERE locale='us' AND brand=? AND title LIKE ? ORDER BY id LIMIT 80",
                              (brand, prefix + "%")).fetchall()
        for row in rows:
            product = catalog.present(row)
            size_match = re.search(r", (X-Small|Small|Medium|Large|X-Large|XX-Large)$", product["title"])
            if not size_match or not product["color"].strip():
                skipped["size_or_color_unresolved"] += 1
                continue
            sku = product["id"]
            public = {key: product[key] for key in ("id", "title", "brand", "color", "description")}
            source = "esci:" + sku
            pack = "2-Pack" in product["title"]
            variants[sku] = {
                "sku": sku, "style_id": style_id, "category": "t_shirt", "audience": "men",
                "brand": product["brand"], "color": product["color"].strip().casefold(),
                "size": SIZES[size_match.group(1)], "title": product["title"],
                "pieces_per_catalog_unit": 2 if pack else 1,
                "weight_grams_per_catalog_unit": 400 if pack else 200,
                "unit_price_cents": round(product["price_usd"] * 100),
                "version": 1, "source_record": public, "source_record_sha256": digest(public),
                "provenance": {
                    "sku": {"kind": "public_asin_used_as_lab_sku", "evidence_id": source + ":id"},
                    "brand": {"kind": "public_metadata", "evidence_id": source + ":brand"},
                    "color": {"kind": "normalized_public_metadata", "evidence_id": source + ":color"},
                    "size": {"kind": "normalized_title", "evidence_id": source + ":title", "raw": size_match.group(1)},
                    "category": {"kind": "title_classification", "evidence_id": source + ":title"},
                    "style_id": {"kind": "research_grouping", "evidence_id": "lab-style:" + style_id},
                    "pieces_per_catalog_unit": {"kind": "explicit_title_pack_count" if pack else "merchant_simulation",
                                                "evidence_id": source + ":title" if pack else "sim-unit:" + sku},
                    "weight_grams_per_catalog_unit": {"kind": "merchant_simulation", "evidence_id": "sim-weight:" + sku},
                    "unit_price_cents": {"kind": "merchant_simulation", "evidence_id": "sim-price:" + sku},
                },
            }
    if not variants or any(not any(v["style_id"] == style for v in variants.values()) for style in STYLES):
        raise ValueError("Missing source-linked apparel styles")
    stock = {sku: {"available_catalog_units": int(hashlib.sha256(sku.encode()).hexdigest()[:8], 16) % 81,
                   "version": 1, "evidence_id": "sim-stock:CN-SZ:" + sku} for sku in variants}
    for sku, count in (("us:B06XWMKR2F", 8), ("us:B06XWMJ9XF", 45), ("us:B06XWPQT19", 30)):
        if sku in stock:
            stock[sku]["available_catalog_units"] = count
    rules = {brand: {"allowed_sales_regions": ["DE", "GB", "US"], "wholesale_minimum_pieces_per_sku": 10,
                     "version": 1, "evidence_id": "sim-brand-policy:" + brand}
             for brand, _ in STYLES.values()}
    world = {"dataset_id": "apparel-fulfillment-v1-20260908", "catalog_dataset": DATASET,
             "catalog_source_url": "https://github.com/amazon-science/esci-data",
             "notice": "Public product text plus explicitly simulated merchant operations. No live stock, manufacturer SKU relationship or real brand distribution rights are asserted.",
             "warehouse": "CN-SZ", "variants": variants, "stock": stock, "brand_rules": rules,
             "styles": {key: {"brand": value[0], "title_prefix": value[1], "relationship": "research grouping, not manufacturer-confirmed parent ASIN"}
                        for key, value in STYLES.items()},
             "selection": {"rule": "Up to 80 ASIN-sorted records for each of three exact title-prefix families; retain explicit supported size suffix and nonempty color",
                           "skipped": dict(skipped), "rows": len(variants)},
             "simulation_fields": ["stock", "brand_rules", "style relationships", "single-piece unit assumptions where no pack count is explicit", "weight", "price"]}
    path.write_text(json.dumps(world, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"path": str(path), "sha256": digest(world), "variants": len(variants),
            "styles": dict(Counter(v["style_id"] for v in variants.values())), "skipped": dict(skipped)}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
