"""Blizzard's recipe endpoint gives every crafting-quality rank the same
crafted_item id. Verify we collapse those to the cheapest rather than
reporting one opportunity several times."""
import json, sys
from wowcraft import (build_price_index, compute_margins, render_dashboard,
                      VARIANT_SPREAD, VARIANT_MIN_LISTINGS)

fails = []
def must(l, c):
    print(f"{'PASS' if c else 'FAIL'}  {l}")
    if not c: fails.append(l)

auctions = [
    {"item": {"id": 1}, "quantity": 1000, "unit_price": 100},   # cheap reagent
    {"item": {"id": 2}, "quantity": 1000, "unit_price": 300},   # pricier reagent
    {"item": {"id": 9}, "quantity": 1000, "unit_price": 10000}, # the output
]
prices = build_price_index(auctions, "commodity")

def rec(rid, reagent_id, qty):
    return {"id": rid, "name": f"Rank {rid}", "profession_name": "Alchemy",
            "skill_tier_name": "T", "crafted_item_id": 9,
            "crafted_qty_min": 1, "crafted_qty_max": 1,
            "reagents_json": json.dumps([{"id": reagent_id, "quantity": qty}])}

# Three "ranks" of the same craft, all pointing at item 9.
recipes = [rec(1, 1, 5), rec(2, 2, 5), rec(3, 2, 9)]
# min_listings=1: these fixtures list each item once because the subject here
# is rank collapsing, not whether the market is deep enough to trust.
res, sk = compute_margins(recipes, prices, {1: "A", 2: "B", 9: "Out"}, batch=1,
                          min_listings=1)

must("collapsed to one row", len(res) == 1)
must("kept the cheapest recipe", res[0].recipe_id == 1)
must("cost is the cheapest", res[0].cost == 500.0)
must("variant count reported", res[0].variant_count == 3)
must("collapse counted in skipped", sk.get("quality_variants_collapsed") == 2)

# Distinct outputs must NOT be collapsed.
auctions.append({"item": {"id": 10}, "quantity": 100, "unit_price": 9000})
prices2 = build_price_index(auctions, "commodity")
r2 = dict(rec(4, 1, 5), crafted_item_id=10)
res2, sk2 = compute_margins([rec(1, 1, 5), r2], prices2, {}, batch=1,
                            min_listings=1)
must("distinct outputs kept separate", len(res2) == 2)
must("no false collapse", "quality_variants_collapsed" not in sk2)
must("single recipes report variant_count 1",
     all(r.variant_count == 1 for r in res2))

# ---- bonus-list variants: a different ambiguity ----------------------
# Above is several RECIPES sharing one output id. This is one output id
# listed at several ITEM LEVELS, which the recipe endpoint also cannot tell
# apart - so they are priced together, at the cheap end, and the margin shown
# is a floor. Commodities never carry bonus lists, so reagents are unaffected.
gear = [
    {"item": {"id": 1}, "quantity": 1000, "unit_price": 100},
    # The same output at three item levels: 10k, 30k, 90k. Two listings each,
    # because one listing is not a market - see below.
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [2]}, "quantity": 5, "buyout": 150000},
    {"item": {"id": 9, "bonus_lists": [2]}, "quantity": 5, "buyout": 150000},
    {"item": {"id": 9, "bonus_lists": [3]}, "quantity": 5, "buyout": 450000},
    {"item": {"id": 9, "bonus_lists": [3]}, "quantity": 5, "buyout": 450000},
]
gp = build_price_index(gear, "realm")
must("variants are counted", gp[9].variant_count == 3)
must("the dearest traded variant is recorded", gp[9].variant_high == 90000.0)
must("the pooled price still comes from the whole ladder",
     gp[9].sell_unit_price == 10000.0)
must("a reagent with no bonus lists reports one variant",
     gp[1].variant_count == 1)
# Sold by one seller, so there is no second opinion on the price and nothing
# to quote. Harmless: the badge requires variant_count > 1 before it looks.
must("a single listing is not treated as a traded price",
     gp[1].variant_high == 0.0)

res3, _ = compute_margins([rec(1, 1, 5)], gp, {1: "A", 9: "Out"}, batch=1,
                          min_listings=1)
must("the result carries the variant count", res3[0].output_variant_count == 3)
must("the result carries the dearest variant",
     res3[0].output_variant_high == 90000.0)

# Every variant at the same price is the common case and must not be flagged,
# or the badge becomes noise nobody reads.
flat = [
    {"item": {"id": 1}, "quantity": 1000, "unit_price": 100},
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [2]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [2]}, "quantity": 5, "buyout": 50000},
]
fp = build_price_index(flat, "realm")
must("flat variants are still counted", fp[9].variant_count == 2)
must("but carry no spread to report",
     fp[9].variant_high == fp[9].sell_unit_price)

# One listing is not a market. The first version of this badge quoted a lone
# 2.6M listing on old PvP gear as though a craft were worth 2.48M, which is
# exactly the fiction the project keeps out of its headline figures.
lone = [
    {"item": {"id": 1}, "quantity": 1000, "unit_price": 100},
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    {"item": {"id": 9, "bonus_lists": [1]}, "quantity": 5, "buyout": 50000},
    # A single fishing listing at 50x the going rate.
    {"item": {"id": 9, "bonus_lists": [2]}, "quantity": 1, "buyout": 5000000},
]
lp = build_price_index(lone, "realm")
must("the lone variant is still counted", lp[9].variant_count == 2)
must("but it is not quoted as a price", lp[9].variant_high == 10000.0)
res5, _ = compute_margins([rec(1, 1, 5)], lp, {1: "A", 9: "Out"}, batch=1,
                          min_listings=1)
html3 = render_dashboard(res5, {"realm_slug": "r", "region": "eu"},
                         1700000000, {}, {}, 50, 1, 1)
must("a single fishing listing does not earn a badge",
     "revenue is a floor" not in html3)

# The badge itself, through the renderer.
cfg = {"realm_slug": "r", "region": "eu"}
html = render_dashboard(res3, cfg, 1700000000, {}, {}, 50, 1, 1)
must("a wide spread is badged", "revenue is a floor" in html)
must("the badge says what the dearest variant would pay",
     "instead" in html and "bonus-list variants" in html)

res4, _ = compute_margins([rec(1, 1, 5)], fp, {1: "A", 9: "Out"}, batch=1,
                          min_listings=1)
html2 = render_dashboard(res4, cfg, 1700000000, {}, {}, 50, 1, 1)
must("flat variants are not badged", "revenue is a floor" not in html2)
must(f"the threshold is a spread of {VARIANT_SPREAD}x", VARIANT_SPREAD > 1.0)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
