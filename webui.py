"""The dashboard as a web page, redrawn in the browser at whatever the screen is.

The MJPEG stream exists because the Miyoo can only decode video: it is a 640x480
bitmap, and on anything sharper than the panel it was drawn for it looks like a
640x480 bitmap. A Windows handheld runs a browser, so it can be sent the numbers
instead and lay them out in vectors — text stays crisp at 1080p or 1600p, and the
PC stops re-encoding JPEGs for it.

It is the same dashboard, not a second one: the same tiles with the same numbers
in the same palettes, generated from :mod:`theme` so the two clients cannot drift
apart. What differs is how much of it fits at once. The panel has two pages
because 640x480 holds half a dashboard; a 1280x720 window holds four columns, so
there both pages' tiles are laid out together and nothing has to be paged to. A
portrait or narrow window falls back to the two pages.

Everything is driven from the keyboard, because a handheld has no mouse:

    ← → / PgUp PgDn / 1 2   turn the page (only when it is paged)
    T                       next theme
    F                       fullscreen
    [ ]                     slower / faster refresh
    N / P / 0               next / previous / this machine
    R                       re-scan the LAN for machines
    H or ?                  key list

None of those keys exist on a phone, and a Windows handheld has a touchscreen
long before it has a keyboard, so every one of them is also a button in a row
along the bottom, a swipe across the tiles turns the page, and the page
indicator is itself a control. Below 560px — a phone, held in one hand — the
dashboard stops being a fixed screenful: the tiles keep the height their own
contents need and the page scrolls, because ten tiles cut from a 390px screen
are ten tiles nobody can read.

N is what makes this more than a view of one PC: the machine serving the page
sweeps the subnet for other PC Monitors (a browser cannot probe a port itself),
and the page then fetches that machine's snapshot directly from it — which is
why the read-only endpoints answer with an open CORS header.

The page holds no state the server needs to know about: the theme, the page, the
refresh interval and the machine being watched are remembered in localStorage, so
a handheld that reopens the page comes back the way it was left without the PC
having to track it. The PC's settings supply only the defaults.

Two of those defaults are settings rather than constants because they are the
handheld's business rather than the dashboard's: how often to poll — a panel on
battery may want one second or ten, and unlike the MJPEG stream, slowing this
down costs nothing in fidelity — and whether the key map shows in the corner.
"""

from __future__ import annotations

import json

import theme

REFRESH_MS = 1000


def _theme_css() -> str:
    """Every theme as a block of custom properties, keyed by ``data-theme``."""
    blocks = []
    for name in theme.NAMES:
        decls = ";".join(
            f"--{key.lower().replace('_', '-')}:{value}"
            for key, value in theme.hex_tokens(name).items())
        blocks.append(f'html[data-theme="{name}"]{{{decls}}}')
    return "\n".join(blocks)


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
/* Tuned so a 1280x720 handheld lands on ~16px rather than the 12px the old
   height-led formula gave it: at that size the panel is an arm's length away,
   and 12px text is what made the web page harder to read than the 640x480
   stream it replaced.
   The fixed 8px term is what makes Ctrl +/- do anything. A size written purely
   in vw/vh is zoom-proof by construction — zooming shrinks the viewport in CSS
   pixels by exactly as much as it magnifies them — so the px half of the sum is
   the part the browser's zoom can still act on. */
html{font-size:clamp(12px,8px + 0.33vh + 0.39vw,24px);color-scheme:dark;
  -webkit-text-size-adjust:100%}
/* dvh after vh, not instead of it: on a phone the address bar slides away and
   100vh keeps the taller number, so a page sized in vh is a screenful plus a
   bar's worth of scroll that nothing is written in. Browsers that never had the
   problem also never had dvh and keep the first line. */
body{height:100vh;height:100dvh;overflow:hidden;background:var(--plane);
  color:var(--ink);
  font:1rem/1.35 "Segoe UI","Microsoft YaHei UI","Microsoft YaHei",system-ui,
  sans-serif;font-variant-numeric:tabular-nums;
  -webkit-font-smoothing:antialiased;-webkit-tap-highlight-color:transparent}
html[data-theme="term"] body{font-family:"Cascadia Mono",Consolas,
  "Microsoft YaHei UI",ui-monospace,monospace;letter-spacing:.01em}

#app{height:100vh;height:100dvh;display:flex;flex-direction:column;gap:.35rem;
  padding:.45rem}

/* --- header --- */
header#top{display:flex;align-items:center;gap:.8rem;flex:0 0 auto;
  padding:0 .3rem}
#host{font-weight:600;color:var(--ink2);white-space:nowrap}
#pages{display:flex;gap:.35rem}
.pill{border:1px solid var(--border);border-radius:99px;padding:.05rem .6rem;
  font-size:.85rem;color:var(--muted);background:none;font:inherit;
  font-size:.85rem;line-height:1.35;cursor:pointer}
.pill.on{background:var(--surface);border-color:var(--c-cpu);color:var(--ink)}
#top .grow{flex:1 1 auto;min-width:0}
#wx,#clock,#rate{white-space:nowrap;color:var(--ink2)}
#clock{font-weight:600;color:var(--ink);font-size:1.3rem}
#rate,#link{font-size:.8rem;color:var(--muted)}
#link.bad{color:var(--s-crit)}

/* --- grid --- */
main{flex:1 1 auto;min-height:0;display:grid;gap:.35rem}
main::-webkit-scrollbar{width:.3rem}
main::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px}

/* One screen, everything on it. A 1280x720 window is wide enough for four
   columns, which is what lets both pages' tiles share one view; paging is left
   to the portrait handhelds below, which have no room for this.
   The AI gauge strip is dropped here because the detail table right below it
   says the same numbers and more — showing both would spend a row on nothing.

   The rows are minmax(rem, fr) rather than plain fr so that zoom has somewhere
   to go: the floors are in rem, so zooming in grows them, and once they no
   longer add up to a screen the dashboard scrolls instead of pressing every
   tile down to a clipped half-row. Unzoomed the floors are slack — 720p has
   about 44rem of room against 33rem of floor — so nothing scrolls until the
   text really is too big for the window. */
main.all{grid-template-columns:repeat(4,1fr);overflow-y:auto;
  grid-template-rows:minmax(11rem,1.81fr) minmax(6.6rem,.94fr) minmax(7.5rem,1.62fr)
    minmax(7rem,1.22fr) minmax(5.7rem,.93fr);
  grid-template-areas:
    "cpu cpu gpu fps" "cputop gputop memtop disk" "net net docker docker"
    "aidet aidet advice advice" "wx wx pwr pwr"}

main.p0{grid-template-columns:repeat(3,1fr);
  grid-template-rows:1.5fr 1.1fr 1.1fr 1.15fr 1fr .5fr;
  grid-template-areas:
    "cpu cpu cpu" "fps gpu memtop" "cputop gputop disk"
    "net net net" "ai ai ai" "pwr pwr pwr"}
main.p1{grid-template-columns:repeat(3,1fr);
  grid-template-rows:1.3fr 1fr .5fr;
  grid-template-areas:"docker docker docker" "advice aidet aidet" "wx wx wx"}
