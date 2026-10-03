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

const MON=['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MON_FULL=['','January','February','March','April','May','June','July','August','September','October','November','December'];
const $=s=>document.querySelector(s);
let view='overview',ov=null,month=null,day=null,seq=[],byId={},fidx=-1;
let todayMD=null,calendarData=null;   // On This Day (date being shown) + Calendar overview payload
// Every full-screen feature overlay, plus the nested person-detail / trip-sheet sub-overlays
// and Focus. Two jobs: the breadcrumb band hides whenever any is .on (renderCrumb), and
// closeOverlays() is the SINGLE place that drops them — so no show*() can leak a stale sibling
// (the Stage-1 settings→cuttingview bug). closeOverlays(except) clears all but `except`.
// resmodal joined the registry 2026-08-09: it is position:fixed inset:0 at z-index 55 --
// higher than every other overlay -- but was never registered, so closeOverlays() could
// not drop it. Opening the residence form and then navigating away left a full-viewport
// scrim sitting on top of the new view. Exactly the stale-sibling class this registry
// exists to prevent. tests/test_overlays.py now fails if a fixed full-viewport overlay
// is added without registering it.
const OVL=['placesview','tripsheet','mapview','peopleview','persondetail','vaultview','nsfwview','cuttingview','calendarview','settingsview','searchview','focus','resmodal','keysview','paletteview','exportgate','triageview'];
// --- command palette (audit 8.5) ------------------------------------------
// "One input to go anywhere." Sources are whatever the client already has -- the seven
// routes and the overview's years and months -- so opening the palette costs no request
// and cannot be stale relative to what is on screen. Semantic search as a provider
// (8.5's "beach 2019" -> embeddings) is a later addition; this is the navigation half.
let PAL=[], palSel=0, PAL_PEOPLE=null;

// 9.5 makes the palette the only search input, fanning into Places/Trips, People and
// Frames. People are not otherwise loaded until the People view opens, so fetch them
// once on first palette use and keep them -- a name list is small and does not go stale
// within a session in any way that matters here.
async function palLoadPeople(){
 if(PAL_PEOPLE)return PAL_PEOPLE;
 try{ const d=await jget('/api/people'); PAL_PEOPLE=d.people||[]; }
 catch(e){ PAL_PEOPLE=[]; }
 return PAL_PEOPLE;
}

// Trips, for the same reason and by the same route as people above. TRIPS is otherwise
// only fetched when the Trips view opens, so the palette's '#' grammar had nothing to
// match against from anywhere else in the app.
async function palLoadTrips(){
 if(TRIPS)return TRIPS;
 try{ TRIPS=await jget('/api/trips'); }
 catch(e){ TRIPS=[]; }
 return TRIPS;
}

function palSources(){
 const out=[
  {kind:'view',name:'Overview',hint:'/',go:()=>{closeOverlays(null);showOverview();}},
  {kind:'view',name:'Today',hint:'/today',go:()=>{closeOverlays(null);openToday();}},
  {kind:'view',name:'Calendar',hint:'/calendar',go:()=>showCalendar()},
  {kind:'view',name:'Trips',hint:'/trips',go:()=>showTrips()},
  {kind:'view',name:'Map',hint:'/map',go:()=>showMap()},
  {kind:'view',name:'People',hint:'/people',go:()=>showPeople()},
  {kind:'view',name:'Search',hint:'/search',go:()=>{if(typeof showSearch==='function')showSearch();}},
  {kind:'view',name:'Cutting Room',hint:'/cutting-room',go:()=>showCuttingRoom()},
  {kind:'view',name:'Settings',hint:'/settings',go:()=>showSettings()},
  {kind:'view',name:'Keyboard',hint:'?',go:()=>showKeys()},
 ];
 if(window.LOCAL_FULLRES){
  out.push({kind:'view',name:'Vault',hint:'/vault',go:()=>showVault()});
  out.push({kind:'view',name:'Closed Set',hint:'/nsfw',go:()=>showNsfw()});
  out.push({kind:'view',name:'Setup',hint:'darkroom',go:()=>{location.href='/setup';}});
 }
 for(const p of (PAL_PEOPLE||[])){
  const n=p.name; if(!n)continue;
  out.push({kind:'person',name:n,hint:(p.known_faces||0)+' faces',
            match:'@'+n,
            go:()=>{showPeople().then(()=>{ if(typeof openPerson==='function')openPerson(p.person_id); });}});
 }
 // '#' filters the palette to kind 'trip' or 'place'. Neither kind was ever pushed
 // here, so the grammar the palette advertises in its own hint row could not match
 // anything: typing "#baird" against a real 258-frame trip returned zero rows. The
 // registry held 310 entries -- 11 view, 20 person, 25 year, 254 month, 0 trip.
 (TRIPS||[]).forEach((t,i)=>{
  const n=t.title||t.city; if(!n)return;
  out.push({kind:'trip',name:n,hint:(t.frames||0).toLocaleString()+' frames · '+(t.days||0)+'d',
            match:'#'+n,
            go:()=>{showTrips().then(()=>openTripSheet(i));}});
 });
 if(ov&&ov.years)for(const y of ov.years){
  out.push({kind:'year',name:String(y.year),hint:(y.total||0)+' frames',
            go:()=>{closeOverlays(null);showOverview();}});
  if(y.months)for(const m of y.months){
   out.push({kind:'month',name:(m.label||(y.year+'-'+m.m)),hint:(m.total||0)+' frames',
             go:()=>openMonth(y.year,m.m,m.undated)});
  }
 }
 return out;
}

// Subsequence match, the usual palette behaviour: "cut" finds Cutting Room, "26j" finds
// Jan 2026. Score prefers earlier and tighter matches so exact prefixes float up.
function palScore(q,text){
 if(!q)return 0;
 const t=text.toLowerCase();let ti=0,first=-1,gaps=0;
 for(const ch of q){
  const i=t.indexOf(ch,ti);
  if(i<0)return -1;
  if(first<0)first=i;else gaps+=i-ti;
  ti=i+1;
 }
 return 1000-first*4-gaps;
}
function palRender(){
 const raw=($('#palq').value||'').trim();
 const q=raw.toLowerCase();
 // 9.5's grammar, taught by narrowing rather than by documentation: a leading @ keeps
 // only people, # only trips/places. Bare text stays broad and offers the semantic
 // engine as an explicit row -- it is never run per keystroke, because the first
 // /api/search hit loads a ~2.7GB text model and a palette that stalls is not a palette.
 let pool=PAL, body=q;
 if(q.startsWith('@')){ pool=PAL.filter(r=>r.kind==='person'); body=q.slice(1); }
 else if(q.startsWith('#')){ pool=PAL.filter(r=>r.kind==='trip'||r.kind==='place'); body=q.slice(1); }
 const scored=pool.map(r=>({r,s:palScore(body,r.name+' '+(r.hint||''))}))
                 .filter(x=>x.s>=0).sort((a,b)=>b.s-a.s).slice(0,40);
 if(window.SEARCH_ENABLED && body.length>=2 && !q.startsWith('@') && !q.startsWith('#')){
  scored.push({s:-1,r:{kind:'frames',name:raw,hint:'semantic search',
    go:()=>{showSearch().then(()=>{const el=$('#searchq'); if(el){el.value=raw; runSearch();}});}}});
 }
 palSel=Math.min(palSel,Math.max(0,scored.length-1));
 const box=$('#palresults');
 if(!scored.length){box.innerHTML='<div class=palempty>Nothing on the table for that.</div>';return;}
 box.innerHTML=scored.map((x,i)=>
   `<div class="palrow${i===palSel?' on':''}" data-i="${i}" role=option>`+
   `<span class=palkind>${x.r.kind}</span><span class=palname>${escHtml(x.r.name)}</span>`+
   `<span class=palhint>${escHtml(x.r.hint||'')}</span></div>`).join('');
 if(!q){
  box.insertAdjacentHTML('afterbegin','<div class=palhints>'+
   '<span><b>@</b>name</span><span><b>#</b>trip</span><span>a year</span>'+
   (window.SEARCH_ENABLED?'<span>or just describe a photo</span>':'')+'</div>');
 }
 box._rows=scored.map(x=>x.r);
 box.querySelectorAll('.palrow').forEach(el=>{
  el.onclick=()=>palGo(+el.dataset.i);
  el.onmouseenter=()=>{palSel=+el.dataset.i;palPaint();};
 });
}
function palPaint(){
 $('#palresults').querySelectorAll('.palrow').forEach((el,i)=>el.classList.toggle('on',i===palSel));
}
function palGo(i){
 const box=$('#palresults');const r=(box._rows||[])[i==null?palSel:i];
 palClose();
 if(r&&typeof r.go==='function')r.go();
}
function palOpen(){
 PAL=palSources();palSel=0;
 Promise.all([palLoadPeople(),palLoadTrips()]).then(()=>{PAL=palSources();palRender();});
 closeOverlays('paletteview');
 $('#paletteview').classList.add('on');
 const q=$('#palq');q.value='';
 // bound here rather than at load: #palq exists in the shell, but binding on open keeps
 // the handler with the thing that owns the lifecycle. Without it the list rendered once
 // and never responded to typing -- caught by the end-to-end test, not by the suite.
 q.oninput=()=>{palSel=0;palRender();};
 palRender();q.focus();
}
function palClose(){$('#paletteview').classList.remove('on');}
document.addEventListener('keydown',e=>{
 if(!$('#paletteview')||!$('#paletteview').classList.contains('on'))return;
 if(e.key==='ArrowDown'){e.preventDefault();palSel++;palRender();}
 else if(e.key==='ArrowUp'){e.preventDefault();palSel=Math.max(0,palSel-1);palRender();}
 else if(e.key==='Enter'){e.preventDefault();palGo();}
 else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();palClose();}
},true);

// --- zoom to pixel (audit 8.7) --------------------------------------------
// Space toggles fit <-> 100%, centred on where you clicked (or the middle for a keyboard
// toggle). The full-resolution source is fetched only on the way IN, so browsing never
// pays for it, and only for owners -- /api/full is LAN-gated and 403s for guests, who
// keep the preview rather than get a broken image.
function focusZoomed(){const st=$('#fstage');return !!(st&&st.classList.contains('zoomed'));}
function exitZoom(){
 const st=$('#fstage');if(!st)return;
 st.classList.remove('zoomed');
 const h=st.querySelector('.zoomhint');if(h)h.remove();
 const im=$('#fimg');if(im&&im.dataset.fitsrc){im.src=im.dataset.fitsrc;delete im.dataset.fitsrc;}
 st.onpointerdown=st.onpointermove=st.onpointerup=st.onpointercancel=null;
}
function toggleZoom(cx,cy){
 const st=$('#fstage'),im=$('#fimg');
 if(!st||!im)return;
 if(focusZoomed()){exitZoom();return;}
 const it=seq[fidx];
 if(!it||it.is_video){toast('zoom is for stills');return;}
 im.dataset.fitsrc=im.src;                       // exact restore, not a re-render
 if(window.LOCAL_FULLRES)im.src='/api/full/'+it.id;
 st.classList.add('zoomed');
 if(!st.querySelector('.zoomhint')){
  const h=document.createElement('div');h.className='zoomhint';
  h.textContent=window.LOCAL_FULLRES?'100% · space to fit':'preview · space to fit';
  st.appendChild(h);
 }
 const centre=()=>{
  const r=st.getBoundingClientRect();
  const fx=(cx==null?0.5:Math.min(1,Math.max(0,(cx-r.left)/r.width)));
  const fy=(cy==null?0.5:Math.min(1,Math.max(0,(cy-r.top)/r.height)));
  st.scrollLeft=fx*(st.scrollWidth-st.clientWidth);
  st.scrollTop =fy*(st.scrollHeight-st.clientHeight);
 };
 if(im.complete)centre();else im.addEventListener('load',centre,{once:true});
 let dragging=false,sx=0,sy=0,sl=0,stp=0;
 st.onpointerdown=e=>{dragging=true;sx=e.clientX;sy=e.clientY;sl=st.scrollLeft;stp=st.scrollTop;
  try{st.setPointerCapture(e.pointerId);}catch(_){}};
 st.onpointermove=e=>{if(!dragging)return;st.scrollLeft=sl-(e.clientX-sx);st.scrollTop=stp-(e.clientY-sy);};
 st.onpointerup=st.onpointercancel=()=>{dragging=false;};
}

// --- density (audit 8.4) --------------------------------------------------
// Three steps on [ and ]. Persisted in localStorage rather than loupe-settings as 8.4
// suggests: it is a per-device view preference, and a settings write would need the W23
// write token, which would make changing zoom level fail on a device that has not been
// enrolled. Same reasoning as where the token itself lives.
const DENSITIES=['sheet','standard','study'];
function currentDensity(){
 const d=localStorage.getItem('loupe_density');
 return DENSITIES.includes(d)?d:'standard';
}
function applyDensity(d){
 if(d==='standard')document.documentElement.removeAttribute('data-density');
 else document.documentElement.setAttribute('data-density',d);
 localStorage.setItem('loupe_density',d);
}
function stepDensity(dir){
 const i=DENSITIES.indexOf(currentDensity());
 const n=Math.min(DENSITIES.length-1,Math.max(0,i+dir));
 if(n===i){toast(dir<0?'tightest already':'loosest already');return;}
 applyDensity(DENSITIES[n]);
 toast('density: '+DENSITIES[n]);
}
applyDensity(currentDensity());

// --- routing (audit 8.5) --------------------------------------------------
// pushState, so Back steps back through spaces instead of leaving the app. The guard is
// the whole trick: on a popstate the browser has ALREADY moved location.pathname to the
// target, so when the restored view calls syncUrl with that same path it is a no-op and
// no duplicate entry is pushed. That avoids the usual suppress-flag, which would not
// survive the bootstrap's async `showOverview().then(...)` chain anyway.
function syncUrl(path){
 if(location.pathname===path)return;
 if(history.pushState)try{history.pushState(null,'',path);}catch(e){}
}
// One dispatch table for both the first paint and every Back/Forward, so a route can
// never work on load and not on Back.
function routeTo(path){
 if(path==='/nsfw'){ if(window.LOCAL_FULLRES) showOverview().then(()=>showNsfw()); else location.replace('/'); }
 else if(path==='/vault'){showOverview().then(()=>showVault());}
 else if(path==='/settings'){showOverview().then(()=>showSettings());}
 else if(path==='/trips'||path==='/places'){showOverview().then(()=>showTrips());}
 else if(path==='/map'){showOverview().then(()=>showMap());}
 else if(path==='/cutting-room'){showOverview().then(()=>showCuttingRoom());}
 else if(path==='/people'){showOverview().then(()=>showPeople());}
 else if(path==='/today'){
  const qs=new URLSearchParams(location.search);
  const m=parseInt(qs.get('m'),10),d=parseInt(qs.get('d'),10);
  showOverview().then(()=>{closeOverlays(null);openToday(m||undefined,d||undefined);});
 }
 else if(path==='/calendar'){showOverview().then(()=>showCalendar());}
 else{closeOverlays(null);showOverview();}
}
window.addEventListener('popstate',()=>routeTo(location.pathname));
function closeOverlays(except){OVL.forEach(id=>{if(id!==except){const e=$('#'+id);if(e)e.classList.remove('on');}});}
let decState=null;   // cut/keep review view
let filt={status:'all'};
let sortMode='time';   // 'time' | 'worst' (aesthetic ascending — score orders, never nominates)
let MODE='lib';                              // 'lib' (whole library) | 'cand' (rule-flagged subset)
let cfilt={rule:'all',fp:false,iso:false};   // Candidates-view filters
let thumbTimer=null;
const RULECOL={B4:'#BA7517',B3:'#b794f4',B2:'#6fb8ff',B5:'#d98c5f',A2a:'#e0667a',A2b:'#e0b14a',SD:'#7fb0b8',PB:'#8fbf7a'};
const RULELBL={B4:'blurry',B3:'burst',B2:'screenshot',B5:'junk',A2a:'<1s',A2b:'1–3s',SD:'screen/doc',PB:'place-burst'};
const PRI=['B4','B3','B2','A2b','A2a','B5','SD','PB'];
const SPIN='<svg viewBox="0 0 96 96" width="40" height="40" role="img" aria-label="loading"><g transform="translate(-67.5,-41.25) scale(1.75)"><line x1="51" y1="46" x2="51" y2="56" stroke="#BA7517" stroke-width="5"/><line x1="81" y1="46" x2="81" y2="56" stroke="#BA7517" stroke-width="5"/><ellipse cx="66" cy="56" rx="15" ry="7" fill="none" stroke="#BA7517" stroke-width="5"/><ellipse cx="66" cy="46" rx="15" ry="7" fill="#14110C"/><ellipse cx="66" cy="46" rx="15" ry="7" fill="#BA7517" fill-opacity="0.14"/><ellipse cx="66" cy="46" rx="15" ry="7" fill="none" stroke="#BA7517" stroke-width="5"/><path d="M55 44 Q60 40 68 41.5" fill="none" stroke="#FBF6EA" stroke-width="2.2" stroke-opacity="0.72" stroke-linecap="round"/><ellipse cx="66" cy="46" rx="15" ry="7" fill="none" stroke="#E2902A" stroke-width="5.5" stroke-linecap="round" stroke-dasharray="24 47.4" stroke-dashoffset="0"><animate attributeName="stroke-dashoffset" from="0" to="-71.4" dur="1.3s" repeatCount="indefinite"/></ellipse></g></svg>';
function phspin(){const d=document.createElement('div');d.className='ph';d.innerHTML=SPIN;return d;}
function primaryRule(it){return (it.rules&&(PRI.find(r=>it.rules.includes(r))||it.rules[0]))||'';}
function mq(hasq){return MODE!=='cand'?'':(hasq?'&mode=cand':'?mode=cand');}
function updateModeBtn(){
 /* #modetog is now the Cutting Room entry (a destination, not a filter toggle) — leave its
    label alone; only the candidate-export button still keys off MODE. */
 const e=$('#expcand');if(e)e.style.display=MODE==='cand'?'':'none';}
function toggleMode(){
 MODE=MODE==='cand'?'lib':'cand';updateModeBtn();cfilt={rule:'all',fp:false,iso:false};closeFocus();
 if(day){const dd=day.d,my=month.y,mm=month.m,mu=month.undated;openMonth(my,mm,mu).then(()=>openDay(dd));}
 else if(month){reopenMonth();}
 else showOverview();
}
// 9.4's door. The export prepares the app's only irreversible INTENT, so it gets a
// gate: the live count, what each protection held back, and the count typed back before
// the button arms. The preflight shares its computation with the export, so the number
// shown is the number written.
async function exportCandidates(){
 let pre;
 try{ pre=await jget('/api/export-candidates/preflight'); }
 catch(e){ toast('could not check the manifest'); return; }
 const held=pre.held_back||{};
 const heldRows=Object.keys(held).map(k=>
   `<li class="${held[k]?'held':''}"><span>${escHtml(k)}</span><b>${held[k]}</b></li>`).join('');
 $('#gatebody').innerHTML =
  `<div>You are preparing <span class=gcount>${pre.count.toLocaleString()}</span> frames `+
  `(${pre.gb} GB) for deletion.</div>`+
  `<div style="margin-top:8px">Nothing is deleted here. This writes a manifest; `+
  `acting on it stays a human step outside Loupe.</div>`+
  `<ul><li><span>cut in total</span><b>${pre.cut_total.toLocaleString()}</b></li>`+
  `<li><span>not rule-flagged</span><b>${pre.not_rule_flagged.toLocaleString()}</b></li>`+
  heldRows+
  `<li><span>in the manifest</span><b>${pre.count.toLocaleString()}</b></li></ul>`;
 const inp=$('#gatetype'), go=$('#gatego');
 inp.value=''; go.disabled=true; go.classList.remove('armed');
 $('#gatetypelabel').textContent=`Type ${pre.count} to unlock`;
 inp.oninput=()=>{
  const ok=inp.value.trim()===String(pre.count);
  go.disabled=!ok; go.classList.toggle('armed',ok);
 };
 $('#gatecancel').onclick=()=>closeExportGate();
 go.onclick=async()=>{
  if(go.disabled)return;
  closeExportGate();
  const r=await jget('/api/export-candidates');
  toast(`wrote manifest — ${r.count} frames (${r.gb} GB) → ${r.path.split('/').pop()}`);
 };
 closeOverlays('exportgate');
 $('#exportgate').classList.add('on');
 inp.focus();
}
function closeExportGate(){$('#exportgate').classList.remove('on');}
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function toast(m){const t=$('#toast');t.textContent=m;t.style.display='block';clearTimeout(t._t);t._t=setTimeout(()=>t.style.display='none',3000);}
async function jget(u){return (await fetch(u)).json();}
jget('/api/edits').then(list => { window._EDITED_IDS = new Set(list); }).catch(()=>{});
window._focusOriginal=false;
function isEditedAsset(it){return !!(it&&window._EDITED_IDS&&(window._EDITED_IDS.has(it.id)||window._EDITED_IDS.has(String(it.id))||window._EDITED_IDS.has(Number(it.id))));}
function focusPreviewSrc(it){return '/api/preview/'+it.id+(window._focusOriginal?'?original=1':'');}

// ---------- living contact-sheet card backdrops ----------
const live=(function(){
 const REDUCED=matchMedia('(prefers-reduced-motion:reduce)').matches;
 const CYCLE=7000;
 let slots=6; const q=[];                              // cap concurrent thumb loads
 function pump(){ while(slots>0 && q.length){ slots--; q.shift()(); } }
 function load(src){ return new Promise(res=>{ q.push(()=>{ const im=new Image();
   const fin=ok=>{ slots++; pump(); res(ok); };
   im.onload=()=>fin(true); im.onerror=()=>fin(false); im.src=src; }); pump(); }); }
 const cards=new Map(); let io=null;
 function stop(){ if(io){io.disconnect();io=null;} cards.forEach(s=>{if(s.timer)clearTimeout(s.timer);}); cards.clear(); }
 async function activate(el,st){
   if(st.active) return; st.active=true;
   if(!st.summed){const ms=el.parentElement&&el.parentElement.querySelector('.mcap');if(ms&&ms.dataset.sumqs){st.summed=true;sum.render(ms,ms.dataset.sumqs,'overview');}}
   if(st.ids===null){ try{ const r=await jget('/api/sample?'+el.dataset.period); st.ids=r.ids||[]; st.portrait=r.portrait||[]; }
                      catch(_){ st.ids=[]; st.portrait=[]; } if(!st.active) return; }
   if(!st.ids.length) return;
   st.idx=0; await show(el,st,st.ids[0],st.portrait[0]);
   if(REDUCED || st.ids.length<2) return;
   st.timer=setTimeout(()=>tick(el,st), CYCLE+Math.random()*CYCLE);   // random per-card phase
 }
 function deactivate(el,st){
   st.active=false; if(st.timer){clearTimeout(st.timer);st.timer=null;}
   el.querySelectorAll('img.bg').forEach(b=>{b.classList.remove('show');b.removeAttribute('src');});  // free images
 }
 async function tick(el,st){
   if(!st.active) return;
   st.idx=(st.idx+1)%st.ids.length; await show(el,st,st.ids[st.idx],st.portrait&&st.portrait[st.idx]);
   if(!st.active) return; st.timer=setTimeout(()=>tick(el,st), CYCLE);
 }
 async function show(el,st,id,port){
   const src='/thumb/'+id+'.jpg?live=1';
   const ok=await load(src); if(!ok || !st.active) return;            // preload before fade
   const bgs=el.querySelectorAll('img.bg'); if(bgs.length<2) return;
   const cur=st.cur||0, nxt=cur^1;
   bgs[nxt].classList.toggle('port',!!port);                          // size per THIS photo's orientation (pillarbox portraits)
   bgs[nxt].src=src; bgs[nxt].classList.add('show'); bgs[cur].classList.remove('show'); st.cur=nxt;
 }
 function attach(){
   stop();
   io=new IntersectionObserver(es=>es.forEach(e=>{ const st=cards.get(e.target); if(!st)return;
     if(e.isIntersecting) activate(e.target,st); else deactivate(e.target,st); }), {rootMargin:'250px'});
   document.querySelectorAll('[data-period]').forEach(el=>{
     cards.set(el,{ids:null,idx:0,cur:0,timer:null,active:false}); io.observe(el); });
 }
 return {attach, stop};
})();

