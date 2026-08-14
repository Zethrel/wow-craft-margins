"""Blizzard's recipe endpoint gives every crafting-quality rank the same
crafted_item id. Verify we collapse those to the cheapest rather than
reporting one opportunity several times."""
import json, sys
from wowcraft import build_price_index, compute_margins

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

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
