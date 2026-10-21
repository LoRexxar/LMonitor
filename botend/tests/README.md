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

## 清理原则

- 删除 fixture 前同时检查显式引用与动态加载入口；历史版本不同不等于内容重复。
- 业务契约已改变时，更新 fixture/行为断言，保留原来的权限、失败保护和边界目标；不能简单删除失败用例。
- 不锁死静态资源的历史发布日期。公式、条件标签、交互应各自验证对应行为，避免复制无关文案断言。
- 成功响应内容测试先断言状态码和 `success`，避免错误响应恰好缺少敏感字段导致“假通过”。
- 环境专属测试、显式浏览器脚本和框架 setup/teardown 钩子不是无人引用的死代码。
