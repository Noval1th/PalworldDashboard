"""Palworld save parser -> <dataDir>/palworld-save.json

Reads Level.sav (world save) + each Players/<uid>.sav and extracts guild / Pal / tamer detail that the REST
API does not expose: guilds, Pal counts + top species, world playtime, a trophy summary (Lucky / Alpha / top
Pal), and per-player Paldeck completion / tech points / captures / exploration. Run on a slow cadence
(scheduled task, every ~15 min); the collector merges the JSON into palworld.json.

FORMAT NOTES (Palworld 1.0):
  * Saves use the "PlM" container (Oodle/Kraken compressed), not the old "PlZ" (zlib). palworld-save-tools
    only knows PlZ, so we decompress with `ooz` (pyooz) ourselves and hand the raw GVAS to GvasFile.read.
  * 1.0 appends fields to the character RawData struct -> upstream's decoder raises "EOF not reached".
    We only ever READ, so we stop after the property list and ignore the trailing bytes.
  * 1.0 also inserted two 4-byte fields into the guild struct (one before base_ids, one before
    base_camp_level), so upstream's guild decoder desyncs and dies. We decode the guild prefix by hand and
    resolve membership by testing whether a player's 16-byte UUID appears anywhere in the guild blob -
    that survives Pocketpair appending more fields later.

PRIVACY: player UUIDs are used only to correlate in-memory. Nothing but names/levels/counts is written out.
Reads palworldSaveRoot / dataDir / speciesTotal from config.json next to this script.
"""
import datetime
import json
import os
import struct
import sys
import time

import ooz
from palworld_save_tools.gvas import GvasFile
from palworld_save_tools.paltypes import PALWORLD_CUSTOM_PROPERTIES, PALWORLD_TYPE_HINTS
import palworld_save_tools.rawdata.character as character

HERE = os.path.dirname(os.path.abspath(__file__))
# utf-8-sig so a config.json saved by Notepad/PowerShell with a UTF-8 BOM still parses
with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as _f:
    _CFG = json.load(_f)
SAVEROOT = _CFG["palworldSaveRoot"]
OUT = os.path.join(_CFG["dataDir"], "palworld-save.json")


def find_level():
    """Locate Level.sav. NEVER hardcode the world GUID - a world wipe generates a new one."""
    best, best_mt = None, -1
    if os.path.isdir(SAVEROOT):
        for d in os.listdir(SAVEROOT):
            f = os.path.join(SAVEROOT, d, "Level.sav")
            if os.path.isfile(f):
                mt = os.path.getmtime(f)
                if mt > best_mt:
                    best, best_mt = f, mt
    return best
NAMES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pal-names.json")


def load_names():
    """Return (names, suffixes) keyed in LOWERCASE.

    Palworld is not consistent about CharacterID capitalisation - the same species appeared as
    "SheepBall" in one world and "Sheepball" in the next - so every lookup is case-insensitive.
    """
    try:
        with open(NAMES, encoding="utf-8") as f:
            d = json.load(f)
        names = {k.lower(): v for k, v in d.get("names", {}).items()}
        suffixes = {k.lower(): v for k, v in d.get("suffixes", {}).items()}
        return names, suffixes
    except Exception as e:
        print("pal-names.json unavailable (%s); falling back to internal IDs" % e, file=sys.stderr)
        return {}, {}


def pal_name(cid, names, suffixes):
    """PinkCat -> Cattiva; SheepBall_Ice -> Lamball Cryst; BOSS_Penguin -> Alpha Pengullet.

    Variants are DERIVED from the suffix, not stored, so the table stays small and a new variant of a
    known Pal resolves for free. Anything unknown falls back to the raw internal ID (cosmetic, never wrong).
    Matching is case-insensitive: see load_names().
    """
    if not cid:
        return "?"
    raw = cid
    alpha = False
    if raw.lower().startswith("boss_"):
        alpha = True
        raw = raw[5:]
    base, _, suf = raw.partition("_")
    disp = names.get(base.lower())
    if not disp:
        return cid  # unknown species: show the internal ID rather than invent a name
    if suf:
        disp += " " + suffixes.get(suf.lower(), suf)
    if alpha:
        disp = "Alpha " + disp
    return disp


def char_decode_bytes(parent_reader, char_bytes):
    # 1.0 appends unknown trailing bytes; the property list itself self-terminates, so just stop there.
    reader = parent_reader.internal_copy(bytes(char_bytes), debug=False)
    return {"object": reader.properties_until_end()}


character.decode_bytes = char_decode_bytes

