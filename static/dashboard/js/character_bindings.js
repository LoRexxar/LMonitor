const characterImageUrl = window.publishedImageUrl || (() => '');
/* Native forms work without JavaScript; enhance confirmation and pending state. */
document.querySelectorAll('[data-character-icon]').forEach(image => {
    const fallback = () => {
        const candidate = characterImageUrl(image.dataset.fallback);
        image.dataset.fallback = '';
        if (candidate && candidate !== image.src) {
            image.src = candidate;
        } else {
            image.hidden = true;
        }
    };
    image.addEventListener('error', fallback);
    if (image.complete && !image.naturalWidth) fallback();
});
document.querySelectorAll('.cb-page form[method="post"]').forEach(form => {
    form.addEventListener('submit', event => {
        if (form.dataset.pending === '1') { event.preventDefault(); return; }
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
            event.preventDefault(); return;
        }
        form.dataset.pending = '1';
        const button = event.submitter;
        if (button) {
            // Keep named submitters enabled until form data has been serialized.
            button.setAttribute('aria-disabled', 'true');
            button.textContent = button.value === 'bind' || form.elements.action?.value === 'bind' ? '正在验证…' : '正在保存…';
        }
    });
});
window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
