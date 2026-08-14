"""Hand-checked tests for the pricing and margin maths."""
import json, sys
from wowcraft import build_price_index, compute_margins, AH_CUT, GOLD

fails = []
def check(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}: got {got!r} want {want!r}")
    if not ok: fails.append(name)

# ---- 1. supply ladder / buy_cost -------------------------------------
# 10 @ 100c, 10 @ 200c, 10 @ 500c  -> total 30 units
auctions = [
    {"item": {"id": 1}, "quantity": 10, "unit_price": 500},
    {"item": {"id": 1}, "quantity": 10, "unit_price": 100},
    {"item": {"id": 1}, "quantity": 10, "unit_price": 200},
]
px = build_price_index(auctions, "commodity")[1]
check("min unit price", px.min_unit_price, 100.0)
check("total quantity", px.total_quantity, 30)
# buying 5 units: all from the 100c tier
check("buy_cost(5)", px.buy_cost(5), 500.0)
# buying 15: 10@100 + 5@200 = 1000 + 1000 = 2000
check("buy_cost(15)", px.buy_cost(15), 2000.0)
# buying 25: 1000 + 2000 + 5*500 = 5500
check("buy_cost(25)", px.buy_cost(25), 5500.0)
# buying 31: more than is listed -> None
check("buy_cost(31) is None", px.buy_cost(31) is None, True)
# sell price = qty-weighted 15th percentile. target = 30*0.15 = 4.5,
# reached inside the first (cheapest) tier -> 100
check("sell percentile price", px.sell_unit_price, 100.0)

# a single 1-copper troll listing must NOT define the sell price
troll = auctions + [{"item": {"id": 1}, "quantity": 1, "unit_price": 1}]
px2 = build_price_index(troll, "commodity")[1]
check("troll listing ignored for sell price", px2.sell_unit_price, 100.0)
check("troll listing shows in min", px2.min_unit_price, 1.0)

# bid-only auctions are excluded (not reliably purchasable)
bidonly = build_price_index(
    [{"item": {"id": 7}, "quantity": 1, "bid": 50}], "realm")
check("bid-only excluded", 7 in bidonly, False)

# realm listings: buyout is per stack, must divide by quantity
stack = build_price_index(
    [{"item": {"id": 8}, "quantity": 4, "buyout": 800}], "realm")[8]
check("stack buyout -> unit price", stack.sell_unit_price, 200.0)

# ---- 2. margin arithmetic --------------------------------------------
# Recipe: 2x item 1 -> 1x item 2.  batch = 10.
# reagent need = 20 units of item 1 = 1000 + 2000 + 0 ... let's compute:
#   10@100 = 1000, 10@200 = 2000  -> 20 units cost 3000c
# output item 2: 100 units @ 5000c each -> sell price 5000
#   revenue = 5000 * (1*10) * 0.95 = 47500
#   margin  = 47500 - 3000 = 44500 ; pct = 44500/3000*100 = 1483.33%
auctions2 = auctions + [
    {"item": {"id": 2}, "quantity": 100, "unit_price": 5000}]
prices = build_price_index(auctions2, "commodity")
recipes = [{
    "id": 99, "name": "Test", "profession_name": "Testing",
    "skill_tier_name": "T", "crafted_item_id": 2,
    "crafted_qty_min": 1, "crafted_qty_max": 1,
    "reagents_json": json.dumps([{"id": 1, "quantity": 2}]),
}]
# min_listings=1 throughout this section: these fixtures exist to check the
# arithmetic, not the liquidity gate, which section 5 covers on its own.
res, skipped = compute_margins(recipes, prices, {1: "Reagent", 2: "Output"},
                               batch=10, min_listings=1)
check("one result", len(res), 1)
r = res[0]
check("cost", r.cost, 3000.0)
check("revenue", r.revenue, 5000.0 * 10 * (1 - AH_CUT))
check("margin", r.margin, 47500.0 - 3000.0)
check("margin_pct", round(r.margin_pct, 2), 1483.33)
check("craftable units", r.craftable_units, 10)
check("reagent breakdown qty", r.reagent_breakdown[0]["qty"], 20)
check("reagent breakdown total", r.reagent_breakdown[0]["total"], 3000.0)