@media (max-aspect-ratio:1/1){
  main.p0{grid-template-columns:repeat(2,1fr);
    grid-template-rows:1.5fr 1.2fr 1.2fr 1.2fr 1.3fr 1fr .55fr;
    grid-template-areas:
      "cpu cpu" "fps gpu" "cputop gputop" "memtop disk"
      "net net" "ai ai" "pwr pwr"}
  main.p1{grid-template-columns:1fr;
    grid-template-rows:1.2fr 1fr 1.2fr auto;
    grid-template-areas:"docker" "advice" "aidet" "wx"}
}

/* --- tiles --- */
.tile{grid-area:var(--area);background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:.35rem .6rem;display:flex;
  flex-direction:column;min-width:0;min-height:0;overflow:hidden}
.tile > h2{font-size:.95rem;font-weight:600;color:var(--ink2);
  display:flex;justify-content:space-between;gap:.5rem;align-items:baseline;
  flex:0 0 auto}
.tile > h2 em{font-style:normal;font-size:.82rem;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* "safe" centring: once zoom makes the contents taller than the tile they pile
   downwards from the top instead of spilling equally in both directions, which
   is what used to push a tile's first line up behind its own heading. */
.body{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;
  justify-content:safe center;gap:.25rem}
.row{display:flex;align-items:baseline;gap:.5rem;min-width:0}
.row.wrapok{flex-wrap:wrap}
.grow{flex:1 1 auto;min-width:0}
.hero{font-size:2.5rem;font-weight:700;line-height:1;letter-spacing:-.02em}
.hero small{font-size:.95rem;font-weight:600;margin-left:.1rem;color:var(--muted)}
.sub{font-size:.85rem;color:var(--muted);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.k{font-size:.82rem;color:var(--muted)}
.v{font-weight:600}
.right{margin-left:auto;text-align:right}
.dim{color:var(--muted)}
.good{color:var(--s-good)}.warn{color:var(--s-warn)}.crit{color:var(--s-crit)}

/* meters and sparklines */
.meter{display:block;width:100%;height:.42rem;border-radius:99px;
  overflow:hidden;flex:1 1 auto;min-width:2rem}
.meter i{display:block;height:100%;border-radius:99px}
.spark{width:100%;height:100%;display:block}
.sparkwrap{flex:1 1 auto;min-height:1.4rem;display:flex;
  border-bottom:1px solid var(--grid)}
.netrow{flex:1 1 0;min-height:0;display:flex;flex-direction:column;gap:.1rem}

/* core grid
   The column count is set from the core count in JS rather than left to
   auto-fit, because this grid is read by comparing the cells against each
   other: auto-fit gives the last row as many columns as it has cells, so four
   cores spread out into cells three times the width of the ones above them and
   the layout reads as a difference in the numbers. Fixed columns and a fixed
   cell height keep every core the same shape, and the two lines inside a cell
   are always both present — a cell missing its clock line would be a hole in
   the same way. */
.cores{display:grid;gap:.15rem;flex:0 0 auto}
.core{background:var(--plane);border-radius:calc(var(--radius) / 2);
  padding:.1rem .2rem;font-size:.7rem;color:var(--muted);line-height:1.15;
  min-width:0;text-align:center;font-variant-numeric:tabular-nums}
.core b{display:block;font-size:.8rem;color:var(--ink2);font-weight:600;
  overflow:hidden}
.core em{display:block;font-style:normal;overflow:hidden;white-space:nowrap;
  text-overflow:ellipsis}
.core .bar{height:.18rem;border-radius:99px;background:var(--grid);
  margin-top:.15rem;overflow:hidden}
.core .bar i{display:block;height:100%;background:var(--c-cpu)}

/* tables */
table{width:100%;border-collapse:collapse;font-size:.92rem}
td{padding:.1rem 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.name{max-width:0;width:99%;color:var(--ink2)}
td.num{text-align:right;padding-left:.5rem;font-weight:600}
td.bar{width:32%;padding-left:.5rem}
.dot{display:inline-block;width:.45rem;height:.45rem;border-radius:99px;
  margin-right:.35rem;vertical-align:middle}
.scroll{overflow-y:auto;min-height:0;flex:1 1 auto}
.scroll::-webkit-scrollbar{width:.3rem}
.scroll::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px}
/* The lists that scroll themselves (see the loop below) keep the bar out of the
   way: nobody is dragging it, and a permanent 0.3rem gutter down a tile that is
   already short of width is worse than no bar at all. */
.scroll.loop{scrollbar-width:none}
.scroll.loop::-webkit-scrollbar{width:0}

/* ai gauges */
/* Capped rather than stretched: a gauge as wide as a fifth of a 27" screen puts
   its label and its number too far apart to read as one thing. */
.gauges{display:grid;grid-template-columns:repeat(5,minmax(0,14rem));
  justify-content:space-between;gap:.5rem;flex:0 0 auto}
@media (max-aspect-ratio:1/1){
  .gauges{grid-template-columns:repeat(3,minmax(0,1fr))}}
.gauge{display:flex;flex-direction:column;gap:.15rem;min-width:0}
.gauge .top{display:flex;justify-content:space-between;align-items:baseline;
  gap:.3rem;font-size:.8rem;color:var(--muted)}
.gauge .top b{font-size:1rem;font-weight:700}
.gauge .rst{font-size:.72rem;color:var(--muted);text-align:right;
  min-height:.8rem}
.notes{font-size:.8rem;color:var(--muted);display:flex;gap:.4rem;
  flex-wrap:wrap;align-items:baseline}

/* quota cards */
/* Four windows across two providers, in a tile that is wide and short — so all
   four cards go in one row, each one three short lines tall, which is what the
   tile's floor height has room for. The column count is spelled out rather than
   left to auto-fit: a fitted third column would wrap the fourth card onto a
   second row the tile has no height for. A handheld turns the same four into a
   2x2, where the tile is full width and taller. The count is fixed, so unlike
   the container list this tile can never outgrow itself or need to scroll. */
.qcards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:.35rem;align-content:center;min-height:0}
@media (max-aspect-ratio:1/1){
  .qcards{grid-template-columns:repeat(2,minmax(0,1fr))}}
.qcard{display:flex;flex-direction:column;gap:.18rem;min-width:0;
  padding:.3rem .45rem;border:1px solid var(--border);border-radius:.5rem;
  background:color-mix(in srgb,var(--c-ai) 6%,transparent)}
.qcard .head{display:flex;justify-content:space-between;align-items:baseline;
  gap:.3rem;font-size:.78rem;color:var(--muted);white-space:nowrap}
.qcard .head b{font-weight:600;color:var(--ink2)}
.qcard .head .left{font-size:1.15rem;font-weight:700}
.qcard .foot{display:flex;justify-content:space-between;gap:.3rem;
  font-size:.72rem;color:var(--muted);white-space:nowrap}

/* advice */
.para{font-size:1.02rem;line-height:1.45;color:var(--ink);overflow:hidden}
.wxrow{display:flex;gap:.5rem;justify-content:space-between;text-align:center;
  flex:1 1 auto;align-items:center}
.wxcell{flex:1 1 0;min-width:0}
.wxcell .k{display:block}
.wxcell b{font-size:1.15rem}

/* The key map, as a footnote rather than a toolbar: nothing on this page is
   clickable, so a first-time reader has to be told the keys exist somewhere —
   but only once, which is why it is the smallest and dimmest thing on screen
   and can be turned off on the settings page.
   It rides in the header rather than at the foot of the page because the grid
   is sized to fill the window exactly: a line of its own at the bottom would
   come out of the tiles, and an overlay in the corner would sit on top of
   whatever the bottom-right tile had to say. The header has spare width and no
   spare height, which is precisely what this needs. Below the width where that
   is true it goes away — H still opens the full table. */
#hints{font-size:.72rem;color:var(--muted);opacity:.7;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;min-width:0}
@media (max-width:860px){#hints{display:none}}

/* --- the button bar ---
   The keys stay the fast way to drive this, and on the Miyoo they are the only
   way — but a Windows handheld is a touchscreen and a phone is nothing else, so
   on those there is no key to press and every key needs a control here.
   It is a row of the flex column rather than something floating in a corner for
   the same reason the hints ride in the header: the grid is sized to fill the
   window exactly, and a bar hovering over the bottom-right tile would sit on
   whatever that tile had to say. It costs about 2rem of the tiles, which the
   layout has to spare, and it can be turned off on the settings page for a
   handheld that is only ever driven by its buttons.
   The two live readouts in it — the palette's name and the refresh interval —
   are why the header's dim little #rate can go away on a phone: the same two
   numbers, at a size that survives being read at arm's length. */
#bar{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:.3rem;align-items:center;
  justify-content:center;padding-bottom:env(safe-area-inset-bottom)}
#bar .grp{display:flex;align-items:stretch}
#bar button,#bar .val{font:inherit;font-size:.85rem;line-height:1;
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:.4rem .7rem;min-height:2.1rem;
  display:flex;align-items:center;justify-content:center;white-space:nowrap;
  -webkit-user-select:none;user-select:none}
