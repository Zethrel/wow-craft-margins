#!/usr/bin/env python3
"""Read the WowCraftExport SavedVariables file and diff it against the cache.

This is the bridge between the addon and `wowcraft.py`. The addon sees what
Blizzard's Game Data API will not publish - reagent slot quantities and the
items that legally fill each slot - so this answers two questions before we
build anything on top of it:

  1. Was our name matching right? The API does not say what modern recipes
     craft, so `init` guesses by name. The addon knows for certain.
  2. How much of the "cost is a floor" problem does the export actually fix?

Standard library only, like the scanner. Run it after /wcexport and /reload.
"""
import argparse
import json
import os
import re
import sqlite3
import sys

DEFAULT_WOW = r"D:\Games\World of Warcraft\_retail_"
# The client names the file after the ADDON, not after the saved variable, so
# this is WowCraftExport.lua holding a table called WowCraftExportDB.
SAVED_VAR_NAMES = ("WowCraftExport.lua", "WowCraftExportDB.lua")

# Lua escapes WoW writes into SavedVariables. Unescaped in one pass, because
# doing them one at a time would turn \\" into " instead of \".
LUA_UNESCAPE = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


# WoW inlines UI escape sequences in some strings: |A:atlas:h:w|a for the
# quality-tier icons on Jewelcrafting's "Refine ..." recipes, |Ttexture|t,
# |cAARRGGBB ... |r colouring, |Hlink|h[text]|h hyperlinks. Left in place they
# never match a cached recipe name and render as gibberish on the dashboard.
WOW_MARKUP = re.compile(
    r"\|A:.-?\|a|\|A:[^|]*\|a|\|T[^|]*\|t|\|c[0-9a-fA-F]{8}|\|r|\|H[^|]*\|h|\|h")


def clean_name(name) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"\s{2,}", " ", WOW_MARKUP.sub("", name)).strip()


def find_export(wow_dir: str) -> list:
    """Every export file under the WoW folder, newest first.

    Account-wide saved variables land in WTF/Account/<ACCOUNT>/SavedVariables,
    but per-character ones live a few levels deeper, so this walks the lot.
    `.bak` copies are the client's previous flush and are ignored."""
    found = []
    account_root = os.path.join(wow_dir, "WTF", "Account")
    for root, _dirs, files in os.walk(account_root):
        for name in files:
            if name in SAVED_VAR_NAMES:
                found.append(os.path.join(root, name))
    return sorted(found, key=os.path.getmtime, reverse=True)