# ---- 3. skip conditions ----------------------------------------------
# batch 20 needs 40 units of item 1 but only 30 are listed -> skipped
res2, sk2 = compute_margins(recipes, prices, {}, batch=20, min_listings=1)
check("insufficient supply skipped", len(res2), 0)
check("skip reason recorded", sk2["no_reagent_price"], 1)

# unlisted output -> skipped
recipes3 = [dict(recipes[0], crafted_item_id=4242)]
res3, sk3 = compute_margins(recipes3, prices, {}, batch=1, min_listings=1)
check("unlisted output skipped", sk3["no_output_price"], 1)

# ---- 4. variable crafted quantity averages ---------------------------
recipes4 = [dict(recipes[0], crafted_qty_min=2, crafted_qty_max=4)]
res4, _ = compute_margins(recipes4, prices, {}, batch=10, min_listings=1)
# avg 3 per craft * 10 crafts = 30 units out
check("variable qty averaged", res4[0].craftable_units, 30)

# ---- 5. thin markets are not market prices ---------------------------
# One listing means one person's ask sets the price and the 15th percentile
# has nothing to be a percentile of.
thin = [{"item": {"id": 7}, "quantity": 1, "unit_price": 5_000_000}]
deep = [{"item": {"id": 8}, "quantity": 1, "unit_price": 5_000_000}
        for _ in range(4)]
px_thin = build_price_index(thin + deep + auctions, "commodity")
r_thin = [dict(recipes[0], id=90, crafted_item_id=7),
          dict(recipes[0], id=91, crafted_item_id=8)]
res5, sk5 = compute_margins(r_thin, px_thin, {}, batch=1, min_listings=3)
check("thin output skipped", sk5["thin_market"], 1)
check("deep output kept", len(res5), 1)
check("the kept one is the deep market", res5[0].crafted_item_id, 8)
res6, sk6 = compute_margins(r_thin, px_thin, {}, batch=1, min_listings=1)
check("min_listings=1 prices everything", len(res6), 2)
check("nothing called thin at min_listings=1", sk6["thin_market"], 0)

# The floor is opt-in: by default a one-listing output is still priced, and it
# is the listing count on the dashboard that tells you to be careful.
res7, sk7 = compute_margins(r_thin, px_thin, {}, batch=1)
check("liquidity floor is off by default", len(res7), 2)
check("no thin skips by default", sk7["thin_market"], 0)

# ---- 6. costing from the client's reagent slots ----------------------
# The API under-reports required reagents on modern recipes, so where the
# client has told us the slots we cost every one of them: required slots
# because they are mandatory, optional ones because assuming they stay empty
# is what made these margins fictional.
from wowcraft import cost_from_slots

slot_auctions = [
    {"item": {"id": 11}, "quantity": 100, "unit_price": 1000},   # cheap fill
    {"item": {"id": 12}, "quantity": 100, "unit_price": 9000},   # dear fill
    {"item": {"id": 13}, "quantity": 100, "unit_price": 2000},   # optional
]
sp = build_price_index(slot_auctions, "commodity")
slots = [
    {"type": 1, "required": True, "quantity": 2, "items": [12, 11]},
    {"type": 2, "required": False, "quantity": 1, "items": [13]},
]
cost, bill, fills, missing, to_buy = cost_from_slots(slots, sp, {}, batch=1)
check("slots price without error", missing, False)
# cheapest legal fill for the required slot is 2x1000, not 2x9000
check("required slot takes the cheapest legal item", cost, 2000.0 + 2000.0)
check("optional slot filled too", fills, 1)
check("bill lists both slots", len(bill), 2)
check("optional marked in the bill", bill[1]["optional"], True)
check("required not marked optional", bill[0]["optional"], False)

# Batch scales every slot.
cost10, _b, _f, _m, _t = cost_from_slots(slots, sp, {}, batch=10)
check("slot cost scales with batch", cost10, 20000.0 + 20000.0)

