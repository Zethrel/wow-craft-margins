# wowcraft — WoW crafting margin scanner

Finds crafts where the reagents cost less than the finished item sells for, on your
realm, using **Blizzard's official Battle.net Game Data API**. No game files, no
client reverse-engineering, nothing that touches the ToS grey area.

- One file, `wowcraft.py`
- **Zero third-party dependencies** — Python 3.9+ standard library only
- Local SQLite database, so every run adds to your own price history
- Output is a single self-contained `dashboard.html` (works offline, dark mode included)

---

## Setup (about five minutes)

**1. Get API credentials.** Go to <https://develop.battle.net/access/clients>, sign
in with your Battle.net account, and click *Create Client*. Name it anything;
redirect URL can be `https://localhost`. You get a **Client ID** and **Client
Secret**. It is free and self-service.

**2. Configure.**

```bash
python3 wowcraft.py config          # writes a starter config.json
```

Open `config.json` and paste in `client_id` and `client_secret`. **Region, locale and
realm are already set to Argent Dawn (EU)** — if you move realms, the slug is the
realm name lowercased with hyphens (*Argent Dawn* → `argent-dawn`). See
`config.example.json` for the full annotated version.

Prefer to keep secrets out of the file? Set `BNET_CLIENT_ID` and `BNET_CLIENT_SECRET`
as environment variables instead; they override the config.

**3. Check it works — do this before anything else.**

```bash
python3 wowcraft.py doctor
```

Probes every endpoint the tool depends on and writes `doctor-report.txt`. If
anything is broken, that file says exactly what and where. It contains **no
credentials**, so it is safe to paste anywhere for help.

It also prints every profession's skill tiers and hands you a ready-to-paste
`skill_tiers` list for the current expansion — worth doing before `init`, because
filtering to one expansion turns a fifteen-minute run into about one.

**4. Cache the recipe data.** Run once, and again after each content patch:

```bash
python3 wowcraft.py init
```

This pulls every recipe definition for the professions and skill tiers you listed,
then makes a second pass to work out what the modern ones actually craft (see
*What this tool does not know*, below — Blizzard stopped publishing that). Filtered
to the current expansion the two passes take about 100 seconds; unfiltered across
all expansions it is closer to fifteen minutes. The results are cached in SQLite,
so `scan` never repeats this.

**5. Scan.**

```bash
python3 wowcraft.py scan
```

Fetches the region-wide commodity auctions plus your realm's auctions, prices every
cached recipe, stores a snapshot, and writes `dashboard.html`. Open it in a browser.

Try `python3 wowcraft.py demo` at any point if you want to see the dashboard before doing
any of the above — it runs the whole pipeline on synthetic data with no credentials.

---

## Running it regularly

Blizzard refreshes auction data **hourly**. Snapshots are keyed on the server's own
`Last-Modified` timestamp rather than on your clock, so running `scan` five times in
one hour stores **one** snapshot, not five identical ones — your price history stays
honest no matter how twitchy your scheduling is. The tool tells you when it has seen
the same data twice.

The dashboard draws a sparkline per craft once you have two or more snapshots. That
history is the part that tells you whether a margin is a real trend or one person
having a bad day.

Linux/macOS cron, hourly:

```
0 * * * * cd /path/to/wow-craft-margins && /usr/bin/python3 wowcraft.py scan >> scan.log 2>&1
```

Windows: use the included `run-scan.cmd` rather than calling Python directly.

```powershell
$cmd = "D:\path\to\wow-craft-margins\run-scan.cmd"
$action = New-ScheduledTaskAction -Execute $cmd -WorkingDirectory (Split-Path $cmd)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(5) `
           -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "wowcraft hourly scan" -Action $action -Trigger $trigger
```

Point the task's **Execute** at the `.cmd` itself, not at `cmd.exe /c "..."`. If the
project path contains an ampersand — as `D:\Claude & Zethrel\...` does — cmd.exe
parses it as a command separator and the task dies before it reaches its first
line, with exit code 1 and an empty log. Naming the batch file directly avoids
that quoting entirely, which is also why the wrapper exists at all.

The wrapper appends to `scan.log` with a timestamp per run, records a non-zero
exit code, and trims the log to its last 400 lines once it passes 2 MB.

To run **whether or not you are logged on**, use S4U — it needs no stored
password, and outbound HTTPS works fine under it (only network *shares* do
not). From an elevated PowerShell:

```powershell
$p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
     -LogonType S4U -RunLevel Limited