// ---------- AI-polished period summaries (Layer-1 instant, async venue+prose upgrade) ----------
const sum=(function(){
 const cache=new Map(); let slots=3; const q=[];
 function pump(){ while(slots>0&&q.length){ slots--; q.shift()(); } }
 function queued(fn){ return new Promise(res=>{ q.push(async()=>{ try{res(await fn());}catch(_){res(null);}finally{slots++;pump();} }); pump(); }); }
 function ord(n){const s=['th','st','nd','rd'],v=n%100;return n+(s[(v-20)%10]||s[v]||s[0]);}
 function sp(a){return a?String(a).split(',')[0]:'';}
 function factsHtml(f,venues){
  if(!f) return '';
  const named=(venues||[]).map(v=>v.venue).filter(Boolean);
  const places=(named.length?named:(f.areas||[])).slice(0,2).map(sp);
  const parts=[`${f.frames} frames`];
  if(f.clips) parts.push(`${f.clips} clips`);
  if(places.length) parts.push(places.map(esc).join(' · '));
  if(f.tod) parts.push('mostly '+esc(f.tod));
  let so=null;
  if(f.spike&&f.busiest_day) so=`busiest the ${ord(f.busiest_day.day)}`;
  else if(named.length) so=`${named.length} venue${named.length>1?'s':''}`;
  else if(f.trips&&f.trips.length) so=`trip · ${esc(sp(f.trips[0]))}`;
  return (so?`<span class=amber>${so}</span> · `:'')+parts.join(' · ');
 }
 function paint(el,r,level){
  const f=r&&r.facts; if(!f){el.innerHTML='';return;}
  if(level==='overview'){ el.innerHTML=(r.prose?`<div class="sum-prose ov">${esc(r.prose)}</div>`:'')+`<div class=msum>${factsHtml(f,r.venues)}</div>`; return; }
  el.innerHTML=(r.prose?`<div class=sum-prose>${esc(r.prose)}</div>`:'')+`<div class=sum-facts>${factsHtml(f,r.venues)}</div>`;
 }
 async function render(el,qs,level){
  if(!el) return; el.dataset.sumqs=qs;
  const c=cache.get(qs); if(c){ paint(el,c,level); if(c.ready) return; }
  let r=c; if(!r){ r=await jget('/api/summary?'+qs); cache.set(qs,r); paint(el,r,level); }
  if(r&&r.facts&&!r.ready){                         // async upgrade (capped), then swap in
   const up=await queued(()=>jget('/api/summary?'+qs+'&gen=1'));
   if(up&&up.facts){ cache.set(qs,up); if(el.dataset.sumqs===qs) paint(el,up,level); }
  }
 }
 async function proseOnly(el,qs){            // grid tiles want ONLY the prose (their meta strip is deterministic)
  if(!el)return;
  let r=cache.get(qs);
  if(!r){ r=await queued(()=>jget('/api/summary?'+qs)); if(r)cache.set(qs,r); }
  if(r&&r.prose)el.textContent=r.prose;
  if(r&&r.facts&&!r.ready){ const up=await queued(()=>jget('/api/summary?'+qs+'&gen=1'));
   if(up){cache.set(qs,up); if(up.prose&&el.dataset.sumqs===qs)el.textContent=up.prose;} }
 }
 return {render,cache,proseOnly};
})();

// ---------- overview (discovery carousel + per-year scrubbable carousels) ----------
let carouselMonths=null;     // seeded once per page-load, stable across return-to-overview
function applyTileMode(m){
 const mosaic=m==='mosaic';
 document.body.classList.toggle('t-hero',!mosaic);
 document.body.classList.toggle('t-mosaic',mosaic);
 document.querySelectorAll('.ttoggle a').forEach(a=>a.classList.toggle('on',a.dataset.t===m));
 document.querySelectorAll('.ytile').forEach(t=>t._book&&t._book.kick());   // start/stop page-turns per mode
}
// ---- photobook-spread page-turn controller (mosaic mode) ----
const PAGE_HOLD=5500, FLIP=700;                 // tunable: hold per spread / page-turn duration
const tnum=v=>{const n=parseInt(v,10);return isNaN(n)?0:n;};
function makeBook(el){
 const spread=el.querySelector('.spread');
 const pgL=el.querySelector('.spread .pgL'), Limg=pgL&&pgL.querySelector('img');
 const Uimg=el.querySelector('.spread .under img');
 const leaf=el.querySelector('.spread .leaf');
 const Fimg=el.querySelector('.spread .leaf .front img'), Bimg=el.querySelector('.spread .leaf .back img');
 const REDUCED=matchMedia('(prefers-reduced-motion:reduce)').matches;
 const seed=((tnum(el.dataset.y)*12+tnum(el.dataset.m))*2654435761)>>>0;   // stable per-tile phase seed
 let pairs=null,idx=0,t=null,alive=false,paused=false,running=false,flipping=false,started=false;
 const TH=id=>'/thumb/'+id+'.jpg?live=1';
 const mosaic=()=>document.body.classList.contains('t-mosaic');
 function preload(p){if(p)[p[0],p[1]].forEach(id=>{if(id){const im=new Image();im.src=TH(id);}});}
 function leafFlat(){if(leaf){leaf.style.transition='none';leaf.style.transform='rotateY(0deg)';}}
 function show(p){
  if(spread)spread.classList.toggle('single',!p[1]);
  if(pgL){pgL.style.display=p[0]?'':'none'; if(p[0]&&Limg)Limg.src=TH(p[0]);}   // static left page
  if(p[1]&&Fimg)Fimg.src=TH(p[1]);                                              // leaf FRONT = current right
  flipping=false; leafFlat();
 }
 function tick(){                                  // turn the RIGHT leaf LEFT over the left page -> next pair
  if(!alive||paused||flipping||!mosaic()){running=false;return;}
  const nx=(idx+1)%pairs.length, np=pairs[nx];
  if(np[0]==null&&np[1]==null){running=false;return;}
  running=true; flipping=true;
  if(Bimg&&np[0]!=null)Bimg.src=TH(np[0]);                       // leaf BACK = next-left (shows when face-down on the left)
  if(Uimg)Uimg.src=TH(np[1]!=null?np[1]:np[0]);                  // underlay = next-right (revealed as the leaf lifts)
  leaf.style.transition='transform '+FLIP+'ms cubic-bezier(.42,.04,.34,1)';
  leaf.style.transform='rotateY(-180deg)';                       // hinge at spine, sweep LEFT, land on the left page
  t=setTimeout(()=>{                                             // turn done -> advance, snap leaf back seamlessly
   idx=nx;
   if(Limg&&np[0]!=null)Limg.src=TH(np[0]);                      // static left now = next-left (matches leaf back)
   if(Fimg)Fimg.src=TH(np[1]!=null?np[1]:np[0]);                // leaf front now = next-right (matches underlay)
   leafFlat(); void leaf.offsetWidth;
   flipping=false; running=false; preload(pairs[(idx+1)%pairs.length]); maybeRun();
  },FLIP);
 }
 function maybeRun(){
  if(REDUCED||!alive||paused||running||flipping||!pairs||pairs.length<2||!mosaic())return;
  running=true;
  const delay=started?PAGE_HOLD+(seed%800):1000+(seed%PAGE_HOLD);  // seeded first-phase -> no two tiles in unison
  started=true; t=setTimeout(tick,delay);
 }
 return {
  feed(ids){pairs=[];for(let i=0;i<ids.length;i+=2)pairs.push([ids[i],ids[i+1]||null]);
   if(!pairs.length)pairs=[[null,null]];idx=0;started=false;show(pairs[0]);preload(pairs[1]);maybeRun();},
  start(){alive=true;maybeRun();},
  stop(){alive=false;if(t){clearTimeout(t);t=null;}running=false;flipping=false;leafFlat();},
  pause(){paused=true;if(!flipping&&t){clearTimeout(t);t=null;running=false;}},   // let an in-flight turn finish
  resume(){paused=false;maybeRun();},
  kick(){if(t){clearTimeout(t);t=null;}running=false;flipping=false;leafFlat();maybeRun();}
 };
}
function setNav(id){  // highlight the active view's nav button (its glyph + label go amber via currentColor)
 ['overviewtog','todaytog','calendartog','placestog','maptog','peopletog','vaulttog','nsfwtog','searchtog','modetog','settingstog'].forEach(x=>{const b=$('#'+x);if(b)b.classList.toggle('navon',x===id);});
 if(id!=='nsfwtog'){const nv=$('#nsfwview');if(nv)nv.classList.remove('on');}   // central hide when navigating to any other view
 if(window.syncTabbar)syncTabbar();
}
async function showOverview(){
 view='overview';stopThumbPoll();closeFocus();decState=null;setNav('overviewtog');
 placesReview=null;_fromMap=false;closeOverlays(null);   // base surface: clear every overlay
 ov=await jget('/api/overview'+mq(false));updateModeBtn();
 month=null;day=null;renderCrumb();renderStrip(ov.summary);
 const allMonths=ov.years.flatMap(y=>y.months);          // months that actually have photos
 // amber safelight: the single most-recent in-progress month
 let cur=null;
 for(const c of allMonths) if(c.state==='in-progress' && (!cur||c.y>cur.y||(c.y===cur.y&&c.m>cur.m))) cur=c;
 const curKey=cur?cur.y+'-'+cur.m:'';
 let h='';
 // 1) discovery carousel — the only auto-moving thing; seeded once, doubled for seamless loop
 if(carouselMonths===null){
  const pool=allMonths.slice();
  for(let i=pool.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[pool[i],pool[j]]=[pool[j],pool[i]];}
  carouselMonths=pool.slice(0,Math.min(16,pool.length));
 }
 if(carouselMonths.length){
  const strip=carouselMonths.map(ctile).join('');
  h+=`<div class=carousel><div class=marquee>${strip}${strip}</div></div>`;
 }
 // per-year scrubbable carousels (newest first; only populated months, calendar order)
 for(const y of ov.years){
  const months=y.months.slice().sort((a,b)=>a.m-b.m);
  h+=`<div class=year>${y.year}<span class=yp>${(y.total||0).toLocaleString()} frames · ${y.pct}% reviewed</span></div>`;
  h+=yrow(months,curKey);
 }
 if(ov.undated)h+=`<div class=year>Undated</div>`+yrow([ov.undated],curKey);
 $('#app').innerHTML=h;
 applyTileMode('mosaic');     // photobook is the sole permanent view (toggle removed)
 document.querySelectorAll('.ytile[data-y],.ctile[data-y]').forEach(el=>el.onclick=()=>openMonth(el.dataset.y,el.dataset.m,el.dataset.u==='1'));
 document.querySelectorAll('.yrow').forEach(wireYear);
 gridObserve();     // lazy-load each tile's hero/mosaic frames + prose as it scrolls in
 live.attach();     // cycling backdrops for the discovery carousel tiles (data-period)
}
function yrow(months,curKey){
 const tiles=months.map(c=>ytile(c,curKey)).join('');
 return `<div class=yrow>
   <button class="yarrow yprev" tabindex=-1 aria-label="previous months">‹</button>
   <div class=yscroll tabindex=0>${tiles}</div>
   <button class="yarrow ynext" tabindex=-1 aria-label="next months">›</button></div>`;
}
function ytile(c,curKey){
 const period=c.undated?'undated=1':`y=${c.y}&m=${c.m}`;
 const isCur=(c.y+'-'+c.m)===curKey;
 const name=c.undated?'Undated':(MON[c.m]||c.label||'');
 const ab=c.undated?'—':(MON[c.m]||'').toUpperCase();
 const meta=`${ab} · ${(c.total||0).toLocaleString()} · <span class=pct>${Math.round(c.pct||0)}%</span>`;
 return `<div class="ytile ${c.state}${isCur?' cur':''}" data-y="${c.y==null?'':c.y}" data-m="${c.m==null?'':c.m}" data-u="${c.undated?1:0}" data-sample="${period}">
   <div class=hero><img alt="" loading=lazy></div>
   <div class=spread><div class=pgL><img alt=""></div><div class=under><img alt=""></div><div class=leaf><div class="face front"><img alt=""></div><div class="face back"><img alt=""></div></div><div class=spine></div></div>
   <div class=tscrim></div>
   <div class=tbody>
    <div class=mn>${esc(name)}</div>
    <div class=mprose data-sumqs="${c.undated?'':`level=month&y=${c.y}&m=${c.m}`}"></div>
    <div class=mmeta>${meta}</div>
   </div></div>`;
}
function ctile(c){
 const period=c.undated?'undated=1':`y=${c.y}&m=${c.m}`;
 const lbl=c.undated?'UNDATED':`${(MON[c.m]||'').toUpperCase()} '${String(c.y).slice(2)}`;
 const n=((tnum(c.y)*12+tnum(c.m))%36)+1;     // decorative, stable frame number tick
 return `<div class=ctile data-y="${c.y==null?'':c.y}" data-m="${c.m==null?'':c.m}" data-u="${c.undated?1:0}" data-period="${period}">
   <div class=cframe><img class=bg alt=""><img class=bg alt=""></div>
   <div class=crebate><span class=cep>${esc(lbl)}</span><span class=cnum>→ ${n}A</span><span class=cstock>LOUPE 400</span></div></div>`;
}
function wireYear(row){
 const sc=row.querySelector('.yscroll'), prev=row.querySelector('.yprev'), next=row.querySelector('.ynext');
 const beh=matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth';
 const tw=()=>{const t=sc.querySelector('.ytile');return t?t.offsetWidth+12:sc.clientWidth;};
 const step=()=>{const w=tw();return Math.max(w,(Math.floor(sc.clientWidth/w)-1)*w)||w;};  // ~one page, keep a tile of context
 const atEnd=()=>sc.scrollLeft+sc.clientWidth>=sc.scrollWidth-4, atStart=()=>sc.scrollLeft<=4;
 function go(dir){
  if(dir>0) atEnd()?sc.scrollTo({left:0,behavior:beh}):sc.scrollBy({left:step(),behavior:beh});      // wrap Dec->Jan
  else      atStart()?sc.scrollTo({left:sc.scrollWidth,behavior:beh}):sc.scrollBy({left:-step(),behavior:beh}); // wrap Jan->Dec
 }
 prev.onclick=()=>go(-1); next.onclick=()=>go(1);
 sc.addEventListener('keydown',e=>{if(e.key==='ArrowRight'){e.preventDefault();go(1);}else if(e.key==='ArrowLeft'){e.preventDefault();go(-1);}});
 let down=false,sx=0,sl=0,moved=false;                                   // pointer drag (mouse); touch/trackpad use native scroll
 sc.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse'){down=true;sx=e.clientX;sl=sc.scrollLeft;moved=false;}});
 sc.addEventListener('pointermove',e=>{if(!down)return;const dx=e.clientX-sx;
   if(!moved&&Math.abs(dx)>4){moved=true;sc.setPointerCapture(e.pointerId);}   // capture only once dragging -> a plain click still reaches the tile
   if(moved)sc.scrollLeft=sl-dx;});
 sc.addEventListener('pointerup',e=>{if(!down)return;down=false;
   if(moved){if(sc.hasPointerCapture(e.pointerId))sc.releasePointerCapture(e.pointerId);
     const cap=ev=>{ev.stopPropagation();ev.preventDefault();sc.removeEventListener('click',cap,true);};sc.addEventListener('click',cap,true);}});
 const upd=()=>{const ovf=sc.scrollWidth>sc.clientWidth+4;prev.classList.toggle('show',ovf);next.classList.toggle('show',ovf);};
 upd(); window.addEventListener('resize',upd); row._upd=upd;
 // pause this row's page-turns while it's being scrubbed; resume a beat after it settles
 let rt=null;
 const pauseRow=()=>{clearTimeout(rt);row.querySelectorAll('.ytile').forEach(t=>t._book&&t._book.pause());};
 const resumeSoon=()=>{clearTimeout(rt);rt=setTimeout(()=>row.querySelectorAll('.ytile').forEach(t=>t._book&&t._book.resume()),700);};
 sc.addEventListener('scroll',()=>{pauseRow();resumeSoon();},{passive:true});
 sc.addEventListener('pointerdown',pauseRow); sc.addEventListener('pointerup',resumeSoon);
}
function gridObserve(){
 if(window._gio)window._gio.disconnect();
 const io=new IntersectionObserver(es=>es.forEach(e=>{
  const el=e.target;
  if(e.isIntersecting){
   if(!el._init){ el._init=true; el._book=makeBook(el);                     // one-time: fetch frames, prose
    const samp=el.dataset.sample;
    if(samp)jget('/api/sample?best=1&n=10&'+samp).then(r=>{const ids=(r&&r.ids)||[];
      const hi=el.querySelector('.hero img'); if(hi&&ids[0])hi.src='/thumb/'+ids[0]+'.jpg?live=1';   // B: best frame
      el._book.feed(ids);                                                   // C: pairs for the spread (lazy thumbs on flip)
    }).catch(()=>el._book.feed([]));
    const pr=el.querySelector('.mprose'); if(pr&&pr.dataset.sumqs)sum.proseOnly(pr,pr.dataset.sumqs);
   }
   el._book&&el._book.start();                                              // in-view only -> animate
  } else if(el._book){ el._book.stop(); }                                   // offscreen -> freeze
 }),{rootMargin:'200px'});
 document.querySelectorAll('.ytile[data-sample]').forEach(el=>io.observe(el));
 window._gio=io;
}
function renderStrip(s){if(!s||s.total==null){$('#strip').textContent='';return;}
 const lbl=(s.cand||MODE==='cand')?'candidates':'library';
 const dl=(st,inner)=>`<a class=declink role=button tabindex=0 title="Review all ${st==='cut'?'cut':'kept'}" onclick="showDecisions('${st}')" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();showDecisions('${st}')}">${inner}</a>`;
 $('#strip').innerHTML=`<span class=stat>${lbl} <b>${s.total}</b></span><span class=stat>reviewed <b>${s.decided}</b> (<span class=amber>${s.pct}%</span>)</span><span class=stat>${dl('cut',`cut <b class=r>${s.cut}</b>`)}</span><span class=stat>${dl('keep',`kept <b class=g>${s.keep}</b>`)}</span><span class=stat>remaining <b>${s.remaining}</b></span><span class=stat>reclaim <b>${s.gb} GB</b></span>`;}
async function showDecisions(state){
 if(state!=='cut'&&state!=='keep')state='cut';
 view='decisions';month=null;day=null;placesReview=null;
 closeOverlays(null);   // base surface (reachable from the stats-strip links while an overlay is open)
 stopThumbPoll();closeFocus();
 const r=await jget('/api/decisions?state='+state+(MODE==='cand'?'&cand=1':''));
 const items=r.items||[];
 decState={state,label:(state==='cut'?'Pending cut':'Pending keep'),count:(r.count!=null?r.count:items.length),items};
 byId={};items.forEach(it=>byId[it.id]=it);
 renderCrumb();renderDecisions();startThumbPoll();
}
function renderDecisions(){
 live.stop();
 const items=decState.items.filter(it=>it.state===decState.state);
 const n=items.length, noun=(n===1?'frame':'frames');
 let h=`<div class=dechead><span class=dectitle>${decState.state==='cut'?'Pending cut':'Pending keep'}</span><span class=deccount>· ${n} ${noun}</span></div>`;
 if(!n)h+=`<div class=decempty>Nothing is marked ${decState.state==='cut'?'cut':'keep'} right now.</div>`;
 else h+='<div class=grid>'+items.map(tile).join('')+'</div>';
 $('#app').innerHTML=h;
 document.querySelectorAll('.tile').forEach(el=>el.onclick=()=>{seq=decState.items.filter(it=>it.state===decState.state);enterFocus(seq.findIndex(it=>it.id===+el.dataset.id));});
}
// Temporal trail (month › week › day [› focus pos]) — no leading Overview node. Returns the
// joined HTML; rendered into #crumb and (primarily now) the Month/Day toolbar crumb slot.
function tempCrumb(){
 const sub=(d,t)=>`<span class=sub>${d}/${t}</span>`;
 const parts=[];
 if(month){
  const ml=`${esc(month.label)} ${sub(month.summary.decided,month.summary.total)}`;
  parts.push(day ? `<a onclick="reopenMonth()" title="Back to ${esc(month.label)}">${ml}</a>`
                 : `<span class=cur>${ml}</span>`);
 }
 if(day){
  if(day.wklabel) parts.push(`<a onclick="reopenMonth()" title="Back to ${esc(month.label)}">${esc(day.wklabel)}</a>`);
  const dl=`${esc(day.label)} ${sub(day.summary.decided,day.summary.total)}`;
  parts.push(view==='focus' ? `<a onclick="exitFocus()" title="Back to ${esc(day.label)}">${dl}</a>`
                            : `<span class=cur>${dl}</span>`);
 }
 if(view==='focus'&&seq[fidx]) parts.push(`<span class=cur>${fidx+1}/${seq.length}</span>`);
 return parts.join('<span class=sep>›</span>');
}
function renderCrumb(){
 // Contextual band: the breadcrumb shows only on the base/temporal surface. Synced here at the
 // TOP (not the end) because renderCrumb has early returns in the places/decisions branches, and
 // renderCrumb runs after the .on toggles on every transition (incl. focus enter via renderFocus
 // and focus exit), so the band never goes stale.
 const anyOvl=OVL.some(id=>{const e=$('#'+id);return e&&e.classList.contains('on');});
 const hc=document.querySelector('.hcrumb');if(hc)hc.style.display=anyOvl?'none':'';
 if(view==='places'||placesReview){
  const sheetOn=$('#tripsheet').classList.contains('on');
  const tripsCur=!placesReview&&!sheetOn;
  const parent=_fromMap
    ? (tripsCur ? `<span class=cur>🗺 Map</span>` : `<a onclick="showMap()" title="Back to Map">🗺 Map</a>`)
    : (tripsCur ? `<span class=cur>⌖ Trips</span>` : `<a onclick="showTrips()" title="Back to Trips">⌖ Trips</a>`);
  const parts=[`<a onclick="showOverview()" title="Overview">⌂ Overview</a>`, parent];
  if(placesReview&&view==='focus'){                       // in review (from a sheet or the gallery/map)
   if(SHEET&&placesReview.label===SHEET.t.title)parts.push(`<a onclick="exitFocus()" title="Back to the sheet">${esc(SHEET.t.title)}</a>`);
   else parts.push(`<span class=sub>${esc(placesReview.label)}</span>`);
   if(seq[fidx])parts.push(`<span class=cur>${fidx+1}/${seq.length}</span>`);
  } else if(sheetOn&&SHEET){                               // on the contact sheet
   parts.push(`<span class=cur>${esc(SHEET.t.title)}</span>`);
  }
  $('#crumb').innerHTML=parts.join('<span class=sep>›</span>');return;
 }
 if(decState){
  const parts=[`<a onclick="showOverview()" title="Back to overview">⌂ Overview</a>`];
  if(view==='focus'){parts.push(`<a onclick="exitFocus()" title="Back to ${esc(decState.label)}">${esc(decState.label)}</a>`);if(seq[fidx])parts.push(`<span class=cur>${fidx+1}/${seq.length}</span>`);}
  else parts.push(`<span class=cur>${esc(decState.label)}</span>`);
  $('#crumb').innerHTML=parts.join('<span class=sep>›</span>');return;
 }
 // Temporal surface: the trail now rides in the Month/Day toolbar (tempCrumb). Keep #crumb in
 // sync, but the header band stays hidden across the whole temporal surface (overview/month/day).
 $('#crumb').innerHTML=tempCrumb();
 if(hc) hc.style.display='none';
}