def load_exports(path: str) -> list:
    """Every profession export in one SavedVariables file, in file order.

    The addon stores one JSON string per skill line, so rather than model the
    Lua nesting this pulls out every quoted string and keeps the ones that
    parse as an export. That reads both the current per-profession layout and
    the original single-export one, and survives Blizzard reformatting the
    file around them."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    found = []
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"', text):
        blob = match.group(1)
        if '\\"recipes\\"' not in blob and '"recipes"' not in blob:
            continue
        raw = re.sub(r"\\(.)", lambda m: LUA_UNESCAPE.get(m.group(1), m.group(1)),
                     blob)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("recipes"), list):
            found.append(data)
    if not found:
        raise SystemExit(
            f"No export found in {path}.\nThe file exists but holds nothing "
            "usable - run /wcexport with a profession window open, then "
            "/reload.")
    return found


def load_export(path: str) -> dict:
    """The single newest export in a file. Kept for callers wanting just one."""
    return sorted(load_exports(path),
                  key=lambda d: d.get("exported_at") or 0)[-1]


def merge_exports(paths: list) -> list:
    """One export per profession across every file, newest wins.

    Professions live on different characters and possibly different WoW
    accounts, so the same skill line can appear in several files; and a
    re-export of one profession should not resurrect an older copy of it."""
    best = {}
    skipped = 0
    for path in paths:
        for data in load_exports(path):
            prof = data.get("profession") or {}
            # An export taken before the profession window finished loading has
            # recipes but no identity. Earlier addon versions wrote those; they
            # are duplicates of a real profession and cannot be filed, so drop
            # them rather than reporting a nameless sixth profession.
            if not prof.get("professionID") or not prof.get("professionName"):
                skipped += 1
                continue
            key = prof["professionID"]
            stamp = data.get("exported_at") or 0
            current = best.get(key)
            if current is None or stamp >= (current[0].get("exported_at") or 0):
                best[key] = (data, path)
    if skipped:
        print(f"note: ignored {skipped} export(s) with no profession attached "
              "(taken while the window was still loading)")
    return [pair for _key, pair in sorted(
        best.items(),
        key=lambda kv: str((kv[1][0].get("profession") or {})
                           .get("professionName", "")))]


def load_inventory(paths: list) -> dict:
    """{character: {item_id: count}} from every SavedVariables file found.

    The addon writes one JSON blob per container group per character. Groups
    are merged per character, and characters are kept separate so the report
    can say who is holding what."""
    out: dict = {}
    # Each group blob is preceded, somewhere above it, by the character whose
    # table it sits in. Rather than parse the Lua nesting, walk the file in
    # order and remember the most recent "Name-Realm" key seen: the blobs that
    # follow belong to it. Robust to however the client chooses to indent.
    token = re.compile(
        r'\["(?P<who>[^"\\]+-[^"\\]+)"\]\s*=\s*\{'
        r'|\["(?P<group>bags|bank|warband)"\]\s*=\s*"(?P<blob>(?:[^"\\]|\\.)*)"')
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        start = text.find('["inventory"]')
        if start < 0:
            continue
        who = None
        for match in token.finditer(text, start):
            if match.group("who"):
                who = match.group("who")
                out.setdefault(who, {})
                continue
            if not who:
                continue
            raw = re.sub(r"\\(.)",
                         lambda m: LUA_UNESCAPE.get(m.group(1), m.group(1)),
                         match.group("blob"))
            try:
                held = json.loads(raw)
            except json.JSONDecodeError:
                continue
            counts = out[who]
            for item_id, count in held.items():
                try:
                    counts[int(item_id)] = counts.get(int(item_id), 0) + int(count)
                except (TypeError, ValueError):
                    continue
    return out


def report_export(data: dict) -> None:
    prof = data.get("profession") or {}
    print(f"  build        : {data.get('build')}  locale {data.get('locale')}")
    # professionID here is the skill LINE id - 2906 is Midnight Alchemy, the
    # same number the Game Data API uses for that tier - while
    # parentProfessionID is the profession proper. Handy join keys, so show
    # both rather than the name alone.
    print(f"  profession   : {prof.get('professionName', '?')} "
          f"(skill line {prof.get('professionID', '?')}, "
          f"profession {prof.get('parentProfessionName', '?')} "
          f"{prof.get('parentProfessionID', '?')})")
    print(f"  source call  : {data.get('profession_source')}")
    print(f"  recipes      : {data.get('recipe_count')}")

    diag = data.get("diagnostics") or {}
    secrets = diag.get("secrets") or {}
    if secrets:
        print("  SECRET VALUES - these fields could not be read:")
        for field, count in sorted(secrets.items(), key=lambda kv: -kv[1]):
            print(f"      {field:38s} x{count}")
    else:
        print("  secret values: none - every field read cleanly")
    for bucket in ("missing", "unreadable", "empty"):
        entries = diag.get(bucket) or {}
        if entries:
            print(f"  {bucket}:")
            for field, count in sorted(entries.items(), key=lambda kv: -kv[1]):
                print(f"      {field:38s} x{count}")
    if diag.get("error_count"):
        print(f"  errors       : {diag['error_count']}")
        for err in (diag.get("errors") or [])[:5]:
            print(f"      {err}")


def summarise_slots(recipes: list) -> None:
    basic = optional = finishing = untyped = 0
    priced_slots = with_items = 0
    for rec in recipes:
        for slot in rec.get("slots") or []:
            kind = slot.get("type")
            if kind == 1:
                basic += 1
            elif kind == 2:
                optional += 1
            elif kind == 3:
                finishing += 1
            else:
                untyped += 1
            if slot.get("quantity"):
                priced_slots += 1
            if slot.get("items"):
                with_items += 1
    total = basic + optional + finishing + untyped
    print(f"  reagent slots: {total}  "
          f"(basic {basic}, optional {optional}, finishing {finishing}"
          + (f", untyped {untyped}" if untyped else "") + ")")
    print(f"    with a required quantity : {priced_slots}/{total}")
    print(f"    with a legal item list   : {with_items}/{total}")

    qualities = [len(r.get("qualities") or []) for r in recipes]
    multi = sum(1 for q in qualities if q > 1)
    print(f"  recipes with more than one quality tier: {multi}/{len(recipes)}")
    if not multi:
        # Measured on the first real export: GetRecipeOutputItemData returns
        # the same item id whatever quality is asked for, at least when called
        # without a reagent allocation. So crafting ranks stay unsolved - the
        # addon has not rescued that one, and nothing here should pretend it
        # has until a call is found that does distinguish them.
        print("    (every quality asked for returned the same item, so ranks")
        print("     remain unresolved - same gap as the API, not a fix)")


def diff_against_cache(recipes: list, db_path: str,
                       profession: str = "") -> None:
    if not os.path.exists(db_path):
        print(f"  no cache at {db_path} - run `wowcraft.py init` to compare")
        return
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cached = {}
    for row in db.execute("SELECT * FROM recipe"):
        name = (row["name"] or "").casefold()
        if name:
            cached.setdefault(name, []).append(row)

    # The two sides do not share a recipe id: the addon reports the client's
    # recipe/spell id while the Game Data API has its own numbering. Names are
    # the only common key, which is the same reason name matching works at all.
    #
    # Names repeat across expansions though, and GetAllRecipeIDs returns the
    # whole profession rather than the open tier, so a bare name lookup will
    # happily match a Midnight recipe against its Classic namesake and report
    # a disagreement that means nothing. Only unambiguous matches - one cached
    # recipe of that name in this profession - are used to judge anything.
    matched = ambiguous = 0
    unmatched = []
    agreed = disagreed = unknown_side = 0
    guess_checked = guess_right = 0
    floor_fixable = floor_total = 0
    examples = []

    for rec in recipes:
        name = clean_name(rec.get("name")).casefold()
        rows = cached.get(name) or []
        if profession:
            narrowed = [r for r in rows if (r["profession_name"] or "") == profession]
            if narrowed:
                rows = narrowed
        if not rows:
            unmatched.append(clean_name(rec.get("name")))
            continue
        if len(rows) > 1:
            ambiguous += 1
            continue
        matched += 1
        row = rows[0]
        theirs = rec.get("output_item_id")
        ours = row["crafted_item_id"]
        if theirs is None or ours is None:
            unknown_side += 1
        elif theirs == ours:
            agreed += 1
        else:
            disagreed += 1
            if len(examples) < 10:
                examples.append((clean_name(rec.get("name")), ours, theirs,
                                 row["crafted_source"]))
        # How reliable was the name matching specifically?
        if row["crafted_source"] in ("name", "name?") and theirs and ours:
            guess_checked += 1
            if theirs == ours:
                guess_right += 1
        # And how much of the floor-cost problem does this fix?
        if row["uses_slots"]:
            floor_total += 1
            slots = rec.get("slots") or []
            extra = [s for s in slots if s.get("type") in (2, 3)]
            if extra and all(s.get("quantity") is not None for s in extra) \
                    and all(s.get("items") for s in extra):
                floor_fixable += 1

    print(f"  unambiguous name matches: {matched}/{len(recipes)}"
          + (f" (profession filter: {profession})" if profession else ""))
    print(f"    name used by several cached recipes, skipped: {ambiguous}")
    if unmatched:
        print(f"    not in the cache at all ({len(unmatched)}): "
              + ", ".join(str(n) for n in unmatched[:6])
              + (" ..." if len(unmatched) > 6 else ""))
    print(f"    same output item : {agreed}")
    print(f"    DIFFERENT output : {disagreed}")
    if unknown_side:
        print(f"    one side had no output id: {unknown_side}")
    if guess_checked:
        pct = guess_right / guess_checked * 100
        print(f"  name-guessed outputs verified: {guess_right}/{guess_checked} "
              f"correct ({pct:.0f}%)")
    if floor_total:
        print(f"  floor-cost recipes the export can price: "
              f"{floor_fixable}/{floor_total}")
    if examples:
        print("  disagreements (cache vs addon):")
        for name, ours, theirs, source in examples:
            delta = theirs - ours
            print(f"      {str(name)[:32]:34s} cached {ours} ({source}) "
                  f"-> addon {theirs}  [{delta:+d}]")


def apply_to_cache(exports: list, db_path: str) -> None:
    """Write the client's outputs and reagent slots over the API's guesses.

    Only unambiguous matches are written - one cached recipe of that name in
    that profession - because a wrong join would replace a correct output with
    a confident lie, which is worse than the guess it replaced."""
    if not os.path.exists(db_path):
        print(f"  no cache at {db_path} - run `wowcraft.py init` first")
        return
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    have = {r["name"] for r in db.execute("PRAGMA table_info(recipe)")}
    if "slots_json" not in have:
        db.execute("ALTER TABLE recipe ADD COLUMN slots_json TEXT")

    cached = {}
    for row in db.execute("SELECT id, name, profession_name, crafted_item_id, "
                          "skill_tier_id, skill_tier_name FROM recipe "
                          "WHERE id > 0"):
        key = ((row["profession_name"] or ""), (row["name"] or "").casefold())
        cached.setdefault(key, []).append(row)

    updates, changed_output, skipped_ambiguous = [], 0, 0
    for data, _path in exports:
        prof = data.get("profession") or {}
        profession = prof.get("parentProfessionName") or ""
        for rec in data.get("recipes") or []:
            rows = cached.get((profession, clean_name(rec.get("name")).casefold()))
            if not rows:
                continue
            if len(rows) > 1:
                skipped_ambiguous += 1
                continue
            slots = [s for s in (rec.get("slots") or [])
                     if s.get("items") and s.get("quantity")]
            output = rec.get("output_item_id")
            if not output or not slots:
                continue
            if rows[0]["crafted_item_id"] != output:
                changed_output += 1
            updates.append((output, json.dumps([
                {"type": s.get("type"), "required": bool(s.get("required")),
                 "quantity": s.get("quantity"), "items": s.get("items")}
                for s in slots]), rows[0]["id"]))

    db.executemany(
        "UPDATE recipe SET crafted_item_id=?, slots_json=?, crafted_source='client' "
        "WHERE id=?", updates)
    db.commit()
    print(f"  updated {len(updates)} cached recipes with client data")
    print(f"    corrected outputs the API had guessed wrong: {changed_output}")
    print(f"    skipped, name not unique in the profession   : {skipped_ambiguous}")
    inserted, skipped_useless = insert_missing(exports, db, cached)
    print(f"  inserted {inserted} recipes the API never listed at all")
    if skipped_useless:
        print(f"    ignored {skipped_useless} with no output or no reagents "
              "(utility and profession-stat entries)")
    print("  re-run `wowcraft.py scan` to price them properly.")


def learn_tier_bands(exports: list, cached: dict) -> dict:
    """Work out which recipe ids belong to which skill tier, from the overlap.

    The client allocates recipe ids in blocks per expansion - on this account
    the medians run 11k for Classic through 445k for Khaz Algar and 1.23M for
    Midnight, cleanly ordered. Thousands of recipes appear in both the client
    export and the API cache, and for those the true tier is known, so the
    bands can be measured rather than assumed.

    Returns {profession: [(median_id, tier_name, tier_id), ...]}."""
    samples = {}
    for data, _path in exports:
        prof = data.get("profession") or {}
        profession = prof.get("parentProfessionName") or ""
        for rec in data.get("recipes") or []:
            rows = cached.get((profession, clean_name(rec.get("name")).casefold()))
            if not rows or len(rows) != 1:
                continue
            row = rows[0]
            if not row["skill_tier_name"]:
                continue
            key = (profession, row["skill_tier_name"], row["skill_tier_id"])
            samples.setdefault(key, []).append(int(rec["id"]))
    bands = {}
    for (profession, tier_name, tier_id), ids in samples.items():
        if len(ids) < 3:      # too few to place a band with any confidence
            continue
        ids.sort()
        # Where the block starts, not where it centres. A low quantile rather
        # than the outright minimum, in case a stray recipe is filed in a tier
        # it does not belong to - but only just above the floor, because the
        # blocks butt up against each other and a boundary set too high hands
        # the start of one expansion to the previous one.
        bands.setdefault(profession, []).append(
            (ids[len(ids) // 20], tier_name, tier_id))
    for entries in bands.values():
        entries.sort()
    return bands


def pick_tier(bands: list, recipe_id: int, fallback: tuple) -> tuple:
    """The tier whose id block this recipe falls in.

    Ids are handed out in ascending blocks per expansion, so the right tier is
    the last one that starts at or below this id. Matching to the nearest band
    centre instead gets borderline cases wrong - a Pandaria dish at 104,298
    sits almost exactly between the Cataclysm and Pandaria medians and lands in
    the wrong one, while the block boundaries separate them cleanly."""
    if not bands:
        return fallback
    chosen = None
    for start, tier_name, tier_id in bands:      # sorted ascending
        if recipe_id >= start:
            chosen = (tier_name, tier_id)
        else:
            break
    return chosen or (bands[0][1], bands[0][2])


def insert_missing(exports: list, db, cached: dict) -> tuple:
    """Add recipes the client knows and the Game Data API never listed.

    Keyed on the NEGATED client recipe id. The two sides number recipes
    independently and they genuinely overlap - 98 of the client's ids collide
    with cached API ids on this account - so inserting under the client's own
    id would silently overwrite unrelated recipes. Negative ids cannot collide
    with either namespace and make the row's origin obvious in the database.

    The skill tier is inferred from the recipe id rather than taken from the
    open window: GetAllRecipeIDs returns the whole profession, so a Pandaria
    dish exported from the Midnight tab would otherwise be filed under
    Midnight and pollute `--tier Midnight`. See learn_tier_bands.
    """
    bands = learn_tier_bands(exports, cached)
    inserted = useless = 0
    retiered = 0
    rows = []
    for data, _path in exports:
        prof = data.get("profession") or {}
        profession = prof.get("parentProfessionName") or ""
        open_tier = (prof.get("professionName"), prof.get("professionID"))
        for rec in data.get("recipes") or []:
            if cached.get((profession, (rec.get("name") or "").casefold())):
                continue
            slots = [s for s in (rec.get("slots") or [])
                     if s.get("items") and s.get("quantity")]
            output = rec.get("output_item_id")
            recipe_name = clean_name(rec.get("name"))
            if not output or not slots or not recipe_name:
                useless += 1
                continue
            required = [s for s in slots if s.get("required")]
            qmin = rec.get("qty_min") or 1
            tier_name, tier_id = pick_tier(bands.get(profession) or [],
                                           int(rec["id"]), open_tier)
            if tier_name != open_tier[0]:
                retiered += 1
            rows.append((
                -abs(int(rec["id"])), recipe_name,
                prof.get("parentProfessionID"), profession,
                tier_id, tier_name,
                output, qmin, rec.get("qty_max") or qmin,
                # Fallback bill for any code path that reads reagents_json;
                # the slots below are what actually gets costed.
                json.dumps([{"id": s["items"][0], "quantity": s["quantity"]}
                            for s in required]),
                "client",
                1 if any(not s.get("required") for s in slots) else 0,
                json.dumps([
                    {"type": s.get("type"), "required": bool(s.get("required")),
                     "quantity": s.get("quantity"), "items": s.get("items")}
                    for s in slots]),
            ))
            inserted += 1
    db.executemany(
        "INSERT INTO recipe(id,name,profession_id,profession_name,skill_tier_id,"
        "skill_tier_name,crafted_item_id,crafted_qty_min,crafted_qty_max,"
        "reagents_json,crafted_source,uses_slots,slots_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET crafted_item_id=excluded.crafted_item_id, "
        "slots_json=excluded.slots_json, name=excluded.name, "
        "skill_tier_id=excluded.skill_tier_id, "
        "skill_tier_name=excluded.skill_tier_name", rows)
    db.commit()
    distinct = len({r[0] for r in rows})
    if retiered:
        print(f"    {retiered} filed under an older tier than the window that "
              "exported them, going by recipe id")
    return distinct, useless


def resolve_item_names(db_path: str, config_path: str = "config.json",
                       client=None) -> None:
    """Look up names for items the cache references but cannot name.

    The addon exports item ids and no names - it dodges an encoding problem
    that way, and the API knows the names anyway. But the items that legally
    fill a reagent slot are often ones no cached recipe ever mentioned, so
    without this the dashboard shows a reagent bill reading `item 222514`.

    Names never change, so this is a one-off per item: only ids with no name
    are fetched, and a second run has nothing left to do."""
    import wowcraft as W

    if not os.path.exists(db_path):
        print(f"  no cache at {db_path}")
        return
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    named = {r["id"] for r in db.execute(
        "SELECT id FROM item WHERE name IS NOT NULL AND name <> ''")}
    needed = set()
    for row in db.execute("SELECT reagents_json, slots_json, crafted_item_id "
                          "FROM recipe"):
        if row["crafted_item_id"]:
            needed.add(row["crafted_item_id"])
        for reagent in json.loads(row["reagents_json"] or "[]"):
            needed.add(reagent["id"])
        for slot in json.loads(row["slots_json"] or "null") or []:
            needed.update(slot.get("items") or [])
    missing = sorted(needed - named)
    if not missing:
        print("  every referenced item is already named")
        return

    if client is None:
        cfg = W.load_config(config_path)
        if not cfg.get("client_id") or not cfg.get("client_secret"):
            print("  need API credentials in config.json to look up names")
            return
        client = W.BlizzardClient(cfg["client_id"], cfg["client_secret"],
                                  cfg["region"], cfg["locale"])
    print(f"  looking up {len(missing)} unnamed items "
          f"(~{len(missing) / W.RATE_LIMIT_PER_SEC:.0f}s)...")
    payloads = client.get_many(
        ((i, f"/data/wow/item/{i}") for i in missing), "static")

    rows = []
    for item_id, payload in payloads.items():
        name = (payload or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        rows.append((item_id, name,
                     ((payload.get("quality") or {}).get("type")),
                     payload.get("level")))
    db.executemany(
        "INSERT INTO item(id,name,quality,level) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
        "quality=excluded.quality, level=excluded.level", rows)
    db.commit()
    print(f"  named {len(rows)} items")
    # The rest 404ed or came back without a name. Items get removed from the
    # game and their ids linger in recipes, so this is expected rather than a
    # failure - say how many so a growing number is noticeable, and leave them
    # unnamed rather than inventing a label.
    absent = len(missing) - len(rows)
    if absent:
        print(f"    {absent} the API would not name (removed from the game?), "
              "left as raw ids")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", help="path to WowCraftExportDB.lua")
    ap.add_argument("-w", "--wow", default=DEFAULT_WOW,
                    help=f"WoW _retail_ folder (default {DEFAULT_WOW})")
    ap.add_argument("-d", "--db", default="wowcraft.sqlite3")
    ap.add_argument("--dump", metavar="FILE",
                    help="also write the decoded export as JSON")
    ap.add_argument("--names", action="store_true",
                    help="look up names for items the cache references but "
                         "cannot name, so reagent bills stop reading "
                         "\"item 222514\". Implied by --apply.")
    ap.add_argument("-c", "--config", default="config.json",
                    help="config.json holding the API credentials")
    ap.add_argument("--apply", action="store_true",
                    help="write the client's outputs and reagent slots into "
                         "the cache, replacing the API's guesses. Without this "
                         "the run only reports.")
    args = ap.parse_args(argv)

    if args.file:
        paths = [args.file]
    else:
        paths = find_export(args.wow)
        if not paths:
            print(f"No {SAVED_VAR_NAMES[0]} found under {args.wow}.")
            print("In game: open a profession window, /wcexport, then /reload.")
            print("The file only appears once the client flushes it to disk.")
            print("It is named after the addon, not after the saved variable.")
            return 1

    exports = merge_exports(paths)
    print(f"{len(exports)} profession export(s) across {len(paths)} file(s)")

    everything = []
    for data, path in exports:
        prof = data.get("profession") or {}
        print()
        print("=" * 68)
        print(f"{prof.get('professionName', '?')}   ({os.path.basename(path)})")
        print("=" * 68)
        report_export(data)

        recipes = data.get("recipes") or []
        everything += recipes
        print()
        print("what the client knows that the API does not:")
        summarise_slots(recipes)

        print()
        print("against the API-derived cache:")
        # The client reports the open skill line ("Midnight Alchemy") with the
        # base profession alongside it; the cache keys on the base name.
        diff_against_cache(recipes, args.db,
                           prof.get("parentProfessionName") or "")

    if len(exports) > 1:
        print()
        print("=" * 68)
        print(f"ALL PROFESSIONS  ({len(everything)} recipes)")
        print("=" * 68)
        summarise_slots(everything)

    held = load_inventory(paths)
    if held:
        print()
        print("your materials:")
        for who in sorted(held):
            print(f"  {who:28s} {len(held[who]):5d} distinct items, "
                  f"{sum(held[who].values()):,} total")

    if args.apply:
        print()
        print("applying to the cache:")
        apply_to_cache(exports, args.db)
        if held:
            import wowcraft as W
            store = W.Store(args.db)
            rows = store.save_inventory(held)
            print(f"  stored {rows} inventory rows across {len(held)} "
                  "character(s)")
            store.close()

    if args.names and not args.apply:
        print()
        print("resolving item names:")
        resolve_item_names(args.db, args.config)

    if args.dump:
        payload = ([data for data, _path in exports] if len(exports) > 1
                   else exports[0][0])
        with open(args.dump, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
