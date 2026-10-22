# 测试入口与维护边界

从仓库根目录运行。`botend/tests/__init__.py` 是 Django/unittest 递归发现所需的包入口；不要删除，也不要在其中批量导入测试。普通测试文件使用 `test_*.py`。

## Python / Django

```sh
python manage.py test --settings=LMonitor.settings_test_sqlite
# 聚焦模块：
python manage.py test botend.tests.test_fetch_item_metadata_command --settings=LMonitor.settings_test_sqlite
```

普通测试必须使用隔离 SQLite 设置，不能以 `settings_dev` 运行测试（该配置可能连接生产数据库）。需要 MySQL/InnoDB 锁语义的用例应在本机隔离数据库运行；SQLite 的通过或跳过不证明锁行为。

“发现 N 项”只表示成功收集，不等于 N 项全部通过。失败项、环境条件跳过与通过项分别报告；测试为空或导入失败不得当作成功。

使用文件缓存的测试通过 `aggregation_cache_helpers.isolate_work_cache(self)` 注册独立目录和清理，不能依赖共享目录、固定数据库主键或上次执行产生的缓存。

## 固定 MDT 6.2.20 测试资产（新 worktree 必须显式准备）

`test_mythic_planner.py` 的真实转换测试读取本 worktree 的 ignored
`botend/data/mythic_planner/vendor/mythic-dungeon-tools-6.2.20`。
`prepare_mdt_assets.py` 是测试专用入口，不匹配 Django `test*.py` 发现规则，
不导入 Django、不访问数据库，也不调用业务自动更新器或安装“最新版本”。
在新 worktree 根目录执行（二选一）：

```sh
# 推荐：使用已有官方缓存 ZIP，完全离线
python3 botend/tests/prepare_mdt_assets.py --archive /absolute/path/mdt-6.2.20-upstream.zip
# 仅显式选择此参数才联网，从官方 codeload 下载固定 commit
python3 botend/tests/prepare_mdt_assets.py --download
# 安装器自身的隔离验证可指定目标；Django 转换测试仍读取上面的固定路径
python3 botend/tests/prepare_mdt_assets.py --archive /absolute/path/mdt-6.2.20-upstream.zip \
  --target /tmp/my-mdt-bootstrap-check/assets
```

固定官方 commit 为 `7fd7672d41b3aff933cbfd5b0f508fc4f62e33ae`，ZIP SHA256 为
`890e7784e0cce38598c745607784b2c10438116337770f13ef5f61d3ac1eae38`。
工具仅需 Python 3.11+ 标准库及仓库现有纯 Python 转换器；原子不覆盖发布使用 Linux
`renameat2`，不支持时明确失败。先校验 ZIP 哈希、路径及成员类型，在同父目录暂存，
完成后原子安装；完整相同的已有目录只核验并返回 `already-installed`，
未知、修改过的目录（包括空目录）及符号链接拒绝覆盖，不提供 force 模式。
不要在生产目录运行，也不要用其他版本 ZIP 或复制其他版本 vendor 代替。

官方上游有 2524 个文件；项目补充表从 tracked `mdt_6_2_20.json` 恢复，
沿用 `auto_update.prepare_candidate` 的已审核来源筛选和当前 NPC 存在规则，
并保留发布包中的原 target/sources/notes 元数据，严格核对 3 个副本、283 条关系及哈希。
不手造 Lua、法术数据或额外 fixture。发布包或哈希变化需单独审核，不自动放宽校验。

这是 **MDT 文件依赖**，不是数据库或原生测试依赖：安装成功不代表 MySQL/InnoDB
锁语义、C++ harness、浏览器或整套 Django 已通过。MySQL 仍须本机隔离库；原生
C++ 仍须下节的 `g++` 与真实 `SIMC_SKILL_DAMAGE_SOURCE`，不得用 mock/skip 替代。

## Node.js

使用 Node.js 22（本套件已验证的运行版本）；DOM 测试需要 `jsdom@26.1.0`。依赖可以安装在独立临时目录，不要把 node_modules 提交到仓库或安装进生产运行目录。

```sh
npm install --prefix /tmp/lmonitor-js-tests --no-audit --no-fund --ignore-scripts jsdom@26.1.0
NODE_PATH=/tmp/lmonitor-js-tests/node_modules node --test --test-concurrency=1 \
  botend/tests/*_test.js botend/tests/js/*.test.mjs
```

