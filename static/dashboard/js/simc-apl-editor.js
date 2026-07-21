import {
    EditorState, EditorView, keymap, lineNumbers, highlightActiveLine,
    highlightActiveLineGutter, drawSelection, dropCursor, rectangularSelection,
    crosshairCursor, defaultKeymap, history, historyKeymap, indentWithTab,
    syntaxHighlighting, defaultHighlightStyle, bracketMatching, foldGutter,
    foldKeymap, autocompletion, completionKeymap, closeBrackets,
    closeBracketsKeymap, lintGutter, lintKeymap, setDiagnostics,
    searchKeymap, highlightSelectionMatches,
} from '../../vendor/codemirror/codemirror-6.0.1.bundle.js';
import {simcAplLanguage} from './simc-apl-language.js';

const VALIDATION_URL = '/api/simc-workbench/apl-validation/';
const COMPLETION_URL = '/api/simc-workbench/apl-completions/';

export function codePointColumnToOffset(text, lineNumber, columnNumber) {
    const lines = String(text).split('\n');
    const lineIndex = Math.max(0, Math.min(lines.length - 1, Number(lineNumber) - 1));
    const prefix = lines.slice(0, lineIndex).reduce((total, line) => total + line.length + 1, 0);
    return prefix + Array.from(lines[lineIndex]).slice(0, Math.max(0, Number(columnNumber) - 1)).join('').length;
}

export function diagnosticRangeToOffsets(text, range) {
    const from = codePointColumnToOffset(text, range?.start?.line || 1, range?.start?.column || 1);
    const to = codePointColumnToOffset(text, range?.end?.line || 1, range?.end?.column || 1);
    return {from, to: Math.max(from, to)};
}

export const editorIndentKeymap = indentWithTab;

export function completionItemsToOptions(items) {
    return (Array.isArray(items) ? items : []).map(item => ({
        label: String(item.label || item.insert_text || ''),
        apply: String(item.insert_text || item.label || ''),
        type: item.kind === 'variable' ? 'variable' : item.kind === 'action' ? 'function' : 'keyword',
        detail: String(item.name_zh || item.detail || ''),
        info: String(item.description || ''),
    })).filter(item => item.label && item.apply);
}

export function completionReplacementFrom(word) {
    if (!word) return null;
    return word.from + word.text.lastIndexOf('.') + 1;
}

export function createVersionedRequest(fetchImpl, url) {
    let version = 0;
    let controller = null;
    return {
        async run(payload, headers = {}) {
            version += 1;
            const requestVersion = version;
            if (controller) controller.abort();
            controller = new AbortController();
            const response = await fetchImpl(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json', ...headers},
                credentials: 'same-origin',
                signal: controller.signal,
                body: JSON.stringify({...payload, document_version: requestVersion}),
            });
            const body = await response.json();
            if (!response.ok || body.success !== true) throw new Error(body.error?.message || body.error || '请求失败');
            if (requestVersion !== version || body.data?.document_version !== requestVersion) return null;
            return body.data;
        },
        cancel() {
            version += 1;
            if (controller) controller.abort();
            controller = null;
        },
        get version() { return version; },
    };
}

function positionPayload(state, position) {
    const line = state.doc.lineAt(position);
    return {line: line.number, column: Array.from(line.text.slice(0, position - line.from)).length + 1};
}

function severity(value) {
    return value === 'warning' || value === 'info' ? value : 'error';
}

