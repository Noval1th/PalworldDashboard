#!/usr/bin/env python3
"""Turn the live dashboard page into the repo's de-branded, config-neutral copy.

    python debrand.py <path-to-live-palworld.html>      # writes web/index.html

WHY THIS EXISTS: the repo copy had drifted ~17 KB behind the live page (settings modal, collapsible
sections, Palapalooza UX, join button, broadcast panel - none of it made it back) because de-branding was
done by hand and therefore skipped. Keeping it as a script makes the sync a one-liner, so it actually
happens. Run it whenever the live page changes, then commit web/index.html.

WHAT IT REMOVES, and why each is a *deployment* detail rather than a feature:
  * the server's own name (hardcoded in the live page) -> a generic placeholder that the page fills in at
    runtime from `palworld.json`'s `info.servername`, so every deployment shows its own name for free;
  * the Discord invite -> a DISCORD_URL constant, empty by default, with the button hidden unless set.

NOTE FOR MAINTAINERS: this script is itself published, so it must not contain anybody's server name, host,
invite or paths. It therefore *discovers* the identifiers from the input page and verifies those exact
strings are gone, rather than carrying a hardcoded blocklist.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "web", "index.html")

# Hosts the page is legitimately allowed to reference. Anything else is treated as a deployment detail.
ALLOWED_HOSTS = {"www.w3.org", "www.palpedia.net"}
# Placeholders this script itself introduces, so they are not mistaken for a leaked identity.
GENERIC_NAMES = {"palworld server", "palworld server dashboard"}


def discover_identity(src):
    """Pull the deployment's own name out of the source page, so it can be verified gone afterwards
    without hardcoding it in this (public) file."""
    found = set()
    for pat in (r"<title>([^<]*)</title>",
                r"<h1[^>]*>([^<]+)</h1>",
                r"new Notification\('([^']*)'"):
        m = re.search(pat, src)
        if m:
            v = m.group(1).strip()
            if v and v.lower() not in GENERIC_NAMES:
                found.add(v)
    return found


def sub_once(s, old, new, what):
    assert old in s, "de-brand anchor missing (%s): %r" % (what, old[:60])
    return s.replace(old, new, 1)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: debrand.py <live palworld.html>")
    s = open(sys.argv[1], encoding="utf-8").read().replace("\r\n", "\n")
    before = len(s)
    identity = discover_identity(s)

    # ---- 1. page title ----
    s = re.sub(r"<title>[^<]*</title>", "<title>Palworld Server Dashboard</title>", s, count=1)

    # ---- 2. server name: hardcoded -> filled in from live data ----
    s = sub_once(s, "<h1>", '<h1 id="srvname">', "h1 open tag")
    s = re.sub(r'<h1 id="srvname">[^<]*</h1>', '<h1 id="srvname">Palworld Server</h1>', s, count=1)

    # ---- 3. Discord invite -> opt-in constant ----
    s = re.sub(r'<a class="joinbtn" href="[^"]*"',
               '<a class="joinbtn" id="joinBtn" href="#" style="display:none"', s, count=1)

    # ---- 4. runtime wiring for both of the above ----
    ver_line = ("  document.getElementById('ver').textContent="
                "d.info?((d.info.description||d.info.servername||'')+'  ·  '+(d.info.version||'')):'';")
    s = sub_once(s, ver_line, ver_line + """
  // Name comes from the server's own data, so this page is deployment-neutral.
  if(d.info&&d.info.servername)document.getElementById('srvname').textContent=d.info.servername;""",
                 "version line")

    # The third and least visible copy of the server's name: a desktop-notification title buried in the
    # connection-alerts code. Exactly the kind of thing a hand de-brand misses.
    s = re.sub(r"new Notification\('[^']*'",
               "new Notification(document.getElementById('srvname').textContent||'Palworld Server'",
               s, count=1)

    s = sub_once(s, "<script>\n", """<script>
/* ---- deployment config ----------------------------------------------------
   Set DISCORD_URL to your own Discord invite link to show a "Join the Server"
   button in the header. Left empty, the button stays hidden. */
const DISCORD_URL='';
document.addEventListener('DOMContentLoaded',()=>{
  const b=document.getElementById('joinBtn');
  if(b&&DISCORD_URL){ b.href=DISCORD_URL; b.target='_blank'; b.rel='noopener noreferrer'; b.style.display=''; }
});
""", "script open tag")

    # ---- 5. refuse to publish anything identifying ----
    problems = []
    for name in identity:
        if name in s:
            problems.append("server identity %r still present" % name)
    for host in set(re.findall(r"https?://([A-Za-z0-9.\-]+)", s)):
        if host not in ALLOWED_HOSTS:
            problems.append("unexpected host %r" % host)
    for pat, what in ((r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "IP address"),
                      (r"[A-Za-z]:\\\\[A-Za-z]", "Windows path"),
                      (r"\b[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", "user@host")):
        for m in re.finditer(pat, s):
            problems.append("%s: %r" % (what, m.group(0)))
    if problems:
        sys.exit("REFUSING TO WRITE - identifying strings survived:\n  " + "\n  ".join(problems[:10]))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    open(OUT, "w", encoding="utf-8", newline="\n").write(s)
    print("wrote %s: %d -> %d chars (scrubbed: %s)"
          % (OUT, before, len(s), ", ".join(sorted(identity)) or "nothing found"))


if __name__ == "__main__":
    main()
