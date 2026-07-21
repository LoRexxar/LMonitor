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
const SYMBOLS_URL = '/api/simc-workbench/apl-symbols/';
const KEYWORDS_URL = '/api/simc-workbench/apl-keywords/';
const BILINGUAL_URL = '/api/convert-text/';

const CATALOG_CATEGORIES = [
    ['all', '全部'], ['class', '职业'], ['spec', '专精'], ['talent', '天赋'],
    ['hero_tree', '英雄'], ['global', '通用'],
];

function catalogCategory(item) {
    if (item.scope === 'hero_tree' || item.kind === 'hero_tree') return 'hero_tree';
    if (item.kind === 'talent') return 'talent';
    if (item.scope === 'spec') return 'spec';
    if (item.scope === 'class') return 'class';
    return 'global';
}

function tokenAt(state, position) {
    const line = state.doc.lineAt(position);
    const relative = position - line.from;
    const matcher = /[\p{L}\p{N}_.]+/gu;
    for (const match of line.text.matchAll(matcher)) {
        if (match.index <= relative && relative <= match.index + match[0].length) return match[0];
    }
    return '';
}

function createCatalogAssistant(options) {
    const host = options.host;
    if (!host) return {reload() {}, updateContext() {}, destroy() {}};
    const fetchImpl = options.fetchImpl;
    let controller = null;
    let contextController = null;
    let contextTimer = null;
    let destroyed = false;
    let page = 1;
    let totalPages = 1;
    let query = '';
    let category = 'all';
    let items = [];
    let contextToken = '';
    let contextItem = null;

    host.innerHTML = `<div class="simc-apl-assistant__mobile-heading"><strong>技能助手</strong><button type="button" data-apl-assistant-close>关闭</button></div><div class="simc-apl-assistant__toolbar">
        <label class="simc-apl-assistant__search"><span class="sr-only">搜索技能</span><input type="search" data-apl-catalog-query placeholder="搜索中文、英文、Token 或 SpellID"></label>
        <div class="simc-apl-assistant__categories" data-apl-catalog-categories>${CATALOG_CATEGORIES.map(([value, label]) => `<button type="button" data-category="${value}" class="${value === 'all' ? 'is-active' : ''}">${label}</button>`).join('')}</div>
    </div><div class="simc-apl-context" data-apl-context-help><span>将光标放在 APL token 上查看中文说明。</span></div>
    <div class="simc-apl-catalog" data-apl-catalog-list></div>
    <div class="simc-apl-catalog__pager"><button type="button" data-page-action="previous">上一页</button><span data-page-summary>第 1 页</span><button type="button" data-page-action="next">下一页</button></div>`;
    const list = host.querySelector('[data-apl-catalog-list]');
    const contextHelp = host.querySelector('[data-apl-context-help]');
    const summary = host.querySelector('[data-page-summary]');
    host.querySelector('[data-apl-assistant-close]').addEventListener('click', () => options.close?.());

    function metadata(item) {
        return `${item.source || '未知来源'} · SimC ${item.simc_revision || '-'} · Build ${item.game_build || '-'}`;
    }

    function renderContext() {
        contextHelp.replaceChildren();
        if (!contextToken) {
            const hint = document.createElement('span');
            hint.textContent = '将光标放在 APL token 上查看中文说明。';
            contextHelp.append(hint);
            return;
        }
        const item = contextItem;
        const token = document.createElement('code');
        token.textContent = contextToken;
        if (!item) {
            const hint = document.createElement('span');
            hint.textContent = '当前目录页没有匹配说明，可使用搜索定位。';
            contextHelp.append(token, hint);
            return;
        }
        const name = document.createElement('strong');
        name.textContent = item.name_zh || item.name_en || item.token;
        const description = document.createElement('span');
        description.textContent = item.description_zh || '暂无中文说明';
        const meta = document.createElement('small');
        meta.textContent = metadata(item);
        contextHelp.append(name, token, description, meta);
    }

    function render() {
        const visible = category === 'all' ? items : items.filter(item => catalogCategory(item) === category);
        list.replaceChildren();
        if (!visible.length) {
            const empty = document.createElement('p');
            empty.className = 'simc-apl-catalog__empty';
            empty.textContent = '当前分类没有匹配技能';
            list.append(empty);
        }
        visible.forEach(item => {
            const card = document.createElement('article');
            card.className = `simc-apl-skill${item.insertable ? '' : ' is-disabled'}`;
            card.title = metadata(item);
            const heading = document.createElement('div');
            heading.className = 'simc-apl-skill__heading';
            const text = document.createElement('div');
            const name = document.createElement('strong');
            name.textContent = item.name_zh || item.name_en || item.token || `Spell ${item.spell_id}`;
            const token = document.createElement('code');
            token.textContent = item.token || `SpellID ${item.spell_id || '-'}`;
            text.append(name, token);
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = item.insertable ? '插入' : '不可插入';
            button.disabled = !item.insertable || !item.token;
            if (!button.disabled) button.addEventListener('click', () => options.insert(item.token));
            heading.append(text, button);
            const description = document.createElement('p');
            description.textContent = item.description_zh || item.name_en || '暂无说明';
            const foot = document.createElement('small');
            foot.textContent = item.insertable ? metadata(item) : (item.reason || '尚无 SimC token 映射');
            card.append(heading, description, foot);
            list.append(card);
        });
        summary.textContent = `第 ${page}/${Math.max(1, totalPages)} 页`;
        host.querySelector('[data-page-action="previous"]').disabled = page <= 1;
        host.querySelector('[data-page-action="next"]').disabled = page >= totalPages;
        renderContext();
    }

    async function loadKeywordCatalog(activeController = controller) {
        const response = await fetchImpl(KEYWORDS_URL, {credentials: 'same-origin', signal: activeController.signal});
        const body = await response.json();
        if (!response.ok || body.success !== true) throw new Error('中英文关键词库不可用');
        if (destroyed || controller !== activeController || activeController.signal.aborted) return;
        const matches = keywordPairsToCatalogItems(body.data || [], query);
        totalPages = Math.max(1, Math.ceil(matches.length / 50));
        if (page > totalPages) page = totalPages;
        items = matches.slice((page - 1) * 50, page * 50);
        render();
    }

    async function load(resetPage = false) {
        if (destroyed) return;
        if (resetPage) page = 1;
        if (controller) controller.abort();
        controller = new AbortController();
        replaceTextMessage(list, '目录加载中…');
        const params = new URLSearchParams({spec: String(options.getSpec() || ''), page: String(page), page_size: '50'});
        if (query) params.set('query', query);
        try {
            const response = await fetchImpl(`${SYMBOLS_URL}?${params}`, {credentials: 'same-origin', signal: controller.signal});
            const body = await response.json();
            if (!response.ok || body.success !== true) {
                if (body.error?.code === 'catalog_unavailable') {
                    await loadKeywordCatalog(controller);
                    return;
                }
                throw new Error(body.error?.message || '技能目录不可用');
            }
            if (destroyed) return;
            items = body.data?.items || [];
            totalPages = body.data?.pagination?.total_pages || 1;
            render();
        } catch (error) {
            if (error.name === 'AbortError' || destroyed) return;
            replaceTextMessage(list, error.message || '技能目录暂不可用');
        }
    }

    async function loadContext(token) {
        if (contextController) contextController.abort();
        if (!token || destroyed) {
            contextItem = null;
            renderContext();
            return;
        }
        contextController = new AbortController();
        const params = new URLSearchParams({spec: String(options.getSpec() || ''), query: token, page: '1', page_size: '20'});
        try {
            const response = await fetchImpl(`${SYMBOLS_URL}?${params}`, {credentials: 'same-origin', signal: contextController.signal});
            const body = await response.json();
            if (!response.ok || body.success !== true) {
                if (body.error?.code === 'catalog_unavailable') {
                    const keywordResponse = await fetchImpl(KEYWORDS_URL, {credentials: 'same-origin', signal: contextController.signal});
                    const keywordBody = await keywordResponse.json();
                    if (!keywordResponse.ok || keywordBody.success !== true || destroyed || contextToken !== token) return;
                    const fallback = keywordPairsToCatalogItems(keywordBody.data || [], token)
                        .find(row => row.token === token || row.token.replaceAll('_', ' ') === token);
                    contextItem = fallback || null;
                    renderContext();
                    return;
                }
                return;
            }
            if (destroyed || contextToken !== token) return;
            contextItem = (body.data?.items || []).find(row => row.token === token) || null;
            renderContext();
        } catch (error) {
            if (error.name !== 'AbortError' && !destroyed && contextToken === token) renderContext();
        }
    }

    let searchTimer = null;
    host.querySelector('[data-apl-catalog-query]').addEventListener('input', event => {
        query = event.target.value.trim();
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => load(true), 250);
    });
    host.querySelector('[data-apl-catalog-categories]').addEventListener('click', event => {
        const button = event.target.closest('[data-category]');
        if (!button) return;
        category = button.dataset.category;
        host.querySelectorAll('[data-category]').forEach(node => node.classList.toggle('is-active', node === button));
        render();
    });
    host.querySelector('[data-page-action="previous"]').addEventListener('click', () => { if (page > 1) { page -= 1; load(); } });
    host.querySelector('[data-page-action="next"]').addEventListener('click', () => { if (page < totalPages) { page += 1; load(); } });
    load();
    return {
        reload: () => { contextItem = null; load(true); loadContext(contextToken); },
        updateContext(token) {
            if (token === contextToken) return;
            contextToken = token;
            contextItem = null;
            renderContext();
            clearTimeout(contextTimer);
            contextTimer = setTimeout(() => loadContext(token), 180);
        },
        destroy() {
            destroyed = true;
            clearTimeout(searchTimer);
            clearTimeout(contextTimer);
            if (controller) controller.abort();
            if (contextController) contextController.abort();
            host.replaceChildren();
        },
    };
}