// ---------- month → weeks → days ----------
function monthQS(){return month.undated?'?undated=1':`?y=${month.y}&m=${month.m}`;}
async function openMonth(y,m,undated){
 const u=undated?'?undated=1':`?y=${y}&m=${m}`;
 const d=await jget('/api/month'+u+mq(true));
 month=d;day=null;view='month';stopThumbPoll();closeFocus();renderCrumb();renderMonth();
}
function reopenMonth(){openMonth(month.y,month.m,month.undated);}
function renderMonth(){
 let h=`<div class=toolbar><span class=crumb>${tempCrumb()}</span><span class=sub style="font-family:var(--mo);font-size:11px;color:var(--mut)">tap a day to start culling</span><span class=spacer></span>
  <button class=amber onclick="reopenMonth()">⟳ refresh</button>
  <button onclick="exportMonth()">⤓ export this month</button></div>`;
 month.weeks.forEach(w=>{
  const wq=month.undated?'':`level=week&y=${month.y}&m=${month.m}&wk=${w.key}`;
  h+=`<div class=wblock>
   <div class=whead>
    <div class=wlabel>${esc(w.label)}<span class=wc>${w.total} items · ${w.decided} done · ${w.pct}%</span></div>
    <div class=wsum data-sumqs="${wq}"></div>
   </div>
   ${sheet(w)}</div>`;
 });
 $('#app').innerHTML=h;
 document.querySelectorAll('.dframe').forEach(el=>el.onclick=()=>openDay(+el.dataset.d));
 live.attach();
 document.querySelectorAll('.wsum[data-sumqs]').forEach(el=>{if(el.dataset.sumqs)sum.render(el,el.dataset.sumqs,'week');});
}
// each week becomes one contact sheet (film base + sprocket perfs + a strip of day frames)
function sheet(w){
 // one amber safelight per sheet: a lone in-progress day, else the busiest day
 const prog=w.days.filter(d=>d.state==='in-progress');
 const amberDay = prog.length===1 ? prog[0].day
                : w.days.reduce((a,b)=>b.total>a.total?b:a, w.days[0]).day;
 const perf='<div class=csperf>'+Array(20).fill('<i></i>').join('')+'</div>';
 return `<div class=csheet>${perf}
  <div class=csrow>${w.days.map(d=>dframe(d,d.day===amberDay)).join('')}</div>
  ${perf}</div>`;
}
function dframe(d,amber){
 const period=month.undated?`undated=1&d=${d.day}`:`y=${month.y}&m=${month.m}&d=${d.day}`;
 const lbl=d.day?('DAY '+d.day):'UNDATED';
 return `<div class="dframe ${d.state}${amber?' amber':''}" data-d="${d.day}" data-period="${period}">
  <div class=dwin><img class=bg alt=""><img class=bg alt=""></div>
  <div class=dreb><span class=dlab>${lbl}</span><span class=dct>${d.total}</span></div>
  <div class=dnotch><i style="width:${d.pct}%"></i></div></div>`;
}

// ---------- day (working unit) ----------
let showPairs=false;   // reveal hidden Live motion clips in the day grid (off by default)
async function openDay(d){
 const u=monthQS()+`&d=${d}`+(showPairs?'&showpairs=1':'');
 const data=await jget('/api/day'+u+mq(true));
 day=data;byId={};data.items.forEach(it=>byId[it.id]=it);
 view='day';closeFocus();renderCrumb();renderDay();startThumbPoll();
}
// ---------- On This Day (the cross-year sibling of openDay) ----------
// Reuses `day` + view='day' wholesale -- every existing day-grid, focus-mode and
// keyboard/decide interaction already only reads day.items/day.summary/day.label,
// so nothing there needs to know this queue spans years instead of one month. The
// one flag (day.crossYear) exists purely so renderDay()/tempCrumb()/revpairs can
// tell the two apart for the handful of things that DO differ (date-nav strip,
// year separators in the grid, no single-month AI summary widget).
async function openToday(m,d){
 if(m==null||d==null){const t=new Date();m=t.getMonth()+1;d=t.getDate();}
 todayMD={m,d};
 stopThumbPoll();closeFocus();setNav('todaytog');closeOverlays(null);
 const u=`?m=${m}&d=${d}`+(showPairs?'&showpairs=1':'');
 const data=await jget('/api/on-this-day'+u+mq(true));
 data.crossYear=true;
 month=null;day=data;byId={};data.items.forEach(it=>byId[it.id]=it);
 view='day';closeFocus();renderCrumb();renderDay();startThumbPoll();
 syncUrl('/today');   // bare path; the exact date isn't deep-linked, matching /trips' pattern
}
function shiftToday(delta){
 const y=new Date().getFullYear();               // this year's calendar governs whether Feb 29 exists
 const dt=new Date(y,todayMD.m-1,todayMD.d);
 dt.setDate(dt.getDate()+delta);
 openToday(dt.getMonth()+1,dt.getDate());
}
function todayNavBar(){
 const y=new Date().getFullYear();
 const pad=n=>String(n).padStart(2,'0');
 return `<div class=todaynav>
  <button onclick="shiftToday(-1)" title="Previous day">‹ prev</button>
  <input type=date id=todaypick value="${y}-${pad(day.m)}-${pad(day.d)}">
  <button onclick="shiftToday(1)" title="Next day">next ›</button>
  <button class=amber onclick="openToday()" title="Jump to today">⟳ today</button>
  <span class=spacer></span></div>`;
}
// visibleItems() is already sorted newest-year-first, then by ts within a year
// (server-side) when sortMode==='time' -- so a year boundary is just wherever
// it.year changes. Only meaningful in time-sort; worst-first reorders past it.
function withYearHeaders(items){
 let out='',lastY;
 items.forEach(it=>{
  if(it.year!==lastY){out+=`<div class=yearhead>${it.year||'undated'}</div>`;lastY=it.year;}
  out+=tile(it);
 });
 return out;
}
function passFilter(it){
 if(filt.status!=='all'&&it.state!==filt.status)return false;
 if(MODE==='cand'){
  if(cfilt.rule!=='all'&&!(it.rules&&it.rules.includes(cfilt.rule)))return false;
  if(cfilt.fp&&!(it.m&&it.m.B4&&it.m.B4.fp))return false;
  if(cfilt.iso&&(it.m&&it.m.B4&&it.m.B4.burst))return false;
 }
 return true;
}
function visibleItems(){
 let v=day.items.filter(passFilter);
 if(sortMode==='worst')v=v.slice().sort((a,b)=>{       // aesthetic ascending; unscored sink to the end
  const av=a.ascore==null?2:a.ascore, bv=b.ascore==null?2:b.ascore;
  return av-bv || (a.ts||0)-(b.ts||0);});
 return v;
}
function renderDay(){
 live.stop();   // leaving the card views — drop backdrop timers/observers
 const undec=day.items.filter(it=>it.state==='undecided').length;
 const sweepLbl=day.crossYear?'✓ mark rest reviewed (keep)':'✓ mark rest of day reviewed (keep)';
 let h=day.crossYear?todayNavBar():'';
 h+=`<div class=toolbar>
  <span class=crumb>${tempCrumb()}</span>
  <select id=fstatus><option value=all>all</option><option value=undecided>undecided</option><option value=keep>keep</option><option value=cut>cut</option></select>
  <select id=fsort title="aesthetic score orders, never nominates"><option value=time>sort: time</option><option value=worst>sort: worst-first</option></select>
  <span class="chip ${showPairs?'on':''}" id=revpairs title="reveal the hidden Live Photo motion clips">◉ Live clips</span>
  <span class=tbactions><button id=revu class=amber>▶ review undecided (${undec})</button><button id=sweep class=green>${sweepLbl}</button></span></div>`;
 if(MODE==='cand'){
  const chips=['all'].concat(PRI).map(r=>`<span class="chip ${cfilt.rule===r?'on':''}" data-r="${r}">${r==='all'?'all':RULELBL[r]}</span>`).join('');
  h+=`<div class=candbar>${chips}
   <span class="chip ${cfilt.fp?'on':''}" id=cfp>fp-rescue</span>
   <span class="chip ${cfilt.iso?'on':''}" id=ciso>isolated</span>
   <span class=spacer></span>
   <button id=brescue class=brescue>↩ rescue all cut (filter)</button>
   <button id=bcut class=bcut>✕ cut all (filter)</button>
   <button id=bkeep class=bkeep>✓ keep all (filter)</button></div>`;
 }
 h+=`<div id=daysum class=daysum></div>`;
 const items=visibleItems();
 if(!items.length)h+='<div style="color:var(--mut);font-family:var(--mo);padding:20px">— nothing in this filter —</div>';
 else h+='<div class=grid>'+(day.crossYear&&sortMode==='time'?withYearHeaders(items):items.map(tile).join(''))+'</div>';
 $('#app').innerHTML=h;
 if(!day.undated&&day.y!=null&&day.d)sum.render($('#daysum'),`level=day&y=${day.y}&m=${day.m}&d=${day.d}`,'day');
 const fs=$('#fstatus');fs.value=filt.status;fs.onchange=()=>{filt.status=fs.value;renderDay();};
 const so=$('#fsort');so.value=sortMode;so.onchange=()=>{sortMode=so.value;renderDay();};
 {const rp=$('#revpairs');if(rp)rp.onclick=()=>{showPairs=!showPairs;day.crossYear?openToday(day.m,day.d):openDay(day.d);};}
 {const tp=$('#todaypick');if(tp)tp.onchange=()=>{const [,mm,dd]=tp.value.split('-').map(Number);if(mm&&dd)openToday(mm,dd);};}
 $('#revu').onclick=()=>{const v=visibleItems();const i=v.findIndex(it=>it.state==='undecided');if(i<0){toast('none undecided here');return;}seq=v;enterFocus(i);};
 $('#sweep').onclick=sweepDay;
 if(MODE==='cand'){
  document.querySelectorAll('.chip[data-r]').forEach(c=>c.onclick=()=>{cfilt.rule=c.dataset.r;renderDay();});
  $('#cfp').onclick=()=>{cfilt.fp=!cfilt.fp;renderDay();};
  $('#ciso').onclick=()=>{cfilt.iso=!cfilt.iso;renderDay();};
  $('#bcut').onclick=()=>bulkCand('cut');$('#bkeep').onclick=()=>bulkCand('keep');
  $('#brescue').onclick=rescueCand;
 }
 document.querySelectorAll('.tile').forEach(el=>el.onclick=()=>enterFocusFromId(+el.dataset.id));
}
// 9.4: "the biggest button in any tray is rescue (un-cut), not confirm -- the room is
// biased toward keeping, matching the data model's bias." bulkCand only ever touched
// UNDECIDED frames, so there was no way to take a cut back from the floor at all; the
// only bulk actions were the two that add decisions.
//
// Rescue returns cut frames to undecided rather than to keep. A rescued frame is one you
// have not decided about yet -- promoting it straight to keep would substitute one
// unconsidered decision for another, and the whole room exists to avoid that.
function rescueCand(){
 const v=visibleItems().filter(it=>it.state==='cut');
 if(!v.length){toast('nothing cut in this filter');return;}
 decide(v,'undecided',false);
 toast('rescued '+v.length.toLocaleString()+' back to undecided');
}
function bulkCand(state){
 let v=visibleItems().filter(it=>it.state==='undecided');
 if(!v.length){toast('nothing undecided in filter');return;}
 // Protect guard: bulk CUT never sweeps a protected person OR an edit-linked
 // asset silently. Edit-linked = a deliberate edit or its original (variant pair);
 // the pair is protected as a unit, matching the server-side export choke.
 if(state==='cut'){
  const guarded=it=>it.protected||it.is_edit||it.has_edits;
  const prot=v.filter(guarded);
  if(prot.length){
   const inc=confirm(`${v.length} undecided in filter — ${prot.length} are PROTECTED `
    +`(a person on your protected-people list, or an edit/original variant pair).\n\n`
    +`OK = cut all ${v.length}, including the protected\n`
    +`Cancel = cut only the ${v.length-prot.length} unprotected, skip the protected`);
   if(!inc){v=v.filter(it=>!guarded(it));}
   if(!v.length){toast('only protected frames here — nothing cut');return;}
   decide(v,state,false);
   toast(inc?`cut ${v.length} (protected included)`:`cut ${v.length} · skipped ${prot.length} protected`);
   return;
  }
 }
 if(!confirm(`${state.toUpperCase()} ${v.length} undecided in the current filter?`))return;
 decide(v,state,false);
}
function tile(it){
 const img=it.thumb?`<img loading=lazy src="/thumb/${it.id}.jpg" onerror="this.replaceWith(phspin())">`:'<div class=ph data-pid="'+it.id+'">'+SPIN+'</div>';
 const play=it.is_video?'<span class="badge2 b-play">▶</span>':'';
 const st=it.state!=='undecided'?`<span class="badge2 b-st st-${it.state}">${it.state}</span>`:'';
 let rb='';
 if(MODE==='cand'&&it.rules&&it.rules.length){const r=primaryRule(it);
  rb=`<span class=rb style="background:${RULECOL[r]||'#BA7517'}">${RULELBL[r]||r}</span>`;
  if(it.m&&it.m.B4&&it.m.B4.fp)rb+='<span class="badge2 b-fp">FP</span>';}
 let pip='';
 if(it.ascore!=null){const col=it.ascore>=0.6?'var(--keep)':it.ascore<=0.25?'var(--cut)':'var(--amber)';
  pip=`<span class="badge2 b-pip" style="border-color:${col};color:${col}" title="Apple aesthetic score">${it.ascore.toFixed(2)}</span>`;}
 const live=it.live?'<span class="badge2 b-live" title="Live Photo — motion clip bound to this still">◉ Live</span>':'';
  const edited = (window._EDITED_IDS && (window._EDITED_IDS.has(it.id) || window._EDITED_IDS.has(String(it.id)) || window._EDITED_IDS.has(Number(it.id)))) ? '<span class="badge2 b-edit" style="background:#8b5cf6;color:#fff;margin-left:4px" title="Apple Photos Edited Render">🎨 Edited</span>' : '';
 const movtag=it.live_mov_of?'<span class=rb style="background:#7fb0b8">↳ motion</span>':'';
 return `<div class="tile ${it.state}${it.protected?' prot':''}" data-id="${it.id}" style="--ar:${it.ar||1}">${img}${rb}${movtag}${pip}${play}${live}${edited}${st}
  <div class=tcap>#${it.id} · ${it.ext}${it.is_video&&it.dur?' '+it.dur.toFixed(1)+'s':''}${it.live_mov_of?' · motion of #'+it.live_mov_of:''}</div></div>`;
}
function sweepDay(){
 const v=day.items.filter(it=>it.state==='undecided');
 if(!v.length){toast('nothing undecided in this day');return;}
 if(!confirm(`Mark ${v.length} still-undecided items in "${day.label}" as KEEP?`))return;
 decide(v,'keep',false);
}

// ---------- on-demand thumb polling (pending tiles upgrade as they render) ----------
function startThumbPoll(){stopThumbPoll();thumbTimer=setInterval(pollThumbs,1500);pollThumbs();}
function stopThumbPoll(){if(thumbTimer){clearInterval(thumbTimer);thumbTimer=null;}}
function pollThumbs(){
 const list=(view==='day'&&day)?day.items:((view==='decisions'&&decState)?decState.items:null);
 if(!list){stopThumbPoll();return;}
 const pend=list.filter(it=>!it.thumb);
 if(!pend.length){stopThumbPoll();return;}
 pend.forEach(it=>{
  const im=new Image();
  im.onload=()=>{it.thumb=true;const ph=document.querySelector('.ph[data-pid="'+it.id+'"]');
   if(ph){const ni=document.createElement('img');ni.src='/thumb/'+it.id+'.jpg';ph.replaceWith(ni);}};
  im.src='/thumb/'+it.id+'.jpg?t='+Date.now();
 });
}

