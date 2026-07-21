import assert from 'node:assert/strict';
import test from 'node:test';

import {
    codePointColumnToOffset,
    completionItemsToOptions,
    completionReplacementFrom,
    createVersionedRequest,
    diagnosticRangeToOffsets,
    editorIndentKeymap,
    replaceTextMessage,
    runSingleSubmission,
} from '../../../static/dashboard/js/simc-apl-editor.js';

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