export function replaceTextMessage(host, message) {
    const node = document.createElement('p');
    node.className = 'simc-apl-catalog__empty';
    node.textContent = String(message || '');
    host.replaceChildren(node);
    return node;
}

export async function runSingleSubmission(form, task) {
    if (!form || form.dataset.aplSubmitting === '1') return false;
    const submitButton = form.querySelector('[type="submit"]');
    form.dataset.aplSubmitting = '1';
    form.setAttribute('aria-busy', 'true');
    if (submitButton) submitButton.disabled = true;
    try {
        await task();
        return true;
    } finally {
        delete form.dataset.aplSubmitting;
        form.removeAttribute('aria-busy');
        if (submitButton) submitButton.disabled = false;
    }
}

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

export function catalogItemsToCompletionOptions(items) {
    return (Array.isArray(items) ? items : [])
        .filter(item => item?.insertable === true && item.token)
        .map(item => {
            const token = String(item.token);
            const nameZh = String(item.name_zh || '');
            const nameEn = String(item.name_en || '');
            return {
                label: nameZh ? `${nameZh} · ${token}` : (nameEn ? `${nameEn} · ${token}` : token),
                apply: token,
                type: item.kind === 'action' ? 'function' : 'variable',
                detail: nameEn,
                info: String(item.description_zh || ''),
            };
        });
}

