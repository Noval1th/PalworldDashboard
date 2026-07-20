#!/usr/bin/env python3
"""Regenerate pal-types.json and pal-passives.json from the palworld-save-pal dataset.

This is a DEVELOPMENT tool, not part of collection. It runs by hand when a Palworld update adds
species or passives; the collector only ever reads the two small JSON files it produces.

Why not read this data at collection time? Two reasons. The element of a species and the name of a
passive are static game data - they change on game patches, not on the minute the collector runs -
so fetching them every cycle would add a network dependency that can fail, for data that cannot
change. And the upstream files total ~1.5 MB against the ~50 KB we actually need.

Why this source? It is keyed on the same internal CharacterIDs the save uses (including variant
suffixes like AmaterasuWolf_Dark), so no name-matching guesswork is involved. It also independently
reproduces every override we worked out by hand - RaijinDaughter_Water -> Dazzi Noct, MummyPal ->
Gildra, Sekhmet -> Sekhmet - which is a good sign it agrees with the game rather than with a wiki.

Usage:  python gen-pal-data.py [--offline DIR]
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request

BASE = "https://raw.githubusercontent.com/oMaN-Rod/palworld-save-pal/main/data/json/"
FILES = {
    "pals": "pals.json",
    "passives": "passive_skills.json",
    "skills": "active_skills.json",
    "l10n_passives": "l10n/en/passive_skills.json",
    "l10n_skills": "l10n/en/active_skills.json",
    "l10n_elements": "l10n/en/elements.json",
}
HERE = os.path.dirname(os.path.abspath(__file__))


def fetch(name, rel, offline):
    if offline:
        path = os.path.join(offline, os.path.basename(rel))
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    req = urllib.request.Request(BASE + rel, headers={"User-Agent": "pal-dashboard-gen"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def clean_desc(d):
    """Strip the game's inline colour markup, keeping the values it wraps.

    Descriptions arrive like 'Hunger decreases <NumRed_13>+15.0%</> faster.' - the tags are UI colour
    hints, but the number inside them is the whole point, so the tags go and the contents stay.
    Trailing '.0' on whole percentages is dropped too: '+15.0%' reads better as '+15%'.
    """
    if not d:
        return None
    d = re.sub(r"<[^>]*>", "", d)                 # <NumRed_13> ... </> and friends
    d = re.sub(r"(\d)\.0(?=%|\b)", r"\1", d)      # 15.0% -> 15%
    d = re.sub(r"\s+", " ", d).strip()
    return d or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", help="read the upstream files from this directory instead of fetching")
    args = ap.parse_args()

    src = {k: fetch(k, v, args.offline) for k, v in FILES.items()}
    stamp = datetime.date.today().isoformat()

    # ---- elements ----
    # The game's internal element names are not the ones players see: Normal is Neutral, Leaf is
    # Grass, Earth is Ground, Electricity is Electric. Translate once here so nothing downstream
    # has to know the difference.
    elmap = {k: v["localized_name"] for k, v in src["l10n_elements"].items()}
    elements = {}
    for cid, rec in src["pals"].items():
        if not rec.get("is_pal"):
            continue
        types = [elmap.get(e, e) for e in (rec.get("element_types") or [])]
        if types:                      # human NPCs (Hunter_*, Male_Soldier) legitimately have none
            elements[cid.lower()] = types

    # ---- passives ----
    # rank carries the sign: negative ranks are detrimental traits (Downtrodden, Brittle), positive
    # are beneficial, and 4+ is the Legend/Lucky tier. Keeping it lets the UI separate good from bad
    # without a second table of judgements.
    passives = {}
    for pid, rec in src["passives"].items():
        l10n = src["l10n_passives"].get(pid) or {}
        nm = l10n.get("localized_name")
        if not nm:
            continue
        entry = {"name": nm, "rank": rec.get("rank", 0)}
        d = clean_desc(l10n.get("description"))
        if d:
            entry["desc"] = d
        passives[pid.lower()] = entry

    # ---- active skills (moves) ----
    # Keys upstream are "EPalWazaID::AirCanon"; the save's EquipWaza/MasteredWaza carry the same prefix and
    # the parser strips it, so store the stripped lowercase id to match what the parser hands us.
    # Names matter here: a regex prettifier turns AirCanon into "Air Canon", but the game calls it
    # "Air Cannon". Only the real table gets that right.
    moves = {}
    for mid, rec in src["skills"].items():
        nm = (src["l10n_skills"].get(mid) or {}).get("localized_name")
        if not nm:
            continue
        key = mid.split("::")[-1].lower()
        moves[key] = {
            "name": nm,
            "element": elmap.get(rec.get("element"), rec.get("element")),
            "kind": rec.get("type"),      # Shot / Melee / Support
            "power": rec.get("power"),
        }

    note = ("generated by gen-pal-data.py from github.com/oMaN-Rod/palworld-save-pal "
            "(data/json) - do not edit by hand, rerun the generator")
    out = [
        ("pal-types.json", {"_note": note, "_generated": stamp, "elements": elements}),
        ("pal-passives.json", {"_note": note, "_generated": stamp, "passives": passives}),
        ("pal-moves.json", {"_note": note, "_generated": stamp, "moves": moves}),
    ]
    for fname, doc in out:
        path = os.path.join(HERE, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write("\n")
        n = len(doc.get("elements") or doc.get("passives") or doc.get("moves"))
        print("wrote %s: %d entries (%d KB)" % (fname, n, os.path.getsize(path) // 1024))

    if not elements or not passives or not moves:
        print("ERROR: one of the tables came out empty", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
