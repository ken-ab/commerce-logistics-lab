from copy import deepcopy
import unittest

from apparel_fulfillment.orders import OrderError, alternatives, check_order, substitution


def fixture():
    variants = {}
    for sku, color, size, brand, pack in (("A", "black", "M", "BrandA", 1),
                                         ("B", "black", "M", "BrandB", 1),
                                         ("C", "white", "L", "BrandA", 2)):
        variants[sku] = {"sku": sku, "title": sku, "style_id": "STYLE-" + sku, "category": "t_shirt",
                         "audience": "men", "brand": brand, "color": color, "size": size, "version": 1,
                         "pieces_per_catalog_unit": pack, "weight_grams_per_catalog_unit": 200 * pack,
                         "unit_price_cents": 1000, "provenance": {key: {"evidence_id": sku + ":" + key}
                         for key in ("brand", "color", "size", "category", "pieces_per_catalog_unit")}}
    return {"dataset_id": "isolated-test-fixture", "warehouse": "CN-SZ", "variants": variants,
            "stock": {sku: {"available_catalog_units": 20, "version": 1, "evidence_id": "stock:" + sku} for sku in variants},
            "brand_rules": {brand: {"allowed_sales_regions": ["DE"], "wholesale_minimum_pieces_per_sku": 10,
                                   "version": 1, "evidence_id": "rule:" + brand} for brand in ("BrandA", "BrandB")}}


def request(**line_changes):
    return {"sales_region": "DE", "wholesale": True, "needs_shipping": False,
            "lines": [{"line_id": "one", "quantity": 20, "unit": "piece", "brand": "BrandA",
                       "category": "t_shirt", "size": "M", "color": "black", **line_changes}]}


class ApparelOrderTests(unittest.TestCase):
    def test_exact_order_has_source_versions_and_does_not_deduct_stock(self):
        world = fixture(); before = deepcopy(world)
        result = check_order(request(), [{"line_id": "one", "sku": "A"}], world)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["quantities_catalog_units"], {"A": 20})
        self.assertEqual(result["source_versions"]["A"]["stock_version"], 1)
        self.assertEqual(world, before)

    def test_duplicate_sku_lines_aggregate_inventory_and_minimum(self):
        req = request(quantity=5)
        req["lines"].append({**req["lines"][0], "line_id": "two"})
        picks = [{"line_id": line["line_id"], "sku": "A"} for line in req["lines"]]
        self.assertEqual(check_order(req, picks, fixture())["status"], "ready")
        req["lines"][1]["quantity"] = 16
        result = check_order(req, picks, fixture())
        self.assertEqual(result["status"], "unfulfillable")
        self.assertIn("insufficient_stock", [i["code"] for i in result["issues"]])

    def test_pack_conversion_is_exact_and_partial_pack_is_not_silent(self):
        req = request(size="L", color="white")
        pick = [{"line_id": "one", "sku": "C"}]
        self.assertEqual(check_order(req, pick, fixture())["quantities_catalog_units"], {"C": 10})
        req["lines"][0]["quantity"] = 21
        result = check_order(req, pick, fixture())
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["issues"][0]["code"], "cannot_split_catalog_pack")

    def test_changed_requirement_needs_specific_confirmation(self):
        req = request(); world = fixture(); pick = [{"line_id": "one", "sku": "B"}]
        result = check_order(req, pick, world)
        self.assertEqual(result["status"], "needs_clarification")
        change = result["substitution_proposals"][0]
        self.assertEqual(change["differences"], [{"field": "brand", "requested": "BrandA", "candidate": "BrandB"}])
        approved = frozenset({change["approval_id"]})
        self.assertEqual(check_order(req, pick, world, approved_ids=approved)["status"], "ready")
        changed = request(quantity=19)
        self.assertEqual(check_order(changed, pick, world, approved_ids=approved)["status"], "needs_clarification")
        world["variants"]["B"]["version"] += 1
        self.assertEqual(check_order(req, pick, world, approved_ids=approved)["status"], "needs_clarification")

    def test_substitution_cannot_waive_region_or_stock_rules(self):
        req = request(); req["sales_region"] = "JP"; world = fixture()
        change = substitution(req, req["lines"][0], world["variants"]["B"])
        result = check_order(req, [{"line_id": "one", "sku": "B"}], world,
                             approved_ids=frozenset({change["approval_id"]}))
        self.assertEqual(result["status"], "unfulfillable")
        self.assertIn("sales_region_not_allowed", [i["code"] for i in result["issues"]])

    def test_alternatives_exclude_unavailable_and_disclose_differences(self):
        world = fixture(); world["stock"]["A"]["available_catalog_units"] = 8
        rows = alternatives(request(), "one", world)
        self.assertNotIn("A", [r["sku"] for r in rows])
        b = next(r for r in rows if r["sku"] == "B")
        self.assertTrue(b["requires_confirmation"])
        self.assertEqual(b["differences"][0]["field"], "brand")

    def test_missing_variant_or_unit_evidence_does_not_pass(self):
        for field in ("size", "pieces_per_catalog_unit"):
            world = fixture(); del world["variants"]["A"]["provenance"][field]
            self.assertEqual(check_order(request(), [{"line_id": "one", "sku": "A"}], world)["status"], "needs_clarification")
        self.assertEqual(check_order(request(unit=None), [{"line_id": "one", "sku": "A"}], fixture())["status"], "needs_clarification")

    def test_incomplete_selections_and_explicit_wrong_size_do_not_pass(self):
        self.assertEqual(check_order(request(), [], fixture())["status"], "needs_clarification")
        result = check_order(request(size="L"), [{"line_id": "one", "sku": "A"}], fixture())
        self.assertEqual(result["status"], "needs_clarification")

    def test_invalid_quantities_or_duplicate_line_ids_rejected(self):
        for number in (0, -1, True, 1.5, "20"):
            with self.assertRaises(OrderError):
                check_order(request(quantity=number), [], fixture())
        req = request(); req["lines"].append(deepcopy(req["lines"][0]))
        with self.assertRaises(OrderError):
            check_order(req, [], fixture())


if __name__ == "__main__":
    unittest.main()