#bar button{color:var(--ink2);min-width:2.2rem;cursor:pointer;
  touch-action:manipulation}
#bar .val{color:var(--muted);font-size:.78rem;font-variant-numeric:tabular-nums}
/* Segmented, so ‹ and › read as two halves of one control rather than as two
   unrelated things that happen to be next to each other. */
#bar .grp > *{border-radius:0;margin-left:-1px}
#bar .grp > :first-child{border-radius:var(--radius) 0 0 var(--radius);
  margin-left:0}
#bar .grp > :last-child{border-radius:0 var(--radius) var(--radius) 0}
/* No :hover rule: on a touchscreen the last thing tapped keeps the hover state
   until something else is, which would leave a button looking pressed. */
#bar button:active{background:var(--plane);border-color:var(--c-cpu);
  color:var(--ink)}
#bar button:focus-visible{outline:2px solid var(--c-cpu);outline-offset:1px}
/* Nothing to turn to when every tile is already on screen. */
#bar.single .paged{display:none}

/* help */
#help{position:fixed;inset:0;background:color-mix(in srgb,var(--plane) 88%,#000);
  display:none;align-items:center;justify-content:center;z-index:9}
#help.show{display:flex}
#help .card{background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.2rem 1.6rem;min-width:22rem;
  max-width:min(94vw,34rem);max-height:88vh;overflow-y:auto}
#help h3{font-size:1rem;margin-bottom:.7rem;color:var(--ink)}
#help td{padding:.15rem .4rem;font-size:.85rem}
#help td:first-child{color:var(--c-cpu);font-weight:700;white-space:nowrap}
#help td:last-child{color:var(--ink2)}
#help .close{margin:.9rem auto 0;display:block;font:inherit;font-size:.85rem;
  color:var(--ink2);background:var(--plane);border:1px solid var(--border);
  border-radius:var(--radius);padding:.45rem 1.4rem;cursor:pointer;
  touch-action:manipulation}

/* --- phones ---------------------------------------------------------------
   Everything above assumes a screen with room for two columns and enough height
   that a tile can be given a fraction of it. A phone is neither: the fluid type
   size lands on 12px at 390px wide, and ten tiles carved out of one screenful
   leave each of them a line and a half. So below this width the dashboard stops
   being a fixed screenful and becomes a column that scrolls — each tile keeps
   the height its own contents need, the page runs past the bottom of the
   window, and the button bar sticks to the foot of the screen where a thumb
   already is. The two pages stay: they are what keeps that column short enough
   to be worth scrolling through. */
@media (max-width:560px){
  html{font-size:clamp(13px,7px + 2.1vw,19px)}
  body{height:auto;min-height:100dvh;overflow-y:auto;overflow-x:hidden;
    overscroll-behavior-y:contain}
  #app{height:auto;min-height:100dvh;padding:.4rem}
  /* Still one line, but a narrow one: the clock and the page pills stay, the
     machine's name gives way first, and the two dim readouts go entirely —
     the bar says the refresh rate, and the weather has a tile of its own. */
  header#top{gap:.45rem;padding:0 .1rem}
  #host{overflow:hidden;text-overflow:ellipsis;min-width:0}
  #pages{flex:0 0 auto}
  #clock{font-size:1.1rem}
  #wx,#rate{display:none}
  main.p0,main.p1{display:flex;flex-direction:column;overflow:visible}
  .tile{flex:0 0 auto}
  .hero{font-size:2.2rem}
  /* No tile height left to be a fraction of, so the self-scrolling lists get a
     ceiling of their own: fifteen containers would otherwise push everything
     below them three screenfuls down. */
  .scroll{max-height:13rem}
  /* A tile is as tall as its contents here, and an SVG told to be as wide as
     the screen would otherwise claim a third of that width in height — a
     sparkline three inches tall, pushing the next tile off the screen. */
  .sparkwrap{flex:0 0 auto;height:3.2rem;min-height:0}
  .gauges{grid-template-columns:repeat(2,minmax(0,1fr))}
  #bar{position:sticky;bottom:0;z-index:5;background:var(--plane);
    margin:.1rem -.4rem -.4rem;border-top:1px solid var(--border);
    padding:.35rem .3rem calc(.35rem + env(safe-area-inset-bottom))}
  /* Taller than on a desktop and no wider: a fingertip needs the height, and
     the row has to get through ten controls on a screen this narrow. */
  #bar button,#bar .val{min-height:2.4rem;padding:.5rem .55rem;font-size:.8rem}
  /* The key column is written as "← → / PgUp PgDn" and the description next to
     it is a sentence: side by side they need more width than a phone has, so
     here the keys are allowed to wrap like anything else rather than holding
     the table open and pushing half of every line off the screen. */
  #help{padding:1rem}
  #help .card{min-width:0;width:100%;padding:1rem;overflow-x:hidden}
  #help table{width:100%}
  #help td{font-size:.8rem;padding:.2rem .3rem}
  /* The global td is nowrap — right for a tile's table of numbers, wrong for a
     column of sentences on a 390px screen. */
  #help td,#help td:first-child{white-space:normal;overflow:visible}
}
"""

JS = r"""
const THEMES = __THEMES__;
const LABELS = __LABELS__;
const PAGES = ["总览", "详情"];
const store = {
  get(k, d){ try { return localStorage.getItem("pcmon." + k) ?? d; }
             catch(e){ return d; } },
  set(k, v){ try { localStorage.setItem("pcmon." + k, v); } catch(e){} },
};

