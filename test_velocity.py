"""Sale rate and smoothed market value - the two things TSM has that a single
scan cannot give you.

The API never reports a sale. The only signal available is that units which
were listed are not listed any more, so sale rate here is the fall in listed
quantity between scans, accumulated per day. It undercounts (somebody posting
more between two scans hides what went) and overcounts (a cancellation looks
like a sale), and what it is actually good for is telling "this moves" apart
from "this sits" - which margin alone cannot.

Market value is the other half: one scan's price is one moment, and TSM
averages fourteen days precisely because a single reading is one seller having
a bad afternoon.
"""
import os
import sys
import tempfile
import time

import wowcraft as W

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


DAY = 86400
tmp = tempfile.mkdtemp()
today = W.day_bucket(int(time.time()))


def px(sell, qty, mn=None):
    return W.ItemPrice(item_id=1, source="commodity", sell_unit_price=sell,
                       min_unit_price=mn if mn is not None else sell,
                       total_quantity=qty, listing_count=3)


# ---- 1. removals accumulate across a day -----------------------------
s = W.Store(os.path.join(tmp, "vel.sqlite3"))
# First reading of all: nothing to diff against, so nothing is claimed.
s.save_prices(today, {1: px(100, 500)})
row = s.db.execute("SELECT units_removed, removal_obs FROM price_snapshot "
                   "WHERE item_id=1").fetchone()
must("the first ever reading claims no sales", row[0] == 0 and row[1] == 0)

s.save_prices(today, {1: px(100, 460)}, elapsed=3600)   # 40 gone in an hour
s.save_prices(today, {1: px(100, 450)}, elapsed=3600)   # 10 more in another
row = s.db.execute("SELECT units_removed, removal_obs, seconds_covered "
                   "FROM price_snapshot WHERE item_id=1").fetchone()
must("removals accumulate across the day's scans", row[0] == 50)
must("observations are counted", row[1] == 2)
must("so does the market time they span", row[2] == 7200)

# Somebody posting more is not a sale, and must not net off what did sell.
s.save_prices(today, {1: px(100, 900)})
row = s.db.execute("SELECT units_removed FROM price_snapshot "
                   "WHERE item_id=1").fetchone()
must("a restock does not subtract from the day's sales", row[0] == 50)
s.save_prices(today, {1: px(100, 880)})
row = s.db.execute("SELECT units_removed FROM price_snapshot "
                   "WHERE item_id=1").fetchone()
must("and selling resumes from the new level", row[0] == 70)

# Re-scanning data Blizzard has not refreshed is not a quiet hour of trade.
before = s.db.execute("SELECT removal_obs FROM price_snapshot "
                      "WHERE item_id=1").fetchone()[0]
s.save_prices(today, {1: px(100, 880)}, count_removals=False)
after = s.db.execute("SELECT removal_obs FROM price_snapshot "
                     "WHERE item_id=1").fetchone()[0]
must("a repeat of unchanged data is not counted as an observation",
     after == before)
s.close()

# ---- 1b. sales, from watching individual auctions --------------------
# Aggregate quantity cannot tell a sale from a cancellation or a repost.
# An auction id can: a posting that shrank was bought from, and one that
# vanished with hours still to run did not expire.
sf = W.Store(os.path.join(tmp, "flow.sqlite3"))


def auc(aid, item, qty, left="VERY_LONG"):
    return {"id": aid, "item": {"id": item}, "quantity": qty,
            "time_left": left}


# First sight of the market: nothing to compare against, so no sales claimed.
first = sf.record_auction_flow([auc(1, 50, 100), auc(2, 50, 40),
                                auc(3, 60, 1), auc(4, 60, 1, "SHORT")])
must("the first scan claims no sales", first == {})

flow = sf.record_auction_flow([
    auc(1, 50, 70),        # 30 bought off a surviving stack
    auc(3, 60, 1),         # untouched
    # auction 2 (40 units, VERY_LONG) has gone: sold or cancelled, not expired
    # auction 4 (1 unit, SHORT) has gone: might simply have run out
])
confirmed, likely = flow[50]
must("a shrunken listing is a confirmed purchase", confirmed == 30.0)
must("a vanished listing with hours left counts as sold", likely == 40.0)
must("an auction that vanished on SHORT is not counted",
     60 not in flow or flow[60] == (0.0, 0.0))