// ---------- decide / export ----------
async function decide(items,state,advance){
 const ids=items.map(it=>it.id);
 items.forEach(it=>{if(byId[it.id])byId[it.id].state=state;it.state=state;});
 if(navigator.vibrate)navigator.vibrate(state==='cut'?[16]:8);
 if(view==='focus'){updateFocusState();if(advance)focusNext();}
 else if(view==='day')renderDay();
 const payload=items.map(it=>({id:it.id,bucket:(it.bucket!=null?it.bucket:(day?day.bucket:null))}));
 const r=await (await fetch('/api/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:payload,state,cand:MODE==='cand'})})).json();
 if(r.global){renderStrip(r.global);recomputeDay();}
}
function recomputeDay(){
 if(!day)return;
 day.summary.decided=day.items.filter(it=>it.state!=='undecided').length;
 day.summary.cut=day.items.filter(it=>it.state==='cut').length;
 renderCrumb();
}
async function exportMonth(){
 const u=month.undated?'?undated=1':`?y=${month.y}&m=${month.m}`;
 const r=await jget('/api/export'+u);
 toast(`exported ${r.count} cut (${r.gb} GB) → ${r.path.split('/').pop()}`);
}
function help(){alert('Darkroom — whole-library cull\n\nOverview → month → week → DAY → tap a photo = focus mode.\nFocus: swipe LEFT cut · RIGHT keep · UP skip · DOWN back. Auto-advances.\nKeys: K keep · X cut · S more like this · U undo · ←/→ prev/next · Esc close.\nTouch: long-press a frame = peek (info only, no navigation) · two-finger tap in focus = undo.\n\n"Mark rest of day reviewed (keep)" sweeps all still-undecided items in the day to KEEP — swipe-cut the few you want gone, then one tap closes the day.\n\nToggle ◫ Candidates to filter to the rule-flagged subset (blurry / burst / screenshots / junk / short clips) with rule badges, per-rule chips and fp-rescue — same months & decisions underneath; a cut is a cut in either view.\n\nExport writes per-month lists (culling/library-delete-YYYY-MM.csv) and a candidates slice (candidates-delete.csv), cut ids only. Nothing is ever deleted by this tool.');}

// ---------- focus ----------
function enterFocusFromId(id){seq=visibleItems();const i=seq.findIndex(it=>it.id===id);enterFocus(i<0?0:i);}
// --- hero transition (audit line 299) -------------------------------------
// document.startViewTransition snapshots old and new and animates between them; giving
// the tapped thumbnail and #fimg the same view-transition-name makes it a true
// shared-element transition rather than a cross-fade. Loupe's fetch-then-rerender
// pattern is the ideal host -- this wraps the existing render call and needs no
// framework.
//
// The source element is derived from the item id rather than threaded through the five
// enterFocus call sites, so every entry point gets the transition by construction --
// grid, decisions review, search results and the resume path alike.
//
// #fimg carries the name permanently in CSS; only one exists at a time. The tile's name
// is cleared INSIDE the callback so the new state never has two elements claiming it,
// which is a hard error that silently drops the animation.
function _heroSource(it){
 if(!it)return null;
 const t=document.querySelector('.tile[data-id="'+it.id+'"] img');
 return (t&&t.isConnected)?t:null;
}
function _withHero(src,mutate){
 if(!document.startViewTransition||
    (window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches)){
  mutate();return;
 }
 if(src)src.style.viewTransitionName='loupe-hero';
 let t;
 try{
  t=document.startViewTransition(()=>{ if(src)src.style.viewTransitionName=''; mutate(); });
 }catch(e){ if(src)src.style.viewTransitionName=''; mutate(); return; }
 if(t&&t.finished&&t.finished.finally)t.finished.finally(()=>{
  if(src)src.style.viewTransitionName='';
 });
}
function enterFocus(i){
 const src=_heroSource(seq[i]);
 _withHero(src,()=>{view='focus';fidx=i;$('#focus').classList.add('on');renderFocus();});
}
function closeFocus(){
 exitZoom();
 // Reverse direction: the frame settles back into the sheet it came from. #fimg is
 // hidden once .on is dropped, and hidden elements do not participate, so the name
 // cannot collide with the tile reclaiming it.
 const src=_heroSource(seq[fidx]);
 _withHero(src,()=>{$('#focus').classList.remove('on');});
}
function exitFocus(){closeFocus();
 if(placesReview){const back=placesReview.onExit;placesReview=null;(back||(()=>showTrips(true)))();return;}
 if(decState){view='decisions';renderCrumb();renderDecisions();return;}
 view='day';renderCrumb();renderDay();}
// Places/Trips -> existing review flow, parameterized by an explicit id list (no new decision surface)
let placesReview=null;
function enterReview(items,label,start,onExit){      // items already fetched; reuse the focus/swipe flow
 if(!items||!items.length){toast('no reviewable frames');return;}
 placesReview={label:label||'place',onExit:onExit||null};
 seq=items;byId={};seq.forEach(it=>byId[it.id]=it);
 day=null;            // spans months; per-item it.bucket carries the decision target
 enterFocus(Math.min(Math.max(start||0,0),seq.length-1));
}
const REVIEW_CAP=4000;      // one URL's worth of ids; the server bounds the read too
async function reviewIds(ids,label,opts){
 opts=opts||{};
 if(!ids||!ids.length){toast('no frames here');return;}
 // The cap was silent. "Review all 5,956 -> " then opened the first 4,000 and said
 // nothing, in the room that prepares deletion -- so a set could be worked to its end
 // while ~2,000 frames were never shown. It still caps; it just says so now.
 const capped=ids.length>REVIEW_CAP;
 const use=capped?ids.slice(0,REVIEW_CAP):ids;
 const data=await jget('/api/items?ids='+use.join(','));
 const got=(data.items||[]).length;
 // got < use.length is normal and not a truncation: /api/items hides Live Photo motion
 // components and vaulted frames, which are counted in the room's raw rule universe but
 // are not reviewable. Saying "N of M" beats silently showing fewer than promised.
 if(capped)toast('opening the first '+REVIEW_CAP.toLocaleString()+' of '+ids.length.toLocaleString());
 else if(got<use.length)toast(got.toLocaleString()+' reviewable of '+use.length.toLocaleString()+' flagged');
 enterReview(data.items,label,opts.start,opts.onExit);
}

// ================= Trips: postcard gallery (primary) + map (secondary) =================
let PL=null, TRIPS=null;                 // payload + trip list, loaded once
function escHtml(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function loadScript(src){return new Promise((res,rej)=>{const s=document.createElement('script');s.src=src;s.onload=res;s.onerror=rej;document.head.appendChild(s);});}
async function ensureLeaflet(){
 if(window.L&&window.Supercluster)return;
 if(!document.getElementById('leafcss')){const l=document.createElement('link');l.id='leafcss';l.rel='stylesheet';l.href='https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';document.head.appendChild(l);}
 if(!window.L)await loadScript('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js');
 if(!window.Supercluster)await loadScript('https://unpkg.com/supercluster@8.0.1/dist/supercluster.min.js');
}
async function showTrips(resume){
 view='places';stopThumbPoll();closeFocus();placesReview=null;_fromMap=false;setNav('placestog');   // view sentinel stays 'places'
 closeOverlays('placesview');
 $('#placesview').classList.add('on');renderCrumb();
 syncUrl('/trips');
 if(resume)return;
 if(!TRIPS)TRIPS=await jget('/api/trips');
 $('#plcov').textContent=`${TRIPS.length} ${TRIPS.length===1?'journey':'journeys'} away from home`;
 buildGallery();
}
const _MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function drfmt(s,e){const a=s.split('-'),b=e.split('-');const f=p=>`${_MON[+p[1]-1]} ${+p[2]}`;
 if(s===e)return `${f(a)}, ${a[0]}`;
 if(a[0]===b[0]&&a[1]===b[1])return `${_MON[+a[1]-1]} ${+a[2]}–${+b[2]}, ${a[0]}`;
 if(a[0]===b[0])return `${f(a)} – ${f(b)}, ${a[0]}`;
 return `${f(a)} ${a[0]} – ${f(b)} ${b[0]}`;}
// 9.1: "a 3-frame peek strip under the hero". Built from the ids the trips payload
// already carries, so no API change -- the hero is skipped so the strip shows three
// frames you have not already seen on the card.
function peekStrip(t){
 const ids=(t.ids||[]).filter(id=>id!==t.hero).slice(0,3);
 if(ids.length<3)return '';
 return '<div class=peekstrip aria-hidden=true>'+
   ids.map(id=>`<img loading=lazy src="/thumb/${id}.jpg" alt="">`).join('')+'</div>';
}
function buildGallery(){
 const g=$('#plgallery');
 g.innerHTML=TRIPS.map((t,i)=>{
   const hero=t.hero!=null
     ? `<img class=hero loading=lazy src="/thumb/${t.hero}.jpg" alt="" onerror="this.outerHTML='<div class=well>no preview</div>'">`
     : `<div class=well>no preview</div>`;
   const pm=t.postmark&&t.postmark.state?`<div class=pm><b>${escHtml(t.postmark.state)}</b><i>${escHtml(t.postmark.month||'')}</i></div>`:'';
   return `<div class=pc tabindex=0 role=button data-i="${i}" aria-label="Review trip to ${escHtml(t.title)}, ${t.frames} frames">
     <div class=stripe></div>${pm}${hero}${peekStrip(t)}
     <div class=cap><div class=city>${escHtml(t.city||t.title)}</div><div class=reg>${escHtml(t.region||'')}</div>
       <div class=dr>${drfmt(t.start,t.end)}</div></div>
     <div class=rev>Review ${t.frames} →</div>
     <div class=foot><span>${t.frames} frames · ${t.days} day${t.days===1?'':'s'}</span><a class=vmap data-vi="${i}" title="See this trip on the map">view on map ↗</a></div>
   </div>`;}).join('') || '<div style="color:var(--mut);font-family:var(--mo);padding:30px">— no trips detected —</div>';
 // The hero's real aspect ratio, read off the decoded image rather than added to the
 // API: one source of truth (the file), and the card is correct even for a hero whose
 // stored dimensions are missing. Until it lands the card uses the 4/3 default, so
 // nothing jumps except the one card that was wrong.
 g.querySelectorAll('.pc .hero').forEach(im=>{
  const set=()=>{ if(im.naturalWidth&&im.naturalHeight)
    im.closest('.pc').style.setProperty('--ar',im.naturalWidth+'/'+im.naturalHeight); };
  if(im.complete)set(); else im.addEventListener('load',set,{once:true});
 });
 const open=el=>openTripSheet(+el.dataset.i);   // postcard -> trip-overview (contact sheet), not straight to review
 g.querySelectorAll('.pc').forEach(el=>{
   el.onclick=()=>open(el);
   el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open(el);}};
 });
 g.querySelectorAll('.vmap').forEach(el=>el.onclick=e=>{e.stopPropagation();showMap({trip:+el.dataset.vi});});  // cross-lens deep link
}

// ===== Trip overview: travel-wrapped contact sheet (in front of review) =====
let SHEET=null;            // {i, t, items, cols, obs}
const TS_FH=92;            // frame cell height px (4:3 -> width derived)
function tsCounts(items){let k=0,c=0;for(const it of items){if(it.state==='keep')k++;else if(it.state==='cut')c++;}return {k,c,t:items.length-k-c};}
async function openTripSheet(i,resume){
 const t=TRIPS[i];if(!t)return;
 view='places';closeFocus();placesReview=null;
 closeOverlays('tripsheet');$('#tripsheet').classList.add('on');
 // 9.1: "the table stays visible behind it ... you never lose the room." closeOverlays
 // hides every sibling, #placesview included, so the CSS dim alone was decorative --
 // it was dimming a display:none element. The room is put back deliberately, and it is
 // the side sheet's own width that leaves it visible.
 $('#placesview').classList.add('on');
 if(resume&&SHEET&&SHEET.i===i){renderMast();refreshMarks();renderCrumb();return;}  // back from review: refresh only
 const data=await jget('/api/trip_items?i='+i);                            // by index (tiny URL); reused for sheet + review
 const items=data.items.slice().sort((a,b)=>(a.ts||0)-(b.ts||0));          // roll order (day-level ok)
 SHEET={i,t,items};
 $('#tsreview').onclick=()=>enterReview(SHEET.items,t.title,0,()=>openTripSheet(i,true));
 const sh=$('#tssheet');
 sh.onclick=e=>{const c=e.target.closest('.fcell');if(c)enterReview(SHEET.items,SHEET.t.title,+c.dataset.j,()=>openTripSheet(SHEET.i,true));};
 sh.onmousemove=tsLoupe; sh.onmouseleave=()=>{$('#tloupe').style.display='none';};
 renderMast();buildSheet();renderCrumb();
 sh.scrollTop=0;
}
function renderMast(){
 const t=SHEET.t,c=tsCounts(SHEET.items);
 const pm=t.postmark&&t.postmark.state?`<div class=pm><b>${escHtml(t.postmark.state)}</b><i>${escHtml(t.postmark.month||'')}</i></div>`:'';
 $('#tsmast').innerHTML=`${pm}<div class=city>${escHtml(t.city||t.title)}</div><div class=reg>${escHtml(t.region||'')}</div>
   <div class=exif><span><b>${SHEET.items.length}</b> frames</span><span><b>${t.days}</b> day${t.days===1?'':'s'}</span>
   <span>${drfmt(t.start,t.end)}</span><span>${escHtml(t.coord||'')}</span>
   <span class=prog><b class=k>${c.k}</b> kept · <b class=c>${c.c}</b> cut · <b>${c.t}</b> to review</span></div>`;
 $('#tsreview').textContent=`Review all ${SHEET.items.length} →`;
}
function tsCols(){const w=$('#tssheet').clientWidth-30;const fw=Math.round(TS_FH*4/3)+6;return Math.max(3,Math.floor(w/fw));}
function buildSheet(){
 const sheet=$('#tssheet');sheet.innerHTML='';
 if(SHEET.obs)SHEET.obs.disconnect();
 const cols=tsCols();SHEET.cols=cols;const fw=Math.round(TS_FH*4/3);
 const items=SHEET.items,nstrips=Math.ceil(items.length/cols);
 SHEET.obs=new IntersectionObserver(ents=>{           // lazy: mount visible strips, unmount offscreen (memory cap)
   for(const e of ents){const el=e.target;
     if(e.isIntersecting){if(!el.dataset.mounted)mountStrip(el,+el.dataset.s,cols,fw);}
     else if(el.dataset.mounted){el.querySelector('.frames').innerHTML='';delete el.dataset.mounted;}}
 },{root:sheet,rootMargin:'500px 0px'});
 for(let s=0;s<nstrips;s++){
   const strip=document.createElement('div');strip.className='strip';strip.dataset.s=s;
   strip.style.minHeight=(TS_FH+30)+'px';
   strip.innerHTML=`<div class="rail t"></div><div class="rail b"></div><div class=edge>LOUPE&nbsp;#${String(s*cols+1).padStart(4,'0')}</div><div class=frames></div>`;
   sheet.appendChild(strip);SHEET.obs.observe(strip);
 }
}
function mountStrip(strip,s,cols,fw){
 const items=SHEET.items,frag=[];
 for(let j=s*cols;j<Math.min((s+1)*cols,items.length);j++){
   const it=items[j],mk=it.state==='keep'?'<div class="gmark keep"></div>':it.state==='cut'?'<div class="gmark cut"></div>':'';
   frag.push(`<div class=fcell data-j="${j}" style="width:${fw}px;height:${TS_FH}px"><img loading=lazy src="/thumb/${it.id}.jpg" alt="" onerror="this.style.visibility='hidden'">${mk}<span class=fn>${j+1}</span></div>`);
 }
 strip.querySelector('.frames').innerHTML=frag.join('');
 strip.dataset.mounted='1';
}
function refreshMarks(){    // re-derive grease marks from the (review-mutated) items on mounted strips
 document.querySelectorAll('#tssheet .fcell').forEach(el=>{
   const it=SHEET.items[+el.dataset.j];if(!it)return;
   const want=it.state==='keep'?'keep':it.state==='cut'?'cut':'';
   let m=el.querySelector('.gmark');
   if(!want){if(m)m.remove();}
   else{if(!m){m=document.createElement('div');el.appendChild(m);}m.className='gmark '+want;}
 });
}
function tsLoupe(e){        // cursor-following magnifier of the ALREADY-CACHED thumb — no /api/preview, no transcode
 const cell=e.target.closest('.fcell'),lp=$('#tloupe');
 if(!cell||!cell.querySelector('img')){lp.style.display='none';return;}
 const it=SHEET.items[+cell.dataset.j],r=cell.getBoundingClientRect();
 const px=(e.clientX-r.left)/r.width,py=(e.clientY-r.top)/r.height,Z=3.4,LW=168;
 lp.style.display='block';lp.style.left=(e.clientX-LW/2)+'px';lp.style.top=(e.clientY-LW/2)+'px';
 lp.style.backgroundImage=`url(/thumb/${it.id}.jpg)`;            // same URL the cell <img> already loaded -> cache hit
 lp.style.backgroundSize=(r.width*Z)+'px '+(r.height*Z)+'px';
 lp.style.backgroundPosition=`${LW/2-px*r.width*Z}px ${LW/2-py*r.height*Z}px`;
 const sc=it.ascore!=null?Math.round(it.ascore*100):'—';
 const sh=it.blurpct==null?'no data':(it.blurpct>=40?'sharp':'soft');
 $('#tloupechip').textContent=`score ${sc} · ${sh}`;
}
let _tsRz;addEventListener('resize',()=>{if(!$('#tripsheet').classList.contains('on')||!SHEET)return;
 clearTimeout(_tsRz);_tsRz=setTimeout(()=>buildSheet(),200);});

// ===== Settings + Residences (residences = the source of truth for is_home) =====
let SETTINGS_RES=null, PLACE_NAMES=null, _resEdit=null, _resDraftAreas=[];
async function showSettings(){
 view='settings';stopThumbPoll();closeFocus();setNav('settingstog');
 closeOverlays('settingsview');$('#settingsview').classList.add('on');renderCrumb();
 syncUrl('/settings');   // sync band (hides it on this overlay)
 if(!PLACE_NAMES){PLACE_NAMES=(await jget('/api/place_names')).places||[];}
 if(!SETTINGS_RES){SETTINGS_RES=(await jget('/api/residences')).residences||[];}
 // owner-only "Nudity screening" settings section (a guest never sees the nav item)
 const nn=$('#nav-nsfw');
 if(nn){ nn.style.display=window.LOCAL_FULLRES?'':'none';
   if(!nn._wired){ nn._wired=1;
     nn.onclick=()=>selectSettingsSection('nsfw');
     const nr=$('#nav-residences'); if(nr) nr.onclick=()=>selectSettingsSection('residences'); } }
 selectSettingsSection('residences');
}
function selectSettingsSection(which){
 const nr=$('#nav-residences'); if(nr) nr.classList.toggle('on', which==='residences');
 const nn=$('#nav-nsfw'); if(nn) nn.classList.toggle('on', which==='nsfw');
 if(which==='nsfw') renderNsfwSettings(); else renderResidences();
}
// Nudity-screening section: the canonical config home (same write routes as /nsfw, so the
// two threshold surfaces stay in sync). Owner-only. No second amber in this view.
async function renderNsfwSettings(){
 let c={}; try{ c=await jget('/api/nsfw/config'); }catch(e){ $('#setcontent').innerHTML='<div class=setsub>Screening config unavailable.</div>'; return; }
 const en=!!c.nsfw_enabled, thr=(c.nsfw_threshold!=null?Number(c.nsfw_threshold):0.5), n=c.flagged||0;
 $('#setcontent').innerHTML=`<div class=seth>Closed Set</div>
   <div class=setsub>On-device screening of your library for possible nudity. Everything runs on this machine — nothing is uploaded, and flagged frames are never deleted. Owner-only; never shown to shared viewers.</div>
   <div class=nsfwset>
     <label class=nsfwtoggle><input type=checkbox id=setnsfwen ${en?'checked':''}> <span>Enable screening</span></label>
     <div class=nsfwsetsub>The master switch. Turning it off doesn’t delete scores or hide the review console — those work regardless.</div>
     <div class=nsfwthrrow>
       <span class=nsfwsetlbl>Flag at score ≥ <b id=setthrval>${thr.toFixed(2)}</b></span>
       <input id=setthr type=range min=0 max=1 step=0.01 value=${thr}
              oninput="document.getElementById('setthrval').textContent=Number(this.value).toFixed(2)"
              onchange="nsfwSettingsThreshold(this.value)">
     </div>
     <div class=nsfwsetsub>Lower = more aggressive (more false positives). <b id=setflagged>${n.toLocaleString()}</b> flagged now.</div>
     <a class=nsfwreviewlink id=setreview href="/nsfw">Open the Closed Set (<span id=setreviewn>${n.toLocaleString()}</span>) →</a>
   </div>`;
 $('#setnsfwen').onchange=async e=>{const on=e.target.checked;
   try{ await fetch('/api/settings/nsfw',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:on})}); toast(on?'screening enabled':'screening disabled'); }
   catch(err){ e.target.checked=!on; toast('could not change'); }};
 $('#setreview').onclick=e=>{ e.preventDefault(); showNsfw(); };
}
async function nsfwSettingsThreshold(v){
 const t=Number(v); $('#setthrval').textContent=t.toFixed(2);
 let r={};
 try{ r=await (await fetch('/api/settings/nsfw_threshold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({threshold:t})})).json(); }
 catch(e){ toast('could not set threshold'); return; }
 const n=(r.flagged!=null?r.flagged:0).toLocaleString();
 const f=$('#setflagged'); if(f) f.textContent=n;
 const rn=$('#setreviewn'); if(rn) rn.textContent=n;   // the route already rebuilt NSFW_IDS — no rescan/restart
}
function _ym2m(s){if(!s)return null;const p=s.split('-');return (+p[0])*12+(+p[1]||1)-1;}
function renderResidences(){
 const A=_ym2m('2002-01'),B=_ym2m('2026-12');
 let tl='<div class=tl>';
 for(let y=2002;y<=2026;y+=4)tl+=`<span class=yr style="left:${(100*(_ym2m(y+'-01')-A)/(B-A)).toFixed(1)}%">${y}</span>`;
 SETTINGS_RES.forEach(R=>{const s=_ym2m(R.start),e=R.end?_ym2m(R.end):B;
   tl+=`<div class=seg style="left:${(100*(s-A)/(B-A)).toFixed(1)}%;width:${Math.max(100*(e-s)/(B-A),1.2).toFixed(1)}%;background:${R.color||'#BA7517'}">${escHtml(R.label)}</div>`;});
 tl+='</div>';
 const cards=SETTINGS_RES.map((R,i)=>`<div class=rescard><div class=sw style="background:${R.color||'#BA7517'}"></div>
   <div class=body><div class=rl>${escHtml(R.label)}</div>
     <div class=rd>${escHtml(R.start)} → ${R.end?escHtml(R.end):'Present'} · within ${R.radius_km} km</div>
     <div class=chips>${(R.areas||[]).map(a=>`<span class=rchip>${escHtml(a)}</span>`).join('')||'<span class=rchip style="opacity:.6">no areas</span>'}</div></div>
   <div class=acts><button data-edit="${i}">edit</button><button data-rm="${i}" class=bcut>remove</button></div></div>`).join('');
 $('#setcontent').innerHTML=`<div class=seth>Residences</div>
   <div class=setsub>Where you lived and when — the single source of truth for what counts as “home”. Trips, the map’s hide-home, and place-bursts all read this; empty stretches are gaps with no declared home.</div>
   ${tl}<div class=rescards>${cards||'<div style="color:var(--mut);font-family:var(--mo)">no residences yet</div>'}</div>
   <button class="amber addres" id=addres>+ Add residence</button>`;
 $('#addres').onclick=()=>openResForm(null);
 document.querySelectorAll('#setcontent [data-edit]').forEach(b=>b.onclick=()=>openResForm(+b.dataset.edit));
 document.querySelectorAll('#setcontent [data-rm]').forEach(b=>b.onclick=()=>removeRes(+b.dataset.rm));
}
function openResForm(idx){
 _resEdit=idx;
 const R=idx==null?{label:'',areas:[],radius_km:40,start:'',end:null}:SETTINGS_RES[idx];
 _resDraftAreas=(R.areas||[]).slice();
 const present=!R.end;
 $('#resform').innerHTML=`<h3>${idx==null?'Add residence':'Edit residence'}</h3>
   <label>Label</label><input type=text id=rf_label value="${escHtml(R.label)}">
   <label>Areas — your library’s own places</label>
   <div class=atype><div class=achips id=rf_chips></div><input type=text id=rf_area autocomplete=off placeholder="type a city…"><div class=adrop id=rf_drop></div></div>
   <div class=resrow><div><label>Backstop radius (km)</label><input type=number id=rf_radius value="${R.radius_km||40}" min=1 max=500></div>
     <div><label>Start (month)</label><input type=month id=rf_start value="${R.start||''}"></div></div>
   <div class=resrow><div><label>End (month)</label><input type=month id=rf_end value="${R.end||''}" ${present?'disabled':''}></div>
     <div><label class=present><input type=checkbox id=rf_present ${present?'checked':''}> Present (no end)</label></div></div>
   <div class=resfoot><button onclick="closeResForm()">cancel</button><button class=amber id=rf_save>Save</button></div>`;
 renderAreaChips();
 $('#rf_present').onchange=e=>{$('#rf_end').disabled=e.target.checked;if(e.target.checked)$('#rf_end').value='';};
 const ai=$('#rf_area');ai.oninput=()=>areaDrop(ai.value);ai.onfocus=()=>areaDrop(ai.value);
 ai.onblur=()=>setTimeout(()=>$('#rf_drop').classList.remove('on'),180);
 $('#rf_save').onclick=saveResForm;
 $('#resmodal').classList.add('on');
}
function renderAreaChips(){
 $('#rf_chips').innerHTML=_resDraftAreas.map((a,i)=>`<span class=achip>${escHtml(a)}<b data-x="${i}">✕</b></span>`).join('');
 document.querySelectorAll('#rf_chips [data-x]').forEach(b=>b.onclick=()=>{_resDraftAreas.splice(+b.dataset.x,1);renderAreaChips();});
}
function areaDrop(q){
 q=(q||'').toLowerCase();
 const m=PLACE_NAMES.filter(p=>p.name.toLowerCase().includes(q)&&!_resDraftAreas.includes(p.name)).slice(0,12);
 const d=$('#rf_drop');
 d.innerHTML=m.map(p=>`<div data-n="${escHtml(p.name)}">${escHtml(p.name)}<span class=ct>${p.count}</span></div>`).join('');
 d.classList.toggle('on',m.length>0);
 d.querySelectorAll('[data-n]').forEach(el=>el.onmousedown=e=>{e.preventDefault();_resDraftAreas.push(el.dataset.n);renderAreaChips();$('#rf_area').value='';areaDrop('');$('#rf_area').focus();});
}
function closeResForm(){$('#resmodal').classList.remove('on');}
async function saveResForm(){
 const start=$('#rf_start').value;if(!start){toast('start month is required');return;}
 const present=$('#rf_present').checked;
 const R={id:(_resEdit!=null&&SETTINGS_RES[_resEdit].id)||('r'+Date.now()),
   label:$('#rf_label').value.trim()||'Untitled',areas:_resDraftAreas.slice(),
   radius_km:+$('#rf_radius').value||40,start:start,end:present?null:($('#rf_end').value||null),
   color:(_resEdit!=null&&SETTINGS_RES[_resEdit].color)||'#BA7517',
   order:_resEdit!=null?SETTINGS_RES[_resEdit].order:SETTINGS_RES.length};
 if(_resEdit==null)SETTINGS_RES.push(R);else SETTINGS_RES[_resEdit]=R;
 await persistResidences();closeResForm();renderResidences();
}
async function removeRes(idx){
 if(!confirm(`Remove residence “${SETTINGS_RES[idx].label}”?`))return;
 SETTINGS_RES.splice(idx,1);await persistResidences();renderResidences();
}
async function persistResidences(){
 const r=await (await fetch('/api/residences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({residences:SETTINGS_RES})})).json();
 TRIPS=null;PL=null;SHEET=null;MAP_PTS=null;MAP_TRIPS=null;MAP_RES=null;mapDirty=true;   // is_home changed → trips/map/sheet recompute on next open
 toast(r&&r.ok?'residences saved · trips & map recomputed':'save failed');
}

function showPlaces(r){return showTrips(r);}   // back-compat alias

// ===================== MAP: top-level dark light-table place browser =====================
// Reuses the offline-geocoded points (/api/map/points), trips (/api/map/trips), and
// residences (/api/residences, now with computed centers). Client-side supercluster over
// 50k+ points. Basemap is a VENDORED, simplified, public-domain Natural Earth GeoJSON
// (/static/basemap.geojson) drawn as the warm "light-table" field — NO external tiles,
// no street map, no OSM/CARTO attribution. (Self-hosted vector tiles remain a later option.)
let MAP_PTS=null, MAP_TRIPS=null, MAP_RES=null, MAP_HOME=null, MAP_SPAN=null, MAP_NOGPS=0, MAP_TOTAL=0, MAP_BASEMAP=null, MASK_RADIUS_KM=null;
let lmap=null, lmapBuilt=false, mapSC=null, mapClusterLayer=null, mapTripLayer=null, mapZoneLayer=null, mapApproxLayer=null, mapBaseLayer=null;
let mapDirty=false, mapFrom=2002, mapTo=2026, mapCardIds=null, _fromMap=false, _scrubT=null;
let mapSel=null;   // {lat,lon,leaf} of the currently selected place (white-ring state)
const MAP_SPAN_LO=2002, MAP_SPAN_HI=2026;                 // library span (the scrubber bounds)

async function showMap(opts){
 opts=opts||{};
 view='map';stopThumbPoll();closeFocus();placesReview=null;setNav('maptog');
 closeOverlays('mapview');
 $('#mapview').classList.add('on');renderCrumb();
 syncUrl('/map');
 try{await ensureLeaflet();}catch(e){toast('map libraries failed to load (offline?)');return;}
 if(!MAP_BASEMAP){try{MAP_BASEMAP=await jget('/static/basemap.geojson');}catch(e){MAP_BASEMAP={land:null,states:null};}}
 if(!MAP_PTS||mapDirty){
   const d=await jget('/api/map/points');
   MAP_PTS=d.points;MAP_HOME=d.home;MAP_SPAN=d.span;MAP_NOGPS=d.no_gps;MAP_TOTAL=d.total;
   MASK_RADIUS_KM=d.mask_radius_km||null;
   MAP_TRIPS=await jget('/api/map/trips');
   MAP_RES=(await jget('/api/residences')).residences||[];
   mapDirty=false;
 }
 buildMapOnce();
 $('#mapcov').textContent=`${MAP_PTS.length.toLocaleString()} of ${MAP_TOTAL.toLocaleString()} placed · ${(100*MAP_PTS.length/Math.max(MAP_TOTAL,1)).toFixed(0)}% have GPS`;
 $('#mapnote').innerHTML=`<b>${MAP_NOGPS.toLocaleString()}</b> have no location and live in <a onclick="showOverview()">Library</a>.`;
 applyMapFilters();
 setTimeout(()=>lmap.invalidateSize(),60);
 if(opts.trip!=null)focusMapTrip(opts.trip);
}
function buildMapOnce(){
 if(lmapBuilt){return;}
 const calm=matchMedia('(prefers-reduced-motion:reduce)').matches;   // honor reduced-motion for pan/zoom too
 lmap=L.map('lmap',{preferCanvas:true,worldCopyJump:true,zoomControl:true,attributionControl:false,
        zoomAnimation:!calm,fadeAnimation:!calm,markerZoomAnimation:!calm,inertia:!calm,
        maxBounds:[[-85,-200],[85,200]],minZoom:2})
        .setView([MAP_HOME.lat,MAP_HOME.lng],4);
 lmap.zoomControl.setPosition('topright');         // keep the +/- clear of the Layers panel (top-left)
 // Vendored, simplified, PUBLIC-DOMAIN Natural Earth basemap — the warm light-table
 // field. No tiles, no street map, no labels, no attribution (Natural Earth is PD).
 const baseRenderer=L.canvas({padding:0.5});
 mapBaseLayer=L.layerGroup().addTo(lmap);
 drawGraticule();                                  // faint lat/lon grid, under the land
 if(MAP_BASEMAP&&MAP_BASEMAP.land)
   L.geoJSON(MAP_BASEMAP.land,{renderer:baseRenderer,interactive:false,
     style:{fillColor:'#221c12',fillOpacity:1,color:'rgba(186,117,23,.28)',weight:0.8}}).addTo(mapBaseLayer);
 if(MAP_BASEMAP&&MAP_BASEMAP.states)
   L.geoJSON(MAP_BASEMAP.states,{renderer:baseRenderer,interactive:false,
     style:{fill:false,color:'rgba(186,117,23,.35)',weight:1}}).addTo(mapBaseLayer);
 mapZoneLayer=L.layerGroup().addTo(lmap);        // residence zones (under points)
 mapApproxLayer=L.layerGroup().addTo(lmap);      // guest-masked approximate-area circles
 mapTripLayer=L.layerGroup().addTo(lmap);        // trip polylines
 mapClusterLayer=L.layerGroup().addTo(lmap);     // place clusters (top)
 lmap.on('moveend zoomend',renderMapClusters);
 lmap.on('click',()=>closeMapCard());   // tap the field → deselect + dismiss the card
 // layer toggles
 $('#lyrTrips').onchange=()=>drawTrips();
 $('#lyrHome').onchange=()=>{$('#lyrHomeHint').textContent=$('#lyrHome').checked?'on · clusters':'off · zones';applyMapFilters();};
 // dual-handle time scrubber (handles may cross; we just take min/max)
 const A=$('#scrubA'),B=$('#scrubB');
 const onScrub=()=>{const a=+A.value,b=+B.value;mapFrom=Math.min(a,b);mapTo=Math.max(a,b);
   $('#scrublab').textContent=mapFrom+' – '+mapTo;paintScrubFill();
   clearTimeout(_scrubT);_scrubT=setTimeout(applyMapFilters,140);};   // debounce the 50k re-index during drag
 A.oninput=onScrub;B.oninput=onScrub;
 paintScrubFill();
 lmapBuilt=true;
}
function paintScrubFill(){
 const lo=MAP_SPAN_LO,hi=MAP_SPAN_HI,f=$('#scrubfill');if(!f)return;
 f.style.left=(100*(mapFrom-lo)/(hi-lo))+'%';
 f.style.right=(100*(hi-mapTo)/(hi-lo))+'%';
}
function mapPointsFiltered(){
 const homeOn=$('#lyrHome').checked;
 return MAP_PTS.filter(p=>{
   if(!homeOn&&p.home===1)return false;                 // Home off → suppress home-era clusters
   const y=p.y||0;if(y&&(y<mapFrom||y>mapTo))return false;
   return true;});
}
function applyMapFilters(){
 const filtered=mapPointsFiltered();
 const pts=filtered.filter(p=>!p.approx).map(p=>({type:'Feature',
   properties:{id:p.id,place:p.place,t:p.t},geometry:{type:'Point',coordinates:[p.lng,p.lat]}}));
 // Larger radius → at low zoom the country reads as a few clean glowing places, not a
 // swarm of overlapping town bubbles; the hierarchy still breaks down to frames on zoom.
 mapSC=new Supercluster({radius:88,maxZoom:18,minPoints:3}).load(pts);
 mapApproxLayer.clearLayers();
 for(const p of filtered){
   if(!p.approx)continue;
   L.circle([p.lat,p.lng],{radius:(MASK_RADIUS_KM||1)*1000,
     color:'#BA7517',weight:1,opacity:.6,fillColor:'#BA7517',fillOpacity:.12,dashArray:'4 3'}).addTo(mapApproxLayer);
 }
 drawZones();drawTrips();renderMapClusters();
}
function renderMapClusters(){
 if(!mapSC||!mapClusterLayer)return;
 mapClusterLayer.clearLayers();
 const b=lmap.getBounds(),z=Math.round(lmap.getZoom());
 const cl=mapSC.getClusters([b.getWest(),b.getSouth(),b.getEast(),b.getNorth()],z);
 // 9.2's heat ring: "cluster border thickness encodes count decile (1-4px) in neutral".
 // The decile is taken across the clusters actually on screen and recomputed per draw,
 // so the ring compares what you can see rather than against a library-wide constant
 // that would make every cluster look identical when zoomed into one city.
 const counts=cl.filter(c=>c.properties.cluster).map(c=>c.properties.point_count).sort((a,b)=>a-b);
 const decile=n=>{
  if(counts.length<2)return 0;
  let lo=0,hi=counts.length;while(lo<hi){const m=(lo+hi)>>1;if(counts[m]<n)lo=m+1;else hi=m;}
  return Math.min(9,Math.floor(lo/counts.length*10));
 };
 for(const c of cl){
  const [lon,lat]=c.geometry.coordinates;
  const sel=mapSel&&Math.abs(lat-mapSel.lat)<1e-4&&Math.abs(lon-mapSel.lon)<1e-4;
  if(c.properties.cluster){
   const n=c.properties.point_count,cid=c.properties.cluster_id;
   const sz=Math.round(Math.min(74,26+Math.log2(n)*7.5));
   const label=(z>=4&&n>=12)?dominantPlace(cid):'';   // Fraunces paper label on meaningful clusters only
   const cnt=n>999?(n/1000).toFixed(1)+'k':n;
   // 9.2: "clusters render as frame-stacks (2-3 offset rectangles + tabular count),
   // never dots -- photographs remain the unit even at 30,000 ft." A third frame only
   // appears once a cluster is big enough for the stack to read as a pile.
   const w=sz,h=Math.round(sz*0.74);
   const deep=n>=25;
   const ring=1+Math.round(decile(n)/9*3);            // 1-4px, neutral
   const html=`<div class="mcl${sel?' sel':''}" style="width:${w}px;height:${h}px;font-size:${Math.min(15,9+sz/10)}px">`+
              (deep?'<i class=f3></i>':'')+'<i class=f2></i>'+
              `<b style="border-width:${ring}px">${cnt}</b></div>`+
              (label?`<div class=mcl-lbl>${escHtml(label)}</div>`:'');
   const ic=L.divIcon({html,className:'',iconSize:[w,h],iconAnchor:[w/2,h/2]});
   L.marker([lat,lon],{icon:ic}).addTo(mapClusterLayer)
     .on('click',(e)=>{L.DomEvent.stopPropagation(e);selectCluster(cid,lat,lon);});   // select → preview card (two-step door)
  }else{
   const ic=L.divIcon({html:`<div class="mptglow${sel?' sel':''}"></div>`,className:'',iconSize:[14,14],iconAnchor:[7,7]});
   L.marker([lat,lon],{icon:ic}).addTo(mapClusterLayer)
     .on('click',(e)=>{L.DomEvent.stopPropagation(e);selectSingle(c.properties.id,c.properties.place,lat,lon,c.properties.t);});
  }
 }
}
function drawGraticule(){   // faint lat/lon grid under the land, per the mock
 const r=L.canvas({padding:0.5}),st={color:'rgba(233,228,214,.05)',weight:1,interactive:false,renderer:r};
 for(let la=-80;la<=80;la+=20)L.polyline([[la,-180],[la,180]],st).addTo(mapBaseLayer);
 for(let lo=-180;lo<=180;lo+=20)L.polyline([[-85,lo],[85,lo]],st).addTo(mapBaseLayer);
}
function dominantPlace(cid){       // modal place name among a sample of the cluster's leaves
 let leaves;try{leaves=mapSC.getLeaves(cid,80);}catch(e){return '';}
 const tally={};let best=null,bn=0;
 for(const l of leaves){const p=l.properties.place;if(!p)continue;tally[p]=(tally[p]||0)+1;if(tally[p]>bn){bn=tally[p];best=p;}}
 return best||'';
}
function drawZones(){
 if(!mapZoneLayer)return;mapZoneLayer.clearLayers();
 if($('#lyrHome').checked)return;                  // Home ON → clusters shown instead of zones
 for(const R of (MAP_RES||[])){
   const c=R.center;if(!c||c.lat==null)continue;
   L.circle([c.lat,c.lng],{radius:(R.radius_km||40)*1000,color:R.color||'#BA7517',weight:1.5,
     dashArray:'6 6',fillColor:R.color||'#BA7517',fillOpacity:.08}).addTo(mapZoneLayer)
     .bindTooltip(`${escHtml(R.label||'home')} · ${escHtml(R.start||'')}–${escHtml(R.end||'now')}`,{direction:'top',className:'mapzonetip'});
 }
}
function tripInRange(t){const sy=t.start?+t.start.slice(0,4):null,ey=t.end?+t.end.slice(0,4):sy;
 return !((ey!=null&&ey<mapFrom)||(sy!=null&&sy>mapTo));}
function drawTrips(){
 if(!mapTripLayer)return;mapTripLayer.clearLayers();
 if(!$('#lyrTrips').checked)return;
 for(const t of (MAP_TRIPS||[])){
   if(!tripInRange(t))continue;
   const line=(t.stops||[]).map(s=>[s.lat,s.lng]);if(line.length<1)continue;
   if(line.length>=2){
     L.polyline(line,{color:'#cdb389',weight:2,opacity:.85,dashArray:'2 7',lineCap:'round'}).addTo(mapTripLayer)
       .on('click',(e)=>{L.DomEvent.stopPropagation(e);openTripFromMap(t);});
   }
   // film-strip stops: small square frames along the journey
   for(const s of t.stops){
     L.circleMarker([s.lat,s.lng],{radius:3.5,color:'#1a1207',weight:1,fillColor:'#cdb389',fillOpacity:.95})
      .addTo(mapTripLayer).on('click',(e)=>{L.DomEvent.stopPropagation(e);openTripFromMap(t);});
   }
 }
}
function openTripFromMap(t){
 _fromMap=true;
 // reuse the existing trip-overview contact sheet, by index, from the map
 if(t.i!=null&&TRIPS){showTrips(true);openTripSheet(t.i);}
 else reviewIds([],t.title);
}
// ----- two-step door: select a place → preview card → existing reviewIds() review -----
function selectCluster(cid,lat,lon){
 const leaves=mapSC.getLeaves(cid,1000000);
 mapSel={lat,lon};
 const place=dominantPlace(cid)||'This place';
 showMapCard(place,leaves.map(l=>l.properties.id),clusterMeta(leaves));
 renderMapClusters();                 // re-paint so the picked place gets the white ring
}
function selectSingle(id,place,lat,lon,t){
 mapSel={lat,lon};
 const first=t?fmtMon(t):null;
 showMapCard(place||'A single frame',[id],{visits:1,first,last:first});
 renderMapClusters();
}
function fmtMon(t){return new Date(t*1000).toLocaleString('en-US',{month:'short',year:'numeric'});}
function clusterMeta(leaves){          // frames + distinct-day VISITS + date span, from the leaves' timestamps
 const days=new Set();let mn=Infinity,mx=-Infinity;
 const lo=788918400,hi=Date.now()/1000+86400;   // ignore corrupt EXIF stamps (pre-1995 / future) so the span stays sane
 for(const l of leaves){const t=l.properties.t;if(!t||t<lo||t>hi)continue;
   const d=new Date(t*1000);days.add(d.getFullYear()+'-'+d.getMonth()+'-'+d.getDate());
   if(t<mn)mn=t;if(t>mx)mx=t;}
 return {visits:days.size, first:mn<Infinity?fmtMon(mn):null, last:mx>-Infinity?fmtMon(mx):null};
}
function sampleEven(arr,k){if(arr.length<=k)return arr.slice();
 const out=[],step=arr.length/k;for(let i=0;i<k;i++)out.push(arr[Math.floor(i*step)]);return out;}
function showMapCard(title,ids,meta){
 meta=meta||{};mapCardIds=ids;
 $('#mctitle').textContent=title;
 const parts=[`${ids.length.toLocaleString()} frame${ids.length===1?'':'s'}`];
 if(meta.visits)parts.push(`${meta.visits} visit${meta.visits===1?'':'s'}`);
 if(meta.first&&meta.last)parts.push(meta.first===meta.last?meta.first:`${meta.first} – ${meta.last}`);
 $('#mcsub').textContent=parts.join(' · ');
 // A strip, not a sample: 6 thumbs was a preview of the cluster, and 9.2 asks for the
 // cluster itself. Capped so a 20,000-frame cluster does not build 20,000 <img>.
 $('#mcthumbs').innerHTML=sampleEven(ids,60).map(id=>`<img loading=lazy src="/thumb/${id}.jpg" alt="" onerror="this.style.visibility='hidden'">`).join('');
 $('#mcopen').onclick=()=>{_fromMap=true;const keep=mapCardIds;closeMapCard(true);reviewIds(keep,title,{onExit:()=>showMap()});};
 const card=$('#mapcard');card.classList.add('on');requestAnimationFrame(()=>card.classList.add('show'));
}
function closeMapCard(skipRender){
 const c=$('#mapcard');if(c)c.classList.remove('show','on');
 mapCardIds=null;mapSel=null;
 if(!skipRender&&mapClusterLayer)renderMapClusters();   // clear the white ring
}
function focusMapTrip(i){
 const tt=(MAP_TRIPS||[]).find(x=>x.i===i);
 if(!tt){toast('that trip is filtered out of the current range');return;}
 // turn the Trips layer on and frame the journey
 $('#lyrTrips').checked=true;drawTrips();
 const b=tt.bounds;if(b)lmap.fitBounds([[b[0],b[1]],[b[2],b[3]]],{padding:[70,70],maxZoom:11});
 else lmap.setView([tt.lat,tt.lng],8);
 toast(`showing ${tt.title}`);
}

// ===================== The Cutting Room — explained doorway into candidate review =====================
// Read-only overview; makes NO decisions. "Review all" hands a rule's ids to the EXISTING
// candidate review surface (MODE='cand' so it shows rule context); all keep/cut happens there.
let CR=null;
async function showCuttingRoom(){
 view='cutting';stopThumbPoll();closeFocus();placesReview=null;setNav('modetog');
 closeOverlays('cuttingview');
 $('#cuttingview').classList.add('on');renderCrumb();
 syncUrl('/cutting-room');
 if(!CR)CR=await jget('/api/cutting-room');
 renderCuttingRoom();
}
// ---------- Calendar — 366-day completion overview ----------
async function showCalendar(){
 view='calendar';stopThumbPoll();closeFocus();placesReview=null;setNav('calendartog');
 closeOverlays('calendarview');
 $('#calendarview').classList.add('on');renderCrumb();
 syncUrl('/calendar');
 calendarData=await jget('/api/calendar'+mq(true));
 renderCalendar();
}
function renderCalendar(){
 const s=calendarData.summary;
 $('#calstats').innerHTML=
   `<span class=st><b class="v amber">${s.decided.toLocaleString()}</b> / <b class=v>${s.total.toLocaleString()}</b> reviewed</span>`+
   `<span class=st><b class="v g">${s.pct}%</b> of the calendar</span>`;
 let h='';
 for(let m=1;m<=12;m++){
  const days=calendarData.days.filter(d=>d.m===m);
  const cells=days.map(d=>`<div class="calcell ${d.state}" data-m="${d.m}" data-d="${d.d}"
    title="${escHtml(d.label)}: ${d.decided}/${d.total} reviewed (${d.pct}%)"><span class=caldnum>${d.d}</span></div>`).join('');
  h+=`<div class=calmonth><div class=calmonthlabel>${MON_FULL[m]}</div><div class=caldays>${cells}</div></div>`;
 }
 $('#calgrid').innerHTML=h;
 $('#calgrid').querySelectorAll('.calcell').forEach(el=>{
  el.onclick=()=>openToday(+el.dataset.m,+el.dataset.d);
 });
}
// ---- D8 / audit 9.4: the floor -------------------------------------------------
// "The floor: rule bins (B2-B5/A2a/A2b + future embedding-dupes C10) as labeled trays
// down the left; the selected tray's frames spill onto the sheet with their triggering
// rule as small-caps caption."
//
// The room was a grid of cards instead: every bin equally prominent, each showing five
// sample thumbs, and the only way to see a bin's contents was to leave the room for the
// review surface. That is a directory of piles, not a floor you work on -- you could not
// look INTO a bin without committing to reviewing it.
//
// Trays now run down the left and the selected one spills onto the sheet beside them.
// The rule slip travels with the sheet, so the reason a bin exists is next to its frames
// rather than on a card you have navigated away from.
let CRSEL=null, CRIDS=null;

function renderCuttingRoom(){
 const t=CR.total;
 $('#crstats').innerHTML=
   `<span class=st><b class="v amber">${t.unique.toLocaleString()}</b> set aside</span>`+
   `<span class=st>~<b class=v>${t.reclaim_gb}</b> GB reclaimable</span>`+
   `<span class=st><b class="v g">${t.kept.toLocaleString()}</b> kept so far</span>`+
   `<span class=st><b class=v>${t.untouched.toLocaleString()}</b> untouched</span>`;

 if(!CRSEL||!CR.cats.some(c=>c.key===CRSEL))CRSEL=(CR.cats[0]||{}).key||null;

 const trays=CR.cats.map(c=>{
   const conf=c.confidence==='High confidence'?'high':c.confidence==='Review'?'review':'';
   return `<button class="crtray${c.key===CRSEL?' on':''}${c.accent?' amber':''}" role=tab
      aria-selected="${c.key===CRSEL}" data-rule="${escHtml(c.key)}" data-title="${escHtml(c.title)}">
     <span class=crbadge>${escHtml(c.key)}</span>
     <span class=trname>${escHtml(c.title)}</span>
     <span class="trn">${c.count.toLocaleString()}</span>
     ${conf?`<span class="crchip ${conf}">${escHtml(c.confidence)}</span>`:''}
   </button>`;}).join('');

 $('#crgrid').innerHTML=
   `<div class=crfloor>
      <div class=crtrays role=tablist aria-label="Rule bins">${trays}</div>
      <div class=crpane id=crpane></div>
    </div>`;

 $('#crgrid').querySelectorAll('.crtray').forEach(el=>{
   el.onclick=()=>{CRSEL=el.dataset.rule;renderCuttingRoom();};
 });
 renderTraySheet();
}

async function renderTraySheet(){
 const pane=$('#crpane');if(!pane)return;
 const c=CR.cats.find(x=>x.key===CRSEL);
 if(!c){pane.innerHTML='';return;}
 const head=`<div class=crhead>
     <div>
       <div class=crtitle>${escHtml(c.title)}</div>
       <div class=crcount><b>${c.count.toLocaleString()}</b><span class=rc>frames · ~${c.reclaim_gb} GB</span></div>
     </div>
     <a class=crrev role=button tabindex=0 data-rule="${escHtml(c.key)}" data-title="${escHtml(c.title)}">Review all ${c.count.toLocaleString()} →</a>
   </div>
   <p class=crexplain>${escHtml(c.explain)}</p>
   <div class=crslip><span class=k>The rule</span><span class=r>${escHtml(c.rule)}</span><span class=k>Watch for</span><span class=r>${escHtml(c.watch)}</span></div>`;
 pane.innerHTML=head+'<div class=crframes id=crframes><div class=crwait>opening the tray…</div></div>';
 pane.querySelectorAll('.crrev').forEach(el=>{
   const go=()=>{const k=el.dataset.rule;
     if(k==='NSFW'){showNsfw('nudity');return;}
     if(k==='PROD'){showNsfw('production');return;}
     reviewCuttingSet(k,el.dataset.title);};
   el.onclick=go;el.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}};
 });

 const key=c.key;
 let ids=[],pos=null;
 try{ const d=await jget('/api/cutting-room/ids?rule='+encodeURIComponent(key));
      ids=d.ids||[]; pos=d.pos||null; }
 catch(e){ ids=c.thumbs||[]; }
 if(CRSEL!==key)return;                        // a faster click won the race
 CRIDS=ids;
 const SHOWN=90;                               // the sheet is a look inside, not the whole pile
 const view=ids.slice(0,SHOWN);
 // 9.4's caption in full: "BURST EXTRA · 5 OF 7" -- the rule, then the frame's position
 // within its own burst. I previously left the second half out on the grounds that it
 // needed C2. That was wrong: the pipeline has computed cluster_id, sharp_rank and
 // cluster_size per B3 frame all along (culling.py, BURST_GAP_S=5), and they simply were
 // not reaching the client. It is the frame's rank in ITS BURST, never its index in the
 // tray -- those would read identically and mean entirely different things.
 const cap=escHtml((c.title||key).toUpperCase());
 const capOf=id=>{
  const p=pos&&pos[id];
  return p?cap+' · '+p[0]+' OF '+p[1]:cap;
 };
 const f=$('#crframes');if(!f)return;
 f.innerHTML=view.length
   ? view.map(id=>`<figure class=crframe>
       <img loading=lazy src="/thumb/${id}.jpg" alt="" onerror="this.style.visibility='hidden'">
       <figcaption class=crcap>${capOf(id)}</figcaption></figure>`).join('')
     +(ids.length>SHOWN?`<div class=crmore>+${(ids.length-SHOWN).toLocaleString()} more in this tray</div>`:'')
   : '<div class=crwait>— this tray is empty —</div>';
}

async function reviewCuttingSet(rule,title){
 const d=await jget('/api/cutting-room/ids?rule='+encodeURIComponent(rule));
 if(!d.ids||!d.ids.length){toast('nothing in this pile');return;}
 MODE='cand';updateModeBtn();           // the existing candidate review surface (rule badges, cand stats)
 reviewIds(d.ids,title,{onExit:()=>showCuttingRoom()});
}
async function renderFocus(){
 renderCrumb();
 if(window.resetCard)resetCard();
 if(fidx<0||fidx>=seq.length){$('#fstage').innerHTML='<div style="color:var(--mut);font-family:var(--mo)">— end of this view —</div>';$('#fmeta').innerHTML='';$('#fhpos').textContent='done';return;}
 const it=seq[fidx];
 $('#fstage').innerHTML=`<img id=fimg src="/thumb/${it.id}.jpg" onerror="this.replaceWith(phspin())">`+
  '<div class="fr cut" id=frcut>CUT</div><div class="fr keep" id=frkeep>KEEP</div><div class="fr skip" id=frskip>SKIP</div>';
 $('#fhpos').textContent=`${fidx+1} / ${seq.length}`;updateFocusState();
 const fb=$('#ffull');if(fb){if(it.is_video){fb.style.display='';fb.textContent='▶ play';}else if(window.LOCAL_FULLRES){fb.style.display='';fb.textContent='▶ full';}else fb.style.display='none';}
 window._liveMov=null;window._livePlaying=false;{const lb=$('#flive');if(lb){lb.style.display='none';lb.classList.remove('on');}}   // reset loop on every nav; renderMeta re-shows if Live
 {const vb=$('#fvault');if(vb){const on=it.vaulted;vb.textContent=on?'🔓 un-vault':'🔒 vault';vb.classList.toggle('on',!!on);}}
 {const eb=$('#fedited');if(eb){const show=!it.is_video&&isEditedAsset(it);eb.style.display=show?'':'none';if(!show)window._focusOriginal=false;eb.classList.toggle('on',!!window._focusOriginal);eb.textContent=window._focusOriginal?'original':'🎨 edited';}}
 // progressive: once you settle on a PHOTO, swap the soft thumb for a crisp high-res preview
 if(window._pvt)clearTimeout(window._pvt);
 if(!it.is_video){const wantId=it.id;
  window._pvt=setTimeout(()=>{const pv=new Image();
   pv.onload=()=>{const im=$('#fimg');if(im&&seq[fidx]&&seq[fidx].id===wantId)im.src=focusPreviewSrc(seq[fidx]);};
   pv.src=focusPreviewSrc(it);},350);}   // 350ms debounce: skip photos swiped straight past
 const d=await jget('/api/item/'+it.id);
 if(seq[fidx]&&seq[fidx].id===it.id)renderMeta(d);
}
function toggleSig(){$('#focus').classList.toggle('nosig');}
// ◉ Live: play the bound motion clip as a LOOPING, muted, controls-less Live-Photo preview.
// Toggle off (or any frame nav, which rebuilds #fstage) returns to the static still.
function playLive(){
 if(!window._liveMov)return;
 const it=seq[fidx];if(!it)return;
 if(window._livePlaying){restoreStill(it);return;}            // toggle OFF -> static still
 $('#fstage').innerHTML=`<video src="/api/play/${window._liveMov}" muted loop autoplay playsinline preload=auto></video>`;
 window._livePlaying=true;const lb=$('#flive');if(lb)lb.classList.add('on');
}
function restoreStill(it){
 $('#fstage').innerHTML=`<img id=fimg src="${focusPreviewSrc(it)}" onerror="this.onerror=null;this.src='/thumb/${it.id}.jpg'">`+
  '<div class="fr cut" id=frcut>CUT</div><div class="fr keep" id=frkeep>KEEP</div><div class="fr skip" id=frskip>SKIP</div>';
 window._livePlaying=false;const lb=$('#flive');if(lb)lb.classList.remove('on');
}
// percentile bar fill: keep-green near the top, cut-red near the bottom, amber between
function pcolor(p){return p>=80?'var(--keep)':p<=20?'var(--cut)':'var(--amber)';}
function ordp(n){const s=['th','st','nd','rd'],v=n%100;return n+(s[(v-20)%10]||s[v]||s[0]);}   // 1st/2nd/3rd; 11–13 → th (the IIFE's ord() is out of scope here)
function scoreBar(nm,o,fmt,na){
 if(!o||o.pct==null)return `<div class=score><div class=top><span class=nm>${nm}</span><span class=na>${na||'— no Apple data'}</span></div></div>`;
 return `<div class=score><div class=top><span class=nm>${nm}</span><span><span class=vl>${fmt(o.value)}</span><span class=pc>${ordp(o.pct)} pct</span></span></div>`
  +`<div class=pbar><i style="width:${o.pct}%;background:${pcolor(o.pct)}"></i></div></div>`;
}
function lchips(arr){return arr.map(l=>`<span class=lchip>${esc(l.term)}${l.score>0?`<b>${l.score.toFixed(2)}</b>`:''}</span>`).join('');}
function renderMeta(d){
 const c=d.context||{}, s=d.signal||{}, sc=s.scores||{}, ct=s.content||{};
 let h='';
 if(MODE==='cand'&&d.driver)h+=`<div class=drv style="background:${RULECOL[d.rule]||'#BA7517'}">${esc(d.driver)}</div>`;
 // Live Photo: enable the motion-clip play button + mark the panel
 if(d.live){window._liveMov=d.live.mov_id;const lb=$('#flive');if(lb)lb.style.display='';
  h+=`<div class=sdtag style="background:#2a2015;border-color:var(--amber);color:#f0d9b3" title="motion clip bound to this still">◉ Live Photo — ▶ Live plays the motion</div>`;}

 // ---- Scores: value + library-relative percentile bar ----
 h+=`<div class=sig><div class=sig-h>Scores</div>`;
 h+=scoreBar('aesthetic (Apple)',sc.aesthetic,v=>v.toFixed(3));
 h+=scoreBar('sharpness',sc.sharpness,v=>Math.round(v).toLocaleString(),'— not analyzed');
 h+=`</div>`;

 // ---- Content: scene/object labels + the screenshot/document cull-tell ----
 h+=`<div class=sig><div class=sig-h>Content</div>`;
 if(!s.apple){h+=`<div class=na>— no Apple data</div>`;}
 else{
  if(s.screenshot)h+=`<div class=sdtag>▣ screenshot / document</div>`;
  if(ct.scene&&ct.scene.length)h+=`<div>${lchips(ct.scene)}</div>`;
  const extra=[['food',ct.food],['landmark',ct.landmark],['species',ct.species],['document',ct.document]];
  extra.forEach(([nm,arr])=>{if(arr&&arr.length)h+=`<div class=lgrp>${nm}</div><div>${lchips(arr)}</div>`;});
  if(ct.ocr_count)h+=`<div class=lgrp>${ct.ocr_count} OCR text token${ct.ocr_count>1?'s':''}</div>`;
  if(!(ct.scene&&ct.scene.length)&&!ct.ocr_count&&!s.screenshot)h+=`<div class=na>(no labels)</div>`;
 }
 h+=`</div>`;

 // ---- People: chip per named person; protected get the amber ring ----
 h+=`<div class=sig><div class=sig-h>People</div>`;
 if(!s.apple){h+=`<div class=na>— no Apple data</div>`;}
 else if(s.persons&&s.persons.length){h+=s.persons.map(p=>`<span class="pchip ${p.protected?'prot':''}">${esc(p.name)}</span>`).join('');}
 else{h+=`<div class=na>(none named)</div>`;}
 h+=`</div>`;

 // ---- Frame facts: project-side, present for every asset ----
 h+=`<div class=sig><div class=sig-h>Frame facts</div>`;
 h+=`<div class=ctx><span class=wd>${esc(c.weekday||'')}</span> ${esc(c.when||'')}`;
 if(c.place)h+=`<span class=pl>📍 ${esc(c.place)}</span>`;
 h+=`</div>`;
 h+='<div class=exif>'+d.exif.map(e=>`<span class=k>${esc(e[0])}</span><span>${esc(e[1])}</span>`).join('')+'</div>';
 if(d.hints&&d.hints.length)h+='<div class=hints>'+d.hints.map(e=>`${esc(e[0])}: ${esc(e[1])}`).join(' · ')+'</div>';
 if(d.gps)h+=`<div class=gps>📍 ${d.gps.city?esc(d.gps.city):'(uncoded)'} <span class=coord><a href="${d.gps.map}" target=_blank rel=noopener>${d.gps.lat}, ${d.gps.lon}</a></span></div>`;
 h+=`<details class=exp><summary>Full EXIF</summary><div class=exif style="margin-top:6px">`+d.full.map(e=>`<span class=k>${esc(e[0])}</span><span style="word-break:break-all">${esc(e[1])}</span>`).join('')+`</div></details>`;
 h+=`</div>`;
 $('#fmeta').innerHTML=h;
}
function updateFocusState(){const it=seq[fidx];$('#fhstate').textContent=it?it.state:'';}
function focusNext(){fidx++;if(fidx>=seq.length){renderFocus();toast('reviewed all in this view');return;}renderFocus();}
function focusPrev(){if(fidx>0){fidx--;renderFocus();}}
// Real undo (audit 9.8 wires two-finger tap to this, and the help text has always
// advertised "U undo").
//
// It did not undo. focusDecide passes advance=true, so X cuts the current frame and
// steps fidx forward; the old 'u' handler then ran decide([seq[fidx]],'undecided') on
// the frame you had ALREADY moved to. That frame is normally still undecided, so the
// keystroke was a no-op with a plausible-looking vibrate, and the frame you actually
// cut stayed cut. The one case where it was worse than a no-op: on an already-decided
// next frame it silently discarded THAT decision instead.
//
// So remember the frame and the state it held before the decision, and put both back.
let lastDec=null;
function focusDecide(state){
 const it=seq[fidx];if(!it)return;
 lastDec={id:it.id,prev:it.state,idx:fidx};
 decide([it],state,true);
}
function undoLast(){
 if(!lastDec){toast('nothing to undo');return;}
 const {id,prev,idx}=lastDec;
 const it=byId[id]||seq[idx];
 if(!it){lastDec=null;toast('nothing to undo');return;}
 lastDec=null;                       // one level, and it is spent once used
 if(idx>=0&&idx<seq.length)fidx=idx; // step back to the frame being undone, so you see it
 decide([it],prev,false);
 renderFocus();
 toast('undone \u2014 back to '+prev);
}
function focusFlash(kind){const el=$('#fr'+kind);if(el){el.classList.add('show');setTimeout(()=>el.classList.remove('show'),190);}}
// Vault = a THIRD axis (visibility), orthogonal to keep/cut. Toggles mark/unmark; keep/cut untouched.
async function focusVault(){
 const it=seq[fidx];if(!it)return;
 const on=!it.vaulted;
 await fetch('/api/vault',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:it.id,action:on?'mark':'unmark'})});
 if(navigator.vibrate)navigator.vibrate(12);
 toast(on?'🔒 vaulted — hidden from all views':'🔓 returned to library');
 const id=it.id;
 seq=seq.filter(x=>x.id!==id);delete byId[id];        // gone from this sequence immediately
 if(SHEET&&SHEET.items)SHEET.items=SHEET.items.filter(x=>x.id!==id);
 if(day&&day.items)day.items=day.items.filter(x=>x.id!==id);
 TRIPS=null;PL=null;                                  // trips/map recompute without (or with) it
 if(fidx>=seq.length)fidx=seq.length-1;
 if(seq.length)renderFocus();else exitFocus();
 refreshStripAfterVault();
}
async function refreshStripAfterVault(){try{renderStrip(await jget('/api/stats'+(MODE==='cand'?'?mode=cand':'')));}catch(e){}}

