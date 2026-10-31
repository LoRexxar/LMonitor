// Browser DOM regression; see requirements-simc-ui-test.txt for setup.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
test('actor-page controls and async fences in real Chromium DOM', {timeout: 120000}, () => {
    const result = spawnSync(process.env.SIMC_UI_PYTHON || 'python3',
        [path.join(__dirname, 'simc_skill_page_ui_browser.py')],
        {encoding: 'utf8', timeout: 110000, maxBuffer: 2000000,
            env: {...process.env, SIMC_UI_NODE: process.execPath}});
    assert.ifError(result.error);
    assert.equal(result.status, 0, result.stdout + result.stderr);
    console.log(result.stdout + result.stderr);
});