// The URL wins over what was remembered, so a shortcut can pin a handheld to
// one page or palette without touching the PC's settings.
const qs = new URLSearchParams(location.search);
let page = +(qs.get("page") ?? store.get("page", 0)) || 0;
let theme = qs.get("theme") || store.get("theme", __DEFAULT__);
let every = +store.get("every", __REFRESH__) || __REFRESH__;
let snap = null, live = false;

// --- which machine ---------------------------------------------------------
// The page can watch any PC Monitor on the LAN, not just the one serving it.
// The list comes from this PC — a browser cannot probe a port, so it cannot
// find the others itself — and every fetch is then prefixed with base(): "" for
// the machine we were loaded from, "http://ip:port" for anyone else. Which one
// is remembered by address rather than by index, because the list can reorder
// between sweeps and coming back to a rearranged list should not land you on a
// different machine than you left.
let hosts = [], hostAt = store.get("host", ""), scanning = false;
const hostIdx = () => Math.max(0, hosts.findIndex((h) => key(h) === hostAt));
const key = (h) => h.ip + ":" + h.port;
const current = () => hosts[hostIdx()] || null;
function base(){
  const h = current();
  return (!h || h.self) ? "" : "http://" + h.ip + ":" + h.port;
}

async function loadHosts(rescan){
  try {
    // Always from the machine that served the page: it is the one running the
    // sweep, and asking the machine being watched would move the list around
    // under you every time you switched.
    const res = await fetch("/hosts.json" + (rescan ? "?rescan=1" : ""),
                            {cache: "no-store"});
    const data = await res.json();
    hosts = data.hosts || [];
    scanning = !!data.scanning;
  } catch (e){
    hosts = [];
  }
  // A remembered machine that has gone away falls back to this one rather than
  // leaving the page fetching an address that no longer answers.
  if (hostAt && !hosts.some((h) => key(h) === hostAt)) setHost(0, true);
  paint();
}