// ===== Vault view (gated, opt-in surface; shows ONLY vaulted items) =====
let VAULT_ITEMS=[];
// ===================== People (Faces, phase 1 — read-only) =====================
let PEOPLE=null, curPerson=null, curPid=null, curSug=null, sugThreshold=0.88, sugShowAll=false;
async function showPeople(){
 view='people';stopThumbPoll();closeFocus();placesReview=null;setNav('peopletog');
 closeOverlays('peopleview');   // also clears the nested #persondetail (in OVL) → people opens on the grid
 $('#peopleview').classList.add('on');renderCrumb();
 syncUrl('/people');
 if(!PEOPLE){
   $('#peoplegrid').style.display='';$('#peoplegrid').innerHTML='<div class=pdcap>Loading faces… (the first request builds the in-memory embedding index — a few seconds).</div>';
   try{PEOPLE=await jget('/api/people');}catch(e){$('#peoplegrid').innerHTML='<div class=pdcap>Failed to load people.</div>';return;}
 }
 renderPeople();
 loadNameBox();   // owner-only "people to name" strip — async, never blocks the grid
}
function renderPeople(){
 const d=PEOPLE,g=$('#peoplegrid');g.style.display='';$('#persondetail').classList.remove('on');
// 9.3's progress header. Two bars, not one: IDENTIFIED is faces attached to a named
// person -- work finished -- and CLUSTERED is faces merely grouped, which is the size of
// the queue still waiting. The audit quotes one figure as "identified"; it was actually
// the clustered share, and showing only that would overstate finished work by 2.3x.
{
 const t=d.total_faces||0, idn=d.identified_faces, cl=d.clustered_faces;
 const pct=n=>t&&n!=null?(100*n/t):null;
 const pi=pct(idn), pc=pct(cl);
 $('#peoplecount').innerHTML =
  `${d.people.length} people · ${t.toLocaleString()} faces detected`+
  (pi!=null?`<span class=peopleprog title="faces attached to a named person">`+
    `<span class=ppbar><i style="width:${pi.toFixed(1)}%"></i></span>`+
    `<b>${pi.toFixed(1)}%</b> identified</span>`:'')+
  (pc!=null?`<span class=peopleprog title="faces grouped into clusters, named or not — the triage queue">`+
    `<span class="ppbar dim"><i style="width:${pc.toFixed(1)}%"></i></span>`+
    `<b>${pc.toFixed(1)}%</b> clustered</span>`:'');
}
 g.innerHTML=d.people.map(p=>`<div class=pcard data-id="${p.person_id}">
   ${p.rep_face_id!=null?`<img class=pava loading=lazy src="/api/face/${p.rep_face_id}.jpg" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'pava none',textContent:'?'}))">`:`<div class="pava none">?</div>`}
   <div class=pname>${esc(p.name)}${p.is_protected?' 🔒':''}</div>
   <div class=pmeta><b>${p.known_faces}</b> faces${p.confirmed?' · '+p.confirmed+'✓':''} · ${p.apple_assets.toLocaleString()} Apple</div></div>`).join('');
 g.querySelectorAll('.pcard').forEach(el=>el.onclick=()=>openPerson(+el.dataset.id));
}
async function openPerson(pid){
 $('#peoplegrid').style.display='none';$('#namebox').style.display='none';
 const pd=$('#persondetail');pd.classList.add('on');window.scrollTo(0,0);
 curPid=pid;sugShowAll=false;
 $('#pdname').textContent='…';$('#pdcount').textContent='';$('#pdknowncount').textContent='';$('#pdsugmeta').textContent='';$('#pdsugctl').innerHTML='';
 $('#pdknown').innerHTML='';$('#pdsug').innerHTML='<div class=pdcap>Computing similarities…</div>';
 await loadPerson(pid);
}
async function loadPerson(pid){                          // (re)load gallery + suggestions (used for the snowball re-run too)
 const det=await jget('/api/person?id='+pid);curPerson=det;
 if(det.error){$('#pdsug').innerHTML='<div class=pdcap>'+esc(det.error)+'</div>';return;}
 $('#pdname').textContent=det.name+(det.is_protected?' 🔒':'');
 $('#pdcount').textContent=det.known_faces+' faces · '+det.confirmed+' confirmed · '+det.apple_assets.toLocaleString()+' Apple-tagged';
 $('#pdknowncount').textContent='('+det.shown+' of '+det.known_faces+')';
 $('#pdknown').innerHTML=det.faces.length?det.faces.map(gcell).join(''):'<div class=pdcap>No faces yet — no anchors to seed from.</div>';
 wireGallery($('#pdknown'));
 $('#pdsugctl').innerHTML='';$('#pdsug').innerHTML='<div class=pdcap>Computing similarities…</div>';
 const sug=await jget('/api/person/suggestions?id='+pid+'&k=150');curSug=sug;
 if(sug.note||!sug.suggestions||!sug.suggestions.length){
   $('#pdsugmeta').textContent='';$('#pdsug').innerHTML='<div class=pdcap>'+esc(sug.note||'No suggestions.')+'</div>';return;
 }
 renderSugCtl();renderSug();
}
function renderSugCtl(){
 $('#pdsugctl').innerHTML=`≥ <input id=sugthr type=number step=0.01 min=0 max=1 value="${sugThreshold.toFixed(2)}">`
   +`<button class=sugbulk id=sugbulkbtn>✓ confirm all above</button>`
   +`<button id=sugmorebtn></button>`
   +`<button onclick="loadPerson(curPid)" title="re-run with the expanded anchor set">↻ re-run</button>`;
 $('#sugthr').onchange=e=>{sugThreshold=Math.max(0,Math.min(1,parseFloat(e.target.value)||0.88));renderSug();};
 $('#sugbulkbtn').onclick=bulkConfirm;
 $('#sugmorebtn').onclick=()=>{sugShowAll=!sugShowAll;renderSug();};
}
function shownSug(){return sugShowAll?curSug.suggestions:curSug.suggestions.filter(f=>f.score>=sugThreshold);}
function renderSug(){
 const shown=shownSug(),total=curSug.suggestions.length,above=curSug.suggestions.filter(f=>f.score>=sugThreshold).length;
 $('#pdsugmeta').textContent=shown.length+' shown'+(sugShowAll?'':' ≥ '+sugThreshold.toFixed(2))+' of top '+total+' · '+curSug.anchors_used+'/'+curSug.anchors+' anchors';
 const mb=$('#sugmorebtn');if(mb)mb.textContent=sugShowAll?'show ≥ threshold':('show all '+total);
 const bb=$('#sugbulkbtn');if(bb)bb.textContent='✓ confirm all above ('+above+')';
 $('#pdsug').innerHTML=shown.length?shown.map(sugcell).join(''):'<div class=pdcap>None at this threshold — lower it or “show all”.</div>';
 wireSug($('#pdsug'));
}
function sugcell(f){
 const cls=f.score>=0.5?'hi':(f.score<0.35?'lo':'');
 return `<div class=fcell data-fid="${f.face_id}" data-asset="${f.asset_id}" data-score="${f.score}" title="asset #${f.asset_id} · face #${f.face_id}">
   <img loading=lazy src="/api/face/${f.face_id}.jpg" onerror="this.closest('.fcell').style.display='none'">
   <div class="fsc ${cls}">${f.score.toFixed(3)}</div>
   <div class=facts><button class=fy title="Confirm">✓</button><button class=fn title="Not this person">✕</button></div></div>`;
}
function gcell(f){
 return `<div class="fcell${f.confirmed?' confirmed':''}" data-asset="${f.asset_id}" title="asset #${f.asset_id} · face #${f.face_id}${f.confirmed?' · confirmed':' · seed'}">
   <img loading=lazy src="/api/face/${f.face_id}.jpg" onerror="this.closest('.fcell').style.display='none'">
   <div class="fsc">${f.confirmed?'✓':'det '+(f.det_score!=null?f.det_score.toFixed(2):'')}</div></div>`;
}
function wireGallery(root){root.querySelectorAll('.fcell img').forEach(im=>im.onclick=()=>reviewIds([+im.closest('.fcell').dataset.asset],'face'));}
function wireSug(root){root.querySelectorAll('.fcell').forEach(el=>{
 const fid=+el.dataset.fid;
 el.querySelector('img').onclick=()=>reviewIds([+el.dataset.asset],'face');
 el.querySelector('.fy').onclick=e=>{e.stopPropagation();confirmFaces([{face_id:fid,score:+el.dataset.score}]);};
 el.querySelector('.fn').onclick=e=>{e.stopPropagation();rejectFaces([fid]);};
});}
async function confirmFaces(list){
 const r=await fetch('/api/person/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({person_id:curPid,faces:list})}).then(x=>x.json()).catch(()=>({error:'request failed'}));
 if(r.error){toast(r.error);return;}
 const ids=new Set(list.map(f=>f.face_id));curSug.suggestions=curSug.suggestions.filter(f=>!ids.has(f.face_id));
 PEOPLE=null;toast('✓ confirmed '+(r.confirmed||0)+' · '+esc(curPerson.name));renderSug();
}
async function rejectFaces(list){
 const r=await fetch('/api/person/reject',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({person_id:curPid,face_ids:list})}).then(x=>x.json()).catch(()=>({error:'request failed'}));
 if(r.error){toast(r.error);return;}
 const ids=new Set(list);curSug.suggestions=curSug.suggestions.filter(f=>!ids.has(f.face_id));
 toast('✕ rejected '+(r.rejected||0));renderSug();
}
async function bulkConfirm(){
 const list=curSug.suggestions.filter(f=>f.score>=sugThreshold).map(f=>({face_id:f.face_id,score:f.score}));
 if(!list.length){toast('nothing at/above '+sugThreshold.toFixed(2));return;}
 if(!confirm('Confirm '+list.length+' face(s) ≥ '+sugThreshold.toFixed(2)+' as '+curPerson.name+'?'))return;
 const r=await fetch('/api/person/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({person_id:curPid,faces:list})}).then(x=>x.json()).catch(()=>({error:'request failed'}));
 if(r.error){toast(r.error);return;}
 PEOPLE=null;toast('✓ confirmed '+(r.confirmed||0)+' · re-running with expanded anchors');
 await loadPerson(curPid);                               // snowball: confirmed angles now pull in more
}

// ---- People to name (stage 1b): ranked unnamed clusters → name → local person ----
// ---- D7 / audit 9.3: cluster triage --------------------------------------------
// "Cluster triage mode: one cluster at a time -- 12 exemplar crops in a sheet, big
// 'same person?' prompt; K confirm-merge into suggested person, X not-them, -> skip.
// Same muscle memory as photo culling (the app has ONE gesture language)."
//
// The name box shows the same clusters as a GRID of cards, each with four crops and its
// own text field. That is a form to fill in, not a triage loop: every card asks you to
// decide and to type, nothing is ever the current one, and there is no way to say "not
// now" without losing your place. Triage is the same data as a queue -- one cluster,
// twelve faces, three keys.
//
// K is 9.3's "confirm-merge into suggested person" ONLY where the suggestion earns it.
// candidates() returns a per-cluster suggest {name, score, confident} -- nearest named
// person by centroid -- and measured on this library that field runs 0.047 to 0.546,
// with only about 3 in 100 clearing the provisional 0.45 bar. Most unnamed clusters are
// simply nobody in the named list, so a blanket "same person?" prompt would be wrong
// most of the time and would train K as a reflex to agree.
//
// So the prompt states what is actually known. With a confident suggestion the question
// names the person and K merges into them. Without one, K means "yes, one person" and
// opens the naming field. Same key, and it never asserts more than the score supports.
let TR=null, TRI=0;

async function openTriage(){
 if(!window.LOCAL_FULLRES){toast('owner only');return;}
 if(!NB||!NB.candidates||!NB.candidates.length){toast('nothing to triage at this bar');return;}
 TR=NB.candidates.slice(); TRI=0;
 closeOverlays('triageview');
 $('#triageview').classList.add('on');
 renderTriage();
}
function closeTriage(){
 $('#triageview').classList.remove('on');
 TR=null;
 loadNameBox();          // fold whatever was decided back into the grid behind
}
function triageOpen(){return $('#triageview')&&$('#triageview').classList.contains('on');}

function renderTriage(){
 const body=$('#trbody'); if(!body)return;
 if(!TR||TRI>=TR.length){
  $('#trcount').textContent='';
  body.innerHTML='<div class=trdone><b>Nothing left in this queue.</b>'+
    '<div>Every cluster at this bar has been named, dismissed or skipped.</div>'+
    '<button class=trgo onclick="closeTriage()">Back to People</button></div>';
  return;
 }
 const c=TR[TRI];
 $('#trcount').textContent=(TRI+1)+' of '+TR.length;
 const kh=$('#trkeys');
 if(kh)kh.textContent=(c.suggest&&c.suggest.confident)
   ? 'K yes, add to '+c.suggest.name+' · X not a person · → skip · Esc leave'
   : 'K same person · X not a person · → skip · Esc leave';
 // 9.3 asks for twelve exemplars; clusters.rep_face_ids holds four, which is enough to
 // notice a face and not enough to judge whether a cluster is ONE face. The full sheet
 // is fetched from cluster_faces below; the four rep ids are the first paint so the card
 // is never empty.
 const faces=(c.rep_face_ids||[]).slice(0,12);
 const sg=c.suggest&&c.suggest.confident?c.suggest:null;
 body.innerHTML=`<div class=trcard data-cid="${c.cluster_id}">
   <div class=trask>${sg?`Is this ${escHtml(sg.name)}?`:'Same person?'}</div>
   ${sg?`<div class=trsug>nearest named person · similarity <b>${sg.score.toFixed(2)}</b>
      <span class=trloose>(provisional bar 0.45 — uncalibrated)</span></div>`:''}
   <div class=trstat><b>${c.n_assets.toLocaleString()}</b> photos · across <b>${c.n_days.toLocaleString()}</b> days
     · ${escHtml(c.first_day||'?')} → ${escHtml(c.last_day||'?')}</div>
   <div class=trsheet>${faces.map(f=>`<img loading=lazy src="/api/face/${f}.jpg" alt=""
      onerror="this.style.visibility='hidden'">`).join('')}</div>
   <div class=trnamebox id=trname>
     <input type=text id=trinput placeholder="Who is this?" maxlength=80 autocomplete=off>
     <div class=nbac id=trac></div>
     <button class="trgo nbgo" id=trgo>Name</button>
     <button class=trcancel id=trcancel>cancel</button>
   </div>
   <div class=trerr id=trerr></div>
 </div>`;
 wireTriage(c);
 loadTriageSheet(c);
}

// The twelve exemplars, and the cohesion figure that says how tight the cluster is.
// Fetched per card rather than bundled into /api/people/candidates, which the name-box
// grid also uses and which would otherwise carry twelve ids for every one of 25 cards.
async function loadTriageSheet(c){
 const cid=c.cluster_id;
 let d=null;
 try{ d=await jget('/api/cluster/faces?cluster_id='+cid+'&k=12'); }catch(e){ return; }
 if(!d||!d.faces||!d.faces.length)return;
 const card=$('.trcard'); if(!card||+card.dataset.cid!==cid)return;   // a faster key won
 const sheet=card.querySelector('.trsheet');
 if(sheet)sheet.innerHTML=d.faces.map(f=>`<img loading=lazy src="/api/face/${f}.jpg" alt=""
   onerror="this.style.visibility='hidden'">`).join('');
 const st=card.querySelector('.trstat');
 if(st&&typeof d.cohesion==='number'){
  // Cohesion is shown rather than used to reorder the queue: candidates() ranks by
  // recurrence (n_days, then n_assets) and documents why -- raw face counts are
  // burst-inflated -- and silently changing that order would also change the name-box
  // grid, which reads the same endpoint.
  st.insertAdjacentHTML('beforeend',
    ` · cohesion <b>${d.cohesion.toFixed(2)}</b> <span class=trloose>(${d.loosest.toFixed(2)} at the edge)</span>`);
 }
}

function triageAdvance(){ TRI++; renderTriage(); }

function wireTriage(c){
 const wrap=$('#trname'), inp=$('#trinput'), go=$('#trgo');
 wrap.classList.remove('on');
 go.onclick=async()=>{
  const v=(inp.value||'').trim();
  if(!v){inp.focus();return;}
  go.disabled=true;
  const m=nbMatch(v);
  const url=m?'/api/person/assign_cluster':'/api/person/create';
  const payload=m?{person_id:m.person_id,cluster_id:c.cluster_id}
                 :{name:v,cluster_id:c.cluster_id};
  let r;
  try{ r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(payload)}).then(x=>x.json()); }
  catch(e){ r={error:'request failed'}; }
  go.disabled=false;
  if(r&&r.error){$('#trerr').textContent=r.error;return;}
  toast((m?'added to ':'named ')+v);
  PEOPLE=null;                       // the named list changed
  triageAdvance();
 };
 $('#trcancel').onclick=()=>{wrap.classList.remove('on');$('#trerr').textContent='';};
 inp.oninput=()=>{nbSync(wrap);nbAcRender(wrap);};
 inp.onkeydown=e=>{
  e.stopPropagation();                            // the triage keys must not fire while typing
  if(e.key==='Enter'){e.preventDefault();go.click();}
  if(e.key==='Escape'){e.preventDefault();wrap.classList.remove('on');}
 };
}

async function triageName(){
 const c=TR&&TR[TRI]; if(!c)return;
 const sg=c.suggest&&c.suggest.confident?c.suggest:null;
 if(sg){
  // 9.3's confirm-merge, on the ~3% of clusters where the suggestion clears the bar.
  let r;
  try{ r=await fetch('/api/person/assign_cluster',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({person_id:sg.person_id,cluster_id:c.cluster_id})})
        .then(x=>x.json()); }
  catch(e){ r={error:'request failed'}; }
  if(r&&r.error){const e=$('#trerr');if(e)e.textContent=r.error;return;}
  toast('added to '+sg.name);
  PEOPLE=null;
  triageAdvance();
  return;
 }
 const wrap=$('#trname'); if(!wrap)return;
 wrap.classList.add('on');
 const i=$('#trinput'); if(i)i.focus();
}
async function triageDismiss(){
 const c=TR&&TR[TRI]; if(!c)return;
 const cid=c.cluster_id;
 triageAdvance();                    // advance first: the decision is made, do not wait on the round trip
 try{
  await fetch('/api/people/candidates/dismiss',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:cid})});
 }catch(e){ toast('could not dismiss — it will reappear'); }
}

// Owner-only surface on the LAN-gated /api/people/candidates + /api/person/create
// backend. Naming a cluster creates persons(source='local') + bulk assignments;
// the card resolves and the new person appears in the named grid below.
let NB=null, nbStrict=false, nbLimit=25;
async function loadNameBox(){
 const box=$('#namebox');
 if(!window.LOCAL_FULLRES){box.style.display='none';return;}   // guests never see unnamed strangers
 try{NB=await jget('/api/people/candidates?strict='+(nbStrict?1:0)+'&limit='+nbLimit);}
 catch(e){box.style.display='none';return;}
 if(!NB||NB.error||NB.note){box.style.display='none';return;}  // gated off or no cluster store
 renderNameBox();
}
function renderNameBox(){
 const box=$('#namebox');if(!NB)return;
 const cs=NB.candidates||[];
 if(!NB.total&&!nbStrict){box.style.display='none';return;}   // nothing left at the default bar — stay out of the way
 box.style.display='';
 $('#nbmeta').textContent=cs.length+' of '+NB.total+' unnamed · bar ≥'+NB.min_assets+' photos & ≥'+NB.min_days+' days';
 $('#nbctl').innerHTML=
   (cs.length?`<button id=nbtriage class=amber>▶ triage ${cs.length} one at a time</button>`:'')
   +`<button id=nbstrict>${nbStrict?'widen: ≥10 photos & ≥5 days':'narrow: ≥20 photos & ≥10 days'}</button>`
   +(NB.total>cs.length?`<button id=nbmore>show all ${Math.min(NB.total,100)}</button>`:'');
 const tg=$('#nbtriage');if(tg)tg.onclick=openTriage;
 const st=$('#nbstrict');if(st)st.onclick=()=>{nbStrict=!nbStrict;nbLimit=25;loadNameBox();};
 const mo=$('#nbmore');if(mo)mo.onclick=()=>{nbLimit=100;loadNameBox();};
 $('#nbgrid').innerHTML=cs.length?cs.map(nbcard).join('')
   :'<div class=pdcap>No unnamed clusters at this bar — widen it, or everyone recurring is already named.</div>';
 wireNameBox();
}
function nbcard(c){
 return `<div class=nbcard data-cid="${c.cluster_id}">
   <div class=nbfaces>${c.rep_face_ids.slice(0,4).map(f=>`<img loading=lazy src="/api/face/${f}.jpg" title="face #${f}" onerror="this.style.visibility='hidden'">`).join('')}</div>
   <div class=nbstat><b>${c.n_assets.toLocaleString()}</b> photos · across <b>${c.n_days.toLocaleString()}</b> days</div>
   <div class=nbspan>${c.first_day||'?'} → ${c.last_day||'?'}</div>
   <div class=nbrow><input type=text placeholder="Who is this?" maxlength=80 autocomplete=off><div class=nbac></div><button class=nbgo>Name</button></div>
   <div class=nberr></div>
   <button class=nbskip title="Not a person / don't suggest again. Hides this cluster only — no photo or face is touched.">✕ not a person</button></div>`;
}
// ---- stage 1c: autocomplete over existing person names → add-to vs create ----
// Typing filters the named-people list; picking (or exactly typing) an existing
// name flips the button to "Add to [Name]" → /api/person/assign_cluster folds
// the cluster into that person. A novel name keeps "Name" → /api/person/create.
function nbNames(){return ((PEOPLE&&PEOPLE.people)||[]).map(p=>({person_id:p.person_id,name:p.name}));}
function nbMatch(v){v=(v||'').trim().toLowerCase();return v?nbNames().find(p=>p.name.toLowerCase()===v)||null:null;}
function nbSync(el){
 const go=el.querySelector('.nbgo');if(go.disabled)return;
 const m=nbMatch(el.querySelector('input').value);
 go.textContent=m?'Add to '+m.name:'Name';
}
function nbAcHide(el){const ac=el.querySelector('.nbac');if(ac)ac.style.display='none';}
function nbAcRender(el){
 const inp=el.querySelector('input'),ac=el.querySelector('.nbac');
 const v=inp.value.trim().toLowerCase();
 let ns=nbNames();
 if(v)ns=ns.filter(p=>p.name.toLowerCase().includes(v))
   .sort((a,b)=>(b.name.toLowerCase().startsWith(v)-a.name.toLowerCase().startsWith(v))||a.name.localeCompare(b.name));
 ns=ns.slice(0,12);
 ac.innerHTML=ns.map(p=>`<div class=nbacit>${esc(p.name)}</div>`).join('');
 ac.style.display=ns.length?'block':'none';
 ac.querySelectorAll('.nbacit').forEach(it=>{
  it.onmousedown=e=>{e.preventDefault();inp.value=it.textContent;nbAcHide(el);nbSync(el);};
 });
}
function nbAcMove(el,d){
 const ac=el.querySelector('.nbac');
 if(ac.style.display==='none')return;
 const its=[...ac.querySelectorAll('.nbacit')];if(!its.length)return;
 let i=its.findIndex(x=>x.classList.contains('on'));
 its.forEach(x=>x.classList.remove('on'));
 i=i<0?(d>0?0:its.length-1):(i+d+its.length)%its.length;
 its[i].classList.add('on');its[i].scrollIntoView({block:'nearest'});
}
function wireNameBox(){
 $('#nbgrid').querySelectorAll('.nbcard').forEach(el=>{
  const cid=+el.dataset.cid,inp=el.querySelector('input');
  el.querySelector('.nbgo').onclick=()=>nameCluster(el,cid);
  inp.oninput=()=>{nbAcRender(el);nbSync(el);};
  inp.onfocus=()=>nbAcRender(el);
  inp.onblur=()=>setTimeout(()=>nbAcHide(el),150);
  inp.onkeydown=e=>{
   if(e.key==='ArrowDown'){e.preventDefault();nbAcMove(el,1);return;}
   if(e.key==='ArrowUp'){e.preventDefault();nbAcMove(el,-1);return;}
   if(e.key==='Escape'){nbAcHide(el);return;}
   if(e.key==='Enter'){
    const on=el.querySelector('.nbac .nbacit.on');
    if(on&&el.querySelector('.nbac').style.display!=='none'){inp.value=on.textContent;nbAcHide(el);nbSync(el);return;}
    nameCluster(el,cid);
   }
  };
  el.querySelector('.nbskip').onclick=()=>dismissCluster(el,cid);
 });
}
function nbDrop(cid){NB.candidates=NB.candidates.filter(c=>c.cluster_id!==cid);NB.total--;renderNameBox();}
async function nameCluster(el,cid){
 const inp=el.querySelector('input'),go=el.querySelector('.nbgo'),err=el.querySelector('.nberr');
 const name=(inp.value||'').trim();err.textContent='';
 if(!name){err.textContent='enter a name first';inp.focus();return;}
 if(go.disabled)return;
 nbAcHide(el);
 const m=nbMatch(name);                                // existing person → fold, else create
 go.disabled=true;go.textContent='…';
 const url=m?'/api/person/assign_cluster':'/api/person/create';
 const payload=m?{person_id:m.person_id,cluster_id:cid}:{name,cluster_id:cid};
 const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(x=>x.json()).catch(()=>({error:'request failed'}));
 go.disabled=false;nbSync(el);
 if(r.error){err.textContent=r.error;return;}          // guard errors (dup name / claimed cluster) inline
 if(r.already_named){nbDrop(cid);toast('already named — refreshed');return;}
 nbDrop(cid);
 toast(m?'✓ added '+r.faces_assigned+' faces to '+esc(r.name)
        :'✓ '+esc(r.name)+' created · '+r.faces_assigned+' faces');
 PEOPLE=null;                                          // person joins/updates the named grid below
 try{PEOPLE=await jget('/api/people');renderPeople();}catch(e){}
}
async function dismissCluster(el,cid){
 const sk=el.querySelector('.nbskip');if(sk.disabled)return;sk.disabled=true;
 const r=await fetch('/api/people/candidates/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cluster_id:cid})}).then(x=>x.json()).catch(()=>({error:'request failed'}));
 sk.disabled=false;
 if(r.error){el.querySelector('.nberr').textContent=r.error;return;}
 nbDrop(cid);toast('✕ hidden as not-a-person · no photos affected');
}

async function showVault(){
 if(!window.LOCAL_FULLRES){location.replace('/');return;}   // owner-only — never render for a guest
 view='vault';stopThumbPoll();closeFocus();placesReview=null;setNav('vaulttog');
 closeOverlays('vaultview');
 $('#vaultview').classList.add('on');renderCrumb();
 syncUrl('/vault');   // sync band (hides it on this overlay)
 VAULT_ITEMS=(await jget('/api/vault_items')).items||[];
 renderVault();
}
function renderVault(){
 const g=$('#vaultgrid');
 $('#vaultcount').textContent=VAULT_ITEMS.length+' item'+(VAULT_ITEMS.length===1?'':'s');
 if(!VAULT_ITEMS.length){g.innerHTML='<div class=vempty>Nothing vaulted. In review, press <b>V</b> or tap 🔒 vault to mark an item personal — it hides from every view and lives only here.</div>';return;}
 g.innerHTML=VAULT_ITEMS.map((it,i)=>`<div class="tile" data-i="${i}">
   <img loading=lazy src="/thumb/${it.id}.jpg" onerror="this.replaceWith(phspin())">
   <button class=unvault data-u="${it.id}" title="Return to library">⤴ un-vault</button>
   <div class=tcap>#${it.id} · ${esc(it.ext)}</div></div>`).join('');
 g.querySelectorAll('.tile').forEach(el=>el.querySelector('img').onclick=()=>enterReview(VAULT_ITEMS,'vault',+el.dataset.i,()=>showVault()));
 g.querySelectorAll('.unvault').forEach(b=>b.onclick=async e=>{e.stopPropagation();
   await fetch('/api/vault',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:+b.dataset.u,action:'unmark'})});
   VAULT_ITEMS=VAULT_ITEMS.filter(x=>x.id!==+b.dataset.u);TRIPS=null;PL=null;SHEET=null;
   toast('🔓 returned to library');renderVault();refreshStripAfterVault();});
}

