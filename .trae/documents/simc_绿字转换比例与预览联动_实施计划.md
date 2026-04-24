# SimC 绿字转换比例与预览联动实施计划

## Summary
- 目标：新增“按专精配置绿字换算比例”的数据模型，并在前端提供可编辑表格；在 SimC 配置新增/编辑弹窗中实时展示暴击、急速、精通、全能的换算百分比。
- 成功标准：
  - 后端存在独立转换表，字段满足：专精 + 暴击换算值 + 急速换算值 + 精通换算值 + 精通系数 + 全能换算值（共 6 列，含专精标识）。
  - 前端可在侧栏进入该表，支持展示/新增/编辑/删除。
  - SimC 配置预览区实时展示四项百分比，精通按“(精通绿字 / 精通换算值) * 精通系数”计算。
  - 迁移后自动插入 fury 基线数据：46 / 44 / 46 / 1.4 / 54。
  - 百分比统一保留 2 位小数。
  - 专精未配置时，预览显示“未配置（--）”，不回退 fury。

## Current State Analysis
- 数据模型：
  - `botend/models.py` 当前包含 `SimcProfile`，仅存储绿字数值（`gear_crit/haste/mastery/versatility`），没有“按专精换算比例”表。
- 后台通用表格：
  - `botend/dashboard/dashboard.py` 的 `DashboardView` 负责左侧表清单、`get_table_data`、`create/update/delete`。
  - 该文件内多个 `model_map` 与 `models` 列表决定某表是否可在前端管理。
- 前端 SimC 配置页：
  - `templates/dashboard/index.html` 中新增/编辑 SimC 配置弹窗已有属性输入区和“天赋预览”区域。
  - `static/dashboard/js/main.js` 已有 `updateTalentPreview()`、`openAddSimcProfileModal()`、`openEditSimcProfileModal()` 与属性输入变更监听能力，可复用做实时百分比预览。

## Proposed Changes

### 1) 新增模型与迁移
- 文件：`botend/models.py`
- 变更：
  - 新增模型 `SimcSecondaryStatRule`（命名可按现有风格微调），字段：
    - `spec` (CharField, 唯一)
    - `crit_per_percent` (Float/Integer，建议 Float 便于未来小数)
    - `haste_per_percent`
    - `mastery_per_percent`
    - `mastery_coefficient`
    - `versatility_per_percent`
    - 可选：`is_active`（若希望软停用；本期默认不加，保持简洁）
  - `Meta.db_table` 与中文 `verbose_name` 补齐。
- 文件：`botend/migrations/0040_simcsecondarystatrule.py`（新建）
- 变更：
  - 创建新表。
  - 使用 `RunPython` 写入默认 fury 记录：
    - `spec='fury'`
    - `crit_per_percent=46`
    - `haste_per_percent=44`
    - `mastery_per_percent=46`
    - `mastery_coefficient=1.4`
    - `versatility_per_percent=54`
  - 保证幂等（存在则跳过）。

### 2) 接入 Dashboard 通用表管理
- 文件：`botend/dashboard/dashboard.py`
- 变更：
  - 顶部模型导入加入新模型。
  - `MODEL_DESCRIPTIONS` 增加中文描述（如“绿字转换比例”）。
  - `get()` 的 `models` 列表加入新模型，以便左侧菜单展示。
  - `get_table_data()` 的 `model_map` 加入新模型。
  - `update_table_row()` / `delete_table_row()` / `create_table_row()` 的 `model_map` 同步加入新模型，确保可增删改。
  - 可选：为该表增加 `order_by('spec')` 的专门分支，提升展示稳定性。

### 3) 前端菜单与表格显示适配
- 文件：`templates/dashboard/index.html`
- 变更：
  - 利用现有 `tables_info` 自动渲染机制即可出现新菜单项；若需要可在 SimC 分组下额外加显式入口（与 `SimcProfile/SimcTask` 一致的体验）。
- 文件：`static/dashboard/js/main.js`
- 变更：
  - `displayFields` 对新表定制显示列顺序：
    - `spec`, `crit_per_percent`, `haste_per_percent`, `mastery_per_percent`, `mastery_coefficient`, `versatility_per_percent`
  - 数值列右对齐并保留合理显示格式（不强制整数，允许 `1.4`）。

### 4) SimC 配置预览新增“绿字百分比”展示
- 文件：`templates/dashboard/index.html`
- 变更：
  - 在新增/编辑 SimC 配置弹窗的属性输入区域下方，新增一个小型预览块（两处都要）：
    - 显示当前专精使用的换算参数概览；
    - 显示四项结果：暴击%、急速%、精通%、全能%；
    - 专精未配置时显示“未配置（--）”提示。
- 文件：`static/dashboard/js/main.js`
- 变更：
  - 新增加载规则函数（页面级缓存）：
    - 通过 `fetchTableData('SimcSecondaryStatRule')` 或直接 POST `get_table_data` 拉取规则，构建 `spec -> rule` 映射。
  - 新增计算函数：
    - `crit_pct = gear_crit / crit_per_percent`
    - `haste_pct = gear_haste / haste_per_percent`
    - `mastery_pct = (gear_mastery / mastery_per_percent) * mastery_coefficient`
    - `vers_pct = gear_versatility / versatility_per_percent`
    - 统一 `toFixed(2)`。
  - 在以下时机触发刷新：
    - 打开新增/编辑弹窗；
    - 专精变更；
    - 四个绿字输入框变更（`input` 事件）；
    - 复制配置后进入编辑时。
  - 对换算值为 0 或非法值做保护（显示 `--` 并给提示）。

## Assumptions & Decisions
- 决策：迁移自动写入 fury 默认换算记录（46/44/46/1.4/54）。
- 决策：百分比保留 2 位小数。
- 决策：专精未配置时不回退 fury，直接显示“未配置（--）”。
- 决策：本期采用 Dashboard 现有通用表格能力做“编辑和展示表格”，不额外新建专门 API 页面。
- 假设：`spec` 值沿用当前 SimC 配置的小写标识（如 `fury/arms/fire`）。

## Verification Steps
- 数据层：
  - 运行迁移后，确认新表存在且包含 fury 默认记录。
  - 手工插入/编辑一条非 fury 专精记录，验证唯一约束与存储正确。
- Dashboard 表格：
  - 左侧可见“绿字转换比例”菜单；
  - 可新增、编辑、删除记录；
  - 列顺序和数值显示正确。
- SimC 配置预览联动：
  - 在新增弹窗：切换专精和绿字时，四项百分比实时变化，精通按系数计算。
  - 在编辑弹窗：加载既有配置时，百分比立即显示并可随输入变更更新。
  - 删除某专精规则后，切到该专精显示“未配置（--）”。
- 回归检查：
  - 现有天赋预览（Wowhead/Raidbots）不受影响；
  - SimC 配置创建/编辑/复制流程正常。
