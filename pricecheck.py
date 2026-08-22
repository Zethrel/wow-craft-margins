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
from tkinter import font as tkfont
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
        self.realm = ""
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
        # Which realm these prices are for. The dashboard puts it under the
        # title and this window said nothing at all, which is a real gap the
        # moment you keep one database per realm - the numbers look identical
        # either way, because commodities are region-wide and only the gear
        # differs.
        realm = db.execute(
            "SELECT value FROM meta WHERE key='realm_slug'").fetchone()
        self.realm = (realm["value"] if realm else "") or ""

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


# The dashboard's own palette, token for token. The two are one product and
# were drifting apart: this window had its own near-miss greys, its own idea
# of what "positive" looks like, and banded rows the report has never had. The
# values below are lifted straight from the dark theme in wowcraft.CSS - keep
# them in step when that changes.
#
# Black and yellow, after the logo. Dark because this sits open beside a game
# that is dark, and a white grid at 11pm is the reason people close a tool like
# this.
#
# Yellow is an accent, not a surface. A saturated yellow is the brightest thing
# a screen can do, and a window full of it is unreadable within a minute - so
# it marks the few things worth the eye: the row you picked, the field you are
# typing in, the number you came to read. Everything else is black, grey and
# the off-white that carries the rest.
#
# Backgrounds are warm-tinted rather than pure #000: on an LCD, true black
# beside a bright yellow makes the edges buzz.
THEME = {
    "plane":    "#0e0e0c",   # the page behind everything
    "surface":  "#181713",   # cards, and the table that sits in one
    "ink":      "#f2f0e6",
    "ink_2":    "#b8b5a5",
    "muted":    "#8d8a7c",
    "grid":     "#2a2a22",   # hairlines and borders
    "axis":     "#3a3a30",   # a shade up from grid: scrollbars, badge edges
    "accent":   "#f5e400",   # the logo yellow, as a FILL
    "accent_ink": "#0e0e0c",  # what goes on top of that fill. Black, always -
                             # white on this yellow measures 1.3:1.
    # Direction. Yellow up and orange down is the dashboard's pairing, not a
    # traffic light: gold is what is being counted, so up is the accent colour
    # and down steps away from it. Direction is on every row as an arrow too,
    # which matters because these two differ in hue more than in brightness.
    "pos":      "#f5e400",
    "neg":      "#e0784f",
}


def blend(base: str, tint: str, amount: float) -> str:
    """Mix `tint` into `base`. Both "#rrggbb", `amount` 0..1."""
    def parts(h):
        return [int(h[i:i + 2], 16) for i in (1, 3, 5)]
    a, b = parts(base), parts(tint)
    return "#%02x%02x%02x" % tuple(
        min(255, max(0, round(x + (y - x) * amount))) for x, y in zip(a, b))


# Percent move before a row is worth marking. Measured over a full table: half
# of all items move more than 22% in a day and a quarter move more than 50%,
# so a 20% bar is not "notable", it is Tuesday.
TREND_STRONG = 50.0

# How hard to tint a strongly-moving row - and it is 0, which turns row
# tinting off. The dashboard has never tinted a row background; it colours the
# number and leaves the row alone, and this window now does the same. Nothing
# is lost by it: the arrow and the percentage carry direction on every row
# whether or not the colour is there. Raise it to about 0.28 to bring the wash
# back.
TREND_TINT = 0.0

# The wash under the pointer, which is what replaces banding as the way to
# follow a wide row across. 8% yellow, exactly as `tbody tr:hover` on the
# dashboard - a full-strength yellow row would drown the numbers on it.
HOVER_TINT = 0.08

# 13.5px text at 96dpi is 10pt, and the dashboard's rows come out around 31px
# tall. 26 is a deliberate step back from that: this window is meant to sit
# open beside a game on half a monitor, so it buys back four rows a screen
# against the report's more generous spacing.
ROW_HEIGHT = 26


def pick_font(*candidates) -> str:
    """First font family that actually exists, so this can name Segoe UI
    without breaking anywhere that has never heard of it."""
    try:
        available = {name.lower() for name in tkfont.families()}
    except tk.TclError:
        return candidates[-1]
    for name in candidates:
        if name.lower() in available:
            return name
    return candidates[-1]


