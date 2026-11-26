// 只在缺少站内图标且技能进入视口时查询公开技能编号。
(() => {
    const displayIcon = window.publishedWowIconUrl || window.publishedImageUrl || (url => url);
    if (customElements.get('combat-spell-icon')) return;
    const key = 'combat-spell-icons:v1';
    const slugs = new Map();
    const pending = new Map();
    const queue = new Set();
    let active = 0;
    try {
        for (const [id, slug] of JSON.parse(sessionStorage.getItem(key) || '[]')) {
            if (/^[a-z0-9_-]+$/i.test(slug)) slugs.set(id, slug);
        }
    } catch { /* 禁用浏览器存储时仅在当前页面复用。 */ }

    async function lookup(id) {
        if (slugs.has(id)) return slugs.get(id);
        if (pending.has(id)) return pending.get(id);
        const request = (async () => {
            const response = await fetch('https://nether.wowhead.com/tooltip/spell/' + id, {
                credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(6000)
            });
            if (!response.ok) return '';
            const {icon} = await response.json();
            if (typeof icon !== 'string' || !/^[a-z0-9_-]+$/i.test(icon)) return '';
            slugs.set(id, icon);
            if (slugs.size > 512) slugs.delete(slugs.keys().next().value);
            try { sessionStorage.setItem(key, JSON.stringify([...slugs])); } catch { /* 存储不可用。 */ }
            return icon;
        })();
        pending.set(id, request);
        try { return await request; } finally { pending.delete(id); }
    }

    async function draw(node) {
        try {
            const icon = await lookup(node.dataset.spell);
            if (icon && node.isConnected) {
                const img = new Image(22, 22);
                img.className = 'combat-entity-icon';
                img.alt = '';
                img.referrerPolicy = 'no-referrer';
                img.onerror = () => { node.hidden = true; };
                const url = await displayIcon('https://wow.zamimg.com/images/wow/icons/small/' + icon + '.jpg');
                if (!node.isConnected) return;
                img.src = url;
                node.replaceChildren(img);
                return;
            }
        } catch { /* 图标服务不可用时仍可查看技能名称和统计。 */ }
        if (node.isConnected) node.hidden = true;
    }

    function drain() {
        while (active < 3 && queue.size) {
            const node = queue.values().next().value;
            queue.delete(node);
            if (!node.isConnected) continue;
            visible.unobserve(node);
            active++;
            draw(node).finally(() => { active--; drain(); });
        }
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
            if (/^[1-9]\d{0,9}$/.test(this.dataset.spell) && !this.firstChild) visible.observe(this);
        }
        disconnectedCallback() {
            queue.delete(this);
            visible.unobserve(this);
        }
    });
})();
