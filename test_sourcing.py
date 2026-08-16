"""Reagents priced as the cheaper of buying or making them.

The reason this needs care rather than just recursion: Blizzard's reagent
lists are incomplete on modern tiers, so a sub-craft's cost can be understated.
Substituting an understated cost makes the parent look cheaper and its margin
look better - errors compound in the flattering direction, which is the one
that loses money. Hence the refusal to source through any recipe whose own
cost is only a floor.
"""
import json
import sys

import wowcraft as W

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


def rec(rid, name, out, reagents, qty=1, slots=0):
    return {"id": rid, "name": name, "profession_name": "Alchemy",
            "skill_tier_name": "T", "crafted_item_id": out,
            "crafted_qty_min": qty, "crafted_qty_max": qty,
            "uses_slots": slots, "crafted_source": W.CRAFTED_API,
            "reagents_json": json.dumps(reagents)}


def market(**per_unit):
    """One deep listing per item at the given unit price."""
    return W.build_price_index(
        [{"item": {"id": int(i)}, "quantity": 100000, "unit_price": p}
         for i, p in per_unit.items()], "commodity")


# ---- 1. craft the intermediate when it is cheaper --------------------
# Bar (item 2) sells for 1000 but is made from 2 ore (item 1) at 100 = 200.
prices = market(**{"1": 100, "2": 1000, "9": 100000})
bar = rec(10, "Smelt Bar", 2, [{"id": 1, "quantity": 2}])
plate = rec(11, "Plate", 9, [{"id": 2, "quantity": 5}])

src = W.ReagentSourcer([bar, plate], prices, {})
cost, how = src.obtain(2, 5)
must("smelting beats buying", cost == 1000.0)          # 5 bars = 10 ore = 1000
must("and it says which craft", how == "Smelt Bar")
must("buying alone would have cost 5000",
     prices[2].buy_cost(5) == 5000.0)

res, _ = W.compute_margins([bar, plate], prices, {1: "Ore", 2: "Bar", 9: "Plate"},
                           batch=1, min_listings=1)
plate_row = [r for r in res if r.recipe_id == 11][0]
must("the parent's cost uses the smelted price", plate_row.cost == 1000.0)
must("the saving is reported", plate_row.crafted_savings == 4000.0)
line = [b for b in plate_row.reagent_breakdown if b["id"] == 2][0]
must("the bill names the sub-craft", line.get("made_by") == "Smelt Bar")
must("and what it saved", line.get("saved") == 4000.0)

# ---- 2. buying wins when it is cheaper -------------------------------
cheap = market(**{"1": 900, "2": 1000, "9": 100000})
src2 = W.ReagentSourcer([bar, plate], cheap, {})
cost2, how2 = src2.obtain(2, 5)
must("buying wins when the ore is dear", cost2 == 5000.0)
must("and no craft is claimed", how2 == "buy")
res2, _ = W.compute_margins([bar, plate], cheap, {}, batch=1, min_listings=1)
plate2 = [r for r in res2 if r.recipe_id == 11][0]
must("no saving is reported when buying wins", plate2.crafted_savings == 0.0)

# ---- 3. a floor cost must not propagate ------------------------------
# Same numbers, but the sub-craft has optional slots, so its cost is a floor.
floor_bar = rec(10, "Smelt Bar", 2, [{"id": 1, "quantity": 2}], slots=1)
src3 = W.ReagentSourcer([floor_bar, plate], prices, {})
cost3, how3 = src3.obtain(2, 5)
must("a sub-craft whose cost is a floor is refused", cost3 == 5000.0)
must("so the reagent is simply bought", how3 == "buy")

# ---- 4. cycles and depth ---------------------------------------------
# Transmute A->B and B->A. Naive recursion never returns.
a2b = rec(20, "A to B", 2, [{"id": 1, "quantity": 1}])
b2a = rec(21, "B to A", 1, [{"id": 2, "quantity": 1}])
cyc = market(**{"1": 500, "2": 500})
src4 = W.ReagentSourcer([a2b, b2a], cyc, {})
cost4, _ = src4.obtain(1, 10)
must("a cycle terminates instead of hanging", cost4 is not None)
must("and prices sanely", 0 < cost4 <= 5000.0)

# A chain longer than the depth limit still costs, it just stops descending.
chain = [rec(30 + i, f"step{i}", i + 2, [{"id": i + 1, "quantity": 1}])
         for i in range(6)]
deep = market(**{str(i): 1000 for i in range(1, 9)})
src5 = W.ReagentSourcer(chain, deep, {})
deep_cost, _ = src5.obtain(8, 1)
must("a chain deeper than the limit still returns", deep_cost is not None)
must(f"the depth limit is {W.MAX_CRAFT_DEPTH}", W.MAX_CRAFT_DEPTH >= 2)

# ---- 5. quantities round up, you cannot half-craft --------------------
# One craft makes 3 bars; needing 4 means crafting twice = 4 ore.
three = rec(40, "Smelt Three", 2, [{"id": 1, "quantity": 2}], qty=3)
src6 = W.ReagentSourcer([three], market(**{"1": 100, "2": 99999}), {})
c6, _ = src6.obtain(2, 4)
must("needing four of a three-per-craft means crafting twice", c6 == 400.0)

# ---- 6. it can be turned off -----------------------------------------
res3, _ = W.compute_margins([bar, plate], prices, {}, batch=1, min_listings=1,
                            source_reagents=False)
plate3 = [r for r in res3 if r.recipe_id == 11][0]
must("source_reagents=False prices reagents at market", plate3.cost == 5000.0)
must("and claims no savings", plate3.crafted_savings == 0.0)

# ---- 7. the page says the cost assumes crafting ----------------------
html = W.render_dashboard(res, {"realm_slug": "r", "region": "eu"},
                          1700000000, {}, {}, 50, 1, 1)
must("the row is badged", "sourced by crafting" in html)
must("the badge warns it needs the profession",
     "whether you have the professions" in html)
# The largest savings this finds are all transmutes, which are exactly the
# crafts limited to one a day. A saving resting on twenty of them is a
# twenty-day plan, and the badge has to say so.
must("the badge warns about cooldowns", "cooldown" in html)
must("the bill names the craft", "made via Smelt Bar" in html)

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    sys.exit(1)
print("ALL PASS")
