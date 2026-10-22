const {test, createDOM, mount, initDashboard, closeDOM, response, tick} = require('./player_analysis_dom_harness');
// NODE_PATH=/tmp/lmonitor-analysis-ui-test/node_modules node --test botend/tests/player_analysis_dashboard_test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const empty = {items:[],page:1,total:0,total_pages:1};

async function dashboard(search='') {
    const dom = createDOM(`<meta name="csrf-token" content="test-token"><ul>
      <li class="nav-item" data-section="dashboard-home"><a href="?">首页</a></li>
      <li class="nav-item" data-section="player-analyses"><a href="?section=player-analyses">角色分析</a></li></ul>
      <section id="dashboard-home" class="content-section active"></section>
      <section id="player-analyses" class="content-section" style="display:none"><div id="player-analysis-app"></div></section>`,
      {url:'https://example.com/dashboard/'+search,runScripts:'outside-only'});
    await new Promise(resolve=>dom.window.addEventListener('load',resolve,{once:true}));
    const win=dom.window, doc=win.document, calls=[];
    win.fetch=async(url,options)=>{calls.push([url,options]);return response(empty);};
    win.confirm=()=>true;
    win.requestAnimationFrame=fn=>fn();
    win.DASHBOARD_DEFAULT_SECTION='dashboard-home';
    // Evaluate the real router after DOMContentLoaded: unrelated widgets do not boot.
    win.eval(fs.readFileSync(path.join(__dirname,'../../static/dashboard/js/main.js'),'utf8'));
    win.initNavigation();
    const panel=initDashboard(doc);
    return {dom,win,doc,calls,panel,root:doc.getElementById('player-analysis-app'),nav:section=>doc.querySelector(`[data-section="${section}"] a`).click()};
}

test('real sidebar/router lazy mounts once, keeps drafts on return, restores query location',async()=>{
    const {dom,win,doc,calls,panel,root,nav}=await dashboard();
    try {
        await tick(); assert.equal(calls.length,0);
        assert.strictEqual(initDashboard(doc),panel);
        nav('player-analyses'); await tick();
        assert.equal(win.location.search,'?section=player-analyses');
        assert.ok(doc.getElementById('player-analyses').classList.contains('active'));
        assert.equal(calls.length,1);
        root.querySelector('[name="q"]').value='保留筛选';
        root.querySelector('[data-action="new"]').click();await tick();
        root.querySelector('[data-field="conclusion"]').value='未保存人工结论';
        const editor=root.querySelector('.pa-edit-form');
        nav('dashboard-home');await tick();
        assert.equal(win.location.search,'');
        // Restore the previous URL through the same handler used by popstate.
        win.history.replaceState({},'', '?section=player-analyses');
        win.activateDashboardLocation();await tick();
        nav('player-analyses');await tick();
        assert.equal(calls.length,1);
        assert.strictEqual(root.querySelector('.pa-edit-form'),editor);
        assert.equal(root.querySelector('[name="q"]').value,'保留筛选');
        assert.equal(root.querySelector('[data-field="conclusion"]').value,'未保存人工结论');
        root.querySelector('.pa-filters').dispatchEvent(new win.Event('submit',{bubbles:true,cancelable:true}));await tick();
        assert.equal(calls.length,2,'one submit must cause only one request');
        assert.equal(new URL(calls[1][0],win.location).searchParams.get('q'),'保留筛选');
    } finally {closeDOM(dom,panel);}
});

test('direct query entry activates the existing router without eager requests',async()=>{
    const {dom,win,calls,panel}=await dashboard('?section=player-analyses');
    try {
        assert.equal(calls.length,0);
        win.activateDashboardLocation();await tick();
        assert.equal(calls.length,1);
        assert.equal(win.document.querySelectorAll('.pa-filters').length,1);
    } finally {closeDOM(dom,panel);}
});

test('mount is idempotent; destroy aborts reads, removes listeners and ignores late responses',async()=>{
    const dom=createDOM('<div id="root"></div>',{url:'https://example.com/dashboard/'});
    const root=dom.window.document.getElementById('root');
    let release, signal, calls=0;
    const app=mount(root,{fetch:async(_url,options)=>{calls++;signal=options.signal;return new Promise(resolve=>release=resolve);}});
    assert.strictEqual(mount(root),app);
    await tick();assert.equal(calls,1);
    const form=root.querySelector('form');
    app.destroy();assert.equal(signal.aborted,true);
    release(response(empty));await app.ready;
    form.dispatchEvent(new dom.window.Event('submit',{cancelable:true}));await tick();
    assert.equal(calls,1);assert.equal(root.children.length,0);
    const fresh=mount(root,{fetch:async()=>{calls++;return response(empty);}});
    await fresh.ready;assert.equal(calls,2);assert.equal(root.querySelectorAll('.pa-filters').length,1);
    closeDOM(dom,fresh);
});

