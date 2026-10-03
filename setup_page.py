#!/usr/bin/env python3
"""
setup_page.py — the darkroom console HTML for /setup.

Pure presentation. render(initial_json) returns a self-contained page that
paints the injected initial state immediately (no blank flash) and then polls
GET /api/setup/status every ~4s, re-rendering the stage list in place. The
safelight warm-up animation plays only on first paint; prefers-reduced-motion
is honoured; keyboard focus stays visible.

Palette / type are Loupe's existing darkroom tokens (Fraunces / Newsreader /
JetBrains Mono), matching server.py and the marketing site verbatim.
"""

_TEMPLATE = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Loupe — the darkroom</title>
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Newsreader:ital,wght@0,400;0,500;1,400&family=JetBrains+Mono:wght@400;500&display=swap" rel=stylesheet>
<style>
:root{--bg:#15110d;--bg2:#1d1812;--panel:#241d15;--tile:#231d16;--line:#3a2e22;
 --ink:#ece2d3;--mut:#9c8b76;--faint:#766c5a;
 --amber:#BA7517;--lit:#E2902A;--green:#46b06e;--keep:#46b06e;--cut:#bf463b;
 --hd:'Fraunces',Georgia,serif;--bd:'Newsreader',Georgia,serif;--mo:'JetBrains Mono',ui-monospace,monospace}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--bd);
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
body{background-image:radial-gradient(150% 80% at 50% -12%,#1f190f 0%,var(--bg) 58%);min-height:100vh}
a{color:var(--amber);text-decoration:none}
a:hover{color:var(--lit)}
:focus-visible{outline:2px solid var(--lit);outline-offset:2px;border-radius:6px}
.wrap{max-width:880px;margin:0 auto;padding:0 22px}

/* ---------- header / safelight ---------- */
header{border-bottom:1px solid var(--line);background:linear-gradient(#1d1812,#15110d);
 position:sticky;top:0;z-index:20}
.hbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 0 14px}
.brand{display:flex;align-items:center;gap:13px;min-width:0}
.brand img{height:46px;width:auto;display:block}
.kick{font-family:var(--mo);font-size:11px;letter-spacing:.22em;text-transform:uppercase;
 color:var(--amber);display:inline-flex;align-items:center;gap:9px}
.safelight{width:9px;height:9px;border-radius:50%;background:var(--amber);flex:none;
 box-shadow:0 0 10px 2px rgba(186,117,23,.55)}
.ovr{font-family:var(--mo);font-size:11.5px;color:var(--mut);text-align:right;white-space:nowrap}
.ovr b{color:var(--ink);font-weight:500}
.lede{padding:6px 0 20px;max-width:60ch}
.lede h1{font-family:var(--hd);font-weight:500;font-size:clamp(26px,4.4vw,38px);
 letter-spacing:-.01em;margin:0 0 8px;line-height:1.05}
.lede h1 em{font-style:italic;color:var(--lit)}
.lede p{color:var(--mut);font-size:16px;margin:0;line-height:1.5}

/* ready banner */
.ready{display:none;align-items:center;gap:12px;margin:0 0 18px;padding:13px 16px;border-radius:11px;
 border:1px solid #2f5a3a;background:linear-gradient(#16301f,#13261a)}
.ready.show{display:flex}
.ready .dot{width:9px;height:9px;border-radius:50%;background:var(--green);flex:none;
 box-shadow:0 0 10px 2px rgba(70,176,110,.5)}
.ready .t{font-family:var(--hd);font-size:17px;color:#bdebcd}
.ready .s{font-family:var(--mo);font-size:12px;color:#84b394;margin-left:auto}
.ready a.enter{font-family:var(--mo);font-size:12px;color:#0f2616;background:var(--green);
 border:1px solid var(--green);border-radius:8px;padding:8px 14px;font-weight:600;white-space:nowrap}
.ready a.enter:hover{background:#5cc685}

/* ---------- phases ---------- */
.phase{margin:26px 0 0}
.phead{display:flex;align-items:baseline;gap:11px;margin:0 0 13px;
 border-bottom:1px solid var(--line);padding-bottom:9px}
.phead .pn{font-family:var(--mo);font-size:12px;color:var(--faint);letter-spacing:.16em}
.phead .pname{font-family:var(--hd);font-size:21px;color:var(--ink)}
.phead .pname.active{color:var(--lit)}
.phead .pmeta{margin-left:auto;font-family:var(--mo);font-size:11px;color:var(--faint)}

/* ---------- stage card ---------- */
.stage{display:grid;grid-template-columns:auto 1fr auto;gap:4px 14px;align-items:start;
 background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:15px 17px;margin:0 0 11px;
 transition:border-color .2s,opacity .2s}
.stage .lamp{grid-row:1/4;width:13px;height:13px;border-radius:50%;margin-top:5px;flex:none;
 background:var(--faint);box-shadow:none;transition:background .25s,box-shadow .25s}
.stage .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.stage .nm{font-family:var(--hd);font-size:18px;color:var(--ink)}
.stage .loc{font-family:var(--mo);font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;
 color:var(--mut);border:1px solid var(--line);border-radius:11px;padding:2px 8px;background:var(--tile)}
.stage .opt{font-family:var(--mo);font-size:9.5px;letter-spacing:.06em;color:var(--faint);
 border:1px dashed var(--line);border-radius:11px;padding:2px 8px}
.stage .pill{grid-row:1;justify-self:end;font-family:var(--mo);font-size:10.5px;letter-spacing:.05em;
 padding:3px 10px;border-radius:11px;white-space:nowrap;border:1px solid var(--line);color:var(--mut)}
.stage .detail{grid-column:2/4;font-family:var(--bd);font-size:14.5px;color:var(--mut);margin-top:5px;line-height:1.45}
.stage .pbar{grid-column:2/4;height:6px;background:#0c0a07;border:1px solid var(--line);border-radius:4px;
 overflow:hidden;margin-top:10px}
.stage .pbar>i{display:block;height:100%;width:0;background:var(--amber);border-radius:3px;
 transition:width .5s ease}
.stage .nums{grid-column:2/4;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;
 font-family:var(--mo);font-size:11px;color:var(--faint);margin-top:6px}
.stage .nums b{color:var(--mut);font-weight:500}
.stage .log{grid-column:2/4;font-family:var(--mo);font-size:10.5px;color:var(--faint);margin-top:7px;
 display:flex;gap:7px;align-items:center}
.stage .log::before{content:'';width:5px;height:5px;border-radius:50%;background:var(--faint);flex:none}

/* state treatments — amber=working, red=needs you, dim=in the dark, green=done */
.stage.s-done{border-color:#2f5a3a}
.stage.s-done .lamp{background:var(--green);box-shadow:0 0 9px 1px rgba(70,176,110,.45)}
.stage.s-done .pill{color:#9be8b6;border-color:#2f5a3a;background:#15301f}
.stage.s-done .pbar>i{background:var(--green)}
.stage.s-running{border-color:#5a3f1e}
.stage.s-running .lamp{background:var(--amber);box-shadow:0 0 11px 2px rgba(186,117,23,.6)}
.stage.s-running .pill{color:#f0d09a;border-color:#5a3f1e;background:#2a2015}
.stage.s-running .pbar>i{background:var(--amber)}
.stage.s-needs_you{border-color:#5a2b27}
.stage.s-needs_you .lamp{background:var(--cut);box-shadow:0 0 11px 2px rgba(191,70,59,.55)}
.stage.s-needs_you .pill{color:#ff9a8f;border-color:#5a2b27;background:#3a1d1a}
.stage.s-needs_you .nm{color:var(--ink)}
.stage.s-error{border-color:#5a2b27}
.stage.s-error .lamp{background:var(--cut);box-shadow:0 0 11px 2px rgba(191,70,59,.6)}
.stage.s-error .pill{color:#ff9a8f;border-color:#5a2b27;background:#3a1d1a}
.stage.s-queued .lamp{background:#7a5a2a}
.stage.s-queued .pill{color:var(--mut)}
.stage.s-blocked,.stage.s-unknown{opacity:.62}            /* "in the dark" */
.stage.s-blocked .lamp,.stage.s-unknown .lamp{background:#2c241a}
.stage.s-blocked .detail,.stage.s-unknown .detail{color:var(--faint)}

/* ---------- connect (inert this pass) ---------- */
.connect{background:var(--bg2);border:1px solid var(--line);border-radius:12px;padding:18px 18px 16px;margin:0 0 11px}
.connect .note{font-family:var(--mo);font-size:11px;color:var(--faint);margin:0 0 14px;line-height:1.5;
 display:flex;gap:8px}
.connect .note::before{content:'⌁';color:var(--amber)}
.fgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px}
.field{display:flex;flex-direction:column;gap:5px}
.field.full{grid-column:1/3}
.field label{font-family:var(--mo);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut)}
.field input,.field .picker{font-family:var(--mo);font-size:13px;background:#100d09;color:var(--faint);
 border:1px solid var(--line);border-radius:8px;padding:10px 12px;width:100%}
.field input::placeholder{color:#5a513f}
.field input:disabled,.field .picker{cursor:not-allowed;opacity:.85}
.field .picker{display:flex;align-items:center;justify-content:space-between;gap:8px}
.field .picker span.b{color:var(--faint);font-size:11px;border:1px solid var(--line);border-radius:6px;padding:3px 8px}
.connect .cbtns{display:flex;gap:10px;margin-top:15px;align-items:center}
.connect .cbtns button{font-family:var(--mo);font-size:12.5px;background:#221b13;color:var(--faint);
 border:1px solid var(--line);border-radius:8px;padding:10px 16px;cursor:not-allowed}
.connect .cbtns button.primary{background:#2a2015;color:#caa56a;border-color:#5a3f1e;font-weight:600}
.connect .cbtns .soon{font-family:var(--mo);font-size:10.5px;color:var(--faint);letter-spacing:.06em}
/* source choice (active) + skip path */
.srcchoice{display:flex;gap:10px;margin:0 0 16px;flex-wrap:wrap}
.srcopt{font-family:var(--mo);font-size:12.5px;color:var(--mut);background:#1a140d;
 border:1px solid var(--line);border-radius:9px;padding:11px 16px;cursor:pointer;transition:border-color .18s,color .18s,background .18s}
.srcopt:hover{color:var(--ink);border-color:#5a3f1e}
.srcopt[aria-pressed=true]{color:#f0d09a;background:#2a2015;border-color:#5a3f1e;font-weight:600}
.srcpane[hidden]{display:none}
/* the existing-library path uses a REAL editable input (not the inert picker) */
.connect input#libpath{color:var(--ink);cursor:text;opacity:1}
.connect .cbtns button#libsave{cursor:pointer;color:#caa56a}
.connect .cbtns button#libsave:hover{background:#34281a}
.libmsg,.connmsg{font-family:var(--mo);font-size:11px;letter-spacing:.04em}
.libmsg.err,.connmsg.err{color:#ff9a8f}
.libmsg.ok,.connmsg.ok{color:#9be8b6}
.connmsg.work{color:var(--lit)}
/* pre-flight checklist */
.pfhead{font-family:var(--hd);font-size:17px;color:var(--ink);margin:0 0 10px}
.pflist{margin:0 0 14px;padding-left:22px;color:var(--mut);font-size:14px;line-height:1.5}
.pflist li{margin:0 0 11px}
.pflist b{color:var(--ink);font-weight:500}
.pflist em{font-style:italic;color:var(--lit)}
.pfnote{margin-top:5px;font-family:var(--mo);font-size:10.5px;color:var(--faint);line-height:1.45}
.mono{font-family:var(--mo);font-size:.9em;color:var(--ink);background:#1a140d;
 border:1px solid var(--line);border-radius:5px;padding:1px 6px}
/* the 421 remediation callout */
.fix421{display:none;margin:12px 0 0;padding:13px 15px;border-radius:10px;
 border:1px solid #5a2b27;background:#2a1815;font-size:13.5px;color:#f0cfc8;line-height:1.5}
.fix421.show{display:block}
.fix421 b{color:#ffd9d2}
.fix421 .h{font-family:var(--mo);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
 color:#ff9a8f;margin:0 0 7px;display:block}
.note.ok{color:#9be8b6;border-color:#2f5a3a}
.connect input#cAppleId,.connect input#cPassword,.connect input#cCode{color:var(--ink);cursor:text;opacity:1}
.connect .cbtns button#pfContinue,.connect .cbtns button#cSignin,.connect .cbtns button#cVerify{cursor:pointer;color:#caa56a}
@media (max-width:560px){.fgrid{grid-template-columns:1fr}.field.full{grid-column:1}}
/* indeterminate amber activity bar — shown only during enrichment-import work states */
.actbar{height:3px;margin:11px 0 0;border-radius:3px;border:1px solid var(--line);
 background:linear-gradient(90deg,transparent,var(--amber),transparent) -40% 0/40% 100% no-repeat,#0c0a07;
 animation:actslide 1.15s linear infinite}
@keyframes actslide{to{background-position:140% 0}}

footer{color:var(--faint);font-family:var(--mo);font-size:11px;text-align:center;
 padding:30px 0 46px;line-height:1.7}
.pulsewrap{display:inline-block}

/* ---------- animation (first paint only; reduced-motion safe) ---------- */
@media (prefers-reduced-motion:no-preference){
  @keyframes warmup{0%{opacity:0;transform:translateY(6px);filter:brightness(.35) saturate(.5)}
                    100%{opacity:1;transform:none;filter:none}}
  @keyframes safeglow{0%{box-shadow:0 0 0 0 rgba(186,117,23,0)}
                      45%{box-shadow:0 0 16px 5px rgba(186,117,23,.75)}
                      100%{box-shadow:0 0 10px 2px rgba(186,117,23,.55)}}
  @keyframes lamppulse{0%,100%{box-shadow:0 0 8px 1px rgba(186,117,23,.45)}
                       50%{box-shadow:0 0 14px 3px rgba(186,117,23,.85)}}
  body.firstpaint .warm{animation:warmup .9s ease-out both}
  body.firstpaint .warm.d1{animation-delay:.06s}
  body.firstpaint .warm.d2{animation-delay:.12s}
  body.firstpaint .warm.d3{animation-delay:.18s}
  body.firstpaint .warm.d4{animation-delay:.24s}
  body.firstpaint .safelight{animation:safeglow 1.5s ease-out 1}
  .stage.s-running .lamp{animation:lamppulse 1.8s ease-in-out infinite}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
}
</style></head>
<body class=firstpaint>
<header><div class=wrap>
  <div class="hbar warm">
    <div class=brand>
      <img src="/static/brand/loupe-wordmark.svg" alt="Loupe"
           onerror="this.replaceWith(Object.assign(document.createElement('span'),{textContent:'LOUPE',style:'font-family:var(--hd);font-size:30px;letter-spacing:.04em'}))">
      <span class=kick><span class=safelight aria-hidden=true></span>the darkroom</span>
    </div>
    <div class=ovr id=ovr></div>
  </div>
</div></header>

<main class=wrap>
  <div class="lede warm d1">
    <h1>Your library is <em>developing</em>.</h1>
    <p>Loupe is pulling your photos off iCloud and bringing them up in the
       tray — one tray at a time. This page watches the chemistry; it doesn't
       touch it. Leave it open, or come back later.</p>
  </div>

  <div class="ledger warm d1" id=ledger hidden></div>
  <div class="ready warm d1" id=ready>
    <span class=dot aria-hidden=true></span>
    <span class=t>Prints are dry.</span>
    <span class=s id=readymeta></span>
    <a class=enter href="/">Enter the library →</a>
  </div>

  <div id=phases></div>

  <footer class=warm>
    Read-only status · refreshes every 4s · <span id=stamp></span><br>
    Loupe — look closely, choose well.
  </footer>
</main>

<script id=initstate type="application/json">__INIT__</script>
<style>
/* P15's ledger room, in the one place someone already looks at the machine's health.
   Backup state gets a surface because it silently failed for three days and every
   status anyone would think to check said fine. Neutral until it is stale. */
.ledger{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border:1px solid var(--p-line,#2a2318);border-radius:8px;padding:9px 13px;margin:0 0 12px;
  font-family:var(--mo,ui-monospace,monospace);font-size:11.5px;color:#9c8b76;
  font-variant-numeric:tabular-nums}
.ledger b{color:#e9e4d6;font-weight:500}
.ledger .lk{font-variant-caps:all-small-caps;letter-spacing:.05em;color:#766c5a}
.ledger.stale{border-color:#b0463e;color:#e0a49e}
.ledger.stale b{color:#ffd9d4}
.ledger a{color:inherit}
.actline{display:inline-flex;align-items:center;gap:7px}
.actdot{width:7px;height:7px;border-radius:50%;background:#7fd18f;flex:0 0 auto;
  animation:actpulse 1.6s ease-in-out infinite}
@keyframes actpulse{0%,100%{opacity:.35}50%{opacity:1}}
@media (prefers-reduced-motion:reduce){.actdot{animation:none;opacity:.9}}
</style>
<script>
/* P2.2 (W23): ONE client chokepoint. Every non-GET request on this surface picks up
   the shared write token, and a 403 "write token required" enrolls this device once and
   retries. Loopback is exempt server-side, so nothing running on the Loupe host itself
   needs a token. Installed here rather than at the ~19 individual fetch() call sites so
   a new write inherits the header by construction. */
(function(){
  var KEY='loupe_write_token', orig=window.fetch.bind(window);
  function stamp(i,t){ i.headers=Object.assign({},i.headers,{'X-Loupe-Write-Token':t}); }
  window.fetch=function(input,init){
    init=Object.assign({},init||{});
    var m=(init.method||'GET').toUpperCase();
    if(m==='GET'||m==='HEAD') return orig(input,init);
    var t=localStorage.getItem(KEY);
    if(t) stamp(init,t);
    return orig(input,init).then(function(r){
      if(r.status!==403) return r;
      return r.clone().json().catch(function(){return null;}).then(function(j){
        if(!j||j.error!=='write token required') return r;
        var n=window.prompt('Loupe write token for this device:','');
        if(!n||!n.trim()) return r;
        n=n.trim(); localStorage.setItem(KEY,n); stamp(init,n);
        return orig(input,init);
      });
    });
  };
})();
const PHASES=[
 {key:'connect',num:'01',name:'Connect'},
 {key:'pull',   num:'02',name:'Pull'},
 {key:'process',num:'03',name:'Process'},
 {key:'finish', num:'04',name:'Finish'},
];
const LABELS={done:'Dry',running:'Working',needs_you:'Needs you',
 queued:'Queued',blocked:'In the dark',error:'Error',unknown:'Unknown'};
// "Prints are dry" reads "Dry"; the others read literally except finish-done.
function pillLabel(st,id){
 if(st==='done') return id==='dry' ? 'Dry' : 'Done';
 return LABELS[st]||st;
}
const $=s=>document.querySelector(s);
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function nfmt(n){return n==null?null:Number(n).toLocaleString();}
function etaFmt(s){if(s==null)return null;s=Math.max(0,Math.round(s));
 if(s<90)return '~'+s+'s';const m=Math.round(s/60);if(m<90)return '~'+m+'m';
 const h=Math.floor(m/60);return '~'+h+'h '+(m%60)+'m';}

const CONNECT_CARD=`
 <div class="connect warm" id=connectcard>
  <div class=srcchoice role=group aria-label="Library source">
   <button type=button class=srcopt id=srcExisting aria-pressed=false>I already have my library</button>
   <button type=button class=srcopt id=srcIcloud aria-pressed=false>Download from iCloud</button>
  </div>

  <div class=srcpane id=existingPane hidden>
   <div class=note>Already have your library on disk? Point Loupe at it and skip the
     download. Loupe only <b>reads</b> it — nothing is moved, copied, or deleted.</div>
   <div class=fgrid>
    <div class="field full"><label>Where your library lives</label>
      <input type=text id=libpath inputmode=url spellcheck=false autocomplete=off
             placeholder="/mnt/nas2/photos"></div>
   </div>
   <div class=cbtns>
    <button class=primary type=button id=libsave>Use this library</button>
    <span class=libmsg id=libmsg role=status></span>
   </div>
  </div>

  <div class=srcpane id=icloudPane hidden>

   <!-- STEP A — pre-flight checklist (prevents the #1 dead-end before the form). -->
   <div id=preflight>
     <div class=pfhead>Before you sign in — three quick checks</div>
     <ol class=pflist>
       <li><b>Turn iCloud web access on, ADP off.</b> Two <em>independent</em> toggles,
         in different spots under <span class=mono>Settings → [your name] → iCloud</span>:
         turn <b>“Access iCloud Data on the Web”</b> <b>ON</b>, and turn
         <b>Advanced Data Protection</b> <b>OFF</b> (under <span class=mono>Advanced</span>).
         If either is wrong, Apple returns a 421 and the sign-in can’t proceed. Give it
         ~5 minutes to propagate, then confirm you can sign in at
         <span class=mono>icloud.com</span>.
         <div class=pfnote>ADP-off is a real tradeoff — it’s required for this one-time
           pull. Re-enable it afterwards.</div></li>
       <li><b>Two-factor is on, with a trusted device handy</b> — you’ll get a 6-digit
         code to type in here.</li>
       <li><b>Your real Apple ID password</b> is ready (not an app-specific password).</li>
     </ol>
     <div class=cbtns>
       <button class=primary type=button id=pfContinue>I’ve set these up — continue</button>
     </div>
   </div>

   <!-- STEP B — credentials (revealed after the checklist). -->
   <div id=credForm hidden>
     <div class=note>iCloud Photos rejects app-specific passwords, so this needs your
       <b>real</b> Apple ID password — and two-factor still gates it, so the password
       alone can’t sign in. It’s sent once to establish the session and never stored.</div>
     <div class=fgrid>
       <div class=field><label>Apple ID</label>
         <input type=email id=cAppleId placeholder="you@icloud.com" autocomplete=off
                autocapitalize=off spellcheck=false></div>
       <div class=field><label>Apple ID password</label>
         <input type=password id=cPassword placeholder="your real Apple ID password"
                autocomplete=off></div>
     </div>
     <div class=cbtns>
       <button class=primary type=button id=cSignin>Sign in</button>
       <span class=connmsg id=connmsg role=status></span>
     </div>
     <div class=fix421 id=fix421>
       <span class=h>iCloud returned a 421 — two toggles to fix</span>
       On an Apple device, open <b>Settings → [your name] → iCloud</b>. These are
       <b>independent</b> settings in <b>different</b> places:
       <b>turn “Access iCloud Data on the Web” ON</b>, and under <b>Advanced</b>,
       <b>turn Advanced Data Protection OFF</b>. Wait ~5 minutes to propagate, confirm
       you can sign in at <b>icloud.com</b>, then try again here. (Re-enable ADP after
       the one-time pull.)
     </div>
   </div>

   <!-- STEP C — 2FA code (revealed when /start reports two-factor required). -->
   <div id=twofaForm hidden>
     <div class=note>A 6-digit code was sent to your trusted Apple devices. Enter it to
       finish signing in.</div>
     <div class=fgrid>
       <div class=field><label>Two-factor code</label>
         <input type=text id=cCode inputmode=numeric autocomplete=one-time-code
                maxlength=6 pattern="[0-9]*" placeholder="123456"></div>
     </div>
     <div class=cbtns>
       <button class=primary type=button id=cVerify>Verify &amp; finish</button>
       <span class=connmsg id=twofamsg role=status></span>
     </div>
   </div>

   <!-- STEP D — the actual pull trigger stays INERT (Phase 2b wires it). -->
   <div id=connDone hidden>
     <div class=note ok>Signed in — your iCloud session is established.</div>
   </div>
   <div class=cbtns style="margin-top:14px">
     <button class=primary type=button disabled>Connect &amp; load the roll</button>
     <span class=soon>the pull arrives next</span>
   </div>
  </div>
 </div>`;

const ENRICH_CARD=`
 <div class="connect warm" id=enrichcard>
  <div class=note>Bring your Mac's Apple Photos data — scene labels, aesthetic scores,
    and people — into Loupe.</div>
  <div class="field full"><label>Apple enrichment bundle</label>
    <div class=cbtns style="margin-top:8px">
      <button class=primary type=button id=enrchoose>Choose bundle…</button>
      <span class=soon id=enrname>no file chosen</span>
    </div>
  </div>
  <input type=file id=enrfile accept=".tgz,.gz,application/gzip" hidden>
  <div class=pfnote style="margin-top:12px">Make this on your Mac first — run the Loupe
    helper in Terminal, then drop the .tgz it produces here.</div>
  <div class=actbar id=enrbar hidden></div>
  <div class=cbtns style="margin-top:11px">
    <span class=libmsg id=enrmsg role=status></span>
  </div>
  <div class=pfnote id=enrbreak style="margin-top:9px"></div>
 </div>`;

// The connect card is interactive and must survive the 4s poll re-render, so it's
// mounted ONCE into a stable #connectmount and never rebuilt (the input keeps its
// value/focus). Only the read-only stage cards re-render on poll.
function mountConnect(){
 const m=$('#connectmount');
 if(!m || m.firstChild) return;          // absent this render, or already mounted
 m.innerHTML=CONNECT_CARD;
 wireConnect();
}
function selectSource(mode){
 const ex=mode==='existing';
 $('#srcExisting').setAttribute('aria-pressed', ex?'true':'false');
 $('#srcIcloud').setAttribute('aria-pressed', ex?'false':'true');
 $('#existingPane').hidden=!ex;
 $('#icloudPane').hidden=ex;
 if(ex) setTimeout(()=>{const i=$('#libpath'); if(i) i.focus();},0);
}
function setLibMsg(text,kind){const e=$('#libmsg'); if(!e)return;
 e.textContent=text||''; e.className='libmsg'+(kind?' '+kind:'');}
async function saveLibrary(){
 const inp=$('#libpath'), btn=$('#libsave');
 const root=(inp&&inp.value||'').trim();
 if(!root){setLibMsg('Enter where your library lives.','err'); if(inp)inp.focus(); return;}
 btn.disabled=true; setLibMsg('Checking…','');
 try{
   const res=await fetch('/api/setup/library',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({source:'existing',library_root:root})});
   const d=await res.json().catch(()=>({}));
   if(res.ok && d.ok){
     setLibMsg('Saved — using your existing library.','ok');
     poll();                              // refresh status now (Connect/Pull → done)
   }else{
     setLibMsg(d.error||'Could not use that folder.','err');
   }
 }catch(e){ setLibMsg('Network error — try again.','err'); }
 finally{ btn.disabled=false; }
}
// ---- iCloud auth handshake (Connect 2a): pre-flight → credentials → 2FA ----
let TWOFA_TOKEN=null;
function setConnMsg(text,kind){const e=$('#connmsg'); if(e){e.textContent=text||'';e.className='connmsg'+(kind?' '+kind:'');}}
function set2faMsg(text,kind){const e=$('#twofamsg'); if(e){e.textContent=text||'';e.className='connmsg'+(kind?' '+kind:'');}}
function show421(on){const e=$('#fix421'); if(e) e.classList.toggle('show',!!on);}
// Map a server state to a user message (never leaks credentials or raw child output).
const CONN_ERR={
 bad_password:'That Apple ID or password was not accepted.',
 needs_web_access:'iCloud returned a 421 — see the two toggles below.',
 timeout:'The sign-in timed out. Start again.',
 error:'Sign-in could not be completed. Start again.',
 bad_code:'That code was not accepted — check it and try again.',
};
async function startSignin(){
 const aid=($('#cAppleId')||{}).value, pw=($('#cPassword')||{}).value;
 const btn=$('#cSignin'); show421(false);
 if(!aid||!aid.trim()){setConnMsg('Enter your Apple ID.','err');return;}
 if(!pw){setConnMsg('Enter your Apple ID password.','err');return;}
 btn.disabled=true; setConnMsg('Signing in…','work');
 let d={};
 try{
   const res=await fetch('/api/connect/start',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({apple_id:aid.trim(),password:pw})});
   d=await res.json().catch(()=>({}));
 }catch(e){ setConnMsg('Network error — try again.','err'); btn.disabled=false; return; }
 finally{ const p=$('#cPassword'); if(p) p.value=''; }   // clear the password field immediately
 btn.disabled=false;
 if(d.state==='requires_2fa'){
   TWOFA_TOKEN=d.token; setConnMsg('Code sent to your devices.','ok');
   $('#credForm').hidden=true; $('#twofaForm').hidden=false;
   setTimeout(()=>{const c=$('#cCode'); if(c) c.focus();},0);
 }else if(d.state==='authenticated'){
   onSignedIn();
 }else{
   if(d.state==='needs_web_access') show421(true);
   setConnMsg(CONN_ERR[d.state]||d.message||'Sign-in failed.','err');
 }
}
async function verify2fa(){
 const code=($('#cCode')||{}).value, btn=$('#cVerify');
 if(!/^\d{6}$/.test((code||'').trim())){set2faMsg('Enter the 6-digit code.','err');return;}
 btn.disabled=true; set2faMsg('Verifying…','work');
 let d={};
 try{
   const res=await fetch('/api/connect/2fa',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({token:TWOFA_TOKEN,code:code.trim()})});
   d=await res.json().catch(()=>({}));
 }catch(e){ set2faMsg('Network error — try again.','err'); btn.disabled=false; return; }
 finally{ const c=$('#cCode'); if(c) c.value=''; }
 btn.disabled=false;
 if(d.state==='authenticated'){ onSignedIn(); }
 else if(d.state==='error'||d.state==='timeout'){
   // session expired — send the user back to the credential step
   TWOFA_TOKEN=null; $('#twofaForm').hidden=true; $('#credForm').hidden=false;
   setConnMsg(CONN_ERR[d.state]||'Start the sign-in again.','err');
 }else{
   set2faMsg(CONN_ERR[d.state]||d.message||'Verification failed.','err');
 }
}
function onSignedIn(){
 TWOFA_TOKEN=null;
 $('#preflight').hidden=true; $('#credForm').hidden=true; $('#twofaForm').hidden=true;
 $('#connDone').hidden=false;
 poll();                                  // refresh status (Connect stage reflects it)
}
function wireConnect(){
 const e=$('#srcExisting'), i=$('#srcIcloud');
 if(e) e.onclick=()=>selectSource('existing');
 if(i) i.onclick=()=>selectSource('icloud');
 const s=$('#libsave'); if(s) s.onclick=saveLibrary;
 const p=$('#libpath'); if(p) p.onkeydown=ev=>{if(ev.key==='Enter'){ev.preventDefault();saveLibrary();}};
 // iCloud handshake wiring
 const pf=$('#pfContinue'); if(pf) pf.onclick=()=>{      // soft gate: reveal the form
   $('#preflight').hidden=true; $('#credForm').hidden=false;
   setTimeout(()=>{const a=$('#cAppleId'); if(a) a.focus();},0);};
 const si=$('#cSignin'); if(si) si.onclick=startSignin;
 const pw=$('#cPassword'); if(pw) pw.onkeydown=ev=>{if(ev.key==='Enter'){ev.preventDefault();startSignin();}};
 const vf=$('#cVerify'); if(vf) vf.onclick=verify2fa;
 const cc=$('#cCode'); if(cc) cc.onkeydown=ev=>{if(ev.key==='Enter'){ev.preventDefault();verify2fa();}};
}

// ---- enrichment import ("Read the negatives") — mirrors the Connect picker pattern ----
const ENR_LABELS={'filename+date':'filename+date','filename+filesize':'filename+size',
 'filesize_tiebreak':'size-tiebreak','live_photo_video':'live-photo','filename+date_tz':'tz',
 'ambiguous_unresolved':'ambiguous','unmatched':'no Photos record',
 'filename_unique':'filename-unique','filename_only':'filename-only'};
// render() rebuilds #enrichmount from the static ENRICH_CARD template on every
// stage-state change, so the card's presentation must be state-restorable. enrUI holds
// the last-applied state and is replayed by applyEnrUI() on every rebuild. null = idle.
let enrUI=null;     // {text, kind, breakText, busy}
function applyEnrUI(){
 if(!enrUI) return;
 const msg=$('#enrmsg'); if(msg){msg.textContent=enrUI.text||''; msg.className='libmsg'+(enrUI.kind?' '+enrUI.kind:'');}
 const br=$('#enrbreak'); if(br) br.textContent=enrUI.breakText||'';
 const bar=$('#enrbar'); if(bar) bar.hidden=!enrUI.busy;
 const btn=$('#enrchoose'); if(btn) btn.disabled=!!enrUI.busy;   // bar AND button derive from busy
}
function setEnrMsg(text,kind){enrUI=enrUI||{}; enrUI.text=text; enrUI.kind=kind||null; applyEnrUI();}
function enrBusy(on){enrUI=enrUI||{}; enrUI.busy=!!on; applyEnrUI();}
function kfmt(n){n=Number(n)||0; return n>=1000?((n/1000).toFixed(1).replace(/\.0$/,'')+'k'):(''+n);}
function renderMethods(m){if(!m)return; enrUI=enrUI||{};
 enrUI.breakText=Object.keys(m).sort((a,b)=>m[b]-m[a]).map(k=>(ENR_LABELS[k]||k)+' '+kfmt(m[k])).join(' · ');
 applyEnrUI();}
function wireEnrich(){
 const b=$('#enrchoose'), f=$('#enrfile');
 if(b&&f) b.onclick=()=>f.click();
 if(f) f.onchange=()=>{const file=f.files&&f.files[0]; if(!file)return;
   const n=$('#enrname'); if(n) n.textContent=file.name;
   uploadBundle(file);};
}
async function uploadBundle(file){
 enrBusy(true); setEnrMsg('Uploading…','work');
 let d={};
 try{
   const res=await fetch('/api/enrich/import',{method:'POST',
     headers:{'Content-Type':'application/octet-stream'}, body:file});
   d=await res.json().catch(()=>({}));
   if(!res.ok || !d.job_id){
     enrBusy(false); setEnrMsg(d.error||'Upload failed','err'); return;
   }
 }catch(e){ enrBusy(false); setEnrMsg('Network error — try again.','err'); return; }
 setEnrMsg('Rebuilding enrichment from your bundle…','work');
 pollEnrich(d.job_id);
}
// LOAD-BEARING: the worker swaps the db, writes a durable "done" marker, then RESTARTS
// the service — so a thrown/failed fetch is the restart gap, NOT a failure. Recursive
// 2s setTimeout so it stops cleanly on done/failed.
function pollEnrich(jobId){
 setTimeout(async()=>{
   let d;
   try{
     const res=await fetch('/api/enrich/status?job='+jobId,{cache:'no-store'});
     if(!res.ok) throw 0;
     d=await res.json();
   }catch(e){
     setEnrMsg('Applying — Loupe is reloading for a moment… (reconnects automatically)','work');
     return pollEnrich(jobId);
   }
   if(d.state==='done'){
     enrBusy(false);
     setEnrMsg('Done — '+Number(d.coverage||0).toLocaleString()+' photos enriched','ok');
     renderMethods(d.methods);
     poll();                              // flip the negatives stage to green immediately
     return;
   }
   if(d.state==='failed'){
     enrBusy(false);
     setEnrMsg(d.error||'Import failed — your existing data is unchanged.','err');
     return;
   }
   // building / unknown -> keep polling
   setEnrMsg('Rebuilding enrichment from your bundle…','work');
   pollEnrich(jobId);
 }, 2000);
}
function mountEnrich(){
 const m=$('#enrichmount');
 if(!m || m.firstChild) return;          // absent this render, or already mounted
 m.innerHTML=ENRICH_CARD;
 wireEnrich();
 applyEnrUI();           // restore terminal (Done/Failed + breakdown) or in-flight state
}

// ---- compute-stage trigger cards (develop / contact_prints / faces) ----------------
// Generalized from the develop card: one replayable-state machine PER stage, keyed by
// stage id. Each card keeps the enrUI scar contract — a terminal Done/Failed survives the
// 4s #phases re-render because mountRun() replays runUI[stage] after every rebuild.
const RUN_MAX_RETRIES=5;   // matches stage_runner --max-retries default
const RUN_STAGES={
 develop:{
  note:`Develop the roll — read every frame's metadata into the library so it can be dated, placed and sorted. Loupe only <b>reads</b> your originals; nothing is moved.`,
  btn:'Develop the roll', doneBtn:'Developed ✓',
  idle:t=>'Ready — '+nfmt(t)+' frames to develop.',
  run:(d,t)=>'Developing… '+nfmt(d)+' / '+nfmt(t)+' frames',
  done:t=>'Developed — '+nfmt(t)+' frames.',
  fail:'Develop failed — check the negatives and try again.'},
 contact_prints:{
  note:`Make the contact prints — a small thumbnail for every frame so the library is browsable. Reads your originals; writes only the thumbnail cache.`,
  btn:'Make the prints', doneBtn:'Printed ✓',
  idle:t=>'Ready — '+nfmt(t)+' prints to make.',
  run:(d,t)=>'Printing… '+nfmt(d)+' / '+nfmt(t),
  done:t=>'Printed — '+nfmt(t)+' contact prints.',
  fail:'Printing failed — try again.'},
 faces:{
  note:`Spot the faces — scan every image for faces so people can be grouped later. Optional; reads images only.`,
  btn:'Spot the faces', doneBtn:'Faces spotted ✓',
  idle:t=>'Ready — '+nfmt(t)+' frames to scan.',
  run:(d,t)=>'Scanning faces… '+nfmt(d)+' / '+nfmt(t),
  done:t=>'Faces spotted — '+nfmt(t)+' frames.',
  fail:'Face pass failed — try again.'},
 // gated/opt-in card: default OFF; an Enable step (+ disclosure) precedes the scan.
 nsfw:{
  gated:true, enableBtn:'Enable on-device screening',
  note:`On-device nudity screening — NudeNet 3.4.2 (MIT), runs entirely on this machine. The classifier ships with Loupe; nothing is uploaded, and nothing is downloaded at scan time. Review-only: flagged frames are never deleted — they're routed to your private review and hidden from shared viewers.`,
  disabledMsg:'Off — opt in to screen on-device.',
  btn:'Scan for nudity', doneBtn:'Screened ✓',
  idle:t=>'Ready — '+nfmt(t)+' frames to screen.',
  run:(d,t)=>'Screening… '+nfmt(d)+' / '+nfmt(t),
  done:t=>'Screened — '+nfmt(t)+' frames.',
  fail:'Nudity screen failed — try again.'},
};
// per-stage replayable state + in-flight poll guards (keyed by stage id)
const runUI={};        // stage -> {state, text, kind, done, total, pct, busy}
const runPolling={};   // stage -> bool
let nsfwEnabled=false;  // opt-in flag, mirrored from the /api/setup/status nsfw stage each render()
function runCardHTML(stage){
 const c=RUN_STAGES[stage];
 // gated cards carry an extra Enable button (shown only while disabled; applyRunUI toggles).
 const enableBtn = c.gated ? `<button class=primary type=button id=${stage}enable>${c.enableBtn}</button>` : '';
 return `<div class="connect warm" id=${stage}card>
  <div class=note>${c.note}</div>
  <div class=cbtns style="margin-top:8px">
    ${enableBtn}
    <button class=primary type=button id=${stage}btn>${c.btn}</button>
    <span class=libmsg id=${stage}msg role=status></span>
  </div>
  <div class=actbar id=${stage}bar hidden></div>
 </div>`;
}
function applyRunUI(stage){
 const u=runUI[stage]; if(!u) return; const c=RUN_STAGES[stage];
 const msg=$('#'+stage+'msg'); if(msg){msg.textContent=u.text||''; msg.className='libmsg'+(u.kind?' '+u.kind:'');}
 const bar=$('#'+stage+'bar'); if(bar) bar.hidden=!u.busy;                 // bar AND button derive from busy
 const en=$('#'+stage+'enable'), btn=$('#'+stage+'btn');
 if(c.gated && u.state==='disabled'){                                      // gated, not yet opted in
   if(en) en.hidden=false;
   if(btn) btn.hidden=true;
 } else {
   if(en) en.hidden=true;
   if(btn){ btn.hidden=false;
     if(u.state==='done'){ btn.textContent=c.doneBtn; btn.disabled=true; }
     else { btn.textContent=c.btn; btn.disabled=!!u.busy; }
   }
 }
}
function setRunMsg(stage,text,kind){const u=runUI[stage]=runUI[stage]||{}; u.text=text; u.kind=kind||null; u.busy=(kind==='work'); applyRunUI(stage);}
// Map merged /api/run/status -> runUI[stage] (one amber: running/retrying=amber, done=green,
// failed=red — same palette as the enrich card).
function setRunFromStatus(stage,d){
 const c=RUN_STAGES[stage], done=Number(d.done||0), total=Number(d.total||0);
 // gated card: until the owner opts in (nsfwEnabled, mirrored from /api/setup/status),
 // the card is 'disabled' regardless of the underlying scan state.
 let st=d.state||'idle';
 if(c.gated && !nsfwEnabled) st='disabled';
 const u=runUI[stage]=runUI[stage]||{};
 u.state=st; u.done=done; u.total=total; u.pct=d.pct;
 if(st==='disabled'){ u.busy=false; u.kind=null; u.text=c.disabledMsg||''; }
 else if(st==='running'){ u.busy=true; u.kind='work'; u.text=c.run(done,total); }
 else if(st==='retrying'){ u.busy=true; u.kind='work'; u.text='Stalled — retrying ('+(d.attempt||1)+'/'+RUN_MAX_RETRIES+')…'; }
 else if(st==='done'){ u.busy=false; u.kind='ok'; u.text=c.done(total); }
 else if(st==='failed'){ u.busy=false; u.kind='err'; u.text=d.error||c.fail; }
 else { u.busy=false; u.kind=null; u.text=c.idle(total); }
 applyRunUI(stage);
}
async function startRun(stage){
 setRunMsg(stage,'Starting…','work');
 let d={};
 try{
   const res=await fetch('/api/run/start',{method:'POST',
     headers:{'Content-Type':'application/json'}, body:JSON.stringify({stage})});
   d=await res.json().catch(()=>({}));
   if(!res.ok){ setRunMsg(stage,d.error||'Could not start.','err'); return; }
 }catch(e){ setRunMsg(stage,'Network error — try again.','err'); return; }
 pollRun(stage);          // 200 (starting | running | already running) -> watch it
}
// Opt-in: enable on-device screening (LAN-gated owner route), then flip the gated card
// from disabled -> idle by re-seeding from status (now enabled).
async function enableNsfw(stage){
 const en=$('#'+stage+'enable'); if(en) en.disabled=true;
 setRunMsg(stage,'Enabling…','work');
 try{
   const res=await fetch('/api/settings/nsfw',{method:'POST',
     headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:true})});
   const d=await res.json().catch(()=>({}));
   if(!res.ok || !d.nsfw_enabled){ if(en) en.disabled=false; setRunMsg(stage,d.error||'Could not enable.','err'); return; }
 }catch(e){ if(en) en.disabled=false; setRunMsg(stage,'Network error — try again.','err'); return; }
 nsfwEnabled=true;                       // optimistic; render() re-confirms from the server
 runPolling[stage]=false; runUI[stage]=null;
 seedRun(stage);                         // re-read status -> idle (Scan)
}
// LOAD-BEARING: loupe can RESTART under this poll (e.g. an enrich import) — a dropped fetch
// is the restart window, NOT a failure. Recursive 2s setTimeout; stops only on terminal.
function pollRun(stage){
 if(runPolling[stage]) return; runPolling[stage]=true;
 const tick=async()=>{
   let d;
   try{
     const res=await fetch('/api/run/status?stage='+stage,{cache:'no-store'});
     if(!res.ok) throw 0;
     d=await res.json();
   }catch(e){ setTimeout(tick,2000); return; }   // transient/restart — keep working state
   setRunFromStatus(stage,d);
   if(d.state==='done'||d.state==='failed'){ runPolling[stage]=false; return; }
   setTimeout(tick,2000);
 };
 setTimeout(tick,0);
}
async function seedRun(stage){   // first paint: seed idle/terminal text, resume polling if live
 let d; try{ const r=await fetch('/api/run/status?stage='+stage,{cache:'no-store'}); if(!r.ok) return; d=await r.json(); }catch(e){ return; }
 setRunFromStatus(stage,d);
 if((d.state==='running'||d.state==='retrying') && !runPolling[stage]) pollRun(stage);
}
function mountRun(stage){
 const m=$('#'+stage+'mount');
 if(!m || m.firstChild) return;          // absent this render, or already mounted
 m.innerHTML=runCardHTML(stage);
 const b=$('#'+stage+'btn'); if(b) b.onclick=()=>startRun(stage);
 const en=$('#'+stage+'enable'); if(en) en.onclick=()=>enableNsfw(stage);
 applyRunUI(stage);         // replay last-known state across the poll rebuild (terminal survives)
 if(!runUI[stage] && !runPolling[stage]) seedRun(stage);   // only on the very first mount
}

let mounted=false;
// Relative, coarse, and honest: a tray that ran three hours ago does not need minutes,
// and "just now" beats "0h ago". Absolute time lives in the title attribute for anyone
// who needs it.
function agoFmt(ts){
 if(!ts) return null;
 const secs = Math.max(0, Math.floor(Date.now()/1000 - ts));
 if(secs < 90) return 'just now';
 if(secs < 5400) return Math.round(secs/60)+'m ago';
 if(secs < 172800) return Math.round(secs/3600)+'h ago';
 return Math.round(secs/86400)+'d ago';
}
function agoTitle(ts){
 if(!ts) return '';
 try{ return new Date(ts*1000).toLocaleString(); }catch(e){ return ''; }
}
function stageCard(s){
 const p=s.progress||{};
 const done=p.done, total=p.total;
 const pct = (total && done!=null) ? Math.min(100, 100*done/total) : (s.status==='done'?100:0);
 const nums=[];
 if(done!=null||total!=null){
   nums.push(`<span><b>${done!=null?nfmt(done):'—'}</b>${total!=null?' / '+nfmt(total):''}</span>`);
 }
 if(s.status==='running'){
   if(p.rate_per_s) nums.push(`<span>${p.rate_per_s.toFixed(2)}/s</span>`);
   const e=etaFmt(p.eta_seconds); if(e) nums.push(`<span>eta ${e}</span>`);
 }
 if(total && done!=null && s.status!=='done') nums.push(`<span>${(100*done/total).toFixed(0)}%</span>`);
 const ago = agoFmt(s.last_run);
 if(ago) nums.push(`<span class=ranat title="${esc(agoTitle(s.last_run))}">${esc(ago)}</span>`);
 const showBar = (total && done!=null) || s.status==='running' || s.status==='done';
 return `<div class="stage s-${esc(s.status)}${(!mounted)?' warm':''}" data-id="${esc(s.id)}">
   <span class=lamp aria-hidden=true></span>
   <div class=top>
     <span class=nm>${esc(s.name)}</span>
     ${s.location&&s.location!=='none'?`<span class=loc>${esc(s.location)}</span>`:''}
     ${s.optional?`<span class=opt>optional</span>`:''}
   </div>
   <span class=pill role=status>${esc(pillLabel(s.status,s.id))}</span>
   <div class=detail>${esc(s.detail||'')}</div>
   ${showBar?`<div class=pbar><i style="width:${pct.toFixed(1)}%"></i></div>`:''}
   ${nums.length?`<div class=nums>${nums.join('')}</div>`:''}
   ${s.logline?`<div class=log>${esc(s.logline)}</div>`:''}
 </div>`;
}

function render(model){
 const stages=model.stages||[];
 const active=model.overall&&model.overall.active_phase;
 const byPhase={};
 stages.forEach(s=>{(byPhase[s.phase]=byPhase[s.phase]||[]).push(s);});
 let html='';
 for(const ph of PHASES){
   const list=byPhase[ph.key]||[];
   if(!list.length && ph.key!=='connect') continue;
   const dn=list.filter(s=>s.status==='done').length;
   const meta = list.length ? `${dn}/${list.length} done` : '';
   html+=`<section class="phase warm d${PHASES.indexOf(ph)%4+1}">
     <div class=phead>
       <span class=pn>${ph.num}</span>
       <span class="pname${active===ph.key?' active':''}">${ph.name}</span>
       <span class=pmeta>${meta}</span>
     </div>`;
   if(ph.key==='connect') html+='<div id=connectmount></div>';
   // enrichment-import card mounts right after the "Read the negatives" stage card
   html+=list.map(s=>stageCard(s)
     +(s.id==='negatives'?'<div id=enrichmount></div>':'')
     +(['develop','contact_prints','faces','nsfw'].includes(s.id)?'<div id='+s.id+'mount></div>':'')).join('');
   html+=`</section>`;
 }
 $('#phases').innerHTML=html;
 mountConnect();          // build the interactive connect card once; polls won't clobber it
 mountEnrich();           // same persistence mechanism for the enrichment-import card
 // mirror the opt-in flag from the nsfw stage BEFORE mounting its gated card
 const _ns=stages.find(s=>s.id==='nsfw'); if(_ns) nsfwEnabled=!!_ns.enabled;
 mountRun('develop');     // compute-stage trigger cards (per-stage replayable state)
 mountRun('contact_prints');
 mountRun('faces');
 mountRun('nsfw');        // gated/opt-in card

 const o=model.overall||{};
 {
  // P15: the ledger room. Reports the newest file actually present in each destination
  // and its age -- never a unit's exit code, which is exactly what lied for three days.
  const L = model.ledger, el = $('#ledger');
  if (el) {
    if (!L) { el.hidden = true; }
    else {
      el.hidden = false;
      el.classList.toggle('stale', !!L.stale);
      const part = (label, x) => x
        ? `<span><span class=lk>${label}</span> <b>${esc(x.name)}</b> · ${x.age_hours}h ago</span>`
        : `<span><span class=lk>${label}</span> <b>none</b></span>`;
      el.innerHTML =
        `<span class=lk>${L.stale ? 'backup stale' : 'backups'}</span>` +
        part('nas', L.nas) + part('off-host', L.offhost) +
        `<span class=lk>restore · ${esc(L.restore_runbook || '')}</span>`;
    }
  }
 }
 const act = model.activity;
 if(act){
  const bits=[];
  if(act.done!=null) bits.push(nfmt(act.done)+(act.total!=null?' / '+nfmt(act.total):''));
  if(act.rate_per_s) bits.push(act.rate_per_s.toFixed(2)+'/s');
  const e = etaFmt(act.eta_seconds); if(e) bits.push('eta '+e);
  $('#ovr').innerHTML=`<span class=actline><span class=actdot aria-hidden=true></span>`+
    `<b>${esc(act.name||act.stage)}</b>${bits.length?' &middot; '+esc(bits.join(' · ')):''}</span>`;
 } else {
  $('#ovr').innerHTML=`<b>${o.stages_done||0}</b> / ${o.stages_total||0} trays clear`;
 }
 const r=$('#ready'); r.classList.toggle('show', !!o.ready);
 if(o.ready){const lib=model.library||{};
   $('#readymeta').textContent = lib.originals_present!=null
     ? nfmt(lib.originals_present)+' originals' : '';}
 const d=new Date((model.generated_at||0)*1000);
 $('#stamp').textContent='as of '+(model.generated_at?d.toLocaleTimeString():'—');
 mounted=true;
}

// first paint from the injected state — no blank flash, warm-up plays once.
try{ render(JSON.parse($('#initstate').textContent)); }catch(e){ console.error(e); }
// drop the first-paint class once the warm-up has run so polling never replays it.
window.addEventListener('load',()=>setTimeout(()=>document.body.classList.remove('firstpaint'),1600));

async function poll(){
 try{
   const res=await fetch('/api/setup/status',{cache:'no-store'});
   if(res.ok) render(await res.json());
 }catch(e){/* transient; try again next tick */}
}
setInterval(poll, 4000);
</script>
</body></html>"""


def render(initial_json):
    return _TEMPLATE.replace("__INIT__", initial_json)
