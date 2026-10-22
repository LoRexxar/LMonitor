// 只运行来源计算器的数据与导出逻辑，界面操作由空对象接收，不打开浏览器。
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const folder = process.argv[2];
const data = Object.fromEntries(fs.readdirSync(path.join(folder, 'talent-data')).map(name =>
  [name.replace(/\.json$/, ''), JSON.parse(fs.readFileSync(path.join(folder, 'talent-data', name), 'utf8'))]));
const hashes = JSON.parse(fs.readFileSync(path.join(folder, 'talent-hashes.json'), 'utf8'));
const noop = () => {};
const element = {style: {}, classList: {add: noop, remove: noop, contains: () => false}, setAttribute: noop,
  querySelector: () => element, querySelectorAll: () => [], appendChild: noop};
const context = vm.createContext({
  console: {log: noop, warn: noop, error: noop},
  document: {querySelector: () => element, getElementById: () => element},
  window: {location: {hash: ''}}, requestAnimationFrame: noop,
  cachedData: data, inputHashes: hashes,
});
vm.runInContext(fs.readFileSync(path.join(folder, 'icy-midnight-calculator.js'), 'utf8'), context);
vm.runInContext(`
  MidnightTalentCalculatorJSON.get = async slug => {
    if (!cachedData[slug]) throw new Error('来源计算器数据缺失：' + slug);
    return cachedData[slug];
  };
  globalThis.convertAll = async () => {
    const exports = {}, errors = [];
    const renderer = new Proxy({}, {get: (obj, prop) => prop === 'idSuffix' ? '' : () => {}});
    for (const original of inputHashes) {
      const hash = original.trim();
      try {
        const calc = Object.create(MidnightTalentCalculator.prototype);
        Object.assign(calc, {embed: false, heroOnly: false, renderer,
          eventListenerManager: {heroResetButton: () => {}}});
        calc.tree = new MidnightTalentCalculatorTree(calc);
        if (!await calc.loadFromHash(hash)) throw new Error('来源无法解析专精');
        const code = calc.exportString();
        const check = new BinaryArrayReader(code, base64Table);
        if (check.read(8) !== 2 || check.read(16) !== calc.tree.specData.id) throw new Error('导出身份校验失败');
        const nodes = calc.tree.allNodeIds.map(id => calc.tree.getNodeFromId(id)).filter(Boolean)
          .filter(n => n.currentPoints > 0).map(n => ({id:n.id, ranks:n.currentPoints, choice:n.activeChoice}));
        const roundtrip = calc.toHash();
        const normalized = text => text.replace(/^#/, '').endsWith('*')
          ? text.replace(/^#/, '').slice(0, -1) : text.replace(/^#/, '');
        if (normalized(hash) !== normalized(roundtrip)) throw new Error('来源构筑往返核验不一致');
        exports[hash] = {code, spec_id:calc.tree.specData.id, nodes, roundtrip_hash:roundtrip};
      } catch (error) {
        errors.push({hash, error:String(error.stack || error)});
      }
    }
    return {exports, errors};
  };
`, context);
context.convertAll().then(result => {
  fs.writeFileSync(path.join(folder, 'talent-exports.json'), JSON.stringify(result, null, 2) + '\n');
  process.stdout.write(JSON.stringify({成功: Object.keys(result.exports).length, 异常: result.errors.length}) + '\n');
  if (result.errors.length) process.exitCode = 1;
});