function setHost(i, quiet){
  if (!hosts.length) return;
  const h = hosts[(i + hosts.length) % hosts.length];
  hostAt = key(h);
  store.set("host", hostAt);
  // The old machine's numbers must not sit on screen labelled as the new one's.
  snap = null;
  if (!quiet) tick();
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const clamp01 = (v) => v < 0 ? 0 : (v > 1 ? 1 : v);
const num = (v, d = 1) => (v === null || v === undefined || isNaN(v))
  ? "—" : (+v).toFixed(d);

function rate(bps){
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + " MB/s";
  if (bps >= 1024) return Math.round(bps / 1024) + " KB/s";
  return Math.round(bps || 0) + " B/s";
}
function bytes(n){
  const units = [["TB", 1099511627776], ["GB", 1073741824], ["MB", 1048576]];
  for (const [u, step] of units) if (n >= step) return (n / step).toFixed(1) + " " + u;
  return Math.round((n || 0) / 1024) + " KB";
}
function until(ts){
  if (!ts) return "";
  const rem = ts * 1000 - Date.now();
  if (rem <= 0) return "即将";
  if (rem >= 864e5) return Math.round(rem / 864e5) + "天后";
  const h = Math.floor(rem / 36e5), m = Math.floor(rem % 36e5 / 6e4);
  return h + ":" + String(m).padStart(2, "0") + "后";
}
function ago(ts){
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "刚刚";
  if (s < 3600) return Math.round(s / 60) + " 分钟前";
  return Math.round(s / 3600) + " 小时前";
}
// Status ink for a temperature, matching the renderer's thresholds exactly.
const tempCls = (t) => t == null ? "dim" : (t >= 90 ? "crit" : (t >= 80 ? "warn" : "good"));

// --- pieces ---------------------------------------------------------------
const meter = (frac, color) =>
  `<span class="meter" style="background:color-mix(in srgb,${color} 22%,var(--surface))">
     <i style="width:${(clamp01(frac) * 100).toFixed(1)}%;background:${color}"></i></span>`;

function spark(values, color, vmax){
  const v = (values || []).filter((x) => x !== null);
  if (v.length < 2) return "";
  const top = Math.max(vmax || Math.max(...v), 1e-9);
  const pts = v.map((y, i) =>
    (i * 100 / (v.length - 1)).toFixed(2) + "," +
    (30 - clamp01(y / top) * 29).toFixed(2)).join(" ");
  return `<div class="sparkwrap"><svg class="spark" viewBox="0 0 100 30"
    preserveAspectRatio="none"><polygon points="0,30 ${pts} 100,30" fill="${color}"
    opacity=".14"/><polyline points="${pts}" fill="none" stroke="${color}"
    stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"
    stroke-linecap="round"/></svg></div>`;
}

const tile = (area, title, meta, body) =>
  `<section class="tile" style="--area:${area}"><h2>${title}<em>${meta || ""}</em></h2>
   <div class="body">${body}</div></section>`;

function procTable(rows, color, unit, empty){
  if (!rows || !rows.length) return `<div class="sub">${empty}</div>`;
  const top = Math.max(...rows.map((r) => r[1] || 0), 1e-9);
  return "<table>" + rows.map(([name, value]) => `<tr>
    <td class="name">${esc(name)}</td>
    <td class="bar">${meter(value / top, color)}</td>
    <td class="num">${unit === "%" ? num(value, 1) + "%"
      : (value >= 1024 ? (value / 1024).toFixed(1) + " GB" : Math.round(value) + " MB")}</td>
    </tr>`).join("") + "</table>";
}

// --- page 1 ---------------------------------------------------------------
// A fixed number of columns, chosen so the rows come out as even as possible at
// no more than eight cells each: a cell is then the same size wherever it sits,
// and a short last row stays short instead of stretching to fill the width.
function coreCols(n){
  if (n <= 8) return n || 1;
  return Math.ceil(n / Math.ceil(n / 8));
}

function cpuTile(s){
  const c = s.cpu || {};
  const pcts = c.core_pct || [], mhz = c.core_mhz || [];
  // All clocks or none: one cell reading "#7" among fifteen clock readings is
  // the same false difference as a cell of another width.
  const clocks = mhz.length >= pcts.length;
  const cores = pcts.map((p, i) => `<div class="core"><b>${Math.round(p)}%</b>
      <em>${clocks ? (mhz[i] / 1000).toFixed(1) + "G" : "#" + (i + 1)}</em>
      <span class="bar"><i style="width:${clamp01(p / 100) * 100}%"></i></span></div>`
  ).join("");
  const cols = `grid-template-columns:repeat(${coreCols(pcts.length)},minmax(0,1fr))`;
  const temp = c.temp_c == null ? "" :
    `<span class="v ${tempCls(c.temp_c)}">${num(c.temp_c, 0)}°C</span>`;
  const power = c.power_w ? `<span class="k">${num(c.power_w, 0)} W</span>` : "";
  return tile("cpu", "CPU", esc(c.name || ""), `
    <div class="row">
      <div class="hero" style="color:var(--c-cpu)">${num(c.percent, 1)}<small>%</small></div>
      <div class="grow">
        <div class="row"><span class="v">${num(c.ghz, 2)} GHz</span>${temp}${power}
          <span class="k">${c.cores || 0} 线程</span></div>
        <div class="sub">峰值 ${num(c.peak, 0)}%</div>
      </div>
      <div class="right sub">${(s.mem || {}).percent ?
        "内存 " + num(s.mem.percent, 0) + "%" : ""}</div>
    </div>
    <div class="cores" style="${cols}">${cores}</div>
    ${spark(c.hist, "var(--c-cpu)", 100)}`);
}

function gpuTile(s){
  const g = s.gpu || {};
  if (!g.ok) return tile("gpu", "GPU", "", `<div class="sub">没有检测到 NVIDIA 显卡</div>`);
  const vram = g.mem_total_gb ? g.mem_used_gb / g.mem_total_gb : 0;
  return tile("gpu", "GPU", esc(g.name || ""), `
    <div class="row">
      <div class="hero" style="color:var(--c-gpu)">${num(g.percent, 0)}<small>%</small></div>
      <div class="grow">
        <div class="row"><span class="v ${tempCls(g.temp_c)}">${num(g.temp_c, 0)}°C</span>
          <span class="k">${num(g.power_w, 0)} W</span></div>
        <div class="sub">显存 ${num(g.mem_used_gb, 1)} / ${num(g.mem_total_gb, 0)} GB</div>
      </div>
    </div>
    <div class="row">${meter(vram, "var(--c-gpu)")}</div>
    ${spark(g.hist, "var(--c-gpu)", 100)}`);
}

function fpsTile(s){
  const f = s.fps || {};
  if (!f.rtss) return tile("fps", "帧率", "", `<div class="sub">${
    s.platform === "linux" ? "Linux 上没有 FPS 统计" : "RTSS 没有运行"}</div>`);
  if (f.value == null) return tile("fps", "帧率", "等待游戏",
    `<div class="sub">RTSS 在跑，前台没有 3D 程序</div>`);
  const cls = f.value >= 60 ? "good" : (f.value >= 30 ? "warn" : "crit");
  return tile("fps", "帧率", esc(f.process || ""), `
    <div class="row"><div class="hero ${cls}">${num(f.value, 0)}<small>FPS</small></div>
      <div class="grow sub">${num(f.frametime_ms, 1)} ms</div></div>
    ${spark(f.hist, "var(--c-fps)")}`);
}

function diskTile(s){
  const d = s.disk || {};
  if (!d.ok) return tile("disk", "磁盘", "", `<div class="sub">${esc(d.err || "读不到")}</div>`);
  const warn = d.temp_warn || 60, crit = d.temp_crit || 70;
  const tcls = d.temp_c == null ? "dim"
    : (d.temp_c >= crit ? "crit" : (d.temp_c >= warn ? "warn" : "good"));
  return tile("disk", (d.letter || "C") + " 盘",
    num(d.used_gb, 0) + " / " + num(d.total_gb, 0) + " GB", `
    <div class="row"><span class="v">${num(d.used_pct, 0)}%</span>
      ${meter((d.used_pct || 0) / 100, "var(--c-disk)")}
      ${d.temp_c == null ? "" : `<span class="v ${tcls}">${num(d.temp_c, 0)}°C</span>`}</div>
    <div class="row"><span class="k">读</span><span class="v">${rate(d.read_bps)}</span>
      <span class="k right">写</span><span class="v">${rate(d.write_bps)}</span></div>`);
}

// Down and up each keep their own scale, exactly as on the panel: a shared axis
// across two rates that differ by a factor of fifty flattens one of them.
function netRow(arrow, name, color, bps, day, hist, peak){
  return `<div class="netrow">
    <div class="row"><span style="color:${color}">${arrow}</span>
      <span class="v">${rate(bps)}</span>
      <span class="k">${name}</span>
      <span class="k right">今日 ${bytes(day)}</span></div>
    ${spark(hist, color, Math.max(peak || 0, 65536))}</div>`;
}

const memTopTile = (s) => tile("memtop", "内存前三", (s.mem || {}).total_gb
  ? `${num(s.mem.used_gb, 1)} / ${num(s.mem.total_gb, 0)} GB · ${num(s.mem.percent, 0)}%` : "",
  procTable(s.mem_top, "var(--c-mem)", "mb", "暂无"));
const cpuTopTile = (s) =>
  tile("cputop", "CPU 前三", "", procTable(s.top, "var(--c-cpu)", "%", "暂无"));
const gpuTopTile = (s) =>
  tile("gputop", "GPU 前三", "", procTable(s.gpu_top, "var(--c-gpu)", "%", "暂无"));

function netTile(s){
  const n = s.net || {};
  return tile("net", "网络", esc(n.nic || ""),
    netRow("↓", "下载", "var(--c-down)", n.down_bps, n.day_down, n.down_hist,
           n.down_peak) +
    netRow("↑", "上传", "var(--c-up)", n.up_bps, n.day_up, n.up_hist, n.up_peak));
}

function powerTile(s){
  const p = s.power || {};
  const cost = p.cost30 != null ? `30 天电费 ¥${num(p.cost30, 1)}` : "";
  const young = p.days && p.days < 30 ? `（只统计了 ${p.days} 天）` : "";
  return tile("pwr", "整机功耗", (p.estimated ? "估算" : "传感器") + young, `
    <div class="row">
      <span class="hero" style="font-size:1.6rem;color:var(--c-pwr)">${num(p.watts, 0)}<small>W</small></span>
      <span class="k">今日</span><span class="v">${num(p.d1, 2)} 度</span>
      <span class="k">7 天</span><span class="v">${num(p.d7, 1)} 度</span>
      <span class="k">30 天</span><span class="v">${num(p.d30, 1)} 度</span>
      <span class="sub right">${cost}</span>
    </div>`);
}

// Percent gauge with the renderer's own ink rule: only the number turns.
function quotaCell(label, pct, resets){
  if (pct == null) return {label, text: "—", frac: null, cls: "dim", resets};
  const cls = pct >= 100 ? "crit" : (pct >= 85 ? "warn" : "");
  return {label, text: Math.round(pct) + "%", frac: pct / 100, cls, resets};
}

function money(v, cur){
  const sym = {USD: "$", CNY: "¥", RMB: "¥"}[(cur || "").toUpperCase()] || "";
  return sym + (Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(2));
}

function aiCells(ai){
  const c = ai.claude || {}, ds = ai.deepseek || {}, mm = ai.minimax || {};
  const w = (k) => (c[k] || {});
  const cells = [
    quotaCell("C 5h", w("five_hour").pct, w("five_hour").resets_at),
    quotaCell("C 7d", w("seven_day").pct, w("seven_day").resets_at),
  ];
  cells.push(!ds.ok || ds.balance == null
    ? {label: "DS", text: "—", frac: null, cls: "dim"}
    : {label: "DS", text: money(ds.balance, ds.currency), frac: 1,
       cls: ds.available ? "" : "crit"});
  cells.push(quotaCell("M 5h", mm.five_hour, mm.five_hour_reset));
  cells.push(quotaCell("M 周", mm.weekly, mm.weekly_reset));
  return cells;
}

function aiTile(s){
  const ai = s.ai || {}, c = ai.claude;
  const gauges = aiCells(ai).map((g) => `<div class="gauge">
    <div class="top">${g.label}<b class="${g.cls}">${g.text}</b></div>
    ${meter(g.frac == null ? 0 : g.frac, "var(--c-ai)")}
    <div class="rst">${until(g.resets)}</div></div>`).join("");

  let note = "读取中…";
  if (c && !c.five_hour){
    note = ({"no-creds": "未登录 Claude Code", "no-token": "凭据里没有令牌",
             "rate-limited": "接口限流，稍后重试",
             "offline": "连不上 Anthropic"})[c.err] || ("Claude 出错：" + esc(c.err));
  } else if (c){
    note = "Claude " + esc((c.plan || "").replace(/^./, (m) => m.toUpperCase()));
    if (!c.ok) note += " · <span class='warn'>数据可能过期</span>";
  }
  return tile("ai", "AI 额度", "", `<div class="gauges">${gauges}</div>
    <div class="notes">${note}</div>`);
}

// --- page 2 ---------------------------------------------------------------
const DSTATE = {running: "var(--s-good)", paused: "var(--s-warn)",
                restarting: "var(--s-warn)", exited: "var(--s-crit)",
                dead: "var(--s-crit)", created: "var(--muted)"};

function dockerTile(s){
  const d = s.docker || {};
  if (!d.ok) return tile("docker", "Docker", "",
    `<div class="sub">${esc(d.err || "未安装")}</div>`);
  const rows = (d.containers || []).map((c) => `<tr>
    <td class="name"><span class="dot" style="background:${DSTATE[c.state] || "var(--muted)"}"></span>${esc(c.name)}</td>
    <td class="num dim" style="font-weight:400">${esc(c.status || "")}</td>
    <td class="num">${c.cpu == null ? "—" : num(c.cpu, 1) + "%"}</td>
    <td class="num">${c.mem_mb == null ? "—"
      : (c.mem_mb >= 1024 ? (c.mem_mb / 1024).toFixed(1) + " GB"
                          : Math.round(c.mem_mb) + " MB")}</td></tr>`).join("");
  return tile("docker", "Docker 容器",
    `${d.running || 0} / ${d.total || 0} 运行中`,
    `<div class="scroll loop" data-loop="docker"><table>${rows}</table></div>`);
}

function adviceTile(s){
  const a = s.advice || {};
  if (!a.enabled) return tile("advice", "AI 建议", "",
    `<div class="sub">没有开启。设置页里可以打开。</div>`);
  if (!a.ok) return tile("advice", "AI 建议", "",
    `<div class="sub">${esc(a.err || "还没跑过")}</div>`);
  const cls = a.level === "warn" ? "warn" : "";
  return tile("advice", "AI 建议",
    esc(a.provider || "") + " · " + ago(a.at),
    `<div class="para ${cls}">${esc(a.text || "")}</div>`);
}

// The gauges up on page 1 are a glance; this tile answers the question you stop
// to ask — how much of each window is left and when it comes back — for the two
// providers whose quota actually runs out. The bar fills with what has been
// used (the same direction as every other bar on the page, so a full bar always
// means trouble) while the number on top is what is left, which is the part
// worth deciding on.
function quotaCard(provider, window_, pct, resets){
  const left = pct == null ? null : Math.max(0, 100 - pct);
  const cls = left == null ? "dim" : (left <= 0 ? "crit" : (left <= 15 ? "warn" : ""));
  const rst = until(resets);
  return `<div class="qcard">
    <div class="head"><span><b>${esc(provider)}</b> ${esc(window_)}</span>
      <span class="left ${cls}">${left == null ? "—" : Math.round(left) + "%"}</span></div>
    ${meter(pct == null ? 0 : pct / 100, "var(--c-ai)")}
    <div class="foot"><span>${pct == null ? "" : "已用 " + Math.round(pct) + "%"}</span>
      <span>${rst ? rst + "重置" : ""}</span></div></div>`;
}

function aiDetailTile(s){
  const ai = s.ai || {}, c = ai.claude || {}, mm = ai.minimax || {};
  const w = (k) => (c[k] || {});
  return tile("aidet", "AI 额度明细", "大字为剩余", `<div class="qcards">
    ${quotaCard("Claude", "5 小时", w("five_hour").pct, w("five_hour").resets_at)}
    ${quotaCard("Claude", "7 天", w("seven_day").pct, w("seven_day").resets_at)}
    ${quotaCard("MiniMax", "5 小时", mm.five_hour, mm.five_hour_reset)}
    ${quotaCard("MiniMax", "7 天", mm.weekly, mm.weekly_reset)}</div>`);
}

function wxTile(s){
  const w = s.weather || {};
  if (!w.ok) return tile("wx", "天气", "",
    `<div class="sub">${esc(w.err || "读取中…")}</div>`);
  const cell = (k, label, v, day) => !v ? "" : `<div class="wxcell">
    <span class="k">${label}</span><b>${esc(v.text || "")}</b>
    <span class="k">${day ? num(v.tmin, 0) + "~" + num(v.tmax, 0) + "°"
                          : num(v.temp, 0) + "°"}</span></div>`;
  return tile("wx", "天气", esc(w.city || ""), `<div class="wxrow">
    ${cell("now", "现在", w.now)}${cell("h3", "3 小时", w.h3)}
    ${cell("h6", "6 小时", w.h6)}${cell("d1", "明天", w.d1, true)}
    ${cell("d2", "后天", w.d2, true)}</div>`);
}

// --- lists that show themselves -------------------------------------------
// The container list is a list of unknown length — however many containers this
// machine runs — so on a handheld the tile is regularly too short for all of it.
// Nobody is going to scroll it: the page is watched from across a desk and the
// device it was written for has no pointer at all. So a tile that overflows
// walks itself down to the last row, waits, and walks back, forever.
//
// It cannot be a CSS animation. The whole grid is rebuilt from innerHTML on
// every tick, which would restart an animation once a second and leave these
// the tile frozen at its first frame — so the offset lives out here, keyed by
// tile rather than by element, and is put back onto the fresh node after each
// repaint. The same indirection is what lets a list remember where it was while
// a narrow screen is on the other page.
const LOOP_PX_S = 16;     // slow enough to read a row as it goes past
const LOOP_HOLD = 2000;   // ms held at each end, so the ends can be read too
const LOOP_TOUCH = 5000;  // ms a list stays put after a finger has moved it
const loops = {};

function loopState(el){
  const k = el.dataset.loop;
  return loops[k] || (loops[k] = {at: 0, dir: 1, hold: LOOP_HOLD, pause: 0});
}

// The touch equivalent of the hover rule below: a finger that flicks a list
// somewhere meant it to stay there, so the walk stops, adopts wherever the
// finger left it, and picks up again a few seconds later. Delegated, because
// these nodes are replaced once a second.
document.addEventListener("touchstart", (ev) => {
  const el = ev.target.closest("[data-loop]");
  if (el) loopState(el).pause = LOOP_TOUCH;
}, {passive: true});

// Applied right after a repaint: the new node starts at zero, which would make
// the list jump back to the top every tick.
function applyLoops(){
  document.querySelectorAll("[data-loop]").forEach((el) => {
    const st = loopState(el), max = el.scrollHeight - el.clientHeight;
    // A list that now fits — containers stopped, a window closed — is not left
    // parked halfway down its own tile.
    if (max <= 1){ st.at = 0; st.dir = 1; st.hold = LOOP_HOLD; }
    else if (st.at > max){ st.at = max; st.dir = -1; st.hold = LOOP_HOLD; }
    el.scrollTop = st.at;
  });
}

let lastFrame = 0;
function stepLoops(ts){
  // Clamped because a backgrounded tab stops getting frames: without this the
  // first frame after it comes back would carry a minute of travel at once.
  const dt = lastFrame ? Math.min(ts - lastFrame, 100) : 0;
  lastFrame = ts;
  document.querySelectorAll("[data-loop]").forEach((el) => {
    const st = loopState(el), max = el.scrollHeight - el.clientHeight;
    // Hovering hands it back: on a machine that does have a mouse, reading one
    // row should not mean chasing it.
    if (max <= 1 || el.matches(":hover")) return;
    if (st.pause > 0){
      // Wherever the finger left it is where it carries on from, and the walk
      // heads back up rather than dragging the reader off the row they stopped
      // on the moment the pause runs out.
      st.pause -= dt;
      st.at = Math.min(el.scrollTop, max);
      st.hold = LOOP_HOLD;
      return;
    }
    if (st.hold > 0){ st.hold -= dt; }
    else {
      st.at += st.dir * LOOP_PX_S * dt / 1000;
      if (st.at >= max){ st.at = max; st.dir = -1; st.hold = LOOP_HOLD; }
      else if (st.at <= 0){ st.at = 0; st.dir = 1; st.hold = LOOP_HOLD; }
    }
    el.scrollTop = st.at;
  });
  requestAnimationFrame(stepLoops);
}

// --- shell ----------------------------------------------------------------
// A window with room for four columns gets every tile at once and no paging;
// anything narrower or taller than it is wide keeps the two pages, because a
// portrait handheld cannot hold this many tiles legibly.
// The width is 850 rather than the ~1050 four columns want at rest because zoom
// is measured in the same CSS pixels: 150% zoom on a 1280 panel reports 853, and
// someone who zooms in is asking for bigger text, not for a different dashboard.
// Asked again on every paint rather than cached from a change event: zoom moves
// the viewport across this line without reliably firing one, and a page that
// redraws once a second can afford to just look.
const wide = matchMedia("(min-width:850px) and (min-aspect-ratio:5/4)");

function paint(){
  const s = snap;
  const single = wide.matches;
  // Buttons rather than labels: the page indicator is the most obvious thing on
  // screen to tap to change pages, so on a touchscreen it had better work.
  $("pages").innerHTML = single ? "" : PAGES.map((n, i) =>
    `<button class="pill ${i === page ? "on" : ""}" data-act="page${i}">${n}</button>`
  ).join("");
  $("rate").textContent = (every / 1000).toFixed(1) + "s · " + LABELS[theme];
  const bar = $("bar");
  if (bar){
    bar.classList.toggle("single", single);
    $("brate").textContent = (every / 1000).toFixed(1) + "s";
    $("btheme").textContent = LABELS[theme];
    $("bfull").textContent = document.fullscreenElement ? "退出全屏" : "全屏";
  }
  // The machine's own name comes from its snapshot, but the list position has
  // to show even before the first reply arrives — switching to a PC that is not
  // answering should say which PC that is.
  const h = current();
  $("host").textContent = (s ? (s.host || "") : (h ? h.name : ""))
    + (hosts.length > 1 ? `  ${hostIdx() + 1}/${hosts.length}` : "")
    + (h && !h.self ? "  远程" : "");
  if (!s) return;

  $("clock").textContent = s.time || "";
  const now = (s.weather || {}).now;
  $("wx").textContent = (s.weather || {}).ok && now
    ? `${now.text || ""} ${Math.round(now.temp)}°` : "";

  const main = $("main");
  main.className = single ? "all" : "p" + page;
  main.innerHTML = single
    ? cpuTile(s) + gpuTile(s) + fpsTile(s) + cpuTopTile(s) + gpuTopTile(s) +
      memTopTile(s) + diskTile(s) + netTile(s) + dockerTile(s) +
      aiDetailTile(s) + adviceTile(s) + wxTile(s) + powerTile(s)
    : page === 0
    ? cpuTile(s) + fpsTile(s) + gpuTile(s) + memTopTile(s) + cpuTopTile(s) +
      gpuTopTile(s) + diskTile(s) + netTile(s) + aiTile(s) + powerTile(s)
    : dockerTile(s) + adviceTile(s) + aiDetailTile(s) + wxTile(s);
  applyLoops();
}

async function tick(){
  const want = hostAt;
  try {
    const res = await fetch(base() + "/stats.json", {cache: "no-store"});
    const data = await res.json();
    // A slow reply from the machine we just switched away from would otherwise
    // overwrite the new one's numbers.
    if (want !== hostAt) return;
    snap = data;
    live = true;
  } catch (e){
    live = false;
  }
  $("link").textContent = live ? ""
    : (current() && !current().self ? "连不上这台" : "连接中断");
  $("link").className = live ? "" : "bad";
  paint();
}

let timer = null;
function schedule(){
  if (timer) clearInterval(timer);
  timer = setInterval(tick, every);
}

// The same range the settings page offers, so what [ and ] can reach and what
// the PC can be set to are one range rather than two that disagree.
function setEvery(ms){
  every = Math.max(250, Math.min(10000, ms));
  store.set("every", every);
  schedule();
  paint();
}

function setTheme(name){
  theme = THEMES.includes(name) ? name : __DEFAULT__;
  document.documentElement.dataset.theme = name;
  store.set("theme", name);
  paint();
}

function setPage(i){
  page = (i + PAGES.length) % PAGES.length;
  store.set("page", page);
  paint();
}

// --- one action per key ----------------------------------------------------
// The keys, the buttons in the bar and a swipe across the tiles all land here,
// so there is one list of the things this page can do rather than three that
// have to be kept saying the same thing.
function act(a){
  if (a === "next") setPage(page + 1);
  else if (a === "prev") setPage(page - 1);
  else if (a.startsWith("page")) setPage(+a.slice(4));
  else if (a === "theme")
    setTheme(THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]);
  else if (a === "full"){
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => {});
  }
  else if (a === "slower") setEvery(every + 500);
  else if (a === "faster") setEvery(every - 500);
  else if (a === "hostnext") setHost(hostIdx() + 1);
  else if (a === "hostprev") setHost(hostIdx() - 1);
  else if (a === "hostself") setHost(0);
  else if (a === "rescan"){ scanning = true; paint(); loadHosts(true); }
  else if (a === "help") $("help").classList.toggle("show");
  else if (a === "closehelp") $("help").classList.remove("show");
  else return false;
  return true;
}

