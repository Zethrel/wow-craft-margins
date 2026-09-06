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

Run it **by hand** and it also prints that run's log lines back to the console
when it finishes — a scheduled run stays silent, since there is nobody to print
to. It decides by `SESSIONNAME`, which is `Console` or `RDP-Tcp#nn` for a
logged-on session and `Services` or unset in session 0. Output still streams
into `scan.log` while the command runs, so a long `scan` can still be watched
with `Get-Content scan.log -Wait -Tail 20` from another window.

Note that from PowerShell it is `.\run-scan.cmd pull`, and that it takes the
subcommand as its first argument — no argument means `scan`.

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

## Running it in the cloud instead

The scan does not have to happen on your PC. `.github/workflows/scan.yml` runs
it hourly in GitHub Actions and publishes the results to GitHub Pages; `pull`
downloads them. Your machine then needs **no Battle.net credentials, no recipe
cache and no scheduled scan** — and the data stays current while the PC is off.

**The one thing that cannot move.** WoW's Lua sandbox has no sockets and no
HTTP, deliberately, so an addon can never fetch anything. `PriceData.lua` has
to be a real file in the AddOns folder before the client loads it. Something
local must always do the writing — `pull` is that something, reduced to a
download. Every price addon works this way.

### What gets published

`scan --publish site` fills a directory with everything a machine that never
talks to Blizzard would need:

| File | Size | Read by |
|---|---|---|
| `PriceData.lua` | ~520 KB | the in-game addon |
| `dashboard.html` (and `index.html`) | ~1.2 MB | your browser |
| `prices.sqlite3.gz` | ~4.8 MB | `pricecheck` |
| `manifest.json` | ~400 B | `pull`, to skip what has not changed |

`prices.sqlite3.gz` is the real database with `inventory` and `margin_snapshot`
emptied and the file `VACUUM`ed — schema-identical on purpose, so `pricecheck`
opens it with the queries it always used. Nothing needed a second code path.

### Setting it up

**1. Repository secrets.** Settings → Secrets and variables → Actions → *New
repository secret*, twice: `BNET_CLIENT_ID` and `BNET_CLIENT_SECRET`. The tool
already prefers those environment variables over `config.json`, so no code
change. They stay hidden even on a public repo, and the workflow does not run
on pull requests, so a fork cannot read them.

**2. Enable Pages.** Settings → Pages → Source: **GitHub Actions**. Not the
"deploy from a branch" option — the workflow uploads an artifact directly, so
nothing is ever committed to a `gh-pages` branch and the repo stays small.

**3. Edit `ci-config.json`** if your realm is not Argent Dawn (EU). It holds no
secrets and is meant to be committed.

**4. Run it once** — Actions → *hourly scan* → *Run workflow*. The first run
cold-starts from `seed/recipes.sqlite3.gz` and takes about a minute.

**5. Point your PC at it.** In `config.json`:

```json
"pull_url": "https://<your-user>.github.io/wow-craft-margins/"
```

Then, instead of `run-scan.cmd`:

```bash
pull.cmd
```

Swap the scheduled task's **Execute** from `run-scan.cmd` to `pull.cmd` and it
keeps running on a timer, minus the credentials and the API calls. `/reload` in
game picks up new prices.

**Pull twice an hour, at :05 and :35.** GitHub's scheduler is best-effort and
routinely runs late — the workflow's first week on a round `:20` fired once,
half an hour behind. Pulling every thirty minutes means a publish that arrives
forty minutes late is still collected within the hour instead of being missed
entirely. An unchanged pull costs one 693-byte manifest fetch, so the extra
runs are free.

### Sharing it with other people

**Send them the site URL.** `index.html` is a landing page, not the dashboard:
what this is, the addon download, the four steps to get prices in game, the
realms being published, and the caveat about other realms and other regions. It
is 7 KB with no scripts and no external requests. The dashboard is still
published as `dashboard.html` and linked from it — which is also the name `pull`
fetches, so nothing about the pull path changed.

`site_url` and `repo_url` in `ci-config.json` are where its links point. They
have to be absolute: the same file is written into every realm's folder *and*
copied to the site root, so relative links would have to be correct at two
depths at once. Both fall back to `GITHUB_REPOSITORY` when empty, so a fork
needs no edit.

Someone who only wants prices in game needs **the addon folder and nothing
else**. No Python, no scheduled task, no account, no configuration beyond their
realm. Inside the addon folder are two batch files:

| File | What it does |
|---|---|
| `sync.cmd` | fetches the latest `PriceData.lua` into the folder it lives in |
| `sync-hourly.cmd` | registers a scheduled task to do that at :35 past, no admin needed |

`curl.exe` ships with Windows 10 1803 and later, so there is nothing to
install. `sync.cmd` writes into its own directory, so there is no path to
configure — it is already in the right place. Edit the `REALM` line at the top,
or pass a slug: `sync.cmd twisting-nether`.

They will still want the game restarted or `/reload`ed to pick up new prices.

**Why they cannot skip the batch file.** WoW's Lua sandbox has no sockets and
no HTTP, deliberately, so no addon can fetch anything — `PriceData.lua` has to
be a real file before the client loads it. Nor can GitHub push: a remote server
cannot write into somebody's filesystem, so the transfer has to be started
locally. This is the same reason TradeSkillMaster ships a desktop application.

**What this costs, and where it stops scaling.** Nobody you share with ever
touches Blizzard — they fetch static files from Pages — so an extra user costs
zero API calls. What it costs is bandwidth, and GitHub Pages has a *soft* limit
of 100 GB/month:

| Who | Per month | Fits in 100 GB |
|---|---:|---:|
| addon only (`sync-hourly`, hourly) | 0.47 GB | ~210 people |
| dashboard in a browser (5 views/day) | 0.29 GB | ~340 people |
| full `pull.cmd` (everything, hourly) | 7.54 GB | **~13 people** |

The 8.2 MB price database is 75% of that last figure. So the addon is the thing
to hand out freely; `pull.cmd` is for people you would actually help debug, and
the landing page asks them to check first.

**Every realm you share with needs publishing.** Commodity auctions are
region-wide, but realm auctions are not, and that is where crafted gear is
priced. Measured across two realms scanned an hour apart:

| | |
|---|---|
| items priced from **commodities** | 11,863 — **100% identical** across realms |
| items priced from the **realm AH** | 14,272 — **99% different** |

So a friend on another realm would get correct reagent costs and wrong sale
prices on most crafted gear, producing margins that look authoritative and are
not. Add their realm to `realms` in `ci-config.json`; it costs one extra API
call and a few seconds per scan. Each realm publishes to its own folder, and
the first is also published at the root so existing `pull_url` settings keep
working.

Each realm needs **its own database** — history is keyed on the item, not the
realm, so sharing one would blend two markets together. The scanner refuses
rather than allowing it, because the result would look entirely reasonable.

### Kicking a late publisher

Running late is one thing; **dropped slots** are another. Of the first three
observed, two ran 21 and 31 minutes behind and one never published at all.

Set a token and `pull` handles that itself: when the published data is older
than `dispatch_when_stale_minutes` (default 90), it asks GitHub to run the
scan now, waits up to `dispatch_wait_seconds` for it to appear, and collects
it in the same run. Blizzard refreshes hourly, so 90 minutes means a cycle
genuinely went missing rather than merely arriving late — normal lateness never
triggers it. A dispatched run starts immediately, because it is not sitting in
the schedule queue.

It asks **once per half hour at most**, whatever the pull schedule, and the
cooldown survives `--force`. The workflow is not the bottleneck — GitHub's
queue is — so asking twice does not make it arrive sooner.

