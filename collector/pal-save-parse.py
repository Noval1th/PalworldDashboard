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
import hashlib
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


_OVERRIDES = {}      # full CharacterID (lowercase, BOSS_ stripped) -> display name; see load_names()


def load_names():
    """Return (names, suffixes) keyed in LOWERCASE.

    Palworld is not consistent about CharacterID capitalisation - the same species appeared as
    "SheepBall" in one world and "Sheepball" in the next - so every lookup is case-insensitive.

    Exact-ID overrides are loaded into a module global rather than returned, to keep pal_name()'s
    signature (and its five call sites) unchanged.
    """
    global _OVERRIDES
    try:
        with open(NAMES, encoding="utf-8") as f:
            d = json.load(f)
        names = {k.lower(): v for k, v in d.get("names", {}).items()}
        suffixes = {k.lower(): v for k, v in d.get("suffixes", {}).items()}
        _OVERRIDES = {k.lower(): v for k, v in d.get("overrides", {}).items()}
        return names, suffixes
    except Exception as e:
        print("pal-names.json unavailable (%s); falling back to internal IDs" % e, file=sys.stderr)
        _OVERRIDES = {}
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
    # A handful of Pals' internal element suffix doesn't match their official variant name: the save calls
    # Dazzi Noct "RaijinDaughter_Water", which the generic rule would render "Dazzi Aqua" - a Pal that does
    # not exist. An exact-ID override wins over the derived name; alpha prefixing still applies on top.
    forced = _OVERRIDES.get(raw.lower())
    if forced:
        return ("Alpha " + forced) if alpha else forced
    base, _, suf = raw.partition("_")
    disp = names.get(base.lower())
    if not disp:
        return cid  # unknown species: show the internal ID rather than invent a name
    if suf:
        disp += " " + suffixes.get(suf.lower(), suf)
    if alpha:
        disp = "Alpha " + disp
    return disp


_NIL_UID = "0" * 32


def _norm_uid(u):
    if not u:
        return None
    s = str(u).replace("-", "").lower()
    return None if (not s or s == _NIL_UID) else s


def _owner_uid(sp):
    """Owning tamer's uid.

    A Pal stationed at a base camp has its OwnerPlayerUId cleared -- the base holds it, not the player --
    but the save still keeps OldOwnerPlayerUIds, whose last entry is the tamer it came from. Without this
    fallback ~10% of Pals (every base worker) show up ownerless. Verified against the live save: it
    recovers all of them, and the recovered uids match the owners we already know.
    """
    o = _norm_uid(unwrap(sp.get("OwnerPlayerUId")))
    if o:
        return o
    old = unwrap(sp.get("OldOwnerPlayerUIds")) or {}
    vals = old.get("values", []) if isinstance(old, dict) else old
    if not isinstance(vals, list):
        return None
    prev = [u for u in (_norm_uid(unwrap(v)) for v in vals) if u]
    return prev[-1] if prev else None      # most recent previous owner


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


def _flagcount(rec, key):
    """Count truthy entries in a {key, value:bool} flag list (unlock lists store only-true, so len==count)."""
    v = unwrap(rec.get(key, []))
    vv = v.get("values", v) if isinstance(v, dict) else v
    if not isinstance(vv, list):
        return 0
    return sum(1 for e in vv if (unwrap(e.get("value")) if isinstance(e, dict) else e))


def ticks_to_ms(t):
    """.NET DateTime ticks (100ns since year 1) -> Unix epoch ms, or None. 62135596800000 = year1->1970 ms."""
    try:
        t = int(t or 0)
    except (TypeError, ValueError):
        return None
    return t // 10000 - 62135596800000 if t > 0 else None


# Pal-soul stat names are stored in Japanese; map the five to short labels for the Pal detail breakdown.
_SOUL_JP = {"最大HP": "HP", "最大SP": "SP", "攻撃": "Atk",
            "防御": "Def", "作業速度": "Work"}


def _souls(sp, key):
    """Total Pal-soul stat points invested + a per-stat breakdown (JP names mapped)."""
    v = unwrap(sp.get(key)) or {}
    vals = v.get("values", []) if isinstance(v, dict) else (v if isinstance(v, list) else [])
    total, bd = 0, {}
    for e in vals:
        if not isinstance(e, dict):
            continue
        pt = int(unwrap(e.get("StatusPoint"), 0) or 0)
        if pt:
            nm = _SOUL_JP.get(str(unwrap(e.get("StatusName")) or ""), None)
            if nm:
                bd[nm] = bd.get(nm, 0) + pt
            total += pt
    return total, bd