const KEYS = {ArrowRight: "next", PageDown: "next", " ": "next",
              ArrowDown: "next", ArrowLeft: "prev", PageUp: "prev",
              ArrowUp: "prev", t: "theme", f: "full", "[": "slower",
              "]": "faster", n: "hostnext", p: "hostprev", "0": "hostself",
              r: "rescan", h: "help", "?": "help", Escape: "closehelp"};

document.addEventListener("keydown", (ev) => {
  const k = ev.key;
  const a = KEYS[k.length === 1 ? k.toLowerCase() : k]
    || (k >= "1" && k <= String(PAGES.length) ? "page" + (+k - 1) : null);
  if (a && act(a)) ev.preventDefault();
});

// Every control on the page is a [data-act], wherever it sits and whether or
// not it survived the last repaint — the grid is rebuilt from innerHTML once a
// second, so nothing on it can hold a listener of its own.
document.addEventListener("click", (ev) => {
  const el = ev.target.closest("[data-act]");
  if (el) act(el.dataset.act);
  // Tapping the dark around the key table puts it away, which is what a phone
  // user will try before looking for a close button.
  else if (ev.target.id === "help") act("closehelp");
});

// Swipe to turn the page, on the tiles rather than the whole document so that
// the bar and the header stay dead to it. Only sideways, only far enough to be
// deliberate, and never while the finger is mostly going up or down — a phone
// scrolls this page vertically, and a scroll must not also turn the page.
let touch = null;
$("main").addEventListener("touchstart", (ev) => {
  touch = ev.touches.length === 1
    ? {x: ev.touches[0].clientX, y: ev.touches[0].clientY} : null;
}, {passive: true});
$("main").addEventListener("touchend", (ev) => {
  if (!touch || wide.matches) return;
  const t = ev.changedTouches[0];
  const dx = t.clientX - touch.x, dy = t.clientY - touch.y;
  touch = null;
  if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5)
    act(dx < 0 ? "next" : "prev");
}, {passive: true});

