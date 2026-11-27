// Exact releases win. The optional second lookup needs one shared static resource,
// not a per-spell resolver. Never send the source, slug or report URL with it.
(() => {
    const inventory = document.currentScript.dataset.inventory;
    const sizes = ['tiny', 'small', 'medium', 'large'];
    const zam = /^https:\/\/wow\.zamimg\.com\/images\/wow\/icons\/(tiny|small|medium|large)\/([a-zA-Z0-9_-]{1,160})\.jpg$/;
    const large = /^https:\/\/(?:static\.icy-veins\.com\/images\/wow\/large_icons|render\.worldofwarcraft\.com\/us\/icons\/56)\/([a-zA-Z0-9_-]{1,160})\.jpg$/;
    let index;
    function loadIndex() {
        return index ||= fetch(inventory, {mode:'same-origin', redirect:'error', credentials:'omit', referrerPolicy:'no-referrer', signal:AbortSignal.timeout(6000)})
            .then(async r => {
                if (!r.ok) throw Error();
                const doc = await r.json();
                if (doc.schema !== 1 || JSON.stringify(doc.sizes) !== JSON.stringify(sizes)) throw Error();
                const masks = new Map();
                for (const [mask, names] of Object.entries(doc.groups)) {
                    if (!Number.isInteger(+mask) || +mask < 1 || +mask > 15) throw Error();
                    for (const name of names.split(' ')) masks.set(name, +mask);
                }
                return masks;
            }).catch(() => new Map());
    }
    window.publishedWowIconUrl = async source => {
        const exact = window.publishedImageUrl?.(source) || '';
        if (exact || typeof source !== 'string' || source.length > 512) return exact;
        const match = zam.exec(source) || large.exec(source);
        if (!match || match[0] !== source) return '';
        const name = match.at(-1), size = match.length === 3 ? match[1] : 'large';
        // Legacy tiny/ can contain small JPEG bytes. Only exact releases qualify.
        if (size === 'tiny') return '';
        const mask = (await loadIndex()).get(name);
        return mask & (1 << sizes.indexOf(size)) ? `https://oss.wowdaily.cn/wow_icons_oss/${size}/${name}.jpg` : '';
    };
    window.setPublishedWowImage = async (image, source) => {
        const url = await window.publishedWowIconUrl(source);
        if (url) image.src = url;
    };
})();
