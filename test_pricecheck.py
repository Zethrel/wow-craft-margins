"""The price lookup window: auto-refresh, filtering, and the age line.

Tkinter is built but never mapped, so this runs headless. The interesting
property is that the window keeps up with the hourly scan writing underneath
it - and does not lose your search, or blank itself, while doing so.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import tkinter as tk
except ImportError:
    print("SKIP  test_pricecheck.py needs tkinter")
    sys.exit(0)
import pricecheck

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "wowcraft.sqlite3")
if not os.path.exists(SOURCE):
    print("SKIP  test_pricecheck.py needs a scanned wowcraft.sqlite3")
    sys.exit(0)

fails = []


def must(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        fails.append(label)


work = os.path.join(tempfile.mkdtemp(), "live.sqlite3")
shutil.copy(SOURCE, work)

root = tk.Tk()
root.withdraw()
data = pricecheck.Prices(work)
app = pricecheck.App(root, data)
root.update_idletasks()

must("window opens with rows", len(app.tree.get_children()) > 0)
must("opens on the newest expansion",
     app.expansion.get() == data.newest_expansion)
must("nothing looks changed before anything changes",
     data.changed_on_disk() is False)

app.query.set("void-tempered leather")
app.render()
root.update_idletasks()
before = [app.tree.item(i)["values"] for i in app.tree.get_children()]
must("search narrows to the item", len(before) > 0)

# A scan lands underneath the open window.
db = sqlite3.connect(work)
taken = db.execute("SELECT MAX(taken_at) t FROM price_snapshot").fetchone()[0]
target = int(before[0][1])
db.execute("UPDATE price_snapshot SET min_unit_price=11111, sell_unit_price=11111 "
           "WHERE taken_at=? AND item_id=?", (taken, target))
db.execute("INSERT INTO meta(key,value) VALUES('last_data_time',?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (str(int(time.time())),))
db.commit()
db.close()

must("the change is noticed", data.changed_on_disk() is True)
app.tick()
root.update_idletasks()
after = [app.tree.item(i)["values"] for i in app.tree.get_children()]
must("the new price is on screen", after != before)
must("the search survived the refresh",
     app.query.get() == "void-tempered leather")
must("it says a scan was picked up", "picked up a new scan" in app.status.get())
# What you are looking at and how old it is live in the subtitle now - the
# dashboard's own `.sub` line, in the same place - leaving the status bar for
# what changes as you type.
must("the age follows the new data", "just now" in app.subtitle.get())
must("the subtitle names the realm", data.realm in app.subtitle.get())
must("the subtitle says which snapshot", "snapshot" in app.subtitle.get())
must("the status bar keeps the match count", "matching" in app.status.get())

# Ticking again with nothing new must not churn.
app.tick()
root.update_idletasks()
must("a quiet tick changes nothing",
     [app.tree.item(i)["values"] for i in app.tree.get_children()] == after)

# The scan holds the file while writing; the window must ride that out.
locker = sqlite3.connect(work, isolation_level="EXCLUSIVE")
locker.execute("BEGIN EXCLUSIVE")
data.stamp = None
rows_before = len(app.tree.get_children())
app.refresh(automatic=True)
root.update_idletasks()
must("a locked database does not blank the window",
     len(app.tree.get_children()) == rows_before)
locker.rollback()
locker.close()

# Row colouring. Tk gives no per-cell foreground, so a row carries at most
# three tags at once and the last one set has to win: a selected strong mover
# must draw black-on-yellow, not yellow-on-yellow.
app.query.set("")
app.render()
root.update_idletasks()
rows = app.tree.get_children()
must("the table is back", len(rows) > 0)
first = rows[0]


def tags_of(iid):
    """Tk hands back a tuple, except on the builds where a lone tag comes back
    as a bare string - and list("up") is ['u','p'], which fails an assertion
    for the wrong reason."""
    got = app.tree.item(iid, "tags")
    if isinstance(got, (list, tuple)):
        return list(got)
    return [got] if got else []


app.direction[first] = "up"
app.hovered = None
app.tag_row(first)
must("a strong mover is tagged by direction",
     "up" in tags_of(first))
must("and the tint tag stays off while TREND_TINT is 0",
     ("up_bg" in tags_of(first)) == bool(pricecheck.TREND_TINT))
app.hovered = first
app.tag_row(first)
must("hover is added without losing direction",
     tags_of(first)[:2] == ["up", "hover"])
# update() rather than update_idletasks(): <<TreeviewSelect>> is queued as an
# event, and draining idle callbacks does not drain the event queue. With the
# weaker call the binding had simply not run yet, and the assertion below was
# reading the tags from before the selection - which is the sort of test that
# passes or fails on timing rather than on behaviour.
app.tree.selection_set(first)
root.update()
must("selection is tagged last, so it wins",
     tags_of(first)[-1] == "sel")
app.tree.selection_remove(first)
root.update()
must("and is dropped again when the row is deselected",
     "sel" not in tags_of(first))
must("the row keeps its direction colour afterwards", "up" in tags_of(first))
app.hovered = None
app.tag_row(first)
must("leaving the row drops the wash",
     "hover" not in tags_of(first))

# A redraw must not leave the old hover or the old directions behind.
app.render()
root.update_idletasks()
must("a redraw clears the hover", app.hovered is None)

root.destroy()
print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