def build_stamp() -> str:
    """When this build was made - and only when there is a build.

    Run from source there is nothing to say and this returns "". Frozen, it is
    the exe's own timestamp, put in the title bar because there is otherwise
    no way to tell a fresh pricecheck.exe from the one that was sitting there
    before: same name, same icon, same window. A build that silently never ran
    looks exactly like one that did.
    """
    if not getattr(sys, "frozen", False):
        return ""
    try:
        made = os.path.getmtime(sys.executable)
    except OSError:
        return ""
    return time.strftime("%d %b %H:%M", time.localtime(made))


def style_window(root: tk.Tk) -> dict:
    """Dark theme over ttk, matching the dashboard. Built on `clam` because it
    is the only stock theme that lets the Treeview's colours actually be set -
    the native Windows themes draw their own and ignore most of this.

    Returns the named fonts, because the plain tk widgets need them too."""
    root.configure(bg=THEME["plane"])
    # The dashboard asks for ui-sans-serif and gets Segoe UI on Windows. Name
    # it directly here rather than letting Tk fall back to its default, which
    # is still Tk 8.6's ancient bitmap face on some machines.
    family = pick_font("Segoe UI", "Inter", "DejaVu Sans", "Helvetica")
    fonts = {
        "h1": tkfont.Font(family=family, size=16, weight="bold"),
        "sub": tkfont.Font(family=family, size=10),
        "body": tkfont.Font(family=family, size=10),
        "head": tkfont.Font(family=family, size=8, weight="bold"),
        "meta": tkfont.Font(family=family, size=9),
    }
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        return fonts
    style.configure(".", background=THEME["plane"], foreground=THEME["ink"],
                    fieldbackground=THEME["surface"], borderwidth=0,
                    font=fonts["body"])
    style.configure("TFrame", background=THEME["plane"])
    # The combobox popup is a plain Tk listbox, not a ttk widget, so no style
    # reaches it - it stays system white unless it is told otherwise here. On
    # a black window that flash of white when you open the expansion picker is
    # the single most out-of-place thing left.
    root.option_add("*TCombobox*Listbox.background", THEME["surface"])
    root.option_add("*TCombobox*Listbox.foreground", THEME["ink"])
    root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", THEME["accent_ink"])
    root.option_add("*TCombobox*Listbox.font", fonts["body"])
    root.option_add("*TCombobox*Listbox.borderWidth", 0)
    style.configure("TLabel", background=THEME["plane"], foreground=THEME["muted"])
    style.configure("H1.TLabel", foreground=THEME["ink"], font=fonts["h1"])
    style.configure("Sub.TLabel", foreground=THEME["ink_2"], font=fonts["sub"])
    style.configure("Status.TLabel", foreground=THEME["muted"],
                    font=fonts["meta"])
    # The caret is yellow so you can find where you are typing on a black
    # field, and the focus border lights up rather than merely thickening.
    style.configure("TEntry", fieldbackground=THEME["surface"],
                    foreground=THEME["ink"], insertcolor=THEME["accent"],
                    bordercolor=THEME["grid"], lightcolor=THEME["grid"],
                    darkcolor=THEME["grid"], padding=6)
    style.map("TEntry",
              bordercolor=[("focus", THEME["accent"])],
              lightcolor=[("focus", THEME["accent"])],
              darkcolor=[("focus", THEME["accent"])])
    style.configure("TCombobox", fieldbackground=THEME["surface"],
                    background=THEME["surface"], foreground=THEME["ink"],
                    arrowcolor=THEME["muted"], bordercolor=THEME["grid"],
                    lightcolor=THEME["grid"], darkcolor=THEME["grid"],
                    padding=5)
    style.map("TCombobox",
              fieldbackground=[("readonly", THEME["surface"])],
              foreground=[("readonly", THEME["ink"])],
              bordercolor=[("focus", THEME["accent"])],
              arrowcolor=[("active", THEME["accent"])])
    # Buttons sit quiet until hovered, then go full logo: yellow plate, black
    # letter. Black on yellow, never white - white on this yellow is barely
    # a contrast at all.
    style.configure("TButton", background=THEME["surface"],
                    foreground=THEME["ink_2"], bordercolor=THEME["grid"],
                    lightcolor=THEME["grid"], darkcolor=THEME["grid"],
                    padding=(12, 5))
    style.map("TButton",
              background=[("active", THEME["accent"])],
              foreground=[("active", THEME["accent_ink"])])
    # No banding, because the dashboard has none: one flat surface, hairlines
    # between rows, and the row under the pointer washed yellow. Tk cannot
    # draw a border under a row, so the hairline is the one part of that which
    # does not survive the trip.
    style.configure("Treeview", background=THEME["surface"],
                    fieldbackground=THEME["surface"], foreground=THEME["ink"],
                    rowheight=ROW_HEIGHT, borderwidth=0, font=fonts["body"])
    # Headings: small, uppercase, muted, sitting on the same surface as the
    # rows - the dashboard's `th`. Tk has no letter-spacing, which is the only
    # part of that not reproduced.
    style.configure("Treeview.Heading", background=THEME["surface"],
                    foreground=THEME["muted"], relief="flat",
                    borderwidth=0, padding=(8, 8), font=fonts["head"])
    style.map("Treeview.Heading",
              background=[("active", THEME["surface"])],
              foreground=[("active", THEME["accent"])])
    style.map("Treeview", background=[("selected", THEME["accent"])],
              foreground=[("selected", THEME["accent_ink"])])
    style.configure("Vertical.TScrollbar", background=THEME["axis"],
                    troughcolor=THEME["surface"], bordercolor=THEME["surface"],
                    lightcolor=THEME["surface"], darkcolor=THEME["surface"],
                    arrowcolor=THEME["muted"])
    style.map("Vertical.TScrollbar",
              background=[("active", THEME["accent"])])
    return fonts


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
        stamp = build_stamp()
        root.title("wowcraft — price lookup"
                   + (f"   ·   build {stamp}" if stamp else ""))
        root.geometry("980x640")
        root.minsize(680, 400)
        self.fonts = style_window(root)

        # The dashboard opens with a title and one grey line saying what realm
        # and which snapshot you are looking at. That line used to live at the
        # bottom of this window mixed in with the match count, which put the
        # two things you check first - is this my realm, and how old is it -
        # in the least-read pixel on screen.
        head = ttk.Frame(root, padding=(18, 16, 18, 0))
        head.pack(fill="x")
        ttk.Label(head, text="Price lookup", style="H1.TLabel").pack(anchor="w")
        self.subtitle = tk.StringVar()
        ttk.Label(head, textvariable=self.subtitle,
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        top = ttk.Frame(root, padding=(18, 14, 18, 12))
        top.pack(fill="x")
        ttk.Label(top, text="Search", style="Sub.TLabel").pack(side="left",
                                                               padx=(0, 8))
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

        # The card. A tk.Frame rather than a ttk one because only the plain
        # widget exposes highlightthickness, which is the sole way to draw a
        # one-pixel border of a chosen colour around a block in Tk.
        card = tk.Frame(root, bg=THEME["surface"], highlightthickness=1,
                        highlightbackground=THEME["grid"],
                        highlightcolor=THEME["grid"])
        card.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        self.tree = ttk.Treeview(card, columns=[c[0] for c in COLUMNS],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title.upper(),
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=(key == "name"))
        bar = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.status = tk.StringVar()
        ttk.Label(root, textvariable=self.status, anchor="w",
                  style="Status.TLabel",
                  padding=(18, 0, 18, 12)).pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0),
                       pady=10)
        bar.pack(side="left", fill="y", padx=(4, 6), pady=10)

        # A Treeview colours whole rows, never single cells: Tk 8.6 has no
        # per-cell foreground, and this is a limit rather than a styling
        # oversight to work around. The dashboard colours the Trend number and
        # leaves the row alone; here the closest honest equivalent is to
        # colour the row's TEXT - so a strong mover reads as a yellow or an
        # orange line, in the same two colours, instead of the green and red
        # background bands this window used to draw.
        #
        # Only moves past TREND_STRONG are coloured. On a full table half of
        # everything moves more than 22% in a day, so a lower bar lights up
        # the screen and means nothing. Direction is on every row as an arrow
        # regardless, which also means none of this depends on seeing colour.
        self.tree.tag_configure("up", foreground=THEME["pos"])
        self.tree.tag_configure("down", foreground=THEME["neg"])
        # The tinted-background version, kept because TREND_TINT is the one
        # switch that brings it back (it is 0, i.e. off).
        self.tree.tag_configure(
            "up_bg", background=blend(THEME["surface"], THEME["pos"], TREND_TINT))
        self.tree.tag_configure(
            "down_bg", background=blend(THEME["surface"], THEME["neg"], TREND_TINT))
        # With banding gone, something has to help the eye carry a wide row
        # across to the Trend column. The dashboard washes the row under the
        # pointer 8% yellow; this does the same, one row at a time.
        self.tree.tag_configure(
            "hover", background=blend(THEME["surface"], THEME["accent"],
                                      HOVER_TINT))
        # Selection is a tag rather than only a style map because since Tk
        # 8.6.11 a tag beats the map: a selected row that also carried "up"
        # would draw yellow text on the yellow plate and vanish. Applied last,
        # it wins over both.
        self.tree.tag_configure("sel", background=THEME["accent"],
                                foreground=THEME["accent_ink"])
        self.direction: dict = {}
        self.hovered = None
        self.selected = None
        self.tree.bind("<Motion>", self.hover)
        self.tree.bind("<Leave>", lambda _e: self.hover(None))

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

    def tag_row(self, iid: str) -> None:
        """Rebuild one row's tags from what is true about it right now.

        Direction, then hover, then selection - in that order, because Tk
        applies them in order and the last one to set an option wins. Building
        the whole list each time is what keeps hovering a selected mover from
        leaving one of the three behind."""
        if not iid or not self.tree.exists(iid):
            return
        tags = []
        move = self.direction.get(iid)
        if move:
            tags.append(move)
            if TREND_TINT:
                tags.append(move + "_bg")
        if iid == self.hovered:
            tags.append("hover")
        if iid in self.tree.selection():
            tags.append("sel")
        self.tree.item(iid, tags=tags)

    def hover(self, event) -> None:
        """Wash the row under the pointer, and only that row.

        Two rows are re-tagged per move rather than the table redrawn, and the
        guard keeps even that to the moves that cross a row boundary."""
        row = self.tree.identify_row(event.y) if event else ""
        if row == self.hovered:
            return
        was, self.hovered = self.hovered, (row or None)
        self.tag_row(was)
        self.tag_row(row)

    def mark_headings(self) -> None:
        """Arrow on the column being sorted by.

        The headings are clickable and nothing said so, which on a dark grid
        is easy to miss entirely. An arrow rather than a colour because ttk
        styles every heading as one - there is no per-column foreground."""
        for key, title, _w, _a in COLUMNS:
            sorted_on = self.sort_key == key or (
                key == "today" and self.sort_key == "high")
            arrow = (" ▾" if self.sort_desc else " ▴") if sorted_on else ""
            self.tree.heading(key, text=title.upper() + arrow)

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
        self.hovered = None
        self.direction.clear()
        for r in rows[:self.limit]:
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
            move = r["trend"] or 0.0
            iid = str(r["id"])
            self.direction[iid] = ("up" if move >= TREND_STRONG else
                                   "down" if move <= -TREND_STRONG else "")
            self.tree.insert("", "end", iid=iid, tags=(), values=(
                r["name"], r["id"], gold(r["buy"]), gold(r["sell"]),
                f"{r['supply']:,}", f"{r['listings']:,}", today, trend))
            self.tag_row(iid)

        self.shown_count = min(len(rows), self.limit)
        self.match_count = len(rows)
        self.status_line()

    def status_line(self) -> None:
        """Two lines, split the way the dashboard splits them.

        The subtitle says what you are looking at and how old it is - the
        report's own `.sub` line, in the same place. The status bar keeps what
        changes as you type: how much of the table the search left, and
        whatever a selected row has to say."""
        self.subtitle.set(
            f"{self.data.realm or 'unknown realm'}"
            f"   ·   snapshot "
            f"{time.strftime('%d %b %H:%M', time.localtime(self.data.data_time))}"
            f", {age(self.data.data_time)}"
            f"   ·   {len(self.data.rows):,} items priced")
        more = (f" (showing {self.shown_count})"
                if self.match_count > self.shown_count else "")
        where = self.expansion.get()
        scope = "" if where in ("", "All expansions") else f" in {where}"
        note = "updates itself"
        if self.refreshed_at and time.time() - self.refreshed_at < 120:
            note = ("picked up a new scan "
                    + time.strftime("%H:%M", time.localtime(self.refreshed_at)))
        self.status.set(
            f"{self.match_count:,} matching{scope}{more}   ·   {note}")

    def show_history(self, _event=None) -> None:
        selected = self.tree.selection()
        # Re-tag the row that just lost the selection as well as the one that
        # gained it, so a strong mover gets its own colour back on the way out.
        previous, self.selected = self.selected, (selected[0] if selected
                                                  else None)
        for iid in (previous, self.selected):
            self.tag_row(iid)
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
