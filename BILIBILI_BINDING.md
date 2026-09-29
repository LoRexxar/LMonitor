# B 站账号评论验证绑定

## 功能范围

已有账号可在 `/auth/bilibili/` 输入 B 站 UID，获取短时口令，在管理员指定动态下发表一级评论，然后检查并确认绑定。注册表单也支持同一流程，验证结果与账号创建在同一事务内保存。

超级管理员从后台侧栏「账号权限 → B 站绑定设置」切换到 `/dashboard/?section=bilibili-binding`，在原后台内容区配置动态链接、操作说明、口令有效期、检查间隔和扫描页数，并处理人工核验、查询绑定、撤销绑定。进入栏目时才加载数据，切换栏目保留未保存的设置；旧 `/dashboard/bilibili-binding/` 地址兼容跳转到该栏目，不再提供独立管理页。普通员工无管理权限。用户管理列表显示并支持搜索 B 站 UID。

本功能仅证明用户能控制对应 B 站账号，不判断舰长身份，也不维护舰长期限，不自动添加用户组或授予业务权限。

## 启用方式

1. 在目标环境按项目既有方式执行数据库迁移 `python manage.py migrate`，应用 `0229_bilibili_account_binding`。部署时使用该环境原有的设置模块，不使用测试库设置。
2. 按既有部署方式更新静态资源并重启应用。
3. 超级管理员填写真实福利动态完整链接，支持 `https://t.bilibili.com/数字` 和 `https://www.bilibili.com/opus/数字`。分享短链接需要先展开。
4. 点击「测试动态和评论读取」，随后开启绑定并保存。默认关闭，不会自动改变现有注册规则。
5. 如需在公开注册时强制验证，开启「公开注册必须先验证 B 站账号」。网站原有 `ALLOW_REGISTRATION` 仍具有优先权；关闭公开注册时，此设置不会重新开放注册。
6. 用真实 B 站账号完成一次发评论、检查、确认的验收。测试数据及人工模拟通过不等于真实账号归属验证。

设置变更会使尚未完成的申请失效，已绑定账号不受影响。公开注册仍然允许所有完成身份验证的 B 站用户，不能将此开关当作「仅舰长可注册」。

## 验证与隔离

- 验证口令使用随机数生成，同时关联网站会话、当前登录账号、目标 UID、指定动态和配置版本。公开口令本身不能用于领取账号。
- 后端核对 B 站返回的真实评论作者 UID、评论区编号和类型、一级评论位置、完整口令及发布时间。不接受他人的转发、子回复或截图作为自动验证证据。
- UID 按字符串处理，避免前端大整数精度丢失。数据库同时约束一个网站账号和一个 UID 只能存在一条关联。
- 验证成功与完成绑定分开。已有账号需确认；新用户需提交注册表单。凭证只能消耗一次；创建账号失败不会丢失尚有效的验证结果。
- API 使用会话归属检查、CSRF 防护、缓存禁用、申请和检查频率限制。绑定设置和人工审核只允许超级管理员。
- 撤销保留历史 UID 占用，避免被其他网站账号接管；原账号可重新验证同一 UID。暂不提供自助换绑。

## 外部接口与人工核验

自动读取使用 B 站公开网页接口，并非官方第三方登录授权。只向固定 `api.bilibili.com` 主机发出请求，禁止重定向，不接收用户 Cookie 或密码。

先查询动态详情以取得真实评论区编号和类型，再有限扫描最近一级评论。默认最多五页，每页二十条，每次申请最多检查二十次。响应大小、网络超时和页数均受限制。接口风控、格式变化、评论审核或评论超出扫描范围都可能导致验证无法完成。

用户可在口令过期前申请人工核验，申请保留二十四小时。管理员必须直接打开指定动态，核对作者 UID、一级评论位置和完整口令，填写真实评论编号和说明后通过。审核只将申请标为已验证，仍需原会话完成确认或注册。不要仅凭用户截图审核。

当前 IP 申请限制使用服务端 `REMOTE_ADDR`，不直接信任转发头。反向代理后多个用户可能共用该限额，需要结合部署入口的可信代理配置及流量控制评估。SQLite 测试不替代生产数据库的并发验证。

## 本地检查

```powershell
python manage.py check --settings=LMonitor.settings_test_sqlite
python manage.py makemigrations --check --dry-run --settings=LMonitor.settings_test_sqlite
python manage.py test botend.tests.test_bilibili_binding botend.tests.test_dashboard_user_management.DashboardUserManagementApiTests botend.tests.test_dashboard_user_management.DashboardUserManagementFrontendContractTests --settings=LMonitor.settings_test_sqlite --noinput
node --check static/dashboard/js/bilibili_binding.js
node --check static/dashboard/js/bilibili_binding_admin.js
```

自动化测试覆盖会话隔离、验证码复制不可领取、UID 冲突、注册原子性、配置变更、到期、重复提交、外部故障、人工审核权限和撤销。外部评论响应在测试中模拟；实号评论验收需要真实动态和账号配合。