// ===== Search (Stage 5 4c-ii: text -> SigLIP2 NN -> lean items, flag-gated) =====
let searchState={items:[]};
async function showSearch(){
 view='search';stopThumbPoll();closeFocus();placesReview=null;setNav('searchtog');
 closeOverlays('searchview');
 $('#searchview').classList.add('on');renderCrumb();
 const q=$('#searchq'); if(q)q.focus();
 loadZS();
}
let _sT;
$('#searchq').oninput=()=>{clearTimeout(_sT);_sT=setTimeout(runSearch,250);};
$('#searchallcb').onchange=runSearch;
// ---- D9 / audit 9.5 + C4: zero-shot subtract chips ------------------------------
// "precomputed prompt set surfaces as toggle chips on the sheet toolbar (screenshots ·
// documents · food · sunsets) -- one tap subtracts the junk categories from view. The
// chip IS the filter state (no separate filter panel)."
//
// Membership is static between runs of tools/zero_shot.py, so it loads once and is kept
// as Sets: the subtract runs on every re-render and an array scan per frame per category
// would be the slowest thing on the sheet.
let ZS=null, ZSSET=null, ZSOFF=new Set();

async function loadZS(){
 if(ZS)return ZS;
 try{ const r=await jget('/api/zeroshot'); ZS=r&&!r.disabled?r:{cats:{},counts:{}}; }
 catch(e){ ZS={cats:{},counts:{}}; }
 ZSSET={};
 for(const k in (ZS.cats||{})) ZSSET[k]=new Set(ZS.cats[k]);
 return ZS;
}