# Reposting is the case the aggregate got wrong: quantity swings down and up
# while nothing sells. Ids make it obvious - the old auction is gone but a
# new one appeared, and only the vanished one with time left counts.
sf2 = W.Store(os.path.join(tmp, "repost.sqlite3"))
sf2.record_auction_flow([auc(10, 70, 500, "SHORT")])
churn = sf2.record_auction_flow([auc(11, 70, 500)])
must("cancel-and-relist on a short auction is not a sale",
     churn.get(70, (0.0, 0.0)) == (0.0, 0.0))
sf2.close()

# Growing a stack is not a negative sale.
sf3 = W.Store(os.path.join(tmp, "grow.sqlite3"))
sf3.record_auction_flow([auc(20, 80, 10)])
grown = sf3.record_auction_flow([auc(20, 80, 90)])
must("a listing that grew reports nothing", 80 not in grown)
sf3.close()

# The working set is replaced, not appended, or it would grow without bound.
kept = sf.db.execute("SELECT COUNT(*) FROM auction_prev").fetchone()[0]
must("only the latest scan's auctions are kept", kept == 2)
sf.close()

# ---- 2. velocity is units per unit of OBSERVED time ------------------
s = W.Store(os.path.join(tmp, "rate.sqlite3"))
H = 3600.0


def put(day, iid, sold, obs, covered_h):
    s.db.execute("INSERT INTO price_snapshot(taken_at,item_id,source,"
                 "sell_unit_price,total_quantity,sold_confirmed,removal_obs,"
                 "seconds_covered) VALUES(?,?,?,?,?,?,?,?)",
                 (day, iid, "commodity", 100, 10, sold, obs, covered_h * H))


# 100 units over 24h of observation spread across two days = 100/day.
put(today - 2*DAY, 1, 60, 4, 12)
put(today - 1*DAY, 1, 40, 4, 12)
# A short day must not read as a slow day: 25 units in 6 observed hours is
# the same rate as 100 in 24, and today counts because we divide by time.
put(today, 1, 25, 2, 6)
# Watched long enough, and nothing moved: measured, and zero.
put(today - 1*DAY, 2, 0, 8, 24)
# A row with no observation at all is not a measurement.
put(today - 1*DAY, 3, 0, 0, 0)
# Watched, but only briefly - extrapolating an hour to a day is arithmetic,
# not evidence.
put(today - 1*DAY, 4, 5, 1, 1)
s.db.commit()

vel = s.sale_velocity(days=7)
must("rate is units per day of observed market time",
     abs(vel[1] - 100.0) < 1e-6)
must("a part day counts, because time is the divisor", 1 in vel)
must("watched but static is zero, not missing", vel.get(2) == 0.0)
must("never observed is missing, not zero", 3 not in vel)
must("too little observation is withheld, not extrapolated", 4 not in vel)

# ---- 3. market value leans on recent days ----------------------------
s2 = W.Store(os.path.join(tmp, "mv.sqlite3"))
for age, price in ((3, 1000.0), (2, 1000.0), (1, 1000.0), (0, 2000.0)):
    s2.db.execute("INSERT INTO price_snapshot(taken_at,item_id,source,"
                  "sell_unit_price,total_quantity) VALUES(?,?,?,?,?)",
                  (today - age*DAY, 1, "commodity", price, 10))
s2.db.commit()
mv = s2.market_values(days=14, halflife=2.0)
must("market value sits between the old and new price",
     1000.0 < mv[1] < 2000.0)
# Weights are 1, .707, .5, .354 for ages 0..3, so today carries 39% of the
# total and a doubling moves the reported price 39% of the way. Pinned rather
# than bounded, so a change to the decay has to be deliberate.
must("today pulls it 39% of the way, no more", abs(mv[1] - 1390.5) < 1.0)

