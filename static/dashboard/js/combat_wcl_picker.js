(() => {
    const picker = document.querySelector("[data-fight-picker]");
    if (!picker) return;
    const links = Array.from(picker.querySelectorAll("[data-focus]"));
    const panels = picker.querySelectorAll("[data-group]");
    const select = picker.querySelector("select");
    const initial = select.value;

    function show(key) {
        const current = links.find(link => link.dataset.focus === key);
        if (!current) return;
        for (const link of links) {
            if (link === current) link.setAttribute("aria-current", "true");
            else link.removeAttribute("aria-current");
        }
        for (const panel of panels) panel.hidden = panel.dataset.group !== key;
        select.value = key;
    }

    function choose(key) {
        const link = links.find(item => item.dataset.focus === key);
        if (!link || link.hasAttribute("aria-current")) return;
        history.pushState(null, "", link.href);
        show(key);
    }

    for (const link of links) {
        link.addEventListener("click", event => {
            if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            choose(link.dataset.focus);
        });
    }
    select.addEventListener("change", () => choose(select.value));
    window.addEventListener("popstate", () => show(new URL(location.href).searchParams.get("focus") || initial));
    picker.dataset.interactive = "true";
})();