Set-ScheduledTask -TaskName "wowcraft hourly scan" -Principal $p
```

**Do not use the Microsoft Store build of Python for this.** Its real
executable under `C:\Program Files\WindowsApps` is ACL-blocked and refuses to
launch; the only thing that runs is the App Execution Alias, and those do not
resolve outside an interactive logon. A task set to run logged-on-or-not will
fail with an empty log. Install python.org or `winget install
Python.Python.3.13 --scope machine` instead.

The wrapper picks an interpreter by **running** each candidate rather than
testing for its existence — the registry points at the Store's un-runnable
executable, so an existence check selects the one path guaranteed to fail. It
logs which interpreter it chose, and real installs are tried ahead of the
`py.exe` launcher, since that launcher can itself resolve to the Store build.

---

## How the numbers are worked out

This is the part worth understanding, because it's where naive versions of this
tool go wrong.

**Reagent cost is the real ladder cost, not the cheapest listing.** If you need 200
Verdant Ore and there are 10 at 1g, 50 at 3g and 400 at 5g, the honest cost is
`10×1 + 50×3 + 140×5` — not `200×1`. `--batch` controls how many crafts you're
costing (default 20). Raise it and margins fall, because you eat further up the
supply ladder. That is real, not a bug.

**Sale price is a quantity-weighted 15th percentile, not the minimum.** One person
listing a single item for 1 copper should not define the market.

**Thin markets are priced, not skipped — so watch the listing count.** A percentile
cannot protect you when there is only one listing to take a percentile of: that
listing *is* the price, and one optimist asking 190 million gold for an old transmog
piece becomes a 1,780,388% margin. Measured across a full scan, crafts whose output
had 1–2 listings came out at a **+3,623% median margin**; at 10+ listings that falls
to **+45%**, and at 50+ to **−10%**. The deep end is what a real market looks like.

Those crafts are still shown, because thin is not the same as wrong — transmog and
other niche markets are genuinely thin, and that is a legitimate thing to trade. The
supply and listing counts are on the dashboard so you can judge. If you would rather
not see them at all, `--min-listings 3` drops them (on a full scan that is about 417
crafts).

**A reagent shopping list per expansion.** The margin table answers *what should
I make*; the **Reagents to buy** table answers the question before it — what you
will have to buy and whether it is cheap today. Ranked by how many recipes in
that expansion use it, so staples come first rather than whatever is dearest,
with supply, listing count and a price sparkline. Required reagents only:
optional slots are a choice, not a shopping list.

**History is one row per item per day, kept for a week.** Scanning hourly does
not give you a hundred and sixty-eight points; it gives you seven, each refined
through the day. A scan in the morning and another in the evening of the 20th
both write the 20th's entry — the later one simply carries the newer values.
The day boundary is local midnight, so "the 20th" means the 20th where you are.
`history_days` in `config.json` sets the window (0 keeps everything, and grows
the database indefinitely). Days that fall outside it are pruned on each scan
and the file is vacuumed, so it does not creep upward.

**Each day's row keeps that day's range, not just its last reading.** The
headline price is the newest one, but `sell_low`/`sell_high` and
`buy_low`/`buy_high` widen across every scan of the day, so a daily row can
still answer "was this steady, or did it swing?". The reagent table shows it as
**Today's range** and it is sortable, which is how you spot a mat that is
thrashing before you commit to buying it. The in-game tooltip shows the same
line, and only when the price actually moved — its absence means steady, not
unknown.

**Cost assumes you buy everything; "buy" is what is left after your own
materials.** Those are different questions and the tool answers both. The
headline cost is what the craft is worth doing at, computed as if you owned
nothing — that is the number that tells you whether the recipe is any good.
Underneath it, where you already hold some of the reagents, is what finishing
it would actually cost you today, charged pro rata for a partial stack.

Materials come from the addon: bags, bank, reagent bank and warband bank, per
character, pooled across every character you have exported. Bank contents are
only readable while a bank is open, so the addon merges rather than replaces —
closing the bank does not erase what it just saw. `/wcinv` reports what has
been recorded, then `addon_import.py --apply` loads it.

**Revenue subtracts the 5% auction house cut.**

**Bid-only auctions are ignored** — you cannot reliably buy them, so letting them
drag the market price down would be misleading.

**Recipes are skipped, not guessed at**, when the output isn't listed or a reagent
has no price or insufficient supply. The count of each skip reason is printed and
shown on the dashboard, so you can see how much of the recipe list actually got
evaluated.

---

## What this tool does *not* know — read before trusting a number

These are real gaps, not hedging:

- **Optional and finishing reagent slots — the big one on current content.**
  Modern recipes put much of their real cost in *slots*, and Blizzard publishes
  neither what goes in a slot nor how much of it. The `modified-crafting`
  endpoints only hand back the slot's name; its category lists no items. So for
  a slotted recipe the reagent bill is a **floor** and the margin a **ceiling
  nobody can reach**.

  This is not a footnote on Midnight content, it is most of the list. Measured
  on Argent Dawn: of 364 priced crafts, 359 have slots. The five that do not
  (fishing lures) come out at a **−14% median margin, one profitable** — an
  ordinary-looking market. The 359 that do come out at **+2,150% median**, which
  is not a market, it is a missing cost.

  So those rows are badged **"cost is a floor"**, ranked below the fully costed
  crafts, and kept out of every headline figure and the chart. The honest
  summary is that this API cannot value a slotted craft, and the tool says so
  rather than printing a flattering number.

  **The addon bridge fixes this** for professions you export — see below. The
  client knows every slot, its quantity and what legally fills it, so those
  recipes get a real cost and lose the badge. What remains floor-costed is
  whatever you have not exported.
- **What a recipe even makes, on modern content.** Blizzard publishes
  `crafted_item` up to Shadowlands and then stops: every Dragon Isles, Khaz
  Algar and Midnight recipe arrives with reagents and no statement of its
  output. `init` recovers it by searching for an item with the recipe's name,
  which works because those names match. Rows resolved that way are badged
  **"name-matched"**. Where several items share the name, an auction listing
  breaks the tie when it can; otherwise the newest is used and the row is
  badged **"unverified output"** — check that one in game before trusting its
  revenue. On a full Midnight scan, 633 of 641 such recipes matched, 175 of
  them ambiguously.
- **Crafting quality ranks.** The recipe endpoint does not expose the bonus IDs
  that separate rank 1 from rank 3 — [every rank reports the same `crafted_item`](https://us.forums.blizzard.com/en/blizzard/t/recipe-api-returns-the-same-crafted-item-for-different-recipes/15081).
  So several recipes look like they make the same thing with different reagent
  bills. Rather than list them as separate opportunities, the tool collapses them
  to the cheapest recipe per output item and tags the row **"N ranks"**. Read those
  rows as *"the floor cost of making this at some rank"*, not as a specific craft.
- **Inspiration, resourcefulness, multicraft.** All of these move real profit and
  none are visible to the API. They generally push margins *up*.
- **Crafting orders.** Personal and patron orders often beat the open market
  entirely, and aren't in the API at all.
- **Whether it sells.** A margin is a listing-price difference. Supply and listing
  counts are shown so you can judge liquidity, but nothing here predicts velocity.
- **Your skill level.** Every recipe in the tier is evaluated whether or not you
  can make it.

Treat a high margin as a lead to verify in-game, not as gold in the bank. If a
number looks too good, it usually means the item is thinly listed or quality-tiered.

## Verified against the live API

The tool was originally built in a sandbox with `battle.net` blocked, so nothing
above the maths had ever run for real. It has since been run end-to-end against
the live EU API on Argent Dawn (patch 12.1.0), and these are now confirmed
rather than assumed:

- OAuth against `https://oauth.battle.net/token`
- profession index, skill tiers, recipes, item search, commodity and realm
  auctions, connected-realm resolution