# Over a seven-day window that puts 71% of the weight on the last three days,
# which is the shape TSM describes for DBMarket.
recent = sum(0.5 ** (a / 2.0) for a in (0, 1, 2))
whole = sum(0.5 ** (a / 2.0) for a in range(7))
must("the last three days hold about 71% of the weight",
     0.68 < recent / whole < 0.74)

flat = s2.market_values(days=14, halflife=0)
must("halflife 0 is a plain mean", abs(flat[1] - 1250.0) < 1e-9)

# A single day of history cannot smooth anything, and must not pretend to.
s3 = W.Store(os.path.join(tmp, "one.sqlite3"))
s3.save_prices(today, {1: px(777, 10)})
one = s3.market_values(days=14)
must("one day of history returns that day's price", abs(one[1] - 777.0) < 1e-9)
s3.close()
s2.close()
s.close()

# ---- 4. it reaches the result and the page ---------------------------
import json
prices = W.build_price_index(
    [{"item": {"id": 1}, "quantity": 1000, "unit_price": 100},
     {"item": {"id": 9}, "quantity": 100, "unit_price": 10000}], "commodity")
rec = {"id": 1, "name": "Thing", "profession_name": "Alchemy",
       "skill_tier_name": "T", "crafted_item_id": 9,
       "crafted_qty_min": 1, "crafted_qty_max": 1,
       "reagents_json": json.dumps([{"id": 1, "quantity": 5}])}
res, _ = W.compute_margins([rec], prices, {1: "A", 9: "Out"}, batch=1,
                           min_listings=1, velocity={9: 12.5})
must("velocity reaches the result", res[0].output_sold_per_day == 12.5)

res_none, _ = W.compute_margins([rec], prices, {1: "A", 9: "Out"}, batch=1,
                                min_listings=1)
must("no velocity data leaves it unmeasured",
     res_none[0].output_sold_per_day is None)

cfg = {"realm_slug": "r", "region": "eu"}
html = W.render_dashboard(res, cfg, today, {}, {}, 50, 1, 1)
must("the page has a sells-per-day column", 'data-key="vel"' in html)
must("the row carries the rate", 'data-vel="12.5000"' in html)
must("the page offers a moves-only filter", 'id="moves"' in html)

html2 = W.render_dashboard(res_none, cfg, today, {}, {}, 50, 1, 1)
must("unmeasured rows sort to the bottom, not the top",
     'data-vel="-1.0000"' in html2)
must("and read as a dash, not a zero", "&ndash;" in html2)

# Three states, and they must not look alike: not measured, measured and
# nothing left the market, measured and something did.
must("unmeasured renders as a dash", "&ndash;" in W.velocity_str(None))
must("nothing having moved reads as static", W.velocity_str(0.0) == "static")
must("something having moved reads as moves", W.velocity_str(0.25) == "moves")
must("a large figure still only reads as moves",
     W.velocity_str(1234.0) == "moves")

# No rate is printed, at any magnitude. The first version of this column
# summed max(0, previous - current) across scans and published units per day,
# which on live data had 15% of items shifting more than their whole standing
# supply within seven hours - a one-sided sum over a noisy series measures
# volatility, not trade. The sign survives that; the magnitude does not, and
# must not find its way back onto the page.
for value in (0.25, 12.5, 1234.0, 2684177.3):
    rendered = W.velocity_str(value)
    must(f"no number leaks out for {value:g}",
         not any(ch.isdigit() for ch in rendered))

must("the tip explains an unmeasured row",
     "needs several hours" in W.velocity_tip(None))
must("the tip explains what static really means",
     "nothing left the market" in W.velocity_tip(0.0))
must("the tip admits the static blind spot",
     "restocked faster" in W.velocity_tip(0.0))
must("the tip refuses to claim a quantity",
     "cannot be had" in W.velocity_tip(50.0))
must("the tip says cancellations are indistinguishable",
     "cancellations look identical" in W.velocity_tip(50.0).lower())

print()
if fails:
    print(f"{len(fails)} FAILURES: {fails}")
    raise SystemExit(1)
print("ALL PASS")