export function createSimcAplEditor(options) {
    const mount = options.mount;
    if (!mount) throw new Error('APL editor mount is required');
    const fetchImpl = options.fetchImpl || window.fetch.bind(window);
    const validation = createVersionedRequest(fetchImpl, VALIDATION_URL);
    const completion = createVersionedRequest(fetchImpl, COMPLETION_URL);
    const validationDelay = Number.isFinite(options.validationDelay) ? options.validationDelay : 450;
    let destroyed = false;
    let validationTimer = null;
    let diagnostics = [];

    const status = options.status || null;
    const diagnosticsHost = options.diagnosticsHost || null;
    const csrfToken = options.csrfToken || '';
    const requestHeaders = csrfToken ? {'X-CSRFToken': csrfToken} : {};

    function renderDiagnostics(view) {
        if (!diagnosticsHost) return;
        diagnosticsHost.replaceChildren();
        if (!diagnostics.length) {
            const empty = document.createElement('span');
            empty.className = 'simc-apl-diagnostic-empty';
            empty.textContent = '未发现结构问题';
            diagnosticsHost.append(empty);
            return;
        }
        diagnostics.forEach(item => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `simc-apl-diagnostic simc-apl-diagnostic--${item.severity}`;
            button.textContent = `${item.line}:${item.column} ${item.message}`;
            button.addEventListener('click', () => {
                if (destroyed) return;
                view.dispatch({selection: {anchor: item.from}, scrollIntoView: true});
                view.focus();
            }, {once: true});
            diagnosticsHost.append(button);
        });
    }

    async function validate(view) {
        if (destroyed) return;
        if (status) status.textContent = '检查中';
        try {
            const text = view.state.doc.toString();
            const data = await validation.run({
                content: text,
                spec: String(options.getSpec?.() || ''),
                mode: 'structural',
            }, requestHeaders);
            if (!data || destroyed) return;
            diagnostics = (data.diagnostics || []).map(item => {
                const offsets = diagnosticRangeToOffsets(text, item.range);
                return {
                    ...offsets,
                    severity: severity(item.severity),
                    message: String(item.message || item.code || 'APL 结构错误'),
                    line: item.range?.start?.line || 1,
                    column: item.range?.start?.column || 1,
                };
            });
            view.dispatch(setDiagnostics(view.state, diagnostics));
            renderDiagnostics(view);
            if (status) status.textContent = diagnostics.length ? `${diagnostics.length} 个问题` : '结构检查通过';
        } catch (error) {
            if (error.name === 'AbortError' || destroyed) return;
            if (status) status.textContent = '检查暂不可用';
        }
    }

    function scheduleValidation(view) {
        if (validationTimer) clearTimeout(validationTimer);
        validationTimer = setTimeout(() => validate(view), validationDelay);
    }

    async function completionSource(context) {
        try {
            const data = await completion.run({
                content: context.state.doc.toString(),
                position: positionPayload(context.state, context.pos),
                spec: String(options.getSpec?.() || ''),
            }, requestHeaders);
            if (!data || destroyed) return null;
            const word = context.matchBefore(/[\p{L}\p{N}_.]*/u);
            if (!context.explicit && (!word || word.from === word.to)) return null;
            return {
                from: completionReplacementFrom(word) ?? context.pos,
                options: completionItemsToOptions(data.items),
            };
        } catch (error) {
            if (error.name !== 'AbortError' && status) status.textContent = '补全暂不可用';
            return null;
        }
    }

    const view = new EditorView({
        parent: mount,
        state: EditorState.create({
            doc: String(options.value || ''),
            extensions: [
                lineNumbers(), highlightActiveLineGutter(), history(), foldGutter(),
                drawSelection(), dropCursor(), rectangularSelection(), crosshairCursor(),
                highlightActiveLine(), highlightSelectionMatches(), bracketMatching(), closeBrackets(),
                simcAplLanguage, syntaxHighlighting(defaultHighlightStyle, {fallback: true}),
                autocompletion({override: [completionSource], activateOnTyping: true}),
                lintGutter(),
                keymap.of([
                    indentWithTab,
                    ...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap,
                    ...foldKeymap, ...completionKeymap, ...lintKeymap, ...searchKeymap,
                ]),
                EditorView.lineWrapping,
                EditorView.updateListener.of(update => {
                    if (!update.docChanged) return;
                    const value = update.state.doc.toString();
                    options.onChange?.(value);
                    scheduleValidation(update.view);
                }),
            ],
        }),
    });

    renderDiagnostics(view);
    scheduleValidation(view);

    return {
        getValue: () => view.state.doc.toString(),
        focus: () => view.focus(),
        revalidate: () => { validation.cancel(); scheduleValidation(view); },
        destroy() {
            if (destroyed) return;
            destroyed = true;
            if (validationTimer) clearTimeout(validationTimer);
            validation.cancel();
            completion.cancel();
            view.destroy();
            mount.replaceChildren();
        },
    };
}