`*-oracle.mjs` 是由 Python 测试通过 stdin 调用的辅助程序，不是独立测试入口。`simc_benchmark_detail_loader_test.js` 使用临时的本机 loopback HTTP 服务，不请求生产服务。

## 手动浏览器与原生 C++ 回归

以下脚本有意不匹配 Django 的 `test*.py` 发现规则；在具备 Django、Playwright 与 Chromium 的环境中单独运行：

```sh
python botend/tests/dashboard_sidebar_grouping_browser_test.py
python botend/tests/frontend_ai_management_regression.py
python botend/tests/player_analysis_management_browser_test.py
python botend/tests/js/talent-simulator-search.browser.py
```

这些本地 DOM/HTTP fixture 验证真实产品模板和脚本，不等同于正式站认证及生产数据验收。

三个 `simc_skill_damage_*_harness.cpp` 由 `test_simc_skill_damage_expectation.py` 和 `test_simc_skill_damage_pruning.py` 消费。运行时需要 `g++`，以及指向隔离补丁工作树中真实源文件的 `SIMC_SKILL_DAMAGE_SOURCE`；不要为了去掉条件跳过而删除 harness，或把未编译的静态检查称为原生测试通过。

## 组织与共享准备代码

- 按同一业务入口和 fixture 生命周期组织模块；成功、权限、来源拒绝等边界保留独立命名，不按修复日期继续拆小文件，也不把不相关的入口拼成大文件。
- 模板管理 API 集中在 `test_simc_advanced_settings_management.py`，history/artifact API 仍在 `test_simc_workbench_resource_api.py`；不要为模板接口再复制一套用户、Backend 和资源准备。
- 难度导入统一在 `test_item_difficulty_source_imports.py`，legacy 与 PTR2 保留各自来源准备和独立测试类。Legacy 的 DB2 事实只存于 `fixtures/wow_item_legacy_difficulty_69587.json`；导入测试组合 `facts` 与 `import.binding`，不复制整份来源证据。
- 装备展示的 Node 行为集中在 `js/gear-presentation.test.mjs`，首页加载、排行和今日重点集中在 `portal_home_test.js`。每个命名组使用新 VM context，不共享可变测试状态。
- `vm_harness.js` 为源码片段边界做缺失、重复、逆序检查；切片失败应直接报错，不能悄悄测试空代码。玩家分析 DOM 共用 `player_analysis_dom_harness.js`，每项测试都销毁控制器并关闭窗口，包括准备或断言失败路径。
- 共享 fixture 使用普通函数或不含 `test_*` 方法的 mixin。复用其他测试模块的辅助方法时导入模块，不把外部 `TestCase` 暴露为当前模块全局变量，避免单模块重复收集。
- 冒险手册 fixture 的临时目录由 `journal_snapshot_fixtures.py` 负责回收；恢复 settings 和删除目录必须在失败路径同样执行。

## 独立审计工具

`scripts/audit_*`、`scripts/validate_*` 和原生 probe 工具不是默认单元测试；部分会运行真实二进制或写报告，按其 CLI 输入单独执行，不要为“发现测试”而 import 有顶层副作用的脚本。

`audit_wow_item_difficulty.py` 默认只在指定 audit 目录写校验报告。只有显式传入 `--fixture-output <路径>` 且全部独立校验成功后才原子写 fixture，不再默认覆盖仓库夹具。`validate_simc_skill_coverage.py` 以修复前的完整 actor 集合为范围：整专精缺失和重复身份必须失败，不能只遍历修复后的幸存者。

## 清理原则

- 删除 fixture 前同时检查显式引用与动态加载入口；历史版本不同不等于内容重复。
- 业务契约已改变时，更新 fixture/行为断言，保留原来的权限、失败保护和边界目标；不能简单删除失败用例。
- 不锁死静态资源的历史发布日期。公式、条件标签、交互应各自验证对应行为，避免复制无关文案断言。
- 成功响应内容测试先断言状态码和 `success`，避免错误响应恰好缺少敏感字段导致“假通过”。
- 环境专属测试、显式浏览器脚本和框架 setup/teardown 钩子不是无人引用的死代码。
