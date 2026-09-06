"""Gold per day, and how many to make.

A margin says what one craft pays if it sells. On its own it ranks a +800%
craft that shifts a unit a fortnight above a +12% craft that shifts forty a
day, which is backwards - the second one is the business. So the table ranks
on margin per unit x what you could actually sell of it in a day, and turns
that into a quantity to craft.

Everything here is a projection built on two arguable assumptions - a sale
rate that counts cancellations as sales, and a share of the market modelled as
one more seller among those listed - so what these tests hold in place is
mostly the honesty: an unmeasured item gets no forecast rather than a zero, a
loss gets no restock target however fast it moves, and the numbers on the page
are the ones the stated arithmetic produces.
"""
import json
import os
import time

import wowcraft as W

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


def close(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


def result(**kw):
    base = dict(recipe_id=1, recipe_name="R", profession="Alchemy",
                skill_tier="Midnight Alchemy", crafted_item_id=9,
                crafted_item_name="Out", crafted_qty=1.0, cost=1000.0,
                revenue=2000.0, margin=1000.0, margin_pct=100.0,
                craftable_units=10, reagent_breakdown=[], output_supply=1000,
                output_listings=9)
    base.update(kw)
    return W.MarginResult(**base)


# ---- 1. your share of a market ---------------------------------------
# One more seller among those already posted. Ten listings gives you a tenth,
# one listing gives you half, and a crowded market is punished without any
# tuning.
r = result(output_listings=9)
W.project_gold_per_day(r)
must("nine listings puts you at a tenth", close(r.share, 0.1))

r = result(output_listings=1)
W.project_gold_per_day(r)
must("a lone competitor puts you at a half", close(r.share, 0.5))

r = result(output_listings=0)
W.project_gold_per_day(r)
must("an empty market does not divide by zero", close(r.share, 1.0))

r = result(output_listings=9)
W.project_gold_per_day(r, share=0.25)
must("an override wins", close(r.share, 0.25))
r = result(output_listings=9)
W.project_gold_per_day(r, share=7.0)
must("an override cannot promise more than the whole market",
     close(r.share, 1.0))

# ---- 2. the sale rate is capped --------------------------------------
# The raw signal counts a cancelled auction as a sale. On thinly-listed items
# holding big stacks that churn is most of it - live data had Leylight Shard
# claiming 2.1M units a day against 200k listed. Capping at one full turnover
# of standing supply a day leaves the honest 93% of items untouched.
r = result(output_supply=500, output_sold_per_day=100.0)
W.project_gold_per_day(r)
must("a believable rate passes through", close(r.demand_per_day, 100.0))
must("and is not flagged", r.demand_capped is False)

r = result(output_supply=500, output_sold_per_day=5000.0)
W.project_gold_per_day(r)
must("ten turnovers a day is capped at one", close(r.demand_per_day, 500.0))
must("and says so", r.demand_capped is True)

r = result(output_supply=500, output_sold_per_day=5000.0)
W.project_gold_per_day(r, cap_turnover=0)
must("the cap can be turned off", close(r.demand_per_day, 5000.0))

# ---- 3. gold per day is the stated arithmetic ------------------------
# margin per unit x units the market takes x your share of it.
r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0)
W.project_gold_per_day(r)
must("gold per day is margin/unit x demand x share",
     close(r.gold_per_day, (1000.0 / 10) * 40.0 * 0.1))
must("the tooltip shows the arithmetic rather than asserting it",
     "&times;" in W.gold_day_tip(r) and "of it =" in W.gold_day_tip(r))

# ---- 4. unmeasured is not zero ---------------------------------------
# A fresh database, or an item too quiet to have six hours of observation
# behind it, must not be handed a forecast of nothing - it has no forecast.
r = result(output_sold_per_day=None)
W.project_gold_per_day(r)
must("no sale rate means no gold per day", r.gold_per_day is None)
must("and no demand figure either", r.demand_per_day is None)
must("and nothing to craft", r.restock_units == 0)
must("it renders as a dash, not a zero", "&ndash;" in W.gold_day_str(None))
must("the tooltip says why", "no sale rate" in W.gold_day_tip(r).lower())

