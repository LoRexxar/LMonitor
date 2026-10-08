// 输入真实后台投影，执行产品选择函数，输出每件装备的增强目录。
import fs from 'node:fs';
import vm from 'node:vm';
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(new URL('../../../static/portal/js/gear-catalog.js', import.meta.url), 'utf8'), context);
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(cases.map(({payload, variantId}) => context.window.WowGearCatalog.selectEnhancements(payload, variantId))));