// ---- D9 / audit 9.5 + C4: more like this ---------------------------------------
// "'More like this' (C4) from any frame's context menu / S in focus -- the asset's own
// embedding as query; results carry a similarity ring (conic, accent) so the falloff is
// visible."
//
// S was advertised in the help text as "skip" and was not bound to anything: there is no
// k==='s' branch in the focus handler, so pressing it did nothing at all. Skip already
// has two real bindings (arrow-right, and swipe up), so S goes to the thing 9.5 asks it
// to do, and the help text is corrected to match.
let SIMOF=null;

async function moreLikeThis(){
 const it=seq[fidx];
 if(!it){toast('no frame here');return;}
 toast('finding frames like this one…');
 let r;
 try{ r=await jget('/api/similar?id='+it.id+'&k=60'); }
 catch(e){ toast('could not search'); return; }
 if(r.disabled){toast("search isn't enabled");return;}
 const items=r.items||[];
 if(!items.length){toast('no near neighbours for this frame');return;}
 closeFocus();
 await showSearch();
 const q=$('#searchq'); if(q)q.value='';
 SIMOF=it.id; SHELF='frames';
 searchState={items};
 const st=$('#searchstatus');
 if(st)st.textContent=items.length+' frames like #'+it.id;
 renderShelves('like #'+it.id,[],[],items,false);
}

// ---- D9 / audit 9.5: three shelves ---------------------------------------------
// "it fans results into three labeled shelves: Places/Trips (name match), People
// (person match), Frames (SigLIP2 semantic). Shelves render as horizontal sheet
// strips; Enter on a shelf expands it to a full sheet with the query pinned as a
// filter chip."
//
// Search returned one flat grid of semantic frame hits. A query that was plainly a
// place or a person -- "baird", "edgar" -- answered only with frames the model thought
// looked like the words, and the trip and the person themselves were unreachable from
// the one input the app is supposed to search from.
let SHELF=null;                     // null = all three strips; else the expanded shelf

