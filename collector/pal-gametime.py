"""Read the EXACT in-game time from Level.sav and print it as JSON (one line, stdout).

Palworld stores GameTimeSaveData.GameDateTimeTicks - a .NET Int64 tick count (10,000,000 ticks/sec) of
in-game time. floor(ticks / 864000000000) is the day counter (matches /metrics.days); the remainder is the
time of day. This is ground truth - it reflects sleep skips instantly, so the dashboard clock needs no
anchor, no rollover inference, and no midnight assumption.

Cheap (~0.1 s): parses with NO custom properties, so the heavy CharacterSaveParameterMap etc. stay raw bytes.
The collector calls this every poll. Prints nothing (exit 1) if the save can't be read, so the collector
falls back gracefully.

Reads palworldSaveRoot from config.json next to this script.
"""
import json
import os
import sys

import ooz
from palworld_save_tools.gvas import GvasFile
from palworld_save_tools.paltypes import PALWORLD_TYPE_HINTS

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS_PER_DAY = 864000000000  # 10^7 ticks/sec * 86400 sec/day


def save_root():
    # utf-8-sig so a config.json saved by Notepad/PowerShell with a UTF-8 BOM still parses
    with open(os.path.join(HERE, "config.json"), encoding="utf-8-sig") as f:
        return json.load(f)["palworldSaveRoot"]


def newest_level(root):
    best, best_mt = None, -1
    if os.path.isdir(root):
        for d in os.listdir(root):
            f = os.path.join(root, d, "Level.sav")
            if os.path.isfile(f):
                mt = os.path.getmtime(f)
                if mt > best_mt:
                    best, best_mt = f, mt
    return best


def main():
    try:
        root = save_root()
    except Exception:
        sys.exit(1)
    level = newest_level(root)
    if not level:
        sys.exit(1)
    with open(level, "rb") as f:
        raw = f.read()
    magic = raw[8:11]
    if magic == b"PlM":
        data = ooz.decompress(raw[12:], int.from_bytes(raw[0:4], "little"))
    elif magic == b"PlZ":
        import zlib
        data = zlib.decompress(raw[12:])
        if raw[11] == 0x32:
            data = zlib.decompress(data)
    else:
        sys.exit(1)

    # palworld-save-tools prints "Struct type ... not found" notes to stdout; mute them so stdout stays clean JSON
    _real = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w")
        gvas = GvasFile.read(data, PALWORLD_TYPE_HINTS, {})
    finally:
        sys.stdout.close()
        sys.stdout = _real
    ticks = gvas.properties["worldSaveData"]["value"]["GameTimeSaveData"]["value"]["GameDateTimeTicks"]["value"]

    frac = (ticks % TICKS_PER_DAY) / TICKS_PER_DAY
    hour = int(frac * 24)
    minute = int((frac * 24 - hour) * 60)
    print(json.dumps({
        "ticks": ticks,
        "day": ticks // TICKS_PER_DAY,
        "fracOfDay": round(frac, 5),
        "hour": hour,
        "minute": minute,
    }))


if __name__ == "__main__":
    main()
