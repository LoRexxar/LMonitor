import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

import {
    catalogItemsToCompletionOptions,
    codePointColumnToOffset,
    completionItemsToOptions,
    completionReplacementFrom,
    convertDocumentSnapshot,
    createVersionedRequest,
    diagnosticRangeToOffsets,
    editorIndentKeymap,
    editorLanguageTransition,
    formatStructuralValidationStatus,
    keywordPairsToCompletionOptions,
    keywordPairsToCatalogItems,
    mergeCompletionOptions,
    replaceTextMessage,
    runSingleSubmission,
    selectDefaultAplForSpec,
    selectDefaultAplsForSpec,
} from '../../../static/dashboard/js/simc-apl-editor.js';

const editorSourceUrl = new URL('../../../static/dashboard/js/simc-apl-editor.js', import.meta.url);
const workbenchSourceUrl = new URL('../../../static/dashboard/js/simc-workbench.js', import.meta.url);
const editorCssUrl = new URL('../../../static/dashboard/css/simc-apl-editor.css', import.meta.url);

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return {promise, resolve, reject};
}

function response(data) {
    return {ok: true, json: async () => ({success: true, data})};
}

test('diagnostic positions use one-based unicode code points and exclusive ends', () => {
    const text = 'actions=/嗜血\n😀x';
    assert.equal(codePointColumnToOffset(text, 1, 10), 9);
    assert.deepEqual(diagnosticRangeToOffsets(text, {
        start: {line: 2, column: 1}, end: {line: 2, column: 2},
    }), {from: 12, to: 14});
    assert.deepEqual(diagnosticRangeToOffsets(text, {
        start: {line: 2, column: 3}, end: {line: 2, column: 3},
    }), {from: text.length, to: text.length});
});

test('Tab and Shift+Tab use CodeMirror indentation commands', () => {
    assert.equal(editorIndentKeymap.key, 'Tab');
    assert.equal(typeof editorIndentKeymap.run, 'function');
    assert.equal(typeof editorIndentKeymap.shift, 'function');
});

test('completion keeps localized labels but inserts the authoritative token', () => {
    assert.deepEqual(completionItemsToOptions([{
        label: '嗜血 bloodthirst', insert_text: 'bloodthirst', kind: 'action',
        name_zh: '嗜血', description: '造成伤害',
    }]), [{
        label: '嗜血 bloodthirst', apply: 'bloodthirst', type: 'function',
        detail: '嗜血', info: '造成伤害',
    }]);
});

test('qualified completions replace only the token after the last dot', () => {
    assert.equal(completionReplacementFrom({from: 3, text: 'variable.po'}), 12);
    assert.equal(completionReplacementFrom({from: 3, text: 'blood'}), 3);
    assert.equal(completionReplacementFrom(null), null);
});

test('catalog completions show bilingual labels and only insert authoritative tokens', () => {
    assert.deepEqual(catalogItemsToCompletionOptions([
        {
            token: 'bloodthirst', kind: 'action', insertable: true,
            name_zh: '嗜血', name_en: 'Bloodthirst', description_zh: '造成物理伤害。',
        },
        {
            token: null, kind: 'talent', insertable: false,
            name_zh: '未绑定天赋', name_en: 'Unbound Talent',
        },
    ]), [{
        label: '嗜血 · bloodthirst', apply: 'bloodthirst', type: 'function',
        detail: 'Bloodthirst', info: '造成物理伤害。',
    }]);
});

test('keyword pairs provide a safe bilingual fallback when the symbol catalog is unavailable', () => {
    assert.deepEqual(keywordPairsToCompletionOptions([
        {apl_keyword: 'recklessness', cn_keyword: '鲁莽', description: '战士技能', is_active: true},
        {apl_keyword: 'old_token', cn_keyword: '旧词条', is_active: false},
        {apl_keyword: 'actions=/invalid shape', cn_keyword: '非法', is_active: true},
    ], 'reck'), [{
        label: '鲁莽 · recklessness', apply: 'recklessness', type: 'function',
        detail: 'APL 关键词', info: '战士技能',
    }]);
});

test('keyword pairs also populate the visible skill assistant when the symbol catalog is unavailable', () => {
    assert.deepEqual(keywordPairsToCatalogItems([
        {apl_keyword: 'recklessness', cn_keyword: '鲁莽', description: '战士技能', is_active: true},
        {apl_keyword: 'old_token', cn_keyword: '旧词条', is_active: false},
        {apl_keyword: 'actions=/invalid shape', cn_keyword: '非法', is_active: true},
    ], '鲁莽'), [{
        token: 'recklessness', kind: 'keyword', scope: 'global', insertable: true,
        name_zh: '鲁莽', name_en: '', description_zh: '战士技能',
        source: '中英文关键词库', simc_revision: '', game_build: '',
    }]);
});

