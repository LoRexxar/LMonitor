function initDashboardTheme() {
    const storageKey = 'lmonitor-dashboard-theme';
    const root = document.documentElement;
    const toggle = document.getElementById('dashboard-theme-toggle');
    const icon = document.getElementById('dashboard-theme-icon');
    if (!toggle || !icon) return;

    const render = () => {
        const isDark = root.dataset.dashboardTheme === 'dark';
        toggle.setAttribute('aria-pressed', String(isDark));
        toggle.setAttribute('aria-label', isDark ? '切换浅色模式' : '切换深色模式');
        toggle.title = isDark ? '切换浅色模式' : '切换深色模式';
        icon.className = `fas ${isDark ? 'fa-sun' : 'fa-moon'} text-sm`;
    };
    render();
    toggle.addEventListener('click', () => {
        const isDark = root.dataset.dashboardTheme === 'dark';
        if (isDark) delete root.dataset.dashboardTheme;
        else root.dataset.dashboardTheme = 'dark';
        try { localStorage.setItem(storageKey, isDark ? 'light' : 'dark'); } catch (_) {}
        render();
    });
}

function applyDashboardPagePermissions() {
    const permissionCodes = new Set(JSON.parse(document.getElementById('dashboard-permissions-data')?.textContent || '[]'));
    const catalog = JSON.parse(document.getElementById('dashboard-permission-catalog-data')?.textContent || '[]');
    const bySection = new Map(catalog.map(item => [item.section, item.code]));
    document.querySelectorAll('[data-section], [data-dashboard-section]').forEach(item => {
        const section = item.getAttribute('data-section') || item.getAttribute('data-dashboard-section');
        const code = bySection.get(section);
        if (code && !permissionCodes.has(code)) item.remove();
    });
    document.querySelectorAll('.nav-item.has-submenu').forEach(item => {
        if (!item.querySelector('.submenu-item')) item.remove();
    });
    document.querySelectorAll('.content-section').forEach(section => {
        const code = bySection.get(section.id);
        if (code && !permissionCodes.has(code)) section.remove();
    });
}

/**
 * Dashboard页面的JavaScript功能
 * version: 20260715h
 */

document.addEventListener('DOMContentLoaded', function() {
    initDashboardTheme();
    applyDashboardPagePermissions();
    // 初始化页面数据
    initDashboard();

    // 设置定时刷新
    setInterval(refreshData, 30000); // 每30秒刷新一次数据

    // 初始化导航菜单点击事件
    initNavigation();
    initDashboardQuickEntries();
    initDashboardSectionLinks();

    // 初始化子菜单切换
    initSubmenuToggle();

    // 初始化数据库表点击事件
    initTableSelection();

    // 初始化转换器
    initSimcAplConverter();

    // 初始化新增记录功能
    initAddRecord();
    initEditRecord();

    // 初始化侧边栏切换功能
    initSidebarToggle();

    // 初始化搜索功能
    initSearch();
    initDatabaseTableFilter();
    initSimcProfileFilters();
    initWowArticleFilters();
    initWowDailyReportPage();
    initWagoHotfixReportPage();
    initNewsWowPage();
    initErrorLogPage();
    initLogFilePage();

    // 初始化页面大小选择器
    initPageSizeSelector();

    // 初始化用户菜单
    initUserMenu();
    initSystemAlerts();
    initSimcBackendUploadTool();
    initSimcSkillDamagePanel();
    initSimcWorkbench();

    // 显示服务端选择的第一个可访问页面
    const defaultSectionId = window.DASHBOARD_DEFAULT_SECTION || '';
    const defaultMenuItem = document.querySelector(`.nav-item[data-section="${defaultSectionId}"]`)
        || document.querySelector(`[data-dashboard-section="${defaultSectionId}"]`);
    const defaultSection = document.getElementById(defaultSectionId);

    if (defaultMenuItem && defaultSection) {
        const navItems = document.querySelectorAll('.nav-item');
        navItems.forEach(item => item.classList.remove('active'));
        defaultMenuItem.classList.add('active');
        document.querySelectorAll('.content-section').forEach(section => {
            section.style.display = 'none';
            section.classList.remove('active');
        });
        defaultSection.style.display = 'block';
        defaultSection.classList.add('active');
        if (defaultSectionId === 'user-groups' && window.loadDashboardUserGroups) {
            window.loadDashboardUserGroups();
        }
        if (defaultSectionId === 'gear-builder-management' && window.loadGearBuilderManagement) {
            window.loadGearBuilderManagement();
        }
    }

    // 默认展开数据库表菜单（但不激活）
    const databaseTablesMenu = document.querySelector('.nav-item.has-submenu[data-section="database-tables"]');
    if (databaseTablesMenu) {
        // 展开子菜单
        databaseTablesMenu.classList.add('open');
        const submenu = databaseTablesMenu.querySelector('.submenu');
        if (submenu) {
            submenu.style.maxHeight = submenu.scrollHeight + 'px';
            submenu.classList.remove('max-h-0');
        }
        const chevron = databaseTablesMenu.querySelector('.fa-chevron-down');
        if (chevron) {
            chevron.classList.add('rotate-180');
        }
    }

    // 默认展开Tools菜单
    const toolsMenu = document.querySelector('.nav-item.has-submenu[data-section="tools"]');
    if (toolsMenu) {
        // 展开子菜单
        toolsMenu.classList.add('open');
        const submenu = toolsMenu.querySelector('.submenu');
        if (submenu) {
            submenu.style.maxHeight = submenu.scrollHeight + 'px';
            submenu.classList.remove('max-h-0');
        }
        const chevron = toolsMenu.querySelector('.fa-chevron-down');
        if (chevron) {
            chevron.classList.add('rotate-180');
        }
    }

    activateDashboardLocation();
    window.addEventListener('popstate', activateDashboardLocation);
});

/**
 * 初始化仪表盘数据
 */

function initWagoSkillDiffRerunTool() {
    const submitBtn = document.getElementById('wago-rerun-submit');
    const fromInput = document.getElementById('wago-rerun-from-build');
    const toInput = document.getElementById('wago-rerun-to-build');
    if (!submitBtn || submitBtn.dataset.bound === '1') return;
    submitBtn.dataset.bound = '1';
    [fromInput, toInput].forEach(input => {
        if (!input) return;
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                submitWagoSkillDiffRerun();
            }
        });
    });
}

async function submitWagoSkillDiffRerun() {
    const branchEl = document.getElementById('wago-rerun-branch');
    const localeEl = document.getElementById('wago-rerun-locale');
    const fromEl = document.getElementById('wago-rerun-from-build');
    const toEl = document.getElementById('wago-rerun-to-build');
    const btn = document.getElementById('wago-rerun-submit');
    const msg = document.getElementById('wago-rerun-message');
    const resultEl = document.getElementById('wago-rerun-result');

    const payload = {
        branch: (branchEl && branchEl.value || 'wow').trim(),
        locale: (localeEl && localeEl.value || 'enUS').trim(),
        from_build: (fromEl && fromEl.value || '').trim(),
        to_build: (toEl && toEl.value || '').trim(),
    };
    if (!payload.from_build || !payload.to_build) {
        showMessage('请填写 from_build 和 to_build', 'warning');
        return;
    }
    if (payload.from_build === payload.to_build) {
        showMessage('from_build 和 to_build 不能相同', 'warning');
        return;
    }

    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        showMessage('无法获取CSRF令牌，请刷新页面', 'error');
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.classList.add('opacity-60', 'cursor-not-allowed');
        btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>生成中...';
    }
    if (msg) msg.textContent = '正在生成报告，可能需要几十秒到数分钟...';
    if (resultEl) {
        resultEl.classList.add('hidden');
        resultEl.innerHTML = '';
    }

    try {
        const resp = await fetch('/api/wago-skill-diff/rerun/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(payload),
        });
        const contentType = resp.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            const text = await resp.text();
            if (resp.redirected || text.includes('/auth/login/') || text.includes('<html')) {
                throw new Error('接口返回了HTML页面，请确认已登录Dashboard并刷新页面');
            }
            throw new Error(`接口返回非JSON内容：${text.slice(0, 120)}`);
        }
        const data = await resp.json();
        if (!data.success) {
            throw new Error(data.error || '生成失败');
        }
        const reportUrl = data.report_url || (data.report_id ? `/portal/wow-skill-diff/${data.report_id}/` : '');
        if (msg) msg.textContent = data.message || '报告已生成';
        if (resultEl) {
            resultEl.classList.remove('hidden');
            resultEl.innerHTML = `
                <div class="font-semibold text-gray-800 mb-2">生成成功</div>
                <div>分支：${escapeHtml(data.branch || payload.branch)} / Locale：${escapeHtml(data.locale || payload.locale)}</div>
                <div>版本：${escapeHtml(data.from_build || payload.from_build)} → ${escapeHtml(data.to_build || payload.to_build)}</div>
                <div>技能数：${data.spell_count || 0}，职业数：${data.class_count || 0}</div>
                ${reportUrl ? `<a class="inline-flex items-center mt-3 text-blue-600 hover:text-blue-800" href="${reportUrl}" target="_blank"><i class="fas fa-external-link-alt mr-1"></i>打开报告</a>` : ''}
            `;
        }
        showMessage(data.message || 'Wago指定版本报告已生成', 'success');
    } catch (err) {
        const text = String(err && err.message || err || '生成失败');
        if (msg) msg.textContent = `生成失败：${text}`;
        showMessage(`生成失败：${text}`, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.classList.remove('opacity-60', 'cursor-not-allowed');
            btn.innerHTML = '<i class="fas fa-rotate-right mr-2"></i>生成报告';
        }
    }
}

function escapeHtml(value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function initDashboard() {
    // 这里可以添加AJAX请求获取初始数据
    updateSystemStatus();
    updateRecentActivities();
    updateStatistics();
}

/**
 * 初始化侧栏中的可折叠菜单。
 *
 * 这是整个 Dashboard 的通用初始化函数，不能随 SimC 工作流代码一起删除；
 * 否则 DOMContentLoaded 会在绑定数据库表和其他页面入口前中断。
 */
function initSubmenuToggle() {
    document.querySelectorAll('.has-submenu').forEach(item => {
        const mainLink = item.querySelector('a');
        const submenu = item.querySelector('.submenu');
        const chevron = item.querySelector('.fa-chevron-down');
        if (!mainLink || !submenu || mainLink.dataset.submenuBound === '1') return;

        mainLink.dataset.submenuBound = '1';
        mainLink.addEventListener('click', event => {
            event.preventDefault();
            const willOpen = !item.classList.contains('open');
            item.classList.toggle('open', willOpen);
            mainLink.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
            submenu.style.maxHeight = willOpen ? `${submenu.scrollHeight}px` : '0';
            if (chevron) chevron.classList.toggle('rotate-180', willOpen);
        });
    });
}

/**
 * 初始化数据库表概览。
 */
function initTableSelection() {
    calculateTotalRecords();
}

function calculateTotalRecords() {
    const totalRecordsElement = document.getElementById('total-records');
    if (!totalRecordsElement) return;

    let totalRecords = 0;
    document.querySelectorAll('.table-overview-item').forEach(item => {
        const countText = item.querySelector('p:last-child')?.textContent || '';
        const count = Number.parseInt(countText.replace('记录数: ', ''), 10);
        if (Number.isFinite(count)) totalRecords += count;
    });
    totalRecordsElement.textContent = totalRecords.toLocaleString();
}

/**
 * 刷新仪表盘数据
 */
function refreshData() {
    // 更新系统状态
    updateSystemStatus();
    // 更新最近活动
    updateRecentActivities();
    // 更新统计数据
    updateStatistics();
    fetchUnreadSystemAlerts();
}

/**
 * 显示指定 Dashboard 内容区
 */
function showDashboardSection(sectionId) {
    const navItem = document.querySelector(
        `.submenu-item[data-dashboard-section="${sectionId}"], .nav-item[data-section="${sectionId}"]`,
    );
    if (navItem) {
        const link = navItem.querySelector('a');
        if (link) {
            link.click();
        } else {
            navItem.click();
        }
    }
}

function initDashboardSectionLinks() {
    document.querySelectorAll('[data-dashboard-target]').forEach(link => {
        link.addEventListener('click', event => {
            event.preventDefault();
            showDashboardSection(link.dataset.dashboardTarget);
        });
    });
}

function syncDashboardLocation({ section = '', tool = '', table = '' } = {}) {
    const url = new URL(window.location.href);
    url.searchParams.delete('section');
    url.searchParams.delete('tool');
    url.searchParams.delete('table');

    if (section && section !== 'dashboard-home') {
        url.searchParams.set('section', section);
    } else if (tool) {
        url.searchParams.set('tool', tool);
    } else if (table) {
        url.searchParams.set('table', table);
    }

    if (url.href !== window.location.href) {
        window.history.pushState({ dashboardLocation: { section, tool, table } }, '', url);
    }
}

function openDashboardTable(tableName, tableTitle = '') {
    currentTableDisplayName = String(tableTitle || '').trim();
    document.querySelectorAll('.content-section').forEach(section => {
        section.style.display = 'none';
        section.classList.remove('active');
    });
    const databaseTablesSection = document.getElementById('database-tables');
    if (!databaseTablesSection) return;

    databaseTablesSection.style.display = 'block';
    databaseTablesSection.classList.add('active');
    const selectedTableName = document.getElementById('selected-table-name');
    if (selectedTableName) {
        selectedTableName.textContent = currentTableDisplayName || tableName;
    }
    fetchTableData(tableName);
    syncDashboardLocation({ table: tableName });
}

/**
 * 从站内独立子页面返回 Dashboard 时，恢复用户点击的原侧栏目标。
 */
function activateDashboardLocation() {
    const params = new URLSearchParams(window.location.search);
    const section = params.get('section');
    const tool = params.get('tool');
    const table = params.get('table');
    let target = null;

    if (table) {
        target = Array.from(document.querySelectorAll('.submenu-item[data-table], .nav-item[data-dashboard-table]'))
            .find(item => (item.dataset.table || item.dataset.dashboardTable) === table);
    } else if (tool) {
        target = Array.from(document.querySelectorAll('.submenu-item[data-tool]'))
            .find(item => item.dataset.tool === tool);
    } else if (section) {
        const mythicResource = params.get('resource') || '';
        const sectionTargets = Array.from(
            document.querySelectorAll('[data-dashboard-section]'),
        ).filter(item => item.dataset.dashboardSection === section);
        target = (
            mythicResource
                ? sectionTargets.find(
                    item => item.dataset.mythicResource === mythicResource,
                )
                : sectionTargets.find(item => !item.dataset.mythicResource)
        ) || sectionTargets[0];
        if (!target) {
            target = Array.from(document.querySelectorAll('.nav-item[data-section]'))
                .find(item => item.dataset.section === section);
        }
    }
    if (!target) {
        const defaultSection = window.DASHBOARD_DEFAULT_SECTION || '';
        target = Array.from(document.querySelectorAll('[data-dashboard-section]'))
            .find(item => item.dataset.dashboardSection === defaultSection);
        if (!target) {
            target = Array.from(document.querySelectorAll('.nav-item[data-section]'))
                .find(item => item.dataset.section === defaultSection);
        }
    }
    if (!target) return;

    window.requestAnimationFrame(() => {
        const link = target.querySelector('a');
        if (link) link.click();
    });
}

/**
 * 初始化首页快捷入口
 */
function initDashboardQuickEntries() {
    const entries = [
        ['dashboard-hotfix-entry', 'wago-hotfix-reports'],
        ['dashboard-wow-daily-entry', 'wow-daily-reports'],
        ['dashboard-news-entry', 'news'],
    ];
    entries.forEach(([buttonId, sectionId]) => {
        const btn = document.getElementById(buttonId);
        if (btn) {
            btn.onclick = () => showDashboardSection(sectionId);
        }
    });
}

const SIMC_DASHBOARD_SECTIONS = Object.freeze({
    workflow: 'simc-workflow',
    history: 'simc-history',
    advanced: 'simc-advanced',
    'skill-damage': 'simc-skill-damage',
});

function isSimcDashboardSection(sectionId) {
    return Object.values(SIMC_DASHBOARD_SECTIONS).includes(sectionId);
}

function deactivateSimcWorkbench() {
    if (typeof window.simcWorkbenchDeactivatePanel === 'function') {
        window.simcWorkbenchDeactivatePanel('');
    }
    stopSimcAttributeSearch();
    stopSimcCandidateComparisonPolling();
}

/**
 * 初始化导航功能
 */
function initNavigation() {
    const navItems = document.querySelectorAll('.nav-item');
    const submenuItems = document.querySelectorAll('.submenu-item');
    const contentSections = document.querySelectorAll('.content-section');

    // 处理主导航项点击
    navItems.forEach(item => {
        item.addEventListener('click', function(e) {
            // 检查是否点击的是子菜单项
            if (e.target.closest('.submenu-item')) {
                // 如果点击的是子菜单项，阻止事件冒泡
                e.stopPropagation();
                return;
            }

            // 可折叠菜单由 initSubmenuToggle 统一处理，避免同一次点击重复切换。
            if (this.classList.contains('has-submenu')) {
                e.preventDefault();
                return;
            }

            e.preventDefault();

            const dashboardTable = this.getAttribute('data-dashboard-table');
            if (dashboardTable) {
                navItems.forEach(i => {
                    i.classList.remove('active');
                    const link = i.querySelector('a');
                    if (link) {
                        link.classList.remove('bg-blue-50', 'text-blue-600', 'font-medium');
                        link.classList.add('text-gray-700');
                    }
                });
                this.classList.add('active');
                const currentLink = this.querySelector('a');
                if (currentLink) {
                    currentLink.classList.add('bg-blue-50', 'text-blue-600', 'font-medium');
                    currentLink.classList.remove('text-gray-700');
                }
                openDashboardTable(dashboardTable, this.querySelector('a')?.textContent);
                return;
            }

            // 移除所有导航项的active类和样式
            navItems.forEach(i => {
                i.classList.remove('active');
                const link = i.querySelector('a');
                if (link) {
                    link.classList.remove('bg-blue-50', 'text-blue-600', 'font-medium');
                    link.classList.add('text-gray-700');
                }
            });

            // 为当前点击的导航项添加active类和样式
            this.classList.add('active');
            const currentLink = this.querySelector('a');
            if (currentLink) {
                currentLink.classList.add('bg-blue-50', 'text-blue-600', 'font-medium');
                currentLink.classList.remove('text-gray-700');
            }

            // 获取对应的内容区域ID
            const sectionId = this.getAttribute('data-section');
            if (!isSimcDashboardSection(sectionId)) deactivateSimcWorkbench();

            // 隐藏所有内容区域
            contentSections.forEach(section => {
                section.style.display = 'none';
                section.classList.remove('active');
            });

            // 显示对应的内容区域
            const targetSection = document.getElementById(sectionId);
            if (targetSection) {
                targetSection.style.display = 'block';
                targetSection.classList.add('active');
                if (sectionId === 'news') {
                    loadNewsWowArticles();
                }
                if (sectionId === 'wow-daily-reports') {
                    loadWowDailyReports();
                }
                if (sectionId === 'wago-hotfix-reports') {
                    loadWagoHotfixReports();
                }
                if (sectionId === 'log-files' && window.loadLogFilesGlobal) {
                    window.loadLogFilesGlobal();
                }
                if (sectionId === 'user-management' && window.loadDashboardUsers) {
                    window.loadDashboardUsers();
                }
                if (sectionId === 'user-groups' && window.loadDashboardUserGroups) {
                    window.loadDashboardUserGroups();
                }
                if (sectionId === 'gear-builder-management' && window.loadGearBuilderManagement) {
                    window.loadGearBuilderManagement();
                }
                if (isSimcDashboardSection(sectionId)) {
                    const simcPage = Object.keys(SIMC_DASHBOARD_SECTIONS)
                        .find(page => SIMC_DASHBOARD_SECTIONS[page] === sectionId);
                    switchSimcWorkbenchL1Tab(simcPage || 'workflow');
                }
                if (sectionId === SIMC_DASHBOARD_SECTIONS.workflow) {
                    switchSimcPlayerImportMode();
                }
                syncDashboardLocation({ section: sectionId });
            }
        });
    });

    // 处理子菜单项点击
    submenuItems.forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation(); // 阻止事件冒泡到父级菜单项
            const dashboardSection = this.getAttribute('data-dashboard-section');
            const mythicResource = this.getAttribute('data-mythic-resource') || '';
            if (!isSimcDashboardSection(dashboardSection)) deactivateSimcWorkbench();

            // 移除所有子菜单项的active类
            submenuItems.forEach(i => i.classList.remove('active'));

            // 为当前点击的子菜单项添加active类
            this.classList.add('active');

            // 确保父级菜单项也是active
            const parentNavItem = this.closest('.nav-item');
            navItems.forEach(i => i.classList.remove('active'));
            parentNavItem.classList.add('active', 'open');
            const parentLink = parentNavItem.querySelector(':scope > a');
            const parentSubmenu = parentNavItem.querySelector(':scope > .submenu');
            const parentChevron = parentLink?.querySelector('.fa-chevron-down');
            if (parentLink) parentLink.setAttribute('aria-expanded', 'true');
            if (parentSubmenu) parentSubmenu.style.maxHeight = `${parentSubmenu.scrollHeight}px`;
            if (parentChevron) parentChevron.classList.add('rotate-180');

            // 检查是否是工具菜单项
            const toolName = this.getAttribute('data-tool');
            const tableName = this.getAttribute('data-table');

            if (dashboardSection) {
                contentSections.forEach(section => {
                    section.style.display = 'none';
                    section.classList.remove('active');
                });
                const targetSection = document.getElementById(dashboardSection);
                if (targetSection) {
                    targetSection.style.display = 'block';
                    targetSection.classList.add('active');
                    if (isSimcDashboardSection(dashboardSection)) {
                        const simcPage = Object.keys(SIMC_DASHBOARD_SECTIONS)
                            .find(page => SIMC_DASHBOARD_SECTIONS[page] === dashboardSection);
                        switchSimcWorkbenchL1Tab(simcPage || 'workflow');
                    }
                    if (dashboardSection === SIMC_DASHBOARD_SECTIONS.workflow) {
                        switchSimcPlayerImportMode();
                    }
                    document.dispatchEvent(new CustomEvent('dashboard-section-changed', {
                        detail: {
                            section: dashboardSection,
                            mythicResource,
                        },
                    }));
                    syncDashboardLocation({ section: dashboardSection });
                }
            } else if (toolName) {
                // 处理工具菜单项
                const toolTitle = this.querySelector('a').textContent;

                // 显示工具内容区域
                contentSections.forEach(section => {
                    section.style.display = 'none';
                    section.classList.remove('active');
                });
                const toolsSection = document.getElementById('tools');
                if (toolsSection) {
                    toolsSection.style.display = 'block';
                    toolsSection.classList.add('active');

                    // 更新选中的工具名显示
                    const selectedToolName = document.getElementById('selected-tool-name');
                    if (selectedToolName) {
                        selectedToolName.textContent = toolTitle;
                    }

                    // 隐藏所有工具内容
                    const toolContents = document.querySelectorAll('.tool-content');
                    toolContents.forEach(content => {
                        content.style.display = 'none';
                    });

                    // 显示选中的工具内容
                    const selectedToolContent = document.getElementById(toolName);
                    if (selectedToolContent) {
                        selectedToolContent.style.display = 'block';
                        if (toolName === 'wcl-analysis-entry') {
                            initWclDashboardModule();
                            fetchWclDashboardTasks();
                        }
                        if (toolName === 'wago-skill-diff-rerun') {
                            initWagoSkillDiffRerunTool();
                        }
                    }
                    syncDashboardLocation({ tool: toolName });
                }
            } else if (tableName) {
                // 处理数据库表菜单项
                const tableTitle = this.querySelector('a').textContent;
                openDashboardTable(tableName, tableTitle);
            }
        });
    });
}

let newsWowState = {
    page: 1,
    pageSize: 20,
    search: '',
    source: '',
    category: '',
    totalPages: 1,
    totalCount: 0,
};
let newsWowSearchTimer = null;

function initNewsWowPage() {
    const searchInput = document.getElementById('news-wow-search');
    const sourceInput = document.getElementById('news-wow-source-filter');
    const categoryInput = document.getElementById('news-wow-category-filter');
    const pageSizeInput = document.getElementById('news-wow-page-size');
    const resetBtn = document.getElementById('news-wow-reset');
    const refreshBtn = document.getElementById('news-wow-refresh');

    if (searchInput && !searchInput.dataset.bound) {
        searchInput.dataset.bound = '1';
        searchInput.addEventListener('input', function(e) {
            if (newsWowSearchTimer) clearTimeout(newsWowSearchTimer);
            newsWowSearchTimer = setTimeout(() => {
                newsWowState.search = e.target.value.trim();
                loadNewsWowArticles(1);
            }, 350);
        });
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                if (newsWowSearchTimer) clearTimeout(newsWowSearchTimer);
                newsWowState.search = e.target.value.trim();
                loadNewsWowArticles(1);
            }
        });
    }

    if (sourceInput && !sourceInput.dataset.bound) {
        sourceInput.dataset.bound = '1';
        sourceInput.addEventListener('change', function(e) {
            newsWowState.source = e.target.value;
            loadNewsWowArticles(1);
        });
    }
    if (categoryInput && !categoryInput.dataset.bound) {
        categoryInput.dataset.bound = '1';
        categoryInput.addEventListener('change', function(e) {
            newsWowState.category = e.target.value;
            loadNewsWowArticles(1);
        });
    }
    if (pageSizeInput && !pageSizeInput.dataset.bound) {
        pageSizeInput.dataset.bound = '1';
        pageSizeInput.addEventListener('change', function(e) {
            newsWowState.pageSize = parseInt(e.target.value, 10) || 20;
            loadNewsWowArticles(1);
        });
    }
    if (resetBtn && !resetBtn.dataset.bound) {
        resetBtn.dataset.bound = '1';
        resetBtn.addEventListener('click', function() {
            newsWowState.search = '';
            newsWowState.source = '';
            newsWowState.category = '';
            if (searchInput) searchInput.value = '';
            if (sourceInput) sourceInput.value = '';
            if (categoryInput) categoryInput.value = '';
            loadNewsWowArticles(1);
        });
    }
    if (refreshBtn && !refreshBtn.dataset.bound) {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', function() {
            loadNewsWowArticles(newsWowState.page || 1);
        });
    }
}

function loadNewsWowArticles(page = 1) {
    initNewsWowPage();
    const container = document.getElementById('news-wow-list');
    const pager = document.getElementById('news-wow-pagination');
    const summary = document.getElementById('news-wow-summary');
    if (!container) return;
    newsWowState.page = page;
    container.innerHTML = '<div class="p-6 animate-pulse space-y-4"><div class="h-5 bg-gray-200 rounded w-2/3"></div><div class="h-4 bg-gray-200 rounded w-4/5"></div><div class="h-4 bg-gray-200 rounded w-3/5"></div></div>';
    if (pager) pager.innerHTML = '';
    if (summary) summary.textContent = '正在加载新闻...';

    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        container.innerHTML = '<div class="p-8 text-red-500">错误: 无法获取CSRF令牌</div>';
        return;
    }
    const requestData = {
        action: 'get_table_data',
        table_name: 'WowArticle',
        page: page,
        page_size: newsWowState.pageSize || 20,
    };
    if (newsWowState.search) requestData.search = newsWowState.search;
    if (newsWowState.source) requestData.wow_source = newsWowState.source;
    if (newsWowState.category) requestData.wow_category = newsWowState.category;

    fetch('/dashboard/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify(requestData)
    })
    .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    })
    .then(data => {
        if (data.status !== 'success') {
            container.innerHTML = `<div class="p-8 text-red-500">获取数据失败: ${escapeHtml(data.message || '未知错误')}</div>`;
            if (summary) summary.textContent = '';
            return;
        }
        updateNewsWowFilterOptions(data.wow_filter_options || {});
        const items = data.data || [];
        newsWowState.page = data.page || page;
        newsWowState.totalPages = data.total_pages || 1;
        newsWowState.totalCount = data.total_count || items.length;
        displayNewsWowArticles(items);
        displayNewsWowPagination(newsWowState.page, newsWowState.totalPages, newsWowState.totalCount);
        updateNewsWowSummary();
    })
    .catch(err => {
        container.innerHTML = `<div class="p-8 text-red-500">请求错误: ${escapeHtml(err.message)}</div>`;
        if (summary) summary.textContent = '';
    });
}

function updateNewsWowFilterOptions(options) {
    const sourceInput = document.getElementById('news-wow-source-filter');
    const categoryInput = document.getElementById('news-wow-category-filter');
    if (!sourceInput || !categoryInput) return;
    const fillSelect = (select, placeholder, values, current) => {
        const normalized = (Array.isArray(values) ? values : [])
            .map(v => (v || '').toString().trim())
            .filter(v => v);
        select.innerHTML = '';
        const allOption = document.createElement('option');
        allOption.value = '';
        allOption.textContent = placeholder;
        select.appendChild(allOption);
        normalized.forEach(v => {
            const opt = document.createElement('option');
            opt.value = v;
            opt.textContent = v;
            select.appendChild(opt);
        });
        select.value = normalized.includes(current) ? current : '';
    };
    fillSelect(sourceInput, '全部来源', options.sources, newsWowState.source);
    fillSelect(categoryInput, '全部分类', options.categories, newsWowState.category);
}

function updateNewsWowSummary() {
    const summary = document.getElementById('news-wow-summary');
    if (!summary) return;
    const filters = [];
    if (newsWowState.search) filters.push(`搜索“${newsWowState.search}”`);
    if (newsWowState.source) filters.push(`来源：${newsWowState.source}`);
    if (newsWowState.category) filters.push(`分类：${newsWowState.category}`);
    const start = newsWowState.totalCount ? (newsWowState.page - 1) * newsWowState.pageSize + 1 : 0;
    const end = Math.min(newsWowState.page * newsWowState.pageSize, newsWowState.totalCount);
    summary.textContent = `${filters.length ? filters.join(' / ') + '，' : ''}显示 ${start}-${end} 条，共 ${newsWowState.totalCount} 条`;
}

function getNewsWowSourceBadgeClass(source) {
    const s = String(source || '').toLowerCase();
    if (s.includes('wowhead')) return 'bg-indigo-50 text-indigo-700 border-indigo-100';
    if (s.includes('blizzard')) return 'bg-sky-50 text-sky-700 border-sky-100';
    if (s.includes('nga')) return 'bg-orange-50 text-orange-700 border-orange-100';
    return 'bg-slate-50 text-slate-700 border-slate-100';
}

function renderNewsWowListItem(item) {
    const titleCn = item.title_cn || '';
    const title = item.title || titleCn || '未命名文章';
    const displayTitle = titleCn || title;
    const source = item.source || 'unknown';
    const category = item.category || '';
    const author = item.author || '';
    const description = item.description || '';
    const replies = Number(item.reply_count || 0);
    const time = item.publish_time ? formatDateTime(item.publish_time) : '';
    const url = item.url || '';
    const sourceBadge = getNewsWowSourceBadgeClass(source);
    const originalTitle = titleCn && title && titleCn !== title
        ? `<div class="mt-1 text-sm text-slate-500 line-clamp-1">${escapeHtml(title)}</div>`
        : '';
    const metaParts = [];
    if (source) metaParts.push(`<span class="inline-flex items-center px-2 py-0.5 rounded-full border ${sourceBadge}">${escapeHtml(source)}</span>`);
    if (category) metaParts.push(`<span class="inline-flex items-center px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">${escapeHtml(category)}</span>`);
    if (author) metaParts.push(`<span><i class="fas fa-user mr-1 text-slate-400"></i>${escapeHtml(author)}</span>`);
    if (time) metaParts.push(`<span><i class="fas fa-clock mr-1 text-slate-400"></i>${escapeHtml(time)}</span>`);
    if (replies > 0) metaParts.push(`<span><i class="fas fa-comments mr-1 text-slate-400"></i>${replies} 回复</span>`);
    return `
        <article class="news-wow-row bg-white border border-slate-200 rounded-xl p-4 hover:border-blue-200 hover:shadow-md transition-all duration-150" data-id="${escapeHtml(item.id)}">
            <div class="flex flex-col lg:flex-row lg:items-start gap-3">
                <div class="min-w-0 flex-1">
                    <button type="button" class="news-wow-open text-left text-lg font-semibold text-slate-900 hover:text-blue-700 leading-snug" data-id="${escapeHtml(item.id)}">
                        ${escapeHtml(displayTitle)}
                    </button>
                    ${originalTitle}
                    ${description ? `<p class="mt-2 text-sm text-slate-600 line-clamp-2">${escapeHtml(description)}</p>` : ''}
                    <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">${metaParts.join('')}</div>
                </div>
                <div class="flex lg:flex-col gap-2 shrink-0 lg:w-28">
                    <button type="button" class="news-wow-open inline-flex items-center justify-center px-3 py-2 rounded-lg bg-blue-600 text-white text-sm hover:bg-blue-700" data-id="${escapeHtml(item.id)}">
                        <i class="fas fa-book-open mr-1"></i>详情
                    </button>
                    ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="inline-flex items-center justify-center px-3 py-2 rounded-lg bg-slate-100 text-slate-700 text-sm hover:bg-slate-200"><i class="fas fa-arrow-up-right-from-square mr-1"></i>原文</a>` : ''}
                </div>
            </div>
        </article>`;
}

function bindNewsWowOpenButtons() {
    document.querySelectorAll('.news-wow-open').forEach(btn => {
        btn.onclick = () => openNewsWowDetail(btn.dataset.id);
    });
}

function displayNewsWowArticles(items) {
    const container = document.getElementById('news-wow-list');
    if (!container) return;
    if (!items.length) {
        container.innerHTML = `
            <div class="p-12 text-center text-gray-500 bg-white rounded-2xl border border-slate-200">
                <i class="fas fa-newspaper text-4xl text-gray-300 mb-3"></i>
                <p class="text-lg font-medium">没有匹配的文章</p>
                <p class="text-sm text-gray-400 mt-1">换个关键词或清空筛选再试。</p>
            </div>`;
        return;
    }
    container.innerHTML = `<div class="space-y-3">${items.map(renderNewsWowListItem).join('')}</div>`;
    bindNewsWowOpenButtons();
}

function parseNewsWowBlocks(raw) {
    if (!raw) return '';
    try {
        const blocks = JSON.parse(raw);
        if (Array.isArray(blocks)) {
            return blocks.map(block => {
                if (!block || typeof block !== 'object') return '';
                if (block.type === 'html' && block.html) return String(block.html);
                if (block.type === 'image' && block.url) return `<p><img src="${escapeHtml(block.url)}" alt=""></p>`;
                if (block.text) return `<p>${escapeHtml(block.text)}</p>`;
                return '';
            }).join('\n');
        }
    } catch (e) {
        return '';
    }
    return '';
}

function getNewsWowArticleHtml(article) {
    const blocksHtml = parseNewsWowBlocks(article.content_blocks_cn || article.content_blocks || '');
    if (blocksHtml) return blocksHtml;
    const content = article.content_cn || article.content || article.description || '';
    return content ? `<p>${escapeHtml(content).replace(/\n{2,}/g, '</p><p>').replace(/\n/g, '<br>')}</p>` : '<p class="text-slate-400">暂无正文内容。</p>';
}

function openNewsWowDetail(articleId) {
    const modal = document.getElementById('news-wow-detail-modal');
    const body = document.getElementById('news-wow-detail-body');
    if (!modal || !body || !articleId) return;
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.body.style.overflow = 'hidden';
    body.innerHTML = '<div class="p-8 text-sm text-slate-500 animate-pulse">正在加载文章详情...</div>';

    const csrfToken = getCSRFToken();
    fetch('/dashboard/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
        body: JSON.stringify({ action: 'get_wow_article_detail', id: articleId })
    })
    .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
    })
    .then(data => {
        if (data.status !== 'success') throw new Error(data.message || '加载失败');
        const article = data.data || {};
        const titleCn = article.title_cn || '';
        const title = article.title || titleCn || '未命名文章';
        const displayTitle = titleCn || title;
        const url = article.url || '';
        const meta = [];
        if (article.source) meta.push(`<span class="inline-flex items-center px-2 py-1 rounded-full border ${getNewsWowSourceBadgeClass(article.source)}">${escapeHtml(article.source)}</span>`);
        if (article.category) meta.push(`<span class="inline-flex items-center px-2 py-1 rounded-full bg-slate-100 text-slate-600">${escapeHtml(article.category)}</span>`);
        if (article.author) meta.push(`<span><i class="fas fa-user mr-1 text-slate-400"></i>${escapeHtml(article.author)}</span>`);
        if (article.publish_time) meta.push(`<span><i class="fas fa-clock mr-1 text-slate-400"></i>${escapeHtml(formatDateTime(article.publish_time))}</span>`);
        if (Number(article.reply_count || 0) > 0) meta.push(`<span><i class="fas fa-comments mr-1 text-slate-400"></i>${Number(article.reply_count || 0)} 回复</span>`);
        body.innerHTML = `
            <div class="sticky top-0 z-10 bg-white/95 backdrop-blur border-b border-slate-100 px-6 py-5">
                <div class="flex items-start justify-between gap-4">
                    <div class="min-w-0">
                        <h3 class="text-2xl font-bold text-slate-900 leading-tight">${escapeHtml(displayTitle)}</h3>
                        ${titleCn && title && titleCn !== title ? `<p class="mt-2 text-sm text-slate-500">${escapeHtml(title)}</p>` : ''}
                        <div class="mt-3 flex flex-wrap items-center gap-2 text-sm text-slate-500">${meta.join('')}</div>
                    </div>
                    <button type="button" onclick="closeNewsWowDetail()" class="shrink-0 w-9 h-9 rounded-full bg-slate-100 hover:bg-slate-200 text-slate-600"><i class="fas fa-times"></i></button>
                </div>
                ${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="inline-flex items-center mt-4 text-sm font-medium text-blue-700 hover:text-blue-800"><i class="fas fa-arrow-up-right-from-square mr-1"></i>打开原文</a>` : ''}
            </div>
            <div class="news-wow-article-content px-6 py-5 text-slate-800 leading-7">${getNewsWowArticleHtml(article)}</div>`;
    })
    .catch(err => {
        body.innerHTML = `<div class="p-8 text-red-500">加载详情失败：${escapeHtml(err.message)}</div>`;
    });
}

function closeNewsWowDetail() {
    const modal = document.getElementById('news-wow-detail-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    document.body.style.overflow = '';
}

function displayNewsWowPagination(currentPage, totalPages, totalCount) {
    const pager = document.getElementById('news-wow-pagination');
    if (!pager) return;
    const prevDisabled = currentPage <= 1;
    const nextDisabled = currentPage >= totalPages;
    const pageButtons = [];
    const start = Math.max(1, currentPage - 2);
    const end = Math.min(totalPages, currentPage + 2);
    for (let i = start; i <= end; i++) {
        pageButtons.push(`<button class="news-wow-page-btn px-3 py-2 rounded-lg text-sm ${i === currentPage ? 'bg-blue-600 text-white' : 'bg-white border border-gray-200 text-gray-700 hover:bg-gray-100'}" data-page="${i}">${i}</button>`);
    }
    pager.innerHTML = `
        <div class="text-sm text-gray-600">共 ${totalCount} 条，页 ${currentPage}/${totalPages || 1}</div>
        <div class="flex items-center gap-2">
            <button id="news-wow-prev" class="px-3 py-2 bg-white border border-gray-200 text-gray-700 rounded-lg hover:bg-gray-100 text-sm ${prevDisabled ? 'opacity-50 cursor-not-allowed' : ''}" ${prevDisabled ? 'disabled' : ''}>上一页</button>
            ${pageButtons.join('')}
            <button id="news-wow-next" class="px-3 py-2 bg-white border border-gray-200 text-gray-700 rounded-lg hover:bg-gray-100 text-sm ${nextDisabled ? 'opacity-50 cursor-not-allowed' : ''}" ${nextDisabled ? 'disabled' : ''}>下一页</button>
        </div>
    `;
    const prevBtn = document.getElementById('news-wow-prev');
    const nextBtn = document.getElementById('news-wow-next');
    if (prevBtn) prevBtn.onclick = () => { if (!prevDisabled) loadNewsWowArticles(currentPage - 1); };
    if (nextBtn) nextBtn.onclick = () => { if (!nextDisabled) loadNewsWowArticles(currentPage + 1); };
    document.querySelectorAll('.news-wow-page-btn').forEach(btn => {
        btn.onclick = () => loadNewsWowArticles(parseInt(btn.dataset.page, 10) || 1);
    });
}

function initWowDailyReportPage() {
    const refreshBtn = document.getElementById('wow-daily-report-refresh');
    if (refreshBtn) {
        refreshBtn.onclick = () => loadWowDailyReports();
    }
    const genBtn = document.getElementById('wow-daily-report-generate');
    if (genBtn) {
        genBtn.onclick = () => generateWowDailyReport();
    }
}

async function generateWowDailyReport() {
    const genBtn = document.getElementById('wow-daily-report-generate');
    const hintEl = document.getElementById('wow-daily-report-hint');
    if (genBtn) genBtn.disabled = true;
    if (hintEl) hintEl.textContent = '正在生成...';
    try {
        const resp = await fetch('/api/wow-daily-report/generate/', { method: 'POST' });
        const data = await resp.json();
        if (!data || !data.success) {
            throw new Error((data && data.error) || '生成失败');
        }
        showMessage('已生成并更新今天的日报', 'success');
        await loadWowDailyReports();
    } catch (e) {
        showMessage(`生成失败：${String(e.message || e)}`, 'warning');
        if (hintEl) hintEl.textContent = `生成失败：${String(e.message || e)}`;
    } finally {
        if (genBtn) genBtn.disabled = false;
    }
}

async function loadWowDailyReports() {
    const listEl = document.getElementById('wow-daily-report-list');
    const hintEl = document.getElementById('wow-daily-report-hint');
    const countEl = document.getElementById('wow-daily-report-count');
    if (!listEl) return;
    listEl.innerHTML = '<div class="p-4 text-sm text-gray-500">加载中...</div>';
    if (hintEl) hintEl.textContent = '';
    try {
        const resp = await fetch('/api/wow-daily-report/list/?limit=60', { method: 'GET' });
        const data = await resp.json();
        if (!data || !data.success) {
            throw new Error((data && data.error) || '加载失败');
        }
        const items = data.data || [];
        if (countEl) countEl.textContent = `共 ${items.length} 条`;
        if (!items.length) {
            listEl.innerHTML = '<div class="p-4 text-sm text-gray-500">暂无日报记录</div>';
            return;
        }

        listEl.innerHTML = '';
        items.forEach((it) => {
            const date = it.report_date || '';
            const updated = it.updated_at || '';
            const portalUrl = it.portal_url || '';
            const row = document.createElement('div');
            row.className = 'flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between hover:bg-slate-50 transition-colors duration-200';

            const meta = document.createElement('div');
            meta.className = 'min-w-0';
            const title = document.createElement('div');
            title.className = 'font-semibold text-gray-900';
            title.textContent = date ? `${date} 魔兽世界日报` : '魔兽世界日报';
            const timestamp = document.createElement('div');
            timestamp.className = 'mt-1 text-xs text-gray-500';
            timestamp.textContent = updated ? `更新时间：${updated}` : '暂无更新时间';
            meta.append(title, timestamp);

            const action = document.createElement('a');
            action.className = 'inline-flex shrink-0 items-center justify-center rounded-lg bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-700 transition-colors duration-200';
            action.textContent = '打开日报';
            if (portalUrl) {
                action.href = portalUrl;
                action.target = '_blank';
                action.rel = 'noopener noreferrer';
            } else {
                action.classList.add('pointer-events-none', 'opacity-50');
                action.setAttribute('aria-disabled', 'true');
                action.title = '日报页面尚不可用';
            }
            row.append(meta, action);
            listEl.appendChild(row);
        });
        if (hintEl) hintEl.textContent = '每条日报会在新标签页打开 Portal 使用的同一份页面。';
    } catch (e) {
        listEl.innerHTML = `<div class="p-4 text-sm text-red-600">加载失败：${String(e.message || e)}</div>`;
    }
}


function initWagoHotfixReportPage() {
    const refreshBtn = document.getElementById('wago-hotfix-refresh');
    if (refreshBtn) {
        refreshBtn.onclick = () => loadWagoHotfixReports();
    }
}

async function loadWagoHotfixReports() {
    const hintEl = document.getElementById('wago-hotfix-hint');
    const statesEl = document.getElementById('wago-hotfix-states');
    const reportsEl = document.getElementById('wago-hotfix-report-list');
    const eventsEl = document.getElementById('wago-hotfix-event-list');
    const reportCountEl = document.getElementById('wago-hotfix-report-count');
    const eventCountEl = document.getElementById('wago-hotfix-event-count');
    if (!reportsEl || !eventsEl) return;
    if (hintEl) hintEl.textContent = '加载 Hotfix 报告中...';
    if (statesEl) statesEl.innerHTML = '';
    reportsEl.innerHTML = '<div class="p-5 text-sm text-gray-500">加载中...</div>';
    eventsEl.innerHTML = '<div class="p-5 text-sm text-gray-500">加载中...</div>';
    try {
        const resp = await fetch('/api/wago-hotfix-reports/?limit=30', { method: 'GET' });
        const data = await resp.json();
        if (!data || !data.success) {
            throw new Error((data && data.error) || '加载失败');
        }
        const states = data.states || [];
        const reports = data.reports || [];
        const events = data.events || [];
        if (reportCountEl) reportCountEl.textContent = `共 ${reports.length} 条`;
        if (eventCountEl) eventCountEl.textContent = `共 ${events.length} 条`;
        renderWagoHotfixStates(states);
        renderWagoHotfixReports(reports);
        renderWagoHotfixEvents(events);
        if (hintEl) hintEl.textContent = states.length ? 'Hotfix 游标只在完整报告成功后推进；fallback 报告会保留重试机会。' : '暂无 Hotfix 监控状态';
    } catch (e) {
        const msg = `加载失败：${String(e.message || e)}`;
        if (hintEl) hintEl.textContent = msg;
        reportsEl.innerHTML = `<div class="p-5 text-sm text-red-600">${escapeHtml(msg)}</div>`;
        eventsEl.innerHTML = `<div class="p-5 text-sm text-red-600">${escapeHtml(msg)}</div>`;
    }
}

function renderWagoHotfixStates(states) {
    const el = document.getElementById('wago-hotfix-states');
    if (!el) return;
    if (!states || !states.length) {
        el.innerHTML = '<div class="bg-white rounded-xl shadow p-5 text-sm text-gray-500">暂无 Hotfix 监控状态</div>';
        return;
    }
    el.innerHTML = states.map(st => {
        const reportUrl = st.hotfix_report_url || '';
        const wagoUrl = st.hotfix_wago_url || '';
        const cursorWarning = st.cursor_is_ahead_of_known ? `<div class="mt-3 rounded-lg bg-red-50 border border-red-100 px-3 py-2 text-xs text-red-700">游标高于最近已知 push（${escapeHtml(st.latest_known_push || 0)}），监控下次扫描会自动重置并重新检测。</div>` : '';
        return `
            <div class="bg-white rounded-xl shadow-lg border border-gray-100 p-5 border-l-4 border-orange-500">
                <div class="flex items-start justify-between gap-3 mb-3">
                    <div>
                        <div class="text-xs uppercase tracking-wide text-gray-500">${escapeHtml(st.branch || 'wow')} / ${escapeHtml(st.locale || '-')}</div>
                        <div class="text-xl font-bold text-gray-900 mt-1">Push ${escapeHtml(st.hotfix_push_id || 0)}</div>
                    </div>
                    <span class="px-2.5 py-1 rounded-full text-xs font-semibold bg-orange-50 text-orange-700">${escapeHtml(st.hotfix_last_event_status || st.hotfix_last_run_status || 'unknown')}</span>
                </div>
                <div class="text-sm text-gray-600 space-y-1">
                    <div>Build：<span class="font-medium text-gray-800">${escapeHtml(st.build || '-')}</span></div>
                    <div>最近运行：${escapeHtml(st.hotfix_last_run_at || '-')}</div>
                    <div>最近事件：${escapeHtml(st.hotfix_last_event_at || '-')}</div>
                    <div class="line-clamp-2">${escapeHtml(st.hotfix_summary_title || '暂无摘要')}</div>
                </div>
                ${cursorWarning}
                <div class="mt-4 flex flex-wrap gap-2">
                    ${reportUrl ? `<a class="px-3 py-1.5 rounded-lg bg-orange-600 text-white text-sm hover:bg-orange-700" target="_blank" href="${escapeHtml(reportUrl)}"><i class="fas fa-external-link-alt mr-1"></i>打开报告</a>` : ''}
                    ${wagoUrl ? `<a class="px-3 py-1.5 rounded-lg bg-gray-100 text-gray-700 text-sm hover:bg-gray-200" target="_blank" href="${escapeHtml(wagoUrl)}">Wago 原始页</a>` : ''}
                </div>
            </div>
        `;
    }).join('');
}

function renderWagoHotfixReports(reports) {
    const el = document.getElementById('wago-hotfix-report-list');
    if (!el) return;
    if (!reports || !reports.length) {
        el.innerHTML = '<div class="p-5 text-sm text-gray-500">暂无 Hotfix 报告</div>';
        return;
    }
    el.innerHTML = reports.map(r => `
        <div class="p-5 hover:bg-orange-50/40 transition-colors duration-200">
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                    <div class="font-semibold text-gray-900 break-words">${escapeHtml(r.summary_title || `Hotfix ${r.from_push} → ${r.to_push}`)}</div>
                    <div class="mt-1 text-xs text-gray-500">${escapeHtml(r.locale || '-')} · build ${escapeHtml(r.build_num || r.build_str || '-')} · push ${escapeHtml(r.from_push)} → ${escapeHtml(r.to_push)}</div>
                    <div class="mt-1 text-xs text-gray-500">${escapeHtml(r.created_at || '')} · ${escapeHtml(r.table_count || 0)} 表 / ${escapeHtml(r.entry_count || 0)} 项</div>
                </div>
                <div class="flex flex-col gap-2 shrink-0">
                    ${r.report_url ? `<a class="text-sm text-orange-600 hover:text-orange-800" target="_blank" href="${escapeHtml(r.report_url)}">报告</a>` : ''}
                    ${r.wago_url ? `<a class="text-sm text-blue-600 hover:text-blue-800" target="_blank" href="${escapeHtml(r.wago_url)}">Wago</a>` : ''}
                </div>
            </div>
        </div>
    `).join('');
}

function renderWagoHotfixEvents(events) {
    const el = document.getElementById('wago-hotfix-event-list');
    if (!el) return;
    if (!events || !events.length) {
        el.innerHTML = '<div class="p-5 text-sm text-gray-500">暂无 Hotfix 事件</div>';
        return;
    }
    el.innerHTML = events.map(ev => {
        const status = ev.status || 'unknown';
        const warn = status.includes('failed') || status.includes('fallback');
        return `
            <div class="p-5 hover:bg-gray-50 transition-colors duration-200">
                <div class="flex items-start justify-between gap-3">
                    <div class="min-w-0">
                        <div class="flex items-center gap-2 flex-wrap">
                            <span class="font-semibold text-gray-900">Push ${escapeHtml(ev.from_push)} → ${escapeHtml(ev.to_push)}</span>
                            <span class="px-2 py-0.5 rounded-full text-xs ${warn ? 'bg-yellow-50 text-yellow-700' : 'bg-emerald-50 text-emerald-700'}">${escapeHtml(status)}</span>
                        </div>
                        <div class="mt-1 text-xs text-gray-500">${escapeHtml(ev.locale || '-')} · build ${escapeHtml(ev.build_num || ev.build_str || '-')} · ${escapeHtml(ev.detected_at || '')}</div>
                        <div class="mt-1 text-xs text-gray-500">${escapeHtml(ev.summary_title || '')}</div>
                        ${ev.error_message ? `<div class="mt-2 text-xs text-red-600 break-words">${escapeHtml(ev.error_message)}</div>` : ''}
                    </div>
                    <div class="flex flex-col gap-2 shrink-0">
                        ${ev.report_url ? `<a class="text-sm text-orange-600 hover:text-orange-800" target="_blank" href="${escapeHtml(ev.report_url)}">报告</a>` : ''}
                        ${ev.wago_url ? `<a class="text-sm text-blue-600 hover:text-blue-800" target="_blank" href="${escapeHtml(ev.wago_url)}">Wago</a>` : ''}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function renderSimpleMarkdown(md) {
    const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
    const out = [];
    let inCode = false;
    let listOpen = false;
    const closeList = () => {
        if (listOpen) {
            out.push('</ul>');
            listOpen = false;
        }
    };
    const inline = (s) => {
        let x = escapeHtml(s);
        x = x.replace(/`([^`]+)`/g, '<code>$1</code>');
        x = x.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
        return x;
    };
    for (const raw of lines) {
        const line = raw || '';
        if (line.trim().startsWith('```')) {
            if (!inCode) {
                closeList();
                out.push('<pre><code>');
                inCode = true;
            } else {
                out.push('</code></pre>');
                inCode = false;
            }
            continue;
        }
        if (inCode) {
            out.push(escapeHtml(line) + '\n');
            continue;
        }
        const t = line.trim();
        if (!t) {
            closeList();
            continue;
        }
        if (t.startsWith('### ')) {
            closeList();
            out.push(`<h3>${inline(t.slice(4))}</h3>`);
            continue;
        }
        if (t.startsWith('## ')) {
            closeList();
            out.push(`<h2>${inline(t.slice(3))}</h2>`);
            continue;
        }
        if (t.startsWith('# ')) {
            closeList();
            out.push(`<h1>${inline(t.slice(2))}</h1>`);
            continue;
        }
        if (t.startsWith('- ')) {
            if (!listOpen) {
                out.push('<ul>');
                listOpen = true;
            }
            out.push(`<li>${inline(t.slice(2))}</li>`);
            continue;
        }
        closeList();
        out.push(`<p>${inline(t)}</p>`);
    }
    if (inCode) {
        out.push('</code></pre>');
        inCode = false;
    }
    closeList();
    return out.join('');
}

async function downloadFileByFetch(url, date) {
    try {
        const resp = await fetch(url, { method: 'GET' });
        const ct = (resp.headers.get('content-type') || '').toLowerCase();
        if (!resp.ok) {
            if (ct.includes('application/json')) {
                const j = await resp.json();
                throw new Error((j && j.error) || '下载失败');
            }
            throw new Error('下载失败');
        }
        if (ct.includes('application/json')) {
            const j = await resp.json();
            throw new Error((j && j.error) || '下载失败');
        }
        const blob = await resp.blob();
        const a = document.createElement('a');
        const filename = date ? `wow_daily_report_${date}.md` : 'wow_daily_report.md';
        a.href = window.URL.createObjectURL(blob);
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(a.href);
    } catch (e) {
        showMessage(`下载失败：${String(e.message || e)}`, 'warning');
    }
}

// 初始化SimC任务管理事件监听器
function parseSimcTaskExt(ext) {
    if (!ext) return {};
    if (typeof ext === 'object') return ext;
    const text = String(ext).trim();
    if (!text) return {};
    try {
        const parsed = JSON.parse(text);
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch (e) {
        return { selected_attributes: text };
    }
}

function applyRegularPreset(presetValue, timeInputId, targetInputId) {
    if (!presetValue || presetValue === 'custom') return;
    const [timeValue, targetValue] = String(presetValue).split(',');
    const timeInput = document.getElementById(timeInputId);
    const targetInput = document.getElementById(targetInputId);
    if (timeInput && timeValue) timeInput.value = String(parseInt(timeValue, 10) || 300);
    if (targetInput && targetValue) targetInput.value = String(parseInt(targetValue, 10) || 1);
}

function toPositiveInt(value, fallbackValue) {
    const n = parseInt(value, 10);
    if (!Number.isFinite(n) || n <= 0) return fallbackValue;
    return n;
}

function syncSimulationRegularPresetByInputs() {
    const preset = document.getElementById('simulation-regular-preset');
    const timeInput = document.getElementById('simulation-regular-time');
    const targetInput = document.getElementById('simulation-regular-target-count');
    if (!preset || !timeInput || !targetInput) return;
    const t = String(toPositiveInt(timeInput.value, 300));
    const c = String(toPositiveInt(targetInput.value, 1));
    const expected = `${t},${c}`;
    const matched = Array.from(preset.options || []).some(opt => opt.value === expected);
    preset.value = matched ? expected : 'custom';
}

async function loadSimulationRegularDefaultsByProfile(profileId) {
    const fallback = { time: 300, target_count: 1 };
    const pid = toPositiveInt(profileId, 0);
    if (!pid) return fallback;
    try {
        const response = await fetch(`/api/simc-profile/${pid}/`, {
            method: 'GET',
            headers: {
                'X-CSRFToken': getCSRFToken(),
                'Content-Type': 'application/json'
            }
        });
        if (!response.ok) return fallback;
        const data = await response.json();
        if (!data || !data.success) return fallback;
        const payload = (data.data && typeof data.data === 'object') ? data.data : data;
        return {
            time: toPositiveInt(payload.time, 300),
            target_count: toPositiveInt(payload.target_count, 1)
        };
    } catch (error) {
        console.warn('加载模拟默认参数失败，回退到标准值:', error);
        return fallback;
    }
}

function renderSpecBadgeHtml(specValue, displayValue = '', visual = {}) {
    const spec = String(specValue || '').trim();
    const text = String(displayValue || spec || '-').trim();
    const requestedColor = String(visual.class_color || '').trim();
    const classColor = /^#[0-9a-f]{6}$/i.test(requestedColor) ? requestedColor : '#64748B';
    const iconUrl = String(visual.spec_icon_url || '').trim();
    const mark = spec ? spec.charAt(0).toUpperCase() : '?';
    const icon = iconUrl
        ? `<img src="${escapeHtml(iconUrl)}" alt="" class="h-6 w-6 shrink-0 rounded object-cover" style="box-shadow: 0 0 0 1px var(--simc-class-color);">`
        : `<span class="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-[10px] font-bold text-white" style="background: var(--simc-class-color);">${escapeHtml(mark)}</span>`;
    return `<span class="inline-flex items-center gap-1.5 rounded-full border px-1.5 py-1 text-xs font-semibold" style="--simc-class-color: ${classColor}; border-color: var(--simc-class-color); background: color-mix(in srgb, var(--simc-class-color) 10%, white); color: color-mix(in srgb, var(--simc-class-color) 68%, #0f172a);">${icon}<span>${escapeHtml(text)}</span></span>`;
}

function syncSimcTaskInputMode(prefix) {
    const rawCodeInput = document.getElementById(prefix ? `${prefix}-simc-task-raw-code` : 'simc-task-raw-code');
    const profileSelect = document.getElementById(prefix ? `${prefix}-simc-config-select` : 'simc-config-select');
    const taskType = document.getElementById(prefix ? `${prefix}-simc-task-type` : 'simc-task-type');
    if (!rawCodeInput || !profileSelect) return;
    const isAttribute = taskType && String(taskType.value) === '2';
    const hasRaw = String(rawCodeInput.value || '').trim().length > 0;
    if (isAttribute) {
        rawCodeInput.value = '';
        rawCodeInput.disabled = true;
        profileSelect.disabled = false;
        return;
    }
    rawCodeInput.disabled = false;
    profileSelect.disabled = hasRaw;
    if (hasRaw) {
        profileSelect.value = '';
    }
}

function toggleTaskTypeFields(prefix, taskType) {
    const isAttribute = String(taskType) === '2';
    const attrSelect = document.getElementById(prefix ? `${prefix}-simc-task-profile` : 'simc-task-profile');
    const regularBox = document.getElementById(prefix ? `${prefix}-simc-task-regular-options` : 'simc-task-regular-options');
    const stepBox = document.getElementById(prefix ? `${prefix}-simc-task-attribute-step-wrapper` : 'simc-task-attribute-step-wrapper');
    const rawCodeBox = document.getElementById(prefix ? `${prefix}-simc-task-raw-code-wrapper` : 'simc-task-raw-code-wrapper');
    const rawCodeInput = document.getElementById(prefix ? `${prefix}-simc-task-raw-code` : 'simc-task-raw-code');

    if (attrSelect && attrSelect.parentElement) {
        attrSelect.style.display = isAttribute ? 'block' : 'none';
        attrSelect.parentElement.style.display = isAttribute ? 'block' : 'none';
        if (!isAttribute) attrSelect.value = '';
    }
    if (regularBox) regularBox.style.display = isAttribute ? 'none' : 'grid';
    if (stepBox) stepBox.style.display = isAttribute ? 'block' : 'none';
    if (rawCodeBox) rawCodeBox.style.display = isAttribute ? 'none' : 'block';
    if (rawCodeInput) {
        rawCodeInput.disabled = isAttribute;
        if (isAttribute) rawCodeInput.value = '';
    }
    syncSimcTaskInputMode(prefix);
}

// 在DOMContentLoaded事件中初始化SimC工作台
/* ===== SimC Workbench Dialog ===== */
let simcWorkbenchDialogPreviousFocus = null;

function openSimcWorkbenchDialog(contentType, data) {
    const dialog = document.getElementById('simc-workbench-dialog');
    const title = document.getElementById('simc-dialog-title');
    const body = document.getElementById('simc-dialog-body');
    if (!dialog || !title || !body) return;

    const wasHidden = dialog.classList.contains('hidden');
    if (!wasHidden) {
        document.dispatchEvent(new CustomEvent('simc-dialog-replace', { detail: { reason: 'replace' } }));
        document.dispatchEvent(new CustomEvent('simc-dialog-closing', { detail: { reason: 'replace' } }));
    }
    if (wasHidden) simcWorkbenchDialogPreviousFocus = document.activeElement;

    title.textContent = getTitleForDialogContent(contentType);
    body.innerHTML = '<div class="text-center py-8 text-gray-500"><i class="fas fa-spinner fa-spin mr-2"></i>加载中...</div>';
    const panel = document.getElementById('simc-workbench-dialog-content');
    if (panel) panel.scrollTop = 0;

    dialog.classList.remove('hidden');
    document.body.classList.add('simc-dialog-open');

    const firstFocusable = dialog.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
    if (firstFocusable) firstFocusable.focus();
}
window.openSimcWorkbenchDialog = openSimcWorkbenchDialog;

function closeSimcWorkbenchDialog() {
    const dialog = document.getElementById('simc-workbench-dialog');
    if (!dialog) return;
    document.dispatchEvent(new CustomEvent('simc-dialog-closing', { detail: { reason: 'close' } }));
    dialog.classList.add('hidden');
    document.body.classList.remove('simc-dialog-open');

    if (simcWorkbenchDialogPreviousFocus && typeof simcWorkbenchDialogPreviousFocus.focus === 'function') {
        simcWorkbenchDialogPreviousFocus.focus();
    }
    simcWorkbenchDialogPreviousFocus = null;
}
window.closeSimcWorkbenchDialog = closeSimcWorkbenchDialog;

function showSimcTaskCreatedDialog() {
    return new Promise(resolve => {
        const dialog = document.getElementById('simc-workbench-dialog');
        const body = document.getElementById('simc-dialog-body');
        if (!dialog || !body) {
            resolve();
            return;
        }

        let settled = false;
        let dismissTimer = null;
        const finish = () => {
            if (settled) return;
            settled = true;
            if (dismissTimer) window.clearTimeout(dismissTimer);
            document.removeEventListener('simc-dialog-closing', onDialogClosing);
            resolve();
        };
        const onDialogClosing = () => finish();
        const close = () => {
            closeSimcWorkbenchDialog();
            finish();
        };

        openSimcWorkbenchDialog('task-created');
        body.innerHTML = `
            <div class="py-8 text-center">
                <div class="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-emerald-100 text-2xl text-emerald-600" aria-hidden="true"><i class="fas fa-check"></i></div>
                <p class="text-lg font-semibold text-gray-900">任务已新建</p>
                <p class="mt-2 text-sm text-gray-500">任务已进入队列，可在任务列表查看进度。</p>
                <button type="button" data-simc-task-created-history class="mt-6 rounded-xl bg-blue-600 px-5 py-2.5 font-semibold text-white transition-colors hover:bg-blue-700">前往任务列表</button>
                <p class="mt-3 text-xs text-gray-400">此窗口将在 1 秒后自动关闭</p>
            </div>`;
        document.addEventListener('simc-dialog-closing', onDialogClosing, { once: true });
        body.querySelector('[data-simc-task-created-history]')?.addEventListener('click', () => {
            showDashboardSection(SIMC_DASHBOARD_SECTIONS.history);
            close();
        });
        dismissTimer = window.setTimeout(close, 1000);
    });
}

function getTitleForDialogContent(contentType) {
    const titles = {
        'profile-detail': '玩家配置详情',
        'profile-form': '配置管理',
        'template-detail': '模板详情',
        'template-form': '模板管理',
        'apl-form': 'APL 管理',

        'task-detail': '任务详情',
        'task-comparison': '任务对比',
        'task-created': '模拟任务已创建',
    };
    return titles[contentType] || '详情';
}

/* ===== SimC Workbench ===== */

function initSimcWorkbench() {
    const workbench = document.getElementById('simc-workbench');
    if (!workbench || workbench.dataset.initialized === '1') return;
    workbench.dataset.initialized = '1';

    document.querySelectorAll('[data-simc-workflow-entry]').forEach(button => {
        button.addEventListener('click', function() {
            switchSimcWorkbenchL1Tab('workflow', this.dataset.simcWorkflowEntry || 'import');
        });
    });

    // Model entry shortcuts in advanced section
    document.querySelectorAll('.simc-model-entry').forEach(btn => {
        btn.addEventListener('click', function() {
            switchSimcWorkbenchL1Tab('advanced');
            const data = this.dataset;
            const targetTab = data.simcTab;
            if (targetTab) {
                const model = data.simcModel;
                switchSimcWorkbenchTab(targetTab);
                if (model === 'tasks' && typeof window.simcWorkbenchLoadTaskResource === 'function') {
                    window.simcWorkbenchLoadTaskResource(model);
                }
                if (data.ruleSubtab) switchRuleSubtab(model);
            }
        });
    });

    bindSimcWorkbenchSimulationControls();
    bindSimcWorkbenchProfilesControls();
    bindSimcTalentStringControls();
    bindSimcWorkbenchRulesControls();

    document.querySelectorAll('[data-simc-table-shortcut]').forEach(btn => {
        btn.addEventListener('click', function() {
            openSimcTableShortcut(this.getAttribute('data-simc-table-shortcut'));
        });
    });

    // Dialog close handlers
    document.querySelectorAll('[data-simc-dialog-close]').forEach(btn => {
        btn.addEventListener('click', closeSimcWorkbenchDialog);
    });

    // Keep keyboard focus inside the modal; Escape closes it.
    document.addEventListener('keydown', function(event) {
        const dialog = document.getElementById('simc-workbench-dialog');
        if (!dialog || dialog.classList.contains('hidden')) return;
        if (event.key === 'Escape') {
            closeSimcWorkbenchDialog();
            return;
        }
        if (event.key !== 'Tab') return;
        const focusable = Array.from(dialog.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'))
            .filter(element => element.getClientRects().length > 0);
        if (!focusable.length) {
            event.preventDefault();
            return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    switchSimcWorkbenchL1Tab('workflow');
}

function activateSimcDashboardPage(pageName) {
    const targetSectionId = SIMC_DASHBOARD_SECTIONS[pageName];
    const activeSection = document.querySelector('[data-simc-page].content-section.active');
    if (!targetSectionId || !activeSection || activeSection.id === targetSectionId) return;

    document.querySelectorAll('[data-simc-page].content-section').forEach(section => {
        const isTarget = section.id === targetSectionId;
        section.style.display = isTarget ? 'block' : 'none';
        section.classList.toggle('active', isTarget);
    });

    document.querySelectorAll('.submenu-item[data-dashboard-section]').forEach(item => {
        item.classList.toggle('active', item.dataset.dashboardSection === targetSectionId);
    });
    const targetNav = document.querySelector(`.submenu-item[data-dashboard-section="${targetSectionId}"]`);
    const parentNav = targetNav?.closest('.nav-item');
    if (parentNav) {
        document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
        parentNav.classList.add('active');
    }
    document.dispatchEvent(new CustomEvent('dashboard-section-changed', {
        detail: { section: targetSectionId },
    }));
}

function switchSimcWorkbenchL1Tab(l1TabName, childPanelName) {
    const activeL1Tab = l1TabName || 'workflow';
    activateSimcDashboardPage(activeL1Tab);
    const defaultPanels = { workflow: 'import', history: 'tasks', advanced: 'backend' };
    const activeChildPanel = childPanelName || defaultPanels[activeL1Tab];

    if (typeof window.simcWorkbenchDeactivatePanel === 'function') {
        window.simcWorkbenchDeactivatePanel(activeChildPanel);
    }
    if (activeL1Tab !== 'history') stopSimcAttributeSearch();

    document.querySelectorAll('[data-simc-workflow-entry]').forEach(button => {
        const selected = activeL1Tab === 'workflow' && button.dataset.simcWorkflowEntry === activeChildPanel;
        button.classList.toggle('bg-blue-600', selected);
        button.classList.toggle('text-white', selected);
        button.classList.toggle('bg-white', !selected);
        button.classList.toggle('text-gray-700', !selected);
        button.setAttribute('aria-current', selected ? 'page' : 'false');
    });

    const activeRuleSubtab = document.querySelector('[data-rule-subtab][aria-selected="true"]')?.dataset.ruleSubtab || 'secondary-rules';
    updateSimcAdvancedEntryState(activeL1Tab, activeChildPanel, activeRuleSubtab);

    document.querySelectorAll('.simc-l1-panel').forEach(panel => {
        panel.classList.toggle('hidden', panel.getAttribute('data-simc-l1-panel') !== activeL1Tab);
    });

    document.querySelectorAll('.simc-workbench-panel').forEach(panel => {
        panel.classList.toggle('hidden', panel.getAttribute('data-simc-panel') !== activeChildPanel);
    });

    if (activeChildPanel && typeof window.simcWorkbenchLoadPanel === 'function') {
        window.simcWorkbenchLoadPanel(activeChildPanel);
    }
    if (activeChildPanel === 'profiles') loadSimcWorkbenchProfiles();
    if (activeChildPanel === 'talent-strings') loadSimcTalentStrings();
    if (activeChildPanel === 'rules') { loadSimcWorkbenchRules(); loadSimcWorkbenchMastery(); }
}

function updateSimcAdvancedEntryState(activeL1Tab, activeChildPanel, activeRuleSubtab) {
    document.querySelectorAll('.simc-model-entry').forEach(button => {
        const targetPanel = button.dataset.simcTab;
        const targetRule = button.dataset.ruleSubtab;
        const selected = activeL1Tab === 'advanced'
            && targetPanel === activeChildPanel
            && (targetPanel !== 'rules' || targetRule === activeRuleSubtab);
        button.classList.toggle('bg-blue-600', selected);
        button.classList.toggle('text-white', selected);
        button.classList.toggle('bg-white', !selected);
        button.classList.toggle('text-gray-700', !selected);
        button.setAttribute('aria-current', selected ? 'page' : 'false');
    });
}

function switchSimcWorkbenchTab(tabName) {
    const activeTab = tabName || 'import';
    const parentPanels = {
        import: 'workflow',
        tasks: 'history',
        artifacts: 'history',
        profiles: 'workflow',
        'talent-strings': 'workflow',
        templates: 'workflow',
        apl: 'workflow',

        backend: 'advanced',
        rules: 'advanced'
    };
    const parentTab = parentPanels[activeTab] || 'advanced';
    switchSimcWorkbenchL1Tab(parentTab, activeTab);

}

function switchRuleSubtab(resource) {
    const selectedResource = resource === 'mastery-rules' ? 'mastery-rules' : 'secondary-rules';
    document.querySelectorAll('[data-rule-subtab]').forEach(tab => {
        const selected = tab.dataset.ruleSubtab === selectedResource;
        tab.setAttribute('aria-selected', String(selected));
        tab.classList.toggle('active', selected);
        tab.classList.toggle('bg-blue-600', selected);
        tab.classList.toggle('text-white', selected);
    });
    document.querySelectorAll('[data-rule-panel]').forEach(panel => {
        panel.classList.toggle('hidden', panel.dataset.rulePanel !== selectedResource);
    });
    updateSimcAdvancedEntryState('advanced', 'rules', selectedResource);
    if (selectedResource === 'mastery-rules') loadSimcWorkbenchMastery();
    else loadSimcWorkbenchRules();
}

/* ===== SimC 工具台 — 独立天赋字符串 ===== */
let simcTalentStringEditId = null;
function loadSimcTalentStrings() {
    const list = document.getElementById('simc-talent-string-list');
    if (!list) return;
    const spec = document.getElementById('simc-talent-string-spec-filter')?.value || '';
    list.innerHTML = '<p class="py-6 text-center text-sm text-slate-400">加载中...</p>';
    fetch('/api/simc-talent-string/?spec=' + encodeURIComponent(spec), { headers: { 'X-CSRFToken': getCSRFToken() } })
        .then(response => response.json()).then(payload => {
            if (!payload.success) throw new Error(payload.error || '加载失败');
        const rows = payload.data || [];
        list.innerHTML = rows.length ? `<table class="min-w-full text-sm"><thead><tr class="border-b text-left text-xs text-slate-500"><th class="px-3 py-2">名称</th><th class="px-3 py-2">专精</th><th class="px-3 py-2">英雄天赋树</th><th class="px-3 py-2">来源</th><th class="px-3 py-2 text-right">操作</th></tr></thead><tbody>${rows.map(row => { const heroNames = (row.hero_talent_names || []).join('、') || '未解析'; const simulator = row.talent_simulator_url ? `<a href="${escapeHtml(row.talent_simulator_url)}" target="_blank" rel="noopener noreferrer" class="mr-2 text-violet-600">天赋模拟器</a>` : ''; return `<tr class="border-b"><td class="px-3 py-3 font-medium">${escapeHtml(row.name)}</td><td class="px-3 py-3">${escapeHtml(row.label || `${row.spec_label} · ${row.class_label || ''}`.replace(/ · $/, '') || row.spec)}</td><td class="px-3 py-3 text-slate-600">${escapeHtml(heroNames)}</td><td class="px-3 py-3 text-xs text-slate-500">${row.is_system ? '系统资源' : '个人资源'}</td><td class="px-3 py-3 text-right whitespace-nowrap">${simulator}<button type="button" data-talent-string-action="view" data-talent-string-id="${row.id}" class="mr-2 text-slate-600">查看</button><button type="button" data-talent-string-action="copy-code" data-talent-string-id="${row.id}" class="mr-2 text-blue-600">复制字符串</button>${row.can_edit ? `<button type="button" data-talent-string-action="edit" data-talent-string-id="${row.id}" class="mr-2 text-blue-600">编辑</button>` : ''}${row.can_delete ? `<button type="button" data-talent-string-action="delete" data-talent-string-id="${row.id}" class="text-red-600">删除</button>` : ''}</td></tr>`; }).join('')}</tbody></table>` : '<p class="py-6 text-center text-sm text-slate-400">暂无天赋字符串</p>';
            rows.forEach(row => { if (!window.simcTalentStringRows) window.simcTalentStringRows = {}; window.simcTalentStringRows[row.id] = row; });
        }).catch(error => { list.innerHTML = `<p class="py-6 text-center text-sm text-red-500">${escapeHtml(error.message)}</p>`; });
}
function simcTalentStringOpenEditor(row = null) {
    simcTalentStringEditId = row?.id || null;
    openSimcWorkbenchDialog('talent-string-editor', row || {});
    const title = document.getElementById('simc-dialog-title');
    const body = document.getElementById('simc-dialog-body');
    if (!title || !body) return;
    title.textContent = row ? '编辑天赋字符串' : '新增天赋字符串';
    body.innerHTML = `<div class="space-y-4"><div class="grid gap-3 sm:grid-cols-2"><label class="text-sm">名称<input id="simc-talent-string-name" class="mt-1 w-full rounded-lg border px-3 py-2" value="${escapeHtml(row?.name || '')}"></label><label class="text-sm">专精（可选，留空自动识别）<select id="simc-talent-string-spec" class="mt-1 w-full rounded-lg border px-3 py-2"></select></label></div><label class="block text-sm">天赋字符串<textarea id="simc-talent-string-talent" rows="5" class="mt-1 w-full rounded-lg border px-3 py-2 font-mono text-xs">${escapeHtml(row?.talent || '')}</textarea></label><div class="flex justify-end gap-2"><button type="button" id="simc-talent-string-cancel" class="rounded-lg border bg-white px-3 py-2 text-sm">取消</button><button type="button" id="simc-talent-string-save" class="rounded-lg bg-blue-600 px-3 py-2 text-sm text-white">保存</button></div></div>`;
    loadSimcSpecOptions().then(rows => {
        const specSelect = body.querySelector('#simc-talent-string-spec');
        if (!specSelect) return;
        specSelect.innerHTML = '<option value="">选择专精</option>' + rows.map(item => `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label || `${item.spec_label} · ${item.class_label}`)}</option>`).join('');
        specSelect.value = row?.spec || '';
    }).catch(error => showMessage(error.message, 'error'));
    document.getElementById('simc-talent-string-cancel').addEventListener('click', closeSimcWorkbenchDialog);
    document.getElementById('simc-talent-string-save').addEventListener('click', saveSimcTalentString);
}
async function parseSimcTalentStringResponse(response) {
    const text = await response.text();
    let result;
    try {
        result = text ? JSON.parse(text) : {};
    } catch (_) {
        throw new Error(response.status === 403 ? '请求被拒绝，请刷新页面后重试' : `服务器返回了非 JSON 响应（HTTP ${response.status}）`);
    }
    if (!response.ok) throw new Error(result.error || result.detail || `请求失败（HTTP ${response.status}）`);
    return result;
}

async function saveSimcTalentString() {
    const payload = { name: document.getElementById('simc-talent-string-name').value.trim(), spec: document.getElementById('simc-talent-string-spec').value, talent: document.getElementById('simc-talent-string-talent').value.trim() };
    if (!payload.name || !payload.talent) return showMessage('请填写名称和天赋字符串', 'error');
    if (simcTalentStringEditId) payload.id = simcTalentStringEditId;
    try {
        const response = await fetch('/api/simc-talent-string/', { method: simcTalentStringEditId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() }, body: JSON.stringify(payload) });
        const result = await parseSimcTalentStringResponse(response);
        if (!result.success) return showMessage(result.error || '保存失败', 'error');
        closeSimcWorkbenchDialog(); showMessage(result.message || '已保存', 'success'); loadSimcTalentStrings();
    } catch (error) {
        showMessage(error.message || '保存失败，请稍后重试', 'error');
    }
}
function mountTalentThumbnail(container, buildCode, width = 420) {
    if (!container || !buildCode || !window.TalentTreeThumbnail) return Promise.resolve(null);
    container.__talentTreeThumbnail?.destroy?.();
    return window.TalentTreeThumbnail.mount(container, { buildCode }, { width, borderRadius: 12 })
        .then(instance => { container.__talentTreeThumbnail = instance; return instance; })
        .catch(error => { if (error.name !== 'AbortError') console.error('天赋缩略图加载失败', error); return null; });
}
function bindSimcTalentStringControls() {
    const specFilter = document.getElementById('simc-talent-string-spec-filter');
    const specEditor = document.getElementById('simc-talent-string-spec');
    if (!specFilter || specFilter.dataset.bound === '1') return;
    specFilter.dataset.bound = '1';
    loadSimcSpecOptions().then(rows => {
        const options = '<option value="">全部专精</option>' + rows.map(row => `<option value="${escapeHtml(row.value)}">${escapeHtml(row.label || `${row.spec_label} · ${row.class_label}`)}</option>`).join('');
        specFilter.innerHTML = options;
        specFilter.dataset.specCatalogLoaded = '1';
    }).catch(error => showMessage(error.message, 'error'));
    document.getElementById('simc-talent-string-refresh')?.addEventListener('click', loadSimcTalentStrings);
    document.getElementById('simc-talent-string-add')?.addEventListener('click', () => simcTalentStringOpenEditor());
    specFilter.addEventListener('change', loadSimcTalentStrings);
    document.addEventListener('click', async event => {
        const button = event.target.closest('[data-talent-string-action]'); if (!button) return;
        const id = Number(button.dataset.talentStringId); const row = window.simcTalentStringRows?.[id]; const action = button.dataset.talentStringAction;
        if (action === 'view') { openSimcWorkbenchDialog('talent-string-view', row || {}); document.getElementById('simc-dialog-title').textContent = row?.name || '天赋字符串'; const body = document.getElementById('simc-dialog-body'); body.innerHTML = `<div class="space-y-4"><div class="text-sm text-slate-600">${escapeHtml(row?.spec_label || row?.spec || '')} · 英雄天赋树：${escapeHtml((row?.hero_talent_names || []).join('、') || '未解析')}</div><div data-talent-thumbnail-view class="overflow-hidden rounded-xl"></div><pre class="max-h-[55vh] overflow-auto rounded-lg bg-slate-900 p-4 text-xs text-slate-100 whitespace-pre-wrap break-all">${escapeHtml(row?.talent || '')}</pre>${row?.talent_simulator_url ? `<a href="${escapeHtml(row.talent_simulator_url)}" target="_blank" rel="noopener noreferrer" class="inline-flex rounded-lg bg-violet-600 px-3 py-2 text-sm text-white">在天赋模拟器中打开</a>` : ''}</div>`; mountTalentThumbnail(body.querySelector('[data-talent-thumbnail-view]'), row?.talent || ''); return; }
        if (action === 'edit') return simcTalentStringOpenEditor(row);
        if (action === 'copy-code') {
            if (!row?.talent) return showMessage('天赋字符串为空', 'error');
            try {
                await navigator.clipboard.writeText(row.talent);
                showMessage('天赋字符串已复制', 'success');
            } catch (_) {
                showMessage('复制失败，请手动复制天赋字符串', 'error');
            }
            return;
        }
        if (action === 'delete' && confirm('确认删除此天赋字符串？')) { const response = await fetch('/api/simc-talent-string/', { method: 'DELETE', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() }, body: JSON.stringify({ id }) }); const result = await response.json(); showMessage(result.message || result.error, result.success ? 'success' : 'error'); if (result.success) loadSimcTalentStrings(); }
    });
}

/* ===== SimC 工具台 — 配置管理（profiles） ===== */
let simcWbProfileSpecFilter = '';
let simcWbProfilePage = 1;
let simcWbProfileTotalPages = 1;
let simcWbProfileListRequestSerial = 0;
let simcWbProfileListAbortController = null;
let simcWbProfileSort = { key: '', direction: 'asc' };
let simcProfileTalentVersions = { retail: '', ptr: '' };

function simcProfileSortValue(row, key) {
    if (key === 'id') return Number(row.id || 0);
    if (key === 'name') return String(row.name || '');
    if (key === 'spec') return String(row.spec_label || row.spec || '');
    if (key === 'status') return row.is_active ? 1 : 0;
    if (key === 'source') {
        if (row.is_system === true) return `系统默认配置 ${row.version || ''} ${row.sync_version || ''}`;
        const labels = {
            manual_equipment: '手动配置',
            attribute_only: '冻结玩家基线 + 绿字覆盖',
            wcl: 'Warcraft Logs',
            battlenet: 'Battle.net',
        };
        const mode = row.player_config_mode || 'battlenet';
        return `${labels[mode] || mode} ${row.battlenet_region || ''} ${row.battlenet_realm || ''} ${row.battlenet_character || ''}`;
    }
    return '';
}

function sortSimcProfileRows(rows, sortState) {
    const key = sortState?.key || '';
    if (!key) return rows;
    const multiplier = sortState.direction === 'desc' ? -1 : 1;
    return rows.map((row, index) => ({ row, index })).sort((left, right) => {
        const leftValue = simcProfileSortValue(left.row, key);
        const rightValue = simcProfileSortValue(right.row, key);
        let compared;
        if (typeof leftValue === 'number' && typeof rightValue === 'number') {
            compared = leftValue - rightValue;
        } else {
            compared = String(leftValue).localeCompare(String(rightValue), 'zh-CN', {
                numeric: true,
                sensitivity: 'base',
            });
        }
        if (compared) return compared * multiplier;
        return left.index - right.index;
    }).map(item => item.row);
}

function updateSimcProfileSortHeaders() {
    document.querySelectorAll('[data-profile-sort]').forEach(button => {
        const active = button.dataset.profileSort === simcWbProfileSort.key;
        const direction = active ? simcWbProfileSort.direction : '';
        const header = button.closest('th');
        if (header) header.setAttribute('aria-sort', active ? (direction === 'desc' ? 'descending' : 'ascending') : 'none');
        const icon = button.querySelector('[data-profile-sort-icon]');
        if (icon) icon.textContent = active ? (direction === 'desc' ? '▼' : '▲') : '↕';
    });
}

function simcProfileMatchesSpecFilter(row, requestedFilter) {
    const filter = String(requestedFilter || '').trim().toLowerCase();
    if (!filter) return true;
    return String(row.canonical_spec || '').trim().toLowerCase() === filter;
}

function loadSimcWorkbenchProfiles(page) {
    page = page || 1;
    simcWbProfilePage = page;
    const requestedPage = page;
    const requestedFilter = simcWbProfileSpecFilter;
    const requestedSort = { ...simcWbProfileSort };
    const requestSerial = ++simcWbProfileListRequestSerial;
    if (simcWbProfileListAbortController) simcWbProfileListAbortController.abort();
    const abortController = new AbortController();
    simcWbProfileListAbortController = abortController;
    const tbody = document.getElementById('simc-wb-profile-list');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-gray-400"><i class="fas fa-spinner fa-spin mr-2"></i>加载中…</td></tr>';

    const csrf = getCSRFToken();
    if (!csrf) { tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-red-500">无法获取 CSRF Token</td></tr>'; return; }

    fetch('/api/simc-profile/', {
        method: 'GET',
        headers: { 'X-CSRFToken': csrf },
        signal: abortController.signal,
    }).then(r => r.json()).then(data => {
        if (requestSerial !== simcWbProfileListRequestSerial || requestedPage !== simcWbProfilePage || requestedFilter !== simcWbProfileSpecFilter) return;
        if (!data.success) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-red-500">加载失败</td></tr>';
            return;
        }
        simcProfileTalentVersions = {
            retail: String(data.talent_versions?.retail || ''),
            ptr: String(data.talent_versions?.ptr || ''),
        };
        let rows = data.data || [];

        // Client-side spec filtering. System profiles use class_spec while user
        // profiles use the Dashboard canonical spec key, so compare normalized pairs.
        if (requestedFilter) {
            rows = rows.filter(row => simcProfileMatchesSpecFilter(row, requestedFilter));
        }
        rows = sortSimcProfileRows(rows, requestedSort);

        // Client-side pagination
        const total = rows.length;
        simcWbProfileTotalPages = Math.max(1, Math.ceil(total / 20));
        const startIdx = (requestedPage - 1) * 20;
        const endIdx = startIdx + 20;
        const pageRows = rows.slice(startIdx, endIdx);

        if (!pageRows.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-gray-400">暂无配置</td></tr>';
            renderSimcWbPagination('simc-wb-profile-pagination', simcWbProfilePage, simcWbProfileTotalPages, loadSimcWorkbenchProfiles);
            return;
        }

        tbody.innerHTML = pageRows.map((row, idx) => {
            const id = row.id || 0;
            const name = escapeHtml(row.name || '-');
            const spec = row.spec || '';
            const specLabel = row.spec_label || spec;
            const mode = row.player_config_mode || 'battlenet';
            const isSystem = row.is_system === true;
            const equipmentLineCount = Number(row.equipment_line_count || 0);
            const versionLabel = row.version ? ` · 版本 ${row.version}` : '';
            const syncLabel = row.sync_version ? ` · 同步 ${row.sync_version}` : '';
            const sourceText = isSystem
                ? `系统默认配置${versionLabel}${syncLabel}${equipmentLineCount ? ` · ${equipmentLineCount} 行` : ''}`
                : mode === 'manual_equipment'
                    ? ('手动配置 ' + (row.player_equipment ? ('(' + String(row.player_equipment).split('\n').filter(Boolean).length + ' 行)') : ''))
                    : mode === 'attribute_only'
                        ? ('冻结玩家基线 + 绿字覆盖 ' + (row.player_equipment ? ('(' + String(row.player_equipment).split('\n').filter(Boolean).length + ' 行)') : '(历史配置缺少基线)'))
                        : ('Battle.net ' + [row.battlenet_region, row.battlenet_realm, row.battlenet_character].filter(Boolean).join('/'));
            const sourceTitle = escapeHtml(sourceText || '-');
            const statusText = row.is_active ? '生效中' : '未生效';
            const statusClass = row.is_active ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500';
            const offset = startIdx + idx + 1;
            const managementActions = `<button class="text-violet-600 hover:text-violet-800 text-xs" data-profile-row-action="simulate" data-profile-id="${id}" data-profile-spec="${escapeHtml(row.canonical_spec || '')}" title="立即模拟"><i class="fas fa-play"></i></button>
                <button class="text-slate-600 hover:text-slate-900 text-xs" data-profile-row-action="view" data-profile-id="${id}" title="查看详情"><i class="fas fa-eye"></i></button>
                <button class="text-emerald-600 hover:text-emerald-800 text-xs" data-profile-row-action="copy" data-profile-id="${id}" title="复制"><i class="fas fa-copy"></i></button>
                ${row.can_edit ? `<button class="text-blue-600 hover:text-blue-800 text-xs" data-profile-row-action="edit" data-profile-id="${id}" title="编辑"><i class="fas fa-edit"></i></button>` : ''}
                ${row.can_delete ? `<button class="text-red-600 hover:text-red-800 text-xs" data-profile-row-action="delete" data-profile-id="${id}" title="删除"><i class="fas fa-trash-alt"></i></button>` : ''}`;
            return `<tr class="hover:bg-gray-50 border-b border-gray-100">
                <td class="px-3 py-3 text-center text-gray-500 text-xs">${offset}</td>
                <td class="px-3 py-3 text-sm font-medium text-gray-900 max-w-[200px] truncate" title="${name}">${name}</td>
                <td class="px-3 py-3 text-center">${renderSpecBadgeHtml(spec, specLabel, { spec_icon_url: row.spec_icon_url, class_color: row.class_color })}</td>
                <td class="px-3 py-3 text-xs text-gray-500 max-w-[220px] truncate" title="${sourceTitle}">${sourceTitle}</td>
                <td class="px-3 py-3 text-center"><span class="rounded-full px-2 py-1 text-xs ${statusClass}">${statusText}</span></td>
                <td class="px-3 py-3 text-center">
                    <div class="flex items-center justify-center gap-1 flex-wrap">
                        ${managementActions}
                    </div>
                </td>
            </tr>`;
        }).join('');

        renderSimcWbPagination('simc-wb-profile-pagination', simcWbProfilePage, simcWbProfileTotalPages, loadSimcWorkbenchProfiles);
    }).catch(error => {
        if (error.name === 'AbortError') return;
        if (requestSerial !== simcWbProfileListRequestSerial || requestedPage !== simcWbProfilePage || requestedFilter !== simcWbProfileSpecFilter) return;
        tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-red-500">网络错误</td></tr>';
    }).finally(() => {
        if (simcWbProfileListAbortController === abortController) simcWbProfileListAbortController = null;
    });
}

function renderSimcProfileEquipmentCards(items, { compact = false } = {}) {
    const equipment = Array.isArray(items) ? items : [];
    if (!equipment.length) return '<div class="rounded-lg border border-dashed border-slate-300 px-3 py-5 text-center text-sm text-slate-400">未解析到装备槽位</div>';
    return equipment.map(item => {
        const esc = value => escapeHtml(String(value == null || value === '' ? '-' : value));
        const itemId = item.item_id || item.id || '';
        const itemMeta = [item.item_level ? `装等 ${esc(item.item_level)}` : '', itemId ? `#${esc(itemId)}` : ''].filter(Boolean).join(' · ');
        const enchantId = item.enchant?.enchantment_id || item.enchant?.id || '';
        const enchantName = item.enchant?.display_name || item.enchant?.name_zh || item.enchant?.name || item.enchant?.simc_name || (enchantId ? `附魔 #${enchantId}` : '');
        const enchant = enchantName ? `<div class="mt-1 text-xs text-violet-700"><i class="fas fa-magic mr-1"></i>${esc(enchantName)}${enchantId ? `<span class="ml-1 text-violet-400">#${esc(enchantId)}</span>` : ''}</div>` : '';
        const gems = (item.gems || []).length ? `<div class="mt-1 text-xs text-cyan-700"><i class="fas fa-gem mr-1"></i>${item.gems.map(gem => esc(gem.display_name)).join('、')}</div>` : '';
        const tooltipDescription = String(item.display_description || '').trim();
        const iconUrl = String(item.icon_url || '').trim();
        const itemIcon = iconUrl
            ? `<img class="wow-item-icon" src="${escapeHtml(iconUrl)}" alt="" loading="lazy">`
            : '';
        const tooltipAttrs = tooltipDescription
            ? ` data-wow-item-tooltip="${escapeHtml(tooltipDescription)}" data-wow-item-tooltip-name="${escapeHtml(String(item.display_name || itemId || '装备'))}" tabindex="0"`
            : '';
        return `<article${tooltipAttrs} class="min-w-0 rounded-lg border border-slate-200 bg-white ${compact ? 'p-2.5' : 'p-3'} shadow-sm"><div class="flex items-start justify-between gap-3"><div class="flex min-w-0 items-center gap-2">${itemIcon}<div class="min-w-0"><div class="text-[11px] font-semibold text-slate-400">${esc(item.slot_label || item.slot)}</div><div class="mt-0.5 truncate text-sm font-semibold text-slate-800" title="${esc(item.display_name)}">${esc(item.display_name)}</div></div></div><span class="shrink-0 rounded bg-slate-100 px-1.5 py-1 text-[11px] font-medium text-slate-500">${itemMeta || '-'}</span></div>${enchant}${gems}</article>`;
    }).join('');
}

function renderSimcOmniumTalents(items) {
    const entries = Array.isArray(items) ? items : [];
    if (!entries.length) return '';
    return entries.map(entry => {
        const entryId = entry.entry_id || entry.id || '';
        const token = String(entry.token || '').trim();
        const nodeKey = entryId || token || '-';
        const rank = entry.rank ?? '-';
        const displayName = entry.display_name || entry.name_zh || entry.name || '';
        const description = entry.display_description || entry.description_zh || entry.description || '';
        const iconUrl = String(entry.icon_url || '').trim();
        const maxRank = Number(entry.max_rank || 0);
        const rankLabel = maxRank >= Number(rank) ? `${rank}/${maxRank} 级` : `${rank} 级`;
        const identifiers = [
            entryId ? `Entry ${entryId}` : '',
            token,
            entry.spell_id ? `Spell ${entry.spell_id}` : '',
        ].filter(Boolean).join(' · ');
        const icon = iconUrl
            ? `<img class="h-9 w-9 shrink-0 rounded-md border border-amber-200 bg-white object-cover" src="${escapeHtml(iconUrl)}" alt="" loading="lazy">`
            : '<span class="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-amber-200 bg-white text-sm text-amber-600">✦</span>';
        return `<article class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5" data-omnium-entry="${escapeHtml(String(nodeKey))}"><div class="flex items-start gap-2.5">${icon}<div class="min-w-0 flex-1"><div class="flex flex-wrap items-center gap-1.5"><strong class="text-sm text-amber-950">${escapeHtml(String(displayName || `万奥条目 #${nodeKey}`))}</strong><span class="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">万奥符文</span><span class="rounded bg-white px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">${escapeHtml(rankLabel)}</span></div><div class="mt-1 font-mono text-[11px] text-amber-700">${escapeHtml(identifiers)}</div>${description ? `<p class="mt-1.5 text-xs leading-5 text-amber-900/80">${escapeHtml(String(description))}</p>` : ''}</div></div></article>`;
    }).join('');
}

function renderSimcProfileFormEquipmentPreview(detail, formWrap = document.getElementById('simc-wb-profile-form')) {
    const target = formWrap?.querySelector('[data-profile-equipment-preview-content]');
    if (!target) return;
    const omniumTalents = renderSimcOmniumTalents(detail?.omnium_talents);
    const omniumContent = omniumTalents || '<div class="rounded-lg border border-dashed border-amber-200 bg-amber-50/50 px-3 py-3 text-xs text-amber-700">当前 Profile 未解析到 <code class="font-mono">omnium_talents=</code>。请确认原始 SimC 配置包含该行后重新保存。</div>';
    target.className = 'mt-3 space-y-3';
    target.innerHTML = `<section data-profile-omnium-talents><h6 class="text-sm font-semibold text-amber-900">万奥宝典</h6><p class="mb-2 mt-0.5 text-[11px] text-amber-700">独立玩家能力系统，不属于职业天赋树</p><div class="grid gap-2 sm:grid-cols-2">${omniumContent}</div></section><div class="grid gap-2 sm:grid-cols-2">${renderSimcProfileEquipmentCards(detail?.equipment, { compact: true })}</div>`;
}

function renderSimcProfileDetailDialog(detail) {
    const profile = detail.profile || {};
    const esc = value => escapeHtml(String(value == null || value === '' ? '-' : value));
    const talentVersion = String(detail.talent_versions?.[profile.use_ptr ? 'ptr' : 'retail'] || '');
    const talentUrl = simcTalentSimulatorUrl(profile.talent, profile.canonical_spec, talentVersion);
    const talentLink = talentUrl
        ? `<a data-profile-detail-talent-link href="${escapeHtml(talentUrl)}" target="_blank" rel="noopener noreferrer" class="inline-flex items-center rounded-lg border border-violet-200 bg-white px-3 py-1.5 text-sm font-medium text-violet-700 transition hover:border-violet-400 hover:bg-violet-50"><i class="fas fa-project-diagram mr-1.5"></i>打开天赋模拟器</a>`
        : '<span class="text-xs text-slate-400">当前配置没有可查看的天赋码</span>';
    const equipment = renderSimcProfileEquipmentCards(detail.equipment);
    const omniumTalents = renderSimcOmniumTalents(detail.omnium_talents);
    const omniumContent = omniumTalents || '<div class="rounded-lg border border-dashed border-amber-200 bg-amber-50/50 px-3 py-3 text-sm text-amber-700">当前 Profile 未解析到 <code class="font-mono">omnium_talents=</code>。请检查下方原始玩家配置，并重新导入包含该行的 SimC Profile。</div>';
    const consumables = detail.consumables || {};
    const consumableDetails = detail.consumable_details || {};
    const localizedConsumable = (key, fallback) => {
        const row = consumableDetails[key];
        if (row && typeof row === 'object') return row;
        return { value: fallback || '', label: fallback || '' };
    };
    const consumableLabels = {
        flask: '合剂', potion: '药水', food: '食物', augmentation: '增幅符文',
    };
    const consumableRows = Object.entries(consumableLabels)
        .filter(([key]) => consumables[key])
        .map(([key, label]) => {
            const row = localizedConsumable(key, consumables[key]);
            return `<div class="rounded-lg bg-slate-50 px-3 py-2" title="${esc(row.value)}"><div class="text-[11px] text-slate-400">${label}</div><div class="mt-0.5 text-xs font-semibold text-slate-700">${esc(row.label)}</div></div>`;
        });
    const temporaryEnchantLabels = { main_hand: '主手临时附魔', off_hand: '副手临时附魔' };
    Object.entries(consumables.temporary_enchant || {}).forEach(([slot, value]) => {
        const detailRow = consumableDetails.temporary_enchant?.[slot] || { value, label: value };
        consumableRows.push(`<div class="rounded-lg bg-slate-50 px-3 py-2" title="${esc(detailRow.value)}"><div class="text-[11px] text-slate-400">${esc(temporaryEnchantLabels[slot] || slot)}</div><div class="mt-0.5 text-xs font-semibold text-slate-700">${esc(detailRow.label)}</div></div>`);
    });
    const talentStringLabels = {
        talents: '导入字符串', class_talents: '职业天赋', spec_talents: '专精天赋', hero_talents: '英雄天赋',
    };
    const talentStringRows = Object.entries(detail.talent_strings || {}).map(([key, row]) => {
        const entries = Array.isArray(row.entries) ? row.entries : [];
        const parsed = entries.length
            ? `<div class="mt-2 flex flex-wrap gap-1.5">${entries.map(entry => `<span class="rounded bg-violet-50 px-2 py-1 font-mono text-[11px] text-violet-700">spell ${esc(entry.spell_id)} · ${esc(entry.rank)} 级</span>`).join('')}</div>`
            : '';
        return `<div class="border-b border-slate-100 py-2 last:border-b-0"><div class="text-xs font-semibold text-slate-600">${esc(talentStringLabels[key] || key)}</div><div class="mt-1 break-all font-mono text-xs text-slate-500">${esc(row.value)}</div>${parsed}</div>`;
    }).join('');
    const source = detail.source || {};
    const stats = detail.stats || {};
    const sourceLabel = source.label || profile.player_config_mode || '-';
    const statRows = [
        ['力量', stats.strength ?? profile.gear_strength],
        ['暴击', stats.crit ?? profile.gear_crit],
        ['急速', stats.haste ?? profile.gear_haste],
        ['精通', stats.mastery ?? profile.gear_mastery],
        ['全能', stats.versatility ?? profile.gear_versatility],
    ].map(([label, value]) => `<div class="rounded-lg bg-slate-50 px-3 py-2"><div class="text-[11px] text-slate-400">${label}</div><div class="mt-0.5 text-sm font-semibold text-slate-700">${esc(value)}</div></div>`).join('');
    openSimcWorkbenchDialog('profile-detail');
    const body = document.getElementById('simc-dialog-body');
    if (!body) return;
    const syncNotice = profile.is_system ? '<p class="mt-2 text-xs text-amber-700">这是由上游同步维护的系统配置。</p>' : '';
    body.innerHTML = `<div class="space-y-5"><header class="border-b border-slate-200 pb-4"><div class="flex flex-wrap items-start justify-between gap-3"><div><h3 class="text-lg font-bold text-slate-900">${esc(profile.name)}</h3><p class="mt-1 text-sm text-slate-500">${esc(profile.spec_label || profile.spec)} · ${esc(sourceLabel)}</p></div><span class="rounded-full px-2.5 py-1 text-xs font-medium ${profile.is_active ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'}">${profile.is_active ? '生效中' : '未生效'}</span></div>${syncNotice}</header><section class="grid grid-cols-2 gap-2 sm:grid-cols-5">${statRows}</section><section class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-violet-100 bg-violet-50/60 p-3"><div class="min-w-0"><h4 class="font-semibold text-slate-900">天赋配置</h4><p class="mt-1 max-w-xl break-all font-mono text-xs text-slate-500">${esc(profile.talent)}</p></div>${talentLink}</section>${talentStringRows ? `<section><h4 class="mb-2 font-semibold text-slate-900">天赋字符串拆解</h4><div class="rounded-lg border border-slate-200 px-3">${talentStringRows}</div></section>` : ''}<section data-profile-omnium-talents><h4 class="font-semibold text-slate-900">万奥宝典</h4><p class="mb-3 mt-0.5 text-xs text-slate-500">独立玩家能力系统，不属于职业天赋树</p><div class="grid gap-2 sm:grid-cols-2">${omniumContent}</div></section>${consumableRows.length ? `<section><h4 class="mb-3 font-semibold text-slate-900">消耗品与临时附魔</h4><div class="grid grid-cols-2 gap-2 sm:grid-cols-3">${consumableRows.join('')}</div></section>` : ''}<section><h4 class="mb-3 font-semibold text-slate-900">装备、附魔与宝石</h4><div class="grid gap-2 sm:grid-cols-2">${equipment}</div></section><details class="rounded-lg border border-slate-200 bg-slate-50"><summary class="cursor-pointer px-3 py-2 text-sm font-semibold text-slate-700">原始玩家配置</summary><pre class="max-h-[28rem] overflow-auto border-t border-slate-200 bg-slate-950 p-3 text-xs leading-5 text-slate-100 whitespace-pre-wrap">${esc(profile.raw_player_equipment)}</pre></details></div>`;
}
async function simcWbViewProfile(id) {
    try {
        const response = await fetch(`/api/simc-player-config-detail/?profile_id=${encodeURIComponent(id)}`, { headers: { 'X-CSRFToken': getCSRFToken() } });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载配置详情失败');
        renderSimcProfileDetailDialog(payload.data || {});
    } catch (error) { showMessage(String(error.message || error), 'error'); }
}

async function simcWbCopyProfile(id) {
    try {
        const response = await fetch('/api/simc-profile/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ copy_from_id: Number(id) }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '复制配置失败');
        showMessage(`已复制为“${payload.data?.name || '配置副本'}”`, 'success');
        await loadSimcWorkbenchProfiles(simcWbProfilePage);
    } catch (error) {
        showMessage(String(error.message || error), 'error');
    }
}

let simcSpecOptionsPromise = null;

async function loadSimcSpecOptions() {
    if (!simcSpecOptionsPromise) {
        simcSpecOptionsPromise = fetch('/api/simc-spec-options/', {
            headers: { 'X-CSRFToken': getCSRFToken() },
        }).then(async response => {
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || '加载专精选项失败');
            return Array.isArray(payload.data) ? payload.data : [];
        }).catch(error => {
            simcSpecOptionsPromise = null;
            throw error;
        });
    }
    const rows = await simcSpecOptionsPromise;
    const targets = [
        { select: document.getElementById('simc-sim-spec'), placeholder: '-- 请选择目标专精 --' },
        { select: document.getElementById('simc-sim-bnet-spec'), placeholder: '-- 选择专精加载当前赛季 Top10 --' },
        { select: document.getElementById('simc-wb-profile-spec-filter'), placeholder: '全部专精' },
        { select: document.getElementById('simc-profile-spec-filter'), placeholder: '全部专精' },
        { select: document.getElementById('simc-talent-string-spec-filter'), placeholder: '全部专精' },
        { select: document.querySelector('#simc-wb-profile-form-source select[name="spec"]'), placeholder: '请选择专精' },
        { select: document.querySelector('#simc-wb-mastery-form select[name="spec"]'), placeholder: '请选择专精' },
    ];
    targets.forEach(({ select, placeholder }) => {
        if (!select || select.dataset.specCatalogLoaded === '1') return;
        const current = select.value;
        select.replaceChildren();
        const placeholderOption = document.createElement('option');
        placeholderOption.value = '';
        placeholderOption.textContent = placeholder;
        select.appendChild(placeholderOption);
        rows.forEach(row => {
            const option = document.createElement('option');
            option.value = row.value;
            option.textContent = row.label || `${row.spec_label} · ${row.class_label}`;
            select.appendChild(option);
        });
        if (current && rows.some(row => row.value === current)) select.value = current;
        select.dataset.specCatalogLoaded = '1';
    });
    return rows;
}

window.loadSimcSpecOptions = loadSimcSpecOptions;

function bindSimcWorkbenchProfilesControls() {
    const profilePanel = document.getElementById('simc-workbench-profiles-panel');
    if (profilePanel && document.documentElement.dataset.simcProfileActionsBound !== '1') {
        document.documentElement.dataset.simcProfileActionsBound = '1';
        document.addEventListener('click', event => {
            const formActionButton = event.target.closest('[data-profile-form-action]');
            if (formActionButton) {
                const formAction = formActionButton.dataset.profileFormAction;
                if (formAction === 'create') simcWbToggleProfileForm('create');
                if (formAction === 'close') simcWbCloseProfileForm();
                if (formAction === 'save') simcWbSaveProfile();
                return;
            }
            const sortButton = event.target.closest('[data-profile-sort]');
            if (sortButton) {
                const key = sortButton.dataset.profileSort || '';
                simcWbProfileSort = {
                    key,
                    direction: simcWbProfileSort.key === key && simcWbProfileSort.direction === 'asc' ? 'desc' : 'asc',
                };
                updateSimcProfileSortHeaders();
                loadSimcWorkbenchProfiles(1);
                return;
            }
            const rowActionButton = event.target.closest('[data-profile-row-action]');
            if (!rowActionButton) return;
            const rowAction = rowActionButton.dataset.profileRowAction;
            const profileId = rowActionButton.dataset.profileId;
            if (rowAction === 'simulate') {
                window.startSimcSimulationFromResource({ profileId, spec: rowActionButton.dataset.profileSpec }).catch(error => showMessage(String(error.message || error), 'error'));
            }
            if (rowAction === 'view') simcWbViewProfile(profileId);
            if (rowAction === 'copy') simcWbCopyProfile(profileId);
            if (rowAction === 'edit') simcWbEditProfile(profileId);
            if (rowAction === 'delete') simcWbDeleteProfile(profileId, rowActionButton);

        });
        document.addEventListener('change', event => {
            if (event.target.matches('#simc-wb-profile-form select[name="player_config_mode"]')) {
                simcWbSyncProfileFormMode();
            }
        });
    }
    /* 专精筛选选项由后端统一资源提供，显示中文职业与专精名。 */
    const specSel = document.getElementById('simc-wb-profile-spec-filter');
    if (specSel) loadSimcSpecOptions().then(() => {
        specSel.value = simcWbProfileSpecFilter;
    }).catch(error => console.warn('加载配置管理专精筛选失败:', error));
    if (specSel && specSel.dataset.bound !== '1') {
        specSel.dataset.bound = '1';
        specSel.addEventListener('change', function() {
            simcWbProfileSpecFilter = this.value;
            loadSimcWorkbenchProfiles(1);
        });
    }
    const importSaveBtn = document.getElementById('simc-sim-save-profile-btn');
    if (importSaveBtn && importSaveBtn.dataset.bound !== '1') {
        importSaveBtn.dataset.bound = '1';
        importSaveBtn.addEventListener('click', () => simcWbSaveCurrentSimulatorProfile());
    }
    const profileModeSel = document.querySelector('#simc-wb-profile-form select[name="player_config_mode"]');
    if (profileModeSel && profileModeSel.dataset.bound !== '1') {
        profileModeSel.dataset.bound = '1';
        profileModeSel.addEventListener('change', simcWbSyncProfileFormMode);
    }
    const refreshBtn = document.getElementById('simc-wb-profile-refresh');
    if (refreshBtn && refreshBtn.dataset.bound !== '1') {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', () => loadSimcWorkbenchProfiles(simcWbProfilePage));
    }
}

/* ===== SimC 工具台 — 绿字规则（rules） ===== */
let simcWbRulesPage = 1;
let simcWbRulesTotalPages = 1;
let simcWbMasteryPage = 1;
let simcWbMasteryTotalPages = 1;
let simcWbMasteryFormMode = 'create';
let simcWbMasteryFormEditId = null;

function loadSimcWorkbenchRules(page) {
    page = page || 1;
    simcWbRulesPage = page;
    const tbody = document.getElementById('simc-wb-rules-list');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" class="text-center py-6 text-gray-400"><i class="fas fa-spinner fa-spin mr-2"></i>加载中…</td></tr>';

    fetch('/api/simc-workbench/secondary-rules/', {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' }
    }).then(r => r.json()).then(data => {
        if (!data.success) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center py-6 text-red-500">加载失败</td></tr>';
            return;
        }
        const rows = Array.isArray(data.data) ? data.data : [];
        const canWrite = data.can_write || false;

        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="5" class="text-center py-6 text-gray-400">暂无绿字规则</td></tr>';
            return;
        }

        tbody.innerHTML = rows.map((row, idx) => {
            const id = row.id || 0;
            const className = row.class_name || '-';
            const critPerPct = row.crit_per_percent != null ? row.crit_per_percent : '-';
            const hastePerPct = row.haste_per_percent != null ? row.haste_per_percent : '-';
            const masteryPerPct = row.mastery_per_percent != null ? row.mastery_per_percent : '-';
            const versaPerPct = row.versatility_per_percent != null ? row.versatility_per_percent : '-';
            const offset = idx + 1;
            const actions = canWrite
                ? `<button type="button" data-rule-action="edit" data-rule-id="${id}" class="text-blue-600 hover:text-blue-800 mr-2">编辑</button>
                   <button type="button" data-rule-action="delete" data-rule-id="${id}" class="text-red-600 hover:text-red-800">删除</button>`
                : '<span class="text-xs text-gray-400">只读</span>';
            return `<tr class="hover:bg-gray-50 border-b border-gray-100">
                <td class="px-3 py-2.5 text-center text-gray-500 text-xs">${offset}</td>
                <td class="px-3 py-2.5"><span class="inline-flex px-2 py-1 text-xs font-semibold rounded-full bg-blue-100 text-blue-700">${escapeHtml(className)}</span></td>
                <td class="px-3 py-2.5 text-center text-xs font-mono text-gray-700" title="急速 ${hastePerPct} / 暴击 ${critPerPct} / 精通 ${masteryPerPct} / 全能 ${versaPerPct}">
                    暴击 ${critPerPct} &nbsp;|&nbsp; 急速 ${hastePerPct} &nbsp;|&nbsp; 精通 ${masteryPerPct} &nbsp;|&nbsp; 全能 ${versaPerPct}
                </td>
                <td class="px-3 py-2.5 text-center text-xs text-gray-500 font-mono">按职业统一</td>
                <td class="px-3 py-2.5 text-center text-xs">${actions}</td>
            </tr>`;
        }).join('');

        const addBtn = document.querySelector('[data-simc-inline-create="secondary-rules"]');
        if (addBtn) {
            if (canWrite) {
                addBtn.classList.remove('hidden');
            } else {
                addBtn.classList.add('hidden');
            }
        }
    }).catch(err => {
        if (tbody) tbody.innerHTML = '<tr><td colspan="5" class="text-center py-6 text-red-500">网络错误</td></tr>';
    });
}

function bindSimcWorkbenchRulesControls() {
    const refreshBtn = document.getElementById('simc-wb-rules-refresh');
    if (refreshBtn && refreshBtn.dataset.bound !== '1') {
        refreshBtn.dataset.bound = '1';
        refreshBtn.addEventListener('click', () => loadSimcWorkbenchRules(simcWbRulesPage));
    }
    const masteryRefreshBtn = document.getElementById('simc-wb-mastery-refresh');
    if (masteryRefreshBtn && masteryRefreshBtn.dataset.bound !== '1') {
        masteryRefreshBtn.dataset.bound = '1';
        masteryRefreshBtn.addEventListener('click', () => loadSimcWorkbenchMastery(simcWbMasteryPage));
    }

    const ruleCreateBtn = document.querySelector('[data-simc-inline-create="secondary-rules"]');
    if (ruleCreateBtn && ruleCreateBtn.dataset.bound !== '1') {
        ruleCreateBtn.dataset.bound = '1';
        ruleCreateBtn.addEventListener('click', () => simcWbToggleRuleForm('create'));
    }
    const masteryCreateBtn = document.querySelector('[data-simc-inline-create="mastery-rules"]');
    if (masteryCreateBtn && masteryCreateBtn.dataset.bound !== '1') {
        masteryCreateBtn.dataset.bound = '1';
        masteryCreateBtn.addEventListener('click', () => simcWbToggleMasteryForm('create'));
    }

    document.addEventListener('click', function(e) {
        const ruleSubtab = e.target.closest('[data-rule-subtab]');
        if (ruleSubtab) {
            e.preventDefault();
            switchRuleSubtab(ruleSubtab.dataset.ruleSubtab);
            return;
        }
        const ruleAction = e.target.closest('[data-rule-action]');
        if (ruleAction) {
            e.preventDefault();
            const action = ruleAction.dataset.ruleAction;
            const id = ruleAction.dataset.ruleId;
            if (action === 'edit' && id) simcWbEditRule(id);
            else if (action === 'delete' && id) simcWbDeleteRule(id, ruleAction);
            return;
        }
        const masteryAction = e.target.closest('[data-mastery-action]');
        if (masteryAction) {
            e.preventDefault();
            const action = masteryAction.dataset.masteryAction;
            const id = masteryAction.dataset.masteryId;
            if (action === 'edit' && id) simcWbEditMastery(id);
            else if (action === 'delete' && id) simcWbDeleteMastery(id, masteryAction);
            return;
        }
        const ruleFormAction = e.target.closest('[data-rule-form-action]');
        if (ruleFormAction) {
            e.preventDefault();
            const action = ruleFormAction.dataset.ruleFormAction;
            if (action === 'save') simcWbSaveRule();
            else if (action === 'cancel' || action === 'close') simcWbCloseRuleForm();
            return;
        }
        const masteryFormAction = e.target.closest('[data-mastery-form-action]');
        if (masteryFormAction) {
            e.preventDefault();
            const action = masteryFormAction.dataset.masteryFormAction;
            if (action === 'save') simcWbSaveMastery();
            else if (action === 'cancel' || action === 'close') simcWbCloseMasteryForm();
            return;
        }
    }, { capture: true });
}

function loadSimcWorkbenchMastery(page) {
    page = page || 1;
    simcWbMasteryPage = page;
    const tbody = document.getElementById('simc-wb-mastery-list');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="4" class="text-center py-6 text-gray-400"><i class="fas fa-spinner fa-spin mr-2"></i>加载中…</td></tr>';
    fetch('/api/simc-workbench/mastery-rules/', {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' }
    }).then(r => r.json()).then(data => {
        if (!data.success) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center py-6 text-red-500">加载失败</td></tr>';
            return;
        }
        const rows = Array.isArray(data.data) ? data.data : [];
        const canWrite = data.can_write || false;

        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center py-6 text-gray-400">暂无精通系数</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map((row, idx) => {
            const offset = idx + 1;
            const actions = canWrite
                ? `<button type="button" data-mastery-action="edit" data-mastery-id="${row.id}" class="text-blue-600 hover:text-blue-800 mr-2">编辑</button>
                   <button type="button" data-mastery-action="delete" data-mastery-id="${row.id}" class="text-red-600 hover:text-red-800">删除</button>`
                : '<span class="text-xs text-gray-400">只读</span>';
            return `<tr class="hover:bg-gray-50 border-b border-gray-100">
                <td class="px-3 py-2.5 text-center text-gray-500 text-xs">${offset}</td>
                <td class="px-3 py-2.5">${renderSpecBadgeHtml(row.spec || '')}</td>
                <td class="px-3 py-2.5 text-center text-xs font-mono text-gray-700">${row.mastery_coefficient != null ? row.mastery_coefficient : '-'}</td>
                <td class="px-3 py-2.5 text-center text-xs">${actions}</td>
            </tr>`;
        }).join('');

        const addBtn = document.querySelector('[data-simc-inline-create="mastery-rules"]');
        if (addBtn) {
            if (canWrite) {
                addBtn.classList.remove('hidden');
            } else {
                addBtn.classList.add('hidden');
            }
        }
    }).catch(err => {
        if (tbody) tbody.innerHTML = '<tr><td colspan="4" class="text-center py-6 text-red-500">网络错误</td></tr>';
    });
}

/* ===== 工具台通用分页渲染 ===== */
function renderSimcTaskContextHtml(task, extPayload) {
    const ext = extPayload || {};
    const chips = [];
    const mode = ext.player_config_mode || ext.player_import_mode || '';
    if (mode === 'battlenet') {
        const armory = [ext.battlenet_region, ext.battlenet_realm, ext.battlenet_character].filter(Boolean).join('/');
        chips.push('Battle.net' + (armory ? ': ' + armory : ''));
    } else if (mode === 'manual_equipment' || mode === 'equipment') {
        const lines = String(ext.player_equipment || '').split('\n').filter(Boolean).length;
        chips.push('手动装备配置' + (lines ? ': ' + lines + ' 行' : ''));
    } else if (ext.raw_simc_code) {
        chips.push('直接 SimC 代码');
    }
    if (ext.fight_style) chips.push('场景: ' + ext.fight_style);
    if (ext.time || ext.target_count) chips.push('时长/目标: ' + (ext.time || '-') + 's / ' + (ext.target_count || '-'));
    if (ext.selected_apl_id) chips.push('APL #' + ext.selected_apl_id);
    if (!chips.length) return '';
    return `<div class="mt-1 flex flex-wrap gap-1">${chips.map(chip => `<span class="inline-flex px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[11px]">${escapeHtml(chip)}</span>`).join('')}</div>`;
}

function renderSimcTaskContextDetailHtml(task, extPayload) {
    const ext = extPayload || {};
    const rows = [];
    const add = (label, value) => { if (value !== undefined && value !== null && String(value) !== '') rows.push([label, String(value)]); };
    add('职业/专精', [getSimcSpecClass(ext.spec || task.simc_profile_spec || ''), ext.spec || task.simc_profile_spec || ''].filter(Boolean).join(' / '));
    add('保存配置', task.simc_profile_name || ext.profile_name || (ext.raw_simc_code ? '直接 SimC 代码' : ''));
    const mode = ext.player_config_mode || ext.player_import_mode || '';
    if (mode === 'battlenet') add('导入来源', ['Battle.net', ext.battlenet_region, ext.battlenet_realm, ext.battlenet_character].filter(Boolean).join(' / '));
    else if (mode === 'manual_equipment' || mode === 'equipment') add('导入来源', '手动装备配置');
    else if (ext.raw_simc_code) add('导入来源', '直接 SimC 代码');
    add('战斗场景', ext.fight_style);
    if (ext.time || ext.target_count) add('时长/目标', `${ext.time || '-'}s / ${ext.target_count || '-'}`);
    add('APL', ext.selected_apl_id ? `#${ext.selected_apl_id}` : '');
    if (ext.selected_attributes) add('属性模拟项', Array.isArray(ext.selected_attributes) ? ext.selected_attributes.join(', ') : ext.selected_attributes);
    add('属性步进', ext.attribute_step);
    if (!rows.length) return '<span class="text-gray-400">暂无执行上下文</span>';
    return `<dl class="grid grid-cols-1 md:grid-cols-2 gap-2">${rows.map(([k,v]) => `<div><dt class="text-xs text-gray-500">${escapeHtml(k)}</dt><dd class="font-medium text-gray-800 break-all">${escapeHtml(v)}</dd></div>`).join('')}</dl>`;
}

function renderSimcWbPagination(containerId, currentPage, totalPages, loadFn) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (totalPages <= 1) { el.innerHTML = ''; return; }

    let html = '<div class="flex items-center justify-center gap-1">';
    if (currentPage > 1) {
        html += `<button class="simc-wb-page-btn px-2.5 py-1 text-xs rounded bg-white border border-gray-300 hover:bg-gray-50" data-page="${currentPage - 1}">‹ 上一页</button>`;
    }
    const maxVis = 5;
    let start = Math.max(1, currentPage - Math.floor(maxVis / 2));
    let end = Math.min(totalPages, start + maxVis - 1);
    if (end - start + 1 < maxVis) start = Math.max(1, end - maxVis + 1);
    if (start > 1) {
        html += `<button class="simc-wb-page-btn px-2.5 py-1 text-xs rounded bg-white border border-gray-300 hover:bg-gray-50" data-page="1">1</button>`;
        if (start > 2) html += '<span class="px-1.5 text-gray-400 text-xs">…</span>';
    }
    for (let i = start; i <= end; i++) {
        const active = i === currentPage ? 'bg-blue-500 text-white border-blue-500' : 'bg-white border-gray-300 hover:bg-gray-50';
        html += `<button class="simc-wb-page-btn px-2.5 py-1 text-xs rounded border ${active}" data-page="${i}">${i}</button>`;
    }
    if (end < totalPages) {
        if (end < totalPages - 1) html += '<span class="px-1.5 text-gray-400 text-xs">…</span>';
        html += `<button class="simc-wb-page-btn px-2.5 py-1 text-xs rounded bg-white border border-gray-300 hover:bg-gray-50" data-page="${totalPages}">${totalPages}</button>`;
    }
    if (currentPage < totalPages) {
        html += `<button class="simc-wb-page-btn px-2.5 py-1 text-xs rounded bg-white border border-gray-300 hover:bg-gray-50" data-page="${currentPage + 1}">下一页 ›</button>`;
    }
    html += '</div>';
    el.innerHTML = html;
    el.querySelectorAll('.simc-wb-page-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const p = parseInt(btn.dataset.page, 10);
            if (p && typeof loadFn === 'function') loadFn(p);
        });
    });
}

/* ===== SimC 工具台 — 内联 CRUD ===== */
let simcWbProfileFormMode = 'create'; // 'create' | 'edit'
let simcWbProfileFormEditId = null;
let simcWbRuleFormMode = 'create';
let simcWbRuleFormEditId = null;

/* --- Profile CRUD --- */
function getSimcProfileMode(profileData) {
    const mode = String(
        profileData?.player_config_mode || profileData?.player_import_mode || ''
    ).trim();
    if (mode === 'equipment') return 'manual_equipment';
    if (['battlenet', 'manual_equipment', 'attribute_only'].includes(mode)) return mode;
    if (profileData?.battlenet_character) return 'battlenet';
    if (profileData?.player_equipment) return 'manual_equipment';
    return 'attribute_only';
}

function simcProfileFormCanonicalSpec(spec) {
    const key = String(spec || '').trim().toLowerCase();
    const disambiguated = {
        protection_warrior: 'warrior_protection',
        frost_death_knight: 'deathknight_frost',
        frost_mage: 'mage_frost',
        restoration_druid: 'druid_restoration',
        restoration_shaman: 'shaman_restoration',
        holy_priest: 'priest_holy',
        holy_paladin: 'paladin_holy',
        protection_paladin: 'paladin_protection',
    };
    if (disambiguated[key]) return disambiguated[key];
    const normalizedSpec = normalizeSimcSpecKey(key);
    const className = getSimcSpecClass(normalizedSpec).replaceAll('_', '');
    const specName = normalizedSpec === 'frost_dk' ? 'frost' : normalizedSpec;
    return className && specName ? `${className}_${specName}` : '';
}

function updateSimcProfileTalentSimulatorLink(formWrap = document.getElementById('simc-wb-profile-form')) {
    if (!formWrap) return;
    const input = formWrap.querySelector('input[name="talent"]');
    const specSelect = formWrap.querySelector('select[name="spec"]');
    const ptrInput = formWrap.querySelector('input[name="use_ptr"]');
    const link = formWrap.querySelector('[data-profile-talent-simulator-link]');
    if (!input || !specSelect || !ptrInput || !link) return;
    const usePtr = ptrInput.checked;
    const versionKey = simcProfileTalentVersions[usePtr ? 'ptr' : 'retail'] || '';
    const url = simcTalentSimulatorUrl(
        input.value,
        simcProfileFormCanonicalSpec(specSelect.value),
        versionKey,
    );
    if (!url) {
        link.removeAttribute('href');
        link.setAttribute('aria-disabled', 'true');
        link.classList.add('cursor-not-allowed', 'text-violet-300');
        link.classList.remove('text-violet-700', 'hover:border-violet-400', 'hover:bg-violet-100');
        return;
    }
    link.href = url;
    link.setAttribute('aria-disabled', 'false');
    link.classList.remove('cursor-not-allowed', 'text-violet-300');
    link.classList.add('text-violet-700', 'hover:border-violet-400', 'hover:bg-violet-100');
}

async function simcWbToggleProfileForm(mode, profileData) {
    try {
        await loadSimcSpecOptions();
    } catch (error) {
        showMessage(String(error.message || error), 'error');
        return;
    }
    openSimcWorkbenchDialog('profile-form', { mode, profileData });
    const body = document.getElementById('simc-dialog-body');
    if (!body) return;

    const formWrap = document.getElementById('simc-wb-profile-form-source');
    if (!formWrap) return;

    simcWbProfileFormMode = mode;
    if (mode === 'create') {
        simcWbProfileFormEditId = null;
        formWrap.querySelector('.simc-wb-form-title').textContent = '新增配置';
        formWrap.querySelector('input[name="name"]').value = '';
        formWrap.querySelector('select[name="spec"]').value = '';
        formWrap.querySelector('select[name="player_config_mode"]').value = 'battlenet';
        formWrap.querySelector('input[name="use_ptr"]').checked = false;
        formWrap.querySelector('input[name="battlenet_region"]').value = 'eu';
        formWrap.querySelector('input[name="battlenet_realm"]').value = '';
        formWrap.querySelector('input[name="battlenet_character"]').value = '';
        formWrap.querySelector('textarea[name="player_equipment"]').value = '';
        formWrap.querySelector('input[name="talent"]').value = '';
        formWrap.querySelector('input[name="gear_strength"]').value = '';
        formWrap.querySelector('input[name="gear_crit"]').value = '';
        formWrap.querySelector('input[name="gear_haste"]').value = '';
        formWrap.querySelector('input[name="gear_mastery"]').value = '';
        formWrap.querySelector('input[name="gear_versatility"]').value = '';
    } else {
        simcWbProfileFormEditId = profileData.id;
        formWrap.querySelector('.simc-wb-form-title').textContent = '编辑配置 #' + profileData.id;
        formWrap.querySelector('input[name="name"]').value = profileData.name || '';
        const specSel = formWrap.querySelector('select[name="spec"]');
        const profileSpec = String(profileData.canonical_spec || simcProfileFormCanonicalSpec(profileData.spec || '')).trim().toLowerCase();
        specSel.value = profileSpec;
        if (profileSpec && specSel.value !== profileSpec) {
            const option = document.createElement('option');
            option.value = profileSpec;
            option.textContent = profileSpec;
            specSel.appendChild(option);
            specSel.value = profileSpec;
        }
        const profileMode = getSimcProfileMode(profileData);
        formWrap.querySelector('select[name="player_config_mode"]').value = profileMode;
        formWrap.querySelector('input[name="use_ptr"]').checked = profileData.use_ptr === true;
        formWrap.querySelector('input[name="battlenet_region"]').value = profileData.battlenet_region || '';
        formWrap.querySelector('input[name="battlenet_realm"]').value = profileData.battlenet_realm || '';
        formWrap.querySelector('input[name="battlenet_character"]').value = profileData.battlenet_character || '';
        formWrap.querySelector('textarea[name="player_equipment"]').value = profileData.player_equipment || '';
        formWrap.querySelector('input[name="talent"]').value = profileData.talent || '';
        formWrap.querySelector('input[name="gear_strength"]').value = profileData.gear_strength ?? '';
        formWrap.querySelector('input[name="gear_crit"]').value = profileData.gear_crit ?? '';
        formWrap.querySelector('input[name="gear_haste"]').value = profileData.gear_haste ?? '';
        formWrap.querySelector('input[name="gear_mastery"]').value = profileData.gear_mastery ?? '';
        formWrap.querySelector('input[name="gear_versatility"]').value = profileData.gear_versatility ?? '';
    }

    body.innerHTML = '';
    const dialogForm = formWrap.cloneNode(true);
    dialogForm.id = 'simc-wb-profile-form';
    const sourceSelects = formWrap.querySelectorAll('select');
    const clonedSelects = dialogForm.querySelectorAll('select');
    sourceSelects.forEach((sourceSelect, index) => {
        const clonedSelect = clonedSelects[index];
        if (clonedSelect) clonedSelect.value = sourceSelect.value;
    });
    body.appendChild(dialogForm);
    const clonedForm = body.querySelector('#simc-wb-profile-form');
    if (clonedForm) {
        clonedForm.classList.remove('hidden');
        simcWbSyncProfileFormMode();
        const talentInput = clonedForm.querySelector('input[name="talent"]');
        const specSelect = clonedForm.querySelector('select[name="spec"]');
        const ptrInput = clonedForm.querySelector('input[name="use_ptr"]');
        talentInput?.addEventListener('input', () => updateSimcProfileTalentSimulatorLink(clonedForm));
        specSelect?.addEventListener('change', () => updateSimcProfileTalentSimulatorLink(clonedForm));
        ptrInput?.addEventListener('change', () => updateSimcProfileTalentSimulatorLink(clonedForm));
        updateSimcProfileTalentSimulatorLink(clonedForm);
    }
}
function simcWbCloseProfileForm() {
    closeSimcWorkbenchDialog();
    simcWbProfileFormEditId = null;
}
function simcWbSyncProfileFormMode() {
    const formWrap = document.getElementById('simc-wb-profile-form');
    if (!formWrap) return;
    const mode = formWrap.querySelector('select[name="player_config_mode"]')?.value || 'battlenet';
    formWrap.querySelectorAll('[data-profile-mode-section]').forEach(el => {
        el.classList.toggle('hidden', el.getAttribute('data-profile-mode-section') !== mode);
    });
}
async function simcWbSaveProfile() {
    const formWrap = document.getElementById('simc-wb-profile-form');
    if (!formWrap) return;
    const gv = n => formWrap.querySelector('[name="' + n + '"]').value.trim();
    const payload = {
        name: gv('name'),
        spec: gv('spec'),
        player_config_mode: gv('player_config_mode'),
        player_import_mode: gv('player_config_mode'),
        use_ptr: formWrap.querySelector('[name="use_ptr"]')?.checked === true,
        battlenet_region: gv('battlenet_region'),
        battlenet_realm: gv('battlenet_realm'),
        battlenet_character: gv('battlenet_character'),
        player_equipment: gv('player_equipment'),
        talent: gv('talent'),
        gear_strength: gv('gear_strength') === '' ? null : parseInt(gv('gear_strength')),
        gear_crit: gv('gear_crit') === '' ? null : parseInt(gv('gear_crit')),
        gear_haste: gv('gear_haste') === '' ? null : parseInt(gv('gear_haste')),
        gear_mastery: gv('gear_mastery') === '' ? null : parseInt(gv('gear_mastery')),
        gear_versatility: gv('gear_versatility') === '' ? null : parseInt(gv('gear_versatility')),
    };
    if (!payload.name) { showMessage('请输入配置名称', 'error'); return; }
    if (!payload.spec) { showMessage('请输入专精', 'error'); return; }
    const csrf = getCSRFToken();
    const btn = formWrap.querySelector('.simc-wb-form-save');
    const oldHtml = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>保存中…';
    try {
        let resp;
        if (simcWbProfileFormMode === 'edit' && simcWbProfileFormEditId) {
            payload.id = simcWbProfileFormEditId;
            resp = await fetch('/api/simc-profile/', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(payload)
            });
        } else {
            resp = await fetch('/api/simc-profile/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(payload)
            });
        }
        const data = await resp.json();
        if (data.success) {
            showMessage(simcWbProfileFormMode === 'edit' ? '配置已更新' : '配置已创建', 'success');
            simcWbCloseProfileForm();
            loadSimcWorkbenchProfiles(simcWbProfilePage);
        } else {
            showMessage('保存失败: ' + (data.message || data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('保存失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false; btn.innerHTML = oldHtml;
    }
}

async function simcWbDeleteProfile(id, trigger) {
    if (trigger && trigger.dataset.deleteConfirmed !== '1') {
        trigger.dataset.deleteConfirmed = '1';
        trigger.title = '再次点击确认删除';
        trigger.innerHTML = '<span class="font-semibold">确认删除</span>';
        setTimeout(() => {
            if (!trigger.isConnected) return;
            delete trigger.dataset.deleteConfirmed;
            trigger.title = '删除';
            trigger.innerHTML = '<i class="fas fa-trash-alt"></i>';
        }, 5000);
        return;
    }
    try {
        const resp = await fetch('/api/simc-profile/', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ id: Number(id) })
        });
        const data = await resp.json();
        if (data.success) {
            showMessage('配置已永久删除', 'success');
            loadSimcWorkbenchProfiles(simcWbProfilePage);
        } else {
            showMessage('删除失败: ' + (data.message || data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('删除失败: ' + e.message, 'error');
    }
}
async function simcWbEditProfile(id) {
    try {
        const headers = { 'X-CSRFToken': getCSRFToken() };
        const [resp, detailResp] = await Promise.all([
            fetch(`/api/simc-profile/${id}/`, { method: 'GET', headers }),
            fetch(`/api/simc-player-config-detail/?profile_id=${encodeURIComponent(id)}`, { headers }),
        ]);
        const [data, detailPayload] = await Promise.all([resp.json(), detailResp.json()]);
        if (resp.ok && data.success) {
            simcProfileTalentVersions = {
                retail: String(data.talent_versions?.retail || simcProfileTalentVersions.retail || ''),
                ptr: String(data.talent_versions?.ptr || simcProfileTalentVersions.ptr || ''),
            };
            await simcWbToggleProfileForm('edit', data);
            if (detailResp.ok && detailPayload.success) {
                renderSimcProfileFormEquipmentPreview(detailPayload.data || {});
            }
        } else {
            showMessage('未找到配置', 'error');
        }
    } catch (e) { showMessage('加载配置失败: ' + e.message, 'error'); }
}

async function simcWbSaveCurrentSimulatorProfile() {
    const spec = (document.getElementById('simc-sim-spec')?.value || '').trim();
    if (!spec) { showMessage('请先完成玩家来源预检', 'error'); return; }
    let source;
    try {
        source = collectSimcPlayerSource();
    } catch (error) {
        showMessage(String(error.message || error), 'error');
        return;
    }
    let mode;
    if (source.type === 'battlenet') mode = 'battlenet';
    else if (source.type === 'simc_addon') mode = 'manual_equipment';
    else {
        showMessage('当前来源已经是已保存配置，无需重复导入', 'info');
        return;
    }
    const payload = {
        name: spec + '-' + (mode === 'battlenet' ? source.character : 'manual'),
        spec,
        player_config_mode: mode,
        player_import_mode: mode,
        battlenet_region: mode === 'battlenet' ? source.region : '',
        battlenet_realm: mode === 'battlenet' ? source.realm : '',
        battlenet_character: mode === 'battlenet' ? source.character : '',
        player_equipment: mode === 'manual_equipment' ? source.simc_code : '',
        talent: '',
        gear_strength: null,
        gear_crit: null,
        gear_haste: null,
        gear_mastery: null,
        gear_versatility: null,
    };
    switchSimcWorkbenchL1Tab('workflow', 'profiles');
    simcWbToggleProfileForm('create');
    const formWrap = document.getElementById('simc-wb-profile-form');
    if (!formWrap) return;
    Object.entries(payload).forEach(([key, value]) => {
        const field = formWrap.querySelector('[name="' + key + '"]');
        if (field) field.value = value == null ? '' : value;
    });
    simcWbSyncProfileFormMode();
    formWrap.querySelector('input[name="name"]')?.focus();
    showMessage('请确认配置名称和内容，可按需填写最终天赋/属性覆盖后保存', 'info');
}

/* --- Rule CRUD --- */
function simcWbToggleRuleForm(mode, ruleData) {
    const formWrap = document.getElementById('simc-wb-rule-form');
    if (!formWrap) return;
    simcWbRuleFormMode = mode;
    if (mode === 'create') {
        simcWbRuleFormEditId = null;
        formWrap.querySelector('.simc-wb-form-title').textContent = '新增绿字规则';
        formWrap.querySelector('select[name="class_name"]').value = 'warrior';
        formWrap.querySelector('input[name="crit_per_percent"]').value = '46';
        formWrap.querySelector('input[name="haste_per_percent"]').value = '44';
        formWrap.querySelector('input[name="mastery_per_percent"]').value = '46';
        formWrap.querySelector('input[name="versatility_per_percent"]').value = '54';
    } else {
        simcWbRuleFormEditId = ruleData.id;
        formWrap.querySelector('.simc-wb-form-title').textContent = '编辑绿字规则 #' + ruleData.id;
        formWrap.querySelector('select[name="class_name"]').value = ruleData.class_name || 'warrior';
        formWrap.querySelector('input[name="crit_per_percent"]').value = ruleData.crit_per_percent || '';
        formWrap.querySelector('input[name="haste_per_percent"]').value = ruleData.haste_per_percent || '';
        formWrap.querySelector('input[name="mastery_per_percent"]').value = ruleData.mastery_per_percent || '';
        formWrap.querySelector('input[name="versatility_per_percent"]').value = ruleData.versatility_per_percent || '';
    }
    formWrap.classList.remove('hidden');
    formWrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function simcWbCloseRuleForm() {
    const f = document.getElementById('simc-wb-rule-form');
    if (f) f.classList.add('hidden');
    simcWbRuleFormEditId = null;
}
async function simcWbSaveRule() {
    const formWrap = document.getElementById('simc-wb-rule-form');
    if (!formWrap) return;
    const gv = n => formWrap.querySelector('[name="' + n + '"]').value.trim();
    const payload = {
        class_name: gv('class_name'),
        crit_per_percent: parseFloat(gv('crit_per_percent')) || 0,
        haste_per_percent: parseFloat(gv('haste_per_percent')) || 0,
        mastery_per_percent: parseFloat(gv('mastery_per_percent')) || 0,
        versatility_per_percent: parseFloat(gv('versatility_per_percent')) || 0,
    };
    if (!payload.class_name) { showMessage('请选择职业', 'error'); return; }
    const csrf = getCSRFToken();
    const btn = formWrap.querySelector('.simc-wb-form-save');
    const oldHtml = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>保存中…';
    try {
        let resp;
        if (simcWbRuleFormMode === 'edit' && simcWbRuleFormEditId) {
            resp = await fetch('/api/simc-workbench/secondary-rules/' + simcWbRuleFormEditId + '/', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(payload)
            });
        } else {
            resp = await fetch('/api/simc-workbench/secondary-rules/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
                body: JSON.stringify(payload)
            });
        }
        const data = await resp.json();
        if (data.success) {
            showMessage(simcWbRuleFormMode === 'edit' ? '规则已更新' : '规则已创建', 'success');
            simcWbCloseRuleForm();
            loadSimcWorkbenchRules(simcWbRulesPage);
        } else {
            showMessage('保存失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('保存失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false; btn.innerHTML = oldHtml;
    }
}
async function simcWbDeleteRule(id, trigger) {
    if (trigger && trigger.dataset.deleteConfirmed !== '1') {
        trigger.dataset.deleteConfirmed = '1';
        trigger.textContent = '再次点击确认删除';
        trigger.classList.add('font-semibold');
        setTimeout(() => {
            if (!trigger.isConnected) return;
            delete trigger.dataset.deleteConfirmed;
            trigger.textContent = '删除';
            trigger.classList.remove('font-semibold');
        }, 5000);
        return;
    }
    try {
        const resp = await fetch('/api/simc-workbench/secondary-rules/' + id + '/', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() }
        });
        const data = await resp.json();
        if (data.success) {
            showMessage('规则已删除', 'success');
            loadSimcWorkbenchRules(simcWbRulesPage);
        } else {
            showMessage('删除失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('删除失败: ' + e.message, 'error');
    }
}
async function simcWbEditRule(id) {
    try {
        const resp = await fetch('/api/simc-workbench/secondary-rules/' + id + '/', {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await resp.json();
        if (data.success && data.data) {
            simcWbToggleRuleForm('edit', data.data);
        } else {
            showMessage('未找到规则', 'error');
        }
    } catch (e) {
        showMessage('加载规则失败', 'error');
    }
}


async function simcWbToggleMasteryForm(mode, data) {
    const formWrap = document.getElementById('simc-wb-mastery-form');
    if (!formWrap) return;
    let specOptions;
    try {
        specOptions = await loadSimcSpecOptions();
    } catch (error) {
        showMessage(String(error.message || error), 'error');
        return;
    }
    simcWbMasteryFormMode = mode;
    const specSelect = formWrap.querySelector('select[name="spec"]');
    if (mode === 'create') {
        simcWbMasteryFormEditId = null;
        formWrap.querySelector('.simc-wb-form-title').textContent = '新增精通系数';
        specSelect.value = '';
        formWrap.querySelector('input[name="mastery_coefficient"]').value = '';
    } else {
        simcWbMasteryFormEditId = data.id;
        formWrap.querySelector('.simc-wb-form-title').textContent = '编辑精通系数 #' + data.id;
        specSelect.value = specOptions.some(row => row.value === data.spec) ? data.spec : '';
        formWrap.querySelector('input[name="mastery_coefficient"]').value = data.mastery_coefficient != null ? data.mastery_coefficient : '';
    }
    formWrap.classList.remove('hidden');
    formWrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}
function simcWbCloseMasteryForm() {
    const f = document.getElementById('simc-wb-mastery-form');
    if (f) f.classList.add('hidden');
    simcWbMasteryFormEditId = null;
}
async function simcWbSaveMastery() {
    const formWrap = document.getElementById('simc-wb-mastery-form');
    if (!formWrap) return;
    const spec = formWrap.querySelector('select[name="spec"]').value.trim();
    const mastery = parseFloat(formWrap.querySelector('input[name="mastery_coefficient"]').value.trim());
    if (!spec) { showMessage('请输入专精', 'error'); return; }
    if (!Number.isFinite(mastery)) { showMessage('请输入合法精通系数', 'error'); return; }
    const payload = { spec: spec, mastery_coefficient: mastery };
    const btn = formWrap.querySelector('.simc-wb-form-save');
    const oldHtml = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>保存中…';
    try {
        let resp;
        if (simcWbMasteryFormMode === 'edit' && simcWbMasteryFormEditId) {
            resp = await fetch('/api/simc-workbench/mastery-rules/' + simcWbMasteryFormEditId + '/', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                body: JSON.stringify(payload)
            });
        } else {
            resp = await fetch('/api/simc-workbench/mastery-rules/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                body: JSON.stringify(payload)
            });
        }
        const data = await resp.json();
        if (data.success) {
            showMessage(simcWbMasteryFormMode === 'edit' ? '精通系数已更新' : '精通系数已创建', 'success');
            simcWbCloseMasteryForm();
            loadSimcWorkbenchMastery(simcWbMasteryPage);
        } else {
            showMessage('保存失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('保存失败: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = oldHtml;
    }
}
async function simcWbEditMastery(id) {
    try {
        const resp = await fetch('/api/simc-workbench/mastery-rules/' + id + '/', {
            method: 'GET',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await resp.json();
        if (data.success && data.data) {
            simcWbToggleMasteryForm('edit', data.data);
        } else {
            showMessage('未找到精通系数', 'error');
        }
    } catch (e) {
        showMessage('加载精通系数失败', 'error');
    }
}
async function simcWbDeleteMastery(id, trigger) {
    if (trigger && trigger.dataset.deleteConfirmed !== '1') {
        trigger.dataset.deleteConfirmed = '1';
        trigger.textContent = '再次点击确认删除';
        trigger.classList.add('font-semibold');
        setTimeout(() => {
            if (!trigger.isConnected) return;
            delete trigger.dataset.deleteConfirmed;
            trigger.textContent = '删除';
            trigger.classList.remove('font-semibold');
        }, 5000);
        return;
    }
    try {
        const resp = await fetch('/api/simc-workbench/mastery-rules/' + id + '/', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() }
        });
        const data = await resp.json();
        if (data.success) {
            showMessage('精通系数已删除', 'success');
            loadSimcWorkbenchMastery(simcWbMasteryPage);
        } else {
            showMessage('删除失败: ' + (data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('删除失败: ' + e.message, 'error');
    }
}

function renderSimcArtifactFrame(previewUrl, title) {
    const safeUrl = String(previewUrl || '');
    if (!/^\/api\/simc-workbench\/(artifacts\/\d+\/preview\/|tasks\/\d+\/report-preview\/)$/.test(safeUrl)) return '';
    return `<iframe class="w-full min-h-[70vh] border-0 rounded-xl bg-white" sandbox="" referrerpolicy="no-referrer" src="${escapeHtml(safeUrl)}" title="${escapeHtml(title || 'SimC 结果预览')}"></iframe>`;
}

function openSimcWorkbench() {
    const item = document.querySelector('.nav-item[data-section="simc-workbench"]');
    if (item) {
        item.click();
    } else {
        const section = document.getElementById('simc-workbench');
        if (section) section.style.display = 'block';
    }
}

function openSimcTableShortcut(tableName) {
    if (!tableName) return;
    const tableItem = document.querySelector(`.submenu-item[data-table="${tableName}"]`);
    if (tableItem) {
        tableItem.click();
        return;
    }
    if (typeof loadTableData === 'function') {
        loadTableData(tableName);
    }
}

/* === 发起模拟 (新 SimC 模拟面板) === */

const SIMC_SPEC_CLASS_MAP = {
    arms: 'warrior', fury: 'warrior', protection: 'warrior',
    blood: 'death_knight', frost_dk: 'death_knight', unholy: 'death_knight',
    devourer: 'demon_hunter', havoc: 'demon_hunter', vengeance: 'demon_hunter',
    balance: 'druid', feral: 'druid', guardian: 'druid', restoration: 'druid',
    devastation: 'evoker', preservation: 'evoker', augmentation: 'evoker',
    beast_mastery: 'hunter', marksmanship: 'hunter', survival: 'hunter',
    arcane: 'mage', fire: 'mage', frost: 'mage',
    brewmaster: 'monk', mistweaver: 'monk', windwalker: 'monk',
    holy: 'priest', discipline: 'priest', shadow: 'priest', retribution: 'paladin',
    assassination: 'rogue', outlaw: 'rogue', subtlety: 'rogue',
    elemental: 'shaman', enhancement: 'shaman', restoration_shaman: 'shaman',
    affliction: 'warlock', demonology: 'warlock', destruction: 'warlock',
};

function normalizeSimcSpecKey(spec) {
    let key = String(spec || '').trim().toLowerCase();
    if (!key) return '';
    const aliases = {
        deathknight_frost: 'frost_dk', death_knight_frost: 'frost_dk', dk_frost: 'frost_dk',
        shaman_restoration: 'restoration_shaman', resto_shaman: 'restoration_shaman',
    };
    if (aliases[key]) return aliases[key];
    if (SIMC_SPEC_CLASS_MAP[key]) return key;
    const parts = key.split('_');
    for (let i = 1; i < parts.length; i += 1) {
        const suffix = parts.slice(i).join('_');
        if (SIMC_SPEC_CLASS_MAP[suffix]) return suffix;
    }
    return key;
}

function getSimcSpecClass(spec) {
    return SIMC_SPEC_CLASS_MAP[normalizeSimcSpecKey(spec)] || '';
}

let simcBattlenetTopPlayersAbortController = null;

function applySimcBattlenetTopPlayer() {
    const select = document.getElementById('simc-sim-bnet-top-player');
    const option = select?.selectedOptions?.[0];
    if (!option?.value) return;
    const region = document.getElementById('simc-sim-bnet-region');
    const realm = document.getElementById('simc-sim-bnet-realm');
    const character = document.getElementById('simc-sim-bnet-character');
    if (region) region.value = option.dataset.region || '';
    if (realm) realm.value = option.dataset.realm || '';
    if (character) character.value = option.dataset.character || '';
    resolveSimcPlayerSource().catch(error => {
        if (error.name !== 'AbortError') showMessage(String(error.message || error), 'error');
    });
}

function replaceSimcBattlenetTopPlayerOptions(select, rows, placeholder) {
    select.replaceChildren();
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = placeholder;
    select.appendChild(empty);
    rows.forEach(row => {
        const option = document.createElement('option');
        option.value = String(Number(row.id) || '');
        option.dataset.region = String(row.region || '');
        option.dataset.realm = String(row.realm || '');
        option.dataset.character = String(row.character || '');
        option.textContent = String(row.label || '');
        select.appendChild(option);
    });
}

async function loadSimcBattlenetTopPlayers() {
    const specSelect = document.getElementById('simc-sim-bnet-spec');
    const playerSelect = document.getElementById('simc-sim-bnet-top-player');
    const spec = String(specSelect?.value || '');
    simcBattlenetTopPlayersAbortController?.abort();
    if (!playerSelect) return;
    if (!spec) {
        playerSelect.disabled = true;
        replaceSimcBattlenetTopPlayerOptions(playerSelect, [], '先选择专精');
        return;
    }
    const controller = new AbortController();
    simcBattlenetTopPlayersAbortController = controller;
    playerSelect.disabled = true;
    replaceSimcBattlenetTopPlayerOptions(playerSelect, [], '加载中...');
    try {
        const response = await fetch(
            `/api/simc-battlenet-top-players/?spec=${encodeURIComponent(spec)}`,
            { signal: controller.signal },
        );
        const payload = await response.json();
        if (controller !== simcBattlenetTopPlayersAbortController || specSelect?.value !== spec) return;
        if (!response.ok || !payload.success || payload.spec !== spec) {
            throw new Error(payload.error || '加载 Top10 角色失败');
        }
        const rows = Array.isArray(payload.data) ? payload.data : [];
        replaceSimcBattlenetTopPlayerOptions(
            playerSelect,
            rows,
            rows.length ? '-- 选择 Top10 角色 --' : '当前赛季暂无该专精玩家',
        );
        playerSelect.disabled = rows.length === 0;
    } catch (error) {
        if (error.name === 'AbortError' || controller !== simcBattlenetTopPlayersAbortController) return;
        replaceSimcBattlenetTopPlayerOptions(playerSelect, [], '加载失败');
        showMessage(String(error.message || error), 'error');
    }
}

function switchSimcPlayerImportMode({ resolve = true } = {}) {
    const type = document.querySelector('input[name="simc-sim-player-source"]:checked')?.value || 'battlenet';
    document.getElementById('simc-sim-source-specified-spec')?.classList.toggle('hidden', type !== 'specified_spec');
    document.getElementById('simc-sim-source-battlenet')?.classList.toggle('hidden', type !== 'battlenet');
    document.getElementById('simc-sim-source-addon')?.classList.toggle('hidden', type !== 'simc_addon');
    if (type !== 'battlenet') renderSimcBattlenetLoadState('idle');
    if (!resolve) return;
    resolveSimcPlayerSource().catch(error => {
        if (error.name !== 'AbortError') showMessage(String(error.message || error), 'error');
    });
}

function fillSimcAttributeOnlyInputs() {
    // 兼容旧的配置加载调用；引用型工作流不回填任务正文。
}

function selectedSimcReferenceValue(selector) {
    const element = document.querySelector(selector);
    const value = Number.parseInt(String(element?.value || ''), 10);
    return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

async function startSimcSimulationFromResource({ profileId = 0, aplId = 0, spec = '' } = {}) {
    profileId = Number.parseInt(String(profileId), 10) || 0;
    aplId = Number.parseInt(String(aplId), 10) || 0;
    const canonicalSpec = normalizeSimcSpecKey(spec);
    if (!canonicalSpec || (!profileId && !aplId)) {
        throw new Error('缺少可用于模拟的资源或专精');
    }

    // This only preselects the existing simulation form. The task submission still
    // sends IDs to the server, where resource ownership and spec compatibility are
    // enforced again.
    switchSimcWorkbenchL1Tab('workflow', 'import');
    const source = document.querySelector('input[name="simc-sim-player-source"][value="specified_spec"]');
    const specSelect = document.getElementById('simc-sim-spec');
    if (!source || !specSelect) throw new Error('模拟工作流尚未就绪');
    source.checked = true;
    const matchingSpecOption = Array.from(specSelect.options).find(
        option => normalizeSimcSpecKey(option.value) === canonicalSpec
    );
    if (!matchingSpecOption) throw new Error('该资源的专精不受当前模拟工作流支持');
    specSelect.value = matchingSpecOption.value;
    switchSimcPlayerImportMode({ resolve: false });
    await resolveSimcPlayerSource();

    if (profileId) {
        const profileSelect = document.getElementById('simc-sim-profile-select');
        if (!profileSelect || !Array.from(profileSelect.options).some(option => option.value === String(profileId))) {
            throw new Error('该玩家配置不可用于当前专精的模拟');
        }
        profileSelect.value = String(profileId);
        await onSimcProfileSelect();
    }
    if (aplId) {
        simcPendingAplId = String(aplId);
        const aplSelect = document.getElementById('simc-sim-apl-list');
        if (aplSelect && Array.from(aplSelect.options).some(option => option.value === simcPendingAplId)) {
            aplSelect.value = simcPendingAplId;
            simcPendingAplId = '';
        }
    }
    document.getElementById('simc-workbench-import-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    showMessage(profileId ? '已预选玩家配置，可继续设置并发起模拟' : '已预选 APL，可继续设置并发起模拟', 'success');
}
window.startSimcSimulationFromResource = startSimcSimulationFromResource;

let simcResolvedBaseTemplateId = 0;
let simcResolvedCanonicalSpec = '';
let simcPendingAplId = '';
let simcSourceResolutionAbortController = null;

function renderSimcBattlenetLoadState(state, message = '') {
    const host = document.getElementById('simc-sim-bnet-load-status');
    if (!host) return;
    const states = {
        loading: {
            classes: 'border-blue-200 bg-blue-50 text-blue-700',
            icon: '<i class="fas fa-circle-notch fa-spin mr-2" aria-hidden="true"></i>',
            text: '正在从 Battle.net 加载角色信息，请稍候…',
        },
        success: {
            classes: 'border-emerald-200 bg-emerald-50 text-emerald-700',
            icon: '<i class="fas fa-check-circle mr-2" aria-hidden="true"></i>',
            text: 'Battle.net 角色信息加载成功。',
        },
        error: {
            classes: 'border-red-200 bg-red-50 text-red-700',
            icon: '<i class="fas fa-exclamation-circle mr-2" aria-hidden="true"></i>',
            text: 'Battle.net 角色信息加载失败。',
        },
    };
    const config = states[state];
    host.className = `rounded-lg border px-3 py-2 text-sm ${config?.classes || ''}`.trim();
    host.classList.toggle('hidden', !config);
    host.innerHTML = config ? `${config.icon}<span>${escapeHtml(message || config.text)}</span>` : '';
}

function clearSimcResolvedResources() {
    simcSourceResolutionAbortController?.abort();
    simcTargetSpecAbortController?.abort();
    simcTargetSpecAbortController = null;
    simcTargetSpecGeneration += 1;
    simcResolvedCanonicalSpec = '';
    simcResolvedBaseTemplateId = 0;
    const apl = document.getElementById('simc-sim-apl-list');
    if (apl) {
        apl.disabled = true;
        apl.innerHTML = '<option value="">请先完成来源预检以加载 APL</option>';
    }
    const talent = document.getElementById('simc-sim-talent-string');
    if (talent) talent.innerHTML = '<option value="">请选择天赋字符串</option>';
    const detail = document.getElementById('simc-sim-player-detail');
    if (detail) detail.innerHTML = '';
    simcComparisonDefaultTalent = null;
    renderSimcComparisonCandidates({}, []);
}

function collectSimcPlayerSource({ requireComplete = true } = {}) {
    const type = document.querySelector('input[name="simc-sim-player-source"]:checked')?.value || 'battlenet';
    if (type === 'specified_spec') {
        const selected = document.getElementById('simc-sim-profile-select')?.value || 'default';
        if (selected === 'default') return { type: 'default' };
        const profile_id = selectedSimcReferenceValue('#simc-sim-profile-select');
        if (!profile_id) throw new Error('请选择已有玩家配置');
        return { type: 'saved_profile', profile_id };
    }
    if (type === 'battlenet') {
        const region = document.getElementById('simc-sim-bnet-region')?.value || '';
        const realm = document.getElementById('simc-sim-bnet-realm')?.value?.trim() || '';
        const character = document.getElementById('simc-sim-bnet-character')?.value?.trim() || '';
        if (requireComplete && (!region || !realm || !character)) throw new Error('请完整填写 Battle.net 区域、服务器和角色名');
        return { type: 'battlenet', region, realm, character };
    }
    if (type === 'simc_addon') {
        const simc_code = document.getElementById('simc-sim-addon-code')?.value?.trim() || '';
        if (!simc_code) throw new Error('请粘贴 SimC Addon 代码');
        return { type: 'simc_addon', simc_code };
    }
    return { type: 'default' };
}

function requireSimcRunReferences() {
    const base_template_id = simcResolvedBaseTemplateId;
    const selected_apl_id = selectedSimcReferenceValue('#simc-sim-apl-list');
    if (!base_template_id) throw new Error('请选择基础模板');
    if (!selected_apl_id) throw new Error('请选择 APL');
    if (!simcResolvedCanonicalSpec) throw new Error('请先完成来源预检并解析职业专精');
    const player_source = collectSimcPlayerSource();
    const backend_id = selectedSimcReferenceValue('#simc-sim-backend');
    if (!backend_id) throw new Error('请选择 SimC 后端');
    const references = { base_template_id, selected_apl_id, backend_id, player_source, spec: simcResolvedCanonicalSpec };
    const talent_string_id = document.getElementById('simc-sim-talent-string')?.value || '';
    if (!talent_string_id) throw new Error('请选择天赋字符串');
    references.talent_string_id = talent_string_id;
    if (player_source.type === 'saved_profile') references.simc_profile_id = player_source.profile_id;
    return references;
}

async function loadSimcTalentStringCandidates(spec) {
    const select = document.getElementById('simc-sim-talent-string');
    if (!select || !spec) return;
    const response = await fetch(`/api/simc-talent-string-candidates/?spec=${encodeURIComponent(spec)}`);
    const payload = await response.json();
    if (!response.ok || !payload.success) return;
    select.innerHTML = '<option value="">请选择天赋字符串</option>' + payload.data.map(item => {
        const heroTalentNames = Array.isArray(item.hero_talent_names)
            ? item.hero_talent_names.filter(Boolean).join('、')
            : '';
        const label = `${item.name} · ${heroTalentNames || '未解析'}`;
        return `<option value="${escapeHtml(item.id)}">${escapeHtml(label)}</option>`;
    }).join('');
    select.disabled = false;
}

function currentSimcScenario() {
    const scenario = {
        fight_style: document.getElementById('simc-sim-fight-style')?.value || 'Patchwerk',
        time: Math.max(1, Number.parseInt(document.getElementById('simc-sim-time')?.value || '300', 10) || 300),
        target_count: Math.max(1, Number.parseInt(document.getElementById('simc-sim-target-count')?.value || '1', 10) || 1),
        enemy_initial_health_percentage: Math.min(100, Math.max(1, Number(document.getElementById('simc-sim-enemy-initial-health')?.value || '100') || 100)),
        additional_simc_input: document.getElementById('simc-sim-additional-input')?.value || '',
    };
    const profileOverrides = {};
    document.querySelectorAll('[data-simc-profile-override]').forEach(input => {
        const value = String(input.value || '').trim();
        if (value) profileOverrides[input.dataset.simcProfileOverride] = value;
    });
    const mainHandEnchant = profileOverrides.temporary_enchant_main_hand;
    const offHandEnchant = profileOverrides.temporary_enchant_off_hand;
    delete profileOverrides.temporary_enchant_main_hand;
    delete profileOverrides.temporary_enchant_off_hand;
    if (mainHandEnchant || offHandEnchant) {
        profileOverrides.temporary_enchant = [
            mainHandEnchant ? `main_hand:${mainHandEnchant}` : '',
            offHandEnchant ? `off_hand:${offHandEnchant}` : '',
        ].filter(Boolean).join('/');
    }
    if (Object.keys(profileOverrides).length) scenario.profile_overrides = profileOverrides;
    const control = document.getElementById('simc-sim-raid-buff-control');
    scenario.use_class_raid_buff = document.getElementById('simc-sim-use-class-raid-buff')?.checked !== false;
    if (control?.dataset.raidBuffExplicit === '1') {
        scenario.raid_buffs = Array.from(document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]:checked'))
            .map(input => input.value);
    } else {
        delete scenario.raid_buffs;
    }
    scenario.extra_options = Array.from(document.querySelectorAll('[data-simc-extra-option]:checked'))
        .map(input => input.value);
    return scenario;
}

let simcProfileSwitchGeneration = 0;
let simcProfileSwitchAbortController = null;
let simcTargetSpecGeneration = 0;
let simcTargetSpecAbortController = null;
let simcPlayerDetailRequestSerial = 0;
let simcPlayerDetailAbortController = null;

function isCurrentSimcProfileSwitch(control) {
    return Boolean(control
        && control.generation === simcProfileSwitchGeneration
        && control.controller === simcProfileSwitchAbortController
        && selectedSimcReferenceValue('#simc-sim-profile-select') === control.profileId);
}

function isCurrentSimcResourceControl(control) {
    if (!control) return true;
    if (control.kind === 'target-spec') {
        return control.generation === simcTargetSpecGeneration
            && control.controller === simcTargetSpecAbortController
            && simcResolvedCanonicalSpec === control.spec;
    }
    return isCurrentSimcProfileSwitch(control);
}

function beginSimcTargetSpecLoad(spec) {
    simcTargetSpecAbortController?.abort();
    const controller = new AbortController();
    const control = { kind: 'target-spec', generation: ++simcTargetSpecGeneration, spec, controller };
    simcTargetSpecAbortController = controller;
    return control;
}

function beginSimcProfileSwitch(profileId) {
    if (simcProfileSwitchAbortController) {
        const controller = simcProfileSwitchAbortController;
        controller.abort();
    }
    if (simcPlayerDetailAbortController) simcPlayerDetailAbortController.abort();
    simcPlayerDetailAbortController = null;
    simcPlayerDetailRequestSerial += 1;
    stopSimcCandidateComparisonPolling();
    const controller = new AbortController();
    const control = { generation: ++simcProfileSwitchGeneration, profileId, controller };
    simcProfileSwitchAbortController = controller;
    const detailHost = document.getElementById('simc-sim-player-detail');
    if (detailHost) detailHost.innerHTML = '';
    simcComparisonDefaultTalent = null;
    renderSimcComparisonCandidates({}, []);
    return control;
}

async function loadSimcAplCandidates(spec, control = null) {
    const container = document.getElementById('simc-sim-apl-list');
    if (!container) return;
    if (!isCurrentSimcResourceControl(control)) return;
    if (!spec) {
        container.disabled = true;
        container.innerHTML = '<option value="">请选择 Profile 以加载 APL</option>';
        return;
    }
    container.disabled = true;
    container.innerHTML = '<option value="">加载 APL 列表中…</option>';
    try {
        const query = new URLSearchParams({ spec });
        const className = getSimcSpecClass(spec);
        if (className) query.set('class_name', className);
        const options = control ? { signal: control.controller.signal } : {};
        const response = await fetch('/api/simc-apl-candidates/?' + query.toString(), options);
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载 APL 失败');
        if (!isCurrentSimcResourceControl(control)) return;
        const rows = Array.isArray(payload.data) ? payload.data : [];
        simcResolvedBaseTemplateId = Number(payload.default_template_id) || 0;
        loadSimcTalentStringCandidates(spec).catch(() => {});
        const defaults = rows.filter(row => row.is_default === true);
        if (defaults.length !== 1 || !simcResolvedBaseTemplateId) throw new Error('后端未返回唯一默认 APL 和基础模板');
        const selectedAplId = rows.some(row => String(row.id) === simcPendingAplId)
            ? simcPendingAplId
            : String(defaults[0].id);
        simcPendingAplId = '';
        container.innerHTML = rows.length ? rows.map(row => {
            const name = row.name || `APL #${row.id}`;
            const label = ['simc_upstream', 'simc_builtin'].includes(row.source)
                ? `${name} · SimC`
                : name;
            return `<option value="${Number(row.id) || ''}" ${String(row.id) === selectedAplId ? 'selected' : ''}>${escapeHtml(label)}</option>`;
        }).join('') : '<option value="">当前 Profile 专精没有可选 APL</option>';
        container.disabled = rows.length === 0;
    } catch (error) {
        simcResolvedBaseTemplateId = 0;
        if (error.name === 'AbortError' || !isCurrentSimcResourceControl(control)) return;
        container.disabled = true;
        container.innerHTML = `<option value="">${escapeHtml(String(error.message || error))}</option>`;
    }
}

async function simcWbFetchProfilesForWorkbench(signal = undefined) {
    const response = await fetch('/api/simc-profile/', signal ? { signal } : {});
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '加载 Profile 失败');
    return Array.isArray(payload.data) ? payload.data : [];
}

async function loadSimcSimProfileSelect(preferredId = 0, control = null) {
    const select = document.getElementById('simc-sim-profile-select');
    if (!select) return;
    const previous = String(preferredId || select.value || '');
    select.innerHTML = '<option value="">加载中…</option>';
    try {
        const profiles = await simcWbFetchProfilesForWorkbench(control?.controller?.signal);
        if (!isCurrentSimcResourceControl(control)) return;
        const canonicalSpec = String(control?.spec || simcResolvedCanonicalSpec || '').trim().toLowerCase();
        const matchingProfiles = profiles.filter(profile => (
            String(profile.canonical_spec || '').trim().toLowerCase() === canonicalSpec
        ));
        const defaultSystemProfile = matchingProfiles.find(profile => profile.is_system === true) || null;
        const fallbackDefaultOption = defaultSystemProfile ? '' : '<option value="default">系统默认配置</option>';
        select.innerHTML = fallbackDefaultOption + matchingProfiles.map(profile => {
            const label = profile.is_system === true
                ? `系统默认配置 · ${profile.name || `Profile #${profile.id}`}`
                : (profile.name || `Profile #${profile.id}`);
            return `<option value="${Number(profile.id) || ''}" data-spec="${escapeHtml(profile.spec || '')}">${escapeHtml(label)} (${escapeHtml(profile.spec_label || profile.spec || '-')})</option>`;
        }).join('');
        if (matchingProfiles.some(profile => String(profile.id) === previous)) {
            select.value = previous;
        } else if (defaultSystemProfile) {
            select.value = String(defaultSystemProfile.id);
        }
        select.disabled = matchingProfiles.length === 0;
        if (select.value && select.value !== 'default') await onSimcProfileSelect();
    } catch (error) {
        if (error.name === 'AbortError' || !isCurrentSimcResourceControl(control)) return;
        select.innerHTML = '<option value="">加载失败</option>';
        showMessage(String(error.message || error), 'error');
    }
}

async function resolveSimcPlayerSource(preferredProfileId = 0) {
    simcSourceResolutionAbortController?.abort();
    beginSimcProfileSwitch(0);
    clearSimcResolvedResources();
    const controller = new AbortController();
    simcSourceResolutionAbortController = controller;
    const type = document.querySelector('input[name="simc-sim-player-source"]:checked')?.value || 'battlenet';
    let battlenetPreflightCompleted = false;
    try {
        let canonicalSpec = '';
        let detail = null;
        if (type === 'specified_spec') {
            canonicalSpec = String(document.getElementById('simc-sim-spec')?.value || '');
            if (!canonicalSpec) return;
        } else {
            const source = collectSimcPlayerSource({ requireComplete: false });
            if (type === 'battlenet') {
                const battlenetRealm = String(source.realm || '').trim();
                const battlenetCharacter = String(source.character || '').trim();
                if (!battlenetRealm || !battlenetCharacter) {
                    renderSimcBattlenetLoadState('idle');
                    return;
                }
            }
            const url = type === 'battlenet' ? '/api/simc-battlenet-preflight/' : '/api/simc-player-config-detail/';
            const body = type === 'battlenet' ? source : { player_config_mode: 'simc_addon', simc_code: source.simc_code };
            if (type === 'battlenet') renderSimcBattlenetLoadState('loading');
            const response = await fetch(url, {
                method: 'POST', signal: controller.signal,
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                body: JSON.stringify(body),
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || '来源预检失败');
            canonicalSpec = payload.canonical_spec || payload.data?.canonical_spec || '';
            detail = payload.data || null;
            if (type === 'battlenet') {
                battlenetPreflightCompleted = true;
                const identity = detail?.identity || {};
                const characterLabel = [identity.name, identity.realm].filter(Boolean).join(' · ');
                renderSimcBattlenetLoadState('success', characterLabel
                    ? `${characterLabel} 的角色信息加载成功。`
                    : 'Battle.net 角色信息加载成功。');
            }
        }
        if (simcSourceResolutionAbortController !== controller) return;
        simcResolvedCanonicalSpec = canonicalSpec;
        applyImplicitSimcRaidBuffDefaults();
        const control = beginSimcTargetSpecLoad(canonicalSpec);
        await loadSimcAplCandidates(canonicalSpec, control);
        if (type === 'specified_spec') {
            await loadSimcSimProfileSelect(preferredProfileId, control);
            if (document.getElementById('simc-sim-profile-select')?.value === 'default') renderSimcInstantPlayerDetail();
        } else if (type !== 'specified_spec' && detail) {
            renderSimcSavedProfileDetail(detail);
            const comparisonEquipment = Array.isArray(detail.equipment) ? detail.equipment : [];
            renderSimcComparisonCandidates(detail.comparison_candidates || {}, comparisonEquipment);
        }
    } catch (error) {
        if (error.name !== 'AbortError' && simcSourceResolutionAbortController === controller) {
            clearSimcResolvedResources();
            if (type === 'battlenet' && !battlenetPreflightCompleted) {
                renderSimcBattlenetLoadState('error', `Battle.net 角色信息加载失败：${String(error.message || error)}`);
            }
            throw error;
        }
    } finally {
        if (simcSourceResolutionAbortController === controller) simcSourceResolutionAbortController = null;
    }
}

async function onSimcTargetSpecChange() {
    return resolveSimcPlayerSource();
}

async function loadSimcSimSavedProfiles() {
    const container = document.getElementById('simc-sim-saved-profiles');
    if (!container) return;
    try {
        const profiles = await simcWbFetchProfilesForWorkbench();
        container.innerHTML = profiles.length ? profiles.map(profile => `
            <button type="button" class="simc-sim-load-profile mb-1 flex w-full items-center gap-2 rounded-md border bg-white px-2.5 py-1.5 text-left text-xs" data-profile-id="${Number(profile.id) || ''}">
                <span class="min-w-0 flex-1 truncate font-medium">${escapeHtml(profile.name || `Profile #${profile.id}`)}</span>
                <span class="text-gray-500">${escapeHtml(profile.spec_label || profile.spec || '-')}</span>
            </button>`).join('') : '<div class="text-xs text-gray-500">暂无 Profile，请先在 Profile 管理中创建。</div>';
        container.querySelectorAll('.simc-sim-load-profile').forEach(button => button.addEventListener('click', async () => {
            const select = document.getElementById('simc-sim-profile-select');
            if (select) select.value = button.dataset.profileId || '';
            await onSimcProfileSelect();
        }));
    } catch (error) {
        container.innerHTML = `<div class="text-xs text-red-600">${escapeHtml(String(error.message || error))}</div>`;
    }
}

function refreshSimcSavedProfiles() {
    return Promise.all([loadSimcSimProfileSelect(), loadSimcSimSavedProfiles()]);
}

async function onSimcProfileSelect() {
    const select = document.getElementById('simc-sim-profile-select');
    const option = select?.selectedOptions?.[0];
    const profileId = selectedSimcReferenceValue('#simc-sim-profile-select');
    if (select?.value === 'default') {
        beginSimcProfileSwitch(0);
        renderSimcInstantPlayerDetail();
        return;
    }
    const control = beginSimcProfileSwitch(profileId);
    const spec = normalizeSimcSpecKey(simcResolvedCanonicalSpec);
    const profileSpec = normalizeSimcSpecKey(option?.dataset.spec || '');
    if (profileId && profileSpec !== spec) {
        select.value = 'default';
        showMessage('所选 Profile 专精与目标专精不匹配', 'error');
        renderSimcInstantPlayerDetail();
        return;
    }
    if (!profileId) return;
    await refreshSavedSimcPlayerDetail(control);
}

let simcComparisonCurrentEquipment = [];
let simcComparisonDefaultTalent = null;

function updateSimcComparisonSimulationCount() {
    const count = document.getElementById('simc-comparison-simulation-count');
    if (!count) return;
    const selected = Array.from(document.querySelectorAll('.simc-comparison-candidate:checked'));
    const kinds = new Set(selected.map(input => input.dataset.kind));
    if (kinds.size > 1) {
        count.innerHTML = '<span class="text-amber-700">装备与天赋需要分别模拟，请只勾选一种类型</span>';
        return;
    }
    const baseSelected = document.querySelector('.simc-comparison-current:checked') ? 1 : 0;
    const total = baseSelected + selected.length;
    count.innerHTML = `预计模拟 <strong class="text-lg text-violet-700">${total}</strong> 次`;
}

function simcTalentSimulatorUrl(buildCode, requestedCanonicalSpec = '', requestedVersionKey = '') {
    buildCode = String(buildCode || '').trim();
    const canonicalSpec = String(requestedCanonicalSpec || simcResolvedCanonicalSpec || '').trim().toLowerCase();
    const versionKey = String(requestedVersionKey || '').trim();
    const separator = canonicalSpec.indexOf('_');
    if (!buildCode || separator <= 0) return '';
    const classToken = canonicalSpec.slice(0, separator);
    const specToken = canonicalSpec.slice(separator + 1);
    const classAliases = { deathknight: 'DeathKnight', demonhunter: 'DemonHunter' };
    const toPascalCase = value => value.split('_').filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join('');
    const params = new URLSearchParams();
    params.set('class', classAliases[classToken] || toPascalCase(classToken));
    params.set('spec', toPascalCase(specToken));
    params.set('code', buildCode);
    if (versionKey) params.set('version', versionKey);
    return `/portal/talents/?${params.toString()}`;
}

function simcTalentSimulatorLink(buildCode) {
    const url = simcTalentSimulatorUrl(buildCode);
    if (!url) return '';
    return `<a data-talent-simulator-link href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" class="shrink-0 self-center rounded-md border border-violet-200 bg-white px-2 py-1 text-[11px] font-medium text-violet-700 transition hover:border-violet-400 hover:bg-violet-50">在天赋模拟器中打开</a>`;
}

function simcTalentCopyButton(buildCode) {
    return `<button type="button" data-copy-talent-code="${escapeHtml(buildCode)}" class="shrink-0 rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:border-slate-400">复制</button>`;
}

function renderSimcComparisonCandidates(comparison, equipment = null) {
    const container = document.getElementById('simc-sim-comparison-candidates');
    if (!container) return;
    if (Array.isArray(equipment)) simcComparisonCurrentEquipment = equipment;
    const gear = Array.isArray(comparison?.gear) ? comparison.gear : [];
    const talents = Array.isArray(comparison?.talents) ? comparison.talents : [];
    const defaultTalent = comparison?.default_talent && comparison.default_talent.talent
        ? comparison.default_talent
        : simcComparisonDefaultTalent;
    if (comparison?.default_talent && comparison.default_talent.talent) {
        simcComparisonDefaultTalent = comparison.default_talent;
    } else if (comparison && Object.prototype.hasOwnProperty.call(comparison, 'default_talent')) {
        simcComparisonDefaultTalent = null;
    }
    const labels = {head:'头盔',neck:'项链',shoulder:'肩甲',back:'披风',chest:'胸甲',wrist:'护腕',hands:'手套',waist:'腰带',legs:'腿甲',feet:'靴子',finger1:'戒指1',finger2:'戒指2',trinket1:'饰品1',trinket2:'饰品2',main_hand:'主手',off_hand:'副手'};
    const candidatesBySlot = gear.reduce((groups, row) => { (groups[row.slot] ||= []).push(row); return groups; }, {});
    const currentBySlot = Object.fromEntries(simcComparisonCurrentEquipment.map(row => [row.slot, row]));
    const orderedSlots = Object.keys(labels).filter(slot => currentBySlot[slot] || candidatesBySlot[slot]);
    const itemCard = (row, slot, current = false) => {
        const name = row.display_name || row.name || `${labels[slot] || slot} #${row.id || row.item_id || '-'}`;
        const itemId = Number(row.id || row.item_id) || 0;
        const itemLevel = row.item_level ? `<span class="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">ilvl ${Number(row.item_level)}</span>` : '';
        if (current) return `<label data-candidate-card="base" data-candidate-item-row class="flex min-w-0 cursor-pointer items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-1.5"><input type="checkbox" checked class="simc-comparison-current accent-emerald-600"><span class="min-w-0 flex-1 truncate text-xs font-medium text-gray-900">${escapeHtml(name)}</span>${itemLevel}</label>`;
        return `<label data-candidate-card="alternative" data-candidate-item-row class="flex min-w-0 cursor-pointer items-center gap-2 rounded-md border border-violet-200 bg-white px-2 py-1.5 transition hover:border-violet-400 hover:bg-violet-50"><input class="simc-comparison-candidate accent-violet-600" type="checkbox" data-kind="gear_candidates" data-slot="${escapeHtml(row.slot || slot)}" data-item-id="${Number(row.item_id) || 0}" data-source="${escapeHtml(row.source || '')}" data-raw-value="${escapeHtml(row.raw_value || '')}" data-name="${escapeHtml(name)}"><span class="min-w-0 flex-1 truncate text-xs font-medium text-gray-900">${escapeHtml(name)}</span>${itemLevel}</label>`;
    };
    const gearCards = orderedSlots.map(slot => {
        const current = currentBySlot[slot];
        const alternatives = candidatesBySlot[slot] || [];
        return `<section data-candidate-slot-group="${escapeHtml(slot)}" class="overflow-hidden rounded-lg border border-gray-200 bg-slate-50"><div class="flex items-center justify-between border-b border-gray-200 bg-white px-2.5 py-1.5"><div class="text-xs font-semibold text-gray-700">${escapeHtml(labels[slot] || slot)}</div><div class="text-[10px] text-gray-400">${alternatives.length} 个候选</div></div><div class="grid gap-1.5 p-2 sm:grid-cols-2 lg:grid-cols-3">${current ? itemCard(current, slot, true) : '<div class="rounded border border-dashed border-amber-300 bg-amber-50 px-2 py-1.5 text-xs text-amber-700">未解析到已装备物品</div>'}${alternatives.map(row => itemCard(row, slot)).join('')}</div></section>`;
    }).join('');
    const defaultTalentRow = defaultTalent ? `<div data-candidate-card="default-talent" class="flex min-w-0 items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs"><input aria-label="选择默认天赋" class="simc-comparison-current shrink-0 accent-emerald-600" type="checkbox" checked><strong class="shrink-0 text-emerald-900">默认天赋</strong><span title="${escapeHtml(defaultTalent.talent)}" class="min-w-0 flex-1 truncate font-mono text-[10px] text-emerald-700">${escapeHtml(defaultTalent.talent)}</span>${simcTalentCopyButton(defaultTalent.talent)}${simcTalentSimulatorLink(defaultTalent.talent)}</div>` : '';
    const talentRows = talents.map(row => `<div data-candidate-card="talent" class="flex min-w-0 items-center gap-2 rounded-lg border border-violet-200 bg-white px-3 py-2 text-xs hover:border-violet-400"><input aria-label="选择${escapeHtml(row.name || '候选天赋')}" class="simc-comparison-candidate shrink-0 accent-violet-600" type="checkbox" data-kind="talent_candidates" data-name="${escapeHtml(row.name || '候选天赋')}" data-talent="${escapeHtml(row.talent || '')}" data-source="${escapeHtml(row.source || '')}"><strong class="shrink-0 text-gray-900">${escapeHtml(row.name || '候选天赋')}</strong><span title="${escapeHtml(row.talent || '')}" class="min-w-0 flex-1 truncate font-mono text-[10px] text-gray-500">${escapeHtml(row.talent || '')}</span>${simcTalentCopyButton(row.talent || '')}${simcTalentSimulatorLink(row.talent)}</div>`).join('');
    const talentOptions = [defaultTalentRow, talentRows].filter(Boolean).join('');
    const slotOptions = Object.entries(labels).map(([value, label]) => `<option value="${value}">${label}</option>`).join('');
    container.innerHTML = `<div><div class="font-semibold text-gray-900">候选对比</div><p class="mt-1 text-xs text-gray-500">已装备物品默认勾选；取消任意一个已装备物品会同步取消整套基准模拟。</p></div><div class="mt-3 grid gap-2">${gearCards || '<p class="text-xs text-gray-500">尚无已解析装备，可在下方手工加入装备对比项。</p>'}</div>${talentOptions ? `<section class="mt-4 rounded-xl border border-gray-200 bg-slate-50 p-3"><div class="mb-2 text-sm font-semibold">天赋方案</div><div class="grid grid-cols-1 gap-2">${talentOptions}</div><div class="mt-3 grid gap-2"><input id="simc-comparison-add-talent-name" class="rounded border p-2 text-xs" placeholder="方案名称，例如：单体山丘"><textarea id="simc-comparison-add-talent-build" class="min-h-20 rounded border p-2 font-mono text-xs" placeholder="完整天赋树字符串"></textarea><button type="button" id="simc-comparison-add-talent-btn" class="justify-self-start rounded bg-violet-600 px-3 py-2 text-xs text-white">加入天赋方案</button></div></section>` : `<section class="mt-4 rounded-xl border border-gray-200 bg-slate-50 p-3"><div class="mb-2 text-sm font-semibold">天赋方案</div><div class="mt-3 grid gap-2"><input id="simc-comparison-add-talent-name" class="rounded border p-2 text-xs" placeholder="方案名称，例如：单体山丘"><textarea id="simc-comparison-add-talent-build" class="min-h-20 rounded border p-2 font-mono text-xs" placeholder="完整天赋树字符串"></textarea><button type="button" id="simc-comparison-add-talent-btn" class="justify-self-start rounded bg-violet-600 px-3 py-2 text-xs text-white">加入天赋方案</button></div></section>`}<div class="mt-4 rounded-xl border border-dashed border-violet-300 bg-white p-3"><div class="text-xs font-semibold">新增装备对比项</div><div class="mt-2 flex flex-col gap-2 sm:flex-row"><select id="simc-comparison-add-slot" class="rounded border p-2 text-xs">${slotOptions}</select><input id="simc-comparison-add-line" class="min-w-0 flex-1 rounded border p-2 font-mono text-xs" placeholder="head=,id=249952,ilevel=650"><button type="button" id="simc-comparison-add-btn" class="rounded bg-violet-600 px-3 py-2 text-xs text-white">加入对比</button></div></div>`;
    container.classList.toggle('hidden', document.getElementById('simc-sim-mode')?.value !== 'comparison');
    document.getElementById('simc-comparison-add-btn')?.addEventListener('click', addSimcManualComparisonCandidate);
    document.getElementById('simc-comparison-add-talent-btn')?.addEventListener('click', addSimcManualTalentCandidate);
    container.querySelectorAll('[data-copy-talent-code]').forEach(button => button.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(button.dataset.copyTalentCode || '');
            const original = button.textContent;
            button.textContent = '已复制';
            window.setTimeout(() => { button.textContent = original; }, 1200);
        } catch (_) {
            showMessage('复制失败，请手动复制天赋字符串', 'error');
        }
    }));
    container.querySelectorAll('.simc-comparison-candidate').forEach(input => input.addEventListener('change', updateSimcComparisonSimulationCount));
    container.querySelectorAll('.simc-comparison-current').forEach(input => input.addEventListener('change', event => {
        container.querySelectorAll('.simc-comparison-current').forEach(peer => { peer.checked = event.target.checked; });
        updateSimcComparisonSimulationCount();
    }));
    updateSimcComparisonSimulationCount();
}

function addSimcManualComparisonCandidate() {
    const slot = document.getElementById('simc-comparison-add-slot')?.value || '';
    const line = document.getElementById('simc-comparison-add-line')?.value.trim() || '';
    const match = line.match(/^([a-z_]+)\s*=\s*(.*)$/i);
    if (!match || match[1].toLowerCase() !== slot || !/(^|,)\s*id=\d+/i.test(match[2])) {
        showMessage('请输入与所选部位一致且包含有效 id 的 SimC 装备配置', 'warning'); return;
    }
    const itemId = Number((match[2].match(/(?:^|,)\s*id=(\d+)/i) || [])[1]);
    const existing = document.querySelectorAll(`.simc-comparison-candidate[data-slot="${slot}"]`);
    if (Array.from(existing).some(input => Number(input.dataset.itemId) === itemId)) { showMessage('该装备候选已存在', 'warning'); return; }
    const data = { slot, item_id: itemId, source: 'manual', raw_value: match[2].trim(), name: `手工候选 #${itemId}` };
    const current = { gear: [], talents: [] };
    document.querySelectorAll('.simc-comparison-candidate').forEach(input => current[input.dataset.kind === 'talent_candidates' ? 'talents' : 'gear'].push(input.dataset.kind === 'talent_candidates' ? { talent: input.dataset.talent, source: input.dataset.source } : { slot: input.dataset.slot, item_id: Number(input.dataset.itemId), source: input.dataset.source, raw_value: input.dataset.rawValue }));
    current.gear.push(data);
    renderSimcComparisonCandidates(current);
}

function addSimcManualTalentCandidate() {
    const name = document.getElementById('simc-comparison-add-talent-name')?.value.trim() || '';
    const talent = document.getElementById('simc-comparison-add-talent-build')?.value.trim() || '';
    if (!name || !talent) {
        showMessage('请填写方案名称和完整天赋字符串', 'warning');
        return;
    }
    const existing = Array.from(document.querySelectorAll('.simc-comparison-candidate[data-kind="talent_candidates"]'));
    if (existing.some(input => input.dataset.talent === talent)) {
        showMessage('该天赋字符串已存在', 'warning');
        return;
    }
    const current = { gear: [], talents: [] };
    document.querySelectorAll('.simc-comparison-candidate').forEach(input => {
        if (input.dataset.kind === 'talent_candidates') {
            current.talents.push({
                name: input.dataset.name,
                talent: input.dataset.talent,
                source: input.dataset.source,
            });
        } else {
            current.gear.push({
                slot: input.dataset.slot,
                item_id: Number(input.dataset.itemId),
                source: input.dataset.source,
                raw_value: input.dataset.rawValue,
            });
        }
    });
    current.talents.push({ name, talent, source: 'manual' });
    renderSimcComparisonCandidates(current);
}


function updateSimcHomeMode() {
    const mode = document.getElementById('simc-sim-mode')?.value || 'normal';
    const descriptions = {
        normal: '普通模拟将创建一个引用型原子任务。',
        attribute: '属性寻优将在一个任务中生成只改变属性差异的多个候选执行。',
        comparison: '候选对比将在一个任务中执行右侧玩家详情里勾选的同类候选。',
    };
    const options = document.getElementById('simc-sim-mode-options');
    if (options) options.textContent = descriptions[mode] || '';
    const candidates = document.getElementById('simc-sim-comparison-candidates');
    if (candidates) candidates.classList.toggle('hidden', mode !== 'comparison' || !candidates.innerHTML.trim());
    document.getElementById('simc-comparison-simulation-count')?.classList.toggle('hidden', mode !== 'comparison');
}

async function refreshSimcPlayerDetail() {
    const type = document.querySelector('input[name="simc-sim-player-source"]:checked')?.value || 'battlenet';
    if (type === 'specified_spec' && document.getElementById('simc-sim-profile-select')?.value !== 'default') {
        await refreshSavedSimcPlayerDetail();
        return;
    }
    await resolveSimcPlayerSource();
}

function renderSimcInstantPlayerDetail() {
    const host = document.getElementById('simc-sim-player-detail');
    if (!host) return;
    const spec = normalizeSimcSpecKey(simcResolvedCanonicalSpec);
    let source;
    try { source = collectSimcPlayerSource(); }
    catch (error) {
        host.innerHTML = `<span class="text-xs text-amber-700">${escapeHtml(String(error.message || error))}</span>`;
        return;
    }
    const labels = { default: '目标专精默认配置', battlenet: 'Battle.net 即时查询', simc_addon: 'SimC Addon 即时导入' };
    const identity = source.type === 'battlenet'
        ? `${source.region} · ${source.realm} · ${source.character}`
        : source.type === 'simc_addon' ? `${source.simc_code.split(/\r?\n/).length} 行代码` : '提交时解析系统基线';
    host.innerHTML = `<dl class="grid gap-2 text-xs md:grid-cols-2"><div>目标专精：<b>${escapeHtml(spec || '未选择')}</b></div><div>来源：<b>${escapeHtml(labels[source.type] || source.type)}</b></div><div class="md:col-span-2">输入：${escapeHtml(identity)}</div><div class="md:col-span-2 text-gray-500">提交后端时解析并在同一事务中固化为 Profile 不可变版本；来源专精只用于冲突校验。</div></dl>`;
}

function renderSimcSavedProfileDetail(detail) {
    const host = document.getElementById('simc-sim-player-detail');
    if (!host) return;
    if (!detail) {
        host.textContent = '暂无可展示的玩家配置。';
        return;
    }
    const identity = detail.identity || {};
    const talents = detail.talents || { build_code: detail.simc_config?.talent || '' };
    const stats = detail.stats || {};
    const source = detail.source || { type: 'battlenet', label: 'Battle.net 即时角色预检' };
    const identitySpec = identity.spec || detail.spec?.key || '';
    const equipmentRows = Array.isArray(detail.equipment) ? detail.equipment : [];
    const equipmentSummary = (!Array.isArray(detail.equipment) && detail.equipment) || {};
    const value = raw => escapeHtml(String(raw == null || raw === '' ? '-' : raw));
    const secondaryLabels = { crit: '暴击', haste: '急速', mastery: '精通', versatility: '全能' };
    const secondaryRows = Object.entries(stats.secondary || {}).map(([key, stat]) => {
        const row = stat || {};
        const percent = row.percent == null ? '' : ` / ${value(row.percent)}%`;
        return `<div class="rounded bg-white/80 border border-emerald-100 px-2 py-1"><span class="text-gray-500">${value(secondaryLabels[key] || key)}</span> <b class="text-gray-800">${value(row.rating)}</b><span class="text-gray-500"> 绿字${percent}</span></div>`;
    }).join('') || '<span class="text-gray-400">未提供副属性绿字</span>';
    const primary = Object.entries(stats.primary || {}).map(([key, stat]) => `${value(key)} ${value(stat)}`).join(' · ') || '未提供';
    const equipment = equipmentRows.map(item => {
        const enchant = item.enchant ? `<div class="text-[11px] text-violet-700">附魔：${value(item.enchant.display_name)}</div>` : '';
        const gems = (item.gems || []).length
            ? `<div class="text-[11px] text-cyan-700">宝石：${item.gems.map(gem => value(gem.display_name)).join('、')}</div>` : '';
        const itemName = value(item.display_name);
        const itemLevel = item.item_level ? `ilvl ${value(item.item_level)}` : `#${value(item.id)}`;
        return `<div class="rounded-lg bg-white border border-emerald-100 p-2"><div class="text-[11px] text-gray-500">${value(item.slot_label)}</div><div class="font-medium text-gray-800">${itemName} <span class="text-xs text-gray-400">${itemLevel}</span></div>${enchant}${gems}</div>`;
    }).join('') || (equipmentSummary.count
        ? `<div class="rounded-lg bg-white border border-emerald-100 p-2 text-xs text-gray-700">已加载 <b>${value(equipmentSummary.count)}</b> 件装备${equipmentSummary.item_level ? ` · 平均装等 <b>${value(equipmentSummary.item_level)}</b>` : ''}</div>`
        : '<div class="text-gray-400">未解析到装备槽位。</div>');
    const savedLoadouts = (talents.saved_loadouts || []).map(loadout => `<div><span class="font-medium">${value(loadout.name)}</span><div class="font-mono break-all text-[11px] text-gray-500">${value(loadout.build_code)}</div></div>`).join('');
    const missing = (detail.missing_fields || []).map(text => `<li>${value(text)}</li>`).join('');
    host.innerHTML = `
        <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600 mb-3"><span>来源：<b>${value(source.label)}</b></span><span>角色：<b>${value(identity.name)}</b></span><span>职业/专精：<b>${value(identity.class_name)} / ${value(identitySpec)}</b></span>${identity.race ? `<span>种族：<b>${value(identity.race)}</b></span>` : ''}${identity.level ? `<span>等级：<b>${value(identity.level)}</b></span>` : ''}${identity.region ? `<span>地区/服务器：<b>${value(identity.region)} / ${value(identity.realm)}</b></span>` : ''}</div>
        <div class="grid md:grid-cols-2 gap-3 mb-3"><div class="rounded-lg bg-white/70 border border-emerald-100 p-2"><div class="text-xs text-gray-500">当前天赋构筑码</div><div class="font-mono text-xs break-all text-gray-800">${value(talents.build_code)}</div></div><div class="rounded-lg bg-white/70 border border-emerald-100 p-2"><div class="text-xs text-gray-500 mb-1">主属性</div><div class="text-xs text-gray-700">${primary}</div></div></div>
        ${savedLoadouts ? `<div class="mb-3 rounded-lg bg-white/70 border border-emerald-100 p-2"><div class="text-xs text-gray-500 mb-1">已保存天赋方案</div><div class="space-y-2 text-xs">${savedLoadouts}</div></div>` : ''}
        <div class="mb-3"><div class="text-xs text-gray-500 mb-1">副属性（rating / 按规则换算百分比）</div><div class="grid grid-cols-2 gap-2 text-xs">${secondaryRows}</div></div>
        <div><div class="text-xs text-gray-500 mb-1">装备、附魔与宝石</div><div class="grid md:grid-cols-2 gap-2">${equipment}</div></div>
        ${missing ? `<ul class="mt-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2 list-disc list-inside">${missing}</ul>` : ''}`;
}

async function refreshSavedSimcPlayerDetail() {
    const simc_profile_id = selectedSimcReferenceValue('#simc-sim-profile-select');
    if (!simc_profile_id) {
        showMessage('请先选择已有 Profile', 'warning');
        return;
    }
    if (simcPlayerDetailAbortController) simcPlayerDetailAbortController.abort();
    const controller = new AbortController();
    const requestSerial = ++simcPlayerDetailRequestSerial;
    simcPlayerDetailAbortController = controller;
    const host = document.getElementById('simc-sim-player-detail');
    if (host) host.innerHTML = '<span class="text-xs text-gray-500">正在加载角色详情…</span>';
    let payload;
    try {
        const response = await fetch(`/api/simc-player-config-detail/?profile_id=${simc_profile_id}`, { signal: controller.signal });
        payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载 Profile 详情失败');
    } catch (error) {
        if (error.name === 'AbortError') return;
        if (requestSerial === simcPlayerDetailRequestSerial) showMessage(String(error.message || error), 'error');
        return;
    } finally {
        if (simcPlayerDetailAbortController === controller) simcPlayerDetailAbortController = null;
    }
    if (requestSerial !== simcPlayerDetailRequestSerial
        || selectedSimcReferenceValue('#simc-sim-profile-select') !== simc_profile_id) return;
    const detail = payload.data || {};
    renderSimcSavedProfileDetail(detail);
    renderSimcComparisonCandidates(detail.comparison_candidates || {}, detail.equipment || []);
}

let simcCandidatePollControl = null;
let simcCandidateGeneration = 0;

function isCurrentSimcCandidateControl(control) {
    return Boolean(control && simcCandidatePollControl === control && control.generation === simcCandidateGeneration);
}

async function startSelectedSimcCandidateComparisons() {
    let references;
    try { references = requireSimcRunReferences(); }
    catch (error) { showMessage(String(error.message || error), 'warning'); return; }
    const { simc_profile_id, player_source, base_template_id, selected_apl_id, backend_id, talent_string_id } = references;
    const selected = Array.from(document.querySelectorAll('.simc-comparison-candidate:checked'));
    const include_base = Boolean(document.querySelector('.simc-comparison-current:checked'));
    if (!selected.length) { showMessage('请至少选择一个候选', 'warning'); return; }
    const kinds = [...new Set(selected.map(element => element.dataset.kind))];
    if (kinds.length !== 1) { showMessage('装备与天赋候选请分别创建任务', 'warning'); return; }
    const kind = kinds[0];
    const candidates = selected.map(element => kind === 'talent_candidates'
        ? { name: element.dataset.name, talent: element.dataset.talent, source: element.dataset.source }
        : { slot: element.dataset.slot, item_id: Number(element.dataset.itemId), source: element.dataset.source, raw_value: element.dataset.rawValue || '', name: element.dataset.name || '' });
    stopSimcCandidateComparisonPolling();
    const control = { generation: ++simcCandidateGeneration, controller: new AbortController() };
    simcCandidatePollControl = control;
    try {
        const response = await fetch('/api/simc-task/comparison/', {
            method: 'POST', signal: control.controller.signal,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({
                kind, name: `${simcResolvedCanonicalSpec || 'SimC'} 候选对比`,
                spec: simcResolvedCanonicalSpec,
                simc_profile_id, player_source, base_template_id, selected_apl_id, backend_id, talent_string_id, candidates, include_base,
                ...currentSimcScenario(),
            }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '创建比较任务失败');
        if (!isCurrentSimcCandidateControl(control)) return;
        await showSimcTaskCreatedDialog();
    } catch (error) {
        if (error.name !== 'AbortError') showMessage(String(error.message || error), 'error');
    } finally {
        if (isCurrentSimcCandidateControl(control)) simcCandidatePollControl = null;
    }
}

function stopSimcCandidateComparisonPolling() {
    simcCandidateGeneration += 1;
    simcCandidatePollControl?.controller.abort();
    simcCandidatePollControl = null;
}

function simcAttributeSearchRequestBody() {
    const references = requireSimcRunReferences();
    if (references.player_source?.type === 'default') {
        throw new Error('默认配置不包含可冻结的当前绿字；属性寻优请使用 Battle.net 或粘贴含 gear_* 属性的 SimC 代码');
    }
    return {
        kind: 'attribute_variants', name: `${simcResolvedCanonicalSpec || 'SimC'} 四属性自动寻优`,
        spec: simcResolvedCanonicalSpec,
        ...references,
        attribute_step: 100, ...currentSimcScenario(),
    };
}

async function submitSimcAttributeSearch(payload, signal) {
    const response = await fetch('/api/simc-task/comparison/', {
        method: 'POST', signal,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || '创建属性寻优任务失败');
    return result.data;
}

let simcAttributeSearchControl = null;
function stopSimcAttributeSearch() {
    simcAttributeSearchControl?.controller.abort();
    simcAttributeSearchControl = null;
}

async function startSimcAttributeSearch() {
    stopSimcAttributeSearch();
    const control = { controller: new AbortController() };
    simcAttributeSearchControl = control;
    try {
        const data = await submitSimcAttributeSearch(simcAttributeSearchRequestBody(), control.controller.signal);
        if (simcAttributeSearchControl !== control) return;
        await showSimcTaskCreatedDialog();
    } catch (error) {
        if (error.name !== 'AbortError') showMessage(String(error.message || error), 'error');
    } finally {
        if (simcAttributeSearchControl === control) simcAttributeSearchControl = null;
    }
}

async function createSimcAplCandidateTask() {
    let references;
    try { references = requireSimcRunReferences(); }
    catch (error) { showMessage(String(error.message || error), 'warning'); return; }
    const response = await fetch('/api/simc-apl-candidates/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        body: JSON.stringify({
            profile_id: references.simc_profile_id,
            base_template_id: references.base_template_id,
            selected_apl_id: references.selected_apl_id,
            backend_id: references.backend_id,
            talent_string_id: references.talent_string_id,
            candidate_count: 5, include_base: true,
        }),
    });
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '创建 APL 候选任务失败');
    showMessage('APL 候选任务已创建', 'success');
}

async function createSimcSimulationTask() {
    const references = requireSimcRunReferences();
    const scenario = currentSimcScenario();
    const spec = simcResolvedCanonicalSpec;
    const requestBody = {
        name: `${spec} ${scenario.fight_style} ${scenario.time}s ${scenario.target_count}目标`,
        spec: simcResolvedCanonicalSpec,
        ...references,
        ...scenario,
    };
    const response = await fetch('/api/simc-task/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        body: JSON.stringify(requestBody),
    });
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '创建任务失败');
    await showSimcTaskCreatedDialog();
}

async function submitSimcHomeCreation() {
    const button = document.getElementById('simc-sim-submit-btn');
    if (button) button.disabled = true;
    const mode = document.getElementById('simc-sim-mode')?.value || 'normal';
    try {
        requireSimcRunReferences();
        if (mode === 'normal') {
            await createSimcSimulationTask();
        } else if (mode === 'attribute') {
            await startSimcAttributeSearch();
        } else if (mode === 'comparison') {
            await startSelectedSimcCandidateComparisons();
        } else {
            throw new Error('未知的模拟模式');
        }
    } catch (error) {
        showMessage(String(error.message || error), 'warning');
    } finally {
        if (button) button.disabled = false;
    }
}

async function loadSimcRaidBuffOptions() {
    const response = await fetch('/api/simc-raid-buffs/options/');
    const payload = await response.json();
    if (!response.ok || !payload.success || !Array.isArray(payload.data)) {
        throw new Error(payload.error || '加载团队增益失败');
    }
    renderSimcRaidBuffOptions(payload.data);
}

async function loadSimcExtraOptions() {
    const response = await fetch('/api/simc-extra-options/options/');
    const payload = await response.json();
    if (!response.ok || !payload.success || !Array.isArray(payload.data)) {
        throw new Error(payload.error || '加载额外选项失败');
    }
    const host = document.getElementById('simc-sim-extra-options');
    const fourPieceHost = document.getElementById('simc-sim-force-current-tier-4pc');
    if (!host || !fourPieceHost) return;
    const renderOption = option => `
        <label class="flex items-start gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
            <input type="checkbox" value="${escapeHtml(option.value)}" data-simc-extra-option class="mt-0.5 h-4 w-4 shrink-0 rounded border-slate-300">
            <span><b>${escapeHtml(option.label)}</b><small class="block text-xs text-slate-500">${escapeHtml(option.description || '')}</small></span>
        </label>`;
    const fourPieceOption = payload.data.find(option => option.value === 'force_current_tier_4pc');
    fourPieceHost.innerHTML = fourPieceOption ? renderOption(fourPieceOption) : '';
    host.innerHTML = payload.data
        .filter(option => option.value !== 'force_current_tier_4pc')
        .map(renderOption)
        .join('');
}

async function loadSimcConsumableOptions() {
    const response = await fetch('/api/simc-profile/consumable-options/');
    const payload = await response.json();
    if (!response.ok || !payload.success || !payload.data) {
        throw new Error(payload.error || '加载消耗品选项失败');
    }
    document.querySelectorAll('[data-simc-profile-override]').forEach(select => {
        if (select.tagName !== 'SELECT') return;
        const key = select.dataset.simcProfileOverride;
        const current = select.value;
        const options = Array.isArray(payload.data[key]) ? payload.data[key] : [];
        select.innerHTML = '<option value="">不覆盖</option>' + options.map(option =>
            `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`
        ).join('');
        if (options.some(option => option.value === current)) select.value = current;
    });
}

function simcResolvedClassName() {
    return String(simcResolvedCanonicalSpec || '').trim().toLowerCase().split('_', 1)[0];
}

function applyImplicitSimcRaidBuffDefaults() {
    const control = document.getElementById('simc-sim-raid-buff-control');
    document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]').forEach(input => {
        input.checked = false;
    });
    if (control) control.dataset.raidBuffExplicit = '1';
    syncSimcRaidBuffSummary();
}

function renderSimcRaidBuffOptions(options) {
    const host = document.getElementById('simc-sim-raid-buffs');
    if (!host) return;
    host.innerHTML = options.map(option => `
        <label class="simc-raid-buff-option inline-flex min-w-0 items-center gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs text-slate-700">
            <input type="checkbox" value="${escapeHtml(option.value)}" data-default-classes="${escapeHtml(JSON.stringify(option.default_classes || []))}" class="h-4 w-4 shrink-0 rounded border-slate-300">
            <span class="truncate" title="${escapeHtml(option.label)}">${escapeHtml(option.label)}</span>
        </label>`).join('');
    host.querySelectorAll('input[type="checkbox"]').forEach(input => {
        input.addEventListener('change', () => {
            const control = document.getElementById('simc-sim-raid-buff-control');
            if (control) control.dataset.raidBuffExplicit = '1';
            syncSimcRaidBuffSummary();
        });
    });
    applyImplicitSimcRaidBuffDefaults();
}

function syncSimcRaidBuffSummary() {
    const control = document.getElementById('simc-sim-raid-buff-control');
    const boxes = Array.from(document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]'));
    const selected = boxes.filter(box => box.checked).length;
    const master = document.getElementById('simc-sim-raid-buff-all');
    const summary = document.getElementById('simc-sim-raid-buff-summary');
    if (master) {
        master.checked = boxes.length > 0 && selected === boxes.length;
        master.indeterminate = selected > 0 && selected < boxes.length;
    }
    if (summary) summary.textContent = selected
        ? `已选择 ${selected} / ${boxes.length}` : '未选择额外增益';
}

function bindSimcRaidBuffControls() {
    const control = document.getElementById('simc-sim-raid-buff-control');
    const master = document.getElementById('simc-sim-raid-buff-all');
    document.getElementById('simc-sim-use-class-raid-buff')?.addEventListener('change', syncSimcRaidBuffSummary);
    master?.addEventListener('change', () => {
        document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]').forEach(box => { box.checked = master.checked; });
        if (control) control.dataset.raidBuffExplicit = '1';
        syncSimcRaidBuffSummary();
    });
    document.querySelector('[data-simc-raid-buff-action="clear"]')?.addEventListener('click', () => {
        document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]').forEach(box => { box.checked = false; });
        if (control) control.dataset.raidBuffExplicit = '1';
        syncSimcRaidBuffSummary();
    });
    document.querySelector('[data-simc-raid-buff-action="default"]')?.addEventListener('click', () => {
        const classToggle = document.getElementById('simc-sim-use-class-raid-buff');
        if (classToggle) classToggle.checked = true;
        applyImplicitSimcRaidBuffDefaults();
    });
}

async function loadSimcBackendOptions() {
    const select = document.getElementById('simc-sim-backend');
    if (!select) return;
    const response = await fetch('/api/simc-backend-binary/');
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '加载 SimC 后端失败');
    const backends = payload.data?.backends || [];
    select.innerHTML = backends.map(backend => {
        const identifier = escapeHtml(backend.identifier || backend.name || '');
        const gameVersion = escapeHtml(backend.game_version || '-');
        return `<option value="${backend.id}" ${backend.is_default ? 'selected' : ''}>${identifier} · WoW ${gameVersion}</option>`;
    }).join('');
    select.disabled = backends.length === 0;
    if (!backends.length) select.innerHTML = '<option value="">暂无可用后端</option>';
}

async function loadSimcFightStyleOptions() {
    const select = document.getElementById('simc-sim-fight-style');
    if (!select) return;
    const selected = select.value || 'Patchwerk';
    const response = await fetch('/api/simc-fight-styles/options/');
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '加载 SimC 战斗模型失败');
    const styles = Array.isArray(payload.data) ? payload.data : [];
    select.replaceChildren();
    styles.forEach(style => {
        const option = document.createElement('option');
        option.value = String(style.value || '');
        option.textContent = String(style.label || style.value || '');
        select.append(option);
    });
    select.disabled = styles.length === 0;
    if (!styles.length) select.append(new Option('暂无可用战斗模型', ''));
    else if (styles.some(style => String(style.value) === selected)) select.value = selected;
}

function setSimcRerunValue(id, value) {
    const input = document.getElementById(id);
    if (input && value !== undefined && value !== null) input.value = String(value);
}

function applySimcRerunSelectionShell(form, taskId) {
    const params = form.simulation_params || {};
    const source = document.querySelector('input[name="simc-sim-player-source"][value="specified_spec"]');
    if (source) source.checked = true;
    switchSimcPlayerImportMode({ resolve: false });
    setSimcRerunValue('simc-sim-mode', form.mode);
    setSimcRerunValue('simc-sim-fight-style', params.fight_style);
    setSimcRerunValue('simc-sim-time', params.time);
    setSimcRerunValue('simc-sim-target-count', params.target_count ?? params.desired_targets);
    setSimcRerunValue('simc-sim-enemy-initial-health', params.enemy_initial_health_percentage ?? 100);
    setSimcRerunValue('simc-sim-additional-input', params.additional_simc_input);
    const profile = document.getElementById('simc-sim-profile-select');
    if (profile && form.profile_id) {
        profile.innerHTML = `<option value="${Number(form.profile_id)}" selected>${escapeHtml(form.profile_name || `Profile #${form.profile_id}`)}（正在加载详情…）</option>`;
        profile.disabled = true;
    }
    const setPendingSelection = (id, value, label) => {
        const select = document.getElementById(id);
        if (!select || !value) return;
        select.innerHTML = `<option value="${escapeHtml(value)}" selected>${escapeHtml(label)}（正在加载…）</option>`;
        select.disabled = true;
    };
    setPendingSelection('simc-sim-apl-list', form.apl_id, '历史 APL');
    setPendingSelection('simc-sim-talent-string', form.talent_string_id, '历史天赋字符串');
    setPendingSelection('simc-sim-backend', form.backend_id, '历史 SimC 后端');
    const submit = document.getElementById('simc-sim-submit-btn');
    if (submit) submit.disabled = true;
    updateSimcHomeMode();
    syncSimcFightPresetFromInputs();
    showMessage(`正在载入历史任务 #${taskId} 的具体配置…`, 'info');
    return loadSimcSpecOptions().then(() => {
        const spec = document.getElementById('simc-sim-spec');
        if (spec && form.spec) spec.value = form.spec;
    });
}

async function hydrateSimcRerunDependencies(form, taskId, specReady) {
    await specReady;
    await resolveSimcPlayerSource(form.profile_id);
    await Promise.all([
        loadSimcTalentStringCandidates(simcResolvedCanonicalSpec),
        loadSimcBackendOptions(),
        loadSimcRaidBuffOptions(),
        loadSimcExtraOptions(),
        loadSimcConsumableOptions(),
    ]);
    const params = form.simulation_params || {};
    setSimcRerunValue('simc-sim-apl-list', form.apl_id);
    setSimcRerunValue('simc-sim-talent-string', form.talent_string_id);
    setSimcRerunValue('simc-sim-backend', form.backend_id);
    const classRaidBuff = document.getElementById('simc-sim-use-class-raid-buff');
    if (classRaidBuff && typeof params.use_class_raid_buff === 'boolean') classRaidBuff.checked = params.use_class_raid_buff;
    if (Array.isArray(params.raid_buffs)) {
        const control = document.getElementById('simc-sim-raid-buff-control');
        if (control) control.dataset.raidBuffExplicit = '1';
        document.querySelectorAll('#simc-sim-raid-buffs input[type="checkbox"]').forEach(input => {
            input.checked = params.raid_buffs.includes(input.value);
        });
    }
    if (Array.isArray(params.extra_options)) {
        document.querySelectorAll('[data-simc-extra-option]').forEach(input => {
            input.checked = params.extra_options.includes(input.value);
        });
    }
    const submit = document.getElementById('simc-sim-submit-btn');
    if (submit) submit.disabled = false;
    syncSimcRaidBuffSummary();
    showMessage(`已载入历史任务 #${taskId} 的配置；请确认或编辑后再发起模拟。`, 'success');
}

async function loadSimcRerunFormFromLocation() {
    const taskId = new URLSearchParams(window.location.search).get('simc_rerun_task');
    if (!taskId) return;
    const response = await fetch(`/api/simc-workbench/tasks/${encodeURIComponent(taskId)}/?rerun_form=1`);
    const payload = await response.json();
    if (!response.ok || !payload.success || !payload.data?.rerun_form) {
        throw new Error(payload.error || '无法读取历史任务的模拟配置');
    }
    const form = payload.data.rerun_form;
    // `simc_rerun_task` is a one-shot handoff, not persistent workflow state.
    // Consume it only after the projection is available, so a failed fetch can
    // still be retried, while later Dashboard routing never re-hydrates it.
    const url = new URL(window.location.href);
    url.searchParams.delete('simc_rerun_task');
    window.history.replaceState(window.history.state, '', url);
    const specReady = applySimcRerunSelectionShell(form, taskId);
    hydrateSimcRerunDependencies(form, taskId, specReady).catch(error => showMessage(String(error.message || error), 'error'));
}

function bindSimcWorkbenchSimulationControls() {
    loadSimcSpecOptions().catch(error => showMessage(String(error.message || error), 'error'));
    const spec = document.getElementById('simc-sim-spec');
    if (spec && spec.dataset.bound !== '1') {
        spec.dataset.bound = '1';
        spec.addEventListener('change', () => onSimcTargetSpecChange().catch(error => showMessage(String(error.message || error), 'error')));
    }
    const profileSelect = document.getElementById('simc-sim-profile-select');
    if (profileSelect && profileSelect.dataset.bound !== '1') {
        profileSelect.dataset.bound = '1';
        profileSelect.addEventListener('change', () => onSimcProfileSelect().catch(error => showMessage(String(error.message || error), 'error')));
    }
    const sourceInputs = document.querySelectorAll('input[name="simc-sim-player-source"]');
    sourceInputs.forEach(source => {
        if (source.dataset.bound === '1') return;
        source.dataset.bound = '1';
        source.addEventListener('change', switchSimcPlayerImportMode);
    });
    const topSpec = document.getElementById('simc-sim-bnet-spec');
    if (topSpec && topSpec.dataset.bound !== '1') {
        topSpec.dataset.bound = '1';
        topSpec.addEventListener('change', loadSimcBattlenetTopPlayers);
    }
    const topPlayer = document.getElementById('simc-sim-bnet-top-player');
    if (topPlayer && topPlayer.dataset.bound !== '1') {
        topPlayer.dataset.bound = '1';
        topPlayer.addEventListener('change', applySimcBattlenetTopPlayer);
    }
    ['simc-sim-bnet-region', 'simc-sim-bnet-realm', 'simc-sim-bnet-character', 'simc-sim-addon-code'].forEach(id => {
        const input = document.getElementById(id);
        if (!input || input.dataset.sourceBound === '1') return;
        input.dataset.sourceBound = '1';
        input.addEventListener('change', () => resolveSimcPlayerSource().catch(error => {
            if (error.name !== 'AbortError') showMessage(String(error.message || error), 'error');
        }));
        input.addEventListener('input', clearSimcResolvedResources);
    });
    const mode = document.getElementById('simc-sim-mode');
    if (mode && mode.dataset.bound !== '1') {
        mode.dataset.bound = '1';
        mode.addEventListener('change', updateSimcHomeMode);
    }
    const bindings = [
        ['simc-sim-submit-btn', submitSimcHomeCreation],
        ['simc-sim-player-detail-refresh-btn', refreshSimcPlayerDetail],

    ];
    bindings.forEach(([id, handler]) => {
        const element = document.getElementById(id);
        if (element && element.dataset.bound !== '1') {
            element.dataset.bound = '1';
            element.addEventListener('click', handler);
        }
    });
    const preset = document.getElementById('simc-sim-fight-preset');
    preset?.addEventListener('change', () => applySimcFightPreset(preset.value));
    bindSimcRaidBuffControls();
    loadSimcRaidBuffOptions().catch(error => {
        const host = document.getElementById('simc-sim-raid-buffs');
        if (host) host.textContent = String(error.message || error);
    });
    loadSimcExtraOptions().catch(error => {
        const host = document.getElementById('simc-sim-extra-options');
        if (host) host.textContent = String(error.message || error);
    });
    loadSimcFightStyleOptions().catch(error => {
        const select = document.getElementById('simc-sim-fight-style');
        if (select) select.innerHTML = '<option value="">加载战斗模型失败</option>';
        showMessage(String(error.message || error), 'error');
    });
    loadSimcConsumableOptions().catch(error => showMessage(String(error.message || error), 'error'));
    loadSimcBackendOptions().catch(error => showMessage(String(error.message || error), 'error'));
    updateSimcHomeMode();
    switchSimcPlayerImportMode({ resolve: false });
    loadSimcRerunFormFromLocation().catch(error => showMessage(String(error.message || error), 'error'));
}

function applySimcFightPreset(presetValue) {
    if (!presetValue || presetValue === 'custom') return;
    const [time, targets] = String(presetValue).split(',');
    const timeInput = document.getElementById('simc-sim-time');
    const targetInput = document.getElementById('simc-sim-target-count');
    if (timeInput) timeInput.value = String(Number.parseInt(time, 10) || 300);
    if (targetInput) targetInput.value = String(Number.parseInt(targets, 10) || 1);
}

function syncSimcFightPresetFromInputs() {
    const preset = document.getElementById('simc-sim-fight-preset');
    const scenario = currentSimcScenario();
    if (!preset) return;
    const expected = `${scenario.time},${scenario.target_count}`;
    preset.value = Array.from(preset.options).some(option => option.value === expected) ? expected : 'custom';
}

// 全局分页变量
let currentPage = 1;
let pageSize = 50;
let totalPages = 1;
let totalCount = 0;

// 全局表格变量
let currentTableName = '';
let currentTableColumns = [];
let currentFieldTypes = {};
let currentFieldLabels = {};
let currentTableDisplayName = '';
let currentTableCapabilities = {
    can_create: false,
    can_update: false,
    can_delete: false,
    read_only_reason: ''
};
let currentTableRowMap = new Map();
let currentEditRowId = null;
let simcProfileSpecFilter = '';
let simcProfileFightStyleFilter = '';
let wowArticleSourceFilter = '';
let wowArticleCategoryFilter = '';
let secondaryStatRuleMap = null;
let secondaryStatRulePromise = null;
let tableFetchRequestSeq = 0;

const COMMON_ADD_FORM_HIDDEN_FIELDS = new Set([
    'id',
    'created_at',
    'updated_at',
    'create_time',
    'update_time',
    'last_updated',
    'last_seen_at',
    'last_scan_time',
    'raw_data',
    'raw_json',
    'ext_json',
    'extra_json',
    'gear_json',
    'talents_json',
    'stats_json',
    'stats_crawl_status',
    'last_seen_bvid',
    'achievement_points',
    'item_level',
    'avatar_url',
    'profile_url',
]);

const TABLE_FORM_CONFIGS = {
    VideoMonitorTarget: {
        addFields: ['name', 'tag', 'platform', 'target_url', 'is_active'],
        hiddenAddFields: ['target_url_hash', 'last_seen_bvid', 'ext_json'],
        selectFields: {
            tag: [
                { value: '攻略', label: '攻略' },
                { value: '职业', label: '职业' },
                { value: '团本', label: '团本' },
                { value: '大秘境', label: '大秘境' },
                { value: '活动', label: '活动' },
                { value: '综合', label: '综合' },
            ],
            platform: [
                { value: 'bilibili', label: 'bilibili' },
            ],
        },
        defaults: {
            platform: 'bilibili',
            is_active: true,
        },
    },
    PortalEvent: {
        addFields: ['title', 'url', 'source', 'tag', 'start_at', 'end_at', 'status', 'summary', 'image_url', 'external_id', 'is_active'],
        hiddenAddFields: ['raw_data', 'last_seen_at'],
        defaults: {
            is_active: true,
            status: 'active',
        },
    },
    PortalToolLink: {
        addFields: ['name', 'url', 'desc', 'source', 'sort_order', 'is_topbar', 'topbar_order', 'icon_path', 'is_active'],
        hiddenAddFields: ['url_hash'],
        defaults: {
            is_active: true,
            is_topbar: false,
            sort_order: 0,
            topbar_order: 0,
        },
    },
    SeasonMeta: {
        hiddenEditFields: ['mplus_encounters', 'raid_encounters', 'raid_zones'],
        hiddenAddFields: ['mplus_encounters', 'raid_encounters', 'raid_zones'],
    },
    PlayerSpecTopPlayer: {},
    SpecDungeonRanking: {},
    SpecRaidRanking: {},
    PortalMythicstatsDpsRow: {},
    WowSpellSnapshot: {},
    WowSpellEffectSnapshot: {},
    WowSpecSpellMapSnapshot: {},
    WowSkillDiffReport: {},
    WowHotfixReport: {},
    WowDailyReport: {},
};

function getCurrentFormConfig() {
    return TABLE_FORM_CONFIGS[currentTableName] || {};
}

function getFieldInfo(column) {
    return (currentFieldTypes && currentFieldTypes[column]) ? currentFieldTypes[column] : {};
}

function getFieldType(column) {
    return getFieldInfo(column).type || '';
}

function isJsonField(column) {
    return getFieldType(column) === 'JSONField';
}

function isModelBooleanField(column) {
    return getFieldType(column) === 'BooleanField';
}

function isModelNumericField(column) {
    return ['IntegerField', 'BigIntegerField', 'SmallIntegerField', 'PositiveIntegerField', 'PositiveSmallIntegerField', 'FloatField', 'DecimalField', 'AutoField', 'BigAutoField'].includes(getFieldType(column));
}

function isModelDateField(column) {
    return getFieldType(column) === 'DateField';
}

function isModelDateTimeField(column) {
    return getFieldType(column) === 'DateTimeField';
}

function isModelTimeOnlyField(column) {
    return getFieldType(column) === 'TimeField';
}

function isModelTextField(column) {
    return ['TextField', 'JSONField'].includes(getFieldType(column));
}

function getFieldChoices(column) {
    const choices = getFieldInfo(column).choices;
    return Array.isArray(choices) && choices.length ? choices : null;
}

function getChoiceLabel(column, value) {
    const choices = getFieldChoices(column);
    if (!choices) return null;
    const match = choices.find(option => String(option.value) === String(value));
    return match ? match.label : null;
}

function isReadonlyModelField(column) {
    const info = getFieldInfo(column);
    return Boolean(info.primary_key || info.editable === false || info.auto_now || info.auto_now_add);
}

function serializeFieldValueForInput(value) {
    if (value === null || value === undefined) {
        return '';
    }
    if (typeof value === 'object') {
        try {
            return JSON.stringify(value, null, 2);
        } catch (e) {
            return String(value);
        }
    }
    return String(value);
}

function normalizeDateTimeLocalValue(val) {
    if (!val) return null;
    return String(val).replace('T', ' ') + (String(val).length === 16 ? ':00' : '');
}

function parseFieldValueFromInput(column, element) {
    const inputType = getFieldInputType(column);
    if (inputType === 'checkbox') {
        return { ok: true, value: element.checked };
    }
    if (isJsonField(column)) {
        const value = element.value.trim();
        if (value === '') return { ok: true, value: null };
        try {
            return { ok: true, value: JSON.parse(value) };
        } catch (e) {
            return { ok: false, error: 'invalid_json' };
        }
    }
    if (inputType === 'number') {
        const value = element.value.trim();
        if (value === '') return { ok: true, value: null };
        return { ok: true, value: isModelNumericField(column) && !['FloatField', 'DecimalField'].includes(getFieldType(column)) ? parseInt(value, 10) : parseFloat(value) };
    }
    if (isModelDateTimeField(column) || (isTimeField(column) && inputType === 'datetime-local')) {
        return { ok: true, value: normalizeDateTimeLocalValue(element.value) };
    }
    if (isModelDateField(column) || inputType === 'date') {
        return { ok: true, value: element.value || null };
    }
    if (isModelTimeOnlyField(column) || inputType === 'time') {
        return { ok: true, value: element.value || null };
    }
    return { ok: true, value: element.value };
}

function isEditFormHiddenField(column) {
    if (isReadonlyModelField(column)) {
        return true;
    }
    const config = getCurrentFormConfig();
    const hiddenFields = config.hiddenEditFields || [];
    return hiddenFields.includes(column);
}

function isAddFormHiddenField(column) {
    const config = getCurrentFormConfig();
    if (isReadonlyModelField(column)) {
        return true;
    }
    if (config.addFields && !config.addFields.includes(column)) {
        return true;
    }
    const normalizedColumn = column.toLowerCase();
    if (COMMON_ADD_FORM_HIDDEN_FIELDS.has(normalizedColumn) || normalizedColumn.endsWith('_hash')) {
        return true;
    }
    const hiddenFields = config.hiddenAddFields || [];
    return hiddenFields.includes(column);
}

function getAddFormSelectOptions(column) {
    const config = getCurrentFormConfig();
    return (config.selectFields && config.selectFields[column]) || getFieldChoices(column);
}

function getAddFormDefaultValue(column) {
    const config = getCurrentFormConfig();
    return config.defaults ? config.defaults[column] : undefined;
}


/**
 * 获取表数据
 */
function fetchTableData(tableName, page = 1) {
    // 显示加载中
    const tableBody = document.getElementById('table-body');
    if (!tableBody) {
        return;
    }
    tableBody.innerHTML = `<tr><td colspan="100%" class="p-6"><div class="animate-pulse space-y-3"><div class="h-4 bg-gray-200 rounded w-2/3"></div><div class="h-4 bg-gray-200 rounded w-4/5"></div><div class="h-4 bg-gray-200 rounded w-3/5"></div></div></td></tr>`;

    // 保存当前表名和页码
    currentTableName = tableName;
    currentPage = page;
    const requestSeq = ++tableFetchRequestSeq;
    const requestTableName = tableName;
    if (tableName === 'SimcSecondaryStatRule') {
        secondaryStatRuleMap = null;
        secondaryStatRulePromise = null;
    }

    // 如果是SimcTask表，使用专门的API
    updateSimcProfileFilterBar();
    updateWowArticleFilterBar();
    if (tableName === 'SimcTask') {
        switchSimcWorkbenchL1Tab('history', 'tasks');
        if (typeof window.simcWorkbenchLoadTaskResource === 'function') window.simcWorkbenchLoadTaskResource('tasks', page);
        return;
    }

    // 获取CSRF令牌
    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        console.error('无法获取CSRF令牌');
        const tableBody = document.getElementById('table-body');
        if (tableBody) {
            tableBody.innerHTML = '<tr><td colspan="100%" class="p-6 text-red-600">错误: 无法获取CSRF令牌，请刷新页面</td></tr>';
        }
        return;
    }

    // 构建请求数据
    const requestData = {
        action: 'get_table_data',
        table_name: tableName,
        page: page,
        page_size: pageSize
    };

    // 如果有搜索查询，添加到请求数据中
    if (searchQuery && searchQuery.length > 0) {
        requestData.search = searchQuery;
    }
    if (tableName === 'SimcProfile') {
        if (simcProfileSpecFilter) requestData.simc_spec = simcProfileSpecFilter;
        if (simcProfileFightStyleFilter) requestData.simc_fight_style = simcProfileFightStyleFilter;
    }
    if (tableName === 'WowArticle') {
        if (wowArticleSourceFilter) requestData.wow_source = wowArticleSourceFilter;
        if (wowArticleCategoryFilter) requestData.wow_category = wowArticleCategoryFilter;
    }

    // 发送AJAX请求获取表数据
    fetch('/dashboard/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify(requestData)
    })
    .then(response => {
        if (!response.ok) {
            console.error('HTTP响应错误:', response.status, response.statusText);
            throw new Error(`HTTP错误! 状态: ${response.status} ${response.statusText}`);
        }
        return response.json();
    })
    .then(data => {
        if (requestSeq !== tableFetchRequestSeq || currentTableName !== requestTableName) {
            return;
        }
        updateSimcProfileFilterBar();
        updateWowArticleFilterBar();
        if (data.status === 'success') {
            if (data.data && Array.isArray(data.data) && data.fields) {
                // 更新分页信息
                totalPages = data.total_pages || 1;
                totalCount = data.total_count || 0;
                currentPage = data.page || 1;
                pageSize = data.page_size || 50;

                // 保存字段类型信息
                currentFieldTypes = data.field_types || {};
                currentFieldLabels = data.field_labels || {};
                currentTableCapabilities = Object.assign({
                    can_create: false,
                    can_update: false,
                    can_delete: false,
                    read_only_reason: ''
                }, data.capabilities || {});
                const addRecordBtn = document.getElementById('add-record-btn');
                if (addRecordBtn) {
                    addRecordBtn.classList.toggle('hidden', !currentTableCapabilities.can_create);
                    addRecordBtn.disabled = !currentTableCapabilities.can_create;
                    addRecordBtn.title = currentTableCapabilities.can_create ? '' : (currentTableCapabilities.read_only_reason || '该表不支持通用新增');
                }
                if (data.table_display_name || data.table_description) {
                    currentTableDisplayName = data.table_display_name || data.table_description;
                    const selectedTableName = document.getElementById('selected-table-name');
                    if (selectedTableName) {
                        selectedTableName.textContent = currentTableDisplayName;
                    }
                }

                if (requestTableName === 'WowArticle' && data.wow_filter_options) {
                    updateWowArticleFilterOptions(data.wow_filter_options);
                }

                displayTableData(data.data, data.fields, requestTableName);
                updatePagination();
            } else {
                console.error('返回的数据格式不正确:', data);
                const tableBody = document.getElementById('table-body');
                if (tableBody) {
                    tableBody.innerHTML = '<tr><td colspan="100%" class="p-6 text-red-600">错误: 返回的数据格式不正确</td></tr>';
                }
            }
        } else {
            console.error('获取数据失败:', data.message || '未知错误');
            const tableBody = document.getElementById('table-body');
            if (tableBody) {
                tableBody.innerHTML = `<tr><td colspan="100%" class="p-6 text-red-600">获取数据失败: ${escapeHtml(data.message || '未知错误')}</td></tr>`;
            }
        }
    })
    .catch(error => {
        if (requestSeq !== tableFetchRequestSeq || currentTableName !== requestTableName) {
            return;
        }
        console.error('获取表数据时发生错误:', error);
        const tableBody = document.getElementById('table-body');
        if (tableBody) {
            tableBody.innerHTML = `<tr><td colspan="100%" class="p-6 text-red-600">获取数据时发生错误: ${escapeHtml(error.message)}</td></tr>`;
        }
    });
}

function updateSimcProfileFilterBar() {
    const bar = document.getElementById('simc-profile-filter-bar');
    if (!bar) return;
    if (currentTableName === 'SimcProfile') bar.classList.remove('hidden');
    else bar.classList.add('hidden');
}

function updateWowArticleFilterBar() {
    const bar = document.getElementById('wow-article-filter-bar');
    if (!bar) return;
    if (currentTableName === 'WowArticle') bar.classList.remove('hidden');
    else bar.classList.add('hidden');
}

/**
 * 显示表数据
 */
function displayTableData(data, fields, tableName = currentTableName) {
    const renderTableName = tableName;
    const tableHeader = document.getElementById('table-header');
    const tableBody = document.getElementById('table-body');

    // 如果表格元素不存在，直接返回
    if (!tableHeader || !tableBody) {
        return;
    }

    const allFields = Array.from(new Set([
        ...((fields && Array.isArray(fields)) ? fields : []),
        ...((data && data.length > 0 && data[0]) ? Object.keys(data[0]) : [])
    ]));

    // 设置当前表的列信息
    currentTableColumns = allFields;
    currentTableRowMap = new Map();

    // 清空表格
    tableHeader.innerHTML = '';
    tableBody.innerHTML = '';

    // 所有表格都显示序号，不显示数据库ID
    let displayFields = allFields;
    let showCustomIndex = true;

    // 过滤掉ID字段，所有表格都不显示数据库ID
    displayFields = allFields.filter(field => field !== 'id');

    if (renderTableName === 'PortalToolLink') {
        const orderedFields = [
            'name',
            'url',
            'url_hash',
            'desc',
            'source',
            'sort_order',
            'is_topbar',
            'topbar_order',
            'icon_path',
            'is_active'
        ];
        displayFields = orderedFields.filter(field => allFields.includes(field));
    }

    // 针对WechatArticle表的特殊处理：显示序号、title、author和时间字段
    if (renderTableName === 'WechatArticle') {
        displayFields = fields.filter(field =>
            field === 'title' ||
            field === 'author' ||
            field === 'created_at' ||
            field === 'updated_at' ||
            field === 'publish_time'
        );
        // 确保关键字段存在并按顺序排列
        const orderedFields = ['title', 'author', 'publish_time', 'created_at', 'updated_at'];
        displayFields = orderedFields.filter(field => allFields.includes(field));
    }

    // 针对WowArticle表的特殊处理：显示序号、title、source、category、author、publish_time
    else if (renderTableName === 'WowArticle') {
        const orderedFields = ['title', 'source', 'category', 'author', 'publish_time'];
        displayFields = orderedFields.filter(field => allFields.includes(field));
    }

    // 针对RssArticle表的特殊处理：不显示rss_id、url、content_html，限制title长度并可点击跳转
    else if (renderTableName === 'RssArticle') {
        displayFields = allFields.filter(field =>
            !['rss_id', 'url', 'content_html'].includes(field)
        );
    }
    // SimcProfile表只显示指定字段
    else if (renderTableName === 'SimcProfile') {
        displayFields = ['name', 'spec', 'fight_style', 'time', 'target_count'];
    }
    else if (renderTableName === 'SimcSecondaryStatRule') {
        displayFields = [
            'class_name',
            'crit_per_percent',
            'haste_per_percent',
            'mastery_per_percent',
            'versatility_per_percent'
        ];
    }

    // 创建表头
    const headerRow = document.createElement('tr');

    // 所有表格都显示序号列
    const indexTh = document.createElement('th');
    indexTh.className = 'px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-16';
    indexTh.textContent = '序号';
    headerRow.appendChild(indexTh);

    // 定义列宽度映射
    const getColumnWidth = (field, index, totalFields) => {
        // 常见字段的宽度设置
        const fieldWidthMap = {
            'id': 'w-16',           // ID列较窄
            'name': 'w-48',         // 名称列较宽
            'title': 'w-48',        // 标题列较宽
            'target': 'w-64',       // 目标URL列更宽
            'url': 'w-64',          // URL列更宽
            'type': 'w-20',         // 类型列较窄
            'status': 'w-20',       // 状态列较窄
            'is_active': 'w-20',    // 布尔字段较窄
            'is_login': 'w-20',     // 布尔字段较窄
            'is_poc': 'w-20',       // 布尔字段较窄
            'is_exp': 'w-20',       // 布尔字段较窄
            'is_verify': 'w-20',    // 布尔字段较窄
            'flag': 'w-16',         // 标志列较窄
            'wait_time': 'w-24',    // 等待时间列中等
            'last_scan_time': 'w-40', // 时间列中等
            'create_time': 'w-40',  // 创建时间列中等
        };

        // 如果有预定义宽度，使用预定义的
        if (fieldWidthMap[field.toLowerCase()]) {
            return fieldWidthMap[field.toLowerCase()];
        }

        // 根据字段名长度和位置动态分配
        if (field.length <= 5) {
            return 'w-20';  // 短字段名
        } else if (field.length <= 10) {
            return 'w-32';  // 中等字段名
        } else {
            return 'w-48';  // 长字段名
        }
    };

    displayFields.forEach((field, index) => {
        const th = document.createElement('th');
        const widthClass = getColumnWidth(field, index, displayFields.length);
        th.className = `px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider ${widthClass}`;
        th.textContent = getFieldDisplayName(field);
        headerRow.appendChild(th);
    });
    const showActionColumn = currentTableCapabilities.can_update
        || currentTableCapabilities.can_delete;
    if (showActionColumn) {
        const actionTh = document.createElement('th');
        actionTh.className = 'px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-32 action-col-header';
        actionTh.id = 'action-col-header';
        actionTh.textContent = '操作';
        headerRow.appendChild(actionTh);
    }
    tableHeader.appendChild(headerRow);

    if (!data || data.length === 0) {
        const columnCount = displayFields.length + 1 + (showActionColumn ? 1 : 0);
        tableBody.innerHTML = `<tr><td colspan="${columnCount}" class="text-center py-8 text-gray-500">暂无数据</td></tr>`;
        return;
    }

    // 创建表格内容
    data.forEach((row, index) => {
        const tr = document.createElement('tr');
        tr.className = index % 2 === 0 ? 'bg-white hover:bg-gray-50' : 'bg-gray-50 hover:bg-gray-100';

        // 使用行的第一个字段值作为row-id，如果没有则使用index
        const rowId = (row && row.id !== undefined && row.id !== null) ? row.id : (row[allFields[0]] || index);
        tr.setAttribute('data-row-id', rowId);
        currentTableRowMap.set(String(rowId), row);

        // 所有表格都显示序号列，根据分页计算正确的序号
        const indexTd = document.createElement('td');
        indexTd.className = 'px-4 py-4 text-sm text-gray-900 w-16';
        const globalIndex = (currentPage - 1) * pageSize + index + 1;
        indexTd.textContent = globalIndex;
        tr.appendChild(indexTd);

        displayFields.forEach((field, index) => {
            const td = document.createElement('td');
            const widthClass = getColumnWidth(field, index, displayFields.length);
            const nowrap = isTimeField(field) ? ' whitespace-nowrap' : '';
            td.className = `px-4 py-4 text-sm text-gray-900 ${widthClass}${nowrap}`;
            td.setAttribute('data-field', field);

            // 处理字段值
            const cellValue = row[field] !== null ? row[field] : '';
            let cellText = '';

            // JSON 对象/数组用 JSON.stringify，不用 String()（否则变成 [object Object]）
            if (cellValue !== null && cellValue !== '' && typeof cellValue === 'object') {
                try {
                    cellText = JSON.stringify(cellValue);
                } catch(e) {
                    cellText = String(cellValue);
                }
            } else {
                cellText = String(cellValue);
            }

            // 处理undefined值
            if (cellValue === undefined || cellText === 'undefined') {
                cellText = '';
            }

            // 根据字段类型和名称进行特殊处理
            const choiceLabel = getChoiceLabel(field, cellValue);
            if (choiceLabel !== null) {
                const badge = document.createElement('span');
                badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-100 text-slate-800';
                badge.textContent = choiceLabel;
                badge.title = cellText;
                td.appendChild(badge);
            }
            else if (isUrlField(field) && cellText) {
                const safeUrl = getSafeHttpUrl(cellText);
                if (safeUrl) {
                    const link = document.createElement('a');
                    link.href = safeUrl;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.textContent = truncateText(cellText, 30);
                    link.className = 'text-blue-600 hover:text-blue-800 hover:underline cursor-pointer';
                    link.title = cellText;
                    td.appendChild(link);
                } else {
                    td.textContent = cellText;
                    td.title = cellText;
                }
            }
            else if (isBooleanField(field, cellValue)) {
                // 布尔字段显示为状态标签
                const badge = document.createElement('span');
                const isTrue = cellValue === true || cellValue === 'true' || cellValue === 1 || cellValue === '1';
                badge.className = `inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                    isTrue ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                }`;
                // 根据字段名称显示不同的文本
                if (field === 'is_active') {
                    badge.textContent = isTrue ? '启用' : '禁用';
                } else {
                    badge.textContent = isTrue ? '是' : '否';
                }
                td.appendChild(badge);
            }
            else if (isTimeField(field)) {
                // 时间字段格式化显示
                if (cellText && cellText !== 'null') {
                    const formattedTime = formatDateTime(cellText);
                    td.textContent = formattedTime;
                    td.className += ' text-gray-600';
                } else {
                    td.textContent = '-';
                    td.className += ' text-gray-400';
                }
            }
            else if (isNumericField(field) && !isStatusField(field)) {
                // 数值字段右对齐
                td.className += ' text-right';
                if (field === 'score' && cellText) {
                    // 分数字段添加颜色
                    const score = parseFloat(cellText);
                    if (score >= 7) {
                        td.className += ' text-red-600 font-medium';
                    } else if (score >= 4) {
                        td.className += ' text-yellow-600 font-medium';
                    } else {
                        td.className += ' text-green-600';
                    }
                }
                td.textContent = cellText || '0';
            }
            else if (isStatusField(field)) {
                // 状态字段显示为彩色标签
                const statusBadge = document.createElement('span');
                const statusConfig = getStatusConfig(field, cellValue);
                statusBadge.className = `inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${statusConfig.class}`;
                statusBadge.textContent = statusConfig.text;
                td.appendChild(statusBadge);
            }
            else if ((renderTableName === 'WechatArticle' || renderTableName === 'WowArticle' || renderTableName === 'RssArticle') && field === 'title') {
                // WechatArticle、WowArticle和RssArticle表的title字段特殊处理
                const safeUrl = getSafeHttpUrl(row['url'] || '');
                if (safeUrl) {
                    const link = document.createElement('a');
                    link.href = safeUrl;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.textContent = truncateText(cellText, 40);
                    link.className = 'text-blue-600 hover:text-blue-800 hover:underline cursor-pointer';
                    link.title = cellText;
                    td.appendChild(link);
                } else {
                    td.textContent = truncateText(cellText, 40);
                    td.title = cellText;
                }
            }
            else if (renderTableName === 'SimcProfile' && field === 'spec') {
                td.innerHTML = renderSpecBadgeHtml(cellText);
            }
            else if (renderTableName === 'SimcProfile' && field === 'fight_style') {
                // SimcProfile表的战斗风格字段特殊处理
                const fightStyleMap = {
                    'Patchwerk': '木桩战斗',
                    'HecticAddCleave': '混乱小怪切换',
                    'HelterSkelter': '随机目标切换',
                    'Ultraxion': '奥创之源',
                    'Beastlord': '兽王',
                    'CastingPatchwerk': '施法木桩'
                };
                const displayText = fightStyleMap[cellText] || cellText;
                const badge = document.createElement('span');
                badge.className = 'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800';
                badge.textContent = displayText;
                badge.title = cellText;
                td.appendChild(badge);
            }
            else if (renderTableName === 'SimcProfile' && (field === 'gear_strength' || field === 'gear_crit' || field === 'gear_haste' || field === 'gear_mastery' || field === 'gear_versatility')) {
                // 属性覆盖未保存时保持为空；0 是用户显式保存的有效覆盖值。
                td.className += ' text-right font-mono';
                td.textContent = cellText === null || cellText === undefined || cellText === '' ? '-' : cellText;
            }
            else if (renderTableName === 'SimcProfile' && field === 'action_list') {
                // SimcProfile表的动作列表字段截断显示
                td.textContent = truncateText(cellText, 30);
                td.title = cellText;
                td.className += ' truncate font-mono text-sm';
            }
            else if (field.toLowerCase().endsWith('_hash') && cellText) {
                td.className += ' font-mono text-xs';
                td.textContent = truncateText(cellText, 16);
                td.title = cellText;
            }
            else if (isJsonField(field)) {
                td.className += ' font-mono text-xs truncate';
                td.textContent = cellText ? truncateText(cellText, 80) : '-';
                td.title = cellText;
                if (!cellText) td.className += ' text-gray-400';
            }
            else if (isLongTextField(field) || cellText.length > 50) {
                // 长文本字段截断显示
                td.textContent = truncateText(cellText, 50);
                td.title = cellText;
                td.className += ' truncate';
            }
            else {
                // 普通字段直接显示
                td.textContent = cellText || '-';
                if (!cellText) {
                    td.className += ' text-gray-400';
                }
            }

            tr.appendChild(td);
        });

        if (showActionColumn) {
            const actionTd = document.createElement('td');
            actionTd.className = 'px-4 py-4 whitespace-nowrap text-sm font-medium w-32 action-col';
            const actions = document.createElement('div');
            actions.className = 'flex space-x-2';

            const addActionButton = (className, colorClass, iconClass, text) => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className = `${className} ${colorClass} transition-colors duration-200`;
                button.dataset.rowId = String(rowId);
                const icon = document.createElement('i');
                icon.className = `${iconClass} mr-1`;
                button.appendChild(icon);
                button.appendChild(document.createTextNode(text));
                actions.appendChild(button);
            };

            if (currentTableCapabilities.can_update) {
                addActionButton('edit-btn', 'text-blue-600 hover:text-blue-900', 'fas fa-edit', '编辑');
                if (renderTableName === 'MonitorTask') {
                    addActionButton('rerun-btn', 'text-orange-600 hover:text-orange-900', 'fas fa-play', '重跑');
                }
            }
            if (currentTableCapabilities.can_delete) {
                addActionButton('delete-btn', 'text-red-600 hover:text-red-900', 'fas fa-trash', '删除');
            }
            actionTd.appendChild(actions);
            tr.appendChild(actionTd);
        }

        tableBody.appendChild(tr);
    });

    // 绑定编辑和删除事件
    bindTableActions();

    // 固定操作列到可视区域右侧
    initStickyActionColumn();
}

/**
 * 固定操作列到可视区域右侧（滚动时动态定位）
 */
function initStickyActionColumn() {
    const scrollContainer = document.querySelector('.overflow-x-auto');
    const actionHeader = document.getElementById('action-col-header');
    const actionCells = document.querySelectorAll('.action-col');
    if (!scrollContainer || !actionHeader || actionCells.length === 0) return;

    function updateSticky() {
        const scrollLeft = scrollContainer.scrollLeft;
        const containerWidth = scrollContainer.clientWidth;
        const table = document.getElementById('data-table');
        if (!table) return;
        const tableWidth = table.scrollWidth;

        // 需要固定的阈值：当表格比容器宽 100px 以上时才启用
        if (tableWidth - containerWidth < 80) {
            // 表格够窄，不需要固定
            actionHeader.style.position = '';
            actionHeader.style.right = '';
            actionHeader.style.zIndex = '';
            actionHeader.style.background = '';
            actionHeader.style.boxShadow = '';
            actionCells.forEach(td => {
                td.style.position = '';
                td.style.right = '';
                td.style.zIndex = '';
                td.style.background = '';
                td.style.boxShadow = '';
            });
            return;
        }

        const colWidth = actionHeader.offsetWidth || 128;
        // right offset = 表格右边超出容器的部分
        const rightOffset = tableWidth - containerWidth - scrollLeft;

        const headerBg = '#f9fafb';
        const cellBg = '#ffffff';
        const evenBg = '#f9fafb';
        const shadow = '-4px 0 8px rgba(0,0,0,0.08)';

        // 固定表头操作列
        actionHeader.style.position = 'sticky';
        actionHeader.style.right = '0px';
        actionHeader.style.zIndex = '20';
        actionHeader.style.background = headerBg;
        actionHeader.style.boxShadow = shadow;

        // 固定每行操作列
        actionCells.forEach(td => {
            const row = td.parentElement;
            const isEven = row && row.classList.contains('bg-gray-50') || (row && row.sectionRowIndex % 2 === 1);
            td.style.position = 'sticky';
            td.style.right = '0px';
            td.style.zIndex = '10';
            td.style.background = isEven ? evenBg : cellBg;
            td.style.boxShadow = shadow;
        });
    }

    scrollContainer.addEventListener('scroll', updateSticky);
    // 初始化时也执行一次
    updateSticky();
}

/**
 * 绑定表格操作事件
 */
function bindTableActions() {
    // 绑定编辑按钮事件
    document.querySelectorAll('.edit-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            const rowId = this.getAttribute('data-row-id');
            openEditRecordModal(rowId);
        });
    });

    // 绑定删除按钮事件
    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            const rowId = this.getAttribute('data-row-id');
            if (confirm('确定要删除这条记录吗？')) {
                deleteTableRow(rowId);
            }
        });
    });

    // 绑定MonitorTask重跑按钮事件
    document.querySelectorAll('.rerun-btn').forEach(btn => {
        btn.addEventListener('click', function(e) {
            e.preventDefault();
            const rowId = this.getAttribute('data-row-id');
            if (!confirm('确认重跑此任务？')) return;
            const csrfToken = getCSRFToken();
            if (!csrfToken) {
                alert('无法获取CSRF令牌，请刷新页面');
                return;
            }
            fetch('/dashboard/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ action: 'force_run_task', task_id: parseInt(rowId) })
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    showMessage(data.message, 'success');
                } else {
                    showMessage(data.error, 'error');
                }
            })
            .catch(err => showMessage('请求失败', 'error'));
        });
    });


}

/**
 * 切换行编辑模式
 */
function toggleRowEdit(row, rowId) {
    openEditRecordModal(rowId);
}

/**
 * 进入编辑模式
 */
function enterEditMode(row, rowId) {
    row.classList.add('editing');

    // 将所有数据单元格转换为输入框
    const dataCells = row.querySelectorAll('td[data-field]');
    dataCells.forEach(cell => {
        const field = cell.getAttribute('data-field');
        const currentValue = cell.textContent.trim();

        // 时间字段使用 datetime-local 输入框
        if (isTimeField(field)) {
            inputElement = document.createElement('input');
            inputElement.type = 'datetime-local';
            // 尝试将已有值转为 datetime-local 格式
            const parsed = parseDateTimeForInput(currentValue);
            if (parsed) {
                inputElement.value = parsed;
            }
            inputElement.className = 'w-full px-2 py-1 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent';
            cell.innerHTML = '';
            cell.appendChild(inputElement);
            return;
        }

        let inputElement;

        // 检查是否为布尔字段
        if (currentValue === 'True' || currentValue === 'False' || currentValue === 'true' || currentValue === 'false' ||
            currentValue === '启用' || currentValue === '禁用' || currentValue === '是' || currentValue === '否') {
            // 创建下拉选择框
            inputElement = document.createElement('select');
            inputElement.className = 'w-full px-2 py-1 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent';

            const trueOption = document.createElement('option');
            const falseOption = document.createElement('option');

            // 根据字段类型设置选项文本和值
            if (field === 'is_active') {
                trueOption.value = 'True';
                trueOption.textContent = '启用';
                trueOption.selected = (currentValue === '启用' || currentValue === 'True' || currentValue === 'true');

                falseOption.value = 'False';
                falseOption.textContent = '禁用';
                falseOption.selected = (currentValue === '禁用' || currentValue === 'False' || currentValue === 'false');
            } else {
                trueOption.value = 'True';
                trueOption.textContent = '是';
                trueOption.selected = (currentValue === '是' || currentValue === 'True' || currentValue === 'true');

                falseOption.value = 'False';
                falseOption.textContent = '否';
                falseOption.selected = (currentValue === '否' || currentValue === 'False' || currentValue === 'false');
            }

            inputElement.appendChild(trueOption);
            inputElement.appendChild(falseOption);
        } else {
            // 创建文本输入框
            inputElement = document.createElement('input');
            inputElement.type = 'text';
            inputElement.value = currentValue;
            inputElement.className = 'w-full px-2 py-1 border border-gray-300 rounded focus:ring-2 focus:ring-blue-500 focus:border-transparent';
        }

        inputElement.setAttribute('data-original-value', currentValue);

        // 替换单元格内容
        cell.innerHTML = '';
        cell.appendChild(inputElement);
    });

    // 更新操作按钮
    const actionCell = row.querySelector('td:last-child');
    actionCell.innerHTML = `
        <div class="flex space-x-2">
            <button class="save-btn text-green-600 hover:text-green-900 transition-colors duration-200" data-row-id="${rowId}">
                <i class="fas fa-save mr-1"></i>保存
            </button>
            <button class="cancel-btn text-gray-600 hover:text-gray-900 transition-colors duration-200" data-row-id="${rowId}">
                <i class="fas fa-times mr-1"></i>取消
            </button>
        </div>
    `;

    // 绑定保存和取消按钮事件
    actionCell.querySelector('.save-btn').addEventListener('click', function(e) {
        e.preventDefault();
        saveRowEdit(row, rowId);
    });

    actionCell.querySelector('.cancel-btn').addEventListener('click', function(e) {
        e.preventDefault();
        cancelRowEdit(row, rowId);
    });
}

/**
 * 保存行编辑
 */
function saveRowEdit(row, rowId) {
    const dataCells = row.querySelectorAll('td[data-field]');
    const updateData = {};

    // 收集编辑后的数据
    dataCells.forEach(cell => {
        const field = cell.getAttribute('data-field');
        const input = cell.querySelector('input, select');
        if (input) {
            // 排除时间字段，这些字段应该是只读的
            if (isTimeField(field)) {
                return; // 跳过时间字段
            }

            let value = input.value;

            // 处理布尔字段
            if (value === 'true' || value === 'True' || value === '启用' || value === '是') {
                value = true;
            } else if (value === 'false' || value === 'False' || value === '禁用' || value === '否') {
                value = false;
            }
            // 处理数字字段
            else if (!isNaN(value) && value !== '') {
                value = Number(value);
            }

            updateData[field] = value;
        }
    });

    // 发送更新请求到服务器
    updateTableRow(rowId, updateData, row);
}

/**
 * 取消行编辑
 */
function cancelRowEdit(row, rowId) {
    row.classList.remove('editing');

    // 恢复原始值
    const dataCells = row.querySelectorAll('td[data-field]');
    dataCells.forEach(cell => {
        const input = cell.querySelector('input, select');
        if (input) {
            const originalValue = input.getAttribute('data-original-value');
            cell.textContent = originalValue;
        }
    });

    // 恢复操作按钮
    const actionCell = row.querySelector('td:last-child');
    actionCell.innerHTML = `
        <div class="flex space-x-2">
            <button class="edit-btn text-blue-600 hover:text-blue-900 transition-colors duration-200" data-row-id="${rowId}">
                <i class="fas fa-edit mr-1"></i>编辑
            </button>
            <button class="delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-row-id="${rowId}">
                <i class="fas fa-trash mr-1"></i>删除
            </button>
        </div>
    `;

    // 重新绑定事件
    bindTableActions();
}

/**
 * 更新表格行数据
 */
function updateTableRow(rowId, updateData, row) {
    // 获取CSRF令牌
    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        alert('无法获取CSRF令牌，请刷新页面');
        return;
    }

    // 构建请求数据
    const requestData = {
        action: 'update_table_row',
        table_name: currentTableName,
        row_id: rowId,
        update_data: updateData
    };

    // 发送更新请求
    fetch('/dashboard/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify(requestData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            // 更新成功，退出编辑模式
            row.classList.remove('editing');

            // 更新单元格显示
            const dataCells = row.querySelectorAll('td[data-field]');
            dataCells.forEach(cell => {
                const field = cell.getAttribute('data-field');
                const input = cell.querySelector('input, select');
                if (input && updateData[field] !== undefined) {
                    const value = updateData[field];

                    // 清空单元格内容
                    cell.innerHTML = '';

                    // 根据字段类型设置显示内容
                    if (isBooleanField(field, value)) {
                        // 布尔字段显示为状态标签
                        const badge = document.createElement('span');
                        const isTrue = value === true || value === 'true' || value === 1 || value === '1';
                        badge.className = `inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
                            isTrue ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
                        }`;
                        // 根据字段名称显示不同的文本
                        if (field === 'is_active') {
                            badge.textContent = isTrue ? '启用' : '禁用';
                        } else {
                            badge.textContent = isTrue ? '是' : '否';
                        }
                        cell.appendChild(badge);
                    } else {
                        // 其他字段直接显示文本
                        cell.textContent = value;
                    }
                }
            });

            // 恢复操作按钮
            const actionCell = row.querySelector('td:last-child');
            actionCell.innerHTML = `
                <div class="flex space-x-2">
                    <button class="edit-btn text-blue-600 hover:text-blue-900 transition-colors duration-200" data-row-id="${rowId}">
                        <i class="fas fa-edit mr-1"></i>编辑
                    </button>
                    <button class="delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-row-id="${rowId}">
                        <i class="fas fa-trash mr-1"></i>删除
                    </button>
                </div>
            `;

            // 重新绑定事件
            bindTableActions();

            // 显示成功消息
            showMessage('数据更新成功', 'success');
        } else {
            alert('更新失败: ' + (data.message || '未知错误'));
        }
    })
    .catch(error => {
        console.error('更新数据时发生错误:', error);
        alert('更新数据时发生错误: ' + error.message);
    });
}

/**
 * 删除表格行
 */
function deleteTableRow(rowId) {
    // 获取CSRF令牌
    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        alert('无法获取CSRF令牌，请刷新页面');
        return;
    }

    // 构建请求数据
    const requestData = {
        action: 'delete_table_row',
        table_name: currentTableName,
        row_id: rowId
    };

    // 发送删除请求
    fetch('/dashboard/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify(requestData)
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            // 删除成功，移除行
            const row = document.querySelector(`tr[data-row-id="${rowId}"]`);
            if (row) {
                row.remove();
            }

            // 显示成功消息
            showMessage('数据删除成功', 'success');
        } else {
            showMessage('删除失败: ' + (data.message || '未知错误'), 'error');
        }
    })
    .catch(error => {
        console.error('删除数据时发生错误:', error);
        showMessage('删除数据时发生错误: ' + error.message, 'error');
    });
}

/**
 * 显示消息提示
 */
function showMessage(message, type = 'info') {
    const root = document.getElementById('toast-root');
    if (!root) {
        return;
    }
    if (!root.classList.contains('fixed')) {
        root.className = 'fixed top-4 right-4 z-50 space-y-2';
    }
    const toast = document.createElement('div');
    const level = String(type || 'info');
    const colorClass = level === 'success'
        ? 'border-green-500'
        : level === 'error'
        ? 'border-red-500'
        : level === 'warning'
        ? 'border-yellow-500'
        : 'border-blue-500';
    toast.className = `bg-white shadow-lg rounded-lg px-4 py-3 border-l-4 ${colorClass} text-gray-800 transition-opacity duration-200`;
    toast.textContent = String(message || '');
    root.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 220);
    }, 2600);
}

let currentSystemAlerts = [];
let currentSystemAlertTotalUnread = 0;
const systemAlertHomeDisplayLimit = 5;
const systemAlertFetchLimit = 100;
let simcUploadSelectedFile = null;

function initSystemAlerts() {
    const list = document.getElementById('system-alert-home-list');
    const empty = document.getElementById('system-alert-home-empty');
    const hint = document.getElementById('system-alert-home-hint');
    if (!list || !empty || !hint) {
        return;
    }
    const refreshBtn = document.getElementById('system-alert-home-refresh');
    const markAllBtn = document.getElementById('system-alert-home-mark-all');

    if (refreshBtn) {
        refreshBtn.addEventListener('click', async function() {
            await fetchUnreadSystemAlerts(false);
        });
    }

    if (markAllBtn) {
        markAllBtn.addEventListener('click', async function() {
            try {
                const resp = await fetch('/api/system-alert/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'mark_all_read' })
                });
                const data = await resp.json();
                if (data && data.success) {
                    await fetchUnreadSystemAlerts(false);
                    showMessage('已全部标记为已读', 'success');
                    return;
                }
                showMessage(data && data.error ? data.error : '操作失败', 'error');
            } catch (e) {
                showMessage('操作失败: ' + (e && e.message ? e.message : '未知错误'), 'error');
            }
        });
    }

    fetchUnreadSystemAlerts();
}

function renderSimcSkillIdentity(action) {
    const name = action.display_name || action.name || '未命名技能';
    const spellId = action.spell_id || '-';
    return `<div class="font-semibold text-gray-900">${escapeHtml(name)} <span class="font-mono text-xs font-normal text-stone-600">技能 ID：${escapeHtml(spellId)}</span></div>`;
}

function renderSimcSkillDamageSnapshot(snapshot) {
    const hasFiniteSimcSkillDamageNumber = value => (
        typeof value === 'number' && Number.isFinite(value)
    );
    const formatSimcSkillDamageNumber = value => {
        if (!hasFiniteSimcSkillDamageNumber(value)) return '-';
        return value.toFixed(2);
    };
    const formatSimcSkillDamageFactor = value => {
        if (!hasFiniteSimcSkillDamageNumber(value)) return '-';
        return value.toFixed(6).replace(/\.?0+$/, '');
    };
    const formatSimcSkillDamagePercent = (value, signed = false) => {
        if (!hasFiniteSimcSkillDamageNumber(value)) return '-';
        const prefix = signed && value > 0 ? '+' : '';
        return `${prefix}${value.toFixed(2)}%`;
    };
    const renderSimcTalentProbeCondition = (runtimeCondition, scenarioTokens, talentName) => {
        const condition = String(runtimeCondition || '').trim();
        if (condition && !condition.startsWith('启用 ')) return condition;
        const tokens = Array.isArray(scenarioTokens) ? scenarioTokens : [];
        const name = String(talentName || '').trim();
        const parts = [];
        const talentLabel = name.endsWith('天赋') ? name : `${name}天赋`;
        if (tokens.length && name && name !== '基础技能') parts.push(`点出${talentLabel}`);
        [...new Set(tokens.map(token => String(token || '').trim()).filter(Boolean))].forEach(token => {
            const separatorIndex = token.indexOf('.');
            const scope = separatorIndex >= 0 ? token.slice(0, separatorIndex) : '';
            const stateToken = separatorIndex >= 0 ? token.slice(separatorIndex + 1) : token;
            if (!stateToken) return;
            const owner = scope === 'debuff' ? '目标' : '自身';
            parts.push(`${owner}存在 ${stateToken} 效果时`);
        });
        if (condition.includes('35%')) parts.push('血量低于35%');
        return parts.join('，');
    };
    const body = document.getElementById('simc-skill-damage-body');
    const identityEl = document.getElementById('simc-skill-damage-identity');
    const unresolvedEl = document.getElementById('simc-skill-damage-unresolved');
    const specSelect = document.getElementById('simc-skill-damage-spec');
    const heroTreeSelect = document.getElementById('simc-skill-damage-hero-tree');
    const searchInput = document.getElementById('simc-skill-damage-search');
    const nameSortButton = document.getElementById('simc-skill-damage-sort-name');
    const finalSortButton = document.getElementById('simc-skill-damage-sort-final');
    const nameSortHeader = document.getElementById('simc-skill-damage-sort-name-header');
    const finalSortHeader = document.getElementById('simc-skill-damage-sort-final-header');
    const globalModifiersEl = document.getElementById('simc-skill-damage-global-modifiers');
    const conditionFilters = document.getElementById('simc-skill-damage-condition-filters');
    const talentFilterList = document.getElementById('simc-skill-damage-filter-talents');
    const buffFilterList = document.getElementById('simc-skill-damage-filter-buffs');
    const targetTabs = Array.from(document.querySelectorAll('.simc-skill-damage-target-tab'));
    if (!body || !identityEl || !unresolvedEl || !specSelect || !heroTreeSelect || !searchInput
        || !nameSortButton || !finalSortButton || !nameSortHeader || !finalSortHeader
        || !globalModifiersEl || !conditionFilters || !talentFilterList || !buffFilterList
        || !targetTabs.length) return;
    const activeTargetTab = targetTabs.find(tab => tab.getAttribute('aria-selected') === 'true');
    const targetCount = String(activeTargetTab ? activeTargetTab.dataset.targetCount : '1');

    const identity = snapshot && snapshot.identity ? snapshot.identity : {};
    identityEl.textContent = snapshot
        ? `SimC ${identity.simc_revision || '-'} · DBC ${identity.game_build || '-'} · schema r${identity.schema_revision || '-'}`
        : '尚无成功快照';
    const unresolved = snapshot && Array.isArray(snapshot.unresolved)
        ? snapshot.unresolved.filter(item => item && typeof item === 'object')
        : [];
    if (unresolved.length) {
        const visibleUnresolved = unresolved.slice(0, 50);
        const details = visibleUnresolved.map(item => {
            const talent = item.talent && typeof item.talent === 'object' ? item.talent : {};
            const talentLabel = talent.name_zh || talent.name || talent.id || '未知天赋';
            const profileLabel = `${item.class || '-'} / ${item.specialization || '-'}`;
            const targetLabel = item.target_health_percentage == null ? '-' : `${item.target_health_percentage}%`;
            return `<li>${escapeHtml(profileLabel)} · ${escapeHtml(talentLabel)} · 目标血量 ${escapeHtml(targetLabel)} · ${escapeHtml(item.reason || 'runtime_unresolved')}</li>`;
        }).join('');
        const omittedLabel = unresolved.length > visibleUnresolved.length
            ? `<div class="mt-2 text-xs">仅展示前 ${visibleUnresolved.length} 项；完整总数以标题为准。</div>`
            : '';
        unresolvedEl.innerHTML = `<div class="font-semibold">未解析 ${unresolved.length} 项：这些条目未生成伤害数值</div><details class="mt-2"><summary class="cursor-pointer font-medium">查看明细</summary><ul class="mt-2 list-disc space-y-1 pl-5 text-xs">${details}</ul>${omittedLabel}</details>`;
        unresolvedEl.classList.remove('hidden');
    } else {
        unresolvedEl.innerHTML = '';
        unresolvedEl.classList.add('hidden');
    }
    const actors = snapshot && Array.isArray(snapshot.actors)
        ? snapshot.actors.filter(item => item && typeof item === 'object')
        : [];
    const previousSpec = specSelect.value;
    const specRows = [];
    const seenSpecs = new Set();
    actors.forEach(actor => {
        const key = `${actor.class || ''}:${actor.specialization || ''}`;
        if (seenSpecs.has(key)) return;
        seenSpecs.add(key);
        specRows.push({key, label: `${actor.class || '-'} / ${actor.specialization || '-'}`});
    });
    specSelect.innerHTML = '<option value="">请选择专精</option>' + specRows.map(row => (
        `<option value="${escapeHtml(row.key)}">${escapeHtml(row.label)}</option>`
    )).join('');
    if (seenSpecs.has(previousSpec)) specSelect.value = previousSpec;

    const selectedSpec = specSelect.value;
    const previousHeroTree = heroTreeSelect.value;
    const selectedActors = actors.filter(actor => (
        `${actor.class || ''}:${actor.specialization || ''}` === selectedSpec
    ));
    const heroTalentTrees = selectedActors.length && Array.isArray(selectedActors[0].hero_talent_trees)
        ? selectedActors[0].hero_talent_trees.filter(item => item && item.id != null)
        : [];
    heroTreeSelect.innerHTML = '<option value="">请选择英雄天赋</option>' + heroTalentTrees.map(tree => (
        `<option value="${escapeHtml(String(tree.id))}">${escapeHtml(tree.name_zh || tree.name || tree.id)}</option>`
    )).join('');
    heroTreeSelect.disabled = !selectedSpec || !heroTalentTrees.length;
    if (heroTalentTrees.some(tree => String(tree.id) === previousHeroTree)) {
        heroTreeSelect.value = previousHeroTree;
    }
    const selectedHeroTree = heroTreeSelect.value;
    const sortMode = nameSortButton.dataset.active === 'true' ? 'name' : 'final';
    const activeSortButton = sortMode === 'name' ? nameSortButton : finalSortButton;
    const sortDirection = activeSortButton.dataset.direction === 'asc' ? 'asc' : 'desc';
    nameSortButton.textContent = `技能 ${sortMode === 'name' ? (sortDirection === 'asc' ? '↑' : '↓') : '↕'}`;
    finalSortButton.textContent = `${targetCount}目标最终归一化伤害 ${sortMode === 'final' ? (sortDirection === 'asc' ? '↑' : '↓') : '↕'}`;
    nameSortHeader.setAttribute('aria-sort', sortMode === 'name'
        ? (sortDirection === 'asc' ? 'ascending' : 'descending')
        : 'none');
    finalSortHeader.setAttribute('aria-sort', sortMode === 'final'
        ? (sortDirection === 'asc' ? 'ascending' : 'descending')
        : 'none');
    globalModifiersEl.classList.add('hidden');
    globalModifiersEl.innerHTML = '';
    conditionFilters.classList.add('hidden');
    talentFilterList.innerHTML = '';
    buffFilterList.innerHTML = '';
    if (!selectedSpec) {
        body.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-stone-500">请先选择专精</td></tr>';
        return;
    }
    if (!selectedHeroTree) {
        body.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-stone-500">请选择英雄天赋</td></tr>';
        return;
    }

    const globalEffectDisplayPriority = effect => (
        effect.source_type === 'talent' ? 3 : effect.source_type === 'specialization_passive' ? 2 : 1
    );
    const globalEffectDisplayKey = effect => {
        const sourceIdentity = [
            String(effect.effect_id || ''),
            String(effect.source_type || ''),
            Number.isInteger(Number(effect.talent_id)) ? Number(effect.talent_id) : 0,
            String(effect.tree_type || ''),
            Number.isInteger(Number(effect.hero_subtree_id)) ? Number(effect.hero_subtree_id) : 0,
        ];
        const runtimeConditions = Array.isArray(effect.runtime_conditions)
            ? effect.runtime_conditions
                .filter(condition => condition && typeof condition === 'object')
                .map(condition => {
                    const spellId = Number(condition.spell_id);
                    const stacks = Number(condition.stacks);
                    return [
                        String(condition.token || '').trim(),
                        String(condition.scope || ''),
                        Number.isInteger(spellId) && spellId > 0 ? spellId : 0,
                        Number.isInteger(stacks) && stacks > 0 ? stacks : 1,
                    ];
                })
                .sort((left, right) => JSON.stringify(left).localeCompare(JSON.stringify(right)))
            : [];
        const runtimeIdentity = runtimeConditions.length
            ? runtimeConditions
            : (Array.isArray(effect.scenario_tokens)
                ? effect.scenario_tokens.map(token => [String(token), '', 0, 1]).sort()
                : []);
        if (!runtimeIdentity.length) return JSON.stringify(sourceIdentity);
        const projectionKeys = (Array.isArray(effect.projections) ? effect.projections : [])
            .filter(projection => projection && typeof projection === 'object')
            .map(projection => [
                String(projection.kind || ''),
                String(projection.evidence_layer || ''),
                hasFiniteSimcSkillDamageNumber(projection.value)
                    ? formatSimcSkillDamageFactor(projection.value)
                    : '',
                hasFiniteSimcSkillDamageNumber(projection.percentage_points)
                    ? formatSimcSkillDamageFactor(projection.percentage_points)
                    : '',
            ].join(':'))
            .sort();
        return JSON.stringify([sourceIdentity, runtimeIdentity, projectionKeys]);
    };
    const globalEffectsByKey = new Map();
    selectedActors.forEach(actor => {
        const effects = Array.isArray(actor.global_skill_effects)
            ? actor.global_skill_effects
            : [];
        effects.forEach(effect => {
            if (!effect || typeof effect !== 'object') return;
            if (effect.hero_subtree_id != null && String(effect.hero_subtree_id) !== selectedHeroTree) return;
            const key = globalEffectDisplayKey(effect);
            if (!key) return;
            const current = globalEffectsByKey.get(key);
            if (!current || globalEffectDisplayPriority(effect) > globalEffectDisplayPriority(current)) {
                globalEffectsByKey.set(key, effect);
            }
        });
    });
    const globalEffects = Array.from(globalEffectsByKey.values());
    if (globalEffects.length) {
        const items = globalEffects.map(effect => {
            const name = effect.display_name || effect.talent_name_zh || effect.talent_name || effect.source_token || '未知全局效果';
            const runtimeConditions = Array.isArray(effect.runtime_conditions)
                ? effect.runtime_conditions.filter(condition => condition && typeof condition === 'object')
                : [];
            const stackLabels = runtimeConditions.map(condition => Number(condition.stacks))
                .filter(stacks => Number.isInteger(stacks) && stacks > 1)
                .map(stacks => `${stacks}层`);
            const displayName = stackLabels.length ? `${name}（${stackLabels.join('，')}）` : name;
            const condition = effect.source_type === 'talent'
                ? renderSimcTalentProbeCondition(effect.runtime_condition, effect.scenario_tokens, name)
                : (effect.runtime_condition || '');
            const projections = (Array.isArray(effect.projections) ? effect.projections : []).map(projection => {
                if (!projection || typeof projection !== 'object') return '';
                if (projection.kind === 'crit_chance') {
                    return `<span class="whitespace-nowrap"><span class="text-xs text-indigo-700">暴击率</span> <span class="font-mono text-indigo-900">${formatSimcSkillDamagePercent(projection.percentage_points, true)}</span></span>`;
                }
                if (projection.kind === 'damage_multiplier') {
                    const label = String(projection.evidence_layer || '').startsWith('base_damage.') ? '基础伤害' : '全局伤害';
                    return `<span class="whitespace-nowrap"><span class="text-xs text-indigo-700">${label}</span> <span class="font-mono text-indigo-900">${formatSimcSkillDamageFactor(projection.value)}×</span></span>`;
                }
                return '';
            }).filter(Boolean).join('<span class="text-indigo-300"> · </span>');
            return `<div class="rounded-lg border border-indigo-200 bg-white/70 px-3 py-2.5"><div class="flex flex-wrap items-start justify-between gap-2"><span class="font-semibold leading-5 text-indigo-950">${escapeHtml(displayName)}</span><span class="flex flex-wrap gap-2">${projections}</span></div>${condition ? `<div class="mt-1 text-xs leading-4 text-amber-800">${escapeHtml(condition)}</div>` : ''}</div>`;
        }).join('');
        globalModifiersEl.innerHTML = `<div class="mb-1 text-sm font-bold text-indigo-950">全局效果</div><div class="mb-3 text-xs text-indigo-700">所有已识别且完成逐技能投影的全技能效果；对应变体不再进入下方条件筛选。</div><div class="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">${items}</div>`;
        globalModifiersEl.classList.remove('hidden');
    }

    const candidateRows = [];
    const talentConditions = new Map();
    const buffConditions = new Map();
    selectedActors.forEach(actor => {
        const actions = Array.isArray(actor.actions)
            ? actor.actions.filter(item => item && typeof item === 'object' && item.player_skill !== false)
            : [];
        actions.forEach(action => {
            const variant = action.variant && typeof action.variant === 'object' ? action.variant : {};
            if (variant.hero_subtree_id != null && String(variant.hero_subtree_id) !== selectedHeroTree) return;
            const heroSubtreeIds = Array.isArray(action.hero_subtree_ids) ? action.hero_subtree_ids : [];
            if (heroSubtreeIds.length && !heroSubtreeIds.some(id => String(id) === selectedHeroTree)) return;

            const conditionKeys = [];
            if (variant.talent_id != null && Number(variant.talent_id) > 0) {
                const conditionKey = `talent:${variant.talent_id}`;
                const talentLabel = variant.talent_name_zh || variant.talent_name || `天赋 ${variant.talent_id}`;
                conditionKeys.push(conditionKey);
                if (!talentConditions.has(conditionKey)) talentConditions.set(conditionKey, String(talentLabel));
            }
            const runtimeConditions = Array.isArray(variant.runtime_conditions)
                ? variant.runtime_conditions.filter(condition => condition && typeof condition === 'object')
                : [];
            runtimeConditions.forEach(condition => {
                const scope = String(condition.scope || '');
                const spellId = Number(condition.spell_id);
                const token = String(condition.token || '').trim();
                const stacksValue = Number(condition.stacks);
                const stacks = Number.isInteger(stacksValue) && stacksValue > 0 ? stacksValue : 1;
                if (!token && !(Number.isInteger(spellId) && spellId > 0)) return;
                const conditionKey = `state:${JSON.stringify([
                    token,
                    scope,
                    Number.isInteger(spellId) && spellId > 0 ? spellId : 0,
                    stacks,
                ])}`;
                const fallbackToken = token.includes('.') ? token.slice(token.indexOf('.') + 1) : token;
                const conditionName = condition.name_zh || condition.name || fallbackToken || spellId;
                const stackLabel = stacks > 1 ? `（${stacks}层）` : '';
                const conditionLabel = `${['target', 'debuff'].includes(scope) ? '目标' : '自身'}：${conditionName}${stackLabel}`;
                if (!conditionKeys.includes(conditionKey)) conditionKeys.push(conditionKey);
                if (!buffConditions.has(conditionKey)) buffConditions.set(conditionKey, String(conditionLabel));
            });
            candidateRows.push({action, variant, rowConditionKeys: conditionKeys});
        });
    });

    const panel = document.getElementById('simc-skill-damage-panel');
    const filterState = panel && panel.__simcSkillDamageFilterState;
    const excludedConditionKeysByScope = filterState && filterState.excludedConditionKeysByScope instanceof Map
        ? filterState.excludedConditionKeysByScope
        : new Map();
    const filterScopeKey = `${selectedSpec}:${selectedHeroTree}`;
    const excludedConditionKeys = excludedConditionKeysByScope.get(filterScopeKey) || new Set();
    const renderConditionFilterOptions = conditionMap => Array.from(conditionMap.entries())
        .sort((left, right) => left[1].localeCompare(right[1], 'zh-CN', {numeric: true, sensitivity: 'base'}))
        .map(([conditionKey, label]) => (
            `<label class="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-stone-300 bg-white px-3 py-2 text-xs text-stone-700 hover:bg-stone-100"><input type="checkbox" data-condition-key="${escapeHtml(conditionKey)}" class="rounded border-stone-300 text-blue-700"${excludedConditionKeys.has(conditionKey) ? '' : ' checked'}><span>${escapeHtml(label)}</span></label>`
        )).join('');
    talentFilterList.innerHTML = renderConditionFilterOptions(talentConditions)
        || '<span class="text-xs text-stone-500">当前列表没有单项天赋条件</span>';
    buffFilterList.innerHTML = renderConditionFilterOptions(buffConditions)
        || '<span class="text-xs text-stone-500">当前列表没有 Buff 条件</span>';
    if (talentConditions.size || buffConditions.size) conditionFilters.classList.remove('hidden');

    const query = String(searchInput.value || '').trim().toLowerCase();
    const rows = [];
    const excludedFilterKeys = excludedConditionKeys;
    candidateRows.forEach(({action, rowConditionKeys}) => {
        const variant = action.variant && typeof action.variant === 'object' ? action.variant : {};
        const haystack = `${action.display_name || ''} ${action.name || ''} ${action.spell_id || ''} ${variant.talent_name || ''} ${variant.talent_name_zh || ''} ${variant.runtime_condition || ''}`.toLowerCase();
        if (query && !haystack.includes(query)) return;
        if (rowConditionKeys.some(key => excludedFilterKeys.has(key))) return;
        const product = action.product && typeof action.product === 'object' ? action.product : {};
        const damageByTarget = product.final_normalized_damage_by_target;
        const selectedFinalDamage = targetCount === '1'
            ? product.final_normalized_damage
            : (damageByTarget && typeof damageByTarget === 'object' ? damageByTarget[targetCount] : null);
        const finalSortValue = hasFiniteSimcSkillDamageNumber(selectedFinalDamage)
            ? selectedFinalDamage
            : Number.NEGATIVE_INFINITY;
        rows.push({action, product, selectedFinalDamage, finalSortValue, sourceIndex: rows.length});
    });
    rows.sort((left, right) => {
        const leftName = String(left.action.display_name || left.action.name || '');
        const rightName = String(right.action.display_name || right.action.name || '');
        const nameDelta = leftName.localeCompare(rightName, 'zh-CN', {numeric: true, sensitivity: 'base'});
        if (sortMode === 'name' && nameDelta) {
            return sortDirection === 'asc' ? nameDelta : -nameDelta;
        }
        if (sortMode === 'final') {
            const damageDelta = left.finalSortValue - right.finalSortValue;
            if (damageDelta) return sortDirection === 'asc' ? damageDelta : -damageDelta;
        }
        if (nameDelta) return nameDelta;
        return left.sourceIndex - right.sourceIndex;
    });
    body.innerHTML = rows.length ? rows.map(({action, product, selectedFinalDamage}) => {
        const skillMeta = renderSimcSkillIdentity(action);
        const variant = action.variant && typeof action.variant === 'object' ? action.variant : {};
        const talentName = variant.talent_name_zh || variant.talent_name || '基础技能';
        const conditionLabel = renderSimcTalentProbeCondition(
            variant.runtime_condition,
            variant.scenario_tokens,
            talentName,
        );
        const fallbackTalentLabel = talentName.endsWith('天赋') ? talentName : `${talentName}天赋`;
        const variantLabel = conditionLabel || (talentName === '基础技能' ? talentName : `点出${fallbackTalentLabel}`);
        const variantCell = `<div class="text-xs text-amber-800">${escapeHtml(variantLabel)}</div>`;
        const normalizedBase = product.normalized_base_damage;
        const finalDamage = selectedFinalDamage;
        let baseDamageCell = '<span class="text-stone-500">DBC 未解析</span>';
        if (hasFiniteSimcSkillDamageNumber(normalizedBase)) {
            baseDamageCell = formatSimcSkillDamageNumber(normalizedBase);
        }
        let formulaCell = '<span class="text-stone-500">公式未解析</span>';
        const formulaComponents = Array.isArray(product.formula_components)
            ? product.formula_components
            : [];
        const formulaGroups = new Map();
        const formulaBaseLabel = '基础伤害';
        formulaComponents.forEach(component => {
            const baseDamage = component.base_damage;
            const componentSingleTarget = component.final_damage;
            const componentDamageByTarget = component.final_damage_by_target;
            const componentFinal = targetCount === '1'
                ? componentSingleTarget
                : (componentDamageByTarget && typeof componentDamageByTarget === 'object'
                    ? componentDamageByTarget[targetCount]
                    : null);
            const runtimeFactors = Array.isArray(component.runtime_factors)
                ? component.runtime_factors.filter(hasFiniteSimcSkillDamageNumber)
                : [];
            if (!hasFiniteSimcSkillDamageNumber(baseDamage)
                || !hasFiniteSimcSkillDamageNumber(componentFinal)) return;
            const multiTargetFactor = targetCount !== '1'
                && hasFiniteSimcSkillDamageNumber(componentSingleTarget)
                && componentSingleTarget !== 0
                ? componentFinal / componentSingleTarget
                : 1;
            const factorKey = JSON.stringify([runtimeFactors, multiTargetFactor]);
            const group = formulaGroups.get(factorKey) || {
                baseDamage: 0,
                finalDamage: 0,
                runtimeFactors,
                multiTargetFactor,
            };
            group.baseDamage += baseDamage;
            group.finalDamage += componentFinal;
            formulaGroups.set(factorKey, group);
        });
        const formulaTerms = Array.from(formulaGroups.values()).map(group => {
            const factorFormula = group.runtimeFactors
                .map(factor => ` × ${formatSimcSkillDamageFactor(factor)}`)
                .join('');
            const multiTargetFormula = targetCount !== '1'
                ? ` × ${formatSimcSkillDamageFactor(group.multiTargetFactor)}（多目标）`
                : '';
            const term = `${formulaBaseLabel} ${formatSimcSkillDamageFactor(group.baseDamage)}${factorFormula}${multiTargetFormula}`;
            return formulaGroups.size > 1 ? `(${term})` : term;
        });
        if (formulaTerms.length && hasFiniteSimcSkillDamageNumber(finalDamage)) {
            formulaCell = `${formulaTerms.join(' + ')} ≈ ${formatSimcSkillDamageFactor(finalDamage)}`;
        }
        return `<tr class="align-top hover:bg-stone-50"><td class="min-w-[220px] px-3 py-3">${skillMeta}</td><td class="min-w-[190px] px-3 py-3">${variantCell}</td><td class="min-w-[180px] px-3 py-3 font-mono">${baseDamageCell}</td><td class="min-w-[260px] px-3 py-3 font-mono">${formulaCell}</td><td class="px-3 py-3 font-mono font-bold text-blue-900">${formatSimcSkillDamageNumber(finalDamage)}</td></tr>`;
    }).join('') : '<tr><td colspan="5" class="px-4 py-8 text-center text-stone-500">没有符合条件的伤害技能</td></tr>';
}

function initSimcSkillDamagePanel() {
    const panel = document.getElementById('simc-skill-damage-panel');
    if (!panel) return;
    const statusEl = document.getElementById('simc-skill-damage-status');
    const generateBtn = document.getElementById('simc-skill-damage-generate');
    const refreshBtn = document.getElementById('simc-skill-damage-refresh');
    const specSelect = document.getElementById('simc-skill-damage-spec');
    const heroTreeSelect = document.getElementById('simc-skill-damage-hero-tree');
    const searchInput = document.getElementById('simc-skill-damage-search');
    const nameSortButton = document.getElementById('simc-skill-damage-sort-name');
    const finalSortButton = document.getElementById('simc-skill-damage-sort-final');
    const conditionFilters = document.getElementById('simc-skill-damage-condition-filters');
    const filterTabs = Array.from(panel.querySelectorAll('.simc-skill-damage-filter-tab'));
    const targetTabs = Array.from(panel.querySelectorAll('.simc-skill-damage-target-tab'));
    const excludedConditionKeysByScope = new Map();
    panel.__simcSkillDamageFilterState = {excludedConditionKeysByScope};
    let currentSnapshot = null;
    let pollTimer = null;
    let pollInFlight = false;

    const stopPolling = () => {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
    };

    const renderStatus = data => {
        generateBtn.classList.toggle('hidden', !data.can_generate);
        const job = data.job;
        const running = job && ['pending', 'running'].includes(job.status);
        generateBtn.disabled = Boolean(running);
        const progressTotal = Number(job && job.total_spec_count) || 0;
        const completedSpecCount = Number(job && job.spec_count) || 0;
        const progressText = progressTotal
            ? `${completedSpecCount} / ${progressTotal} 个专精`
            : `${completedSpecCount} 个专精`;
        const currentSpecText = job && job.current_specialization
            ? ` · 当前：${job.current_specialization}`
            : '';
        statusEl.textContent = running
            ? `正在生成：${job.identity.game_build} · 已完成 ${progressText}${currentSpecText}`
            : currentSnapshot
                ? `最近成功：${currentSnapshot.spec_count || 0} 个专精、${currentSnapshot.action_count || 0} 个技能 · ${currentSnapshot.completed_at || ''}`
                : (job && job.has_error ? '最近一次生成失败；旧成功快照不会被覆盖。' : '当前还没有成功快照。');
        return running;
    };

    let loadSummary;
    const schedulePoll = () => {
        stopPolling();
        pollTimer = setTimeout(async () => {
            pollTimer = null;
            if (pollInFlight) {
                schedulePoll();
                return;
            }
            pollInFlight = true;
            try {
                await loadSummary();
            } catch (_error) {
                schedulePoll();
            } finally {
                pollInFlight = false;
            }
        }, 3000);
    };

    const loadFullSnapshot = async ({managePolling = true} = {}) => {
        const response = await fetch('/api/simc-skill-damage/', { method: 'GET' });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载技能伤害快照失败');
        const data = payload.data || {};
        currentSnapshot = data.snapshot || null;
        renderSimcSkillDamageSnapshot(currentSnapshot);
        const running = renderStatus(data);
        if (managePolling) {
            if (running) schedulePoll();
            else stopPolling();
        }
    };

    loadSummary = async () => {
        const response = await fetch('/api/simc-skill-damage/?summary=1', { method: 'GET' });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载技能伤害生成进度失败');
        const data = payload.data || {};
        const job = data.job;
        const running = renderStatus(data);
        if (job && !running) {
            await loadFullSnapshot({managePolling: false});
        }
        if (running) schedulePoll();
        else stopPolling();
    };

    refreshBtn.addEventListener('click', () => loadFullSnapshot().catch(error => showMessage(error.message, 'error')));
    filterTabs.forEach(tab => tab.addEventListener('click', () => {
        const selectedScope = String(tab.dataset.filterScope || 'talents');
        filterTabs.forEach(candidate => {
            const active = candidate === tab;
            candidate.setAttribute('aria-selected', active ? 'true' : 'false');
            candidate.classList.toggle('bg-blue-700', active);
            candidate.classList.toggle('text-white', active);
            candidate.classList.toggle('text-stone-700', !active);
        });
        panel.querySelectorAll('[data-filter-panel]').forEach(filterPanel => {
            filterPanel.classList.toggle('hidden', filterPanel.dataset.filterPanel !== selectedScope);
        });
    }));
    conditionFilters.addEventListener('change', event => {
        const input = event.target instanceof HTMLInputElement
            ? event.target.closest('input[data-condition-key]')
            : null;
        if (!input) return;
        const conditionKey = String(input.dataset.conditionKey || '');
        const filterScopeKey = `${specSelect.value}:${heroTreeSelect.value}`;
        if (!conditionKey || !specSelect.value || !heroTreeSelect.value) return;
        const excludedConditionKeys = new Set(excludedConditionKeysByScope.get(filterScopeKey) || []);
        if (input.checked) excludedConditionKeys.delete(conditionKey);
        else excludedConditionKeys.add(conditionKey);
        if (excludedConditionKeys.size) excludedConditionKeysByScope.set(filterScopeKey, excludedConditionKeys);
        else excludedConditionKeysByScope.delete(filterScopeKey);
        renderSimcSkillDamageSnapshot(currentSnapshot);
    });
    targetTabs.forEach(tab => tab.addEventListener('click', () => {
        targetTabs.forEach(candidate => {
            const active = candidate === tab;
            candidate.setAttribute('aria-selected', active ? 'true' : 'false');
            candidate.classList.toggle('bg-blue-700', active);
            candidate.classList.toggle('text-white', active);
            candidate.classList.toggle('border', !active);
            candidate.classList.toggle('border-stone-300', !active);
            candidate.classList.toggle('bg-white', !active);
            candidate.classList.toggle('text-stone-700', !active);
        });
        renderSimcSkillDamageSnapshot(currentSnapshot);
    }));
    specSelect.addEventListener('change', () => {
        heroTreeSelect.value = '';
        renderSimcSkillDamageSnapshot(currentSnapshot);
    });
    heroTreeSelect.addEventListener('change', () => renderSimcSkillDamageSnapshot(currentSnapshot));
    searchInput.addEventListener('input', () => renderSimcSkillDamageSnapshot(currentSnapshot));
    nameSortButton.addEventListener('click', () => {
        const alreadyActive = nameSortButton.dataset.active === 'true';
        nameSortButton.dataset.active = 'true';
        finalSortButton.dataset.active = 'false';
        nameSortButton.dataset.direction = alreadyActive && nameSortButton.dataset.direction === 'asc'
            ? 'desc'
            : 'asc';
        renderSimcSkillDamageSnapshot(currentSnapshot);
    });
    finalSortButton.addEventListener('click', () => {
        const alreadyActive = finalSortButton.dataset.active === 'true';
        finalSortButton.dataset.active = 'true';
        nameSortButton.dataset.active = 'false';
        finalSortButton.dataset.direction = alreadyActive && finalSortButton.dataset.direction === 'desc'
            ? 'asc'
            : 'desc';
        renderSimcSkillDamageSnapshot(currentSnapshot);
    });
    generateBtn.addEventListener('click', async () => {
        generateBtn.disabled = true;
        try {
            const response = await fetch('/api/simc-skill-damage/', {
                method: 'POST',
                headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken()},
                body: '{}'
            });
            const payload = await response.json();
            if (!response.ok || !payload.success) throw new Error(payload.error || '触发生成失败');
            showMessage(payload.message, 'success');
            await loadFullSnapshot();
        } catch (error) {
            generateBtn.disabled = false;
            showMessage(error.message, 'error');
        }
    });
    loadFullSnapshot().catch(error => { statusEl.textContent = error.message; });
}

function initSimcBackendUploadTool() {
    const submitBtn = document.getElementById('simc-compile-submit');
    const checkBtn = document.getElementById('simc-compile-check');
    const threadsInput = document.getElementById('simc-compile-threads');
    const noPullCheck = document.getElementById('simc-compile-no-pull');
    const result = document.getElementById('simc-upload-result');
    const autoUpdateToggle = document.getElementById('simc-auto-update-toggle');
    const autoUpdateLabel = document.getElementById('simc-auto-update-label');

    if (!submitBtn || !checkBtn || !threadsInput || !noPullCheck || !result) {
        return;
    }

    let pollInterval = null;

    const renderBackendInfo = (data) => {
        const platform = document.getElementById('simc-upload-platform');
        const currentVersion = document.getElementById('simc-upload-current-version');
        const latestVersion = document.getElementById('simc-upload-latest-version');
        const sourceDir = document.getElementById('simc-upload-source-dir');
        const buildDir = document.getElementById('simc-upload-build-dir');
        const path = document.getElementById('simc-upload-path');
        const status = document.getElementById('simc-upload-status');
        const lastError = document.getElementById('simc-upload-last-error');
        const progressBar = document.getElementById('simc-upload-progress-bar');
        const progressFill = document.getElementById('simc-upload-progress-fill');

        if (platform) platform.textContent = (data && data.platform) ? String(data.platform) : '-';
        if (currentVersion) currentVersion.textContent = (data && data.current_version) ? String(data.current_version) : '-';
        if (latestVersion) latestVersion.textContent = (data && data.latest_version) ? String(data.latest_version) : '-';
        if (sourceDir) sourceDir.textContent = (data && data.source_dir) ? String(data.source_dir) : '-';
        if (buildDir) buildDir.textContent = (data && data.build_dir) ? String(data.build_dir) : '-';
        if (path) path.textContent = (data && data.binary_path) ? String(data.binary_path) : '-';
        if (status) status.textContent = (data && data.update_status) ? String(data.update_status) : '-';

        const progress = Number(data && data.update_progress) || 0;
        if (progressBar && progressFill) {
            if (data && data.is_updating) {
                progressBar.classList.remove('hidden');
                progressFill.style.width = `${progress}%`;
            } else {
                progressBar.classList.add('hidden');
            }
        }

        const err = (data && data.last_error) ? String(data.last_error) : '';
        if (lastError) {
            if (err) {
                lastError.textContent = err;
                lastError.classList.remove('hidden');
            } else {
                lastError.textContent = '';
                lastError.classList.add('hidden');
            }
        }

        const autoUpdate = data && data.auto_update !== undefined ? Boolean(data.auto_update) : true;
        if (autoUpdateToggle && autoUpdateLabel) {
            autoUpdateToggle.setAttribute('data-enabled', autoUpdate ? 'true' : 'false');
            autoUpdateToggle.setAttribute('aria-checked', autoUpdate ? 'true' : 'false');
            const toggleSpan = autoUpdateToggle.querySelector('span');
            if (autoUpdate) {
                autoUpdateToggle.classList.remove('bg-gray-300');
                autoUpdateToggle.classList.add('bg-blue-600');
                if (toggleSpan) {
                    toggleSpan.classList.remove('translate-x-1');
                    toggleSpan.classList.add('translate-x-6');
                }
                autoUpdateLabel.textContent = '已开启';
            } else {
                autoUpdateToggle.classList.remove('bg-blue-600');
                autoUpdateToggle.classList.add('bg-gray-300');
                if (toggleSpan) {
                    toggleSpan.classList.remove('translate-x-6');
                    toggleSpan.classList.add('translate-x-1');
                }
                autoUpdateLabel.textContent = '已关闭';
            }
        }

        const isUpdating = data && data.is_updating;
        submitBtn.disabled = isUpdating;
        checkBtn.disabled = isUpdating;
        threadsInput.disabled = isUpdating;
        noPullCheck.disabled = isUpdating;
        if (autoUpdateToggle) {
            autoUpdateToggle.disabled = isUpdating;
        }

        if (isUpdating && !pollInterval) {
            startPolling();
        } else if (!isUpdating && pollInterval) {
            stopPolling();
        }
    };

    const fetchBackendInfo = async () => {
        try {
            const resp = await fetch('/api/simc-backend-binary/', { method: 'GET' });
            const data = await resp.json();
            if (data && data.success) {
                renderBackendInfo(data.data || {});
            }
        } catch (e) {
            return;
        }
    };

    const startPolling = () => {
        if (pollInterval) return;
        pollInterval = setInterval(fetchBackendInfo, 3000);
    };

    const stopPolling = () => {
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    };

    const triggerUpdate = async (checkOnly) => {
        const threads = Math.max(1, Math.min(8, parseInt(threadsInput.value) || 2));
        const noPull = noPullCheck.checked;

        try {
            const csrfToken = getCSRFToken();
            if (!csrfToken) {
                showMessage('无法获取CSRF令牌，请刷新页面', 'error');
                return;
            }

            const payload = {
                action: checkOnly ? 'check' : 'update',
                threads: threads,
                no_pull: noPull
            };

            const resp = await fetch('/api/simc-backend-binary/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify(payload)
            });

            const data = await resp.json();
            if (data && data.success) {
                showMessage(data.message || (checkOnly ? '已开始检查' : '已开始编译更新'), 'success');
                result.textContent = data.message || '';
                setTimeout(fetchBackendInfo, 1000);
                if (!checkOnly) {
                    startPolling();
                }
            } else {
                const err = data && data.error ? String(data.error) : (checkOnly ? '检查失败' : '触发编译失败');
                showMessage(err, 'error');
                result.textContent = err;
            }
        } catch (e) {
            const err = e && e.message ? e.message : (checkOnly ? '检查失败' : '触发编译失败');
            showMessage(err, 'error');
            result.textContent = err;
        }
    };

    const toggleAutoUpdate = async () => {
        const currentEnabled = autoUpdateToggle.getAttribute('data-enabled') === 'true';
        const newEnabled = !currentEnabled;

        try {
            const csrfToken = getCSRFToken();
            if (!csrfToken) {
                showMessage('无法获取CSRF令牌，请刷新页面', 'error');
                return;
            }

            autoUpdateToggle.disabled = true;

            const resp = await fetch('/api/simc-backend-binary/', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken
                },
                body: JSON.stringify({
                    action: 'set_auto_update',
                    auto_update: newEnabled
                })
            });

            const data = await resp.json();
            if (data && data.success) {
                showMessage(data.message || `自动更新已${newEnabled ? '开启' : '关闭'}`, 'success');
                if (data.data) {
                    renderBackendInfo(data.data);
                } else {
                    await fetchBackendInfo();
                }
            } else {
                const err = data && data.error ? String(data.error) : '切换自动更新失败';
                showMessage(err, 'error');
                autoUpdateToggle.disabled = false;
            }
        } catch (e) {
            const err = e && e.message ? e.message : '切换自动更新失败';
            showMessage(err, 'error');
            autoUpdateToggle.disabled = false;
        }
    };

    checkBtn.addEventListener('click', () => triggerUpdate(true));
    submitBtn.addEventListener('click', () => triggerUpdate(false));
    if (autoUpdateToggle) {
        autoUpdateToggle.addEventListener('click', toggleAutoUpdate);
    }

    fetchBackendInfo();
}

async function fetchUnreadSystemAlerts(silent = true) {
    try {
        const resp = await fetch(`/api/system-alert/?limit=${systemAlertFetchLimit}`, { method: 'GET' });
        const data = await resp.json();
        if (!data || !data.success) {
            if (!silent) {
                showMessage(data && data.error ? data.error : '获取报警失败', 'error');
            }
            return;
        }
        currentSystemAlerts = Array.isArray(data.data) ? data.data : [];
        currentSystemAlertTotalUnread = Number(data.total_unread || 0);
        renderSystemAlertHome();
    } catch (e) {
        if (!silent) {
            showMessage('获取报警失败: ' + (e && e.message ? e.message : '未知错误'), 'error');
        }
        return;
    }
}

function renderSystemAlertHome() {
    const list = document.getElementById('system-alert-home-list');
    const empty = document.getElementById('system-alert-home-empty');
    const hint = document.getElementById('system-alert-home-hint');
    if (!list || !empty || !hint) {
        return;
    }

    list.innerHTML = '';
    const alerts = Array.isArray(currentSystemAlerts) ? currentSystemAlerts : [];
    if (!alerts.length) {
        empty.classList.remove('hidden');
        hint.textContent = '';
        return;
    }
    empty.classList.add('hidden');
    const shownCount = Math.min(systemAlertHomeDisplayLimit, alerts.length);
    const totalUnread = currentSystemAlertTotalUnread > 0 ? currentSystemAlertTotalUnread : alerts.length;
    hint.textContent = `展示最近 ${shownCount} 条未读（已加载 ${alerts.length} / 共 ${totalUnread} 条）`;

    alerts.slice(0, shownCount).forEach(a => {
        const level = Number(a.level || 3);
        const borderClass = level >= 3 ? 'border-red-500' : level === 2 ? 'border-yellow-500' : 'border-blue-500';
        const badgeClass = level >= 3 ? 'bg-red-50 text-red-700' : level === 2 ? 'bg-yellow-50 text-yellow-700' : 'bg-blue-50 text-blue-700';
        const badgeText = level >= 3 ? '致命' : level === 2 ? '警告' : '提示';

        const wrap = document.createElement('div');
        wrap.className = `bg-white border-l-4 ${borderClass} rounded-lg shadow-sm p-4`;

        const header = document.createElement('div');
        header.className = 'flex items-start justify-between gap-3';

        const left = document.createElement('div');
        left.className = 'min-w-0';

        const title = document.createElement('div');
        title.className = 'text-sm font-semibold text-gray-900 break-words';
        title.textContent = String(a.title || a.category || '报警');

        const meta = document.createElement('div');
        meta.className = 'mt-1 text-xs text-gray-500';
        const count = a.count ? `触发 ${a.count} 次` : '';
        const last = a.last_seen_at ? `最近: ${a.last_seen_at}` : '';
        meta.textContent = [count, last].filter(Boolean).join(' · ');

        left.appendChild(title);
        left.appendChild(meta);

        const right = document.createElement('div');
        right.className = 'flex items-center gap-2 flex-shrink-0';

        const badge = document.createElement('span');
        badge.className = `px-2 py-0.5 rounded-full text-xs font-medium ${badgeClass}`;
        badge.textContent = badgeText;

        const btn = document.createElement('button');
        btn.className = 'px-3 py-1.5 bg-gray-900 text-white rounded-md text-xs hover:bg-gray-800 transition-colors duration-200';
        btn.textContent = '已读';
        btn.addEventListener('click', async function() {
            try {
                const resp = await fetch('/api/system-alert/', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'mark_read', id: a.id })
                });
                const data = await resp.json();
                if (data && data.success) {
                    await fetchUnreadSystemAlerts(false);
                    showMessage('已标记为已读', 'success');
                    return;
                }
                showMessage(data && data.error ? data.error : '操作失败', 'error');
            } catch (e) {
                showMessage('操作失败: ' + (e && e.message ? e.message : '未知错误'), 'error');
            }
        });

        right.appendChild(badge);
        right.appendChild(btn);

        header.appendChild(left);
        header.appendChild(right);

        const content = document.createElement('div');
        content.className = 'mt-3 text-sm text-gray-700 whitespace-pre-wrap break-words';
        content.textContent = String(a.content || '');

        wrap.appendChild(header);
        wrap.appendChild(content);
        list.appendChild(wrap);
    });
}

/**
 * 更新系统状态信息
 */
function updateSystemStatus() {
    // 模拟数据，实际应用中应该从服务器获取
    const uptime = Math.floor(Math.random() * 30) + 1;
    const cpuUsage = Math.floor(Math.random() * 100) + '%';
    const memoryUsage = (Math.random() * 7 + 1).toFixed(1) + 'GB/8GB';

    // 更新DOM元素
    const uptimeEl = document.querySelector('#system-uptime');
    const uptimeHomeEl = document.querySelector('#system-uptime-home');
    const cpuEl = document.querySelector('#system-cpu');
    const memoryEl = document.querySelector('#system-memory');

    const uptimeText = `${uptime}天`;
    if (uptimeEl) uptimeEl.textContent = `服务运行时间: ${uptimeText}`;
    if (uptimeHomeEl) uptimeHomeEl.textContent = uptimeText;
    if (cpuEl) cpuEl.textContent = `CPU使用率: ${cpuUsage}`;
    if (memoryEl) memoryEl.textContent = `内存使用: ${memoryUsage}`;
}

/**
 * 更新最近活动信息
 */
function updateRecentActivities() {
    // 实际应用中应该从服务器获取数据
    // 这里只是模拟数据
    const activities = [
        { time: formatDateTime(new Date()), action: '收到新的webhook请求' },
        { time: formatDateTime(new Date(Date.now() - 1000 * 60 * 30)), action: '系统自动更新完成' },
        { time: formatDateTime(new Date(Date.now() - 1000 * 60 * 60)), action: '用户登录' }
    ];

    // 更新DOM元素
    const activitiesEl = document.querySelector('#recent-activities-list');
    if (activitiesEl) {
        activitiesEl.innerHTML = '';
        activities.forEach(activity => {
            const li = document.createElement('li');
            li.textContent = `${activity.time} - ${activity.action}`;
            activitiesEl.appendChild(li);
        });
    }
}

/**
 * 更新统计数据
 */
function updateStatistics() {
    // 实际应用中应该从服务器获取数据
    // 这里只是模拟数据
    const totalRequests = Math.floor(Math.random() * 10000) + 1000;
    const todayRequests = Math.floor(Math.random() * 200);
    const avgResponseTime = (Math.random() * 2).toFixed(1);

    // 更新DOM元素
    const totalEl = document.querySelector('#stat-total');
    const todayEl = document.querySelector('#stat-today');
    const avgTimeEl = document.querySelector('#stat-avg-time');

    if (totalEl) totalEl.textContent = `总请求数: ${totalRequests.toLocaleString()}`;
    if (todayEl) todayEl.textContent = `今日请求: ${todayRequests}`;
    if (avgTimeEl) avgTimeEl.textContent = `平均响应时间: ${avgResponseTime}秒`;
}

/**
 * 判断是否为URL字段
 */
function isUrlField(field) {
    const urlFields = ['url', 'link', 'target', 'source_url'];
    return urlFields.includes(field.toLowerCase());
}

function getSafeHttpUrl(value) {
    try {
        const parsed = new URL(String(value || '').trim(), window.location.origin);
        return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : null;
    } catch (error) {
        return null;
    }
}

/**
 * 判断是否为布尔字段
 */
function isBooleanField(field, value) {
    const booleanFields = ['is_active', 'is_login', 'is_poc', 'is_exp', 'is_verify', 'is_zombie'];
    return isModelBooleanField(field) ||
           booleanFields.includes(field.toLowerCase()) ||
           typeof value === 'boolean' ||
           value === 'true' || value === 'false';
}

/**
 * 判断是否为时间字段
 */
function isTimeField(field) {
    if (isModelDateTimeField(field) || isModelDateField(field) || isModelTimeOnlyField(field)) {
        return true;
    }
    const timeFields = ['time', 'date', 'created_at', 'updated_at', 'publish_time', 'last_scan_time', 'last_spider_time', 'last_publish_time', 'create_time'];
    // 排除wait_time，它应该显示为数值而不是时间
    if (field.toLowerCase() === 'wait_time') {
        return false;
    }
    // 排除SimcProfile表中的time字段，它是纯数字而不是日期
    if (currentTableName === 'SimcProfile' && field.toLowerCase() === 'time') {
        return false;
    }
    return timeFields.some(timeField => field.toLowerCase().includes(timeField));
}

/**
 * 判断是否为数值字段
 */
function isNumericField(field) {
    const numericFields = ['score', 'severity', 'wait_time', 'type', 'state', 'flag', 'room_member_count', 'msg_type', 'active_type'];
    return isModelNumericField(field) || numericFields.includes(field.toLowerCase());
}

/**
 * 判断是否为状态字段
 */
function isStatusField(field) {
    const statusFields = ['status', 'login_status', 'state'];
    return statusFields.includes(field.toLowerCase());
}

/**
 * 判断是否为长文本字段
 */
function isLongTextField(field) {
    const longTextFields = ['description', 'content_html', 'solutions', 'summary', 'digest', 'reference'];
    return longTextFields.includes(field.toLowerCase());
}

/**
 * 截断文本
 */
function truncateText(text, maxLength) {
    if (!text || text.length <= maxLength) {
        return text;
    }
    return text.substring(0, maxLength) + '...';
}

/**
 * 将显示用的日期字符串转为 datetime-local 输入框格式 (YYYY-MM-DDTHH:MM)
 */
function parseDateTimeForInput(displayStr) {
    if (!displayStr || displayStr === '-' || displayStr === 'null') return '';
    const raw = String(displayStr).trim();
    // "2026-06-12 18:30:00" -> "2026-06-12T18:30"
    const m = raw.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})(:\d{2})?$/);
    if (m) return m[1] + 'T' + m[2];
    // 已经是 ISO 格式
    const m2 = raw.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
    if (m2) return m2[1] + 'T' + m2[2];
    return '';
}

/**
 * 格式化日期时间
 */
function formatDateTime(dateString) {
    if (!dateString || dateString === 'null' || dateString === 'undefined' || dateString === undefined) {
        return '';
    }

    try {
        const raw = String(dateString).trim();
        if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
        if (/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?$/.test(raw)) {
            const parts = raw.split(/\s+/);
            const day = parts[0] || '';
            const time = parts[1] || '';
            const hms = time.length >= 8 ? time.slice(0, 8) : (time.length >= 5 ? time.slice(0, 5) : time);
            return day && hms ? `${day} ${hms}` : raw;
        }
        if (/^\d{2}:\d{2}(:\d{2})?$/.test(raw)) return raw;

        let normalized = raw;
        if (/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?$/.test(normalized)) {
            normalized = normalized.replace(' ', 'T');
        }
        normalized = normalized.replace(/\s+/g, ' ').replace(/ /g, 'T');

        const date = new Date(normalized);
        if (isNaN(date.getTime())) {
            return raw;
        }

        const dtf = new Intl.DateTimeFormat('zh-CN', {
            timeZone: 'Asia/Shanghai',
            hour12: false,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
        const parts = dtf.formatToParts(date);
        const pick = (t) => (parts.find(p => p.type === t)?.value || '');
        const y = pick('year');
        const m = pick('month');
        const d = pick('day');
        const hh = pick('hour');
        const mm = pick('minute');
        const ss = pick('second');
        if (!y || !m || !d) return raw;
        return ss ? `${y}-${m}-${d} ${hh}:${mm}:${ss}` : `${y}-${m}-${d} ${hh}:${mm}`;
    } catch (e) {
        return String(dateString);
    }
}

function formatShanghaiHms(dateInput) {
    try {
        const date = dateInput ? new Date(dateInput) : new Date();
        if (isNaN(date.getTime())) return '';
        return date.toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) {
        return '';
    }
}

/**
 * 获取状态配置
 */
function getStatusConfig(field, value) {
    const configs = {
        'login_status': {
            0: { text: '未登录', class: 'bg-gray-100 text-gray-800' },
            1: { text: '已登录', class: 'bg-green-100 text-green-800' },
            2: { text: '登录失败', class: 'bg-red-100 text-red-800' }
        },
        'state': {
            0: { text: '正常', class: 'bg-green-100 text-green-800' },
            1: { text: '异常', class: 'bg-red-100 text-red-800' },
            2: { text: '待处理', class: 'bg-yellow-100 text-yellow-800' }
        },
        'status': {
            0: { text: '禁用', class: 'bg-gray-100 text-gray-800' },
            1: { text: '启用', class: 'bg-green-100 text-green-800' }
        }
    };

    const fieldConfig = configs[field.toLowerCase()];
    if (fieldConfig && fieldConfig[value]) {
        return fieldConfig[value];
    }

    // 默认配置
    return {
        text: String(value),
        class: 'bg-gray-100 text-gray-800'
    };
}

/**
 * 更新分页控件
 */
function updatePagination() {
    const paginationContainer = document.getElementById('pagination-container');
    if (!paginationContainer) {
        return;
    }

    // 更新分页信息显示
    const pageInfo = document.getElementById('page-info');
    if (pageInfo) {
        const startRecord = (currentPage - 1) * pageSize + 1;
        const endRecord = Math.min(currentPage * pageSize, totalCount);
        pageInfo.textContent = `显示 ${startRecord}-${endRecord} 条，共 ${totalCount} 条记录`;
    }

    // 更新分页按钮
    const paginationButtons = document.getElementById('pagination-buttons');
    if (!paginationButtons) {
        return;
    }

    paginationButtons.innerHTML = '';

    // 如果只有一页，不显示分页按钮
    if (totalPages <= 1) {
        return;
    }

    // 上一页按钮
    const prevButton = document.createElement('button');
    prevButton.className = `px-3 py-1 mx-1 rounded ${currentPage === 1 ? 'bg-gray-300 text-gray-500 cursor-not-allowed' : 'bg-blue-500 text-white hover:bg-blue-600'}`;
    prevButton.textContent = '上一页';
    prevButton.disabled = currentPage === 1;
    prevButton.addEventListener('click', () => {
        if (currentPage > 1) {
            fetchTableData(currentTableName, currentPage - 1);
        }
    });
    paginationButtons.appendChild(prevButton);

    // 页码按钮
    const maxVisiblePages = 5;
    let startPage = Math.max(1, currentPage - Math.floor(maxVisiblePages / 2));
    let endPage = Math.min(totalPages, startPage + maxVisiblePages - 1);

    // 调整起始页
    if (endPage - startPage + 1 < maxVisiblePages) {
        startPage = Math.max(1, endPage - maxVisiblePages + 1);
    }

    // 如果起始页大于1，显示第一页和省略号
    if (startPage > 1) {
        const firstPageButton = document.createElement('button');
        firstPageButton.className = 'px-3 py-1 mx-1 rounded bg-white border border-gray-300 text-gray-700 hover:bg-gray-50';
        firstPageButton.textContent = '1';
        firstPageButton.addEventListener('click', () => {
            fetchTableData(currentTableName, 1);
        });
        paginationButtons.appendChild(firstPageButton);

        if (startPage > 2) {
            const ellipsis = document.createElement('span');
            ellipsis.className = 'px-3 py-1 mx-1 text-gray-500';
            ellipsis.textContent = '...';
            paginationButtons.appendChild(ellipsis);
        }
    }

    // 显示页码按钮
    for (let i = startPage; i <= endPage; i++) {
        const pageButton = document.createElement('button');
        pageButton.className = `px-3 py-1 mx-1 rounded ${i === currentPage ? 'bg-blue-500 text-white' : 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-50'}`;
        pageButton.textContent = i;
        pageButton.addEventListener('click', () => {
            fetchTableData(currentTableName, i);
        });
        paginationButtons.appendChild(pageButton);
    }

    // 如果结束页小于总页数，显示省略号和最后一页
    if (endPage < totalPages) {
        if (endPage < totalPages - 1) {
            const ellipsis = document.createElement('span');
            ellipsis.className = 'px-3 py-1 mx-1 text-gray-500';
            ellipsis.textContent = '...';
            paginationButtons.appendChild(ellipsis);
        }

        const lastPageButton = document.createElement('button');
        lastPageButton.className = 'px-3 py-1 mx-1 rounded bg-white border border-gray-300 text-gray-700 hover:bg-gray-50';
        lastPageButton.textContent = totalPages;
        lastPageButton.addEventListener('click', () => {
            fetchTableData(currentTableName, totalPages);
        });
        paginationButtons.appendChild(lastPageButton);
    }

    // 下一页按钮
    const nextButton = document.createElement('button');
    nextButton.className = `px-3 py-1 mx-1 rounded ${currentPage === totalPages ? 'bg-gray-300 text-gray-500 cursor-not-allowed' : 'bg-blue-500 text-white hover:bg-blue-600'}`;
    nextButton.textContent = '下一页';
    nextButton.disabled = currentPage === totalPages;
    nextButton.addEventListener('click', () => {
        if (currentPage < totalPages) {
            fetchTableData(currentTableName, currentPage + 1);
        }
    });
    paginationButtons.appendChild(nextButton);
}

/**
 * 获取Django CSRF Token
 */
function getCSRFToken() {
    // 首先尝试从cookie中获取
    const cookies = document.cookie.split(';');
    for (let i = 0; i < cookies.length; i++) {
        const cookie = cookies[i].trim();
        if (cookie.startsWith('csrftoken=')) {
            return cookie.substring('csrftoken='.length, cookie.length);
        }
    }

    // 如果cookie中没有，尝试从meta标签获取
    const metaToken = document.querySelector('meta[name="csrf-token"]');
    if (metaToken) {
        return metaToken.getAttribute('content');
    }

    // 如果meta标签中没有，尝试从input标签获取
    const inputToken = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (inputToken) {
        return inputToken.value;
    }

    console.error('无法获取CSRF令牌');
    return null;
}

/**
 * 初始化SimC APL转换工具
 */
function initSimcAplConverter() {
    const modeSelect = document.getElementById('apl-convert-mode');
    const switchBtn = document.getElementById('apl-convert-switch');
    const execBtn = document.getElementById('apl-convert-exec');
    const statusText = document.getElementById('apl-convert-status');
    const sourceLabel = document.getElementById('apl-source-label');
    const targetLabel = document.getElementById('apl-target-label');
    const clearAllBtn = document.getElementById('clear-all');
    const copyResultBtn = document.getElementById('copy-result');
    const simcInput = document.getElementById('simc-input');
    const aplInput = document.getElementById('apl-input');

    if (!modeSelect || !switchBtn || !execBtn || !statusText || !sourceLabel || !targetLabel || !clearAllBtn || !copyResultBtn || !simcInput || !aplInput) {
        return;
    }

    function setStatus(text, level) {
        statusText.textContent = text || '';
        statusText.classList.remove('text-gray-500', 'text-blue-600', 'text-green-600', 'text-red-600', 'text-amber-600');
        const levelMap = {
            loading: 'text-blue-600',
            success: 'text-green-600',
            error: 'text-red-600',
            warning: 'text-amber-600',
            info: 'text-gray-500'
        };
        statusText.classList.add(levelMap[level] || 'text-gray-500');
    }

    function refreshModeDisplay() {
        const mode = modeSelect.value || 'apl_to_cn';
        if (mode === 'cn_to_apl') {
            sourceLabel.textContent = '中文描述（原文）';
            targetLabel.textContent = 'APL结果';
            aplInput.placeholder = '请输入中文动作说明，例如：起手冲锋后释放爆发技能...';
            simcInput.placeholder = '生成的APL结果将显示在这里...';
        } else {
            sourceLabel.textContent = 'APL代码（原文）';
            targetLabel.textContent = '中文结果';
            aplInput.placeholder = '请输入APL格式的代码...';
            simcInput.placeholder = '翻译结果将显示在这里...';
        }
    }

    async function executeConvert() {
        const mode = modeSelect.value || 'apl_to_cn';
        const sourceText = String(aplInput.value || '').trim();
        if (!sourceText) {
            const sourceName = mode === 'cn_to_apl' ? '中文描述' : 'APL代码';
            showMessage(`请先输入${sourceName}`, 'warning');
            setStatus('等待输入内容', 'warning');
            return false;
        }
        try {
            execBtn.disabled = true;
            setStatus('翻译中...', 'loading');
            const result = await convertText(sourceText, mode);
            simcInput.value = result || '';
            setStatus('翻译完成', 'success');
            showMessage('翻译成功', 'success');
            return true;
        } catch (error) {
            setStatus('翻译失败', 'error');
            showMessage('翻译失败: ' + error.message, 'error');
            return false;
        } finally {
            execBtn.disabled = false;
        }
    }

    modeSelect.addEventListener('change', function() {
        refreshModeDisplay();
        const modeText = this.value === 'cn_to_apl' ? '中文 -> APL' : 'APL -> 中文';
        setStatus(`当前方向：${modeText}`, 'info');
    });

    switchBtn.addEventListener('click', function() {
        modeSelect.value = modeSelect.value === 'apl_to_cn' ? 'cn_to_apl' : 'apl_to_cn';
        modeSelect.dispatchEvent(new Event('change'));
    });

    execBtn.addEventListener('click', function() {
        executeConvert();
    });

    aplInput.addEventListener('keydown', function(event) {
        if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
            event.preventDefault();
            executeConvert();
        }
    });

    clearAllBtn.addEventListener('click', function() {
        aplInput.value = '';
        simcInput.value = '';
        setStatus('已清空，准备就绪', 'info');
        showMessage('已清空所有内容', 'info');
    });

    copyResultBtn.addEventListener('click', function() {
        const resultText = String(simcInput.value || '').trim();
        if (!resultText) {
            showMessage('当前没有可复制的翻译结果', 'warning');
            return;
        }
        navigator.clipboard.writeText(resultText)
            .then(() => showMessage('结果已复制到剪贴板', 'success'))
            .catch(() => showMessage('复制失败', 'error'));
    });

    window.__previewCurrentConverterContent = executeConvert;
    refreshModeDisplay();
    setStatus('准备就绪', 'info');

}

async function previewCurrentConverterContent() {
    if (typeof window.__previewCurrentConverterContent !== 'function') {
        showMessage('翻译器尚未初始化', 'warning');
        return false;
    }
    return window.__previewCurrentConverterContent();
}

/**
 * 文本转换函数
 */
async function convertText(text, conversionType, spec = '') {
    try {
        const response = await fetch('/api/convert-text/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCSRFToken()
            },
            body: JSON.stringify({
                text: text,
                conversion_type: conversionType,
                spec: spec
            })
        });

        if (!response.ok) {
            throw new Error('网络请求失败');
        }

        const data = await response.json();

        if (data.success) {
            return data.result;
        } else {
            throw new Error(data.error || '转换失败');
        }
    } catch (error) {
        throw error;
    }
}

/**
 * 初始化新增记录功能
 */
function initAddRecord() {
    const addRecordBtn = document.getElementById('add-record-btn');
    const modal = document.getElementById('add-record-modal');
    const closeModalBtn = document.getElementById('close-modal-btn');
    const cancelBtn = document.getElementById('cancel-btn');
    const addRecordForm = document.getElementById('add-record-form');

    if (!addRecordBtn || !modal) {
        return; // 如果元素不存在，直接返回
    }

    // 新增记录按钮点击事件
    addRecordBtn.addEventListener('click', function() {
        if (!currentTableName) {
            showMessage('请先选择一个表', 'warning');
            return;
        }
        openAddRecordModal();
    });

    // 关闭弹窗事件：只允许明确点击关闭/取消按钮，避免误点遮罩丢失已编辑内容
    closeModalBtn.addEventListener('click', closeAddRecordModal);
    cancelBtn.addEventListener('click', closeAddRecordModal);

    // 表单提交事件
    addRecordForm.addEventListener('submit', function(e) {
        e.preventDefault();
        submitAddRecord();
    });
}

/**
 * 打开新增记录弹窗
 */
function openAddRecordModal() {
    if (!currentTableCapabilities.can_create) {
        showMessage(currentTableCapabilities.read_only_reason || '该表不支持手工新增', 'warning');
        return;
    }

    const modal = document.getElementById('add-record-modal');
    const modalTitle = document.getElementById('modal-title');
    const formFields = document.getElementById('form-fields');

    // 设置弹窗标题
    modalTitle.textContent = `新增${currentTableDisplayName || currentTableName}记录`;

    // 生成表单字段
    generateFormFields(formFields);

    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.body.style.overflow = 'hidden';
}

/**
 * 关闭新增记录弹窗
 */
function closeAddRecordModal() {
    const modal = document.getElementById('add-record-modal');
    const addRecordForm = document.getElementById('add-record-form');

    // 隐藏弹窗
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    document.body.style.overflow = '';

    // 重置表单
    addRecordForm.reset();
}

function initEditRecord() {
    const modal = document.getElementById('edit-record-modal');
    const closeModalBtn = document.getElementById('close-edit-modal-btn');
    const cancelBtn = document.getElementById('cancel-edit-btn');
    const editRecordForm = document.getElementById('edit-record-form');

    if (!modal || !editRecordForm) {
        return;
    }

    if (closeModalBtn) closeModalBtn.addEventListener('click', closeEditRecordModal);
    if (cancelBtn) cancelBtn.addEventListener('click', closeEditRecordModal);

    editRecordForm.addEventListener('submit', function(e) {
        e.preventDefault();
        submitEditRecord();
    });
}

function openEditRecordModal(rowId) {
    const modal = document.getElementById('edit-record-modal');
    const modalTitle = document.getElementById('edit-modal-title');
    const formFields = document.getElementById('edit-form-fields');
    const idInput = document.getElementById('edit-row-id');

    if (!modal || !modalTitle || !formFields || !idInput) {
        return;
    }

    const rowData = currentTableRowMap.get(String(rowId));
    if (!rowData) {
        showMessage('无法获取当前行数据，请刷新后重试', 'error');
        return;
    }

    currentEditRowId = String(rowId);
    idInput.value = currentEditRowId;
    modalTitle.textContent = `编辑${currentTableDisplayName || currentTableName}记录`;

    generateEditFormFields(formFields, rowData);
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    document.body.style.overflow = 'hidden';
}

function closeEditRecordModal() {
    const modal = document.getElementById('edit-record-modal');
    const form = document.getElementById('edit-record-form');
    const fields = document.getElementById('edit-form-fields');
    const idInput = document.getElementById('edit-row-id');

    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
    document.body.style.overflow = '';
    if (form) form.reset();
    if (fields) fields.innerHTML = '';
    if (idInput) idInput.value = '';
    currentEditRowId = null;
}

function generateEditFormFields(container, rowData) {
    container.innerHTML = '';

    if (!currentTableColumns || currentTableColumns.length === 0) {
        container.innerHTML = '<div class="text-center py-8"><i class="fas fa-exclamation-triangle text-gray-400 text-3xl mb-3"></i><p class="text-gray-500">无法获取表字段信息</p></div>';
        return;
    }

    currentTableColumns.forEach(column => {
        const fieldMeta = (currentFieldTypes && currentFieldTypes[column]) || {};
        if (
            column.toLowerCase() === 'id'
            || isEditFormHiddenField(column)
            || fieldMeta.sensitive
            || fieldMeta.editable === false
        ) {
            return;
        }
        if (column.toLowerCase().endsWith('_hash')) {
            const fieldDiv = document.createElement('div');
            fieldDiv.className = 'space-y-1';
            const label = document.createElement('label');
            label.className = 'block text-sm font-semibold text-gray-700';
            label.textContent = getFieldDisplayName(column);
            label.setAttribute('for', `edit-field-${column}`);
            const input = document.createElement('input');
            input.type = 'text';
            input.id = `edit-field-${column}`;
            input.name = column;
            input.readOnly = true;
            input.className = 'w-full px-4 py-2 border border-gray-200 rounded-lg bg-gray-50 font-mono text-xs text-gray-700';
            input.value = rowData && rowData[column] !== null && rowData[column] !== undefined ? String(rowData[column]) : '';
            fieldDiv.appendChild(label);
            fieldDiv.appendChild(input);
            container.appendChild(fieldDiv);
            return;
        }

        const fieldDiv = document.createElement('div');
        fieldDiv.className = 'space-y-1';

        const label = document.createElement('label');
        label.className = 'block text-sm font-semibold text-gray-700';
        label.textContent = getFieldDisplayName(column);
        label.setAttribute('for', `edit-field-${column}`);

        const inputType = getFieldInputType(column);
        const selectOptions = getAddFormSelectOptions(column);
        let inputElement;

        if (selectOptions) {
            inputElement = document.createElement('select');
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent transition-all duration-200 bg-white';
            if (!isRequiredField(column)) {
                const emptyOption = document.createElement('option');
                emptyOption.value = '';
                emptyOption.textContent = '（空）';
                inputElement.appendChild(emptyOption);
            }
            selectOptions.forEach(option => {
                const optionElement = document.createElement('option');
                optionElement.value = option.value;
                optionElement.textContent = option.label;
                inputElement.appendChild(optionElement);
            });
            const raw = rowData && rowData[column] !== null && rowData[column] !== undefined ? rowData[column] : '';
            inputElement.value = String(raw);
        }
        else if (inputType === 'textarea' || isJsonField(column)) {
            inputElement = document.createElement('textarea');
            inputElement.rows = isJsonField(column) ? 8 : 4;
            inputElement.placeholder = isJsonField(column) ? '请输入合法 JSON' : `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent transition-all duration-200 resize-none font-mono text-xs';
            inputElement.value = rowData ? serializeFieldValueForInput(rowData[column]) : '';
        } else if (inputType === 'checkbox') {
            inputElement = document.createElement('input');
            inputElement.type = 'checkbox';
            inputElement.className = 'w-5 h-5 text-emerald-600 border-gray-300 rounded focus:ring-2 focus:ring-emerald-500 transition-all duration-200';
            const v = rowData ? rowData[column] : false;
            inputElement.checked = v === true || v === 'true' || v === 1 || v === '1';
        } else if (inputType === 'date' || inputType === 'datetime-local' || inputType === 'time') {
            inputElement = document.createElement('input');
            inputElement.type = inputType;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent transition-all duration-200';
            const raw = rowData && rowData[column] !== null && rowData[column] !== undefined ? rowData[column] : '';
            if (inputType === 'datetime-local') {
                const parsed = parseDateTimeForInput(String(raw));
                if (parsed) inputElement.value = parsed;
            } else {
                inputElement.value = serializeFieldValueForInput(raw).slice(0, inputType === 'time' ? 8 : 10);
            }
        } else {
            inputElement = document.createElement('input');
            inputElement.type = inputType;
            inputElement.placeholder = `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:border-transparent transition-all duration-200';
            if (inputType === 'number') {
                if (currentFieldTypes && currentFieldTypes[column]) {
                    const fieldType = currentFieldTypes[column].type;
                    if (fieldType === 'FloatField' || fieldType === 'DecimalField') {
                        inputElement.step = 'any';
                    }
                }
            }

            if (currentFieldTypes && currentFieldTypes[column] && currentFieldTypes[column].max_length) {
                inputElement.maxLength = currentFieldTypes[column].max_length;
            }

            const raw = rowData && rowData[column] !== null && rowData[column] !== undefined ? rowData[column] : '';
            inputElement.value = inputType === 'number'
                ? (raw === '' ? '' : String(raw))
                : serializeFieldValueForInput(raw);
        }

        inputElement.id = `edit-field-${column}`;
        inputElement.name = column;

        if (isRequiredField(column)) {
            inputElement.required = true;
            label.innerHTML += ' <span class="text-red-500 ml-1">*</span>';
        }

        fieldDiv.appendChild(label);

        if (inputType === 'checkbox') {
            const checkboxWrapper = document.createElement('div');
            checkboxWrapper.className = 'bg-gray-50 p-3 rounded-lg';
            const checkboxDiv = document.createElement('div');
            checkboxDiv.className = 'flex items-center';
            checkboxDiv.appendChild(inputElement);
            const checkboxLabel = document.createElement('label');
            checkboxLabel.className = 'ml-3 text-sm font-medium text-gray-700 cursor-pointer';
            checkboxLabel.textContent = '启用';
            checkboxLabel.setAttribute('for', `edit-field-${column}`);
            checkboxDiv.appendChild(checkboxLabel);
            checkboxWrapper.appendChild(checkboxDiv);
            fieldDiv.appendChild(checkboxWrapper);
        } else {
            fieldDiv.appendChild(inputElement);
        }

        container.appendChild(fieldDiv);
    });
}

function submitEditRecord() {
    const rowId = currentEditRowId;
    if (!rowId) {
        showMessage('未选择要编辑的记录', 'warning');
        return;
    }

    const updateData = {};
    let invalidJsonField = null;
    currentTableColumns.forEach(column => {
        const fieldMeta = (currentFieldTypes && currentFieldTypes[column]) || {};
        if (
            column === 'id'
            || isEditFormHiddenField(column)
            || fieldMeta.sensitive
            || fieldMeta.editable === false
        ) {
            return;
        }
        if (column.toLowerCase().endsWith('_hash')) {
            return;
        }
        const element = document.getElementById(`edit-field-${column}`);
        if (!element) {
            return;
        }
        const parsedValue = parseFieldValueFromInput(column, element);
        if (!parsedValue.ok) {
            invalidJsonField = column;
            return;
        }
        updateData[column] = parsedValue.value;
    });
    if (invalidJsonField) {
        showMessage(`${getFieldDisplayName(invalidJsonField)} 不是合法 JSON，已取消保存`, 'error');
        return;
    }

    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        showMessage('无法获取CSRF令牌，请刷新页面', 'error');
        return;
    }

    const requestData = {
        action: 'update_table_row',
        table_name: currentTableName,
        row_id: rowId,
        update_data: updateData
    };

    fetch('/dashboard/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken
        },
        body: JSON.stringify(requestData)
    })
    .then(response => response.json())
    .then(result => {
        if (result.status === 'success') {
            showMessage('数据更新成功', 'success');
            closeEditRecordModal();
            fetchTableData(currentTableName, currentPage);
        } else {
            showMessage('更新失败: ' + (result.message || '未知错误'), 'error');
        }
    })
    .catch(error => {
        console.error('更新记录失败:', error);
        showMessage('更新失败: ' + error.message, 'error');
    });
}

/**
 * 生成表单字段
 */
function generateFormFields(container) {
    container.innerHTML = '';

    if (!currentTableColumns || currentTableColumns.length === 0) {
        container.innerHTML = '<div class="text-center py-8"><i class="fas fa-exclamation-triangle text-gray-400 text-3xl mb-3"></i><p class="text-gray-500">无法获取表字段信息</p></div>';
        return;
    }

    currentTableColumns.forEach(column => {
        const fieldMeta = (currentFieldTypes && currentFieldTypes[column]) || {};
        // 新增窗口只暴露后端 schema 明确允许写入的字段。
        if (
            column.toLowerCase() === 'id'
            || isAddFormHiddenField(column)
            || fieldMeta.sensitive
            || fieldMeta.editable === false
        ) {
            return;
        }

        const fieldDiv = document.createElement('div');
        fieldDiv.className = 'space-y-1';

        const label = document.createElement('label');
        label.className = 'block text-sm font-semibold text-gray-700';
        label.textContent = getFieldDisplayName(column);
        label.setAttribute('for', `field-${column}`);

        const inputType = getFieldInputType(column);
        const selectOptions = getAddFormSelectOptions(column);
        const defaultValue = getAddFormDefaultValue(column);
        let inputElement;

        if (selectOptions) {
            inputElement = document.createElement('select');
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 bg-white';
            selectOptions.forEach(option => {
                const optionElement = document.createElement('option');
                optionElement.value = option.value;
                optionElement.textContent = option.label;
                inputElement.appendChild(optionElement);
            });
            if (defaultValue !== undefined) {
                inputElement.value = defaultValue;
            }
        } else if (inputType === 'textarea') {
            // 创建textarea元素
            inputElement = document.createElement('textarea');
            inputElement.rows = 4;
            inputElement.placeholder = `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 resize-none';
        } else if (inputType === 'checkbox') {
            // 创建checkbox元素
            inputElement = document.createElement('input');
            inputElement.type = 'checkbox';
            inputElement.className = 'w-5 h-5 text-blue-600 border-gray-300 rounded focus:ring-2 focus:ring-blue-500 transition-all duration-200';
            // 为checkbox添加默认值处理
            if (defaultValue !== undefined) {
                inputElement.checked = Boolean(defaultValue);
            } else if (currentFieldTypes && currentFieldTypes[column] && currentFieldTypes[column].default !== null) {
                inputElement.checked = currentFieldTypes[column].default;
            }
        } else if (inputType === 'date' || inputType === 'datetime-local' || inputType === 'time') {
            inputElement = document.createElement('input');
            inputElement.type = inputType;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200';
        } else {
            // 创建普通input元素
            inputElement = document.createElement('input');
            inputElement.type = inputType;
            inputElement.placeholder = `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200';
            // 为数字类型设置step属性
            if (inputType === 'number') {
                if (currentFieldTypes && currentFieldTypes[column]) {
                    const fieldType = currentFieldTypes[column].type;
                    if (fieldType === 'FloatField' || fieldType === 'DecimalField') {
                        inputElement.step = 'any';
                    }
                }
            }

            // 为字符字段设置最大长度
            if (currentFieldTypes && currentFieldTypes[column] && currentFieldTypes[column].max_length) {
                inputElement.maxLength = currentFieldTypes[column].max_length;
            }
        }

        inputElement.id = `field-${column}`;
        inputElement.name = column;

        // 设置必填字段
        if (isRequiredField(column)) {
            inputElement.required = true;
            label.innerHTML += ' <span class="text-red-500 ml-1">*</span>';
        }

        fieldDiv.appendChild(label);

        // 为checkbox创建特殊布局
        if (inputType === 'checkbox') {
            const checkboxWrapper = document.createElement('div');
            checkboxWrapper.className = 'bg-gray-50 p-3 rounded-lg';
            const checkboxDiv = document.createElement('div');
            checkboxDiv.className = 'flex items-center';
            checkboxDiv.appendChild(inputElement);
            const checkboxLabel = document.createElement('label');
            checkboxLabel.className = 'ml-3 text-sm font-medium text-gray-700 cursor-pointer';
            checkboxLabel.textContent = '启用';
            checkboxLabel.setAttribute('for', `field-${column}`);
            checkboxDiv.appendChild(checkboxLabel);
            checkboxWrapper.appendChild(checkboxDiv);
            fieldDiv.appendChild(checkboxWrapper);
        } else {
            fieldDiv.appendChild(inputElement);
        }

        container.appendChild(fieldDiv);
    });
}

/**
 * 获取字段显示名称
 */
function getFieldDisplayName(column) {
    const rawLabel = (currentFieldLabels && currentFieldLabels[column]) ? String(currentFieldLabels[column]).trim() : '';
    if (rawLabel && !isProbablyEnglishLabel(rawLabel)) return rawLabel;
    const fieldNames = {
        platform: '平台',
        apl_keyword: 'APL关键字',
        cn_keyword: '中文关键字',
        id: '编号',
        name: '名称',
        title: '标题',
        url: '链接',
        link: '链接',
        target: '目标',
        type: '类型',
        tag: '标记',
        status: '状态',
        content: '内容',
        description: '描述',
        author: '作者',
        source: '来源',
        category: '分类',
        publish_time: '发布时间',
        created_at: '创建时间',
        updated_at: '更新时间',
        create_time: '创建时间',
        update_time: '更新时间',
        last_scan_time: '上次扫描时间',
        wait_time: '间隔(秒)',
        is_active: '是否启用',
        is_login: '是否登录',
        is_verify: '是否验证',
        is_poc: '是否POC',
        is_exp: '是否EXP',
        url_hash: '链接Hash',
        rss_id: 'RSS编号',
        content_html: '内容HTML',
        fight_style: '战斗风格',
        target_count: '目标数量',
    };
    if (fieldNames[column]) return fieldNames[column];
    return translateFieldNameToCn(column);
}

function isProbablyEnglishLabel(s) {
    const v = String(s || '').trim();
    if (!v) return false;
    if (!/^[\x00-\x7F]+$/.test(v)) return false;
    return /[A-Za-z]/.test(v);
}

function translateFieldNameToCn(fieldName) {
    const raw = String(fieldName || '').trim();
    if (!raw) return '';

    const snake = raw
        .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
        .replace(/-+/g, '_')
        .replace(/\s+/g, '_')
        .toLowerCase();

    const tokens = snake.split('_').filter(Boolean);
    if (!tokens.length) return raw;

    const dict = {
        id: '编号',
        name: '名称',
        title: '标题',
        url: '链接',
        link: '链接',
        hash: 'Hash',
        target: '目标',
        type: '类型',
        tag: '标记',
        flag: '标记',
        status: '状态',
        content: '内容',
        desc: '描述',
        description: '描述',
        author: '作者',
        source: '来源',
        category: '分类',
        task: '任务',
        profile: '配置',
        rule: '规则',
        login: '登录',
        password: '密码',
        email: '邮箱',
        token: 'Token',
        secret: '密钥',
        webhook: 'Webhook',
        wechat: '微信',
        wx: '微信',
        article: '文章',
        rss: 'RSS',
        bili: '哔哩',
        vuln: '漏洞',
        poc: 'POC',
        exp: 'EXP',
        verify: '验证',
        group: '群',
        chat: '聊天',
        msg: '消息',
        simc: 'SimC',
        publish: '发布',
        published: '发布',
        create: '创建',
        created: '创建',
        update: '更新',
        updated: '更新',
        time: '时间',
        date: '日期',
        start: '开始',
        end: '结束',
        last: '上次',
        scan: '扫描',
        wait: '间隔',
        interval: '间隔',
        count: '数量',
        num: '数量',
        number: '数量',
        total: '总数',
        week: '周',
        season: '赛季',
        period: '周期',
        dungeon: '副本',
        role: '职责',
        spec: '专精',
        avg: '平均',
        top: '最高',
        runs: '样本数',
        diff: '差值',
        rank: '排名',
        score: '分数',
        level: '等级',
        key: '钥石',
        min: '最小',
        max: '最大',
        crit: '暴击',
        haste: '急速',
        mastery: '精通',
        versatility: '全能',
        coefficient: '系数',
        percent: '百分比',
        ratio: '比例',
        fight: '战斗',
        style: '风格',
        html: 'HTML',
        text: '文本',
        raw: '原始',
        value: '数值',
        is: '是否',
        active: '启用',
        enable: '启用',
        enabled: '启用',
        disable: '禁用',
        disabled: '禁用',
    };

    const parts = tokens.map(t => dict[t] || t);
    const label = parts.join('');
    return label || raw;
}

/**
 * 获取字段输入类型
 */
function getFieldInputType(column) {
    if (column.toLowerCase().endsWith('_hash')) {
        return 'text';
    }
    // 如果有字段类型信息，根据Django字段类型判断
    if (currentFieldTypes && currentFieldTypes[column]) {
        const fieldInfo = currentFieldTypes[column];
        const fieldType = fieldInfo.type;

        switch (fieldType) {
            case 'BooleanField':
                return 'checkbox';
            case 'IntegerField':
            case 'BigIntegerField':
            case 'SmallIntegerField':
            case 'PositiveIntegerField':
            case 'PositiveSmallIntegerField':
                return 'number';
            case 'FloatField':
            case 'DecimalField':
                return 'number';
            case 'DateField':
                return 'date';
            case 'DateTimeField':
                return 'datetime-local';
            case 'TimeField':
                return 'time';
            case 'EmailField':
                return 'email';
            case 'URLField':
                return 'url';
            case 'TextField':
            case 'JSONField':
                return 'textarea';
            case 'CharField':
                // 根据字段名进一步判断
                if (column.toLowerCase().includes('password')) {
                    return 'password';
                }
                if (column.toLowerCase().includes('url')) {
                    return 'url';
                }
                if (column.toLowerCase().includes('email')) {
                    return 'email';
                }
                return 'text';
            default:
                return 'text';
        }
    }

    // 回退到基于字段名的判断
    if (column.toLowerCase().includes('url')) {
        return 'url';
    }
    if (column.toLowerCase().includes('email')) {
        return 'email';
    }
    if (column.toLowerCase().includes('password')) {
        return 'password';
    }
    if (column.toLowerCase().includes('number') || column.toLowerCase().includes('count')) {
        return 'number';
    }
    return 'text';
}

/**
 * 判断是否为必填字段
 */
function isRequiredField(column) {
    const inputType = getFieldInputType(column);
    if (inputType === 'checkbox' || isModelBooleanField(column)) {
        return false;
    }

    const info = getFieldInfo(column);
    if (info && info.type && !info.null && !info.blank && !info.primary_key && !info.auto_now && !info.auto_now_add) {
        return true;
    }
    const requiredFields = ['apl_keyword', 'cn_keyword', 'name', 'title', 'url'];
    return requiredFields.includes(column.toLowerCase());
}

/**
 * 提交新增记录
 */
function submitAddRecord() {
    const form = document.getElementById('add-record-form');
    const data = {};
    let invalidJsonField = null;

    // 遍历所有表单字段，正确处理不同类型的输入
    currentTableColumns.forEach(column => {
        // 跳过自动生成和新增窗口隐藏字段
        if (column === 'id' || isAddFormHiddenField(column)) {
            return;
        }

        const element = document.getElementById(`field-${column}`);
        if (element) {
            const parsedValue = parseFieldValueFromInput(column, element);
            if (!parsedValue.ok) {
                invalidJsonField = column;
                return;
            }
            data[column] = parsedValue.value;
        }
    });
    if (invalidJsonField) {
        showMessage(`${getFieldDisplayName(invalidJsonField)} 不是合法 JSON，已取消添加`, 'error');
        return;
    }

    // 使用通用的 dashboard API
    const apiUrl = '/dashboard/';
    const requestData = {
        action: 'create_table_row',
        table_name: currentTableName,
        create_data: data
    };

    // 发送POST请求
    fetch(apiUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': getCSRFToken()
        },
        body: JSON.stringify(requestData)
    })
    .then(response => response.json())
    .then(result => {
        if (result.success || result.status === 'success') {
            showMessage('记录添加成功', 'success');
            closeAddRecordModal();
            // 刷新表格数据
            fetchTableData(currentTableName, currentPage);
        } else {
            showMessage('添加失败: ' + (result.error || result.message || '未知错误'), 'error');
        }
    })
    .catch(error => {
        console.error('添加记录失败:', error);
        showMessage('添加失败: ' + error.message, 'error');
    });
}



/**
 * 初始化侧边栏切换功能
 */
function initSidebarToggle() {
    const sidebarToggle = document.getElementById('sidebar-toggle');
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');

    if (!sidebarToggle || !sidebar || !sidebarOverlay) {
        return;
    }

    // 汉堡菜单按钮点击事件
    sidebarToggle.addEventListener('click', function() {
        toggleSidebar();
    });

    // 遮罩层点击关闭侧边栏
    sidebarOverlay.addEventListener('click', function() {
        closeSidebar();
    });

    // 移动端选择实际导航目标后收起侧边栏，避免遮挡新内容。
    sidebar.addEventListener('click', function(e) {
        const navigationTarget = e.target.closest('.nav-item:not(.has-submenu), .submenu-item');
        if (navigationTarget && window.innerWidth < 1024) {
            closeSidebar();
        }
    });

    // ESC键关闭侧边栏
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && sidebar.classList.contains('open')) {
            closeSidebar();
        }
    });

    // 窗口大小改变时处理侧边栏状态
    window.addEventListener('resize', function() {
        if (window.innerWidth >= 1024) {
            // 大屏幕时确保侧边栏和遮罩层状态正确
            sidebar.classList.remove('open');
            sidebarOverlay.classList.remove('show');
            document.body.style.overflow = '';
        }
    });
}

/**
 * 切换侧边栏显示状态
 */
function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');

    if (sidebar.classList.contains('open')) {
        closeSidebar();
    } else {
        openSidebar();
    }
}

/**
 * 打开侧边栏
 */
function openSidebar() {
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');

    sidebar.classList.add('open');
    sidebarOverlay.classList.add('show');

    // 防止背景滚动
    document.body.style.overflow = 'hidden';
}

/**
 * 关闭侧边栏
 */
function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    const sidebarOverlay = document.getElementById('sidebar-overlay');

    sidebar.classList.remove('open');
    sidebarOverlay.classList.remove('show');

    // 恢复背景滚动
    document.body.style.overflow = '';
}

// 搜索相关变量
let searchQuery = '';
let searchTimeout = null;

/**
 * 过滤数据库侧栏中的长表清单；同时匹配中文名、物理表名和模型名。
 */
function initDatabaseTableFilter() {
    const input = document.getElementById('database-table-filter');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    input.addEventListener('click', event => event.stopPropagation());
    input.addEventListener('input', function() {
        const query = this.value.trim().toLocaleLowerCase();
        document.querySelectorAll('.database-table-item').forEach(item => {
            const haystack = (item.dataset.tableSearch || '').toLocaleLowerCase();
            item.classList.toggle('hidden', Boolean(query) && !haystack.includes(query));
        });
    });
}

/**
 * 初始化搜索功能
 */
function initSearch() {
    const searchInput = document.getElementById('search-input');
    if (!searchInput) {
        return;
    }

    // 监听搜索输入框的输入事件
    searchInput.addEventListener('input', function(e) {
        const query = e.target.value.trim();

        // 清除之前的定时器
        if (searchTimeout) {
            clearTimeout(searchTimeout);
        }

        // 设置新的定时器，延迟500ms执行搜索
        searchTimeout = setTimeout(() => {
            performSearch(query);
        }, 500);
    });

    // 监听回车键
    searchInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') {
            e.preventDefault();
            const query = e.target.value.trim();
            performSearch(query);
        }
    });
}

function initSimcProfileFilters() {
    const specInput = document.getElementById('simc-profile-spec-filter');
    const fightStyleInput = document.getElementById('simc-profile-fight-style-filter');
    const applyBtn = document.getElementById('simc-profile-filter-apply');
    const resetBtn = document.getElementById('simc-profile-filter-reset');

    if (specInput) loadSimcSpecOptions().catch(error => console.warn('加载 SimC Profile 专精筛选失败:', error));

    if (applyBtn) {
        applyBtn.addEventListener('click', function() {
            simcProfileSpecFilter = specInput ? specInput.value.trim() : '';
            simcProfileFightStyleFilter = fightStyleInput ? fightStyleInput.value.trim() : '';
            if (currentTableName === 'SimcProfile') fetchTableData('SimcProfile', 1);
        });
    }
    if (resetBtn) {
        resetBtn.addEventListener('click', function() {
            simcProfileSpecFilter = '';
            simcProfileFightStyleFilter = '';
            if (specInput) specInput.value = '';
            if (fightStyleInput) fightStyleInput.value = '';
            if (currentTableName === 'SimcProfile') fetchTableData('SimcProfile', 1);
        });
    }
}

function initWowArticleFilters() {
    const sourceInput = document.getElementById('wow-article-source-filter');
    const categoryInput = document.getElementById('wow-article-category-filter');
    const applyBtn = document.getElementById('wow-article-filter-apply');
    const resetBtn = document.getElementById('wow-article-filter-reset');

    if (applyBtn) {
        applyBtn.addEventListener('click', function() {
            wowArticleSourceFilter = sourceInput ? sourceInput.value.trim() : '';
            wowArticleCategoryFilter = categoryInput ? categoryInput.value.trim() : '';
            if (currentTableName === 'WowArticle') fetchTableData('WowArticle', 1);
        });
    }
    if (resetBtn) {
        resetBtn.addEventListener('click', function() {
            wowArticleSourceFilter = '';
            wowArticleCategoryFilter = '';
            if (sourceInput) sourceInput.value = '';
            if (categoryInput) categoryInput.value = '';
            if (currentTableName === 'WowArticle') fetchTableData('WowArticle', 1);
        });
    }
}

function updateWowArticleFilterOptions(options) {
    const sourceInput = document.getElementById('wow-article-source-filter');
    const categoryInput = document.getElementById('wow-article-category-filter');
    if (!sourceInput || !categoryInput) return;

    const currentSource = sourceInput.value;
    const currentCategory = categoryInput.value;

    const sources = (options && Array.isArray(options.sources))
        ? options.sources.map(v => (v || '').toString().trim()).filter(v => v)
        : [];
    const categories = (options && Array.isArray(options.categories))
        ? options.categories.map(v => (v || '').toString().trim()).filter(v => v)
        : [];

    sourceInput.innerHTML = '';
    const allSourceOption = document.createElement('option');
    allSourceOption.value = '';
    allSourceOption.textContent = '全部来源';
    sourceInput.appendChild(allSourceOption);
    for (const v of sources) {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        sourceInput.appendChild(opt);
    }

    categoryInput.innerHTML = '';
    const allCategoryOption = document.createElement('option');
    allCategoryOption.value = '';
    allCategoryOption.textContent = '全部分类';
    categoryInput.appendChild(allCategoryOption);
    for (const v of categories) {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        categoryInput.appendChild(opt);
    }

    sourceInput.value = sources.includes(currentSource) ? currentSource : '';
    categoryInput.value = categories.includes(currentCategory) ? currentCategory : '';
}

/**
 * 初始化页面大小选择器
 */
function initPageSizeSelector() {
    const pageSizeSelect = document.getElementById('page-size-select');
    if (!pageSizeSelect) {
        return;
    }

    // 监听选择器变化事件
    pageSizeSelect.addEventListener('change', function(e) {
        const newPageSize = parseInt(e.target.value);
        if (newPageSize && newPageSize !== pageSize) {
            pageSize = newPageSize;

            // 如果有选中的表，重置到第一页并重新获取数据
            if (currentTableName) {
                currentPage = 1;
                fetchTableData(currentTableName, currentPage);
            }
        }
    });
}

/**
 * 执行搜索
 */
function performSearch(query) {
    searchQuery = query;

    // 如果没有选中表，不执行搜索
    if (!currentTableName) {
        return;
    }

    // 重置到第一页
    currentPage = 1;

    // 重新获取数据
    fetchTableData(currentTableName, currentPage);
}

/**
 * 清除搜索
 */
function clearSearch() {
    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.value = '';
    }
    searchQuery = '';

    // 如果有选中的表，重新加载数据
    if (currentTableName) {
        currentPage = 1;
        fetchTableData(currentTableName, currentPage);
    }
}

/**
 * 初始化用户菜单功能
 */
function initUserMenu() {
    const userMenuButton = document.getElementById('user-menu-button');
    const userMenu = document.getElementById('user-menu');
    const logoutBtn = document.getElementById('logout-btn');

    if (userMenuButton && userMenu) {
        // 点击用户菜单按钮切换菜单显示
        userMenuButton.addEventListener('click', function(e) {
            e.stopPropagation();
            userMenu.classList.toggle('hidden');
        });

        // 点击页面其他地方关闭菜单
        document.addEventListener('click', function(e) {
            if (!userMenuButton.contains(e.target) && !userMenu.contains(e.target)) {
                userMenu.classList.add('hidden');
            }
        });
    }

    // 登出功能
    if (logoutBtn) {
        logoutBtn.addEventListener('click', async function(e) {
            e.preventDefault();

            try {
                const response = await fetch('/auth/logout/', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRFToken': getCSRFToken()
                    }
                });

                const result = await response.json();

                if (result.status === 'success') {
                    // 登出成功，跳转到登录页面
                    window.location.href = result.redirect_url || '/auth/login/';
                } else {
                    showMessage('登出失败: ' + (result.message || '未知错误'), 'error');
                }
            } catch (error) {
                console.error('登出错误:', error);
                showMessage('登出失败，请稍后重试', 'error');
            }
        });
    }
}

// 在DOMContentLoaded事件中初始化SimC APL转换工具
// 关键字管理功能的初始化已移至主要的DOMContentLoaded事件中

// SimcTask 相关函数
let selectedRegularSimcTaskIds = new Set();
let lastRawSimcInspectData = null;

function resetRawSimcInspectResult() {
    lastRawSimcInspectData = null;
    const box = document.getElementById('simc-raw-inspect-result');
    if (box) {
        box.classList.add('hidden');
        box.innerHTML = '';
    }
}

function renderRawSimcInspectResult(data) {
    const box = document.getElementById('simc-raw-inspect-result');
    if (!box) return;
    if (!data) {
        resetRawSimcInspectResult();
        return;
    }
    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const plans = Array.isArray(data.plans) ? data.plans : [];
    const detectedParts = [
        data.character_name ? `角色：${escapeHtml(data.character_name)}` : '角色：未识别',
        data.class ? `职业：${escapeHtml(data.class)}` : '职业：未识别',
        data.spec ? `专精：${escapeHtml(data.spec)}` : '专精：未识别',
        data.default_apl_available ? `默认APL：已匹配 (${data.default_apl_length || 0} 字符)` : '默认APL：未匹配'
    ];
    const planHtml = plans.map(plan => {
        const disabled = plan.enabled ? '' : 'disabled';
        const checked = plan.enabled && plan.checked ? 'checked' : '';
        const reason = plan.reason ? `<div class="text-xs text-gray-500 mt-1">${escapeHtml(plan.reason)}</div>` : '';
        const disabledClass = plan.enabled ? 'bg-white border-indigo-100' : 'bg-gray-50 border-gray-200 opacity-70';
        return `
            <label class="block border ${disabledClass} rounded-md p-2 mt-2">
                <div class="flex items-center gap-2">
                    <input type="checkbox" data-raw-simc-plan="${escapeHtml(plan.id || '')}" ${checked} ${disabled} class="h-4 w-4 text-indigo-600 border-gray-300 rounded">
                    <span class="font-medium text-gray-800">${escapeHtml(plan.label || plan.id || '方案')}</span>
                    ${plan.enabled ? '<span class="text-xs text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full">可创建</span>' : '<span class="text-xs text-gray-600 bg-gray-200 px-2 py-0.5 rounded-full">暂不可用</span>'}
                </div>
                ${reason}
            </label>`;
    }).join('');
    const warningHtml = warnings.length ? `
        <div class="mt-2 p-2 bg-amber-50 border border-amber-100 text-amber-800 rounded">
            ${warnings.map(w => `<div>• ${escapeHtml(w)}</div>`).join('')}
        </div>` : '';
    box.innerHTML = `
        <div class="font-semibold text-indigo-900 mb-1">识别结果</div>
        <div class="text-xs text-indigo-800 flex flex-wrap gap-x-3 gap-y-1">${detectedParts.map(p => `<span>${p}</span>`).join('')}</div>
        ${warningHtml}
        <div class="mt-3">
            <div class="font-semibold text-gray-800">可创建方案</div>
            ${planHtml || '<div class="text-gray-500 mt-2">暂无可创建方案</div>'}
        </div>`;
    box.classList.remove('hidden');
}

let simcBackendUpdatePollTimer = null;

function renderSimcBackendUpdatePanel(payload) {
    const panel = document.getElementById('simc-backend-update-panel');
    if (!panel) return;
    const statusEl = document.getElementById('simc-backend-update-status');
    const versionEl = document.getElementById('simc-backend-update-version');
    const needEl = document.getElementById('simc-backend-update-need');
    const runningEl = document.getElementById('simc-backend-update-running');
    const barEl = document.getElementById('simc-backend-update-progress-bar');
    const textEl = document.getElementById('simc-backend-update-progress-text');

    const data = payload && payload.data ? payload.data : null;
    if (!payload || !payload.success || !data) {
        panel.classList.add('hidden');
        return;
    }

    const progress = Number.isFinite(parseInt(data.update_progress, 10)) ? parseInt(data.update_progress, 10) : 0;
    const statusText = String(data.update_status || '').trim();
    const hasError = String(data.last_error || '').trim();
    const isUpdating = !!data.is_updating;
    const cur = String(data.current_version || '').trim();
    const latest = String(data.latest_version || '').trim();
    const needUpdate = typeof data.need_update !== 'undefined'
        ? !!data.need_update
        : (!!latest && latest !== cur);
    const shouldShow = isUpdating || progress > 0 || !!statusText || !!hasError;

    if (!shouldShow) {
        panel.classList.add('hidden');
        return;
    }

    panel.classList.remove('hidden');
    if (statusEl) statusEl.textContent = hasError ? `失败：${hasError}` : (statusText || '处理中');
    if (versionEl) {
        versionEl.textContent = cur || latest ? `当前: ${cur || '-'}  最新: ${latest || '-'}` : '';
    }
    if (needEl) {
        needEl.textContent = `需要更新: ${needUpdate ? '是' : '否'}`;
        needEl.className = `inline-flex items-center px-2 py-0.5 rounded-full ${needUpdate ? 'bg-amber-100 text-amber-800' : 'bg-emerald-100 text-emerald-800'}`;
    }
    if (runningEl) {
        runningEl.textContent = `正在更新: ${isUpdating ? '是' : '否'}`;
        runningEl.className = `inline-flex items-center px-2 py-0.5 rounded-full ${isUpdating ? 'bg-blue-100 text-blue-800' : 'bg-gray-200 text-gray-700'}`;
    }
    if (barEl) barEl.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    if (textEl) textEl.textContent = isUpdating ? `进度: ${progress}%` : (progress ? `进度: ${progress}%` : '');
}

function startSimcBackendUpdatePolling() {
    if (simcBackendUpdatePollTimer) return;

    const pollOnce = async () => {
        try {
            const resp = await fetch('/api/simc-backend-binary/', {
                method: 'GET',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCSRFToken()
                }
            });
            if (!resp.ok) return;
            const data = await resp.json();
            renderSimcBackendUpdatePanel(data);
            const row = data && data.data ? data.data : {};
            const isUpdating = !!row.is_updating;
            const nextDelay = isUpdating ? 1500 : 30000;
            simcBackendUpdatePollTimer = setTimeout(() => {
                simcBackendUpdatePollTimer = null;
                startSimcBackendUpdatePolling();
            }, nextDelay);
        } catch (e) {
            simcBackendUpdatePollTimer = setTimeout(() => {
                simcBackendUpdatePollTimer = null;
                startSimcBackendUpdatePolling();
            }, 30000);
        }
    };

    pollOnce();
}

let wclDashboardInited = false;
let wclDashboardSubmitting = false;

function initWclDashboardModule() {
    if (wclDashboardInited) return;
    wclDashboardInited = true;

    const refreshBtn = document.getElementById('wcl-dashboard-refresh-btn');

    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => fetchWclDashboardTasks());
    }
}

async function submitWclDashboardTask() {
    if (wclDashboardSubmitting) {
        return;
    }
    const input = document.getElementById('wcl-dashboard-url');
    const msg = document.getElementById('wcl-dashboard-message');
    const submitBtn = document.getElementById('wcl-dashboard-submit-btn');
    const wclUrl = (input && input.value ? input.value : '').trim();
    if (!wclUrl) {
        if (msg) {
            msg.className = 'text-sm text-red-600';
            msg.textContent = '请输入WCL链接';
        }
        return;
    }

    wclDashboardSubmitting = true;
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.classList.add('opacity-60', 'cursor-not-allowed');
    }

    if (msg) {
        msg.className = 'text-sm text-gray-600';
        msg.textContent = '任务提交中...';
    }

    try {
        const resp = await fetch('/api/wcl-analysis-task/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCSRFToken()
            },
            body: JSON.stringify({ wcl_url: wclUrl })
        });
        const data = await resp.json();
        if (!data || !data.success) {
            if (msg) {
                msg.className = 'text-sm text-red-600';
                msg.textContent = (data && data.error) || '任务提交失败';
            }
            return;
        }

        if (msg) {
            msg.className = 'text-sm text-green-600';
            msg.innerHTML = `任务已提交，<a class="underline" target="_blank" href="${data.data.report_url}">点击查看结果页</a>（处理中可刷新）`;
        }
        if (input) input.value = '';
        fetchWclDashboardTasks();
    } catch (e) {
        if (msg) {
            msg.className = 'text-sm text-red-600';
            msg.textContent = `任务提交失败: ${e.message || ''}`;
        }
    } finally {
        wclDashboardSubmitting = false;
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.classList.remove('opacity-60', 'cursor-not-allowed');
        }
    }
}

async function fetchWclDashboardTasks() {
    const tbody = document.getElementById('wcl-dashboard-task-list');
    if (!tbody) return;

    tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-gray-500">加载中...</td></tr>';
    try {
        const resp = await fetch('/api/wcl-analysis-task/?limit=50', {
            method: 'GET',
            credentials: 'same-origin'
        });
        const data = await resp.json();
        if (!data || !data.success) {
            tbody.innerHTML = `<tr><td colspan="6" class="px-4 py-6 text-center text-red-600">${(data && data.error) || '加载失败'}</td></tr>`;
            return;
        }
        const tasks = data.data || [];
        if (!tasks.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-gray-500">暂无任务</td></tr>';
            return;
        }

        const statusMap = {
            0: '待处理',
            1: '处理中',
            2: '成功',
            3: '失败'
        };
        tbody.innerHTML = tasks.map(t => `
            <tr class="hover:bg-gray-50">
                <td class="px-4 py-3 text-sm text-gray-900">${t.id}</td>
                <td class="px-4 py-3 text-sm text-gray-700 break-all">${escapeHtml(t.wcl_url || '')}</td>
                <td class="px-4 py-3 text-sm text-gray-700">${statusMap[t.status] || t.status}</td>
                <td class="px-4 py-3 text-sm text-gray-700">${escapeHtml(t.summary || '')}</td>
                <td class="px-4 py-3 text-sm text-gray-700">${escapeHtml(t.created_at || '')}</td>
                <td class="px-4 py-3 text-sm">
                    <a class="text-blue-600 hover:text-blue-800" target="_blank" href="${t.report_url || '#'}">查看</a>
                </td>
            </tr>
        `).join('');
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-6 text-center text-red-600">加载失败</td></tr>';
    }
}


function initErrorLogPage() {
    const refreshBtn = document.getElementById('error-log-refresh');
    const markAllBtn = document.getElementById('error-log-mark-all-read');
    const deleteAllReadBtn = document.getElementById('error-log-delete-all-read');
    const searchInput = document.getElementById('error-log-search');
    const pageSizeSelect = document.getElementById('error-log-page-size');
    const showReadCheckbox = document.getElementById('error-log-show-read');

    let currentPage = 1;
    let currentSearch = '';

    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => loadErrorLogs(1));
    }
    if (markAllBtn) {
        markAllBtn.addEventListener('click', async () => {
            if (!confirm('确定将所有系统报警标记为已读？')) return;
            try {
                const resp = await fetch('/api/system-alert/', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'mark_all_read' })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('已全部标记为已读', 'success');
                    loadErrorLogs(1);
                } else {
                    showToast(data.error || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        });
    }
    if (deleteAllReadBtn) {
        deleteAllReadBtn.addEventListener('click', async () => {
            if (!confirm('确定清除所有已读的系统报警？此操作不可恢复。')) return;
            try {
                const resp = await fetch('/api/system-alert/', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'delete_all_read' })
                });
                const data = await resp.json();
                if (data.success) {
                    showToast('已清除所有已读日志', 'success');
                    loadErrorLogs(1);
                } else {
                    showToast(data.error || '操作失败', 'error');
                }
            } catch (e) {
                showToast('操作失败', 'error');
            }
        });
    }
    if (searchInput) {
        let searchTimer = null;
        searchInput.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                currentSearch = searchInput.value.trim();
                loadErrorLogs(1);
            }, 300);
        });
    }
    if (pageSizeSelect) {
        pageSizeSelect.addEventListener('change', () => loadErrorLogs(1));
    }
    if (showReadCheckbox) {
        showReadCheckbox.addEventListener('change', () => loadErrorLogs(1));
    }

    async function loadErrorLogs(page) {
        currentPage = page || 1;
        const listEl = document.getElementById('error-log-list');
        const emptyEl = document.getElementById('error-log-empty');
        const pageInfoEl = document.getElementById('error-log-page-info');
        const pageButtonsEl = document.getElementById('error-log-page-buttons');
        if (!listEl) return;

        const pageSize = pageSizeSelect ? pageSizeSelect.value : '20';
        const showRead = showReadCheckbox ? showReadCheckbox.checked : false;

        let url = `/api/system-alert/?page=${currentPage}&page_size=${pageSize}`;
        if (showRead) url += '&show_read=true';

        listEl.innerHTML = '<div class="px-6 py-8 text-center text-gray-500"><i class="fas fa-spinner fa-spin mr-2"></i>加载中...</div>';
        if (emptyEl) emptyEl.classList.add('hidden');

        try {
            const resp = await fetch(url, { method: 'GET', credentials: 'same-origin' });
            const data = await resp.json();
            if (!data.success) {
                listEl.innerHTML = `<div class="px-6 py-8 text-center text-red-600">${data.error || '加载失败'}</div>`;
                return;
            }

            let items = data.data || [];
            if (currentSearch) {
                const q = currentSearch.toLowerCase();
                items = items.filter(a =>
                    (a.title || '').toLowerCase().includes(q) ||
                    (a.content || '').toLowerCase().includes(q) ||
                    (a.subject || '').toLowerCase().includes(q)
                );
            }

            if (!items.length) {
                listEl.innerHTML = '';
                if (emptyEl) emptyEl.classList.remove('hidden');
            } else {
                if (emptyEl) emptyEl.classList.add('hidden');
                listEl.innerHTML = items.map(a => `
                    <div class="px-6 py-4 hover:bg-gray-50 transition-colors duration-150 ${a.is_read ? 'opacity-60' : ''}">
                        <div class="flex items-start justify-between">
                            <div class="flex-1 min-w-0">
                                <div class="flex items-center gap-2 mb-1">
                                    <span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-800">
                                        <i class="fas fa-exclamation-circle mr-1"></i>${escapeHtml(a.category || 'ALERT')}
                                    </span>
                                    <span class="text-xs text-gray-500">${escapeHtml(a.subject || '')}</span>
                                    <span class="text-xs text-gray-400">×${a.count || 1}</span>
                                </div>
                                <p class="text-sm text-gray-900 font-mono break-all">${escapeHtml(a.title || '')}</p>
                                ${a.content && a.content !== a.title ? `<pre class="mt-2 text-xs text-gray-600 bg-gray-50 rounded p-2 max-h-32 overflow-y-auto whitespace-pre-wrap break-all">${escapeHtml(a.content)}</pre>` : ''}
                                <div class="mt-2 flex items-center gap-4 text-xs text-gray-500">
                                    <span><i class="fas fa-clock mr-1"></i>首次: ${escapeHtml(a.first_seen_at || '')}</span>
                                    <span><i class="fas fa-clock mr-1"></i>最近: ${escapeHtml(a.last_seen_at || '')}</span>
                                </div>
                            </div>
                            <div class="flex items-center gap-2 ml-4">
                                <button onclick="markErrorLogRead(${a.id})" class="px-3 py-1 text-xs bg-green-500 text-white rounded hover:bg-green-600 transition-colors">
                                    <i class="fas fa-check mr-1"></i>已读
                                </button>
                                <button onclick="deleteErrorLog(${a.id})" class="px-3 py-1 text-xs bg-red-500 text-white rounded hover:bg-red-600 transition-colors">
                                    <i class="fas fa-trash mr-1"></i>删除
                                </button>
                            </div>
                        </div>
                    </div>
                `).join('');
            }

            if (pageInfoEl) {
                const total = data.total || 0;
                const start = total ? (currentPage - 1) * parseInt(pageSize) + 1 : 0;
                const end = Math.min(currentPage * parseInt(pageSize), total);
                pageInfoEl.textContent = `显示 ${start}-${end} 条，共 ${total} 条记录`;
            }
            if (pageButtonsEl) {
                const totalPages = data.total_pages || 1;
                let btns = '';
                if (currentPage > 1) {
                    btns += `<button onclick="loadErrorLogsGlobal(${currentPage - 1})" class="px-3 py-1 text-sm bg-white border border-gray-300 rounded hover:bg-gray-50">上一页</button>`;
                }
                btns += `<span class="px-3 py-1 text-sm text-gray-700">${currentPage} / ${totalPages}</span>`;
                if (currentPage < totalPages) {
                    btns += `<button onclick="loadErrorLogsGlobal(${currentPage + 1})" class="px-3 py-1 text-sm bg-white border border-gray-300 rounded hover:bg-gray-50">下一页</button>`;
                }
                pageButtonsEl.innerHTML = btns;
            }
        } catch (e) {
            listEl.innerHTML = '<div class="px-6 py-8 text-center text-red-600">加载失败</div>';
        }
    }

    window.loadErrorLogsGlobal = loadErrorLogs;
    window.markErrorLogRead = async function(id) {
        try {
            const resp = await fetch('/api/system-alert/', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'mark_read', id: id })
            });
            const data = await resp.json();
            if (data.success) {
                loadErrorLogs(currentPage);
            }
        } catch (e) {}
    };
    window.deleteErrorLog = async function(id) {
        if (!confirm('确定删除此条错误日志？')) return;
        try {
            const resp = await fetch('/api/system-alert/', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'delete', id: id })
            });
            const data = await resp.json();
            if (data.success) {
                showToast('已删除', 'success');
                loadErrorLogs(currentPage);
            }
        } catch (e) {}
    };

    const navItem = document.querySelector('.nav-item[data-section="error-logs"]');
    if (navItem) {
        navItem.addEventListener('click', () => loadErrorLogs(1));
    }
}


function initLogFilePage() {
    const section = document.getElementById('log-files');
    if (!section) return;

    let currentFilename = '';
    let currentPage = 1;
    let filesLoaded = false;

    const listEl = document.getElementById('log-file-list');
    const emptyEl = document.getElementById('log-file-empty');
    const listHintEl = document.getElementById('log-file-list-hint');
    const contentEl = document.getElementById('log-file-content');
    const contentEmptyEl = document.getElementById('log-file-content-empty');
    const currentNameEl = document.getElementById('log-file-current-name');
    const currentMetaEl = document.getElementById('log-file-current-meta');
    const pageSizeSelect = document.getElementById('log-file-page-size');
    const pageInfoEl = document.getElementById('log-file-page-info');
    const pageButtonsEl = document.getElementById('log-file-page-buttons');
    const refreshBtn = document.getElementById('log-file-refresh');
    const readRefreshBtn = document.getElementById('log-file-read-refresh');
    const sidebarEl = document.getElementById('log-file-sidebar');
    const collapsedRailEl = document.getElementById('log-file-collapsed-rail');
    const collapseBtn = document.getElementById('log-file-sidebar-toggle');
    const expandBtn = document.getElementById('log-file-sidebar-expand');
    let isSidebarCollapsed = false;

    function getLogPageSize() {
        const value = parseInt(pageSizeSelect ? pageSizeSelect.value : '300', 10);
        if (Number.isNaN(value)) return 300;
        return Math.max(1, Math.min(value, 1000));
    }

    function setLogSidebarCollapsed(collapsed) {
        isSidebarCollapsed = collapsed;
        if (!sidebarEl || !collapsedRailEl) return;
        sidebarEl.classList.toggle('hidden', collapsed);
        collapsedRailEl.classList.toggle('hidden', !collapsed);
        collapsedRailEl.classList.toggle('xl:flex', collapsed);
        if (collapseBtn) {
            collapseBtn.setAttribute('aria-expanded', String(!collapsed));
        }
        if (expandBtn) {
            expandBtn.setAttribute('aria-expanded', String(!collapsed));
        }
    }

    async function postDashboard(payload) {
        const csrfToken = getCSRFToken();
        const resp = await fetch('/dashboard/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
            body: JSON.stringify(payload)
        });
        return await resp.json();
    }

    function renderLogFileList(files) {
        if (!listEl) return;
        if (!files.length) {
            listEl.innerHTML = '';
            if (emptyEl) emptyEl.classList.remove('hidden');
            return;
        }
        if (emptyEl) emptyEl.classList.add('hidden');
        listEl.innerHTML = files.map(file => {
            const activeCls = file.filename === currentFilename ? 'bg-blue-50 border-l-4 border-blue-500' : 'hover:bg-gray-50 border-l-4 border-transparent';
            const lineCount = file.line_count >= 0 ? `${file.line_count} 行` : '行数未知';
            return `
                <button type="button" data-log-filename="${escapeHtml(file.filename)}" class="w-full text-left px-5 py-4 transition-colors duration-150 ${activeCls}">
                    <div class="flex items-start justify-between gap-3">
                        <div class="min-w-0">
                            <div class="font-mono text-sm font-semibold text-gray-900 truncate">${escapeHtml(file.filename)}</div>
                            <div class="text-xs text-gray-500 mt-1 flex items-center gap-3 flex-wrap">
                                <span><i class="fas fa-clock mr-1"></i>${escapeHtml(file.mtime_human || '')}</span>
                                <span><i class="fas fa-weight-hanging mr-1"></i>${escapeHtml(file.size_human || '')}</span>
                                <span><i class="fas fa-list-ol mr-1"></i>${escapeHtml(lineCount)}</span>
                            </div>
                        </div>
                        <i class="fas fa-chevron-right text-gray-300 mt-1"></i>
                    </div>
                </button>`;
        }).join('');
        listEl.querySelectorAll('[data-log-filename]').forEach(btn => {
            btn.addEventListener('click', () => readLogFile(btn.getAttribute('data-log-filename'), 1));
        });
    }

    async function loadLogFiles(forceReload = false) {
        if (filesLoaded && !forceReload) return;
        filesLoaded = true;
        if (listEl) listEl.innerHTML = '<div class="px-5 py-8 text-center text-gray-500"><i class="fas fa-spinner fa-spin mr-2"></i>加载日志列表...</div>';
        if (emptyEl) emptyEl.classList.add('hidden');
        try {
            const data = await postDashboard({ action: 'list_log_files' });
            if (data.status !== 'success') {
                if (listEl) listEl.innerHTML = `<div class="px-5 py-8 text-center text-red-600">${escapeHtml(data.message || '加载失败')}</div>`;
                return;
            }
            const files = data.data || [];
            if (listHintEl) listHintEl.textContent = `按修改时间倒序，共 ${files.length} 个日志文件`;
            renderLogFileList(files);
            if (files.length && !currentFilename) {
                readLogFile(files[0].filename, 1);
            } else if (!files.length && contentEmptyEl) {
                contentEmptyEl.classList.remove('hidden');
                if (contentEl) contentEl.innerHTML = '';
            }
        } catch (e) {
            if (listEl) listEl.innerHTML = '<div class="px-5 py-8 text-center text-red-600">加载日志列表失败</div>';
        }
    }

    async function readLogFile(filename, page) {
        if (!filename) return;
        currentFilename = filename;
        currentPage = page || 1;
        if (contentEmptyEl) contentEmptyEl.classList.add('hidden');
        if (contentEl) contentEl.innerHTML = '<div class="px-5 py-8 text-center text-slate-400"><i class="fas fa-spinner fa-spin mr-2"></i>读取日志...</div>';
        if (currentNameEl) currentNameEl.textContent = filename;
        if (currentMetaEl) currentMetaEl.textContent = '内容按文件原始顺序正序读取';

        try {
            const data = await postDashboard({
                action: 'read_log_file',
                filename,
                page: currentPage,
                page_size: getLogPageSize()
            });
            if (data.status !== 'success') {
                if (contentEl) contentEl.innerHTML = `<div class="px-5 py-8 text-center text-red-300">${escapeHtml(data.message || '读取失败')}</div>`;
                return;
            }
            renderLogContent(data.data || {});
            loadLogFiles(true);
        } catch (e) {
            if (contentEl) contentEl.innerHTML = '<div class="px-5 py-8 text-center text-red-300">读取日志失败</div>';
        }
    }

    function renderLogContent(data) {
        const lines = data.lines || [];
        currentPage = data.page || currentPage;
        if (currentNameEl) currentNameEl.textContent = data.filename || currentFilename || '日志文件';
        if (currentMetaEl) {
            currentMetaEl.textContent = `${escapeHtml(data.size_human || '')} · 修改时间 ${escapeHtml(data.mtime_human || '')} · 正序读取`;
        }
        if (!lines.length) {
            if (contentEl) contentEl.innerHTML = '<div class="px-5 py-8 text-center text-slate-400">当前页没有内容</div>';
        } else if (contentEl) {
            contentEl.innerHTML = `<div class="min-w-max">${lines.map(item => `
                <div class="flex hover:bg-slate-800/80">
                    <span class="select-none sticky left-0 bg-slate-900 text-slate-500 text-right w-16 px-3 border-r border-slate-800">${item.line_no}</span>
                    <span class="whitespace-pre px-3 flex-1">${escapeHtml(item.text || '')}</span>
                </div>`).join('')}</div>`;
            contentEl.scrollTop = 0;
            contentEl.scrollLeft = 0;
        }

        const totalLines = data.total_lines || 0;
        const pageSize = data.page_size || getLogPageSize();
        const start = totalLines ? (currentPage - 1) * pageSize + 1 : 0;
        const end = Math.min(currentPage * pageSize, totalLines);
        if (pageInfoEl) pageInfoEl.textContent = `显示 ${start}-${end} 行，共 ${totalLines} 行`;

        const totalPages = data.total_pages || 1;
        let buttons = '';
        if (currentPage > 1) {
            buttons += `<button onclick="readLogFileGlobal('${escapeJsString(currentFilename)}', ${currentPage - 1})" class="px-3 py-1 text-sm bg-white border border-gray-300 rounded hover:bg-gray-50">上一页</button>`;
        }
        buttons += `<span class="px-3 py-1 text-sm text-gray-700">${currentPage} / ${totalPages}</span>`;
        if (currentPage < totalPages) {
            buttons += `<button onclick="readLogFileGlobal('${escapeJsString(currentFilename)}', ${currentPage + 1})" class="px-3 py-1 text-sm bg-white border border-gray-300 rounded hover:bg-gray-50">下一页</button>`;
        }
        if (pageButtonsEl) pageButtonsEl.innerHTML = buttons;
    }

    function escapeJsString(value) {
        return String(value || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n').replace(/\r/g, '\\r');
    }

    if (refreshBtn) refreshBtn.addEventListener('click', () => loadLogFiles(true));
    if (readRefreshBtn) readRefreshBtn.addEventListener('click', () => readLogFile(currentFilename, currentPage));
    if (pageSizeSelect) pageSizeSelect.addEventListener('change', () => readLogFile(currentFilename, 1));
    if (collapseBtn) collapseBtn.addEventListener('click', () => setLogSidebarCollapsed(true));
    if (expandBtn) expandBtn.addEventListener('click', () => setLogSidebarCollapsed(false));

    setLogSidebarCollapsed(false);

    window.loadLogFilesGlobal = () => loadLogFiles(false);
    window.readLogFileGlobal = readLogFile;
}