- `reagents[]` → `{quantity, reagent: {id, name, key}}` — and `quantity` is
  genuinely `0` on the odd reagent, which is dropped rather than charged for
- `modified_crafting_slots[]` → `{display_order, slot_type}`, still with no
  quantity ([a known gap Blizzard has not filled](https://us.forums.blizzard.com/en/blizzard/t/missing-modifiedcraftingslots-quantity-in-recipe-endpoint/49170))
- auctions → `unit_price` on commodities, `buyout`/`bid` on realm listings

One documented shape turned out to be **wrong**, and it is the important one:

> `crafted_item` is **not** present on modern recipes. It is published up to and
> including Shadowlands and absent from Dragon Isles onwards, across every
> profession, locale and namespace tried. A Midnight recipe returns
> `['_links', 'id', 'media', 'modified_crafting_slots', 'name', 'reagents']` —
> reagents, but no statement of what they produce.

That is what the name-matching fallback above exists to work around. `doctor`
samples the newest tier *and* an old one so the contrast is visible in the
report, and exercises the fallback on live data.

Run `doctor` first.

---

## Commands

| Command | What it does |
|---|---|
| `config` | Write a starter `config.json` |
| `init` | Cache recipe definitions (once per patch) |
| `scan` | Fetch auctions, compute margins, write the dashboard |
| `demo` | Run the whole pipeline on synthetic data, no credentials |
| `doctor` | Probe every endpoint, write a shareable diagnostic report |

## Price lookup (`pricecheck.py`)

A small window you open and close, for "what is X going for" when you are
nowhere near an auctioneer.

```bash
python3 pricecheck.py
```

or double-click `pricecheck.cmd` on Windows, which launches it with `pythonw`
so no console sits behind it. It reads `wowcraft.sqlite3` directly, so it shows
whatever the last scan wrote — **Refresh** picks up a newer one without
restarting.

All 29,000-odd priced items, searchable by name or item id, sortable, with
today's range and the trend across the stored history. The dropdown narrows to
one expansion and **opens on the current one** — items carry no expansion of
their own, so it is derived from the recipes that use them, and "current" is
whatever the newest cached skill tier belongs to rather than a hardcoded name.
An item can be in several (Classic herbs still turn up in modern recipes), and
*Not in any recipe* reaches the ~21,000 that no craft touches. Unnamed items are kept
here (unlike the dashboard) because you can search them by id. The **ID** column
matters more than it looks: quality tiers are separate items with identical
names, so two `Void-Tempered Leather` rows at different prices are correct, not
a duplicate.

This deliberately is not part of the dashboard. That is a report you glance at;
eight thousand searchable rows made it a four megabyte page that would not
load.

Useful flags: `--batch N` (crafts per batch, default 20), `--top N` (dashboard rows,
default 200), `--min-listings N` (optional liquidity floor, default 1 = price
everything), `--out FILE`,
`--db FILE`, `--config FILE`.

### Slicing a scan by expansion or profession

`--tier` and `--profession` override `config.json` for one run. They are
repeatable and accept comma-separated lists:

```bash
python3 wowcraft.py scan --tier Midnight
```

```bash
python3 wowcraft.py scan --tier "Khaz Algar,Dragon Isles" --profession Alchemy
```

**This does not make `scan` faster.** Blizzard only serves whole auction dumps —
every scan downloads all ~360k commodity listings and your realm's ~125k
regardless — and scoring cached recipes against that price index takes
milliseconds either way. The flags are for signal, not speed: a narrower page
you can actually read. The dashboard also carries expansion and profession
dropdowns, so one full scan is usually enough and you filter in the browser.

The same flags work on `init`, where they *do* change the cost, because that is
the expensive step. Use them to top up one expansion after a patch:

```bash
python3 wowcraft.py init --tier Midnight
```

The shipped config caches everything (`"skill_tiers": []`) on purpose. Old tiers
are the trustworthy ones — they still publish `crafted_item` and rarely use
reagent slots — so a current-expansion-only cache filters out precisely the
crafts this tool can still value honestly.

## Tests

```bash
python3 test_wowcraft.py    # pricing and margin maths, hand-checked
python3 test_variants.py    # crafting-rank collapsing
python3 test_doctor.py      # doctor against a fake API
python3 test_pipeline.py    # init + scan end-to-end against a fake API
python3 test_addon.py       # the addon's Lua, against a stubbed client
python3 test_prices.py      # the in-game price display, against real generated data
python3 test_inventory.py   # the inventory collector, addon Lua through to owned totals
python3 test_trade.py       # the trade-channel watcher, including that it never sends
```

`test_addon.py` needs `lupa` (`pip install lupa`) to run the addon's Lua for
real; without it the file skips rather than failing, so the scanner itself
still has no third-party dependencies.

297 assertions in total: the supply ladder, percentile pricing,
troll-listing resistance, stack-price normalisation, the AH cut, every skip
condition, crafting-rank collapsing, hourly snapshot de-duplication, init
idempotency, and that the dashboard is genuinely self-contained — plus, for the
modern-recipe path, name resolution and its tie-breaking, zero-quantity
reagents, floor-cost ranking and badging, the liquidity floor, `--tier` /
`--profession` scoping (including that a scoped scan neither reports nor
deletes anything outside its scope), and that a database made by an older
version still opens and scans. The addon's Lua is executed against a stubbed
client covering a healthy export, one where every interesting field comes back
as a secret value, a client missing the APIs outright, and the SavedVariables
escaping round trip.


---

## The addon bridge (`addon/WowCraftExport`)

The game client knows everything the Game Data API withholds. This addon reads
it and writes it to SavedVariables; `addon_import.py` reads that back.

### Prices in game

`scan` also writes `PriceData.lua` into the addon folder when `addon_path` is
set in `config.json`. The addon loads it and adds, for any item it knows:

- cheapest and realistic auction price on reagent tooltips
- craft cost and margin after the AH cut on anything craftable
- a line on the crafting window showing cost, sale price and margin for the
  recipe you have open

Addons cannot read files at runtime, only at load, so **what you see is as
fresh as your last `/reload`**. Every number is stamped with its age for that
reason — "22m ago" rather than a bare figure pretending to be current.
`/wcprices` reports what is loaded.

**It does not scan the auction house.** In-client scanning is throttled, needs
you parked at an auctioneer, and only sees your realm. The API already gives
region-wide prices hourly from a cron job, so prices stay where they are. The
addon only supplies the *crafting model*:

- reagent slot quantities and the items that legally fill each slot — the fix
  for "cost is a floor"
- what the recipe actually crafts, so name matching stops being a guess
- what you already own, so margins can also say what is left to buy

Measured across ten exported professions (5,721 recipes, patch 12.1.0):

| | result |
|---|---|
| secret values encountered | **none** — every field read cleanly |
| reagent slots with a required quantity | 19,341/19,341 |
| slots with their legal item list | 19,109/19,341 |
| recipes given real costs from client data | 4,353 |
| outputs corrected where the API had guessed wrong | 170 |
| recipes the API pipeline never listed at all | 238 |

Name-guess accuracy varied enormously by profession — 100% for Cooking, 92%
Blacksmithing, 88% Engineering, 73% Enchanting, **53% Jewelcrafting** (gems have
many same-named variants). Nearly every error sat in the ambiguous bucket the
dashboard already badges `unverified output`, which is what that badge is for.

The effect on Midnight margins, same auction snapshot, client-costed against
API-only:

| profession | client-costed | API-only before |
|---|---|---|
| Leatherworking | +205% | — |
| Blacksmithing | +66% | — |
| Engineering | +21% | — |
| Jewelcrafting | −41% | — |
| Cooking | −65% | — |
| Alchemy | −72% | — |
| Tailoring | **+51%** | was **+15,809%** |

**Crafting quality ranks are partly recoverable.** `GetRecipeOutputItemData`
returns distinct per-quality items for gear (53 Blacksmithing and 28
Leatherworking recipes) but the same id for every quality on consumables, so
Alchemy and Cooking show none. Not yet modelled either way.

### Trade requests you can fill

People link the item they want made, so there is no text matching to do: the
addon pulls the itemID out of the link and checks it against what **this
character has learned**. Nothing fires unless you can actually make the thing.

Open a profession window once per character and run `/wctrade learn` (or just
open it — a profession window refreshes the set on its own). After that, a
linked request in trade prints a line with the buyer's name, the item, and what
the mats cost you, and lists it in a small movable window. Click a row to open
a whisper box addressed to them.

**It never sends anything.** Clicking fills in the whisper and leaves the
cursor to you. Automated whispering is a spam-policy problem and would buy
nothing — the value is in noticing the request, not in saving a keystroke.
`/wctrade` shows what is being watched, `/wctrade clear` empties the list.

Usage: open a profession window, `/wcexport`, then `/reload` to flush the file.
Repeat once per profession, on whichever character has it — each export is
stored under its own skill line, so professions accumulate rather than
overwrite. `/wcexport list` shows what you have banked, `/wcexport clear`
starts over.

One export per profession is enough: `GetAllRecipeIDs` returns every recipe you
know in that profession, not just the expansion tab you have open. The client
writes everything to
`WTF/Account/<ACCOUNT>/SavedVariables/WowCraftExport.lua` — named after the
addon, not after the saved variable. The importer merges across every account
and character folder it finds, newest export of each profession winning. Then:

```bash
python3 addon_import.py
```

It finds the SavedVariables file, prints what the client could and could not
read, and diffs the export against the cache — including how many of the
name-matched outputs it confirms, and how many floor-cost recipes it can price.
Add `--apply` to write that into the cache, then re-run `scan`:

```bash
python3 addon_import.py --apply
```

Applied rows are badged **client data**, their outputs replace the API's
guesses, and their cost is computed from the client's own reagent slots. A
later `init` will not undo any of it — where the game has spoken, the API's
guess loses.

`--apply` also looks up names for any item the cache references but cannot
name — the items that legally fill a reagent slot are frequently ones no cached
recipe ever mentioned, and without this the reagent bill reads `item 222514`.
Names never change, so it is a one-off per item; `--names` runs just that step.

`--apply` also **adds recipes the API never listed at all**. Those are stored
under the *negated* client recipe id: the two sides number recipes
independently and genuinely collide (98 of the client's ids clash with cached
API ids on this account), so inserting under the client's own id would
overwrite unrelated recipes. A negative id cannot collide with either
namespace and makes the row's origin obvious.

Their skill tier is inferred from the recipe id rather than from the window
that exported them, because `GetAllRecipeIDs` returns the whole profession — a
Pandaria dish exported from the Midnight tab would otherwise pollute
`--tier Midnight`. Ids are handed out in ascending blocks per expansion, and
thousands of recipes appear on both sides, so the block boundaries are measured
from that overlap rather than hardcoded.

### Why this matters more than it sounds

The API does not merely omit optional reagents on modern recipes, it
under-reports the **required** ones. Measured across five exported professions:

| cached tier | API reagent lines | client required slots |
|---|---|---|
| Classic Blacksmithing | 3.46 | 3.52 |
| Outland Blacksmithing | 2.94 | 2.94 |
| Dragon Isles Leatherworking | 1.04 | 3.24 |
| Dragon Isles Blacksmithing | 0.76 | 2.62 |

Old tiers agree; Dragonflight-era tiers report about a third of what the craft
actually consumes. The true cost came out a median **10.9× higher**, which is
the whole of the fantasy-margin problem. On the same auction snapshot, Midnight
recipes costed from client data have a **−46% median margin** against
**+3,289%** for those still priced from the API alone.

Optional and finishing slots are filled with the cheapest item that legally
fits, and the row is badged with how many were filled — assuming they stay
empty is what made the numbers fictional in the first place, but the fill is
still an assumption about how you would craft, so it is labelled rather than
buried.

Collection only: it reads `C_TradeSkillUI` and writes one table. No protected
functions, no secure frames, so no taint. Fields that come back as 12.0 secret
values are counted and skipped rather than used, so the export degrades into a
diagnostic instead of erroring.

Reading the game's memory from outside the client is a different matter
entirely — it is detectable and bannable, and nothing here does it.

---

## Working on the addon

The addon's source of truth is `addon/WowCraftExport/` in this repo. The copy
under `Interface/AddOns/` is what the game runs, which makes it the tempting
one to edit — and a plain copy in the other direction would destroy those
edits silently.

```bash
sync-addon.cmd          # install repo -> game
sync-addon.cmd back     # bring game-side edits back into the repo
```

Installing **refuses** to overwrite an installed file that differs from the
repo's and is newer, and tells you to pull it back instead. Exit code 2 means
something was refused.

`config.json` is gitignored: it holds your Battle.net client id and secret, and
committing it once would put them in the history permanently. `config.example.json`
is the tracked template. `wowcraft.sqlite3` is untracked too — 23 MB that
rewrites itself every scan — but note it holds your price history, which is the
one thing here that cannot be regenerated, so back it up separately if that
matters to you.

This is private code. There is no licence file, and that is deliberate: with no
licence, default copyright applies and nobody has any right to use it.

---

Data © Blizzard Entertainment, retrieved through the public Battle.net Game Data
API. This tool is unofficial and not affiliated with Blizzard.