# Decode ONLY the character map. Everything else (foliage, map objects, item containers, base camps, work)
# stays as opaque bytes -- that is what keeps this to well under a second. The guild map is decoded by hand.
PROPS = {
    k: v
    for k, v in PALWORLD_CUSTOM_PROPERTIES.items()
    if k == ".worldSaveData.CharacterSaveParameterMap.Value.RawData"
}


def unwrap(v, default=None):
    """SaveParameter values are {"type":..,"value":..}; sometimes nested one deeper."""
    while isinstance(v, dict) and "value" in v:
        v = v["value"]
    return default if v is None else v


def guid_disk_bytes(uid_hex):
    """UE serialises a GUID as four little-endian uint32s, so each 4-byte group is byte-swapped on disk."""
    b = bytes.fromhex(uid_hex)
    return b[0:4][::-1] + b[4:8][::-1] + b[8:12][::-1] + b[12:16][::-1]


def read_fstring(b, off):
    (n,) = struct.unpack("<i", b[off : off + 4])
    off += 4
    if n == 0:
        return "", off
    if n < 0:  # UTF-16
        n = -n * 2
        return b[off : off + n - 2].decode("utf-16-le", "replace"), off + n
    return b[off : off + n - 1].decode("utf-8", "replace"), off + n


def parse_guild(b):
    """Decode the stable prefix of a Guild RawData blob (see FORMAT NOTES)."""
    off = 16  # group_id
    _name, off = read_fstring(b, off)  # group_name (the owner's UID as hex, not the guild name)
    (handles,) = struct.unpack("<i", b[off : off + 4])
    off += 4 + handles * 32  # individual_character_handle_ids: (guid, guid) pairs = players AND pals
    off += 1  # org_type
    off += 4  # 1.0: unknown i32
    (nbases,) = struct.unpack("<i", b[off : off + 4])
    off += 4 + nbases * 16  # base_ids
    off += 4  # 1.0: unknown i32
    (camp_level,) = struct.unpack("<i", b[off : off + 4])
    off += 4
    (npoints,) = struct.unpack("<i", b[off : off + 4])
    off += 4 + npoints * 16  # map_object_instance_ids_base_camp_points
    guild_name, off = read_fstring(b, off)
    return {"name": guild_name, "baseLevel": camp_level, "bases": nbases, "handles": handles}


# Paldeck size for the completion %. Set "speciesTotal" in config.json for your game version; the
# leaderboard ranks on raw count, so this only affects the displayed percentage.
SPECIES_TOTAL = int(_CFG.get("speciesTotal", 137))


def _maplen(rec, key):
    v = unwrap(rec.get(key, {}))
    vv = v.get("values", v) if isinstance(v, dict) else v
    return len(vv) if hasattr(vv, "__len__") and not isinstance(vv, str) else 0


def _mapsum(rec, key):
    v = unwrap(rec.get(key, {}))
    vv = v.get("values", v) if isinstance(v, dict) else v
    if isinstance(vv, list):
        return sum(int(unwrap(e.get("value", 0)) or 0) for e in vv if isinstance(e, dict))
    return 0


def read_player_records(level_path):
    """Parse each Players/<uid>.sav for the per-player progression the world save doesn't hold:
    Paldeck completion (RecordData.PaldeckUnlockFlag = species ever discovered, survives releases),
    total captures, tech points, unlocked recipes, dungeon clears, fast-travel points. Keyed by uid (hex).
    Small files (4-12 KB) -> ~0.1 s each. A single bad file is skipped, never fatal."""
    import contextlib
    import io

    pdir = os.path.join(os.path.dirname(level_path), "Players")
    out = {}
    if not os.path.isdir(pdir):
        return out
    for fn in os.listdir(pdir):
        low = fn.lower()
        if not low.endswith(".sav") or "_dps" in low:  # _dps.sav = Pal storage, not the player record
            continue
        uid = fn.split(".")[0].replace("-", "").lower()
        try:
            with open(os.path.join(pdir, fn), "rb") as f:
                raw = f.read()
            if raw[8:11] != b"PlM":
                continue
            data = ooz.decompress(raw[12:], int.from_bytes(raw[0:4], "little"))
            with contextlib.redirect_stdout(io.StringIO()):
                g = GvasFile.read(data, PALWORLD_TYPE_HINTS, PALWORLD_CUSTOM_PROPERTIES)
            pv = g.properties["SaveData"]["value"]
            rec = unwrap(pv.get("RecordData")) or {}
            recipes = unwrap(pv.get("UnlockedRecipeTechnologyNames", {}))
            out[uid] = {
                "paldeck": _maplen(rec, "PaldeckUnlockFlag"),
                "captures": _mapsum(rec, "PalCaptureCount"),
                "techPoints": int(unwrap(pv.get("bossTechnologyPoint"), 0) or 0),
                "recipes": len(recipes.get("values", [])) if isinstance(recipes, dict) else 0,
                "dungeons": int(unwrap(rec.get("NormalDungeonClearCount"), 0) or 0),
                "fastTravel": _maplen(rec, "FastTravelPointUnlockFlag"),
            }
        except Exception as e:
            print("player save %s failed: %s" % (fn, e), file=sys.stderr)
    return out


