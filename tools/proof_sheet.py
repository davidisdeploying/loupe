#!/usr/bin/env python3
"""proof_sheet.py — turn an export manifest into a shareable contact sheet (audit 9.9).

    DATA_ROOT=/data/loupe/state tools/proof_sheet.py [manifest.csv] [-o out.html]

One artifact template: header, a stats band in tabular figures, then the frames as a
contact sheet. Two rules shape everything else:

  JS-FREE. The fleet requires shareable HTML to remain readable with JavaScript
  disabled, so there is none -- no scripts, no fetches, no reveal timers. This is print
  design that happens to open in a browser.

  SELF-CONTAINED. Thumbnails are embedded as data URIs. An artifact that references
  /thumb/123.jpg is a link to a server the recipient cannot reach; it would render as a
  page of broken images the moment it left the LAN, which is precisely when it matters.

@media print is included, so a proof sheet is a literal printable contact sheet.

This is also step 1 of 9.4's export ritual: the sheet of every frame in the manifest,
grease-struck, that you look through before the count is typed.
"""
import argparse
import base64
import csv
import datetime
import html
import os
import sys

STATE = os.environ.get("DATA_ROOT", "/data/loupe/state")
THUMBS = os.path.join(STATE, "culling", "contactsheets", "thumbs")
DEFAULT_CSV = os.path.join(STATE, "culling", "candidates-delete.csv")

# Neutral ramp, light variant -- 9.9 asks artifacts to be recognisably Loupe without the
# app chrome, and a shared sheet is read on paper or in Quick Look, not in a dark room.
CSS = """
:root{--p-bg:#fbf9f5;--p-ink:#221e18;--p-mut:#6b6355;--p-line:#ddd6c9;--p-cut:#a8443c;
 --p-mono:ui-monospace,SFMono-Regular,Menlo,monospace;--p-serif:Georgia,'Times New Roman',serif}
*{box-sizing:border-box}
body{margin:0;background:var(--p-bg);color:var(--p-ink);font-family:var(--p-serif);
 font-size:15px;line-height:1.5;padding:32px}
header{border-bottom:2px solid var(--p-ink);padding-bottom:12px;margin-bottom:18px}
h1{font-size:22px;margin:0 0 2px;letter-spacing:.01em}
.sub{font-family:var(--p-mono);font-size:11px;color:var(--p-mut);letter-spacing:.04em;
 text-transform:lowercase;font-variant-caps:all-small-caps}
.stats{display:flex;flex-wrap:wrap;gap:0 28px;margin:14px 0 22px;
 font-family:var(--p-mono);font-size:12px;font-variant-numeric:tabular-nums slashed-zero}
.stats div{padding:2px 0}
.stats b{font-weight:600}
.sheet{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:10px}
figure{margin:0;break-inside:avoid}
.frame{position:relative;border:1px solid var(--p-line);background:#fff;
 aspect-ratio:1;overflow:hidden}
.frame img{width:100%;height:100%;object-fit:cover;display:block;opacity:.55;
 filter:saturate(.82)}
.frame .miss{display:flex;align-items:center;justify-content:center;height:100%;
 font-family:var(--p-mono);font-size:9px;color:var(--p-mut)}
/* the grease strike, same single diagonal as the app (8.1) */
.frame svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
figcaption{font-family:var(--p-mono);font-size:9.5px;color:var(--p-mut);padding-top:3px;
 font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
footer{margin-top:26px;border-top:1px solid var(--p-line);padding-top:10px;
 font-family:var(--p-mono);font-size:10px;color:var(--p-mut)}
@media print{
  body{padding:0;background:#fff}
  .sheet{grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:7px}
  header{border-bottom-width:1px}
  a[href]:after{content:""}
}
"""

STRIKE = ('<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">'
          '<path d="M8 90 C26 68 58 40 92 10" fill="none" stroke="%23a8443c" '
          'stroke-width="4" stroke-linecap="round" opacity="0.85"/></svg>')


def thumb_data_uri(idv, max_bytes=140_000):
    p = os.path.join(THUMBS, "%s.jpg" % idv)
    try:
        if os.path.getsize(p) > max_bytes:
            return None
        with open(p, "rb") as f:
            return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default=DEFAULT_CSV)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--limit", type=int, default=0, help="cap frames (0 = all)")
    args = ap.parse_args()

    if not os.path.exists(args.manifest):
        print("no manifest at %s" % args.manifest, file=sys.stderr)
        return 2
    with open(args.manifest, newline="") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[:args.limit]

    total_bytes = sum(int(r.get("size_bytes") or 0) for r in rows)
    rules = {}
    for r in rows:
        rules[r.get("rule") or "-"] = rules.get(r.get("rule") or "-", 0) + 1

    now = datetime.datetime.now(datetime.timezone.utc)
    central = now - datetime.timedelta(hours=5)
    stamp = "%s UTC / %s CDT" % (now.strftime("%Y-%m-%d %H:%M"), central.strftime("%H:%M"))

    figs, embedded = [], 0
    for r in rows:
        idv = r.get("id", "")
        uri = thumb_data_uri(idv)
        if uri:
            embedded += 1
            inner = '<img src="%s" alt="">%s' % (uri, STRIKE.replace("%23", "#"))
        else:
            inner = '<div class=miss>no thumb</div>%s' % STRIKE.replace("%23", "#")
        cap = "#%s · %s" % (html.escape(idv), html.escape(r.get("rule") or ""))
        figs.append('<figure><div class=frame>%s</div><figcaption>%s</figcaption></figure>'
                    % (inner, cap))

    rule_bits = " · ".join("%s <b>%d</b>" % (html.escape(k), v)
                           for k, v in sorted(rules.items(), key=lambda x: -x[1]))
    doc = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Loupe — proof sheet</title><style>%s</style></head><body>
<header>
 <h1>Proof sheet — frames prepared for deletion</h1>
 <div class=sub>loupe · %s</div>
</header>
<div class=stats>
 <div>frames <b>%d</b></div>
 <div>size <b>%.2f GB</b></div>
 <div>thumbnails embedded <b>%d</b></div>
 <div>%s</div>
</div>
<div class=sheet>%s</div>
<footer>Nothing is deleted by this document. It is a record of intent; acting on it is a
separate, human step. Generated from %s.</footer>
</body></html>""" % (CSS, html.escape(stamp), len(rows), total_bytes / 1e9,
                     embedded, rule_bits, "".join(figs), html.escape(args.manifest))

    out = args.out or os.path.join(os.path.dirname(args.manifest), "proof-sheet.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    print("wrote %s" % out)
    print("  frames %d · %.2f GB · %d thumbnails embedded · %d bytes"
          % (len(rows), total_bytes / 1e9, embedded, len(doc)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