test('document and catalog completions merge without duplicate insertions', () => {
    assert.deepEqual(mergeCompletionOptions(
        [{label: 'bloodthirst', apply: 'bloodthirst', type: 'keyword'}],
        [
            {label: '嗜血 · bloodthirst', apply: 'bloodthirst', type: 'function'},
            {label: '暴怒 · rampage', apply: 'rampage', type: 'function'},
        ],
    ), [
        {label: 'bloodthirst', apply: 'bloodthirst', type: 'keyword'},
        {label: '暴怒 · rampage', apply: 'rampage', type: 'function'},
    ]);
});

test('structural validation status is explicit about errors, warnings, and scope', () => {
    assert.equal(formatStructuralValidationStatus({error: 0, warning: 0, info: 0}), '结构检查通过');
    assert.equal(formatStructuralValidationStatus({error: 2, warning: 1, info: 4}), '2 个错误 · 1 个警告');
    assert.equal(formatStructuralValidationStatus({error: 0, warning: 3, info: 1}), '0 个错误 · 3 个警告');
});

test('versioned requests abort predecessors and reject late responses', async () => {
    const calls = [];
    const fetchImpl = (_url, options) => {
        const pending = deferred();
        calls.push({options, pending});
        return pending.promise;
    };
    const request = createVersionedRequest(fetchImpl, '/validate');
    const first = request.run({content: 'first'}).catch(error => error.name === 'AbortError' ? null : Promise.reject(error));
    const second = request.run({content: 'second'});
    assert.equal(calls[0].options.signal.aborted, true);
    calls[0].pending.resolve(response({document_version: 1, diagnostics: ['late']}));
    calls[1].pending.resolve(response({document_version: 2, diagnostics: []}));
    assert.equal(await first, null);
    assert.deepEqual(await second, {document_version: 2, diagnostics: []});
});

test('cancel aborts the active request and advances document version', async () => {
    const pending = deferred();
    let signal;
    const request = createVersionedRequest((_url, options) => {
        signal = options.signal;
        return pending.promise;
    }, '/validate');
    const running = request.run({content: 'actions=/x'});
    request.cancel();
    assert.equal(signal.aborted, true);
    assert.equal(request.version, 2);
    pending.resolve(response({document_version: 1}));
    assert.equal(await running, null);
});

test('catalog errors are rendered as text instead of executable markup', () => {
    const previousDocument = globalThis.document;
    globalThis.document = {
        createElement: () => ({className: '', textContent: ''}),
    };
    const host = {replaceChildren(node) { this.child = node; }};
    try {
        const node = replaceTextMessage(host, '<img src=x onerror=alert(1)>');
        assert.equal(host.child, node);
        assert.equal(node.textContent, '<img src=x onerror=alert(1)>');
        assert.equal(node.className, 'simc-apl-catalog__empty');
    } finally {
        globalThis.document = previousDocument;
    }
});

test('APL save runs once, disables submit, and recovers after failure', async () => {
    const pending = deferred();
    const button = {disabled: false};
    const attributes = new Map();
    const form = {
        dataset: {},
        querySelector: () => button,
        setAttribute: (name, value) => attributes.set(name, value),
        removeAttribute: name => attributes.delete(name),
    };
    let calls = 0;
    const first = runSingleSubmission(form, async () => { calls += 1; await pending.promise; });
    const duplicate = await runSingleSubmission(form, async () => { calls += 1; });
    assert.equal(duplicate, false);
    assert.equal(calls, 1);
    assert.equal(button.disabled, true);
    assert.equal(attributes.get('aria-busy'), 'true');
    pending.reject(new Error('save failed'));
    await assert.rejects(first, /save failed/);
    assert.equal(button.disabled, false);
    assert.equal(attributes.has('aria-busy'), false);
    assert.equal(form.dataset.aplSubmitting, undefined);
});

test('language switching is bidirectional and uses one authoritative editor document', () => {
    assert.deepEqual(editorLanguageTransition('apl', 'cn'), {
        from: 'apl', to: 'cn', conversionType: 'apl_to_cn',
    });
    assert.deepEqual(editorLanguageTransition('cn', 'apl'), {
        from: 'cn', to: 'apl', conversionType: 'cn_to_apl',
    });
    assert.equal(editorLanguageTransition('apl', 'apl'), null);
});

test('Chinese save conversion rejects a stale source when the editor changes in flight', async () => {
    const pending = deferred();
    let value = '动作+=/嗜血';
    let version = 7;
    const conversion = convertDocumentSnapshot({
        source: value, version,
        getValue: () => value,
        getVersion: () => version,
        convert: () => pending.promise,
    });
    value = '动作+=/暴怒';
    version += 1;
    pending.resolve('actions+=/bloodthirst');
    await assert.rejects(conversion, /正文已变化/);
});

