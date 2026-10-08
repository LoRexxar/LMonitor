// 从标准输入接收后端真实投影，执行产品筛选函数后输出可比较的业务字段。
import fs from 'node:fs';
import vm from 'node:vm';
const context = {window: {}};
vm.runInNewContext(fs.readFileSync(new URL('../../../static/portal/js/gear-catalog.js', import.meta.url), 'utf8'), context);
const requests = JSON.parse(fs.readFileSync(0, 'utf8'));
const clean = value => {
  if (Array.isArray(value)) return value.map(clean);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !key.startsWith('_')).map(([key, item]) => [key, clean(item)]));
  return value;
};
process.stdout.write(JSON.stringify(requests.map(({payload, options}) => clean(context.window.WowGearCatalog.filter(payload, options)))));