r = result(output_sold_per_day=0.0)
W.project_gold_per_day(r)
must("measured-at-zero does get a figure", r.gold_per_day == 0.0)

# ---- 5. what to craft ------------------------------------------------
# Cover days of your share, less what you already hold.
r = result(margin=1000.0, cost=500.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0)
W.project_gold_per_day(r, cover_days=3.0)
must("three days of four a day is twelve", r.restock_units == 12)
must("its capital is costed at the per-unit cost",
     close(r.restock_cost, 12 * (500.0 / 10)))

r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0, output_owned=5)
W.project_gold_per_day(r, cover_days=3.0)
must("stock on hand is deducted", r.restock_units == 7)
must("and the tooltip says it was",
     "in your bags or bank" in W.restock_tip(r, 3.0))

# What you have already crafted AND listed is the other half of the same
# mistake, and it is the one that shipped: a craft posted this morning was
# suggested again this afternoon because an auction is in neither your bags
# nor your bank.
r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0, output_posted=4)
W.project_gold_per_day(r, cover_days=3.0)
must("stock already on the auction house is deducted too",
     r.restock_units == 8)
must("and the tooltip says which is which",
     "already listed at the auction house" in W.restock_tip(r, 3.0))

r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0,
           output_owned=3, output_posted=4)
W.project_gold_per_day(r, cover_days=3.0)
must("both are deducted together", r.restock_units == 5)
must("and both are named", "bags or bank" in W.restock_tip(r, 3.0)
     and "auction house" in W.restock_tip(r, 3.0))

r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=10000, output_sold_per_day=40.0, output_owned=99)
W.project_gold_per_day(r, cover_days=3.0)
must("holding more than the target asks for nothing", r.restock_units == 0)

# A loss is a loss however fast it moves.
r = result(margin=-5000.0, craftable_units=10, output_listings=1,
           output_supply=10000, output_sold_per_day=400.0)
W.project_gold_per_day(r)
must("a loss-making craft gets no restock target", r.restock_units == 0)
must("but keeps its (negative) gold per day", r.gold_per_day < 0)
must("and the tooltip says why", "loses money" in W.restock_tip(r, 3.0))

# Slower than one a fortnight reads as nothing to make now, not as one.
r = result(margin=1000.0, craftable_units=10, output_listings=9,
           output_supply=100, output_sold_per_day=0.5)
W.project_gold_per_day(r, cover_days=3.0)
must("a trickle rounds to nothing to craft now", r.restock_units == 0)

# ---- 5b. reading your listings back ------------------------------------
# A listing lasts at most 48 hours, and the addon can only read them with the
# auction house open. An old reading is therefore ignored rather than trusted:
# deducting listings that have since sold or expired would suppress crafting
# you actually need to do, so this fails towards "make some".
import tempfile
import time as _time

st = W.Store(os.path.join(tempfile.mkdtemp(), "posted.sqlite3"))
now = int(_time.time())
st.save_posted({"Zethrel-ArgentDawn": {"items": {9: 4, 8: 1}, "seen_at": now}})
must("a fresh reading is used", st.posted() == {9: 4, 8: 1})
must("and its age is available to say so", st.posted_seen_at() == now)

st.save_posted({"Zethrel-ArgentDawn":
                {"items": {9: 4}, "seen_at": now - 50 * 3600}})
must("a reading older than an auction can live is ignored", st.posted() == {})
must("but the timestamp survives, so the page can say why",
     st.posted_seen_at() == now - 50 * 3600)

# An empty read is a fact - "I have nothing listed" - not an absence of news,
# so it must clear what was there rather than leaving stale rows behind.
st.save_posted({"Zethrel-ArgentDawn": {"items": {9: 4}, "seen_at": now}})
st.save_posted({"Zethrel-ArgentDawn": {"items": {}, "seen_at": now}})
must("an empty reading clears the previous one", st.posted() == {})