// The button says 全屏 or 退出全屏, and the browser is the one that knows which:
// F11 and the browser's own chrome change the mode without going through act().
document.addEventListener("fullscreenchange", paint);

// Zoom and window resizes land at once instead of waiting for the next poll,
// coalesced through rAF because dragging an edge fires the event continuously.
let queued = 0;
addEventListener("resize", () => {
  if (queued) return;
  queued = requestAnimationFrame(() => { queued = 0; paint(); });
});

setTheme(theme);
setPage(page);
tick();
schedule();
requestAnimationFrame(stepLoops);
// The host list is asked for on its own slow timer rather than with the numbers:
// it changes when a machine is switched on, not every second, and the sweep
// behind it is a subnet scan.
if (__SCAN__){
  loadHosts(false);
  setInterval(() => loadHosts(false), 30000);
}
"""


def _bar(scan: bool) -> str:
    """The touch equivalent of the key map: one control per key, in one row.

    Grouped the way the keys are — the two page keys, the two refresh keys, the
    machine keys — so that a row of ten buttons reads as four things rather than
    ten. The refresh group carries the interval and the theme button carries the
    palette's name, filled in on every repaint, because a control that changes
    something invisible should say what it changed it to.
    """
    groups = ['<span class="grp paged">'
              '<button data-act="prev" title="上一页">‹</button>'
              '<button data-act="next" title="下一页">›</button></span>',
              '<button data-act="theme" id="btheme" title="切换主题">主题</button>',
              '<span class="grp"><button data-act="slower" title="刷新变慢">−</button>'
              '<span class="val" id="brate"></span>'
              '<button data-act="faster" title="刷新变快">+</button></span>']
    if scan:
        groups.append('<span class="grp">'
                      '<button data-act="hostprev" title="上一台主机">‹ 主机</button>'
                      '<button data-act="hostnext" title="下一台主机">›</button>'
                      '<button data-act="hostself" title="回到这台机器">本机</button>'
                      '<button data-act="rescan" title="重扫局域网">重扫</button></span>')
    groups.append('<button data-act="full" id="bfull">全屏</button>')
    groups.append('<button data-act="help" title="按键表">?</button>')
    return '<nav id="bar">' + "".join(groups) + "</nav>"


def page(cfg: dict | None = None) -> bytes:
    """The whole page as one document — no second request, no external assets."""
    cfg = cfg or {}
    default = theme.resolve(cfg.get("theme"))
    # The refresh interval and whether the host switcher exists at all are the
    # PC's settings, baked in here: the page has no second request to ask them
    # in, and a remembered interval from an earlier visit still wins, because
    # someone who pressed [ on this handheld meant it for this handheld.
    refresh = int(cfg.get("web_refresh_ms") or REFRESH_MS)
    scan = bool(cfg.get("web_scan", True))
    buttons = bool(cfg.get("web_buttons", True))
    script = (JS.replace("__THEMES__", json.dumps(list(theme.NAMES)))
                .replace("__LABELS__", json.dumps(theme.LABELS, ensure_ascii=False))
                .replace("__DEFAULT__", json.dumps(default))
                .replace("__SCAN__", "true" if scan else "false")
                .replace("__REFRESH__", str(refresh)))
    keys = [("← → / PgUp PgDn", "翻页（窄屏才分总览 / 详情两页）"),
            ("1 2", "直接跳到某一页"),
            ("T", "切换主题（" + " / ".join(
                theme.LABELS.get(n, n) for n in theme.NAMES) + "）"),
            ("F", "全屏"),
            ("[ ]", "刷新变慢 / 变快（0.25 ~ 10 秒，默认在设置页改）"),
            ("H", "显示 / 关闭这张表")]
    if scan:
        keys[5:5] = [("N / P", "看局域网里的下一台 / 上一台主机"),
                     ("0", "回到这台机器"),
                     ("R", "立刻重扫局域网")]
    if buttons:
        keys.append(("触屏", "底下那排按钮做的是同样的事；左右滑动也能翻页"))
    rows = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in keys)
    hint_keys = "← → 翻页 · T 主题" + (" · N 换主机" if scan else "") \
        + " · [ ] 刷新 · F 全屏 · H 按键表"
    hints = (f'<span id="hints">{hint_keys}</span>'
             if cfg.get("web_hints", True) else "")
    bar = _bar(scan) if buttons else ""
    return f"""<!doctype html><html lang="zh" data-theme="{default}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PC Monitor</title>
<style>{_theme_css()}{CSS}</style>
<div id="app">
  <header id="top">
    <span id="host"></span>
    <span id="pages"></span>
    <span class="grow"></span>
    {hints}
    <span id="link"></span>
    <span id="rate"></span>
    <span id="wx"></span>
    <span id="clock"></span>
  </header>
  <main id="main" class="p0"></main>
  {bar}
</div>
<div id="help"><div class="card"><h3>按键 / 按钮</h3><table>{rows}</table>
  <button class="close" data-act="closehelp">知道了</button></div></div>
<script>{script}</script>
</html>""".encode("utf-8")


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "hd.html"
    with open(out, "wb") as fh:
        fh.write(page({}))
    print("wrote", out)