test('Chinese save conversion returns authoritative APL for unchanged source', async () => {
    const source = '动作+=/嗜血';
    assert.equal(await convertDocumentSnapshot({
        source, version: 3,
        getValue: () => source,
        getVersion: () => 3,
        convert: async () => 'actions+=/bloodthirst',
    }), 'actions+=/bloodthirst');
});

test('new APL import resolves exactly one active selectable system default for its spec', () => {
    const rows = [
        {id: 1, spec: 'warrior_arms', is_system: true, is_active: true, is_selectable: true},
        {id: 2, spec: 'warrior_fury', is_system: true, is_active: true, is_selectable: true},
        {id: 3, spec: 'warrior_fury', is_system: false, is_active: true, is_selectable: true},
    ];
    assert.equal(selectDefaultAplForSpec(rows, 'warrior_fury')?.id, 2);
    assert.equal(selectDefaultAplForSpec(rows, 'warrior_protection'), null);
    assert.throws(() => selectDefaultAplForSpec([...rows, {...rows[1], id: 4}], 'warrior_fury'), /多个系统默认 APL/);
});

test('new APL form can list every active selectable system default for the selected spec', () => {
    const rows = [
        {id: 1, name: 'Arms', spec: 'warrior_arms', is_system: true, is_active: true, is_selectable: true},
        {id: 2, name: 'Fury ST', spec: 'warrior_fury', is_system: true, is_active: true, is_selectable: true},
        {id: 3, name: 'Fury disabled', spec: 'warrior_fury', is_system: true, is_active: false, is_selectable: true},
        {id: 4, name: 'Mine', spec: 'warrior_fury', is_system: false, is_active: true, is_selectable: true},
        {id: 5, name: 'Fury AoE', spec: 'warrior_fury', is_system: true, is_active: true, is_selectable: true},
    ];
    assert.deepEqual(selectDefaultAplsForSpec(rows, 'warrior_fury').map(row => row.id), [2, 5]);
});

test('APL workspace contract uses a larger desktop dialog, tall editor, and independent wide assistant sidebar', async () => {
    const [css, workbench] = await Promise.all([
        readFile(editorCssUrl, 'utf8'), readFile(workbenchSourceUrl, 'utf8'),
    ]);
    assert.match(css, /\.simc-workbench-dialog__panel\.is-apl-editor-layout\s*\{[^}]*width:\s*min\(96vw,\s*96rem\)/s);
    assert.match(css, /grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(26rem,\s*32rem\)/);
    assert.match(css, /\.simc-apl-editor-mount\s*\{[^}]*min-height:\s*34rem/s);
    assert.match(workbench, /<aside class="simc-apl-assistant"[^>]*aria-label="技能与 Buff 助手"/);
    assert.match(css, /@media \(max-width:\s*900px\)[\s\S]*\.simc-apl-assistant[^}]*position:\s*fixed/);
});

test('new APL form exposes default import and replaces readonly bilingual panel with language switch', async () => {
    const [editorSource, workbench] = await Promise.all([
        readFile(editorSourceUrl, 'utf8'), readFile(workbenchSourceUrl, 'utf8'),
    ]);
    assert.doesNotMatch(workbench, /data-apl-import-default/);
    assert.match(workbench, /data-apl-default-search/);
    assert.match(workbench, /data-apl-default-list/);
    assert.match(workbench, /data-apl-default-choice/);
    assert.match(workbench, /data-apl-language="apl"/);
    assert.match(workbench, /data-apl-language="cn"/);
    assert.doesNotMatch(workbench, /data-apl-bilingual-panel/);
    assert.match(editorSource, /async convertLanguage\(targetLanguage\)/);
    assert.match(editorSource, /documentVersion !== version \|\| view\.state\.doc\.toString\(\) !== source/);
    assert.match(editorSource, /async getValueForSave\(\)/);
    assert.match(workbench, /aplImportGeneration/);
    assert.match(workbench, /aplImportAbortController/);
    assert.match(workbench, /state\.aplEditor === editor/);
    assert.match(workbench, /originalSpec/);
    assert.match(workbench, /apl-validation/);
    assert.doesNotMatch(await readFile(editorCssUrl, 'utf8'), /\.simc-apl-bilingual/);
});

test('editor uses an explicit high-contrast neutral palette instead of the pale-yellow default token color', async () => {
    const css = await readFile(editorCssUrl, 'utf8');
    assert.match(css, /\.simc-apl-editor-mount \.tok-string[^}]*color:\s*#86efac/s);
    assert.match(css, /\.simc-apl-editor-mount \.tok-keyword[^}]*color:\s*#c4b5fd/s);
    assert.doesNotMatch(css, /\.simc-apl-editor-mount[^}]*color:\s*#fde68a/s);
});