# One alt's trip to the auction house must not wipe another's listings.
st.save_posted({"Zethrel-ArgentDawn": {"items": {9: 2}, "seen_at": now}})
st.save_posted({"Alt-ArgentDawn": {"items": {9: 3}, "seen_at": now}})
must("characters are pooled, not overwritten", st.posted() == {9: 5})
st.close()

# ---- 6. the ranking ---------------------------------------------------
# The whole point: a modest margin that moves beats a huge one that does not.
prices = W.build_price_index(
    [{"item": {"id": 1}, "quantity": 100000, "unit_price": 10},
     {"item": {"id": 9}, "quantity": 5000, "unit_price": 1000},
     {"item": {"id": 8}, "quantity": 5000, "unit_price": 100000}],
    "commodity")


def recipe(rid, out):
    return {"id": rid, "name": f"R{rid}", "profession_name": "Alchemy",
            "skill_tier_name": "Midnight Alchemy", "crafted_item_id": out,
            "crafted_qty_min": 1, "crafted_qty_max": 1,
            "reagents_json": json.dumps([{"id": 1, "quantity": 1}])}


names = {1: "Reagent", 8: "Fantasy", 9: "Bread and butter"}
recipes = [recipe(1, 9), recipe(2, 8)]
# Item 8 is worth a hundred times more per craft and nobody buys it.
ranked, _ = W.compute_margins(recipes, prices, names, batch=1,
                              min_listings=1,
                              velocity={9: 200.0, 8: 0.0})
must("the earner outranks the fantasy", ranked[0].crafted_item_id == 9)
must("even though the fantasy has the bigger margin",
     ranked[1].margin > ranked[0].margin)

by_margin, _ = W.compute_margins(recipes, prices, names, batch=1,
                                 min_listings=1,
                                 velocity={9: 200.0, 8: 0.0}, rank="margin")
must("--rank margin restores the old ordering",
     by_margin[0].crafted_item_id == 8)

# Measured beats unmeasured, and both beat a cost we only know a floor for.
half_measured, _ = W.compute_margins(recipes, prices, names, batch=1,
                                     min_listings=1, velocity={9: 5.0})
must("a measured craft outranks an unmeasured one",
     half_measured[0].crafted_item_id == 9
     and half_measured[1].gold_per_day is None)

# ---- 7. the page --------------------------------------------------------
res, _ = W.compute_margins(recipes, prices, names, batch=1, min_listings=1,
                           velocity={9: 200.0, 8: 0.0})
cfg = {"realm_slug": "r", "region": "eu", "cover_days": 3}
html = W.render_dashboard(res, cfg, W.day_bucket(int(time.time())), {}, {},
                          50, 1, 1)
must("the page has a gold per day column", 'data-key="gpd"' in html)
must("the page has a restock column", 'data-key="restock"' in html)
must("rows carry the forecast for sorting", 'data-gpd="' in html)
must("rows carry the restock quantity", 'data-restock="' in html)
must("the chart follows the ranking",
     "by expected gold per day" in html)
must("the caption says what Craft means", "days of cover at your share" in html)
must("a two-listing market is badged as thin",
     "thin market" in html)
must("and the page offers a way to put those aside", 'id="liquid"' in html)
must("rows carry their listing count for that filter", 'data-listings="' in html)

must("the caveat calls them projections",
     "projections, not measurements" in html)
must("and dates the half that comes from the client",
     "as fresh as your last visit" in html)

# With nothing measured at all the page must still render, and must not
# pretend the chart is a forecast.
res_none, _ = W.compute_margins(recipes, prices, names, batch=1,
                                min_listings=1)
html2 = W.render_dashboard(res_none, cfg, W.day_bucket(int(time.time())), {},
                           {}, 50, 1, 1)
must("a fresh database still renders a chart", "crafts by margin" in html2)
must("and says the forecast is not available yet",
     "no output has a measured sale rate yet" in html2.lower())

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    raise SystemExit(1)
print("ALL PASS")