export function keywordPairsToCatalogItems(items, query = '') {
    const normalizedQuery = String(query || '').trim().toLocaleLowerCase();
    return (Array.isArray(items) ? items : [])
        .filter(item => item?.is_active !== false && /^[a-z0-9_]+$/i.test(String(item?.apl_keyword || '')))
        .filter(item => {
            if (!normalizedQuery) return true;
            return [item.apl_keyword, item.cn_keyword, item.description]
                .some(value => String(value || '').toLocaleLowerCase().includes(normalizedQuery));
        })
        .map(item => ({
            token: String(item.apl_keyword),
            kind: 'keyword',
            scope: 'global',
            insertable: true,
            name_zh: String(item.cn_keyword || ''),
            name_en: '',
            description_zh: String(item.description || ''),
            source: '中英文关键词库',
            simc_revision: '',
            game_build: '',
        }));
}

export function keywordPairsToCompletionOptions(items, query = '') {
    return keywordPairsToCatalogItems(items, query).map(item => ({
        label: item.name_zh ? `${item.name_zh} · ${item.token}` : item.token,
        apply: item.token,
        type: 'function',
        detail: 'APL 关键词',
        info: item.description_zh,
    }));
}

export function mergeCompletionOptions(documentOptions, catalogOptions) {
    const merged = [];
    const seen = new Set();
    [...(documentOptions || []), ...(catalogOptions || [])].forEach(item => {
        const key = String(item?.apply || item?.label || '');
        if (!key || seen.has(key)) return;
        seen.add(key);
        merged.push(item);
    });
    return merged;
}

