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
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.db_path):
            raise SystemExit(
                f"No database at {self.db_path}.\n"
                "Run `python wowcraft.py scan` first, or pass --db.")
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
        root.title("wowcraft - price lookup")
        root.geometry("860x560")
        root.minsize(620, 360)

        top = ttk.Frame(root, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        self.query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True, padx=(6, 6))
        entry.focus_set()
        self.expansion = tk.StringVar()
        choices = ["All expansions"] + data.expansions + ["Not in any recipe"]
        picker = ttk.Combobox(top, textvariable=self.expansion, values=choices,
                              state="readonly", width=20)
        picker.pack(side="left", padx=(0, 6))
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
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0),
                       pady=(0, 4))
        bar.pack(side="left", fill="y", padx=(0, 8), pady=(0, 4))

        self.status = tk.StringVar()
        ttk.Label(root, textvariable=self.status, anchor="w",
                  padding=(8, 2, 8, 6)).pack(side="bottom", fill="x")

        self.tree.tag_configure("up", foreground="#2a8a2a")
        self.tree.tag_configure("down", foreground="#c03030")

        # Typing filters as you go; the whole set is in memory so there is no
        # need to debounce.
        self.query.trace_add("write", lambda *_: self.render())
        self.tree.bind("<<TreeviewSelect>>", self.show_history)
        root.bind("<Escape>", lambda _e: (self.query.set(""), entry.focus_set()))
        root.bind("<Control-f>", lambda _e: entry.focus_set())
        self.render()

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

    def sort_by(self, key: str) -> None:
        if key in ("today", "trend"):
            key = "trend" if key == "trend" else "high"
        if self.sort_key == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key, self.sort_desc = key, key != "name"
        self.render()

    def refresh(self) -> None:
        try:
            self.data.load()
        except SystemExit as exc:
            self.status.set(str(exc))
            return
        self.render()

    # -- view ------------------------------------------------------------

    def render(self) -> None:
        rows = self.matching()
        self.tree.delete(*self.tree.get_children())
        for r in rows[:self.limit]:
            today = "-"
            if r["low"] and r["high"]:
                today = ("steady" if abs(r["high"] - r["low"]) < 1
                         else f"{gold(r['low'])} - {gold(r['high'])}")
            trend, tag = "-", ""
            if r["trend"] is not None:
                if abs(r["trend"]) < 0.5:
                    trend = "flat"
                else:
                    trend = f"{r['trend']:+.0f}%"
                    tag = "up" if r["trend"] > 0 else "down"
            self.tree.insert("", "end", iid=str(r["id"]), tags=(tag,), values=(
                r["name"], r["id"], gold(r["buy"]), gold(r["sell"]),
                f"{r['supply']:,}", f"{r['listings']:,}", today, trend))

        shown = min(len(rows), self.limit)
        more = f" (showing {shown})" if len(rows) > self.limit else ""
        where = self.expansion.get()
        scope = "" if where in ("", "All expansions") else f" in {where}"
        self.status.set(
            f"{len(rows):,} of {len(self.data.rows):,} items{scope}{more}   |   "
            f"prices from "
            f"{time.strftime('%d %b %H:%M', time.localtime(self.data.data_time))}"
            f", {age(self.data.data_time)}   |   Refresh after a scan")

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
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--db", default=os.path.join(here, "wowcraft.sqlite3"))
    ap.add_argument("-n", "--limit", type=int, default=300,
                    help="rows drawn at once (default 300); searching narrows "
                         "to what you want long before this matters")
    args = ap.parse_args(argv)

    data = Prices(args.db)
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root, data, args.limit)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
