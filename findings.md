# SimC 现状发现

## 代码结构
- SimC 执行链路：`SimcProfile`（配置）-> `SimcTask`（任务）-> `SimcMonitor`（生成 simc + 执行）。
- 配置管理与任务管理主要在 `botend/dashboard/api.py`，前端交互集中在 `static/dashboard/js/main.js`。
- Dashboard 的 `SimcProfile` 表格当前仅展示 `name/fight_style/time/target_count`，尚无专精字段与独立筛选 UI。

## 数据模型
- `SimcProfile` 现有字段含 `fight_style/time/target_count/talent/action_list`，没有专精字段。
- `SimcTask.ext` 当前被属性模拟用于存储属性组合字符串（如 `crit_versatility`），可扩展但需兼容旧格式。

## 执行模板
- `LMonitor/simc_template.txt` 目前固定 `spec=fury`，与用户配置未联动。
- `SimcMonitor.generate_simc_code()` 仅替换模板占位符，不会动态覆盖 `spec` 行。
- 需要在模板层引入可替换的专精占位符（如 `{spec}`）并与 `SimcProfile.spec` 对齐。
- 当前模板 API 里存在“启用一个模板时禁用其他模板”的逻辑，需要移除，改为可多启用并按专精匹配执行。

## 前端能力
- SimC 配置新增/编辑模态框已有 `fight_style/time/target_count/talent/action_list` 输入。
- 已有 APL 关键字转换、保存/加载机制；但“已保存 APL 列表”内尚无“翻译预览”专用入口。
- 常规模拟任务创建流程目前仅选配置，不支持 task 级 time/target_count 覆盖输入。
- 属性模拟步长在执行器中写死为 50，当前没有前端可配置入口。

## 设计约束
- 需要保持属性模拟逻辑稳定：属性模拟依赖 `SimcTask.ext` 的字符串语义。
- 若引入 `ext` JSON，必须双向兼容（旧字符串 + 新对象）。
- 前端主脚本较长，建议以最小侵入方式新增独立函数并绑定事件。