def _moves(waza):
    """EquipWaza -> clean move ids: EPalWazaID::AirCanon -> AirCanon, dropping None slots."""
    v = unwrap(waza) or {}
    vals = v.get("values", []) if isinstance(v, dict) else (v if isinstance(v, list) else [])
    out = []
    for m in vals:
        s = str(unwrap(m) if isinstance(m, dict) else m).split("::")[-1]
        if s and s.lower() != "none":
            out.append(s)
    return out


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
            cq = unwrap(pv.get("CompletedQuestArray_FullRelease")) or {}
            cqv = cq.get("values", []) if isinstance(cq, dict) else (cq if isinstance(cq, list) else [])
            out[uid] = {
                "paldeck": _maplen(rec, "PaldeckUnlockFlag"),
                "captures": _mapsum(rec, "PalCaptureCount"),
                "techPoints": int(unwrap(pv.get("bossTechnologyPoint"), 0) or 0),
                "recipes": len(recipes.get("values", [])) if isinstance(recipes, dict) else 0,
                "dungeons": int(unwrap(rec.get("NormalDungeonClearCount"), 0) or 0),
                "fastTravel": _maplen(rec, "FastTravelPointUnlockFlag"),
                "uniqueSpecies": int(unwrap(rec.get("TribeCaptureCount"), 0) or 0),
                "areas": _flagcount(rec, "FindAreaFlagMap"),
                "quests": len(cqv),
                "fieldBosses": _flagcount(rec, "NormalBossDefeatFlag"),
                "towerBosses": _flagcount(rec, "TowerBossDefeatFlag"),
                "effigies": _flagcount(rec, "RelicObtainForInstanceFlag"),
                "fish": _mapsum(rec, "FishingCountMap"),
                "crafted": _mapsum(rec, "CraftItemCount"),
                "lastOnline": ticks_to_ms(unwrap(pv.get("LastOnlineDateTime"))),
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
            o = _owner_uid(sp)
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
            ivh = int(unwrap(sp.get("Talent_HP"), 0) or 0)
            ivs = int(unwrap(sp.get("Talent_Shot"), 0) or 0)
            ivd = int(unwrap(sp.get("Talent_Defense"), 0) or 0)
            iv = ivh + ivs + ivd                       # 0-300: sum of the three IV talents
            gender = (str(unwrap(sp.get("Gender")) or "").split("::")[-1]) or None  # Male / Female
            bond = int(unwrap(sp.get("FriendshipPoint"), 0) or 0)
            owned = ticks_to_ms(unwrap(sp.get("OwnedTime")))
            souls, soul_bd = _souls(sp, "GotStatusPointList")
            moves = _moves(sp.get("EquipWaza"))
            is_fav = unwrap(sp.get("FavoriteIndex")) == 1   # the Palbox "favourite" star (default is absent/None)
            # stable per-Pal id = short hash of the save InstanceId (GUID); survives across weeks for the bracket
            iid = c.get("key", {}).get("InstanceId") if isinstance(c.get("key"), dict) else None
            iid = str(unwrap(iid)) if iid is not None else None
            pid = hashlib.sha1(iid.encode()).hexdigest()[:12] if iid else None
            allpals.append({"nick": nick, "cid": cid, "level": lv, "ivsum": iv, "pid": pid,
                            "ivh": ivh, "ivs": ivs, "ivd": ivd, "gender": gender, "bond": bond,
                            "owned": owned, "souls": souls, "soulbd": soul_bd, "moves": moves,
                            "lucky": is_lucky, "alpha": is_alpha, "favorite": is_fav, "owner": o})
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

    # Pal showcase: guarantee EACH tamer's OWN notable Pals so a per-tamer "Notable Pals" view is personal, not
    # server-relative -- every owner contributes their top 12 by level plus ALL their named/favorited/lucky/alpha.
    # Also keep the server top-20 by level/IV for the global "Top Pals" panel. Bounded so the JSON can't balloon.
    def _topidx(key, n):
        return set(sorted(range(len(allpals)), key=lambda i: -allpals[i][key])[:n])
    by_owner = {}
    for i, pp in enumerate(allpals):
        if pp["owner"]:
            by_owner.setdefault(pp["owner"], []).append(i)
    # must-keep: always-notable Pals + each tamer's own 12 highest-level -> these are never dropped by the cap
    must = {i for i, pp in enumerate(allpals) if pp["nick"] or pp["favorite"] or pp["lucky"] or pp["alpha"]}
    for idxs in by_owner.values():
        must |= set(sorted(idxs, key=lambda i: -allpals[i]["level"])[:12])
        # ...plus each tamer's 8 most RECENTLY caught. Every other rule here selects on level or rarity, so a
        # freshly caught low-level Pal was never published and the dashboard's "recently caught" view would
        # silently show the newest of the survivors rather than the newest overall.
        must |= set(sorted(idxs, key=lambda i: -(allpals[i]["owned"] or 0))[:8])
    keep_idx = sorted(must | _topidx("level", 20) | _topidx("ivsum", 20), key=lambda i: -allpals[i]["level"])
    CAP = 360  # published-Pal ceiling; must-keep ranked first so no tamer's notable set is lost to the cap
    if len(keep_idx) > CAP:
        keep_idx = ([i for i in keep_idx if i in must] + [i for i in keep_idx if i not in must])[:CAP]
    showcase = [allpals[i] for i in keep_idx]
    pals_out = [{"pid": pp["pid"], "nick": pp["nick"], "species": pal_name(pp["cid"], names, suffixes), "level": pp["level"],
                 "iv": round(pp["ivsum"] / 3), "ivHp": pp["ivh"], "ivShot": pp["ivs"], "ivDef": pp["ivd"],
                 "gender": pp["gender"], "bond": pp["bond"], "owned": pp["owned"],
                 "souls": pp["souls"], "soulBreakdown": pp["soulbd"], "moves": pp["moves"],
                 "lucky": pp["lucky"], "alpha": pp["alpha"], "favorite": pp["favorite"],
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
                "recipes": prec.get(uid, {}).get("recipes", 0),
                "fastTravel": prec.get(uid, {}).get("fastTravel", 0),
                "dungeons": prec.get(uid, {}).get("dungeons", 0),
                "uniqueSpecies": prec.get(uid, {}).get("uniqueSpecies", 0),
                "areas": prec.get(uid, {}).get("areas", 0),
                "quests": prec.get(uid, {}).get("quests", 0),
                "fieldBosses": prec.get(uid, {}).get("fieldBosses", 0),
                "towerBosses": prec.get(uid, {}).get("towerBosses", 0),
                "effigies": prec.get(uid, {}).get("effigies", 0),
                "fish": prec.get(uid, {}).get("fish", 0),
                "crafted": prec.get(uid, {}).get("crafted", 0),
                "lastOnline": prec.get(uid, {}).get("lastOnline"),
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