function shelfMatches(kinds,q){
 if(!q)return [];
 const ql=q.toLowerCase().replace(/^[@#]/,'');
 return PAL.filter(r=>kinds.includes(r.kind))
   .map(r=>({r,sc:palScore(ql,r.name)}))
   .filter(x=>x.sc>=0)
   .sort((a,b)=>b.sc-a.sc)
   .map(x=>x.r);
}

async function runSearch(){
 const v=(($('#searchq')||{}).value||'').trim();
 const status=$('#searchstatus'), grid=$('#searchgrid');
 if(!v){SHELF=null;searchState={items:[]};if(grid)grid.innerHTML='';if(status)status.textContent='';return;}

 // the name shelves need the same registry the palette uses
 if(!PAL.length||!PAL.some(r=>r.kind==='trip')){
  await Promise.all([palLoadPeople(),palLoadTrips()]);
  PAL=palSources();
 }
 const places=shelfMatches(['trip','place'],v);
 const people=shelfMatches(['person'],v);

 const all=($('#searchallcb')||{}).checked?'&all=1':'';
 let frames=[],disabled=false;
 try{
  const r=await jget('/api/search?q='+encodeURIComponent(v.replace(/^[@#]/,''))+'&k=60'+all);
  disabled=!!r.disabled; frames=r.items||[];
 }catch(e){ frames=[]; }
 searchState={items:frames};

 if(status)status.textContent=disabled?"search isn't enabled"
   :`${places.length} place${places.length===1?'':'s'} · ${people.length} ${people.length===1?'person':'people'} · ${frames.length} frame${frames.length===1?'':'s'}`;
 renderShelves(v,places,people,frames,disabled);
}

function renderShelves(q,places,people,frames,disabled){
 const grid=$('#searchgrid');if(!grid)return;
 const frames0=frames;          // unfiltered, so un-pressing a chip restores its frames
 const STRIP=12;                    // a strip is a look, not the whole shelf
 const chip=`<span class=shelfchip>${escHtml(q)}<button class=chipx aria-label="clear filter">✕</button></span>`;

 // toolbar is rendered ABOVE the body and outside the empty branch. It has to be:
 // the subtract chips live there, and a query whose every result is screenshots
 // subtracts to zero -- at which point the shelf renders its empty state, the chip row
 // goes with it, and there is no way left to press the chip again. The filter UI has to
 // survive its own filter.
 const shelf=(key,label,n,body,empty,toolbar)=>{
  const open=SHELF===key;
  if(SHELF&&!open)return '';
  return `<section class="shelf${open?' open':''}" data-shelf="${key}">
    <header class=shelfhd tabindex=0 role=button aria-expanded="${open}"
            aria-label="${escHtml(label)}, ${n} result${n===1?'':'s'}. Enter to ${open?'collapse':'expand'}.">
      <h3>${escHtml(label)}</h3><span class=shelfn>${n.toLocaleString()}</span>
      ${open?chip:''}
      <span class=shelfx>${open?'collapse ↙':'expand ↗'}</span>
    </header>
    ${toolbar||''}
    ${n?`<div class="${open?'shelfsheet':'shelfstrip'}">${body}</div>`
       :`<div class=shelfempty>${escHtml(empty)}</div>`}
  </section>`;
 };

 // The chip IS the filter state: engaged means that category is subtracted. The count
 // shown is how many of THESE results the chip removes, not the library-wide total --
 // a chip offering to remove 10,152 screenshots from a 60-frame sheet would be a lie
 // about what pressing it does.
 const zschips=()=>{
  const cats=Object.keys((ZS&&ZS.counts)||{}).sort();
  if(!cats.length)return '';
  return '<div class=zsbar>'+cats.map(c=>{
   const n=zsHits[c]||0;
   const off=ZSOFF.has(c);
   return `<button class="zschip${off?' off':''}${n||off?'':' none'}" data-cat="${escHtml(c)}"
     aria-pressed="${off}" title="${off?'show':'hide'} ${escHtml(c)}">`+
     `${off?'+ ':'− '}${escHtml(c)}<span class=zsn>${n.toLocaleString()}</span></button>`;
  }).join('')+'</div>';
 };

 const nameCard=r=>`<button class=shelfcard data-kind="${r.kind}" data-name="${escHtml(r.name)}">
   <span class=scname>${escHtml(r.name)}</span><span class=schint>${escHtml(r.hint||'')}</span></button>`;

 const pl=(SHELF==='places'?places:places.slice(0,STRIP)).map(nameCard).join('');
 const pe=(SHELF==='people'?people:people.slice(0,STRIP)).map(nameCard).join('');
 // The similarity ring is RANK-relative, not absolute. Measured over a real query:
 // the 59 neighbours of #18085 span cosine 0.8916 to 0.9238 -- a spread of 0.032 -- so
 // an absolute ring is a nearly-full circle on every result and shows no falloff at all,
 // which is the one thing 9.5 asks it to show. The exact cosine is on the title.
 // subtract whatever chips are engaged, before anything is measured or drawn
 const zsHits={};
 if(ZSSET)for(const c in ZSSET)zsHits[c]=frames.reduce((n,f)=>n+(ZSSET[c].has(f.id)?1:0),0);
 if(ZSOFF.size&&ZSSET)frames=frames.filter(f=>{
   for(const c of ZSOFF){ if(ZSSET[c]&&ZSSET[c].has(f.id))return false; }
   return true;});

 const sims=frames.map(f=>f.sim).filter(v=>typeof v==='number');
 const smax=sims.length?Math.max(...sims):0, smin=sims.length?Math.min(...sims):0;
 const ringFrac=v=>{
  if(typeof v!=='number')return null;
  if(smax<=smin)return 1;
  return 0.18+0.82*((v-smin)/(smax-smin));      // the furthest still shows a ring
 };
 const fr=(SHELF==='frames'?frames:frames.slice(0,STRIP))
   .map(it=>{
     const f=ringFrac(it.sim);
     const ring=f==null?'':`<span class=simring style="--f:${f.toFixed(3)}" `+
       `title="cosine ${it.sim}"></span>`;
     return `<button class="tile shelftile" data-id="${it.id}">
      <img loading=lazy src="/thumb/${it.id}.jpg" alt="" onerror="this.style.visibility='hidden'">${ring}</button>`;
   }).join('');

 grid.innerHTML=
   shelf('places','Places & Trips',places.length,pl,'no place or trip by that name')+
   shelf('people','People',people.length,pe,'no one by that name')+
   shelf('frames','Frames',frames.length,fr,
         disabled?"search isn't enabled"
                 :(ZSOFF.size?'every result here is subtracted by the chips above'
                             :'nothing that looks like that'),
         zschips());

 grid.querySelectorAll('.shelfhd').forEach(h=>{
  const key=h.closest('.shelf').dataset.shelf;
  const toggle=()=>{SHELF=(SHELF===key)?null:key;renderShelves(q,places,people,frames,disabled);};
  h.onclick=e=>{ if(e.target.closest('.chipx')){SHELF=null;$('#searchq').value='';runSearch();return;} toggle(); };
  h.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}};
 });
 grid.querySelectorAll('.zschip').forEach(el=>{
  el.onclick=e=>{e.stopPropagation();
   const c=el.dataset.cat;
   if(ZSOFF.has(c))ZSOFF.delete(c);else ZSOFF.add(c);
   renderShelves(q,places,people,frames0,disabled);};
 });
 grid.querySelectorAll('.shelfcard').forEach(el=>{
  el.onclick=()=>{const r=PAL.find(x=>x.kind===el.dataset.kind&&x.name===el.dataset.name);if(r&&r.go)r.go();};
 });
 grid.querySelectorAll('.shelftile').forEach(el=>{
  el.onclick=()=>{seq=searchState.items;enterFocus(seq.findIndex(it=>it.id===+el.dataset.id));};
 });
}

// ===== NSFW review console (OWNER-ONLY calibration surface) =====
// Slider-first: the threshold is the wholesale sweep; per-tile "Not nudity" is the fine
// tool. Re-thresholding is a pure re-derive over nsfw.db — no rescan. The flagged set is
// owner-private (the data route is LAN-gated; this view is owner-gated on LOCAL_FULLRES).
// The Closed Set has TWO facets: the nudity screen (threshold slider + reveal-gate + per-
// tile clear) and Production (a plain held-aside work-product grid — no nudity semantics).
// NSFW_FACET selects which; the nudity path is unchanged from before the facet split.
let NSFW_ITEMS=[], NSFW_THR=0.5, nsfwRevealed=false, NSFW_FACET='nudity';   // reveal-gate flag; persists within a visit, reset on re-entry
async function showNsfw(facet){
 if(!window.LOCAL_FULLRES){location.replace('/');return;}   // owner-only — never render for a guest
 NSFW_FACET=(facet==='production')?'production':'nudity';
 view='nsfw';stopThumbPoll();closeFocus();placesReview=null;setNav('nsfwtog');
 closeOverlays('nsfwview');
 syncUrl('/nsfw');
 $('#nsfwview').classList.add('on');renderCrumb();   // sync band (hides it on this overlay)
 nsfwRevealed=false;   // re-gate on every entry: grid blurred behind the explainer until Reveal
 document.querySelectorAll('.nsfwtab').forEach(b=>b.classList.toggle('on',b.dataset.facet===NSFW_FACET));
 const ctl=$('#nsfwctl'); if(ctl) ctl.style.display=(NSFW_FACET==='production')?'none':'';   // threshold is nudity-only
 if(NSFW_FACET==='production'){
   const d=await jget('/api/nsfw_items?facet=production'); NSFW_ITEMS=d.items||[];
 }else{
   const d=await jget('/api/nsfw_items');
   NSFW_ITEMS=d.items||[]; NSFW_THR=(d.threshold!=null?Number(d.threshold):0.5);
   const sl=$('#nsfwthr'); if(sl) sl.value=NSFW_THR;
 }
 renderNsfw();
}
function renderNsfw(){
 const g=$('#nsfwgrid'), note=$('#nsfwsubnote');
 if(NSFW_FACET==='production'){   // plain held-aside work-product grid: no gate, no score, no clear
   $('#nsfwcount').textContent=NSFW_ITEMS.length+' videos';
   if(note) note.textContent='Production · work product — held aside from review · owner-only · never shown to shared viewers';
   const gate=$('#nsfwgate'); if(gate) gate.classList.remove('on'); g.classList.remove('blurred');
   if(!NSFW_ITEMS.length){g.innerHTML='<div class=vempty>No production work-product videos.</div>';return;}
   g.innerHTML=NSFW_ITEMS.map((it,i)=>`<div class="tile" data-i="${i}">
     <img loading=lazy src="/thumb/${it.id}.jpg" onerror="this.replaceWith(phspin())">
     <div class=tcap>#${it.id} · ${esc(it.ext)}</div></div>`).join('');
   g.querySelectorAll('.tile').forEach(el=>el.querySelector('img').onclick=()=>enterReview(NSFW_ITEMS,'production',+el.dataset.i,()=>showNsfw('production')));
   return;
 }
 if(note) note.textContent='on-device · owner-only · never shown to shared viewers · never deleted';
 const t=Number(NSFW_THR).toFixed(2);
 $('#nsfwcount').textContent=NSFW_ITEMS.length+' frames';
 $('#nsfwthrval').textContent=t;
 $('#nsfwsummary').textContent=NSFW_ITEMS.length+' frames on the closed set at threshold '+t+' · clearing only removes a frame from the set; the photo is never deleted';
 const gate=$('#nsfwgate'), gated=(!nsfwRevealed && NSFW_ITEMS.length>0);   // empty set skips the gate; reveal flag persists across re-renders
 g.classList.toggle('blurred',gated); if(gate) gate.classList.toggle('on',gated);
 if(!NSFW_ITEMS.length){g.innerHTML='<div class=vempty>Nothing on the closed set at this threshold. Lower it to widen the net, or the set above this score is clean.</div>';return;}
 // server returns items pre-sorted by score desc — render in order so the knee shows as you scroll
 g.innerHTML=NSFW_ITEMS.map((it,i)=>`<div class="tile" data-i="${i}">
   <span class=nsfwscore title="NudeNet max score">${it.score!=null?Number(it.score).toFixed(2):'—'}</span>
   <img loading=lazy src="/thumb/${it.id}.jpg" onerror="this.replaceWith(phspin())">
   <button class=nsfwclear data-c="${it.id}" title="Not nudity — removes this frame from the closed set only. The photo is never deleted.">✓ Not nudity</button>
   <div class=tcap>#${it.id} · ${esc(it.ext)}</div></div>`).join('');
 g.querySelectorAll('.tile').forEach(el=>el.querySelector('img').onclick=()=>enterReview(NSFW_ITEMS,'nsfw',+el.dataset.i,()=>showNsfw()));
 g.querySelectorAll('.nsfwclear').forEach(b=>b.onclick=async e=>{e.stopPropagation();
   await fetch('/api/nsfw/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({asset_id:+b.dataset.c,cleared:true})});
   NSFW_ITEMS=NSFW_ITEMS.filter(x=>x.id!==+b.dataset.c);
   toast('cleared from the closed set — the photo is untouched');renderNsfw();});
}
// reveal-gate lift: drop the blur + hide the explainer for the rest of this visit (re-blurs on next showNsfw)
function revealNsfw(){
 nsfwRevealed=true;
 const g=$('#nsfwgrid'); if(g) g.classList.remove('blurred');
 const gate=$('#nsfwgate'); if(gate) gate.classList.remove('on');
}
// slider release: persist the threshold (re-derives the set, no rescan) and re-fetch the pile
async function nsfwSetThreshold(v){
 NSFW_THR=Number(v); $('#nsfwthrval').textContent=NSFW_THR.toFixed(2);
 try{ await fetch('/api/settings/nsfw_threshold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({threshold:NSFW_THR})}); }
 catch(e){ toast('could not set threshold'); return; }
 const d=await jget('/api/nsfw_items'); NSFW_ITEMS=d.items||[];
 if(d.threshold!=null) NSFW_THR=Number(d.threshold);
 renderNsfw();
}
function toggleEdited(){const it=seq[fidx];if(!it||it.is_video||!isEditedAsset(it))return;window._focusOriginal=!window._focusOriginal;const im=$('#fimg');if(im)im.src=focusPreviewSrc(it);const eb=$('#fedited');if(eb){eb.classList.toggle('on',!!window._focusOriginal);eb.textContent=window._focusOriginal?'original':'🎨 edited';}}
function focusFull(){const it=seq[fidx];if(!it)return;const s=$('#fstage');
 if(it.is_video){ // transcoded <=720p H.264 preview — plays on LAN and over loupe
   s.innerHTML=`<video src="/api/play/${it.id}" poster="/thumb/${it.id}.jpg" controls autoplay playsinline preload="auto"></video>`;return;}
 if(!window.LOCAL_FULLRES){toast('full-res image is local-only');return;}
 s.innerHTML=`<img src="/api/full/${it.id}">`;}
/* ---- 9.8 deal mode: the card follows the hand --------------------------------
   "a one-frame card stack -- swipe right = keep, swipe left = cut, swipe down = skip"
   with "springs ... driving the card physics via a small pointer-event handler". The
   decisions already worked; what was missing is that nothing MOVED. The frame sat
   still under the finger and then simply changed, so there was no sense of dealing a
   card onto a pile, and no feedback about which way a swipe was about to go while
   there was still time to change your mind.

   The card is the stage itself, not the <img>: #fstage persists, while its innerHTML
   is rebuilt for every frame. */
(function(){const st=$('#fstage');let sx,sy,single=false,busy=false;
 const RM=window.matchMedia('(prefers-reduced-motion:reduce)');
 const THRESH=42;

 function card(dx,dy,anim,fade){
  st.style.transition=anim?'transform .2s cubic-bezier(.2,.7,.3,1),opacity .2s linear':'none';
  st.style.transform=(dx||dy)?'translate('+dx+'px,'+dy+'px) rotate('+(dx/26)+'deg)':'';
  st.style.opacity=fade?'0':'';
 }
 // Always recoverable: renderFocus calls this for every frame, so a transform can
 // never be left applied and strand the next card off-screen.
 window.resetCard=function(){ if(!st)return; st.style.transition='none';
  st.style.transform=''; st.style.opacity=''; };

 st.addEventListener('touchstart',e=>{
  // Single-finger only. Without this, the two-finger undo tap and an ordinary pinch
  // both put a finger in touches[0], and lifting them looked like a swipe -- i.e. a
  // pinch-zoom could cut a frame.
  single=(e.touches.length===1);
  if(!single||busy)return;
  const c=e.touches[0];sx=c.clientX;sy=c.clientY;},{passive:true});

 st.addEventListener('touchmove',e=>{
  if(!single||busy||focusZoomed()||RM.matches)return;   // zoomed = panning, not dealing
  const c=e.touches[0];if(!c)return;
  card(c.clientX-sx,c.clientY-sy,false,false);},{passive:true});

 st.addEventListener('touchend',e=>{
  if(!single||busy)return;
  const c=e.changedTouches[0];const dx=c.clientX-sx,dy=c.clientY-sy;
  if(Math.abs(dx)<THRESH&&Math.abs(dy)<THRESH){card(0,0,true,false);return;}  // spring back
  const horiz=Math.abs(dx)>Math.abs(dy);
  const act=horiz?(dx<0?()=>{focusFlash('cut');focusDecide('cut');}
                         :()=>{focusFlash('keep');focusDecide('keep');})
                 :(dy<0?focusNext:focusPrev);
  if(RM.matches||focusZoomed()){act();return;}
  // deal it off in the direction it was thrown, then apply the decision
  busy=true;
  card(horiz?(dx<0?-innerWidth:innerWidth):dx*0.4,
       horiz?dy*0.4:(dy<0?-innerHeight*0.7:innerHeight*0.7),true,true);
  setTimeout(()=>{busy=false;act();},200);
 },{passive:true});})();
// ---- mobile navigation ---------------------------------------------------------
// The rail works because a phone-width header stacked ten buttons into three labelled
// groups and pushed every photograph below the fold -- the whole first screen was nav.
//
// The bar is CLONED from the header buttons rather than written out again: the icons,
// the labels, the click handlers and the owner/guest gating (Vault, Closed Set and Setup
// carry display:none until the owner check passes) all keep living in exactly one place.
// A clone forwards its click to the original, so nothing here can drift from what the
// rail does.
const TAB_PRIMARY=['overviewtog','placestog','maptog','modetog'];   // + More; reorder here

function navButtons(){
 return [...document.querySelectorAll('.hbtns button[id]')]
   .filter(b=>b.id!=='expcand');            // an action, not a place
}
function navVisible(b){ return b && b.style.display!=='none'; }

function tabClone(b,label){
 const t=document.createElement('button');
 t.className='tabitem';
 t.dataset.for=b.id;
 const ico=b.querySelector('.navico');
 t.innerHTML=(ico?ico.outerHTML:'')+'<span class=tablbl></span>';
 t.querySelector('.tablbl').textContent=label||(b.textContent||'').trim();
 t.setAttribute('aria-label',(b.textContent||'').trim());
 t.onclick=()=>{closeMore();b.click();};
 return t;
}

function buildTabbar(){
 const bar=$('#tabbar'); if(!bar)return;
 const all=navButtons();
 const byId=Object.fromEntries(all.map(b=>[b.id,b]));
 bar.innerHTML='';
 for(const id of TAB_PRIMARY){
  const b=byId[id];
  if(!navVisible(b))continue;
  // "Cutting Room" is two words and a tab label has one line
  bar.appendChild(tabClone(b,id==='modetog'?'Cut':null));
 }
 const rest=all.filter(b=>!TAB_PRIMARY.includes(b.id)&&navVisible(b));
 if(rest.length){
  const more=document.createElement('button');
  more.className='tabitem tabmore';
  more.innerHTML='<svg class=navico viewBox="0 0 24 24" aria-hidden="true">'+
    '<circle cx="5" cy="12" r="1.5" fill="currentColor"></circle>'+
    '<circle cx="12" cy="12" r="1.5" fill="currentColor"></circle>'+
    '<circle cx="19" cy="12" r="1.5" fill="#E2902A"></circle></svg>'+
    '<span class=tablbl>More</span>';
  more.setAttribute('aria-label','More sections');
  more.onclick=openMore;
  bar.appendChild(more);
 }
 syncTabbar();
}

// The rail owns the active state; the bar mirrors it rather than tracking view itself,
// so there is one source of truth for "where am I".
function syncTabbar(){
 const bar=$('#tabbar'); if(!bar)return;
 let anyOn=false;
 bar.querySelectorAll('.tabitem[data-for]').forEach(t=>{
  const src=document.getElementById(t.dataset.for);
  const on=!!(src&&src.classList.contains('navon'));
  t.classList.toggle('on',on);
  if(on)anyOn=true;
 });
 // a section that lives behind More still lights More up, so the bar is never blank
 const m=bar.querySelector('.tabmore');
 if(m)m.classList.toggle('on',!anyOn&&navButtons().some(b=>b.classList.contains('navon')));
}

function openMore(){
 const sheet=$('#moresheet'), grid=$('#msgrid');
 if(!sheet||!grid)return;
 grid.innerHTML='';
 for(const b of navButtons()){
  if(TAB_PRIMARY.includes(b.id)||!navVisible(b))continue;
  const row=document.createElement('button');
  row.className='msitem'+(b.classList.contains('navon')?' on':'');
  const ico=b.querySelector('.navico');
  row.innerHTML=(ico?ico.outerHTML:'')+'<span>'+escHtml((b.textContent||'').trim())+'</span>';
  row.onclick=()=>{closeMore();b.click();};
  grid.appendChild(row);
 }
 sheet.hidden=false;
 requestAnimationFrame(()=>sheet.classList.add('on'));
}
function closeMore(){
 const sheet=$('#moresheet'); if(!sheet)return;
 sheet.classList.remove('on');
 setTimeout(()=>{if(!sheet.classList.contains('on'))sheet.hidden=true;},200);
}
document.addEventListener('click',e=>{
 const sheet=$('#moresheet');
 if(sheet&&!sheet.hidden&&e.target===sheet)closeMore();      // tap the scrim
});

function showKeys(){closeOverlays('keysview');$('#keysview').classList.add('on');}
function closeKeys(){$('#keysview').classList.remove('on');}
document.addEventListener('keydown',e=>{
 // '?' publishes the keyboard map (audit 8.5). Same typing guard as Escape below: a
 // question mark typed into the search box must reach the box, not open a dialog.
 if((e.key==='/'||((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'))&&
    !$('#paletteview').classList.contains('on')){
  const t=e.target;
  if(e.key==='/'&&t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable))return;
  e.preventDefault();palOpen();return;
 }
 if(e.key==='['||e.key===']'){
  const t=e.target;
  if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable))return;
  e.preventDefault();stepDensity(e.key==='['?-1:1);return;
 }
 if(e.key==='?'){
  const t=e.target;
  if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable))return;
  e.preventDefault();
  if($('#keysview').classList.contains('on'))closeKeys();else showKeys();
  return;
 }
 // Escape dismisses the topmost open overlay. Focus view already bound Escape to
 // exitFocus() below, so this is the app's own convention made consistent rather than a
 // new one: #resmodal in particular had NO keyboard exit at all, which is a keyboard
 // trap. Guarded two ways -- typing contexts keep Escape (the name autocomplete binds it
 // to close its dropdown, and element handlers fire first without stopping propagation,
 // so without this guard one Escape would dismiss both), and only the TOPMOST layer
 // closes, so Escape steps back one level instead of collapsing everything at once.
 // 9.3: "same muscle memory as photo culling (the app has ONE gesture language)".
 // Bound before the overlay Escape scan so the triage queue owns its own keys, and
 // skipped entirely while a field has focus so typing a name is not triage input.
 if(triageOpen()){
  const t=e.target;
  const typing=t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable);
  if(!typing){
   const k=e.key.toLowerCase();
   if(k==='k'){e.preventDefault();triageName();return;}
   if(k==='x'){e.preventDefault();triageDismiss();return;}
   if(e.key==='ArrowRight'){e.preventDefault();triageAdvance();return;}
   if(e.key==='Escape'){e.preventDefault();closeTriage();return;}
  }
 }
 if(e.key==='Escape'&&view!=='focus'){
  const t=e.target;
  if(t&&(t.tagName==='INPUT'||t.tagName==='TEXTAREA'||t.isContentEditable))return;
  // 9.2: "map dims; Esc returns." The deck is not an OVL entry, so without this the
  // topmost-overlay scan below found #mapview and Escape closed the whole map -- one
  // level too far. Same steps-back-one-level convention, applied to the lens.
  const deck=$('#mapcard');
  if(deck&&deck.classList.contains('on')){e.preventDefault();closeMapCard();return;}
  let top=null,topZ=-1;
  OVL.forEach(id=>{
   const el=$('#'+id);
   if(!el||!el.classList.contains('on'))return;
   const z=parseInt(getComputedStyle(el).zIndex,10);
   const zz=isNaN(z)?0:z;
   if(zz>=topZ){topZ=zz;top=el;}          // ties resolve to the later registry entry
  });
  if(top){e.preventDefault();top.classList.remove('on');renderCrumb();}
  return;
 }
 if(view!=='focus')return;const k=e.key.toLowerCase();
 if(k==='k'){focusFlash('keep');focusDecide('keep');}
 else if(k==='x'){focusFlash('cut');focusDecide('cut');}
 else if(k==='u'){undoLast();}
 else if(k==='v'){focusVault();}
 else if(k==='s'){moreLikeThis();}
 else if(k==='arrowright'){focusNext();}
 else if(k==='arrowleft'){focusPrev();}
 else if(k===' '){e.preventDefault();toggleZoom(null,null);}
 else if(k==='i'){toggleSig();}
 else if(k==='escape'){if(focusZoomed()){exitZoom();return;}exitFocus();}});

// persistent global header: measure its real height into --gbar-h so the base surface
// and overlays clear it. The header grows in two stages — the Fraunces webfont swap reflows
// the wordmark, AND the breadcrumb/stats rows only populate after showOverview's fetch — so a
// fixed set of triggers (fonts.ready/DOMContentLoaded) fires too early and undershoots. A
// ResizeObserver on #top tracks the real height through every reflow. (This is the header-
// height scar; never hardcode.)
function syncGbarH(){const t=document.getElementById('top');if(!t)return;
 // In rail mode the header is a full-height column, so its offsetHeight is the viewport
 // and every rule that offsets by --gbar-h would be pushed off-screen. Report 0 instead:
 // the 13 header-height rules then collapse to the top edge with no per-rule change.
 const rail=getComputedStyle(document.documentElement).getPropertyValue('--rail-w').trim();
 const railed=rail&&rail!=='0px'&&rail!=='0';
 document.documentElement.style.setProperty('--gbar-h',railed?'0px':t.offsetHeight+'px');}
syncGbarH();
if(document.fonts&&document.fonts.ready)document.fonts.ready.then(syncGbarH);
{const t=document.getElementById('top');if(t&&window.ResizeObserver)new ResizeObserver(syncGbarH).observe(t);}
{let _gbrz;window.addEventListener('resize',()=>{clearTimeout(_gbrz);_gbrz=setTimeout(syncGbarH,150);});}

if(!window.LOCAL_FULLRES){const b=$('#ffull');if(b)b.style.display='none';}
if(window.LOCAL_FULLRES){const nt=$('#nsfwtog');if(nt)nt.style.display='';}   // owner-only nav: reveal Flagged for LOCAL, hidden for guests
if(window.LOCAL_FULLRES){const vt=$('#vaulttog');if(vt)vt.style.display='';}
// 9.8: "Guest = same light table minus the darkroom: rail shows two spaces." The
// darkroom is owner-only (8.5), and the server now 403s /setup and /api/setup/status
// for a tunnel guest, so the whole group comes out of the rail rather than standing
// there as a labelled row of things that answer 403. A guest sees two spaces.
if(window.LOCAL_FULLRES){const su=$('#setuptog');if(su)su.style.display='';}
if(!window.LOCAL_FULLRES){
 const dk=document.querySelector('.navspace[data-space=darkroom]');
 if(dk)dk.style.display='none';
}   // owner-only nav: reveal Vault for LOCAL, hidden for guests
if(window.SEARCH_ENABLED){const st=$('#searchtog');if(st)st.style.display='';}   // reveal Search only when search_backend=local
if(window.LOCAL_FULLRES && window.SEARCH_ENABLED){const sa=$('#searchall');if(sa)sa.style.display='';}   // owner-only: reveal "everything" search toggle
// Built after the owner/guest gating above, not before: the bar is cloned from the
// nav buttons and skips any that are still display:none, so building it first would
// give a guest a bar and an owner a bar missing Vault, Closed Set and Setup.
buildTabbar();
routeTo(location.pathname);   // first paint uses the same table as Back/Forward

/* ---- 9.8 touch vocabulary -------------------------------------------------
   "long-press = peek (popover with EXIF + marks, no navigation); two-finger tap =
   undo (matches Procreate muscle memory)", and "hover-dependent affordances all
   have touch twins".

   A tile's tap already navigates into focus, so peek cannot be a tap -- and on a
   touch device there is no hover to carry it. Long-press is the twin, and the
   click that a long-press would otherwise emit is swallowed so peeking never
   navigates. Peek is strictly read-only: it decides nothing and moves nothing. */
(function(){
 const LONG=480, SLOP=10;
 let timer=null, px=0, py=0, swallow=false;

 function bytes(n){ if(!n) return '—';
  return n>=1073741824 ? (n/1073741824).toFixed(2)+' GB'
       : n>=1048576    ? (n/1048576).toFixed(1)+' MB'
       : Math.round(n/1024)+' KB'; }
 function when(ts){ if(!ts) return '—';
  const d=new Date(ts*1000);
  return d.toLocaleString(undefined,{year:'numeric',month:'short',day:'numeric',
                                     hour:'numeric',minute:'2-digit'}); }

 function peekTile(it,x,y){
  if(!it) return;
  closePeek();
  const rows=[
   ['file', it.path||'—'],
   ['taken', when(it.ts)],
   ['size', bytes(it.size)+(it.ext?' · '+it.ext:'')],
  ];
  if(it.is_video&&it.dur) rows.push(['duration', Math.round(it.dur)+'s']);
  if(it.blurpct!=null)    rows.push(['sharpness', it.blurpct+'th pct']);
  rows.push(['gps', it.has_gps?'yes':'no']);
  // the marks
  rows.push(['decision', it.state||'undecided']);
  if(it.vaulted) rows.push(['vault', 'marked']);

  const el=document.createElement('div');
  el.className='peek'; el.id='peek';
  el.innerHTML='<div class=peekhd>'+esc('#'+it.id)+'</div>'+
   rows.map(r=>'<span class=k>'+esc(r[0])+'</span><span>'+esc(String(r[1]))+'</span>').join('');
  document.body.appendChild(el);
  // keep it on screen
  const w=Math.min(300, window.innerWidth-20);
  el.style.width=w+'px';
  el.style.left=Math.max(10, Math.min(x-w/2, window.innerWidth-w-10))+'px';
  const h=el.getBoundingClientRect().height;
  el.style.top=(y-h-16>10 ? y-h-16 : y+18)+'px';
  if(navigator.vibrate)navigator.vibrate(12);
 }
 function closePeek(){ const el=document.getElementById('peek'); if(el)el.remove(); }
 window.closePeek=closePeek;

 function tileUnder(t){ return t&&t.closest ? t.closest('.tile[data-id]') : null; }

 document.addEventListener('touchstart',e=>{
  closePeek();
  if(e.touches.length!==1) return;
  const t=tileUnder(e.target); if(!t) return;
  const c=e.touches[0]; px=c.clientX; py=c.clientY; swallow=false;
  clearTimeout(timer);
  timer=setTimeout(()=>{ timer=null; swallow=true;
   peekTile(byId[+t.dataset.id], px, py); }, LONG);
 },{passive:true});

 document.addEventListener('touchmove',e=>{
  if(!timer) return;
  const c=e.touches[0]; if(!c) return;
  if(Math.abs(c.clientX-px)>SLOP||Math.abs(c.clientY-py)>SLOP){ clearTimeout(timer); timer=null; }
 },{passive:true});

 document.addEventListener('touchend',()=>{ if(timer){ clearTimeout(timer); timer=null; } },{passive:true});

 // A long press still emits a click; swallow exactly that one so peek never navigates.
 document.addEventListener('click',e=>{
  if(!swallow) return;
  swallow=false; e.preventDefault(); e.stopPropagation();
 },true);

 /* two-finger tap = undo. A tap, not a pinch: both fingers lift quickly and neither
    travels far, so an actual pinch-zoom is not mistaken for an undo. */
 const st=document.getElementById('fstage');
 if(st){
  let armed=false, t0=0, ax=0, ay=0, moved=false;
  st.addEventListener('touchstart',e=>{
   if(e.touches.length===2){ armed=true; moved=false; t0=Date.now();
    ax=(e.touches[0].clientX+e.touches[1].clientX)/2;
    ay=(e.touches[0].clientY+e.touches[1].clientY)/2; }
   else if(e.touches.length>2){ armed=false; }
  },{passive:true});
  st.addEventListener('touchmove',e=>{
   if(!armed||e.touches.length!==2) return;
   const mx=(e.touches[0].clientX+e.touches[1].clientX)/2;
   const my=(e.touches[0].clientY+e.touches[1].clientY)/2;
   if(Math.abs(mx-ax)>SLOP||Math.abs(my-ay)>SLOP) moved=true;
  },{passive:true});
  st.addEventListener('touchend',e=>{
   if(!armed||e.touches.length) return;
   armed=false;
   if(!moved&&Date.now()-t0<320) undoLast();
  },{passive:true});
 }
})();
