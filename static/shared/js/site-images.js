// Display-only lookup. Never send a page URL to a resolver or a third party.
(() => {
    // New shells load the same-origin map synchronously. Older frozen pages
    // still carry their own application/json element.
    let entries = window.publishedImageEntries;
    window.publishedImageUrl = function publishedImageUrl(source) {
        if (typeof source !== 'string' || !/^https?:\/\//.test(source)) return source;
        if (!entries) {
            const payload = document.getElementById('published-image-map');
            entries = payload ? JSON.parse(payload.textContent) : {};
        }
        return Object.hasOwn(entries, source) ? entries[source] : source;
    };

    // Article/preview HTML is already sanitized by its caller. Only visit image
    // slots inside an inert template, before the browser may start requests.
    function srcset(value) {
        // URL tokens may contain commas (data: URLs); preserve descriptors, spacing
        // and separators verbatim. No candidate is parsed as a separate request.
        let i = 0, last = 0, result = '';
        const space = c => /[\t\n\f\r ]/.test(c);
        while (i < value.length) {
            while (i < value.length && (space(value[i]) || value[i] === ',')) i++;
            const start = i;
            while (i < value.length && !space(value[i])) i++;
            const token = value.slice(start, i), url = token.replace(/,+$/, '');
            const display = window.publishedImageUrl(url);
            if (display !== url) {
                result += value.slice(last, start) + display;
                last = start + url.length;
            }
            if (token.endsWith(',')) continue;
            let depth = 0;
            while (i < value.length) {
                const c = value[i++];
                if (c === ',' && !depth) break;
                if (c === '(') depth++;
                if (c === ')' && depth) depth--;
            }
        }
        return result + value.slice(last);
    }
    function attribute(el, name, resolve = window.publishedImageUrl) {
        const raw = el.getAttribute(name);
        if (raw === null) return;
        const display = resolve(raw);
        if (display !== raw) el.setAttribute(name, display);
    }
    window.projectImageSlots = root => {
        for (const el of root.querySelectorAll('img, picture > source')) {
            for (const name of ['src', 'data-src']) attribute(el, name);
            for (const name of ['srcset', 'data-srcset']) attribute(el, name, srcset);
        }
        for (const el of root.querySelectorAll('video[poster]')) attribute(el, 'poster');
        for (const el of root.querySelectorAll('svg image')) {
            attribute(el, 'href');
            const ns = 'http://www.w3.org/1999/xlink';
            const source = el.getAttributeNS(ns, 'href');
            const display = window.publishedImageUrl(source);
            if (display !== source) el.setAttributeNS(ns, 'xlink:href', display);
        }
        for (const el of root.querySelectorAll('[style]')) {
            const style = el.style;
            const raw = style?.backgroundImage;
            if (!raw) continue;
            // CSSOM serializes literal URL tokens as quoted url("...").
            const display = raw.replace(/url\("(https?:\/\/[^"\\]+)"\)/g,
                (token, url) => url === window.publishedImageUrl(url) ? token : `url("${window.publishedImageUrl(url)}")`);
            if (display !== raw) style.setProperty('background-image', display, style.getPropertyPriority('background-image'));
        }
    };
    window.publishedImageHtml = html => {
        const template = document.createElement('template');
        template.innerHTML = html;
        window.projectImageSlots(template.content);
        return template.innerHTML;
    };
})();
