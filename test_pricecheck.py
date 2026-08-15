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
must("the age follows the new data", "just now" in app.status.get())

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

root.destroy()
print()
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