def main():
    t0 = time.time()
    level = find_level()
    if not level:
        raise SystemExit("no Level.sav under " + SAVEROOT)

    with open(level, "rb") as f:
        raw = f.read()
    world_kb = round(len(raw) / 1024)

    ulen = int.from_bytes(raw[0:4], "little")
    magic = raw[8:11]
    if magic == b"PlM":
        data = ooz.decompress(raw[12:], ulen)  # Oodle/Kraken
    elif magic == b"PlZ":
        import zlib

        data = zlib.decompress(raw[12:])
        if raw[11] == 0x32:
            data = zlib.decompress(data)
    else:
        raise SystemExit("unknown save container: %r" % magic)

    gvas = GvasFile.read(data, PALWORLD_TYPE_HINTS, PROPS)
    wsd = gvas.properties["worldSaveData"]["value"]

    # ---- world playtime (real seconds this world has run) ----
    gtv = unwrap(wsd.get("GameTimeSaveData", {}))
    playtime_sec = int(unwrap(gtv.get("RealDateTimeTicks", 0)) or 0) // 10000000 if isinstance(gtv, dict) else 0

    # ---- characters: players and pals ----
    players = {}  # uid_hex -> {name, level}
    pals_by_owner = {}  # uid_hex -> count
    species = {}
    pal_total = 0
    lucky = 0      # IsRarePal (shiny/"Lucky") Pals
    alphas = 0     # BOSS_ prefix (Alpha/boss) Pals
    top_lv, top_cid, top_owner, top_nick = 0, None, None, None
    owner_flags = {}   # uid -> {"lucky": n, "alphas": n}
    allpals = []       # every Pal: {nick, cid, level, ivsum, lucky, alpha, owner} -> ranked into a bounded showcase
    for c in wsd.get("CharacterSaveParameterMap", {}).get("value", []):
        sp = c["value"]["RawData"]["value"]["object"]["SaveParameter"]["value"]
        if unwrap(sp.get("IsPlayer")):
            uid = str(unwrap(c["key"]["PlayerUId"])).replace("-", "").lower()
            players[uid] = {
                "name": unwrap(sp.get("NickName"), "?") or "?",
                "level": int(unwrap(sp.get("Level"), 1) or 1),
            }
        else:
            pal_total += 1
            cid = unwrap(sp.get("CharacterID"))
            if cid:
                species[cid] = species.get(cid, 0) + 1
            o = unwrap(sp.get("OwnerPlayerUId"))
            o = str(o).replace("-", "").lower() if o else None
            if o:
                pals_by_owner[o] = pals_by_owner.get(o, 0) + 1
            lv = int(unwrap(sp.get("Level"), 1) or 1)
            is_lucky = bool(unwrap(sp.get("IsRarePal")))
            is_alpha = bool(str(cid).upper().startswith("BOSS"))
            if is_lucky:
                lucky += 1
            if is_alpha:
                alphas += 1
            if o and (is_lucky or is_alpha):
                f = owner_flags.setdefault(o, {"lucky": 0, "alphas": 0})
                f["lucky"] += int(is_lucky)
                f["alphas"] += int(is_alpha)
            nick = unwrap(sp.get("NickName"))          # present only once a Pal has been renamed
            nick = str(nick).strip() if nick else None
            iv = (int(unwrap(sp.get("Talent_HP"), 0) or 0) + int(unwrap(sp.get("Talent_Shot"), 0) or 0)
                  + int(unwrap(sp.get("Talent_Defense"), 0) or 0))   # 0-300: sum of the three IV talents
            allpals.append({"nick": nick, "cid": cid, "level": lv, "ivsum": iv,
                            "lucky": is_lucky, "alpha": is_alpha, "owner": o})
            if lv > top_lv:
                top_lv, top_cid, top_owner, top_nick = lv, cid, o, nick

    # ---- guilds ----
    guilds = []
    for g in wsd.get("GroupSaveDataMap", {}).get("value", []):
        if g["value"]["GroupType"]["value"]["value"] != "EPalGroupType::Guild":
            continue
        blob = bytes(g["value"]["RawData"]["value"]["values"])
        try:
            gd = parse_guild(blob)
        except Exception as e:
            print("guild parse failed: %s" % e, file=sys.stderr)
            continue
        # membership: a member's 16-byte UUID appears verbatim in the blob. Robust against appended fields.
        members = []
        for uid, p in players.items():
            if guid_disk_bytes(uid) in blob:
                members.append({"name": p["name"], "level": p["level"], "pals": pals_by_owner.get(uid, 0)})
        members.sort(key=lambda m: -m["level"])
        name = gd["name"] or "Unnamed Guild"
        guilds.append(
            {
                "name": name,
                "baseLevel": gd["baseLevel"],
                "bases": gd["bases"],
                "members": members,
                "pals": max(gd["handles"] - len(members), 0),
            }
        )
    guilds.sort(key=lambda x: (-x["baseLevel"], -len(x["members"])))

    names, suffixes = load_names()
    top = sorted(species.items(), key=lambda x: -x[1])[:10]
    # surface gaps in the name table instead of hiding them behind a fallback
    unknown = sorted({c for c in species if pal_name(c, names, suffixes) == c})
    if unknown:
        print("unmapped species (showing internal IDs): %s" % ", ".join(unknown), file=sys.stderr)

    # Pal showcase: a BOUNDED "notable" set (top 20 by level + top 20 by IV + all lucky/alpha/named), so the
    # list can't balloon on a big server. The dashboard ranks/filters this client-side by the chosen dimension.
    def _topidx(key, n):
        return set(sorted(range(len(allpals)), key=lambda i: -allpals[i][key])[:n])
    keep = _topidx("level", 20) | _topidx("ivsum", 20) | {i for i, pp in enumerate(allpals) if pp["lucky"] or pp["alpha"] or pp["nick"]}
    showcase = sorted((allpals[i] for i in keep), key=lambda pp: -pp["level"])[:80]
    pals_out = [{"nick": pp["nick"], "species": pal_name(pp["cid"], names, suffixes), "level": pp["level"],
                 "iv": round(pp["ivsum"] / 3), "lucky": pp["lucky"], "alpha": pp["alpha"],
                 "owner": players.get(pp["owner"], {}).get("name") if pp["owner"] else None}
                for pp in showcase]

    # per-player progression from the individual player saves (Paldeck / tech / captures / exploration)
    prec = read_player_records(level)

    out = {
        "parsedAt": datetime.datetime.now().isoformat(),
        "parseSeconds": round(time.time() - t0, 2),
        "worldKb": world_kb,
        "playtimeSeconds": playtime_sec,
        "speciesTotal": SPECIES_TOTAL,
        "totals": {
            "pals": pal_total,
            "species": len(species),
            "players": len(players),
            "guilds": len(guilds),
        },
        "trophy": {
            "lucky": lucky,
            "alphas": alphas,
            "topPal": {
                "name": pal_name(top_cid, names, suffixes),
                "level": top_lv,
                "owner": players.get(top_owner, {}).get("name") if top_owner else None,
                "nick": top_nick,
            } if top_cid else None,
        },
        "pals": pals_out,
        "topSpecies": [{"name": pal_name(n, names, suffixes), "id": n, "count": c} for n, c in top],
        "unmappedSpecies": unknown,
        "guilds": guilds,
        # keyed by tamer NAME so the collector can merge into the roster without ever touching a UUID
        "tamers": {
            p["name"]: {
                "level": p["level"],
                "pals": pals_by_owner.get(uid, 0),
                "guild": next((g["name"] for g in guilds if any(m["name"] == p["name"] for m in g["members"])), None),
                "paldeck": prec.get(uid, {}).get("paldeck", 0),
                "captures": prec.get(uid, {}).get("captures", 0),
                "techPoints": prec.get(uid, {}).get("techPoints", 0),
                "recipes": prec.get(uid, {}).get("recipes", 0),
                "fastTravel": prec.get(uid, {}).get("fastTravel", 0),
                "dungeons": prec.get(uid, {}).get("dungeons", 0),
                "lucky": owner_flags.get(uid, {}).get("lucky", 0),
                "alphas": owner_flags.get(uid, {}).get("alphas", 0),
            }
            for uid, p in players.items()
        },
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print("wrote %s in %.2fs: %d pals / %d species / %d guilds" % (OUT, out["parseSeconds"], pal_total, len(species), len(guilds)))


if __name__ == "__main__":
    main()