test('panel stylesheet cannot restyle the Dashboard shell or sibling panels',()=>{
    const css=fs.readFileSync(path.join(__dirname,'../../static/dashboard/css/player-analysis-management.css'),'utf8');
    const dom=createDOM(`<style>${css}</style>`);
    function inspect(rules) {
        for(const rule of rules) {
            if(rule.cssRules)inspect(rule.cssRules);
            else assert.match(rule.selectorText, /^(?:html\[data-dashboard-theme="dark"\] )?#player-analyses(?:\s|$)/);
        }
    }
    inspect(dom.window.document.styleSheets[0].cssRules);
    dom.window.close();
});

test('page teardown disconnects the observer and is idempotent; bfcache preserves drafts',async()=>{
    const {dom,win,doc,panel,root,nav,calls}=await dashboard();
    nav('player-analyses');await tick();
    root.querySelector('[name="q"]').value='保留';
    win.dispatchEvent(new win.PageTransitionEvent('pagehide',{persisted:true}));
    assert.equal(root.querySelector('[name="q"]').value,'保留');
    win.dispatchEvent(new win.PageTransitionEvent('pagehide',{persisted:false}));
    assert.equal(root.children.length,0);
    nav('dashboard-home');nav('player-analyses');await tick();
    assert.equal(calls.length,1);
    const replacement=initDashboard(doc);await tick();
    panel.destroy();assert.strictEqual(initDashboard(doc),replacement);
    assert.equal(calls.length,2);
    replacement.destroy();dom.window.close();
});

test('history restores home after direct panel entry without replacing the forward stack',async()=>{
    const {dom,win,doc,calls,panel,nav}=await dashboard('?section=player-analyses');
    try {
        win.DASHBOARD_DEFAULT_SECTION='player-analyses';
        // This fixture excludes unrelated page bootstrap; preserve its actual
        // listener registration contract before driving the real popstate handler.
        assert.match(fs.readFileSync(path.join(__dirname,'../../static/dashboard/js/main.js'),'utf8'),
            /window\.addEventListener\(\s*['"]popstate['"]\s*,\s*activateDashboardLocation\s*\)/);
        win.addEventListener('popstate',win.activateDashboardLocation);
        win.activateDashboardLocation();await tick();
        nav('dashboard-home');nav('player-analyses');await tick();
        const length=win.history.length;
        await new Promise(resolve=>{win.addEventListener('popstate',resolve,{once:true});win.history.back();});
        await tick();
        assert.ok(doc.getElementById('dashboard-home').classList.contains('active'));
        assert.equal(win.location.search,'');
        assert.equal(win.history.length,length);
        await new Promise(resolve=>{win.addEventListener('popstate',resolve,{once:true});win.history.forward();});
        await tick();
        assert.ok(doc.getElementById('player-analyses').classList.contains('active'));
        assert.equal(calls.length,1);
    } finally {closeDOM(dom,panel);}
});

test('router replaces stale section/tool/table selectors and preserves restorable state',async()=>{
    const {win}=await dashboard('?section=old&tool=old&table=old');
    // The small DOM fixture covers section clicks; keep the distinct submenu
    // caller guards so testing the URL helper alone cannot hide broken wiring.
    assert.match(win.initNavigation.toString(), /syncDashboardLocation\(\s*\{\s*section:\s*dashboardSection\s*\}\s*\)/);
    assert.match(win.initNavigation.toString(), /syncDashboardLocation\(\s*\{\s*tool:\s*toolName\s*\}\s*\)/);
    assert.match(win.openDashboardTable.toString(), /syncDashboardLocation\(\s*\{\s*table:\s*tableName\s*\}\s*\)/);
    for (const target of [{section:'player-analyses'}, {tool:'simc'}, {table:'MonitorTask'}, {section:'dashboard-home'}]) {
        const before=win.history.length;
        win.syncDashboardLocation(target);
        const expected=target.section==='dashboard-home' ? {} : target;
        assert.deepEqual(Object.fromEntries(new URLSearchParams(win.location.search)),expected);
        assert.equal(win.history.length,before+1);
        const length=win.history.length;
        win.syncDashboardLocation(target);
        assert.equal(win.history.length,length,'same location must not duplicate browser history');
    }
});

test('real Dashboard theme button restores persisted dark mode and toggles accessible state',async()=>{
    const template=fs.readFileSync(path.join(__dirname,'../../templates/dashboard/index.html'),'utf8');
    const dom=createDOM(template.replace(/{%[\s\S]*?%}/g,''),{url:'https://example.com/dashboard/',runScripts:'outside-only'});
    await new Promise(resolve=>dom.window.addEventListener('load',resolve,{once:true}));
    const win=dom.window, doc=win.document, key='lmonitor-dashboard-theme';
    const restorers=[...doc.scripts].filter(script=>script.textContent.includes(`localStorage.getItem('${key}')`));
    assert.equal(restorers.length,1);
    win.localStorage.setItem(key,'dark');
    win.eval(restorers[0].textContent);
    const mainJS=fs.readFileSync(path.join(__dirname,'../../static/dashboard/js/main.js'),'utf8');
    assert.match(mainJS,/^\s*initDashboardTheme\(\);/m);
    win.eval(mainJS);
    win.initDashboardTheme();
    const toggle=doc.getElementById('dashboard-theme-toggle');
    assert.equal(toggle.type,'button');
    for (const mode of ['dark','light','dark']) {
        assert.equal(doc.documentElement.dataset.dashboardTheme==='dark',mode==='dark');
        assert.equal(toggle.getAttribute('aria-pressed'),String(mode==='dark'));
        assert.equal(win.localStorage.getItem(key),mode);
        assert.ok(toggle.getAttribute('aria-label'));
        toggle.click();
    }
});

test('absent superuser panel never mounts or fetches',()=>{
    const dom=createDOM('<section id="dashboard-home"></section>');
    assert.equal(initDashboard(dom.window.document),null);
    dom.window.close();
});
