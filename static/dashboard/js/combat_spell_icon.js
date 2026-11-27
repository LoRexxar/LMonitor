(() => {
    if (customElements.get('combat-spell-icon')) return;
    const displayIcon = window.publishedWowIconUrl || window.publishedImageUrl || (() => '');
    const legacy = new Map();
    const resolved = new Map();
    const queue = new Set();
    let loading = false;
    try {
        for (const [id, slug] of JSON.parse(sessionStorage.getItem('combat-spell-icons:v1') || '[]')) {
            if (/^[a-z0-9_-]{1,160}$/i.test(slug)) legacy.set(id, slug);
        }
    } catch { /* 浏览器可能禁用存储。 */ }

    function placeholder(node) {
        const mark = document.createElement('span');
        mark.className = 'combat-icon-placeholder';
        mark.textContent = '?';
        mark.title = '暂无技能图标 · #' + node.dataset.spell;
        node.replaceChildren(mark);
    }

    function show(node, url) {
        if (!node.isConnected || !url) return;
        const img = new Image(22, 22);
        img.className = 'combat-entity-icon';
        img.alt = '';
        img.onerror = () => placeholder(node);
        img.src = url;
        node.replaceChildren(img);
    }

    function sources(node) {
        const scope = node.closest('[data-icon-sources]') || document.querySelector('#explorer');
        return scope?.dataset.iconSources || '';
    }

    async function resolveBatch(ids, scope) {
        const icons = new Map();
        for (const id of ids) {
            if (resolved.has(id)) icons.set(id, resolved.get(id));
            else if (legacy.has(id)) {
                const url = await displayIcon('https://wow.zamimg.com/images/wow/icons/small/' + legacy.get(id) + '.jpg');
                if (url) icons.set(id, url);
            }
        }
        for (const path of scope.split(/\s+/).filter(Boolean)) {
            const missing = ids.filter(id => !icons.has(id));
            if (!missing.length) break;
            const url = new URL(path, location.origin);
            if (url.origin !== location.origin) continue;
            missing.forEach(id => url.searchParams.append('id', id));
            const response = await fetch(url, {mode:'same-origin', signal:AbortSignal.timeout(30000)});
            if (!response.ok || response.redirected) continue;
            const data = await response.json();
            for (const [id, image] of Object.entries(data.icons)) {
                icons.set(id, image ? await displayIcon(image) : '');
            }
        }
        for (const [id, image] of icons) if (image) resolved.set(id, image);
        return icons;
    }

    async function drain() {
        if (loading) return;
        loading = true;
        try {
            while (queue.size) {
                const scope = sources(queue.values().next().value);
                const nodes = [], ids = new Set();
                for (const node of queue) {
                    const id = node.dataset.spell;
                    if (sources(node) !== scope || (ids.size === 8 && !ids.has(id))) continue;
                    queue.delete(node);
                    visible.unobserve(node);
                    nodes.push(node);
                    ids.add(id);
                }
                try {
                    const icons = await resolveBatch([...ids], scope);
                    for (const node of nodes) show(node, icons.get(node.dataset.spell));
                } catch { /* 图标查询失败时保留编号和占位，统计仍可用。 */ }
            }
        } finally { loading = false; }
    }

    const visible = new IntersectionObserver(entries => {
        for (const {target, isIntersecting} of entries) {
            if (isIntersecting) queue.add(target);
            else queue.delete(target);
        }
        drain();
    });
    customElements.define('combat-spell-icon', class extends HTMLElement {
        connectedCallback() {
            const img = this.querySelector('img');
            if (img) {
                img.onerror = () => placeholder(this);
                if (img.complete && !img.naturalWidth) placeholder(this);
                return;
            }
            placeholder(this);
            if (/^[1-9]\d{0,9}$/.test(this.dataset.spell)) visible.observe(this);
        }
        disconnectedCallback() {
            queue.delete(this);
            visible.unobserve(this);
        }
    });
})();
