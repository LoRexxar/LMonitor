const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync("static/portal/js/news_index.js", "utf8");
const requests = [];
const elements = new Map();

function makeElement() {
  return {
    hidden: false,
    innerHTML: "",
    textContent: "",
    value: "",
    disabled: false,
    classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {},
    getAttribute() { return ""; },
    addEventListener() {},
    querySelectorAll() { return []; },
  };
}

const context = {
  console,
  URL,
  URLSearchParams,
  setTimeout,
  clearTimeout,
  window: {
    location: { search: "" },
    history: { replaceState() {} },
  },
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement());
      return elements.get(id);
    },
    querySelector() { return makeElement(); },
    querySelectorAll() { return []; },
  },
  fetch(url) {
    return new Promise((resolve) => requests.push({ url, resolve }));
  },
};
vm.createContext(context);
vm.runInContext(`${source}\nthis.__state = NEWS_STATE; this.__loadNews = loadNews;`, context);

(async () => {
  requests.length = 0;
  context.__state.activeTab = "build";
  const buildLoad = context.__loadNews();
  context.__state.activeTab = "hotfix";
  const hotfixLoad = context.__loadNews();

  requests[1].resolve({
    ok: true,
    json: async () => ({ status: "success", data: [{ title: "Hotfix item" }], meta: { page: 1, total: 1, total_pages: 1 } }),
  });
  await hotfixLoad;
  const renderedAfterHotfix = elements.get("news-list").innerHTML;

  requests[0].resolve({
    ok: true,
    json: async () => ({ status: "success", data: [{ title: "Build item" }], meta: { page: 1, total: 1, total_pages: 1 } }),
  });
  await buildLoad;
  const renderedAfterStaleBuild = elements.get("news-list").innerHTML;

  if (!renderedAfterHotfix.includes("Hotfix item")) throw new Error("Hotfix response was not rendered");
  if (renderedAfterStaleBuild !== renderedAfterHotfix) throw new Error("Stale Build response overwrote active Hotfix tab");
  console.log("news_index race test passed");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