export function editorLanguageTransition(fromLanguage, toLanguage) {
    const from = fromLanguage === 'cn' ? 'cn' : 'apl';
    const to = toLanguage === 'cn' ? 'cn' : 'apl';
    if (from === to) return null;
    return {
        from,
        to,
        conversionType: from === 'apl' ? 'apl_to_cn' : 'cn_to_apl',
    };
}

export async function convertDocumentSnapshot({source, version, getValue, getVersion, convert}) {
    const converted = await convert(source);
    if (getVersion() !== version || getValue() !== source) {
        throw new Error('转换期间正文已变化，请确认最新内容后重试保存');
    }
    return String(converted || '');
}

export function selectDefaultAplsForSpec(rows, spec) {
    const normalizedSpec = String(spec || '').trim().toLowerCase();
    if (!normalizedSpec) return [];
    return (Array.isArray(rows) ? rows : []).filter(row => (
        String(row?.spec || '').trim().toLowerCase() === normalizedSpec
        && row?.is_system === true
        && row?.is_active !== false
        && row?.is_selectable !== false
    ));
}

export function selectDefaultAplForSpec(rows, spec) {
    const normalizedSpec = String(spec || '').trim().toLowerCase();
    const matches = selectDefaultAplsForSpec(rows, normalizedSpec);
    if (matches.length > 1) throw new Error(`专精 ${normalizedSpec} 存在多个系统默认 APL`);
    return matches[0] || null;
}

