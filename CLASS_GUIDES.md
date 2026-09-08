# 职业攻略部署与初始化

本模块包含 Dashboard 文章管理、整篇 Markdown 编辑、动态标签、统一专精、作者资料、标签免责声明、Portal 目录与阅读页，以及 Maxroll 增量同步。首次中文内容已打包，无需重新翻译。

## 首次部署

在项目根目录使用线上服务实际使用的 Python 环境执行以下命令。下文 `python` 可替换成 `.venv/bin/python` 或部署使用的 Python 路径。不要在线上设置本地测试用的 `LMONITOR_USE_SQLITE`。

```bash
git pull --ff-only origin master
python -m pip install -r requirements-class-guides.txt
python manage.py migrate --noinput
python manage.py import_class_guide_drafts --dry-run
python manage.py import_class_guide_drafts
python manage.py collectstatic --noinput
```

完成后按现有部署方式重启 Web 服务及 botend 后端，使现有后端加载新增监控插件。若使用项目 `deploy.sh`，应先安装上面的 Markdown 依赖；该脚本负责已有的迁移、静态文件收集和服务重启，中文草稿包仍需执行上述导入命令。

初始化命令默认直接读取仓库内置 ZIP，无需手动解压；可用 `--input-zip 路径` 导入其他压缩包，或用 `--input-dir 目录` 兼容已解压的包。包内根目录包含 `manifest.json`、`terms.json` 和逐篇中文草稿、原文快照。校验预期为 **70 篇草稿、4116 条术语**。第一次导入预期新增 70 篇；重复执行跳过相同内容。目标环境已有人工修改时，新内容保存为候选修订，保留人工稿、已审核版本、作者自定义资料和现有标签；新增 Maxroll 来源攻略自动补充 `maxroll` 标签。人工移除的标签不会在后续同步中被重新添加。

数据库迁移自动创建攻略表、专精约束、Portal“全职业攻略”导航和默认免责声明。免责声明采用已确认的站点文案，后续在 Dashboard 统一修改，不写入文章正文。显示取决于文章标签，移除 `maxroll` 标签即隐藏。更新日期采用对应正文修订抓取的原文日期。

## 验收入口

- 后台：`/dashboard/?section=class-guides`，支持列表、新建、编辑、标签、作者资料、术语管理、免责声明和来源监控设置。
- 前台：`/portal/class-guides/`，也可从 Portal“数据中心 → 全职业攻略”进入。
- 文章编号由目标数据库分配，不要依赖本地的 `/16/` 编号。请从目录进入正文。
- 当前仍为内部预览，后台及 Portal 攻略均要求 `content.class-guides` 权限。使用超级管理员，或在 Dashboard 用户组管理中分配“职业攻略”权限；匿名访问返回 403。

70 篇包括 36 篇团本与 34 篇大秘境攻略，全文已翻译，引用与组件结构差异为 0。67 篇通过自动内容检查，3 篇仍需校订来源引用：恶魔学的两篇攻略涉及 `spell:109997`，恢复萨满团本涉及 `spell:1296629`。来源目录缺少的组合未虚构补齐；自动检查不代替战术内容人工审核。

## 后续更新

在 Dashboard 的“来源与更新”填写已有授权依据，设置检查间隔并开启监控。`MaxrollClassGuideMonitor` 已注册到 botend 现有监控插件列表，使用统一的 `MonitorTask` 调度、执行租约、请求客户端和失败处理；无需增加独立服务或攻略常驻进程。首次安装默认关闭、间隔 6 小时；升级会保留旧监控开关和间隔。

攻略后台和通用监控后台控制同一个任务，任一处修改开关或间隔都会生效，后端重启也保留自定义间隔。现有 botend 后端需要正常运行。每次到期执行一轮检查后返回，只翻译新增、原文变化或之前翻译未完成的文章，保存为待审候选修订，保留人工稿和已审核版本。同步批次仍在攻略后台查看。

旧 `sync_class_guides --watch` 已停用；若此前创建过该命令的常驻服务，请停用它。首次中文包导入不需要翻译 API，后续更新翻译使用站内已有翻译引擎。日常监控不使用固定快照的 `--cache-dir`。

手动同步一次：

```bash
python manage.py sync_class_guides --translate --workers 2
```

只补抓作者资料、不重新翻译正文：

```bash
python manage.py sync_class_guide_authors --workers 4
```

若需要从远端重新建立全部草稿，可在后台登记授权后执行手动同步命令；无需先批量删除文章。断点重跑复用翻译缓存。
