// Per-test ownership also cleans up fixtures when setup or assertions fail.
const nodeTest = require('node:test');
const {AsyncLocalStorage} = require('node:async_hooks');
const {JSDOM} = require('jsdom');
const production = require('../../static/dashboard/js/player-analysis-management.js');
const scope = new AsyncLocalStorage();
function test(name, callback) {
    return nodeTest.test(name, t => scope.run([], async () => {
        const resources = scope.getStore();
        t.after(() => {
            const errors = [];
            for (const close of resources.reverse()) {
                try { close(); } catch (error) { errors.push(error); }
            }
            if (errors.length) throw new AggregateError(errors, 'DOM fixture cleanup failed');
        });
        return callback(t);
    }));
}
function own(resource, close) {
    if (resource == null) return resource; // Absent permission-gated panel owns nothing.
    const resources = scope.getStore();
    if (!resources) throw new Error('DOM fixtures must run inside harness test()');
    resources.push(() => close(resource));
    return resource;
}
function createDOM(...args) { return own(new JSDOM(...args), dom => dom.window.close()); }
function mount(...args) { return own(production.mount(...args), app => app.destroy()); }
function initDashboard(...args) { return own(production.initDashboard(...args), app => app.destroy()); }
function closeDOM(dom, controller) { try { controller?.destroy(); } finally { dom.window.close(); } }
const response = data => ({ok:true,status:200,json:async()=>({status:'success',data:structuredClone(data)})});
const tick = () => new Promise(resolve => setImmediate(resolve));
module.exports = {test, createDOM, mount, initDashboard, closeDOM, response, tick};