**The token.** Create a [fine-grained personal access
token](https://github.com/settings/personal-access-tokens/new): this
repository only, and **Actions: read and write** as the sole permission. Give
it the shortest expiry you will tolerate re-issuing. Then either put it in
`config.json` as `github_token`, or — better — set it as an environment
variable:

```bash
setx WOWCRAFT_GITHUB_TOKEN "github_pat_..."
```

The environment wins over the file. `config.json` is gitignored, but a token
that is never in a file cannot be committed by accident at all. `pull` only
ever puts it in an `Authorization` header: it is never logged, never echoed on
failure, and never written to `.pull-state.json`, all of which the tests check.

Leave `github_token` empty and none of this happens — `pull` says it noticed
the staleness and carries on. Set `dispatch_when_stale_minutes` to `0` to
switch it off entirely.

### When any of it misbehaves

```bash
python3 wowcraft.py doctor
```

`doctor` now covers the pull side as well as the API, and **runs without
Battle.net credentials** — which is the normal state once scanning moved to
CI, and exactly when you want a diagnostic. It reports:

- **[C1]** the published site: manifest reachable, data age against your
  staleness threshold, every published file and its size
- **[C2]** timezones: publisher versus this machine, since a mismatch stores
  every calendar day twice and nothing else complains
- **[C3]** local state: whether sqlite reports the database as damaged,
  database size, recipe count, days of history, whether any date is stored
  twice, and when `PriceData.lua` was last written
- **[C4]** self-dispatch: the token's source and length, the target repo and
  workflow, and whether the token can actually dispatch

That last check **starts no workflow**. It dispatches a ref that cannot exist,
because GitHub validates the permission before it resolves the ref: `403` means
the permission is missing, `422` means the permission is fine and only the
branch was bogus. A `403` also prints the two settings that cause it — the
*Public repositories* access mode is read-only and can never grant
`actions=write`, which GitHub's own error never mentions.

The report contains no credentials and no token — only lengths and sources —
so it stays safe to paste anywhere.

### When the database itself is damaged

```bash
python3 wowcraft.py repair
```

Checks `PRAGMA integrity_check` and, if sqlite reports damage, rebuilds the
file by reading every row with a plain table scan into a fresh, empty schema.
It keeps the damaged original beside the repaired one as
`wowcraft.sqlite3.YYYY-MM-DD.bad`, and says how many rows (if any) it could not
carry across. On a healthy database it does nothing and says so.

This is not hypothetical, and it is worth knowing what it looked like. Two rows
went missing from `price_snapshot`'s primary-key index on a machine that had
been running for weeks. **Nothing announced it.** Every count and every scan
kept returning the right answer, because they read the table. `SELECT DISTINCT`
reads the *index* — so it returned an empty blob that had never been stored,
the history carry compared that against an integer, and `pull` died on the
`TypeError` before the swap. Every hour. For six days. The dashboard kept
updating (it is written before the database is installed) and the tooltip in
game kept quoting week-old prices under a timestamp that looked fine at a
glance.

Three things changed as a result:

- `pull` checks the file before reading it, rebuilds a clean copy to read
  through if it is damaged, and completes — it replaces the whole database
  anyway, so damage in the old one now costs at most the local-only tables,
  never the pull.
- `doctor`'s **[C3]** reports integrity, so the state is visible without
  waiting for something to break.
- `repair` exists for the machine that *scans*, where nothing is coming to
  replace the file and a bad index would otherwise sit there indefinitely.

`pull` heals itself; a scanning machine needs `repair`. A rebuild is cheap —
about a second on a 28 MB database — so running it on suspicion costs nothing.

### The database, and why there is a seed

`scan` needs the recipe cache `init` builds — 9,725 recipes, about fifteen
minutes. Actions runners keep nothing between runs, so the workflow carries the
database in the Actions cache, and `seed/recipes.sqlite3.gz` (794 KB, tracked)
is the cold-start fallback for when that cache is empty or expired. Prices are
deliberately not in the seed; they are refetched every run anyway.

The workflow saves a fresh ~18 MB cache each hour and prunes all but the newest
three, so this does not quietly grow until GitHub starts evicting caches.

### Patch day

Recipe definitions change when the game does and not otherwise, so the hourly
run never pays for re-caching them. When a patch lands, go to **Actions →
*hourly scan* → Run workflow** and tick:

| Input | When |
|---|---|
| **init** | Always, after a patch. Re-caches recipe definitions. |
| **tier** | Set to the new expansion, e.g. `Midnight`. Turns fifteen minutes into about two. Leave empty to re-cache every expansion back to Classic. |
| **names** | If new items are showing up unnamed in `pricecheck`, or newly gathered goods have no price in game. Adds ~6 minutes. |

The run inits, then scans and publishes as usual, then regenerates
`seed/recipes.sqlite3.gz` and commits it if it changed — so a cache eviction
after a patch cold-starts from post-patch recipes rather than silently scanning
against a stale recipe set, which fails as wrong numbers rather than an error.

`init` and `names` together are most of Blizzard's 36,000-per-hour budget. The
worst case is HTTP 429s, which skip individual items rather than losing
anything; the next run picks up whatever is still missing.

These are the only jobs that ever needed Battle.net credentials on your own PC.
They no longer do.

### Set the timezone, or you get every day twice

`ci-config.json` has a `timezone` field, and it **must match the machine that
runs `pull`**. History is one row per day bucketed at *local* midnight, so a
runner left on UTC keys 16 August as `00:00 UTC` while a PC on CEST keys it as
`00:00 CEST` — two rows, same day, forever, with sparklines drawing each day
twice. Nothing errors; it just quietly accumulates.

The workflow exports it as `TZ` before scanning, and the manifest carries the
publisher's UTC offset so `pull` warns loudly if the two ever drift apart.

### What stays local

Two things live on your PC and nowhere else, so `pull` carries both across
rather than replacing the database wholesale:

- **`inventory`** — read from your SavedVariables by `addon_import.py`. Losing
  it would silently re-score every craft as though you own nothing.
- **Price history the publisher does not have.** Past auction snapshots cannot
  be re-fetched once Blizzard moves on, and the publisher's own window starts
  the day it first ran — so a cutover would otherwise throw away everything
  before it.

History is carried a **whole day at a time**, and only for days the publisher
has no data for. A day it does cover always wins: splicing local rows into a
day the publisher also scanned would produce a row that is half one machine's
view and half another's. Carried days still respect `history_days`, so this
cannot smuggle back history retention is meant to have dropped.

The swap is by rename, so an interrupted pull leaves the old database intact.
On Windows a rename onto an open file fails outright, so `pull` retries for a
few seconds and then tells you to close `pricecheck` — leaving the old database
untouched and exiting non-zero rather than reporting success over stale prices.

Keep running `addon_import.py` locally for the inventory side; it is unaffected.

`test_cloud.py` covers the whole round trip — publish, serve, pull — against a
fake API and a local HTTP server, including the inventory hand-off, a corrupt
download, and a *damaged local database*: it empties a primary-key index by
hand (valid page, no rows) and asserts the pull spots it, rebuilds a clean copy
to read through, and still completes.

---

## What the sell price actually is

A single scan's price is a single moment. One seller undercutting hard for an
hour, or a thin patch overnight, moves it a long way — which is why the sell
side is **smoothed across the stored history**, weighted towards recent days.
Weight halves every two days, so over a seven-day window the last three days
carry about 71% of it and today still dominates without being the only voice.

This is the same idea as TradeSkillMaster's `DBMarket`, and the underlying
statistic already matched: TSM describes market value as "roughly around the
15th percentile" of listings, which is exactly what `SELL_PERCENTILE = 0.15`
here has always computed. What was missing was averaging it over time.

Set `"price_basis": "current"` in `config.json` for the old behaviour — this
scan's reading alone.

**Only the sell side is smoothed.** Reagent costs come off the live supply
ladder, because that is what you would actually pay this minute; averaging them
would quote a bill nobody can settle.

## Buy the reagent, or make it?

A reagent is costed at the cheaper of its market price and what it costs to
craft — TSM's `Crafting` price source, and it moves things. On a full scan:

| | |
|---|---|
| recipes sourcing at least one reagent by crafting | 874 (14%) |
| median cost reduction on those | 13% |
| crafts that flip from loss to profit | 73 |
| recipes priceable *only* because a reagent can be made | +208 |

Those last ones had a reagent nobody was selling, so they could not be costed
at all before.

The chains it finds are the ones you would pick yourself — *Transmute: Primal
Might*, *Spellcloth*, *Smelt Khorium* — which is the best evidence it is
working rather than compounding noise.

**Why it refuses some substitutions.** Blizzard's reagent lists are incomplete
on modern tiers, so a sub-craft's cost can be understated — and substituting an
understated cost makes the parent look cheaper and its margin better. Errors
compound in the flattering direction, the one that loses money. So a recipe
whose own cost is only a floor (it has optional or finishing slots) is never
used to price anything above it; that reagent is simply bought.

Depth is capped at three, cycles terminate rather than hang (transmutes that
convert both ways will produce one), and quantities round up because you cannot
half-craft. Set `"source_reagents"` off in the code if you want the old
market-only costing; `compute_margins(..., source_reagents=False)`.

Rows using it are badged **"sourced by crafting"**, and the reagent bill names
the sub-craft and what it saved — so the number is checkable rather than merely
lower.

---

## What to craft, and how many

A margin is what one craft pays **if it sells**. Ranking on it alone puts a
+800% craft that shifts a unit a fortnight above a +12% craft that shifts forty
a day, which is backwards — the second one is the business. So the table ranks
on **Gold/day**, and says how many to make.

```
gold/day = margin per unit  ×  units a day the market takes  ×  your share of it
Craft    = that, over cover_days, less what you already hold
```

Both numbers are on every row, and hovering either shows that arithmetic filled
in with the row's own figures, so it can be argued with rather than believed.
`--rank margin` restores the old ordering.

**Your share is modelled, not measured.** It is `1 / (listings + 1)` — you as
one more seller among those already posted, so ten listings gives you a tenth
and a crowded market promises you less without any tuning. The alternative, a
flat percentage, is wrong in both directions at once: too generous on staples,
too stingy where you are the only crafter. It is still a model. One seller
holding five postings reads as five competitors, and undercutting hard takes
more than your share. `--market-share 0.25` overrides it, `market_share` in
`config.json` sets it permanently.

**The sale rate is capped, because it counts cancellations as sales.** The rate
itself comes from individual auctions and is sound in principle — a posting
that survived with fewer units on it was bought from, one that vanished with
hours still to run cannot have expired — but nothing separates a cancelled
auction from a sold one, and undercut wars cancel constantly. Measured on a
live seven-day database of 30,170 items:

| | |
|---|---|
| median item's daily turnover of its standing supply | 4% |
| items under one full turnover a day | 93% |
| worst offender | Leylight Shard, 2.1M units/day against 200k listed across 88 postings |

Ten turnovers a day is not demand, it is the same few stacks being pulled and
reposted. So the rate is held at one full turnover of standing supply a day,
which leaves the honest 93% untouched, and rows where it bit say **capped**.
Of the 6,894 crafted outputs with a measured rate, 15% are capped and 21% read
zero — nothing left the market at all across the whole week.

`demand_cap_turnover` in `config.json` changes the multiple; 0 turns it off.

*Every figure in this section was measured under `sale_basis: "likely"`, which
is no longer the default — see the next section for what the ladder test did to
them.* They are left here because the cap was designed against them, and because
the size of the correction is the point.

**The cap is a bound, not a fix — so the real one is being measured.** A
commodity ladder is consumed from the cheapest end, which gives a test the
snapshot can actually answer: take the cheapest posting that survived *both*
scans, and count only the units that vanished from **below** it. Anything that
went while a cheaper listing stood there untouched was not bought — the buyer
would have taken the cheaper one first — so what is left is a cancellation.

Every scan now records that figure as `sold_swept` alongside the older
`sold_likely`, and prints how much of the hour's "sold" units the ladder test
accounts for and how much of it left a cheaper listing standing — the second
number being undercut churn counted as trade. The same comparison over the
whole stored window is one line further down, and `Store.sale_signal_summary`
answers it from the columns for any database at any time.

**The figure came in, and it is large.** A week on Argent Dawn EU, 30,166 items:

| | units | share |
|---|---:|---:|
| confirmed partial sales | 24,554,586 | — |
| vanished with hours left (`sold_likely`) | 447,687,569 | 100% |
| of those, swept from below a survivor (`sold_swept`) | 141,634,769 | 31.6% |

**68.4% of what was being counted as sales was not a sale.** `sold_likely` was
overstating demand by about 3.2×, and gold/day is `margin × demand × share`, so
every projection on the dashboard was inflated by roughly the same factor.

`sale_basis` therefore now ships as `"swept"`. A scan that finds no whole day of
the ladder test behind it falls back to `"likely"` and says so rather than
emptying the table.

**Why the per-item measurement and not a flat 0.32 haircut on `likely`.** The
correction is not a constant. Across the 11,543 items where both signals are
positive, `swept ÷ likely` runs p10 **0.06**, median **0.34**, p90 **1.00** —
some markets are almost pure undercut churn and others are almost pure trade.
One average applied to both would be wrong in opposite directions at once. The
per-item figure is already measured, so there is no reason to use the average.

**What changes on screen.** The same 30,166 items keep a measured rate — the
switch costs no coverage. Of the 18,912 that read as moving under `likely`,
11,543 still do; **7,369 drop to exactly zero**. That is not a gap in the data,
it is the answer: units left those markets, and none of it looked like anyone
buying. Those are the crafts to stop making, and `/wccraft` hides them by
default while still separating them from *never measured*.

**The cap is now a guard rather than a crutch.** `demand_cap_turnover` at 1.0
clamped 12.3% of moving items under `likely`; under `swept` it clamps 4.2%.
Demand-to-supply falls from a median of 0.11 to 0.06, p99 from 6.67 to 2.41. It
is left at 1.0 — it still catches the outliers a one-off bulk buy produces —
but it is no longer quietly doing the work the signal should have been doing.

**Three known limits, and the scan prints the first one every run.**

The test is honest on commodities and weak on realm gear, so the churn figure
is reported **split by source**:

```
commodity  churn  68.4%  (141,615,317 swept of 447,451,594 vanished, 24,554,586 confirmed partial)
realm      churn  91.8%  (     19,452 swept of     235,975 vanished,          0 confirmed partial)
```

Commodity buying goes through the client's own cheapest-first purchase call, so
"gone from below a surviving cheaper listing" is *literally* what a buyer does.
Realm auctions are picked one at a time, and two postings of one item id can be
different bonus-list variants at honestly different prices — a buyer taking the
dearer one is recorded here as a cancellation. Realm volume is 0.05% of
commodity volume, which is the only reason that weakness is tolerable rather
than disqualifying; treat a gear rate as the weakest number on the page. (The
zero confirmed partials are not a bug: a realm auction is one item, so there is
no such thing as a partial sale of it.)

Second: an item whose *entire* ladder turned over inside one hour is left out
rather than counted — one buyer clearing the lot and one seller pulling
everything leave the same trace.

Third, and it follows from the design: `sold_swept` is a **lower bound**. It can
only see a sale that left a cheaper listing standing. The truth is somewhere
between it and `sold_likely`, nearer the swept end on deep ladders. Both columns
are recorded on every scan regardless of the setting, so switching back costs
nothing and loses no history.

**The restock target fails towards crafting nothing.** No sale rate means no
target rather than a target of zero — the two are not the same and the ranking
keeps them apart, with unmeasured crafts sorted below every measured one. A
craft that loses money gets no target however fast it moves. Anything slower
than one unit across the cover window rounds to nothing to make now, not to
one. What you already hold, from the addon's inventory export, is deducted.

`--cover-days N` sets the window (default 3). Short is deliberate: an undercut
war or a patch should not catch you holding a month of inventory.

**Why the deduction happens in the client rather than in the scanner.** Both
halves of it — what you hold and what you have listed — are things the game
knows and the database only remembers. `GetItemCount` covers bags, bank and
reagent bank as they are right now; the exported inventory is as fresh as your
last `/reload`. And `pull` downloads a `PriceData.lua` built in the cloud,
where none of your stock exists, so a file that arrived already deducted would
mean "deducted by nobody". Doing it on display fixes both, and keeps the
subtraction in one place so it cannot be applied twice.

The dashboard still deducts on its own, from the exported inventory and
listings, because it is a page and has no client to ask.

**Your own listings are deducted too, when the addon has read them.** Stock
you have already crafted and listed is in neither your bags nor your bank, so
until recently a craft posted in the morning was suggested again in the
afternoon. `auctions.lua` reads `C_AuctionHouse.QueryOwnedAuctions` whenever
you open the auction house and `addon_import.py --apply` stores it; restock
then subtracts what you hold *and* what you have listed.

Two limits, both stamped rather than papered over. Owned auctions are only
readable with the auction house open, so that half is as fresh as your last
visit — and a reading older than 48 hours, the longest an auction can live, is
**ignored** rather than trusted, because deducting listings that have since
sold or expired would suppress crafting you actually need to do. A sold but
uncollected auction is not counted either: it is off the market and not in
your bags, so treating it as stock would suppress a restock twice over.

A read that finds nothing listed is still recorded as a read. "Checked twenty
minutes ago, you have nothing listed" and "your listings have never been read"
are different facts, in the same way a measured zero and an unmeasured item
are.

**What it still cannot see.** Whether you can hit the crafting quality that
sets the price, and that a transmute-sourced reagent is limited to one a day. And the forecast inherits every flaw in the
price under it: on an output with one or two listings the margin is mostly
somebody's asking price, and multiplying a fiction by a sale rate produces a
larger fiction. Those rows are badged **thin market** and there is a *Liquid
markets only* checkbox to put them aside, but they are not hidden — thin is not
the same as wrong, and transmog markets are genuinely thin.

Treat Gold/day as a better question than "which margin is biggest", not as an
answer.

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
optional slots are a choice, not a shopping list. It opens on the current
expansion — read from the newest cached skill tier, not named in the source, so
it follows the game — with the other eleven a click away.

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

**The listing deposit is deliberately not subtracted.** It is refunded when the
item sells, and margin is the answer to "what do I make if this sells", so
taking it off would understate every successful craft. It is only ever lost on
an auction that expires unsold — the case this tool already declines to predict
— and it is small next to the cut: on a 3g15s commodity listing the deposit is
2s, about 0.6% against the 5% the cut takes. It does tie up capital if you list
in bulk, which is worth knowing but is not a margin.

**Bid-only auctions are ignored** — you cannot reliably buy them, so letting them
drag the market price down would be misleading.

**The craft table opens on the current expansion, and guarantees each
expansion a slice.** Ranking purely by margin puts old-world gear on top — of
the best 200 crafts, three were Midnight — so filtering to current content
would have shown an almost empty table. Each expansion gets 25 rows reserved on
top of the global ranking, which is what makes the dropdown worth having.

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
- **Which item level a craft produces.** A separate ambiguity from the one above:
  the same item id is listed on the auction house under several *bonus lists* —
  different item levels or qualities — and the recipe endpoint never says which
  one a craft makes. They are therefore priced together, and since the sell price
  is a low percentile of the pooled ladder, it lands at the cheap end. Measured on
  a full scan: 11% of crafted outputs have more than one variant, and the pooled
  price sits at the cheap end **3,755 times against 21 at the dear end** — so this
  understates revenue rather than inventing it. It makes the tool *miss*
  profitable crafts, not recommend bad ones.

  Where the gap is big enough to change a decision, the row is badged
  **"revenue is a floor"** and the tooltip says what the dearest variant would
  pay. Those rows are also given a guaranteed place in the table, because the
  understated price is exactly what would otherwise rank them out of sight.

  The dearer variant must have **at least two listings** before it is quoted. It
  turned out 79% of the apparent effect was a single seller fishing for a
  mistake — the first version of this badge reported that a lone 2.6M listing on
  old PvP gear made a craft worth 2.48M. On a live scan the honest count is 7
  rows out of 5,919, three of which show a loss that may not be one.
- **How much sold — with caveats, not none.** The API publishes what is
  *listed*, never what changed hands, so sales have to be inferred. The
  aggregate quantity cannot do it: it moves for postings and cancellations too,
  and an early version of this column summed its falls and printed the result
  as units per day. That was nonsense — **15% of items "sold" more than their
  entire standing supply within seven hours**, one of them 1,200 units of an
  item with a single listing. A one-sided sum over a noisy series measures
  volatility, not trade.

  Individual auctions can do it, because each carries an id. Two signals, and
  both are needed because each is blind to half the market:

  - **a listing that shrank** — the same auction id with fewer units on it.
    Nobody partially cancels a posting, so this is a purchase and nothing else.
    Only ever fires on commodities, where one posting holds a stack.
  - **a listing that vanished with hours still to run** — `time_left` was
    `LONG` or `VERY_LONG`, so it cannot have expired within an hourly scan. The
    only signal that works on gear, where an auction is one item that goes
    whole or not at all.

  What remains uncertain is **cancellation**, which is indistinguishable from a
  sale in the second signal. Auctions that vanish on `SHORT` are not counted at
  all, since those may simply have run out.

  That uncertainty has been measured rather than guessed at. Across two live
  commodity snapshots an hour apart — 348,927 auctions against 339,173 — 24% of
  the disappearances had an identical relisting appear in the same interval,
  but those accounted for only **5% of the units**. Reposting concentrates in
  small postings; the large disappearances are unpaired and look like real
  trade. So the figures run about 5% high, and that is a lower bound, since a
  seller who cancels 500 and reposts it as two lots of 250 leaves no matching
  pair.

  Subtracting them is deliberately *not* done: pairing on quantity would also
  remove genuine sales that happen to coincide with a new posting of the same
  size, and trading a measured 5% overcount for an unmeasured false-negative
  rate is a poor bargain.

  The same snapshots explain why both signals are needed. Only 795 of 308,737
  surviving auctions had fewer units on them — 0.26% — because commodity buying
  consumes postings whole from the cheapest end rather than nibbling them. On
  its own, the confirmed signal would measure almost nothing.

  The rate divides by the market time actually observed, taken from Blizzard's
  own `Last-Modified`, so a dropped cron slot slows accumulation rather than
  corrupting the figure. Nothing is shown until six hours are behind it.
- **Cooldowns.** Reagent costs take the cheaper of buying a reagent or making
  it (see below), and the biggest savings it finds are transmutes — which are
  precisely the crafts limited to one a day. Nothing in the API says so. A row
  badged **"sourced by crafting"** whose saving depends on twenty transmutes is
  a twenty-day plan, not a shopping list.
- **Inspiration, resourcefulness, multicraft.** All of these move real profit and
  none are visible to the API. They generally push margins *up*.
- **Crafting orders.** Personal and patron orders often beat the open market
  entirely, and aren't in the API at all.
- **Whether it sells — now estimated, still not known.** A margin is a
  listing-price difference. The **Gold/day** and **Craft** columns turn it into
  a projection using the measured sale rate and a modelled share of the market
  (see *What to craft, and how many* above), which is a better question than
  margin alone — but it rests on a rate that cannot tell a cancellation from a
  sale and a share that is arithmetic rather than observation. Supply and
  listing counts are still on the page so you can judge liquidity yourself.
- **Your skill level.** Every recipe in the tier is evaluated whether or not you
  can make it.

Treat a high margin as a lead to verify in-game, not as gold in the bank. If a
number looks too good, it usually means the item is thinly listed or quality-tiered.

## The quality-tier experiment (settled: it was not item level)

`doctor` section [7] can see that **7,748 of 29,610 priced items** carry more
than one bonus-list variant, and that variants of one item price wildly apart.
It could never say *why*, and the two candidate causes wanted opposite
responses: if the spread is **item level**, price each level as its own market
and the gap closes; if it is something else, modelling item level achieves
nothing.

So it was measured rather than argued about. TradeSkillMaster's
[BonusIdTool][bit] (MIT) computes item level from bonus IDs offline in Python,
which is exactly the shape the auction API hands us. It was vendored, verified
against upstream across 3,005 cases with zero mismatches, wired into [7] behind
a pre-registered verdict — *if item level explains most of the spread, keep it;
if not, delete it* — and run against a live Argent Dawn scan.

[bit]: https://github.com/TradeSkillMaster/BonusIdTool

```
      5,593 item(s) price their variants over 10% apart
        item level accounts for it : 1,071 (19%)
        it does not               : 4,522 (81%)
      median spread across variants      : 6.83x
      median spread within one item level: 3.91x
```

**Item level explains 19% of it.** Grouping variants by item level moved the
median spread from 6.83x only to 3.91x — it removes 43% of the spread and
leaves the rest. On every one of the worst cases it removed *nothing*:

```
item 170112: 100,000.0x across 1 level(s), 100,000.0x within one of them
item 224599:  31,713.8x across 2 level(s),  31,713.8x within one of them
```

A hundred-thousand-fold range inside a single item level is not a valuation
difference at all — it is one seller's asking price against another's, on
variants that mostly have a single listing each. Which sharpens the conclusion
rather than weakening it: **the variant spread is largely listing noise, not a
signal about what the item is worth**, and no bonus-ID modelling would have
touched it.

So `bonusid.py` and its 264 KB data file **were deleted**, exactly as the
verdict said. The cost was an afternoon; the return is that nobody has to
wonder again. If you find yourself reaching for item-level modelling, this is
the result to read first.

What *is* still unmodelled: variants whose price differs by secondary stats, and
the fact that a variant with one listing has no market price at all — only an
asking price. `VARIANT_MIN_LISTINGS` already refuses to price those; the numbers
above are a reminder of why.

---

## Linting the addon

The addon's Lua is checked by [wowlua-ls][wls], a language server built for WoW
Lua rather than a general one with stubs bolted on. It is **not vendored** — it
is GPL-3.0 and this project is not, so it is used as a tool and nothing more,
which carries no obligation.

[wls]: https://github.com/TradeSkillMaster/wowlua-ls

```bash
wowlua_ls check addon/WowCraftExport --severity hint
```

Binaries are on its releases page; on Windows take
`wowlua_ls-x86_64-pc-windows-msvc.exe`.

`.wowluarc.json` in the addon folder declares the two globals it cannot
otherwise see — `WowCraftPrices`, which the generated `PriceData.lua` defines
and which is therefore never in the repo, and `WowCraftExport_Stock`, which
`auctions.lua` deliberately exports for `prices.lua` and `craft.lua` — and sets
`flavors: ["retail"]`, which is what turns on the `wrong-flavor-api` check.

The first run over 3,218 lines found no false positives and two real things:

- `trade.lua` had a `now()` function returning `GetTime()` (monotonic) and, in
  `redraw()`, a `local now = time()` (wall clock) shadowing it. Nothing called
  it in that scope yet, so nothing was broken — but anything later reaching for
  the throttle clock inside `redraw` would have called a number. The local is
  now `wallNow`.
- `main.lua` captured the addon vararg into an unused `ADDON` local.

**It is not in CI, on purpose.** The tool is beta by its own README, and a false
positive failing a build on correct code is worse than no linter. Run it by
hand; wire it in once it has been quiet for a while, and pin the version when
you do — a beta that gains diagnostics between runs will fail builds that
changed nothing.

---

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
| `pull` | Download a published scan instead of running one — no credentials |
| `seed` | Export the recipe cache for the CI workflow to cold-start from |
| `demo` | Run the whole pipeline on synthetic data, no credentials |
| `doctor` | Probe every endpoint *and* the pull side, write a shareable report |
| `repair` | Check the local database and rebuild it if sqlite reports damage |
| `names` | Look up names for every priced item that has none (one-off, ~6 min) |

Blizzard allows 36,000 requests an hour. A full `init` plus a full `names` is
most of that, so doing both in the same hour — or running two copies at once —
earns HTTP 429s. Nothing is lost when that happens: refused items are simply
skipped, reported separately from ones that genuinely have no name, and the
next `names` run fetches only what is still missing.

## Price lookup (`pricecheck.py`)

A small window you open and close, for "what is X going for" when you are
nowhere near an auctioneer.

```bash
python3 pricecheck.py
```

or double-click `pricecheck.cmd` on Windows, which launches it with `pythonw`
so no console sits behind it. It reads `wowcraft.sqlite3` directly and **keeps
itself current**: every thirty seconds it checks whether the scan has written,
reloads if so, and says which scan it picked up. Leave it open on a second
monitor and the numbers stay live without you touching anything. **Refresh**
forces it early.

It is dark on purpose: it sits open beside a game that is dark, and a white
grid at 11pm is why a tool like this gets closed. Direction is shown with an
arrow rather than by tinting the row, because a Treeview can only colour whole
rows and colouring by trend turned every line red or green - which is worse
than no colour at all. Rows are banded so the eye can follow one across seven
columns.

Your search, expansion and sort survive a refresh, a database locked by a scan
mid-write is ridden out rather than blanking the window, and the "prices from"
age is redrawn on the same timer — an age that freezes is worse than no age at
all, since the whole point of stamping it is to stop stale numbers passing for
current ones.

Run `python3 wowcraft.py names` once after your first scan. It fetches each
item's class as well as its name, from the same response, which is also what
puts gathered goods on tooltips in game. `init` only names what recipes
reference, which leaves most of the auction house showing as bare ids — fine on the dashboard, which hides them, but this window shows everything
and "item 274470" answers nothing. It looks up only what is missing, so a
second run costs nothing, and the names come from Blizzard's own item endpoint
rather than from scraping a database site.

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

### Building it as an .exe

```
build-exe.cmd
```

Produces `dist\pricecheck.exe`, about 12 MB, needing no Python installed.
PyInstaller is the only third-party dependency in the project and it is a
**build** dependency only — nothing that runs needs it, and the scanner still
has none. `python -m pip install pyinstaller` if the script says it is missing.

The exe looks for `wowcraft.sqlite3` **beside itself**, then in the working
directory, and names both paths if it finds neither. That matters more than it
sounds: packed with `--onefile` the script is unpacked into a temporary folder
that is deleted on exit, so anything resolved relative to the source file would
point somewhere meaningless. Keep the exe next to the database, or pass
`--db <path>`.

Being a windowed build it has no console, so a failure to start would otherwise
be a program that simply never appears; startup errors are shown in a message
box instead.

`build/`, `dist/` and `*.spec` are gitignored — the exe is a build artefact
that changes with every commit and rebuilds in twenty seconds.

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
python3 test_undercut.py    # the undercut helper, including that it never posts
python3 test_pricecheck.py  # the lookup window, including picking up a scan while open
python3 test_restock.py     # gold/day and the restock quantity
python3 test_velocity.py    # the sale-rate signals, including the ladder sweep
python3 test_sourcing.py    # buy-or-craft for reagents
python3 test_craft.py       # /wccraft, against a stubbed client
python3 test_shop.py        # /wcshop, including that it buys nothing
python3 test_auctions.py    # reading your own listings, including that it posts nothing
python3 test_cloud.py       # publish/pull round trip, and database repair
```

`test_addon.py` needs `lupa` (`pip install lupa`) to run the addon's Lua for
real; without it the file skips rather than failing, so the scanner itself
still has no third-party dependencies.

339 assertions in total: the supply ladder, percentile pricing,
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
- **what the craft is worth a day, and how many to make** — the same two
  numbers the dashboard ranks on, on the tooltip and on the crafting window,
  because standing at the crafting table is where "how many" actually gets
  decided. `PriceData.lua` carries what the *market* wants and the addon
  subtracts your own stock from it live, so "make 3 (of 12, you have 9)"
  counts bags, bank and reagent bank to the second — and works whether the
  file was built by your own `scan` or downloaded by `pull` from a cloud that
  has never seen your bags
- a line on the crafting window showing cost, sale price and margin for the
  recipe you have open

The forecast has three states in game and they are deliberately not alike: a
figure and a quantity when there is a measured sale rate, *nothing selling*
when the rate is a measured zero, and **no line at all** when the output has
never been measured. A craft nobody has been seen buying and a craft nobody has
looked at are different things, and neither is worth zero gold a day.

The file carries every item the recipe cache references — reagents, slot fills
and crafted outputs — plus every **tradeskill item**, whether or not a recipe
uses it. That second group exists because being a reagent and being worth money
are different things. A patch's new fish is the plain case: you catch
Many-Eyed Flounder by the hundred and sell every one, but nothing cooks it, so
on the recipe set alone its tooltip is blank at exactly the moment you want a
price. Tradeskill items are about 2,900 ids and cost ~60 KB; writing all 29,000
priced items instead would roughly double the file the client parses at every
login, to buy prices for gear nobody crafts with.

Item classes come from `names`, so **until you have run it, gathered goods have
no price in game** and `scan` says how many items are waiting on it.

Addons cannot read files at runtime, only at load, so **what you see is as
fresh as your last `/reload`**. Every number is stamped with its age for that
reason — "22m ago" rather than a bare figure pretending to be current.
`/wcprices` reports what is loaded.

That age is the age of the **prices**, taken from Blizzard's own
`Last-Modified`. It used to be stamped with `taken_at`, the day bucket the
rest of the pipeline keys history on — which is local midnight, so the tooltip
was quietly reporting the hours since midnight instead: "4h ago" at four in
the morning on data twenty minutes old, and "23h ago" late at night, on the
same data. `pricecheck.py` reads `last_data_time` for exactly this reason and
always has; the addon writer had never been given it.

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

### What to make (`/wccraft`)

The dashboard ranks every craft the scanner can price — around six thousand
rows — and you can make perhaps two hundred of them. `/wccraft` is the
intersection: the crafts **this character has learned**, ranked by gold a day,
with the quantity to make.

```
/wccraft           the list
/wccraft all       include losses, and outputs nothing has been seen buying
```

It reads two tables that are already in the client — the margins from
`PriceData.lua` and the learned-recipe set `trade.lua` keeps up to date
whenever a profession window is open — so it fetches nothing, scans nothing,
and calls nothing protected. Clicking a row links the item in chat. As
everywhere else in this addon, it never posts and never whispers.

The ordering is the dashboard's, for the same reasons: fully costed crafts
above floor-costed ones, measured sale rates above unmeasured ones, then gold
a day, then margin. Each row says which of those it is — a figure with a
quantity, *no sales seen*, *no rate*, or the `floor` badge — because a ranked
list in the middle of the screen reads as an instruction, and these numbers
are a projection.

**Two things are hidden by default and the header says so**: crafts that lose
money, and crafts whose output has not shifted a single unit all week. A
fantasy margin on something nobody buys is exactly what the gold/day ranking
exists to demote, and demoting it to the bottom of a twenty-row window is the
same as showing it. The count of what was hidden, and the command to see it,
are in the header — a filter nobody knows about is indistinguishable from
missing data.

An empty list always says which of three different things is wrong: no price
data at all, no professions exported yet, or nothing you can make being worth
making at today's prices.

### What to buy (`/wcshop`)

`/wccraft` says what to make. `/wcshop` says what that takes: every reagent the
worth-making crafts consume, summed, netted against what you already hold, and
priced.

```
/wcshop            the list
```

It asks the auction house for nothing — no scan, no search, no purchase.
Clicking a row types the reagent's name into the search box and stops there,
the same way the undercut panel writes a price and leaves *Create Auction* to
you. Away from the auction house it links the item in chat instead.

Four things go into it and all four are already on the client: what to make
and how many (`PriceData.lua`, less your own stock), what one craft consumes
(recorded by `trade.lua` whenever a profession window is open), what you hold
(live), and the cheapest price from the last scan.

Details that are easy to get wrong and are therefore tested:

- **Yield.** A recipe that makes two a craft needs half as many runs, so
  `PriceData.lua` carries the crafted quantity and the list divides by it
  before multiplying anything.
- **Shared stock is netted once**, against the whole list rather than per
  craft. Two recipes wanting the same herb share the stack in your bag;
  subtracting it from both would send you shopping for twice what you are
  short of.
- **Basic reagents only.** Optional and finishing reagents are a choice made
  at the crafting table, and a shopping list that told you to buy them would
  be inventing a decision on your behalf.
- **The cheapest legal quality** is the one costed, which is the same
  assumption the scanner makes when it prices a slot — and carries the same
  caveat, since a better reagent costs more and crafts better.

The header says **runs, not crafts** — "108 runs of Elixir of Minor
Fortitude", because "108 crafts" reads as a hundred and eight different things
to make. One recipe is named outright; more than one would not fit, so the
count stands in and **hovering the summary** lists every recipe with its run
count. Hovering a reagent names the crafts that want it: a hundred of
something is worth knowing the name of before spending two hundred gold on
herbs for it.

Crafts whose reagents have never been recorded are **named**, not merely
counted — "Mystery Brew has no reagents recorded". A count on its own cannot
be acted on, and the usual cause is obvious once it has a name: a second
profession whose window has not been opened on that character this session.

The total is an estimate and says so. Every quantity descends from the restock
projection, so a shopping list multiplies that projection's error by a reagent
count. Crafts whose reagents this character has never recorded are counted in
the header rather than quietly left out.

### Undercutting at the auction house

Open something to sell and a small panel appears beside the auction house
showing what it is currently going for and what your undercut would be. **Use
price** writes that number into the price box; you press Create Auction
yourself.

```
/wcundercut        what it is set to
/wcundercut 3      undercut by 3% instead of the default 5%
/wcundercut auto   fill the box automatically, no button
```

Two deliberate limits. It reads **live listings**, not the hourly scan —
undercutting a fifty-minute-old price is how you end up undercutting nobody,
and the client already holds current listings for whatever you have open. And
it **never posts**: setting an edit box is unprotected, driving the post is not,
and that is where taint and "Interface action failed" come from. The test suite
fails if a posting function is ever called.

It works on both sell frames, but gear is not commodities and is handled
differently in three ways that all matter. Listings are looked up by the sell
frame's own **item key**, not by bare item id, so your 691 is not compared
against someone else's 675. Item results are **not** sorted cheapest-first the
way commodity ones are, so the minimum is taken rather than the first row. And
bid-only listings are ignored — a price nobody can buy at is not a price to
undercut. The number is written into the **buyout** box, never the starting bid.

An item nobody has listed says so rather than inventing a price.

The suggestion is **rounded down to whole silver**, because the auction house
price box has no copper field — a price with copper in it is not one you can
post. Rounded down rather than to nearest, so the result is still an undercut.

Setting the box also fires its change events. The sell frame validates on those,
so a value written behind its back leaves *Create Auction* refusing to work — it
looks like the price took when it did not. `/wcundercut debug` reports which
setter worked and what the frame exposes, if it ever stops matching a future
client.

### Trade requests you can fill

People link the item they want made, so there is no text matching to do: the
addon pulls the itemID out of the link and checks it against what **this
character has learned**. Nothing fires unless you can actually make the thing.

Open a profession window once per character and run `/wctrade learn` (or just
open it — a profession window refreshes the set on its own).

That refresh is **throttled and spread across frames**, because it is not
cheap: it asks `GetRecipeInfo`, `GetRecipeSchematic` and
`GetCraftingOperationInfo` (twice, for the concentration case) for every
recipe the character knows. It used to run in full on every
`TRADE_SKILL_LIST_UPDATE` — an event the client fires on **every keystroke in
the profession search box** — so typing a recipe name walked several thousand
recipes per letter and stuttered the game in the one window whose whole
purpose is typing into it. It now walks at most once every ten seconds, forty
recipes per frame. Nothing is lost by the delay: what you know does not change
while you type. After that, a
linked request in trade prints a line with the buyer's name, the item, and what
the mats cost you, and lists it in a small movable window. Click a row to open
a whisper box addressed to them.

**It never sends anything.** Clicking fills in the whisper and leaves the
cursor to you. Automated whispering is a spam-policy problem and would buy
nothing — the value is in noticing the request, not in saving a keystroke.
`/wctrade` shows what is being watched, `/wctrade clear` empties the list.

**It never asks the auction house for anything on its own**, and that is the
design rather than an omission. The first version queried owned auctions on
`AUCTION_HOUSE_SHOW` — the same moment Blizzard's own UI issues the browse
query that restores your last search — and the auction house runs one
throttled query at a time. The two raced, the browse reply never arrived, and
the window sat on *Searching…* until it was closed and reopened. An addon that
collects data has no business breaking the window it collects from.

So it listens instead. `OWNED_AUCTIONS_UPDATED` fires whenever the client
refreshes your listings, which it does by itself when you open your **Auctions
tab** to look at them, and whatever was in that refresh is what gets recorded.
The cost is that the reading is as fresh as the last time you looked at your
own auctions rather than the last time you walked past an auctioneer.

`/wcauctions` reports what was last read and when, and will force a refresh —
but only with the frame open and only when nothing else is in flight, and it
says which of those stopped it rather than failing quietly. Like everything
else here it only ever reads: nothing posts, cancels or bids, and the test
suite fails if a posting function is ever called.

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
