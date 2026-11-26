// Resolve before assigning an image slot. Never probe the original or use a
// mutation observer: even a detached Image can issue a request immediately.
(() => {
    let entries = window.publishedImageEntries;
    function releaseMap() {
        if (!entries) {
            try { entries = JSON.parse(document.getElementById('published-image-map')?.textContent || '{}') || {}; }
            catch { entries = {}; }
        }
        return entries;
    }
    const cdn = 'https://oss.wowdaily.cn';
    function oss(value) {
        if (typeof value !== 'string' || !value.startsWith(cdn + '/') || /[?#\\\s]/.test(value)) return '';
        try {
            const u = new URL(value);
            return u.origin === cdn && !u.username && !u.password ? u.href : '';
        } catch { return ''; }
    }
    window.publishedImageUrl = function publishedImageUrl(source) {
        if (typeof source !== 'string' || !source) return '';
        if (/^data:image\/(?:png|gif|jpeg|webp|avif|svg\+xml)[;,]/i.test(source)) return source;
        const map = releaseMap();
        const key = source.startsWith('//') ? 'https:' + source : source;
        if (Object.hasOwn(map, key)) return oss(map[key]);
        const direct = oss(source);
        if (direct) return direct;
        try {
            const u = new URL(source, location.href);
            if (u.origin !== location.origin || u.username || u.password) return '';
            if (u.protocol === 'blob:') return u.href;
            if (!/^https?:$/.test(u.protocol)) return '';
            const key = u.pathname + u.search;
            if (Object.hasOwn(map, key)) return oss(map[key]);
            if (u.pathname.startsWith('/static/') && /^\?v=[\w.-]+$/.test(u.search) && Object.hasOwn(map, u.pathname)) return oss(map[u.pathname]);
            // The hash-keyed atlas view returns image bytes, never a redirect.
            if (!u.search && /^\/portal\/api\/rio-statistics\/maps\/[a-f0-9]{64}\/$/.test(u.pathname)) return u.href;
        } catch { /* not a display URL */ }
        return '';
    };

    // Bind released CSS images before the theme stylesheet is first used.
    // Without a release the CSS uses its existing in-document vector (no request).
    for (const [property, digest] of Object.entries({
        '--published-check-image-light': '6dacbc8112b87ed35678fa93fcd78e5c9eac24c34ca28215102989a93ca97250',
        '--published-select-arrow-light': '9e2556c071133b9dbdfcc0296f4d6eda4cf57c6781ef6a13051f4737821e660b',
        '--published-check-image-dark': '6c1517566ee11aca5c38557f622603f306d08282b5819b21b6d5e174ed4c147f',
        '--published-select-arrow-dark': '8074bba1cb3cb458f67957f74f947e9c1a9d13c00cca6bcca5656c9082387c94',
    })) {
        const display = window.publishedImageUrl('css-sha256:' + digest);
        if (display) document.documentElement.style.setProperty(property, `url("${display.replaceAll('"', '%22')}")`);
    }

    function srcset(value) {
        // Consume URL tokens before descriptors: data URLs can contain commas.
        const candidates = [];
        let i = 0;
        const space = c => /[\t\n\f\r ]/.test(c);
        while (i < value.length) {
            while (i < value.length && (space(value[i]) || value[i] === ',')) i++;
            const start = i;
            while (i < value.length && !space(value[i])) i++;
            const token = value.slice(start, i);
            if (!token) break;
            const url = window.publishedImageUrl(token.replace(/,+$/, ''));
            let descriptor = '';
            if (!token.endsWith(',')) {
                const begin = i;
                while (i < value.length && value[i] !== ',') i++;
                descriptor = value.slice(begin, i).trim();
            }
            if (url && (!descriptor || /^(?:\d+w|(?:\d*\.)?\d+x)$/.test(descriptor))) {
                candidates.push(descriptor ? `${url} ${descriptor}` : url);
            }
        }
        return candidates.join(', ');
    }
    function attribute(el, name, resolve = window.publishedImageUrl) {
        if (!el.hasAttribute(name)) return;
        const display = resolve(el.getAttribute(name));
        if (display) el.setAttribute(name, display);
        else el.removeAttribute(name);
    }
    function cssValue(raw) {
        // CSSOM supplies canonical values. Do not pass through indirect image
        // functions, quoted image-set candidates or escaped/ambiguous URLs.
        if (/\\|\/\*|\b(?:image(?:-set)?|src|paint)\s*\(/i.test(raw)) return '';
        let valid = true;
        const literal = /\burl\(\s*(?:"([^"\\]*)"|'([^'\\]*)'|([^\s()"'\\]+))\s*\)/gi;
        if (/\burl\s*\(/i.test(raw.replace(literal, ''))) return '';
        const value = raw.replace(literal, (_, a, b, c) => {
            const url = a ?? b ?? c;
            const display = /^#[\w-]+$/.test(url) ? url : window.publishedImageUrl(url);
            if (!display) valid = false;
            return `url("${display.replaceAll('"', '%22')}")`;
        });
        return valid ? value : '';
    }
    window.projectImageSlots = root => {
        for (const el of root.querySelectorAll('img, picture > source, input[type="image"]')) {
            for (const name of ['src', 'data-src']) attribute(el, name);
            for (const name of ['srcset', 'data-srcset']) attribute(el, name, srcset);
            el.removeAttribute('onerror'); el.removeAttribute('onload');
        }
        for (const el of root.querySelectorAll('video[poster]')) attribute(el, 'poster');
        for (const el of root.querySelectorAll('[background]')) attribute(el, 'background');
        for (const el of root.querySelectorAll('link[rel~="icon" i], link[rel="apple-touch-icon" i], link[as="image" i]')) {
            attribute(el, 'href'); attribute(el, 'imagesrcset', srcset);
        }
        for (const el of root.querySelectorAll('svg image, svg feImage, svg use')) {
            const resolve = value => {
                if (el.localName === 'use') {
                    // External SVG symbol references must remain same-origin.
                    return /^(?:\/static\/(?:portal\/icons\/icons|dashboard\/combat_symbols)\.svg(?:\?v=\d+)?)?#[\w-]+$/.test(value) ? value : '';
                }
                return window.publishedImageUrl(value);
            };
            attribute(el, 'href', resolve);
            const ns = 'http://www.w3.org/1999/xlink';
            if (el.hasAttributeNS(ns, 'href')) {
                const url = resolve(el.getAttributeNS(ns, 'href'));
                if (url) el.setAttributeNS(ns, 'xlink:href', url);
                else el.removeAttributeNS(ns, 'href');
            }
        }
        for (const name of ['fill', 'stroke', 'filter', 'clip-path', 'mask', 'marker-start', 'marker-mid', 'marker-end', 'cursor']) {
            for (const el of root.querySelectorAll(`svg [${name}]`)) attribute(el, name, cssValue);
        }
        for (const el of root.querySelectorAll('[style]')) {
            const style = el.style;
            if (!style) continue;
            for (const name of Array.from(style)) {
                const raw = style.getPropertyValue(name), display = cssValue(raw);
                if (!display) style.removeProperty(name);
                else if (display !== raw) style.setProperty(name, display, style.getPropertyPriority(name));
            }
        }
        // Stylesheets are supplied by the shell, not by an inserted preview.
        for (const el of root.querySelectorAll('base, style, link[rel~="stylesheet" i]')) el.remove();
        for (const el of root.querySelectorAll('template')) window.projectImageSlots(el.content);
    };
    function projectedTemplate(html) {
        const template = document.createElement('template');
        template.innerHTML = html;
        window.projectImageSlots(template.content);
        return template;
    }
    window.publishedImageFragment = html => projectedTemplate(html).content;
    window.publishedImageHtml = html => projectedTemplate(html).innerHTML;
})();
