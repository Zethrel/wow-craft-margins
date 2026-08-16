#!/usr/bin/env python3
"""Price lookup — a small desktop window over the prices `scan` already stores.

Open it, type part of an item name (or an item id), read the price. It reads
wowcraft.sqlite3 directly, so it always shows whatever the last scan wrote; the
hourly task keeps that current and Refresh picks up a new scan without
restarting.

Deliberately not part of the dashboard. That is a report you glance at; this is
a thing you leave open on a second monitor while you play, and putting eight
thousand searchable rows into the HTML made a four megabyte page that would not
load.

Tkinter only, which ships with Python, so there is still nothing to install.

    python pricecheck.py            # or double-click pricecheck.cmd
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wowcraft as W          # for expansion_of, so the two agree on names


def app_dir() -> str:
    """Where the program lives, as the user thinks of it.

    Packaged with --onefile, the script is unpacked into a temporary folder
    that is deleted on exit, so __file__ points somewhere useless and the
    database would never be found. Frozen, the answer is the folder holding
    the .exe."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_db(explicit: str = "") -> str:
    """The database to read, and a clear account of the search if there is
    none - "file not found" with no path is the least useful error there is."""
    if explicit:
        return explicit
    tried = []
    for folder in (app_dir(), os.getcwd()):
        candidate = os.path.join(folder, "wowcraft.sqlite3")
        if candidate in tried:
            # Run from its own folder these are the same place, and listing it
            # twice reads as a bug in the message rather than a missing file.
            continue
        tried.append(candidate)
        if os.path.exists(candidate):
            return candidate
    raise SystemExit(
        "Could not find wowcraft.sqlite3.\n\nLooked in:\n  "
        + "\n  ".join(tried)
        + "\n\nPut the program beside the database, or start it with"
          " --db <path>.")

GOLD = 10000


def gold(copper) -> str:
    if not copper or copper <= 0:
        return "-"
    g = copper / GOLD
    if g >= 1_000_000:
        return f"{g / 1_000_000:.1f}M"
    if g >= 1000:
        return f"{g / 1000:.1f}k"
    if g >= 10:
        return f"{g:,.0f}"
    return f"{g:.2f}"


def age(stamp) -> str:
    if not stamp:
        return "unknown age"
    mins = int((time.time() - stamp) / 60)
    if mins < 1:
        return "just now"
    if mins < 90:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