# An unpriceable REQUIRED slot kills the recipe; an unpriceable optional does
# not, it is simply left out of the bill.
dead_req = [{"type": 1, "required": True, "quantity": 1, "items": [999]}]
_c, _b, _f, miss, _t = cost_from_slots(dead_req, sp, {}, batch=1)
check("unpriceable required slot skips the recipe", miss, True)
dead_opt = [{"type": 1, "required": True, "quantity": 1, "items": [11]},
            {"type": 2, "required": False, "quantity": 1, "items": [999]}]
c2, b2, f2, miss2, _t2 = cost_from_slots(dead_opt, sp, {}, batch=1)
check("unpriceable optional slot is skipped, not fatal", miss2, False)
check("unpriceable optional adds nothing", c2, 1000.0)
check("unpriceable optional is not counted as filled", f2, 0)

# A recipe carrying slots_json is costed from it instead of the API reagents,
# and counts as fully costed even though it has optional slots.
slot_recipe = [{
    "id": 77, "name": "Slotted", "profession_name": "Testing",
    "skill_tier_name": "T", "crafted_item_id": 14,
    "crafted_qty_min": 1, "crafted_qty_max": 1,
    "reagents_json": json.dumps([{"id": 11, "quantity": 1}]),
    "uses_slots": 1, "crafted_source": "client",
    "slots_json": json.dumps(slots),
}]
sp2 = build_price_index(slot_auctions + [
    {"item": {"id": 14}, "quantity": 50, "unit_price": 500000}], "commodity")
res_s, _sk = compute_margins(slot_recipe, sp2, {}, batch=1, min_listings=1)
check("slotted recipe priced", len(res_s), 1)
check("cost came from the slots, not the API reagents", res_s[0].cost, 4000.0)
check("slotted recipe counts as fully costed", res_s[0].cost_complete, True)
check("optional fills reported on the result", res_s[0].optionals_filled, 1)

# Owning the reagents does not change the craft's cost, it changes what is
# left to buy - two different questions, both worth answering.
own_none = cost_from_slots(slots, sp, {}, batch=1)
must_eq = lambda a, b: a == b
check("with nothing owned, buying costs the full amount",
      own_none[4], own_none[0])
own_some = cost_from_slots(slots, sp, {}, batch=1, owned={11: 2})
check("owning a required reagent lowers what is left to buy",
      own_some[4], 2000.0)
check("owning it does not change the craft's cost", own_some[0], own_none[0])
own_all = cost_from_slots(slots, sp, {}, batch=1, owned={11: 99, 13: 99})
check("owning everything leaves nothing to buy", own_all[4], 0.0)
own_part = cost_from_slots(slots, sp, {}, batch=10, owned={11: 10})
# needs 20 of item 11; half owned, so half its cost remains
check("a partial stack is charged pro rata", round(own_part[4]), 30000)

# ---- 7. CLI scope helpers --------------------------------------------
from wowcraft import _split_list, expansion_of

check("repeated flags flattened", _split_list(["Midnight", "Classic"]),
      ["Midnight", "Classic"])
check("comma-separated flag split", _split_list(["Khaz Algar,Dragon Isles"]),
      ["Khaz Algar", "Dragon Isles"])
check("mixed forms and stray spaces", _split_list(["a, b", "c"]), ["a", "b", "c"])
check("empty pieces dropped", _split_list(["a,,", " "]), ["a"])

# The expansion dropdown is built by stripping the profession off the tier name.
check("expansion label", expansion_of("Midnight Alchemy", "Alchemy"), "Midnight")
check("multi-word expansion",
      expansion_of("Dragon Isles Blacksmithing", "Blacksmithing"), "Dragon Isles")
check("faction-split tier label",
      expansion_of("Kul Tiran Alchemy / Zandalari Alchemy", "Alchemy"),
      "Kul Tiran / Zandalari")
check("unknown profession leaves the tier intact",
      expansion_of("Classic Mining", ""), "Classic Mining")

print()
print(f"{'ALL PASS' if not fails else str(len(fails)) + ' FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
