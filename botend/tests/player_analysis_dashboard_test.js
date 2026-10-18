// NODE_PATH=/tmp/lmonitor-analysis-ui-test/node_modules node --test botend/tests/player_analysis_dashboard_test.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const {mount, initDashboard} = require('../../static/dashboard/js/player-analysis-management.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = data => ({ok:true,status:200,json:async()=>({status:'success',data})});
const empty = {items:[],page:1,total:0,total_pages:1};

async function dashboard(search='') {
    const dom = new JSDOM(`<meta name="csrf-token" content="test-token"><ul>
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
    } finally {panel.destroy();dom.window.close();}
});

test('direct query entry activates the existing router without eager requests',async()=>{
    const {dom,win,calls,panel}=await dashboard('?section=player-analyses');
    try {
        assert.equal(calls.length,0);
        win.activateDashboardLocation();await tick();
        assert.equal(calls.length,1);
        assert.equal(win.document.querySelectorAll('.pa-filters').length,1);
    } finally {panel.destroy();dom.window.close();}
});

test('mount is idempotent; destroy aborts reads, removes listeners and ignores late responses',async()=>{
    const dom=new JSDOM('<div id="root"></div>',{url:'https://example.com/dashboard/'});
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
    fresh.destroy();dom.window.close();
});

test('panel stylesheet cannot restyle the Dashboard shell or sibling panels',()=>{
    const css=fs.readFileSync(path.join(__dirname,'../../static/dashboard/css/player-analysis-management.css'),'utf8');
    const dom=new JSDOM(`<style>${css}</style>`);
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

test('absent superuser panel never mounts or fetches',()=>{
    const dom=new JSDOM('<section id="dashboard-home"></section>');
    assert.equal(initDashboard(dom.window.document),null);
    dom.window.close();
});