export function formatStructuralValidationStatus(summary) {
    const errors = Number(summary?.error || 0);
    const warnings = Number(summary?.warning || 0);
    if (!errors && !warnings) return '结构检查通过';
    return `${errors} 个错误 · ${warnings} 个警告`;
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
    let assistant = null;
    let catalogCompletionController = null;
    let completionGeneration = 0;
    let keywordPairsPromise = null;
    let languageController = null;
    let languageVersion = 0;
    let documentVersion = 0;
    let editorLanguage = 'apl';

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
        if (destroyed) return null;
        if (validationTimer) {
            clearTimeout(validationTimer);
            validationTimer = null;
        }
        if (status) status.textContent = '结构检查中…';
        try {
            const text = view.state.doc.toString();
            const data = await validation.run({
                content: text,
                spec: String(options.getSpec?.() || ''),
                mode: 'structural',
            }, requestHeaders);
            if (!data || destroyed || text !== view.state.doc.toString()) return null;
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
            const summary = diagnostics.reduce((counts, item) => {
                counts[item.severity] = (counts[item.severity] || 0) + 1;
                return counts;
            }, {error: 0, warning: 0, info: 0});
            if (status) status.textContent = formatStructuralValidationStatus(summary);
            return {data, diagnostics: [...diagnostics], summary};
        } catch (error) {
            if (error.name === 'AbortError' || destroyed) return null;
            if (status) status.textContent = '结构检查失败，请重试';
            return null;
        }
    }

    function invalidateValidation(view) {
        validation.cancel();
        diagnostics = [];
        renderDiagnostics(view);
        if (status) status.textContent = '等待结构检查';
        queueMicrotask(() => {
            if (!destroyed) view.dispatch(setDiagnostics(view.state, []));
        });
    }

    function scheduleValidation(view) {
        if (validationTimer) clearTimeout(validationTimer);
        validationTimer = setTimeout(() => validate(view), validationDelay);
    }

    async function loadCatalogCompletions(query) {
        if (catalogCompletionController) catalogCompletionController.abort();
        const controller = new AbortController();
        catalogCompletionController = controller;
        const spec = String(options.getSpec?.() || '');
        if (!spec) return null;
        const params = new URLSearchParams({spec, query: String(query || ''), page: '1', page_size: '50'});
        try {
            const response = await fetchImpl(`${SYMBOLS_URL}?${params}`, {
                credentials: 'same-origin', signal: controller.signal,
            });
            const body = await response.json();
            if (!response.ok || body.success !== true || controller !== catalogCompletionController) return null;
            return catalogItemsToCompletionOptions(body.data?.items || []);
        } catch (error) {
            return null;
        }
    }

    async function loadKeywordFallback(query) {
        if (!keywordPairsPromise) {
            keywordPairsPromise = fetchImpl(KEYWORDS_URL, {credentials: 'same-origin'})
                .then(async response => {
                    const body = await response.json();
                    if (!response.ok || body.success !== true) throw new Error('关键词库不可用');
                    return Array.isArray(body.data) ? body.data : [];
                })
                .catch(() => {
                    keywordPairsPromise = null;
                    return [];
                });
        }
        return keywordPairsToCompletionOptions(await keywordPairsPromise, query);
    }

    async function requestLanguageConversion(text, conversionType, signal) {
        const response = await fetchImpl(BILINGUAL_URL, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', ...requestHeaders},
            credentials: 'same-origin',
            body: JSON.stringify({text, conversion_type: conversionType}),
            signal,
        });
        const body = await response.json();
        if (!response.ok || body.success !== true) throw new Error(body.error || 'APL 中英文转换失败');
        return String(body.result || '');
    }

    async function convertLanguage(targetLanguage) {
        const transition = editorLanguageTransition(editorLanguage, targetLanguage);
        if (!transition) return view.state.doc.toString();
        languageController?.abort();
        const controller = new AbortController();
        languageController = controller;
        const requestVersion = ++languageVersion;
        const source = view.state.doc.toString();
        const version = documentVersion;
        const converted = await requestLanguageConversion(
            source, transition.conversionType, controller.signal,
        );
        if (destroyed || controller.signal.aborted || requestVersion !== languageVersion) return null;
        if (documentVersion !== version || view.state.doc.toString() !== source) {
            throw new Error('转换期间正文已变化，请确认最新内容后重试');
        }
        editorLanguage = transition.to;
        diagnostics = [];
        view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: converted}});
        view.focus();
        options.onLanguageChange?.(editorLanguage);
        return converted;
    }

    async function completionSource(context) {
        const generation = ++completionGeneration;
        const word = context.matchBefore(/[\p{L}\p{N}_.]*/u);
        if (!context.explicit && (!word || word.from === word.to)) return null;
        const query = String(word?.text || '').split('.').pop();
        const documentRequest = completion.run({
            content: context.state.doc.toString(),
            position: positionPayload(context.state, context.pos),
            spec: String(options.getSpec?.() || ''),
        }, requestHeaders);
        const [documentResult, catalogResult] = await Promise.allSettled([
            documentRequest,
            loadCatalogCompletions(query),
        ]);
        if (destroyed || generation !== completionGeneration) return null;
        const data = documentResult.status === 'fulfilled' ? documentResult.value : null;
        const documentOptions = completionItemsToOptions(data?.items || []);
        let catalogOptions = catalogResult.status === 'fulfilled' ? catalogResult.value : null;
        if (catalogOptions === null) catalogOptions = await loadKeywordFallback(query);
        if (destroyed || generation !== completionGeneration) return null;
        const completionOptions = mergeCompletionOptions(documentOptions, catalogOptions || []);
        if (!completionOptions.length) return null;
        return {
            from: completionReplacementFrom(word) ?? context.pos,
            options: completionOptions,
        };
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
                    if (update.docChanged) {
                        documentVersion += 1;
                        const value = update.state.doc.toString();
                        options.onChange?.(value);
                        invalidateValidation(update.view);
                        if (editorLanguage === 'apl') scheduleValidation(update.view);
                    }
                    if (update.docChanged || update.selectionSet) {
                        assistant?.updateContext(tokenAt(update.state, update.state.selection.main.head));
                    }
                }),
            ],
        }),
    });

    assistant = createCatalogAssistant({
        host: options.assistantHost,
        fetchImpl,
        getSpec: options.getSpec,
        close: options.closeAssistant,
        insert(token) {
            const head = view.state.selection.main.head;
            view.dispatch({changes: {from: head, insert: token}, selection: {anchor: head + token.length}});
            view.focus();
        },
    });
    assistant.updateContext(tokenAt(view.state, view.state.selection.main.head));
    renderDiagnostics(view);
    scheduleValidation(view);

    return {
        getValue: () => view.state.doc.toString(),
        getLanguage: () => editorLanguage,
        setValue(value, language = 'apl') {
            editorLanguage = language === 'cn' ? 'cn' : 'apl';
            languageVersion += 1;
            languageController?.abort();
            view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: String(value || '')}});
            options.onLanguageChange?.(editorLanguage);
        },
        async convertLanguage(targetLanguage) {
            return convertLanguage(targetLanguage);
        },
        async getValueForSave() {
            return (await this.getSaveSnapshot()).content;
        },
        async getSaveSnapshot() {
            const source = view.state.doc.toString();
            const version = documentVersion;
            const language = editorLanguage;
            const content = language === 'apl' ? source : await convertDocumentSnapshot({
                source, version,
                getValue: () => view.state.doc.toString(),
                getVersion: () => documentVersion,
                convert: value => requestLanguageConversion(value, 'cn_to_apl'),
            });
            if (destroyed || documentVersion !== version || view.state.doc.toString() !== source) {
                throw new Error('保存准备期间正文已变化，请确认最新内容后重试保存');
            }
            return {content, source, language, version};
        },
        isCurrentSaveSnapshot(snapshot) {
            return !destroyed && snapshot?.version === documentVersion
                && snapshot?.source === view.state.doc.toString()
                && snapshot?.language === editorLanguage;
        },
        focus: () => view.focus(),
        validateNow: () => editorLanguage === 'apl' ? validate(view) : Promise.resolve(null),
        revalidate: () => {
            completionGeneration += 1;
            completion.cancel();
            if (catalogCompletionController) catalogCompletionController.abort();
            invalidateValidation(view);
            if (editorLanguage === 'apl') scheduleValidation(view);
            assistant.reload();
        },
        destroy() {
            if (destroyed) return;
            destroyed = true;
            completionGeneration += 1;
            if (validationTimer) clearTimeout(validationTimer);
            validation.cancel();
            completion.cancel();
            if (catalogCompletionController) catalogCompletionController.abort();
            languageController?.abort();
            assistant.destroy();
            view.destroy();
            mount.replaceChildren();
        },
    };
}