class Prices:
    """Everything the window needs, read in one go.

    A few tens of thousands of rows is nothing in memory and makes searching a
    list comprehension rather than a query per keystroke, which is the
    difference between instant and laggy."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.rows: list = []
        self.taken_at = None
        self.history: dict = {}
        self.expansions: list = []
        self.newest_expansion = ""
        self.stamp = None          # file mtime+size when we last read it
        self.load()

    def changed_on_disk(self) -> bool:
        """Has the scan written since we last read?

        Cheaper than reopening the database on a timer, and it does not matter
        if it is momentarily wrong: the next tick catches it."""
        try:
            info = os.stat(self.db_path)
        except OSError:
            return False
        return (info.st_mtime, info.st_size) != self.stamp

    def load(self) -> None:
        if not os.path.exists(self.db_path):
            raise SystemExit(
                f"No database at {self.db_path}.\n"
                "Run `python wowcraft.py scan` first, or pass --db.")
        try:
            info = os.stat(self.db_path)
            self.stamp = (info.st_mtime, info.st_size)
        except OSError:
            self.stamp = None
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT MAX(taken_at) AS t FROM price_snapshot").fetchone()
        self.taken_at = row["t"] if row else None
        if not self.taken_at:
            raise SystemExit("The database holds no prices yet. Run a scan.")
        # taken_at is the day bucket - midnight - so it would report this
        # morning's scan as ten hours old. The real age of the data is
        # Blizzard's own Last-Modified, which scan records as it goes.
        meta = db.execute("SELECT value FROM meta WHERE key='last_data_time'").fetchone()
        try:
            self.data_time = int(meta["value"]) if meta else self.taken_at
        except (TypeError, ValueError):
            self.data_time = self.taken_at

        # Items carry no expansion of their own, so it comes from the recipes
        # that use them: anything a Midnight recipe consumes or produces is a
        # Midnight item. An item can belong to several - Classic herbs still
        # turn up in modern recipes - so this is a set per item rather than a
        # single label, and the filter asks "is it used in X".
        item_expansions: dict = {}
        newest = (0, "")
        for r in db.execute(
                "SELECT skill_tier_id, skill_tier_name, profession_name, "
                "crafted_item_id, reagents_json, slots_json FROM recipe"):
            expansion = W.expansion_of(r["skill_tier_name"] or "",
                                       r["profession_name"] or "")
            if (r["skill_tier_id"] or 0) > newest[0]:
                newest = (r["skill_tier_id"] or 0, expansion)
            ids = set()
            if r["crafted_item_id"]:
                ids.add(r["crafted_item_id"])
            for reagent in json.loads(r["reagents_json"] or "[]"):
                ids.add(reagent["id"])
            for slot in json.loads(r["slots_json"] or "null") or []:
                ids.update(slot.get("items") or [])
            for item_id in ids:
                item_expansions.setdefault(item_id, set()).add(expansion)
        # Newest skill tier wins, so this follows the game rather than needing
        # editing every expansion.
        self.newest_expansion = newest[1]
        self.expansions = sorted({e for s in item_expansions.values() for e in s})

        names = {r["id"]: r["name"] for r in db.execute(
            "SELECT id, name FROM item WHERE name IS NOT NULL AND name <> ''")}

        # Every stored day, so the window can show where a price has been.
        for r in db.execute("SELECT item_id, taken_at, sell_unit_price "
                            "FROM price_snapshot ORDER BY taken_at"):
            self.history.setdefault(r["item_id"], []).append(
                (r["taken_at"], r["sell_unit_price"]))

        # One row per item. price_snapshot holds a row per source, and an item
        # listed both region-wide and on the realm would otherwise appear
        # twice at two different prices. Commodities win where both exist,
        # for the same reason `scan` prefers them: region-wide is deeper.
        best: dict = {}
        for r in db.execute(
                "SELECT item_id, sell_unit_price, min_unit_price, total_quantity, "
                "listing_count, buy_low, buy_high, source FROM price_snapshot "
                "WHERE taken_at = ?", (self.taken_at,)):
            item_id = r["item_id"]
            if r["source"] != "commodity" and item_id in best:
                continue
            # Unnamed items are kept, unlike on the dashboard: here you can
            # search by id, so a row you can find is a row worth having.
            name = names.get(item_id) or f"item {item_id}"
            past = self.history.get(item_id) or []
            trend = None
            if len(past) > 1 and past[0][1]:
                trend = (past[-1][1] - past[0][1]) / past[0][1] * 100.0
            best[item_id] = {
                "id": item_id,
                "name": name,
                "search": name.lower(),
                "buy": r["min_unit_price"] or 0.0,
                "sell": r["sell_unit_price"] or 0.0,
                "supply": r["total_quantity"] or 0,
                "listings": r["listing_count"] or 0,
                "low": r["buy_low"],
                "high": r["buy_high"],
                "trend": trend,
                "source": r["source"],
                "expansions": item_expansions.get(item_id) or set(),
            }
        db.close()
        self.rows = sorted(best.values(), key=lambda x: -x["supply"])


# Black and yellow, after the logo. Dark because this sits open beside a game
# that is dark, and a white grid at 11pm is the reason people close a tool like
# this.
#
# Yellow is an accent, not a surface. A saturated yellow is the brightest thing
# a screen can do, and a window full of it is unreadable within a minute - so
# it marks the few things worth the eye: the row you picked, the field you are
# typing in, the headings you can click. Everything else is black, grey and the
# off-white that carries the actual numbers. (The sorted column is marked with
# an arrow rather than a colour: ttk styles all headings together, so there is
# no way to light up just one.)
#
# Backgrounds are warm-tinted rather than pure #000: on an LCD, true black
# beside a bright yellow makes the edges buzz.
THEME = {
    "bg":       "#0e0e0c",
    "panel":    "#181713",
    "row":      "#121210",
    "row_alt":  "#1a1a16",
    "sel":      "#f5e400",   # the logo yellow, as a selected row
    "sel_text": "#0e0e0c",   # black on it, because yellow cannot carry white
    "text":     "#eceade",
    "muted":    "#8d8a7c",
    "accent":   "#f5e400",
    "accent_dim": "#b3a600",  # yellow at rest, for borders and headings
    "line":     "#2c2b24",
    # Kept because they name meanings the window will want when it grows a
    # profit column: money, and direction. Tuned to sit with the yellow rather
    # than fight it - a pure #6cc06c green next to #f5e400 reads as a traffic
    # light. Not referenced yet; nothing here has a direction to show.
    "gold":     "#f5e400",
    "pos":      "#9fc95a",
    "neg":      "#e0784f",
}


def blend(base: str, tint: str, amount: float) -> str:
    """Mix `tint` into `base`. Both "#rrggbb", `amount` 0..1."""
    def parts(h):
        return [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    a, b = parts(base), parts(tint)
    return "#%02x%02x%02x" % tuple(
        min(255, max(0, round(x + (y - x) * amount))) for x, y in zip(a, b))


# Percent move before a row is worth marking, set from the data rather than
# from taste. Measured over a full table: half of all items move more than 22%
# in a day and a quarter move more than 50%. This is a volatile market, so a
# 20% bar is not "notable", it is Tuesday - it lit up a third of the screen,
# which is how an earlier attempt at trend colouring made the table unreadable.
# At 50% the tint marks roughly one row in seven and means something.
#
# Direction is on every row as an arrow regardless, so nothing here depends on
# seeing the colour - which matters, because the two tints differ mostly in
# hue and red/green at equal brightness is the one pairing colourblind readers
# cannot separate.
TREND_STRONG = 50.0

# How hard to tint. Strong enough to read as a colour cast across a wide row
# (1.8:1 against plain banding), gentle enough that the off-white text still
# sits at 8.5:1 on it.
TREND_TINT = 0.28


def style_window(root: tk.Tk) -> None:
    """Dark theme over ttk. Built on `clam` because it is the only stock theme
    that lets the Treeview's colours actually be set - the native Windows
    themes draw their own and ignore most of this."""
    root.configure(bg=THEME["bg"])
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        return
    style.configure(".", background=THEME["bg"], foreground=THEME["text"],
                    fieldbackground=THEME["panel"], borderwidth=0)
    style.configure("TFrame", background=THEME["bg"])
    style.configure("TLabel", background=THEME["bg"], foreground=THEME["muted"])
    style.configure("Status.TLabel", foreground=THEME["muted"])
    # The caret is yellow so you can find where you are typing on a black
    # field, and the focus border lights up rather than merely thickening.
    style.configure("TEntry", fieldbackground=THEME["panel"],
                    foreground=THEME["text"], insertcolor=THEME["accent"],
                    bordercolor=THEME["line"], lightcolor=THEME["line"],
                    darkcolor=THEME["line"], padding=5)
    style.map("TEntry",
              bordercolor=[("focus", THEME["accent"])],
              lightcolor=[("focus", THEME["accent"])],
              darkcolor=[("focus", THEME["accent"])])
    style.configure("TCombobox", fieldbackground=THEME["panel"],
                    background=THEME["panel"], foreground=THEME["text"],
                    arrowcolor=THEME["accent_dim"], bordercolor=THEME["line"],
                    padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", THEME["panel"])],
              foreground=[("readonly", THEME["text"])],
              bordercolor=[("focus", THEME["accent"])],
              arrowcolor=[("active", THEME["accent"])])
    # Buttons sit quiet until hovered, then go full logo: yellow plate, black
    # letter. Black on yellow, never white - white on this yellow is barely
    # a contrast at all.
    style.configure("TButton", background=THEME["panel"],
                    foreground=THEME["text"], bordercolor=THEME["line"],
                    padding=(10, 4))
    style.map("TButton",
              background=[("active", THEME["accent"])],
              foreground=[("active", THEME["sel_text"])])
    style.configure("Treeview", background=THEME["row"],
                    fieldbackground=THEME["row"], foreground=THEME["text"],
                    rowheight=22, borderwidth=0)
    style.configure("Treeview.Heading", background=THEME["panel"],
                    foreground=THEME["accent_dim"], relief="flat",
                    padding=(6, 5))
    style.map("Treeview.Heading",
              background=[("active", THEME["line"])],
              foreground=[("active", THEME["accent"])])
    style.map("Treeview", background=[("selected", THEME["sel"])],
              foreground=[("selected", THEME["sel_text"])])
    style.configure("Vertical.TScrollbar", background=THEME["line"],
                    troughcolor=THEME["bg"], bordercolor=THEME["bg"],
                    arrowcolor=THEME["muted"])
    style.map("Vertical.TScrollbar",
              background=[("active", THEME["accent_dim"])])


COLUMNS = (
    ("name", "Item", 240, "w"),
    # Shown because quality tiers are separate items with identical names -
    # two "Void-Tempered Scales" rows at different prices are not a bug, and
    # without the id there is no way to tell which is which.
    ("id", "ID", 70, "e"),
    ("buy", "Cheapest", 90, "e"),
    ("sell", "Realistic", 90, "e"),
    ("supply", "Supply", 90, "e"),
    ("listings", "Listings", 70, "e"),
    ("today", "Today", 120, "e"),
    ("trend", "Trend", 70, "e"),
)


class App:
    def __init__(self, root: tk.Tk, data: Prices, limit: int = 300):
        self.root, self.data, self.limit = root, data, limit
        self.sort_key, self.sort_desc = "supply", True
        root.title("wowcraft — price lookup")
        root.geometry("900x600")
        root.minsize(660, 380)
        style_window(root)

        top = ttk.Frame(root, padding=(12, 12, 12, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left", padx=(0, 8))
        self.query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        entry.focus_set()
        self.expansion = tk.StringVar()
        choices = ["All expansions"] + data.expansions + ["Not in any recipe"]
        picker = ttk.Combobox(top, textvariable=self.expansion, values=choices,
                              state="readonly", width=20)
        picker.pack(side="left", padx=(0, 8))
        # Default to whatever the newest skill tier belongs to - Midnight now,
        # and whatever follows it later without anyone editing this.
        self.expansion.set(data.newest_expansion or "All expansions")
        picker.bind("<<ComboboxSelected>>", lambda _e: self.render())
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side="left")

        self.tree = ttk.Treeview(root, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "name"))
        bar = ttk.Scrollbar(root, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.status = tk.StringVar()
        ttk.Label(root, textvariable=self.status, anchor="w",
                  style="Status.TLabel",
                  padding=(12, 6, 12, 10)).pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True, padx=(12, 0),
                       pady=(0, 4))
        bar.pack(side="left", fill="y", padx=(0, 12), pady=(0, 4))

        # A Treeview colours whole rows, never single cells, so the trend
        # column cannot be tinted on its own - Tk 8.6 has no per-cell
        # foreground and this is not a styling oversight to work around.
        #
        # Tinting the row by trend was tried and abandoned because every line
        # came out red or green. The fix is not to abandon the idea but to
        # raise the bar: only moves past TREND_STRONG shade the row, so the
        # majority stay on plain banding and the ones that shifted stand out.
        # Direction is still on every row as an arrow, which also means the
        # information does not depend on seeing colour at all.
        #
        # One tag per (band, direction) rather than layering two tags: Tk's
        # precedence when two tags both set a background is not something to
        # rely on.
        for band, bg in (("odd", THEME["row_alt"]), ("even", THEME["row"])):
            self.tree.tag_configure(band, background=bg)
            self.tree.tag_configure(
                "up_" + band,
                background=blend(bg, THEME["pos"], TREND_TINT))
            self.tree.tag_configure(
                "down_" + band,
                background=blend(bg, THEME["neg"], TREND_TINT))

        # Typing filters as you go; the whole set is in memory so there is no
        # need to debounce.
        self.refreshed_at = None
        self.query.trace_add("write", lambda *_: self.render())
        self.tree.bind("<<TreeviewSelect>>", self.show_history)
        root.bind("<Escape>", lambda _e: (self.query.set(""), entry.focus_set()))
        root.bind("<Control-f>", lambda _e: entry.focus_set())
        self.render()
        self.tick()

    # -- data ------------------------------------------------------------

    def matching(self) -> list:
        q = self.query.get().strip().lower()
        rows = self.data.rows
        chosen = self.expansion.get()
        if chosen == "Not in any recipe":
            rows = [r for r in rows if not r["expansions"]]
        elif chosen and chosen != "All expansions":
            rows = [r for r in rows if chosen in r["expansions"]]
        if q:
            # An id search is exact; a name search is a substring.
            if q.isdigit():
                wanted = int(q)
                rows = [r for r in rows if r["id"] == wanted or q in r["search"]]
            else:
                rows = [r for r in rows if q in r["search"]]
        reverse = self.sort_desc
        rows = sorted(rows, key=lambda r: (r[self.sort_key] is None,
                                           r[self.sort_key]
                                           if self.sort_key != "name"
                                           else r["search"]),
                      reverse=reverse)
        return rows

    def mark_headings(self) -> None:
        """Arrow on the column being sorted by.

        The headings are clickable and nothing said so, which on a dark grid
        is easy to miss entirely. An arrow rather than a colour because ttk
        styles every heading as one - there is no per-column foreground."""
        for key, title, _w, _a in COLUMNS:
            sorted_on = self.sort_key == key or (
                key == "today" and self.sort_key == "high")
            arrow = (" ▾" if self.sort_desc else " ▴") if sorted_on else ""
            self.tree.heading(key, text=title + arrow)

    def sort_by(self, key: str) -> None:
        if key in ("today", "trend"):
            key = "trend" if key == "trend" else "high"
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = key, key != "name"
        self.render()

    def refresh(self, automatic: bool = False) -> None:
        try:
            self.data.load()
        except SystemExit as exc:
            self.status.set(str(exc))
            return
        except sqlite3.OperationalError:
            # The scan is mid-write and holding the file. Nothing to do but
            # come back on the next tick; the old numbers stay on screen
            # rather than the window blanking.
            return
        self.refreshed_at = time.time() if automatic else None
        self.render()

    def tick(self) -> None:
        """Pick up a new scan on its own, and keep the age honest.

        The hourly task rewrites the database underneath this window. Polling
        the file's mtime every half minute costs nothing and means the numbers
        on screen are never quietly an hour old - which is exactly the failure
        the age stamp exists to prevent, so leaving it to a button would have
        been half a job."""
        try:
            if self.data.changed_on_disk():
                self.refresh(automatic=True)
            else:
                # Even with no new data the age is ticking, so redraw the
                # status line rather than letting it read "2m ago" forever.
                self.status_line()
        finally:
            self.root.after(30_000, self.tick)

    # -- view ------------------------------------------------------------

    def render(self) -> None:
        rows = self.matching()
        self.mark_headings()
        self.tree.delete(*self.tree.get_children())
        for index, r in enumerate(rows[:self.limit]):
            today = "-"
            if r["low"] and r["high"]:
                today = ("steady" if abs(r["high"] - r["low"]) < 1
                         else f"{gold(r['low'])} - {gold(r['high'])}")
            trend = "-"
            if r["trend"] is not None:
                if abs(r["trend"]) < 0.5:
                    trend = "flat"
                else:
                    arrow = "▲" if r["trend"] > 0 else "▼"
                    trend = f"{arrow} {abs(r['trend']):.0f}%"
            band = "odd" if index % 2 else "even"
            move = r["trend"] or 0.0
            if move >= TREND_STRONG:
                band = "up_" + band
            elif move <= -TREND_STRONG:
                band = "down_" + band
            self.tree.insert("", "end", iid=str(r["id"]), tags=(band,), values=(
                r["name"], r["id"], gold(r["buy"]), gold(r["sell"]),
                f"{r['supply']:,}", f"{r['listings']:,}", today, trend))

        self.shown_count = min(len(rows), self.limit)
        self.match_count = len(rows)
        self.status_line()

    def status_line(self) -> None:
        more = (f" (showing {self.shown_count})"
                if self.match_count > self.shown_count else "")
        where = self.expansion.get()
        scope = "" if where in ("", "All expansions") else f" in {where}"
        note = "updates itself"
        if self.refreshed_at and time.time() - self.refreshed_at < 120:
            note = ("picked up a new scan "
                    + time.strftime("%H:%M", time.localtime(self.refreshed_at)))
        self.status.set(
            f"{self.match_count:,} of {len(self.data.rows):,} items{scope}{more}"
            f"   |   prices from "
            f"{time.strftime('%d %b %H:%M', time.localtime(self.data.data_time))}"
            f", {age(self.data.data_time)}   |   {note}")

    def show_history(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        item_id = int(selected[0])
        past = self.data.history.get(item_id) or []
        if len(past) < 2:
            self.status.set(f"item {item_id}: only one day stored so far")
            return
        trail = "  ".join(
            f"{time.strftime('%d %b', time.localtime(t))} {gold(p)}"
            for t, p in past[-7:])
        self.status.set(f"item {item_id}:  {trail}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--db", default="",
                    help="database to read (default: wowcraft.sqlite3 beside "
                         "the program, then the working directory)")
    ap.add_argument("-n", "--limit", type=int, default=300,
                    help="rows drawn at once (default 300); searching narrows "
                         "to what you want long before this matters")
    args = ap.parse_args(argv)

    data = Prices(find_db(args.db))
    root = tk.Tk()
    App(root, data, args.limit)
    root.mainloop()
    return 0


def _fatal(message: str) -> None:
    """Frozen, there is no console for a traceback to land in, so a failure to
    start would just be a program that does not appear. Say it in a box."""
    try:
        import tkinter.messagebox as mb
        hidden = tk.Tk()
        hidden.withdraw()
        mb.showerror("wowcraft - price lookup", message)
        hidden.destroy()
    except Exception:
        print(message)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            _fatal(exc.code)
            sys.exit(1)
        raise
