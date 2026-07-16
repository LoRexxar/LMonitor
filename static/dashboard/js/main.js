/**
 * Dashboard页面的JavaScript功能
 * version: 20260715h
 */

document.addEventListener('DOMContentLoaded', function() {
    // 初始化页面数据
    initDashboard();

    // 设置定时刷新
    setInterval(refreshData, 30000); // 每30秒刷新一次数据

    // 初始化导航菜单点击事件
    initNavigation();
    initDashboardQuickEntries();

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
    initSimcWorkbench();

    // 默认显示首页内容
    const homeMenuItem = document.querySelector('.nav-item[data-section="dashboard-home"]');
    const homeSection = document.getElementById('dashboard-home');
    const databaseSection = document.getElementById('database-tables');

    if (homeMenuItem && homeSection) {
        // 设置首页菜单为活动状态
        const navItems = document.querySelectorAll('.nav-item');
        navItems.forEach(item => item.classList.remove('active'));
        homeMenuItem.classList.add('active');

        // 显示首页内容，隐藏其他内容
        homeSection.style.display = 'block';
        homeSection.classList.add('active');
        if (databaseSection) {
            databaseSection.style.display = 'none';
            databaseSection.classList.remove('active');
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
    const navItem = document.querySelector(`.nav-item[data-section="${sectionId}"]`);
    if (navItem) {
        const link = navItem.querySelector('a');
        if (link) {
            link.click();
        } else {
            navItem.click();
        }
    }
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

            // 如果点击的是有子菜单的项，切换子菜单的显示/隐藏
            if (this.classList.contains('has-submenu')) {
                this.classList.toggle('open');
                e.preventDefault();
                return;
            }

            e.preventDefault();

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
            if (sectionId !== 'simc-workbench') deactivateSimcWorkbench();

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
                if (sectionId === 'simc-workbench') {
                    switchSimcWorkbenchL1Tab('workflow');
                }
            }
        });
    });

    // 处理子菜单项点击
    submenuItems.forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation(); // 阻止事件冒泡到父级菜单项
            deactivateSimcWorkbench();

            // 移除所有子菜单项的active类
            submenuItems.forEach(i => i.classList.remove('active'));

            // 为当前点击的子菜单项添加active类
            this.classList.add('active');

            // 确保父级菜单项也是active
            const parentNavItem = this.closest('.nav-item');
            navItems.forEach(i => i.classList.remove('active'));
            parentNavItem.classList.add('active');

            // 检查是否是工具菜单项
            const toolName = this.getAttribute('data-tool');
            const tableName = this.getAttribute('data-table');

            if (toolName) {
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
                }
            } else if (tableName) {
                // 处理数据库表菜单项
                const tableTitle = this.querySelector('a').textContent;
                currentTableDisplayName = String(tableTitle || '').trim();

                    // 显示数据库表内容区域
                    contentSections.forEach(section => {
                        section.style.display = 'none';
                        section.classList.remove('active');
                    });
                    const databaseTablesSection = document.getElementById('database-tables');
                    if (databaseTablesSection) {
                        databaseTablesSection.style.display = 'block';
                        databaseTablesSection.classList.add('active');

                        // 更新选中的表名显示
                        const selectedTableName = document.getElementById('selected-table-name');
                        if (selectedTableName) {
                            selectedTableName.textContent = currentTableDisplayName || tableName;
                        }

                        // 获取表数据
                        fetchTableData(tableName);
                    }
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

let wowDailyReportState = {
    selectedDate: '',
    selectedId: null,
    rawMd: '',
};

function initWowDailyReportPage() {
    const refreshBtn = document.getElementById('wow-daily-report-refresh');
    if (refreshBtn) {
        refreshBtn.onclick = () => loadWowDailyReports();
    }
    const genBtn = document.getElementById('wow-daily-report-generate');
    if (genBtn) {
        genBtn.onclick = () => generateWowDailyReport();
    }
    const copyBtn = document.getElementById('wow-daily-report-copy');
    if (copyBtn) {
        copyBtn.onclick = () => copyWowDailyReport();
    }
    const downloadBtn = document.getElementById('wow-daily-report-download');
    if (downloadBtn) {
        downloadBtn.onclick = () => downloadWowDailyReport();
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
            renderWowDailyReportPreview('', '');
            setWowDailyReportActions(false);
            return;
        }

        listEl.innerHTML = '';
        items.forEach((it, idx) => {
            const date = it.report_date || '';
            const updated = it.updated_at || '';
            const id = it.id;
            const btn = document.createElement('button');
            const active = (wowDailyReportState.selectedDate && wowDailyReportState.selectedDate === date) || (!wowDailyReportState.selectedDate && idx === 0);
            btn.className = `w-full text-left px-3 py-2.5 border-b border-gray-100 hover:bg-blue-50 transition-colors duration-200 ${active ? 'bg-blue-50' : ''}`;
            btn.innerHTML = `
                <div class="font-semibold text-gray-900 leading-5">${date || '-'}</div>
                <div class="text-xs text-gray-500 mt-1 leading-4">${updated || ''}</div>
                <div class="text-xs text-gray-400 mt-1">点击预览</div>
            `;
            btn.onclick = () => previewWowDailyReport({ id, date });
            listEl.appendChild(btn);
        });

        if (!wowDailyReportState.selectedDate) {
            const first = items[0];
            await previewWowDailyReport({ id: first.id, date: first.report_date });
        } else {
            const found = items.find(it => it.report_date === wowDailyReportState.selectedDate);
            if (found) {
                await previewWowDailyReport({ id: found.id, date: found.report_date });
            } else {
                const first = items[0];
                await previewWowDailyReport({ id: first.id, date: first.report_date });
            }
        }
        if (hintEl) hintEl.textContent = '同一天重复生成会更新同一份文件内容';
    } catch (e) {
        listEl.innerHTML = `<div class="p-4 text-sm text-red-600">加载失败：${String(e.message || e)}</div>`;
        setWowDailyReportActions(false);
    }
}

async function previewWowDailyReport({ id, date }) {
    const hintEl = document.getElementById('wow-daily-report-hint');
    if (hintEl) hintEl.textContent = '加载内容中...';
    try {
        const url = id ? `/api/wow-daily-report/content/?id=${encodeURIComponent(id)}` : `/api/wow-daily-report/content/?date=${encodeURIComponent(date || '')}`;
        const resp = await fetch(url, { method: 'GET' });
        const data = await resp.json();
        if (!data || !data.success) {
            throw new Error((data && data.error) || '加载失败');
        }
        const payload = data.data || {};
        wowDailyReportState.selectedDate = payload.report_date || (date || '');
        wowDailyReportState.selectedId = payload.id || id || null;
        wowDailyReportState.rawMd = payload.content || '';
        wowDailyReportState.format = payload.format || (String(payload.md_path || '').toLowerCase().endsWith('.html') ? 'html' : 'markdown');
        renderWowDailyReportPreview(wowDailyReportState.selectedDate, wowDailyReportState.rawMd, wowDailyReportState.format);
        setWowDailyReportActions(true);
        if (hintEl) hintEl.textContent = payload.updated_at ? `更新时间：${payload.updated_at}` : '';
    } catch (e) {
        renderWowDailyReportPreview('', '');
        setWowDailyReportActions(false);
        if (hintEl) hintEl.textContent = `加载失败：${String(e.message || e)}`;
    }
}

function renderWowDailyReportPreview(date, content, format) {
    const previewEl = document.getElementById('wow-daily-report-preview');
    const rawEl = document.getElementById('wow-daily-report-raw');
    if (rawEl) rawEl.value = content || '';
    if (!previewEl) return;
    if (!content) {
        previewEl.innerHTML = '<div class="text-sm text-gray-500">请选择一条日报进行预览</div>';
        return;
    }
    if (format === 'html') {
        previewEl.innerHTML = '';
        const iframe = document.createElement('iframe');

        iframe.setAttribute('title', `WoW 日报预览 ${date || ''}`);
        iframe.setAttribute('sandbox', 'allow-popups allow-popups-to-escape-sandbox');
        iframe.srcdoc = content;
        previewEl.appendChild(iframe);
        return;
    }
    try {
        if (window.marked && typeof window.marked.parse === 'function') {
            previewEl.innerHTML = window.marked.parse(content);
            return;
        }
    } catch (e) {}
    previewEl.innerHTML = renderSimpleMarkdown(content);
}

function setWowDailyReportActions(enabled) {
    const copyBtn = document.getElementById('wow-daily-report-copy');
    const downloadBtn = document.getElementById('wow-daily-report-download');
    if (copyBtn) copyBtn.disabled = !enabled;
    if (downloadBtn) downloadBtn.disabled = !enabled;
}

async function copyWowDailyReport() {
    const md = wowDailyReportState.rawMd || '';
    if (!md) return;
    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(md);
            showMessage('已复制到剪贴板', 'success');
            return;
        }
    } catch (e) {}
    try {
        const rawEl = document.getElementById('wow-daily-report-raw');
        if (!rawEl) throw new Error('复制失败');
        rawEl.classList.remove('hidden');
        rawEl.select();
        document.execCommand('copy');
        rawEl.classList.add('hidden');
        showMessage('已复制到剪贴板', 'success');
    } catch (e) {
        showMessage('复制失败', 'warning');
    }
}

function downloadWowDailyReport() {
    const date = wowDailyReportState.selectedDate || '';
    const id = wowDailyReportState.selectedId;
    const url = id ? `/api/wow-daily-report/download/?id=${encodeURIComponent(id)}` : `/api/wow-daily-report/download/?date=${encodeURIComponent(date)}`;
    downloadFileByFetch(url, date);
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

function getSpecBadgeClass(specValue) {
    const spec = String(specValue || '').trim().toLowerCase();
    if (spec === 'fury') return 'bg-orange-100 text-orange-800 border border-orange-200';
    if (spec === 'arms') return 'bg-blue-100 text-blue-800 border border-blue-200';
    if (spec === 'protection') return 'bg-slate-100 text-slate-800 border border-slate-200';
    if (spec === 'fire') return 'bg-red-100 text-red-800 border border-red-200';
    if (spec === 'frost') return 'bg-cyan-100 text-cyan-800 border border-cyan-200';
    if (spec === 'arcane') return 'bg-purple-100 text-purple-800 border border-purple-200';
    return 'bg-gray-100 text-gray-700 border border-gray-200';
}

function getSpecDotClass(specValue) {
    const spec = String(specValue || '').trim().toLowerCase();
    if (spec === 'fury') return 'bg-orange-500 text-white';
    if (spec === 'arms') return 'bg-blue-500 text-white';
    if (spec === 'protection') return 'bg-slate-500 text-white';
    if (spec === 'fire') return 'bg-red-500 text-white';
    if (spec === 'frost') return 'bg-cyan-500 text-white';
    if (spec === 'arcane') return 'bg-purple-500 text-white';
    return 'bg-gray-500 text-white';
}

function renderSpecBadgeHtml(specValue) {
    const spec = String(specValue || '').trim();
    const text = spec || '-';
    const cls = getSpecBadgeClass(spec);
    const dotCls = getSpecDotClass(spec);
    const mark = spec ? spec.charAt(0).toUpperCase() : '?';
    return `<span class="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold ${cls}"><span class="inline-flex items-center justify-center w-4 h-4 rounded-full text-[10px] font-bold ${dotCls}">${escapeHtml(mark)}</span><span>${escapeHtml(text)}</span></span>`;
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
    simcWbCancelProfileDetail();
    dialog.classList.add('hidden');
    document.body.classList.remove('simc-dialog-open');

    if (simcWorkbenchDialogPreviousFocus && typeof simcWorkbenchDialogPreviousFocus.focus === 'function') {
        simcWorkbenchDialogPreviousFocus.focus();
    }
    simcWorkbenchDialogPreviousFocus = null;
}
window.closeSimcWorkbenchDialog = closeSimcWorkbenchDialog;

function getTitleForDialogContent(contentType) {
    const titles = {
        'profile-detail': '配置详情',
        'profile-form': '配置管理',
        'template-detail': '模板详情',
        'template-form': '模板管理',
        'apl-form': 'APL 管理',
        'task-detail': '任务详情',
        'batch-detail': '批次详情'
    };
    return titles[contentType] || '详情';
}

/* ===== SimC Workbench ===== */

function initSimcWorkbench() {
    const workbench = document.getElementById('simc-workbench');
    if (!workbench || workbench.dataset.initialized === '1') return;
    workbench.dataset.initialized = '1';

    // L1 tab switching
    document.querySelectorAll('.simc-l1-tab').forEach(tab => {
        tab.addEventListener('click', function() {
            switchSimcWorkbenchL1Tab(this.getAttribute('data-simc-l1-tab') || 'workflow');
        });
    });
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
                if ((model === 'tasks' || model === 'batches') && typeof window.simcWorkbenchLoadTaskResource === 'function') {
                    window.simcWorkbenchLoadTaskResource(model);
                }
                if (data.ruleSubtab) switchRuleSubtab(model);
            }
        });
    });

    bindSimcWorkbenchSimulationControls();
    bindSimcWorkbenchProfilesControls();
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

function switchSimcWorkbenchL1Tab(l1TabName, childPanelName) {
    const activeL1Tab = l1TabName || 'workflow';
    const defaultPanels = { workflow: 'import', history: 'tasks', advanced: 'backend' };
    const activeChildPanel = childPanelName || defaultPanels[activeL1Tab];

    if (typeof window.simcWorkbenchDeactivatePanel === 'function') {
        window.simcWorkbenchDeactivatePanel(activeChildPanel);
    }
    if (activeChildPanel !== 'profiles') simcWbCancelProfileDetail(true);
    if (activeL1Tab !== 'history') stopSimcAttributeSearch();

    document.querySelectorAll('.simc-l1-tab').forEach(tab => {
        const isActive = tab.getAttribute('data-simc-l1-tab') === activeL1Tab;
        tab.classList.toggle('bg-blue-600', isActive);
        tab.classList.toggle('text-white', isActive);
        tab.classList.toggle('shadow-sm', isActive);
        tab.classList.toggle('bg-white', !isActive);
        tab.classList.toggle('text-gray-700', !isActive);
        tab.classList.toggle('border', !isActive);
        tab.classList.toggle('border-gray-200', !isActive);
        tab.classList.toggle('hover:bg-gray-50', !isActive);
        tab.setAttribute('aria-selected', String(isActive));
    });

    document.querySelectorAll('[data-simc-workflow-entry]').forEach(button => {
        const selected = activeL1Tab === 'workflow' && button.dataset.simcWorkflowEntry === activeChildPanel;
        button.classList.toggle('bg-blue-600', selected);
        button.classList.toggle('text-white', selected);
        button.classList.toggle('bg-white', !selected);
        button.classList.toggle('text-gray-700', !selected);
        button.setAttribute('aria-current', selected ? 'page' : 'false');
    });

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
    if (activeChildPanel === 'rules') { loadSimcWorkbenchRules(); loadSimcWorkbenchMastery(); }
}

function switchSimcWorkbenchTab(tabName) {
    const activeTab = tabName || 'import';
    const parentPanels = {
        import: 'workflow',
        tasks: 'history',
        batches: 'history',
        artifacts: 'history',
        profiles: 'workflow',
        templates: 'workflow',
        apl: 'workflow',
        'apl-keywords': 'advanced',
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
    if (selectedResource === 'mastery-rules') loadSimcWorkbenchMastery();
    else loadSimcWorkbenchRules();
}

/* ===== SimC 工具台 — 配置管理（profiles） ===== */
let simcWbProfileSpecFilter = '';
let simcWbProfilePage = 1;
let simcWbProfileTotalPages = 1;
let simcWbProfileListRequestSerial = 0;
let simcWbProfileListAbortController = null;
let simcWbProfileDetailRequestSerial = 0;
let simcWbProfileDetailAbortController = null;
let simcWbProfileDetailId = '';

function simcWbCancelProfileDetail(clear = false) {
    simcWbProfileDetailRequestSerial += 1;
    if (simcWbProfileDetailAbortController) simcWbProfileDetailAbortController.abort();
    simcWbProfileDetailAbortController = null;
    simcWbProfileDetailId = '';
    if (clear) {
        const host = document.getElementById('simc-wb-profile-detail');
        host?.classList.add('hidden');
        host?.replaceChildren();
    }
}

function loadSimcWorkbenchProfiles(page) {
    page = page || 1;
    simcWbProfilePage = page;
    const requestedPage = page;
    const requestedFilter = simcWbProfileSpecFilter;
    const requestSerial = ++simcWbProfileListRequestSerial;
    if (simcWbProfileListAbortController) simcWbProfileListAbortController.abort();
    const abortController = new AbortController();
    simcWbProfileListAbortController = abortController;
    const tbody = document.getElementById('simc-wb-profile-list');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-gray-400"><i class="fas fa-spinner fa-spin mr-2"></i>加载中…</td></tr>';

    const csrf = getCSRFToken();
    if (!csrf) { tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-red-500">无法获取 CSRF Token</td></tr>'; return; }

    fetch('/api/simc-profile/?include_inactive=1', {
        method: 'GET',
        headers: { 'X-CSRFToken': csrf },
        signal: abortController.signal,
    }).then(r => r.json()).then(data => {
        if (requestSerial !== simcWbProfileListRequestSerial || requestedPage !== simcWbProfilePage || requestedFilter !== simcWbProfileSpecFilter) return;
        if (!data.success) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-6 text-red-500">加载失败</td></tr>';
            return;
        }
        let rows = data.data || [];

        // Client-side spec filtering
        if (requestedFilter) {
            rows = rows.filter(row => {
                const spec = (row.spec || '').toLowerCase();
                const filter = requestedFilter.toLowerCase();
                return spec.includes(filter) || spec === filter;
            });
        }

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
            const mode = row.player_config_mode || 'battlenet';
            const sourceText = mode === 'manual_equipment'
                ? ('手动配置 ' + (row.player_equipment ? ('(' + String(row.player_equipment).split('\n').filter(Boolean).length + ' 行)') : ''))
                : mode === 'attribute_only'
                    ? ('冻结玩家基线 + 绿字覆盖 ' + (row.player_equipment ? ('(' + String(row.player_equipment).split('\n').filter(Boolean).length + ' 行)') : '(历史配置缺少基线)'))
                    : ('Battle.net ' + [row.battlenet_region, row.battlenet_realm, row.battlenet_character].filter(Boolean).join('/'));
            const sourceTitle = escapeHtml(sourceText || '-');
            const offset = startIdx + idx + 1;
            const isActive = row.is_active !== false;
            const launchAction = isActive
                ? `<button class="px-2 py-1 text-xs bg-green-600 hover:bg-green-700 text-white rounded" data-profile-row-action="simulate" data-profile-id="${id}" title="启动模拟">启动模拟</button>`
                : '<span class="inline-flex px-2 py-1 text-xs rounded-full bg-gray-100 text-gray-500">已停用</span>';
            const managementActions = `<button class="text-slate-700 hover:text-slate-900 text-xs" data-profile-row-action="detail" data-profile-id="${id}" title="查看">查看</button>` + (isActive
                ? `<button class="text-green-600 hover:text-green-800 text-xs" data-profile-row-action="load" data-profile-id="${id}" title="加载到发起模拟"><i class="fas fa-arrow-right"></i></button>
                   <button class="text-blue-600 hover:text-blue-800 text-xs" data-profile-row-action="edit" data-profile-id="${id}" title="编辑"><i class="fas fa-edit"></i></button>
                   <button class="text-amber-600 hover:text-amber-800 text-xs" data-profile-row-action="deactivate" data-profile-id="${id}" title="停用"><i class="fas fa-pause"></i></button>`
                : `<button class="text-green-600 hover:text-green-800 text-xs" data-profile-row-action="restore" data-profile-id="${id}" title="恢复"><i class="fas fa-rotate-left mr-1"></i>恢复</button>`);
            return `<tr class="hover:bg-gray-50 border-b border-gray-100 ${isActive ? '' : 'opacity-70'}">
                <td class="px-3 py-3 text-center text-gray-500 text-xs">${offset}</td>
                <td class="px-3 py-3 text-sm font-medium text-gray-900 max-w-[200px] truncate" title="${name}">${name}</td>
                <td class="px-3 py-3 text-center">${renderSpecBadgeHtml(spec)}</td>
                <td class="px-3 py-3 text-xs text-gray-500 max-w-[220px] truncate" title="${sourceTitle}">${sourceTitle}</td>
                <td class="px-3 py-3 text-center">
                    ${launchAction}
                </td>
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

function simcWbShowProfileDetail(id) {
    openSimcWorkbenchDialog('profile-detail', { id });
    const body = document.getElementById('simc-dialog-body');
    if (!body) return;

    simcWbCancelProfileDetail();
    const requestSerial = simcWbProfileDetailRequestSerial;
    simcWbProfileDetailId = String(id);
    simcWbProfileDetailAbortController = new AbortController();
    const abortController = simcWbProfileDetailAbortController;

    body.innerHTML = '<p class="text-sm text-gray-500">正在加载配置…</p>';
    fetch('/api/simc-workbench/profiles/' + encodeURIComponent(id) + '/', {
        headers: { 'Content-Type': 'application/json' },
        signal: abortController.signal,
    }).then(response => response.json()).then(data => {
        if (requestSerial !== simcWbProfileDetailRequestSerial || simcWbProfileDetailId !== String(id)) return;
        if (!data.success || !data.data) throw new Error('load failed');
        const row = data.data;
        const inactive = row.is_active === false;
        body.innerHTML = `${inactive ? '<p class="rounded bg-amber-50 p-3 text-sm text-amber-800 mb-3">此配置已停用，不可加载或运行；恢复后方可使用。</p>' : ''}<dl class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm"><div><dt class="font-semibold text-gray-700">配置名</dt><dd class="mt-1">${escapeHtml(row.name || '-')}</dd></div><div><dt class="font-semibold text-gray-700">专精</dt><dd class="mt-1">${escapeHtml(row.spec || '-')}</dd></div><div><dt class="font-semibold text-gray-700">来源</dt><dd class="mt-1">${escapeHtml(row.player_config_mode || '-')}</dd></div><div><dt class="font-semibold text-gray-700">状态</dt><dd class="mt-1">${inactive ? '已停用' : '启用中'}</dd></div></dl>`;
    }).catch(error => {
        if (error.name === 'AbortError') return;
        if (requestSerial !== simcWbProfileDetailRequestSerial || simcWbProfileDetailId !== String(id)) return;
        body.innerHTML = `<p class="text-sm text-red-600">配置详情加载失败</p><button type="button" data-profile-row-action="detail" data-profile-id="${escapeHtml(id)}" class="mt-2 min-h-[36px] px-3 rounded bg-blue-600 text-white">重试</button>`;
    }).finally(() => {
        if (simcWbProfileDetailAbortController === abortController) simcWbProfileDetailAbortController = null;
    });
}

function bindSimcWorkbenchProfilesControls() {
    const profilePanel = document.getElementById('simc-workbench-profiles-panel');
    if (profilePanel && document.documentElement.dataset.simcProfileActionsBound !== '1') {
        document.documentElement.dataset.simcProfileActionsBound = '1';
        document.addEventListener('click', event => {
            const detailClose = event.target.closest('[data-profile-detail-close]');
            if (detailClose) {
                simcWbCancelProfileDetail(true);
                closeSimcWorkbenchDialog();
                return;
            }
            const formActionButton = event.target.closest('[data-profile-form-action]');
            if (formActionButton) {
                const formAction = formActionButton.dataset.profileFormAction;
                if (formAction === 'create') simcWbToggleProfileForm('create');
                if (formAction === 'close') simcWbCloseProfileForm();
                if (formAction === 'save') simcWbSaveProfile();
                return;
            }
            const rowActionButton = event.target.closest('[data-profile-row-action]');
            if (!rowActionButton) return;
            const rowAction = rowActionButton.dataset.profileRowAction;
            const profileId = rowActionButton.dataset.profileId;
            if (rowAction === 'detail') simcWbShowProfileDetail(profileId);
            if (rowAction === 'simulate') simcWbLaunchSimulation(profileId);
            if (rowAction === 'load') simcWbLoadProfileToSimulator(profileId);
            if (rowAction === 'edit') simcWbEditProfile(profileId);
            if (rowAction === 'deactivate') simcWbSetProfileActive(profileId, false);
            if (rowAction === 'restore') simcWbSetProfileActive(profileId, true);
        });
        document.addEventListener('change', event => {
            if (event.target.matches('#simc-wb-profile-form select[name="player_config_mode"]')) {
                simcWbSyncProfileFormMode();
            }
        });
    }
    /* 填充专精下拉选项 - 使用真实专精 key */
    const specSel = document.getElementById('simc-wb-profile-spec-filter');
    if (specSel && specSel.options.length <= 1 && !specSel.dataset.loaded) {
        specSel.dataset.loaded = '1';
        const specs = [
            'blood','frost_death_knight','unholy',
            'havoc','vengeance',
            'balance','feral','guardian','restoration_druid',
            'devastation','preservation','augmentation',
            'beast_mastery','marksmanship','survival',
            'arcane','fire','frost_mage',
            'brewmaster','windwalker','mistweaver',
            'holy_paladin','protection_paladin','retribution',
            'discipline','holy_priest','shadow',
            'assassination','outlaw','subtlety',
            'elemental','enhancement','restoration_shaman',
            'affliction','demonology','destruction',
            'arms','fury','protection_warrior'
        ];
        specs.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s; opt.textContent = s;
            specSel.appendChild(opt);
        });
    }
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
// 属性型配置没有玩家块；载入后在本地保留其天赋和绿字，供预览/任务提交使用。
let simcWbAttributeOnlyConfig = null;
let simcWbRuleFormMode = 'create';
let simcWbRuleFormEditId = null;

/* --- Profile CRUD --- */
function simcWbToggleProfileForm(mode, profileData) {
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
        formWrap.querySelector('select[name="spec"]').value = 'fury';
        formWrap.querySelector('select[name="player_config_mode"]').value = 'battlenet';
        formWrap.querySelector('input[name="battlenet_region"]').value = 'eu';
        formWrap.querySelector('input[name="battlenet_realm"]').value = '';
        formWrap.querySelector('input[name="battlenet_character"]').value = '';
        formWrap.querySelector('textarea[name="player_equipment"]').value = '';
        formWrap.querySelector('input[name="talent"]').value = '';
        formWrap.querySelector('input[name="gear_strength"]').value = '0';
        formWrap.querySelector('input[name="gear_crit"]').value = '8730';
        formWrap.querySelector('input[name="gear_haste"]').value = '20141';
        formWrap.querySelector('input[name="gear_mastery"]').value = '21785';
        formWrap.querySelector('input[name="gear_versatility"]').value = '7257';
        simcWbAttributeOnlyConfig = null;
    } else {
        simcWbProfileFormEditId = profileData.id;
        formWrap.querySelector('.simc-wb-form-title').textContent = '编辑配置 #' + profileData.id;
        formWrap.querySelector('input[name="name"]').value = profileData.name || '';
        const specSel = formWrap.querySelector('select[name="spec"]');
        specSel.value = profileData.spec || 'fury';
        const profileMode = getSimcProfileMode(profileData);
        formWrap.querySelector('select[name="player_config_mode"]').value = profileMode;
        formWrap.querySelector('input[name="battlenet_region"]').value = profileData.battlenet_region || '';
        formWrap.querySelector('input[name="battlenet_realm"]').value = profileData.battlenet_realm || '';
        formWrap.querySelector('input[name="battlenet_character"]').value = profileData.battlenet_character || '';
        formWrap.querySelector('textarea[name="player_equipment"]').value = profileData.player_equipment || '';
        formWrap.querySelector('input[name="talent"]').value = profileData.talent || '';
        formWrap.querySelector('input[name="gear_strength"]').value = profileData.gear_strength || 0;
        formWrap.querySelector('input[name="gear_crit"]').value = profileData.gear_crit || 0;
        formWrap.querySelector('input[name="gear_haste"]').value = profileData.gear_haste || 0;
        formWrap.querySelector('input[name="gear_mastery"]').value = profileData.gear_mastery || 0;
        formWrap.querySelector('input[name="gear_versatility"]').value = profileData.gear_versatility || 0;
    }

    body.innerHTML = '';
    const dialogForm = formWrap.cloneNode(true);
    dialogForm.id = 'simc-wb-profile-form';
    body.appendChild(dialogForm);
    const clonedForm = body.querySelector('#simc-wb-profile-form');
    if (clonedForm) {
        clonedForm.classList.remove('hidden');
        simcWbSyncProfileFormMode();
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
async function simcWbLaunchSimulation(profileId) {
    const csrf = getCSRFToken();
    if (!csrf) { showMessage('无法获取 CSRF Token', 'error'); return; }
    try {
        const resp = await fetch('/api/simc-profile/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
            body: JSON.stringify({ simulate_now: true, profile_id: profileId })
        });
        const data = await resp.json();
        if (data.success) {
            showMessage('已创建模拟任务并入队，请在任务列表查看', 'success');
            switchSimcWorkbenchL1Tab('history');
        } else {
            showMessage('启动模拟失败: ' + (data.message || data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage('启动模拟失败: ' + e.message, 'error');
    }
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
        battlenet_region: gv('battlenet_region'),
        battlenet_realm: gv('battlenet_realm'),
        battlenet_character: gv('battlenet_character'),
        player_equipment: gv('player_equipment'),
        talent: gv('talent'),
        gear_strength: parseInt(gv('gear_strength')) || 0,
        gear_crit: parseInt(gv('gear_crit')) || 0,
        gear_haste: parseInt(gv('gear_haste')) || 0,
        gear_mastery: parseInt(gv('gear_mastery')) || 0,
        gear_versatility: parseInt(gv('gear_versatility')) || 0,
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
async function simcWbSetProfileActive(id, isActive) {
    try {
        const resp = await fetch('/api/simc-profile/', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ id: Number(id), status_only: true, is_active: isActive })
        });
        const data = await resp.json();
        if (data.success) {
            showMessage(isActive ? '配置已恢复' : '配置已停用', 'success');
            loadSimcWorkbenchProfiles(simcWbProfilePage);
        } else {
            showMessage((isActive ? '恢复失败: ' : '停用失败: ') + (data.message || data.error || '未知错误'), 'error');
        }
    } catch (e) {
        showMessage((isActive ? '恢复失败: ' : '停用失败: ') + e.message, 'error');
    }
}
async function simcWbEditProfile(id) {
    try {
        const resp = await fetch(`/api/simc-profile/${id}/`, {
            method: 'GET',
            headers: { 'X-CSRFToken': getCSRFToken() }
        });
        const data = await resp.json();
        if (data.success) {
            simcWbToggleProfileForm('edit', data);
        } else {
            showMessage('未找到配置', 'error');
        }
    } catch (e) { showMessage('加载配置失败: ' + e.message, 'error'); }
}

async function simcWbLoadProfileToSimulator(id) {
    try {
        const resp = await fetch(`/api/simc-profile/${id}/`, {
            method: 'GET',
            headers: { 'X-CSRFToken': getCSRFToken() }
        });
        const data = await resp.json();
        if (!data.success) { showMessage('未找到配置', 'error'); return; }
        const profile = data;
        const specEl = document.getElementById('simc-sim-spec');
        if (specEl && profile.spec) {
            specEl.value = normalizeSimcSpecKey(profile.spec || '');
            specEl.dispatchEvent(new Event('change'));
        }
        const mode = profile.player_config_mode || 'battlenet';
        document.querySelectorAll('input[name="simc-player-import-mode"]').forEach(r => { r.checked = r.value === mode; });
        if (typeof switchSimcPlayerImportMode === 'function') switchSimcPlayerImportMode(mode);
        simcWbAttributeOnlyConfig = mode === 'attribute_only' ? {
            talent: profile.talent || '', gear_strength: Number(profile.gear_strength || 0), gear_crit: Number(profile.gear_crit || 0),
            gear_haste: Number(profile.gear_haste || 0), gear_mastery: Number(profile.gear_mastery || 0),
            gear_versatility: Number(profile.gear_versatility || 0), profile_id: profile.id,
        } : null;
        if (mode === 'attribute_only') fillSimcAttributeOnlyInputs(simcWbAttributeOnlyConfig);
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
        setVal('simc-sim-battlenet-region', profile.battlenet_region || 'eu');
        setVal('simc-sim-battlenet-realm', profile.battlenet_realm || '');
        setVal('simc-sim-battlenet-character', profile.battlenet_character || '');
        setVal('simc-sim-equipment', ['manual_equipment', 'attribute_only'].includes(mode) ? (profile.player_equipment || '') : '');
        showMessage('已加载配置：' + (profile.name || ('#' + id)), 'success');
        switchSimcWorkbenchL1Tab('workflow');
        setTimeout(() => refreshSimcPlayerDetail(), 0);
    } catch (e) {
        showMessage('加载配置失败: ' + e.message, 'error');
    }
}

async function simcWbSaveCurrentSimulatorProfile() {
    const spec = (document.getElementById('simc-sim-spec')?.value || '').trim();
    if (!spec) { showMessage('请先选择专精', 'error'); return; }
    const mode = document.querySelector('input[name="simc-player-import-mode"]:checked')?.value || 'battlenet';
    const attributeConfig = mode === 'attribute_only' ? syncSimcAttributeOnlyConfigFromInputs() : null;
    if (mode === 'attribute_only' && !attributeConfig.talent) {
        showMessage('请填写天赋构筑码后再保存', 'error'); return;
    }
    const payload = {
        name: spec + '-' + (mode === 'battlenet' ? (document.getElementById('simc-sim-battlenet-character')?.value || 'profile') : 'manual'),
        spec: spec,
        player_config_mode: mode,
        player_import_mode: mode,
        battlenet_region: mode === 'battlenet' ? (document.getElementById('simc-sim-battlenet-region')?.value || '').trim() : '',
        battlenet_realm: mode === 'battlenet' ? (document.getElementById('simc-sim-battlenet-realm')?.value || '').trim() : '',
        battlenet_character: mode === 'battlenet' ? (document.getElementById('simc-sim-battlenet-character')?.value || '').trim() : '',
        player_equipment: ['manual_equipment', 'attribute_only'].includes(mode) ? (document.getElementById('simc-sim-equipment')?.value || '') : '',
        talent: attributeConfig?.talent || '',
        gear_strength: attributeConfig?.gear_strength || 0,
        gear_crit: attributeConfig?.gear_crit || 0,
        gear_haste: attributeConfig?.gear_haste || 0,
        gear_mastery: attributeConfig?.gear_mastery || 0,
        gear_versatility: attributeConfig?.gear_versatility || 0,
    };
    if (mode === 'battlenet' && (!payload.battlenet_region || !payload.battlenet_realm || !payload.battlenet_character)) {
        showMessage('Battle.net 配置需要填写地区、服务器和角色名', 'error'); return;
    }
    if (mode === 'manual_equipment' && !payload.player_equipment.trim()) {
        showMessage('手动配置需要填写装备/天赋玩家块', 'error'); return;
    }
    if (mode === 'attribute_only' && !payload.player_equipment.trim()) {
        showMessage('属性配置需要填写冻结的玩家装备基线', 'error'); return;
    }
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
    showMessage('请确认配置名称和内容后保存', 'info');
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


function simcWbToggleMasteryForm(mode, data) {
    const formWrap = document.getElementById('simc-wb-mastery-form');
    if (!formWrap) return;
    simcWbMasteryFormMode = mode;
    if (mode === 'create') {
        simcWbMasteryFormEditId = null;
        formWrap.querySelector('.simc-wb-form-title').textContent = '新增精通系数';
        formWrap.querySelector('input[name="spec"]').value = '';
        formWrap.querySelector('input[name="mastery_coefficient"]').value = '';
    } else {
        simcWbMasteryFormEditId = data.id;
        formWrap.querySelector('.simc-wb-form-title').textContent = '编辑精通系数 #' + data.id;
        formWrap.querySelector('input[name="spec"]').value = data.spec || '';
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
    const spec = formWrap.querySelector('input[name="spec"]').value.trim();
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

function syncSimcAttributeOnlyConfigFromInputs() {
    const value = id => (document.getElementById(id)?.value || '').trim();
    simcWbAttributeOnlyConfig = {
        talent: value('simc-sim-attribute-talent'),
        gear_strength: Math.max(0, parseInt(value('simc-sim-attribute-strength'), 10) || 0),
        gear_crit: Math.max(0, parseInt(value('simc-sim-attribute-crit'), 10) || 0),
        gear_haste: Math.max(0, parseInt(value('simc-sim-attribute-haste'), 10) || 0),
        gear_mastery: Math.max(0, parseInt(value('simc-sim-attribute-mastery'), 10) || 0),
        gear_versatility: Math.max(0, parseInt(value('simc-sim-attribute-versatility'), 10) || 0),
        profile_id: simcWbAttributeOnlyConfig?.profile_id || null,
    };
    return simcWbAttributeOnlyConfig;
}

function fillSimcAttributeOnlyInputs(config) {
    const value = config || {};
    const setValue = (id, input) => { const el = document.getElementById(id); if (el) el.value = input ?? ''; };
    setValue('simc-sim-attribute-talent', value.talent || '');
    setValue('simc-sim-attribute-strength', value.gear_strength || 0);
    setValue('simc-sim-attribute-crit', value.gear_crit || 0);
    setValue('simc-sim-attribute-haste', value.gear_haste || 0);
    setValue('simc-sim-attribute-mastery', value.gear_mastery || 0);
    setValue('simc-sim-attribute-versatility', value.gear_versatility || 0);
}

function switchSimcPlayerImportMode(mode) {
    const battlenetSection = document.getElementById('simc-player-battlenet-section');
    const equipmentSection = document.getElementById('simc-player-equipment-section');
    const attributeSection = document.getElementById('simc-player-attribute-only-section');
    if (battlenetSection) battlenetSection.classList.toggle('hidden', mode !== 'battlenet');
    if (equipmentSection) equipmentSection.classList.toggle('hidden', !['manual_equipment', 'attribute_only'].includes(mode));
    if (attributeSection) attributeSection.classList.toggle('hidden', mode !== 'attribute_only');
    if (mode === 'attribute_only') {
        fillSimcAttributeOnlyInputs(simcWbAttributeOnlyConfig);
    }
}

function parseSpecFromPlayerBlock(playerBlock) {
    if (!playerBlock) return null;
    const text = String(playerBlock).toLowerCase();
    const specMatch = text.match(/^\s*spec\s*=\s*(\w+)/m);
    return specMatch ? specMatch[1] : null;
}

function autoSelectSpecIfSafe(parsedSpec) {
    if (!parsedSpec) return false;
    const normalized = normalizeSimcSpecKey(parsedSpec);
    if (!normalized) return false;
    const specEl = document.getElementById('simc-sim-spec');
    if (!specEl) return false;
    const currentSpec = (specEl.value || '').trim();
    if (currentSpec && currentSpec !== normalized) return false;
    specEl.value = normalized;
    specEl.dispatchEvent(new Event('change'));
    return true;
}

function switchSimcPlayerConfigMode(mode) {
    // 兼容旧入口：旧 equipment 视为手动装备。
    if (mode === 'equipment') mode = 'manual_equipment';
    if (mode === 'battlenet' || mode === 'manual_equipment' || mode === 'attribute_only') {
        switchSimcPlayerImportMode(mode);
        return;
    }
}

const SIMC_SPEC_CLASS_MAP = {
    arms: 'warrior', fury: 'warrior', protection: 'warrior',
    blood: 'death_knight', frost_dk: 'death_knight', unholy: 'death_knight',
    havoc: 'demon_hunter', vengeance: 'demon_hunter',
    balance: 'druid', feral: 'druid', guardian: 'druid', restoration: 'druid',
    devastation: 'evoker', preservation: 'evoker', augmentation: 'evoker',
    beast_mastery: 'hunter', marksmanship: 'hunter', survival: 'hunter',
    arcane: 'mage', fire: 'mage', frost: 'mage',
    brewmaster: 'monk', mistweaver: 'monk', windwalker: 'monk',
    holy: 'priest', discipline: 'priest', shadow: 'priest',
    retribution: 'paladin',
    assassination: 'rogue', outlaw: 'rogue', subtlety: 'rogue',
    elemental: 'shaman', enhancement: 'shaman', restoration_shaman: 'shaman',
    affliction: 'warlock', demonology: 'warlock', destruction: 'warlock',
};

function normalizeSimcSpecKey(spec) {
    let key = String(spec || '').trim().toLowerCase();
    if (!key) return '';
    // 数据库里的 APL/旧配置可能是 warrior_fury 这类 class_spec，导入区统一用短专精过滤。
    const directAliases = {
        deathknight_frost: 'frost_dk', death_knight_frost: 'frost_dk', dk_frost: 'frost_dk',
        shaman_restoration: 'restoration_shaman', resto_shaman: 'restoration_shaman',
    };
    if (directAliases[key]) return directAliases[key];
    if (SIMC_SPEC_CLASS_MAP[key]) return key;
    const parts = key.split('_');
    for (let i = 1; i < parts.length; i++) {
        const suffix = parts.slice(i).join('_');
        if (SIMC_SPEC_CLASS_MAP[suffix]) return suffix;
    }
    return key;
}

function getSimcSpecClass(spec) {
    return SIMC_SPEC_CLASS_MAP[normalizeSimcSpecKey(spec)] || '';
}

function getCurrentSimcWorkbenchSpecFilter() {
    const spec = normalizeSimcSpecKey((document.getElementById('simc-sim-spec') || {}).value || '');
    return { spec, className: getSimcSpecClass(spec) };
}

function filterSimcProfilesForCurrentImport(profiles) {
    const filter = getCurrentSimcWorkbenchSpecFilter();
    if (!filter.spec) return { profiles, filter };
    const exact = profiles.filter(p => normalizeSimcSpecKey(p.spec) === filter.spec);
    if (exact.length) return { profiles: exact, filter };
    const sameClass = profiles.filter(p => getSimcSpecClass(p.spec) && getSimcSpecClass(p.spec) === filter.className);
    return { profiles: sameClass, filter };
}

async function loadSimcAplCandidates(spec) {
    const container = document.getElementById('simc-sim-apl-list');
    const editor = document.getElementById('apl-override');
    if (!container) return;
    if (editor) editor.value = '';
    if (!spec) {
        container.className = 'rounded-xl border border-dashed border-gray-300 bg-gray-50 p-4 text-sm text-gray-500';
        container.innerHTML = '请先选择专精以加载 APL 列表。';
        return;
    }
    container.className = 'rounded-xl border border-dashed border-gray-300 bg-gray-50 p-4 text-sm text-gray-500';
    container.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>加载 APL 列表中…';
    try {
        const className = getSimcSpecClass(spec);
        const qs = new URLSearchParams({ spec: spec });
        if (className) qs.set('class_name', className);
        const resp = await fetch('/api/simc-apl-candidates/?' + qs.toString());
        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || data.message || '加载失败');
        const candidates = Array.isArray(data.data) ? data.data : (data.candidates || []);
        if (!candidates.length) {
            container.className = 'rounded-xl bg-amber-50 border border-amber-200 text-amber-800 p-4 text-sm';
            container.innerHTML = '当前专精没有可选 APL，将按默认逻辑运行。';
            return;
        }
        container.className = 'rounded-xl border border-blue-200 bg-blue-50 p-4 text-sm text-gray-700';
        container.innerHTML = candidates.map(apl => `
            <label class="flex items-start gap-2 rounded-xl border border-gray-200 bg-white p-3 mb-2 cursor-pointer hover:bg-gray-50 transition-colors">
                <input type="radio" name="simc-sim-apl" value="${escapeHtml(String(apl.id || ''))}" class="mt-1 h-4 w-4 text-blue-600 border-gray-300" ${candidates.length === 1 ? 'checked' : ''}>
                <span>
                    <span class="font-semibold text-gray-900">${escapeHtml(apl.name || apl.spec || 'APL')}</span>
                    <span class="ml-2 text-xs px-2 py-0.5 rounded-full ${apl.template_type === 'default_apl' ? 'bg-sky-100 text-sky-700' : 'bg-violet-100 text-violet-700'}">${apl.template_type === 'default_apl' ? '默认自动同步' : '个人维护'}</span>
                    <span class="block text-xs text-gray-500">${escapeHtml(apl.spec || '')} · ${escapeHtml(apl.source || '')} · ${apl.content_length || 0} 字符</span>
                </span>
            </label>
        `).join('');
        container.querySelectorAll('input[name="simc-sim-apl"]').forEach((radio) => {
            radio.addEventListener('change', () => loadSimcAplOverride(radio.value));
        });
        const selected = container.querySelector('input[name="simc-sim-apl"]:checked');
        if (selected) await loadSimcAplOverride(selected.value);
    } catch (err) {
        console.error('Load APL candidates failed:', err);
        container.className = 'rounded-xl bg-red-50 border border-red-200 text-red-800 p-4 text-sm';
        container.innerHTML = '加载 APL 失败：' + escapeHtml(String(err.message || err));
    }
}

async function loadSimcAplOverride(aplId) {
    const editor = document.getElementById('apl-override');
    if (!editor) return;
    if (!aplId) { editor.value = ''; return; }
    const response = await fetch('/api/simc-template/?id=' + encodeURIComponent(aplId));
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '加载 APL 内容失败');
    editor.value = payload.content || payload.template_content || '';
}

async function loadSimcBaseTemplateContent(templateId) {
    const editor = document.getElementById('base-template-content');
    if (!editor) return;
    if (!templateId) { editor.value = ''; return; }
    const response = await fetch('/api/simc-template/?id=' + encodeURIComponent(templateId));
    const payload = await response.json();
    if (!response.ok || !payload.success) throw new Error(payload.error || '加载基础模板内容失败');
    editor.value = payload.content || payload.template_content || '';
}

async function loadSimcSnapshotDefaults(spec) {
    const baseSelect = document.getElementById('base-template-select');
    const baseEditor = document.getElementById('base-template-content');
    const baselineEditor = document.getElementById('player-baseline-config');
    if (!spec) {
        if (baseSelect) baseSelect.innerHTML = '<option value="">使用默认基础模板</option>';
        if (baseEditor) baseEditor.value = '';
        if (baselineEditor) baselineEditor.value = '';
        return;
    }
    if (baseSelect) {
        const response = await fetch('/api/simc-template/?template_type=base_template');
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载基础模板失败');
        const normalized = normalizeSimcSpecKey(spec);
        const rows = (payload.templates || []).filter((row) => {
            if (row.is_active === false) return false;
            const rowSpec = normalizeSimcSpecKey(row.spec || '');
            const rawSpec = String(row.spec || '').trim().toLowerCase();
            return !rowSpec || ['default', 'all', '*'].includes(rawSpec) || rowSpec === normalized;
        });
        baseSelect.innerHTML = '<option value="">使用唯一启用的默认基础模板</option>' + rows.map((row) =>
            `<option value="${escapeHtml(String(row.id))}">${escapeHtml(row.name || row.spec || ('模板 #' + row.id))}</option>`
        ).join('');
        const exactRows = rows.filter((row) => normalizeSimcSpecKey(row.spec || '') === normalized);
        if (exactRows.length === 1) baseSelect.value = String(exactRows[0].id);
        else if (rows.length === 1) baseSelect.value = String(rows[0].id);
        baseSelect.onchange = () => loadSimcBaseTemplateContent(baseSelect.value).catch((err) => {
            console.error('Load base template content failed:', err);
            showMessage('加载基础模板内容失败：' + String(err.message || err), 'error');
        });
        await loadSimcBaseTemplateContent(baseSelect.value);
    }
    if (baselineEditor) {
        const response = await fetch('/api/simc-player-config-detail/?spec=' + encodeURIComponent(spec));
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '加载默认玩家基线失败');
        baselineEditor.value = (payload.data || {}).player_equipment || '';
        const legacyEditor = document.getElementById('simc-sim-equipment');
        if (legacyEditor && !legacyEditor.value.trim()) legacyEditor.value = baselineEditor.value;
    }
}

async function simcWbFetchProfilesForWorkbench() {
    const response = await fetch('/api/simc-profile/', {
        method: 'GET',
        headers: { 'X-CSRFToken': getCSRFToken() },
    });
    const data = await response.json();
    if (!response.ok || !data.success) {
        throw new Error(data.error || data.message || '加载保存配置失败');
    }
    return Array.isArray(data.data) ? data.data : (data.profiles || []);
}

async function loadSimcSimProfileSelect() {
    const select = document.getElementById('simc-sim-profile-select');
    if (!select) return;
    select.innerHTML = '<option value="">加载中…</option>';
    try {
        const resp = await fetch('/api/simc-profile/');
        const data = await resp.json();
        if (!resp.ok || !data.success) throw new Error(data.error || '加载失败');
        const profiles = Array.isArray(data.data) ? data.data : (data.profiles || []);
        if (!profiles.length) {
            select.innerHTML = '<option value="">暂无已保存配置</option>';
            return;
        }
        select.innerHTML = '<option value="">-- 请选择配置 --</option>' +
            profiles.map(p => `<option value="${p.id}" data-spec="${escapeHtml(p.spec || '')}" data-talent="${escapeHtml(p.talent || '')}" data-gear-crit="${p.gear_crit || 0}" data-gear-haste="${p.gear_haste || 0}" data-gear-mastery="${p.gear_mastery || 0}" data-gear-versatility="${p.gear_versatility || 0}">${escapeHtml(p.name || '配置#' + p.id)} (${escapeHtml(p.spec || '')})</option>`).join('');
    } catch (err) {
        console.error('Load profiles failed:', err);
        select.innerHTML = '<option value="">加载失败</option>';
    }
}

async function loadSimcSimSavedProfiles() {
    const container = document.getElementById('simc-sim-saved-profiles');
    if (!container) return;
    const filter = getCurrentSimcWorkbenchSpecFilter();
    const filterText = filter.spec ? `（${filter.spec}${filter.className ? ' / ' + filter.className : ''}）` : '';
    container.innerHTML = `<div class="text-gray-400 py-1 text-xs"><i class="fas fa-spinner fa-spin mr-1"></i>加载保存配置${escapeHtml(filterText)}…</div>`;
    try {
        const allProfiles = await simcWbFetchProfilesForWorkbench();
        const filtered = filterSimcProfilesForCurrentImport(allProfiles);
        const profiles = filtered.profiles;
        if (!profiles.length) {
            const hint = filtered.filter.spec
                ? `当前专精 ${escapeHtml(filtered.filter.spec)} 没有已保存配置。请切换专精、在“配置管理”新增，或直接填写玩家信息后保存当前。`
                : '暂无已保存配置';
            container.innerHTML = `<div class="text-gray-400 py-1 text-xs">${hint}</div>`;
            return;
        }
        container.innerHTML = profiles.map(p => {
            const mode = getSimcProfileMode(p);
            const source = mode === 'manual_equipment'
                ? ('手动配置 ' + (p.player_equipment ? String(p.player_equipment).split('\n').filter(Boolean).length + ' 行' : ''))
                : mode === 'attribute_only'
                    ? ('冻结玩家基线 + 绿字覆盖 ' + (p.player_equipment ? String(p.player_equipment).split('\n').filter(Boolean).length + ' 行' : '（历史配置缺少基线）'))
                    : ('Battle.net ' + [p.battlenet_region, p.battlenet_realm, p.battlenet_character].filter(Boolean).join('/'));
            const spec = normalizeSimcSpecKey(p.spec || '');
            const className = getSimcSpecClass(p.spec || '');
            return `
            <button type="button" class="simc-sim-load-profile flex w-full items-center gap-2 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 mb-1 text-left text-xs hover:border-blue-300 hover:bg-blue-50" data-profile-id="${p.id}" title="${escapeHtml(source || '-')}">
                <span class="min-w-0 flex-1 truncate font-medium text-gray-800">${escapeHtml(p.name || '配置#' + p.id)}</span>
                <span class="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">${escapeHtml(spec || p.spec || '-')}</span>
                <span class="max-w-24 shrink-0 truncate text-[10px] text-gray-400">${escapeHtml(source || '-')}</span>
            </button>`;
        }).join('');
        container.querySelectorAll('.simc-sim-load-profile').forEach(btn => btn.addEventListener('click', () => simcWbLoadProfileToSimulator(btn.dataset.profileId)));
    } catch (err) {
        console.error('Load saved SimC profiles failed:', err);
        container.innerHTML = '<div class="text-red-500 text-center py-2">加载失败：' + escapeHtml(String(err.message || err)) + '</div>';
    }
}

function refreshSimcSavedProfiles() {
    return loadSimcSimSavedProfiles();
}

function getSimcProfileMode(profile) {
    const storedMode = (profile.player_config_mode || profile.player_import_mode || '').trim();
    const hasEquipment = Boolean(profile.player_equipment);
    const hasBattlenetIdentity = Boolean(profile.battlenet_region || profile.battlenet_realm || profile.battlenet_character);
    // 迁移前的属性型记录会被新增字段默认标记为 battlenet。不能只信 mode：
    // 没有 Battle.net 三元组、没有装备时，真实的 talent + ratings 才是配置来源。
    if (storedMode === 'attribute_only') return 'attribute_only';
    if (hasEquipment) return 'manual_equipment';
    if (hasBattlenetIdentity) return 'battlenet';
    return 'attribute_only';
}

function onSimcProfileSelect() {
    // 已保存配置由右侧列表载入，具体回填由 simcWbLoadProfileToSimulator 处理。
}

function renderSimcPlayerDetail(detail) {
    const container = document.getElementById('simc-sim-player-detail');
    if (!container) return;
    if (!detail) { container.textContent = '暂无可展示的玩家配置。'; return; }
    const identity = detail.identity || {};
    const talents = detail.talents || {};
    const stats = detail.stats || {};
    const source = detail.source || {};
    const esc = value => escapeHtml(String(value == null || value === '' ? '-' : value));
    const professionText = Object.entries(identity.professions || {}).map(([name, level]) => `${esc(name)} ${esc(level)}`).join(' / ');
    const savedLoadouts = (talents.saved_loadouts || []).filter(loadout => loadout.build_code).map(loadout => `<div class="mt-1 text-xs text-gray-600"><b>${esc(loadout.name)}</b>：<span class="font-mono break-all">${esc(loadout.build_code)}</span></div>`).join('');
    const omniumTalents = (detail.omnium_talents || []).length ? `<div class="mt-1 text-xs text-gray-600">永恒天赋：${detail.omnium_talents.map(t => '#' + esc(t.id) + ' ×' + esc(t.rank)).join('、')}</div>` : '';
    const secondaryLabels = { crit: '暴击', haste: '急速', mastery: '精通', versatility: '全能' };
    const secondaryRows = Object.entries(stats.secondary || {}).map(([key, stat]) =>
        `<div class="rounded bg-white/80 border border-emerald-100 px-2 py-1"><span class="text-gray-500">${secondaryLabels[key] || key}</span> <b class="text-gray-800">${esc(stat.rating)}</b> <span class="text-gray-500">绿字${stat.percent == null ? '' : ' / ' + esc(stat.percent) + '%'}</span></div>`
    ).join('') || '<span class="text-gray-400">未提供副属性绿字</span>';
    const equipment = (detail.equipment || []).map(item => {
        const ench = item.enchant ? `<div class="text-[11px] text-violet-700">附魔：${esc(item.enchant.display_name)}</div>` : '';
        const gems = (item.gems || []).length ? `<div class="text-[11px] text-cyan-700">宝石：${item.gems.map(g => esc(g.display_name)).join('、')}</div>` : '';
        const crafted = (item.crafted_stats || []).length ? `<div class="text-[11px] text-orange-700">制作属性：${item.crafted_stats.map(esc).join(' / ')}${item.crafting_quality ? ' · 品质 ' + esc(item.crafting_quality) : ''}</div>` : '';
        const name = item.wowhead_url ? `<a class="text-blue-700 hover:underline" href="${esc(item.wowhead_url)}">${esc(item.display_name)}</a>` : esc(item.display_name);
        return `<div class="rounded-lg bg-white border border-emerald-100 p-2"><div class="text-[11px] text-gray-500">${esc(item.slot_label)}</div><div class="font-medium text-gray-800">${name} <span class="text-xs text-gray-400">${item.item_level ? 'ilvl ' + esc(item.item_level) : '#' + esc(item.id)}</span></div>${ench}${gems}${crafted}</div>`;
    }).join('') || '<div class="text-gray-400">未解析到装备槽位。</div>';
    const missing = (detail.missing_fields || []).map(text => `<li>${esc(text)}</li>`).join('');
    const comparison = detail.comparison_candidates || {};
    renderSimcComparisonCandidates(comparison);
    container.innerHTML = `
        <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-gray-600 mb-3"><span>来源：<b>${esc(source.label)}</b></span><span>角色：<b>${esc(identity.name)}</b></span><span>职业/专精：<b>${esc(identity.class_name)} / ${esc(identity.spec)}</b></span>${identity.race ? `<span>种族：<b>${esc(identity.race)}</b></span>` : ''}${identity.level ? `<span>等级：<b>${esc(identity.level)}</b></span>` : ''}${identity.role ? `<span>定位：<b>${esc(identity.role)}</b></span>` : ''}${identity.region ? `<span>地区/服务器：<b>${esc(identity.region)} / ${esc(identity.realm)}</b></span>` : ''}${professionText ? `<span>专业：<b>${professionText}</b></span>` : ''}</div>
        <div class="grid md:grid-cols-2 gap-3 mb-3"><div class="rounded-lg bg-white/70 border border-emerald-100 p-2"><div class="text-xs text-gray-500">当前天赋构筑码</div><div class="font-mono text-xs break-all text-gray-800">${esc(talents.build_code)}</div>${savedLoadouts}${omniumTalents}</div><div class="rounded-lg bg-white/70 border border-emerald-100 p-2"><div class="text-xs text-gray-500 mb-1">主属性</div><div class="text-xs text-gray-700">${Object.entries(stats.primary || {}).map(([key, value]) => esc(key) + ' ' + esc(value)).join(' · ') || '导出块未包含主属性数值'}</div></div></div>
        <div class="mb-3"><div class="text-xs text-gray-500 mb-1">副属性（rating / 按规则换算百分比）</div><div class="grid grid-cols-2 gap-2 text-xs">${secondaryRows}</div></div>
        <div><div class="text-xs text-gray-500 mb-1">装备、附魔与宝石</div><div class="grid md:grid-cols-2 gap-2">${equipment}</div></div>
        ${missing ? `<ul class="mt-3 text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg p-2 list-disc list-inside">${missing}</ul>` : ''}`;
}

function renderSimcComparisonCandidates(comparison) {
    const container = document.getElementById('simc-sim-comparison-candidates');
    if (!container) return;
    const gear = Array.isArray(comparison.gear) ? comparison.gear : [];
    const trinkets = gear.filter(row => ['trinket1', 'trinket2'].includes(String(row.slot || '').toLowerCase()));
    const otherGear = gear.filter(row => !['trinket1', 'trinket2'].includes(String(row.slot || '').toLowerCase()));
    const talents = Array.isArray(comparison.talents) ? comparison.talents : [];
    if (!gear.length && !talents.length) { container.classList.add('hidden'); container.innerHTML = ''; return; }
    const max = Math.max(1, Number(comparison.max_selectable || 7));
    const esc = value => escapeHtml(String(value == null ? '' : value));
    const gearRows = (rows, cssClass) => rows.map(row => `<label class="flex gap-2 items-start text-xs rounded border border-gray-200 bg-white p-2"><input class="${cssClass} mt-0.5" type="checkbox" data-slot="${esc(row.slot)}" data-item-id="${esc(row.item_id)}" data-source="${esc(row.source)}"><span><b>${esc(row.name || `${row.slot} #${row.item_id}`)}</b><span class="block text-gray-500">${esc(row.slot)} · #${esc(row.item_id)} · ${esc(row.source)}</span></span></label>`).join('');
    const talentRows = talents.map(row => `<label class="flex gap-2 items-start text-xs rounded border border-gray-200 bg-white p-2"><input class="simc-comparison-talent mt-0.5" type="checkbox" data-talent="${esc(row.talent)}"><span><b>${esc(row.name || '候选天赋')}</b><span class="block font-mono break-all text-gray-500">${esc(row.talent)}</span></span></label>`).join('');
    const categoryHeader = kind => ({
        trinket_candidates: '<div class="flex items-center gap-2"><input id="simc-comparison-kind-trinket_candidates" type="checkbox" class="simc-comparison-kind-toggle" data-simc-comparison-kind="trinket_candidates"><label for="simc-comparison-kind-trinket_candidates" class="text-xs font-semibold text-gray-700">饰品候选</label></div>',
        gear_candidates: '<div class="flex items-center gap-2"><input id="simc-comparison-kind-gear_candidates" type="checkbox" class="simc-comparison-kind-toggle" data-simc-comparison-kind="gear_candidates"><label for="simc-comparison-kind-gear_candidates" class="text-xs font-semibold text-gray-700">其他装备候选</label></div>',
        talent_candidates: '<div class="flex items-center gap-2"><input id="simc-comparison-kind-talent_candidates" type="checkbox" class="simc-comparison-kind-toggle" data-simc-comparison-kind="talent_candidates"><label for="simc-comparison-kind-talent_candidates" class="text-xs font-semibold text-gray-700">天赋候选</label></div>',
    })[kind] || '';
    container.classList.remove('hidden');
    container.innerHTML = `<div class="font-semibold text-gray-800"><i class="fas fa-code-branch text-indigo-600 mr-1"></i>多方案对比</div><p class="text-xs text-gray-500 mt-1">候选仅来自当前已加载的手动玩家块；勾选类别后会各自创建独立批次和排名，不会混合饰品、其他装备与天赋。</p>${trinkets.length ? `<div class="mt-3">${categoryHeader('trinket_candidates')}<div class="grid gap-2 mt-2">${gearRows(trinkets, 'simc-comparison-trinket')}</div></div>` : ''}${otherGear.length ? `<div class="mt-3">${categoryHeader('gear_candidates')}<div class="grid gap-2 mt-2">${gearRows(otherGear, 'simc-comparison-gear')}</div></div>` : ''}${talents.length ? `<div class="mt-3">${categoryHeader('talent_candidates')}<div class="grid gap-2 mt-2">${talentRows}</div></div>` : ''}<div class="mt-3 flex items-center justify-between gap-2"><span class="text-xs text-gray-500">每个类别最多 ${max} 个候选（含基准最多 ${max + 1} 个方案）。</span><button type="button" class="simc-comparison-submit px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-xs">对比已勾选类别</button></div>`;
    container.querySelector('.simc-comparison-submit')?.addEventListener('click', () => startSelectedSimcCandidateComparisons(max));
}

function getSelectedSimcComparisonCandidates(kind) {
    if (kind === 'trinket_candidates') return Array.from(document.querySelectorAll('.simc-comparison-trinket:checked')).map(el => ({ slot: el.dataset.slot, item_id: Number(el.dataset.itemId), source: el.dataset.source }));
    if (kind === 'gear_candidates') return Array.from(document.querySelectorAll('.simc-comparison-gear:checked')).map(el => ({ slot: el.dataset.slot, item_id: Number(el.dataset.itemId), source: el.dataset.source }));
    return Array.from(document.querySelectorAll('.simc-comparison-talent:checked')).map(el => ({ talent: el.dataset.talent }));
}

let simcCandidatePollControl = null;
let simcCandidateGeneration = 0;

function isCurrentSimcCandidateControl(control) {
    return Boolean(control && !control.cancelled && simcCandidatePollControl === control && control.generation === simcCandidateGeneration);
}

async function startSelectedSimcCandidateComparisons(maxSelectable) {
    const spec = (document.getElementById('simc-sim-spec') || {}).value || '';
    const playerEquipment = ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
    const selectedKinds = Array.from(document.querySelectorAll('.simc-comparison-kind-toggle:checked')).map(el => el.dataset.simcComparisonKind);
    if (!spec || !playerEquipment) { showMessage('请先选择专精并填写手动装备玩家块', 'warning'); return; }
    if (!selectedKinds.length) { showMessage('请至少勾选一个候选类别', 'warning'); return; }
    const selections = Object.fromEntries(selectedKinds.map(kind => [kind, getSelectedSimcComparisonCandidates(kind)]));
    const emptyKind = selectedKinds.find(kind => !selections[kind].length);
    if (emptyKind) { showMessage('已勾选的类别请至少选择一个候选方案', 'warning'); return; }
    const oversizedKind = selectedKinds.find(kind => selections[kind].length > maxSelectable);
    if (oversizedKind) { showMessage(`每个类别最多选择 ${maxSelectable} 个候选（含基准最多 ${maxSelectable + 1} 个方案）`, 'warning'); return; }
    stopSimcCandidateComparisonPolling();
    const button = document.querySelector('.simc-comparison-submit');
    const old = button?.innerHTML;
    const control = { timer: null, resolve: null, button, oldLabel: old, cancelled: false, controller: new AbortController(), generation: ++simcCandidateGeneration };
    simcCandidatePollControl = control;
    try {
        if (button) { button.disabled = true; button.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>创建批次中…'; }
        const apl = document.querySelector('input[name="simc-sim-apl"]:checked');
        const baseTemplate = document.getElementById('base-template-select');
        const baseTemplateEditor = document.getElementById('base-template-content');
        const aplOverride = document.getElementById('apl-override');
        // 类别按用户勾选顺序串行：前一个批次创建/运行失败时，不再继续创建后续类别。
        const batches = [];
        for (const kind of selectedKinds) {
            if (!isCurrentSimcCandidateControl(control)) return;
            const requestKind = kind === 'trinket_candidates' ? 'gear_candidates' : kind;
            const response = await fetch('/api/simc-task/batch/', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() }, signal: control.controller.signal, body: JSON.stringify({ kind: requestKind, category: kind, name: `${spec} ${kind === 'trinket_candidates' ? '饰品' : kind === 'gear_candidates' ? '其他装备' : '天赋'}候选对比`, spec, player_config_mode: 'manual_equipment', player_equipment: playerEquipment, candidates: selections[kind], fight_style: (document.getElementById('simc-sim-fight-style') || {}).value || 'Patchwerk', time: parseInt((document.getElementById('simc-sim-time') || {}).value || '300', 10) || 300, target_count: parseInt((document.getElementById('simc-sim-target-count') || {}).value || '1', 10) || 1, selected_apl_id: apl?.value ? parseInt(apl.value, 10) : undefined, override_action_list: aplOverride ? aplOverride.value : undefined, base_template_id: baseTemplate?.value ? parseInt(baseTemplate.value, 10) : undefined, base_template_content: baseTemplateEditor ? baseTemplateEditor.value : undefined }) });
            const payload = await response.json();
            if (!isCurrentSimcCandidateControl(control)) return;
            if (!response.ok || !payload.success) throw new Error(payload.error || '创建比较批次失败');
            const batch = { batchId: payload.data.batch_id, kind, accepted: payload.data.accepted };
            batches.push(batch);
            showMessage(`已创建 ${kind} 独立比较批次，正在等待模拟完成…`, 'success');
            switchSimcWorkbenchL1Tab('history');
            if (!isCurrentSimcCandidateControl(control)) return;
            const completed = await pollSimcCandidateComparison(batch.batchId, batch.kind, control);
            if (!completed || !isCurrentSimcCandidateControl(control)) return;
        }
        if (isCurrentSimcCandidateControl(control)) showMessage(`已完成 ${batches.length} 个已勾选类别的独立比较`, 'success');
    } catch (error) {
        if (error.name !== 'AbortError' && isCurrentSimcCandidateControl(control)) showMessage('创建多方案对比失败：' + String(error.message || error), 'error');
    } finally {
        if (isCurrentSimcCandidateControl(control)) {
            if (button) { delete button.dataset.simcPolling; button.disabled = false; button.innerHTML = old; }
            simcCandidatePollControl = null;
        }
    }
}

function stopSimcCandidateComparisonPolling() {
    if (!simcCandidatePollControl) return;
    const control = simcCandidatePollControl;
    control.cancelled = true;
    simcCandidateGeneration += 1;
    control.controller.abort();
    if (simcCandidatePollControl.timer) clearTimeout(simcCandidatePollControl.timer);
    simcCandidatePollControl = null;
    if (control.resolve) control.resolve(false);
    if (control.button) {
        delete control.button.dataset.simcPolling;
        control.button.disabled = false;
        control.button.innerHTML = control.oldLabel;
    }
}

function pollSimcCandidateComparison(batchId, kind, control) {
    if (!batchId || !isCurrentSimcCandidateControl(control)) return Promise.resolve(false);
    if (control.button) { control.button.dataset.simcPolling = '1'; control.button.disabled = true; }
    return new Promise(resolve => {
        control.resolve = resolve;
        const finish = completed => {
            if (control.resolve !== resolve) return;
            control.resolve = null;
            resolve(completed);
        };
        const poll = async () => {
            if (!isCurrentSimcCandidateControl(control)) return;
            try {
                const response = await fetch('/api/simc-regular-compare/?batch_id=' + encodeURIComponent(batchId), { signal: control.controller.signal });
                const payload = await response.json();
                if (!isCurrentSimcCandidateControl(control)) return;
                if (!response.ok || !payload.success) throw new Error(payload.error || '获取候选对比进度失败');
                const batch = payload.data.batch || {};
                const status = batch.current_round_status || batch;
                const done = Number(status.succeeded || 0);
                const total = Number(batch.current_round_total || batch.total || 0);
                if (control.button) control.button.innerHTML = `<i class="fas fa-spinner fa-spin mr-1"></i>${kind} 模拟中 ${done}/${total}`;
                if (status.failed) throw new Error(`有 ${status.failed} 个候选模拟失败，未生成比较报告`);
                if (status.pending || status.running) {
                    control.timer = setTimeout(poll, 5000);
                    return;
                }
                showMessage(`${kind} 候选对比已完成，请在任务结果中心查看`, 'success');
                switchSimcWorkbenchTab('artifacts');
                finish(true);
            } catch (error) {
                if (error.name === 'AbortError' || !isCurrentSimcCandidateControl(control)) return;
                showMessage('候选对比已停止：' + String(error.message || error), 'error');
                finish(false);
            }
        };
        poll();
    });
}

async function preflightSimcBattlenet() {
    const button = document.getElementById('simc-sim-battlenet-preflight-btn');
    const status = document.getElementById('simc-sim-battlenet-preflight-status');
    const spec = (document.getElementById('simc-sim-spec') || {}).value || '';
    const region = ((document.getElementById('simc-sim-battlenet-region') || {}).value || '').trim();
    const realm = ((document.getElementById('simc-sim-battlenet-realm') || {}).value || '').trim();
    const character = ((document.getElementById('simc-sim-battlenet-character') || {}).value || '').trim();
    if (!spec || !region || !realm || !character) {
        showMessage('请先选择专精并填写 Battle.net 地区、服务器和角色名', 'warning');
        return;
    }
    if (button) { button.disabled = true; button.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>获取中…'; }
    if (status) { status.className = 'text-xs text-blue-600'; status.textContent = '正在获取角色配置并验证…'; }
    try {
        const response = await fetch('/api/simc-battlenet-preflight/', {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify({ region, realm, character, spec }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.success) throw new Error(payload.error || '角色预检失败');
        const data = payload.data || {};
        const identity = data.identity || {}, profileSpec = data.spec || {}, equipment = data.equipment || {};
        const warnings = Array.isArray(data.warnings) ? data.warnings : [];
        if (status) {
            status.className = `text-xs ${data.simc_ready ? 'text-emerald-700' : 'text-amber-700'}`;
            const statSummary = data.stats?.secondary ? Object.entries(data.stats.secondary).map(([name, row]) => `${name} ${row?.rating ?? '-'}`).join(' · ') : '';
            const warningText = warnings.length ? `；${warnings.join('；')}` : '';
            status.textContent = data.simc_ready
                ? `已验证角色与装备：${identity.name || character} · ${profileSpec.name || profileSpec.key || '-'} · ${equipment.count || 0} 件装备，平均装等 ${equipment.item_level || '-'}${statSummary ? ' · ' + statSummary : ''}${warningText}`
                : `已获取但不可直接模拟：${warnings.join('；') || '角色配置不完整'}`;
        }
        if (data.simc_ready) showMessage('Battle.net 角色、装备与属性已获取；保存后会在 SimC 执行时通过 armory 导入当前构筑。', 'success');
        else showMessage('Battle.net 角色已获取，但当前不能直接启动模拟', 'warning');
    } catch (error) {
        if (status) { status.className = 'text-xs text-red-600'; status.textContent = '预检失败：' + String(error.message || error); }
        showMessage('获取 Battle.net 配置失败：' + String(error.message || error), 'error');
    } finally {
        if (button) { button.disabled = false; button.innerHTML = '<i class="fas fa-cloud-download-alt mr-1"></i>获取配置并验证'; }
    }
}

async function refreshSimcPlayerDetail() {
    const refreshBtn = document.getElementById('simc-sim-player-detail-refresh-btn');
    const spec = (document.getElementById('simc-sim-spec') || {}).value || '';
    if (!spec) { showMessage('请先选择专精', 'warning'); return; }
    const checkedMode = document.querySelector('input[name="simc-player-import-mode"]:checked');
    const mode = checkedMode ? checkedMode.value : 'battlenet';
    const requestBody = { spec, player_import_mode: mode, player_config_mode: mode };
    if (mode === 'manual_equipment') {
        requestBody.player_equipment = ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
        if (!requestBody.player_equipment) { showMessage('请粘贴玩家装备/天赋信息块', 'warning'); return; }
    } else if (mode === 'attribute_only') {
        const config = syncSimcAttributeOnlyConfigFromInputs();
        if (!config.talent) { showMessage('请填写天赋构筑码', 'warning'); return; }
        requestBody.player_equipment = ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
        if (!requestBody.player_equipment) { showMessage('请填写冻结的玩家装备基线', 'warning'); return; }
        Object.assign(requestBody, config);
    } else {
        requestBody.battlenet_region = ((document.getElementById('simc-sim-battlenet-region') || {}).value || '').trim();
        requestBody.battlenet_realm = ((document.getElementById('simc-sim-battlenet-realm') || {}).value || '').trim();
        requestBody.battlenet_character = ((document.getElementById('simc-sim-battlenet-character') || {}).value || '').trim();
        if (!requestBody.battlenet_region || !requestBody.battlenet_realm || !requestBody.battlenet_character) {
            showMessage('请填写 Battle.net 地区、服务器和角色名', 'warning'); return;
        }
    }

    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>刷新中…'; }
    try {
        const resp = await fetch('/api/simc-player-config-detail/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
            body: JSON.stringify(requestBody),
        });
        const payload = await resp.json();
        if (!resp.ok || !payload.success) throw new Error(payload.error || payload.message || '刷新详情失败');
        renderSimcPlayerDetail(payload.data || {});
    } catch (error) {

    } finally {
        if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.innerHTML = '<i class="fas fa-sync-alt mr-1"></i>刷新详情'; }
    }
}


let simcAttributeSearchTimer = null;
let simcAttributeSearchGeneration = 0;
let simcAttributeSearchControl = null;

function isCurrentSimcAttributeSearch(generation) {
    return Boolean(simcAttributeSearchControl && generation === simcAttributeSearchGeneration && simcAttributeSearchControl.generation === generation);
}

function stopSimcAttributeSearch() {
    simcAttributeSearchGeneration += 1;
    if (simcAttributeSearchTimer) clearTimeout(simcAttributeSearchTimer);
    simcAttributeSearchTimer = null;
    if (simcAttributeSearchControl) {
        const control = simcAttributeSearchControl;
        control.controller.abort();
        if (control.button) {
            control.button.disabled = false;
            control.button.innerHTML = control.oldLabel;
        }
    }
    simcAttributeSearchControl = null;
}

function setSimcAttributeSearchStatus(message, tone = 'text-amber-800') {
    const el = document.getElementById('simc-sim-attribute-search-status');
    if (!el) return;
    el.className = `mt-2 text-xs ${tone}`;
    el.classList.remove('hidden');
    el.innerHTML = message;
}

function simcAttributeSearchRequestBody() {
    const spec = (document.getElementById('simc-sim-spec') || {}).value || '';
    const config = syncSimcAttributeOnlyConfigFromInputs();
    const step = 50;
    if (!spec) throw new Error('请先在“发起模拟”选择专精');
    if (!config.talent) throw new Error('请填写天赋构筑码');
    const playerBaseline = ((document.getElementById('player-baseline-config') || {}).value || '').trim();
    const playerEquipment = playerBaseline
        || ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
    if (!playerEquipment) throw new Error('请填写冻结的玩家装备基线');
    const fightStyle = (document.getElementById('simc-sim-fight-style') || {}).value || 'Patchwerk';
    const time = parseInt((document.getElementById('simc-sim-time') || {}).value || '300', 10) || 300;
    const targetCount = parseInt((document.getElementById('simc-sim-target-count') || {}).value || '1', 10) || 1;
    const aplRadio = document.querySelector('input[name="simc-sim-apl"]:checked');
    const baseTemplate = document.getElementById('base-template-select');
    const baseTemplateEditor = document.getElementById('base-template-content');
    const aplOverride = document.getElementById('apl-override');
    const apl_override = aplOverride ? aplOverride.value : undefined;
    return {
        kind: 'attribute_variants', name: `${spec} 四属性自动寻优`, spec,
        player_config_mode: 'attribute_only', player_equipment: playerEquipment,
        talent: config.talent, gear_strength: config.gear_strength,
        gear_crit: config.gear_crit, gear_haste: config.gear_haste,
        gear_mastery: config.gear_mastery, gear_versatility: config.gear_versatility,
        attribute_step: step, fight_style: fightStyle, time, target_count: targetCount,
        selected_apl_id: aplRadio && aplRadio.value ? parseInt(aplRadio.value, 10) : undefined,
        base_template_id: baseTemplate && baseTemplate.value ? parseInt(baseTemplate.value, 10) : undefined,
        base_template_content: baseTemplateEditor ? baseTemplateEditor.value : undefined,
        override_action_list: apl_override,
    };
}

async function submitSimcAttributeSearch(payload, signal) {
    const response = await fetch('/api/simc-task/batch/', {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
        signal,
        body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || '创建属性寻优批次失败');
    return result.data;
}

function formatSimcAttributeRatings(ratings) {
    const row = ratings || {};
    return `暴击 ${row.crit ?? '-'} / 急速 ${row.haste ?? '-'} / 精通 ${row.mastery ?? '-'} / 全能 ${row.versatility ?? '-'}`;
}

function renderSimcAttributeSearchReport(report) {
    if (!report || !report.recommendation) return '';
    const recommendation = report.recommendation;
    const dps = Number(recommendation.dps || 0).toLocaleString();
    const stopText = report.local_optimum
        ? '已验证为当前条件下 50 rating 两两交换邻域局部最优'
        : report.stop_reason === 'awaiting_current_round'
            ? '当前轮尚未完成，以下仅显示已完成的独立 SimC 结果'
            : '已选出当前轮实际 DPS 更优点，正在继续下一轮';
    const totalRows = Array.isArray(report.all_candidates) ? report.all_candidates.length : (report.candidates || []).length;
    const rows = (report.all_candidates || report.candidates || []).map((row) => {
        const dpsText = row.dps === null || row.dps === undefined ? '等待结果' : Number(row.dps).toLocaleString();
        const state = row.dps === null || row.dps === undefined ? 'text-gray-400' : '';
        return `
        <tr class="${row.id === recommendation.id ? 'bg-emerald-50' : ''}">
            <td class="px-2 py-1.5">第${row.round}轮</td>
            <td class="px-2 py-1.5 font-mono ${state}">${dpsText}</td>
            <td class="px-2 py-1.5">${escapeHtml(formatSimcAttributeRatings(row.ratings))}</td>
            <td class="px-2 py-1.5">${escapeHtml(row.label || '')}</td>
        </tr>`;
    }).join('');
    return `<div class="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-gray-700">
        <div class="font-semibold text-emerald-800">四属性 50 rating 局部寻优报告</div>
        <div class="mt-1">推荐：<span class="font-semibold">${escapeHtml(formatSimcAttributeRatings(recommendation.ratings))}</span> · DPS ${dps}</div>
        <div class="mt-1">${escapeHtml(stopText)}；已完成 ${report.rounds_completed || 0} 轮，当前批次 ${totalRows} 个候选，总绿字 ${report.total_rating ?? '-'}。</div>
        <div class="mt-2 overflow-x-auto"><table class="min-w-full text-left"><thead class="text-gray-500"><tr><th class="px-2 py-1">轮次</th><th class="px-2 py-1">DPS</th><th class="px-2 py-1">四属性</th><th class="px-2 py-1">候选</th></tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
}

function pollSimcAttributeSearch(batchId, generation) {
    if (!isCurrentSimcAttributeSearch(generation)) return;
    if (simcAttributeSearchTimer) clearTimeout(simcAttributeSearchTimer);
    const poll = async () => {
        if (!isCurrentSimcAttributeSearch(generation)) return;
        try {
            const response = await fetch('/api/simc-regular-compare/?batch_id=' + encodeURIComponent(batchId), { signal: simcAttributeSearchControl.controller.signal });
            const result = await response.json();
            if (!isCurrentSimcAttributeSearch(generation)) return;
            if (!response.ok || !result.success) throw new Error(result.error || '获取属性寻优进度失败');
            const batch = result.data.batch;
            const reportHtml = renderSimcAttributeSearchReport(result.data.attribute_report);
            const roundStatus = batch.current_round_status || batch;
            if (roundStatus.failed) throw new Error('本轮存在失败任务，已停止自动寻优；请检查任务日志后重新发起。');
            setSimcAttributeSearchStatus(`第 ${batch.current_round || Math.max(...result.data.tasks.map(t => Number((t.candidate || {}).round || 1)))} 轮：完成 ${roundStatus.succeeded}/${batch.current_round_total || batch.total}，运行 ${roundStatus.running}，等待 ${roundStatus.pending}${roundStatus.failed ? '，失败 ' + roundStatus.failed : ''}${reportHtml}`);
            if (roundStatus.pending || roundStatus.running) {
                if (!isCurrentSimcAttributeSearch(generation)) return;
                simcAttributeSearchTimer = setTimeout(poll, 5000);
                return;
            }
            if (!isCurrentSimcAttributeSearch(generation)) return;
            const next = await submitSimcAttributeSearch(
                { continue_batch_id: batchId, min_attribute_step: 50 },
                simcAttributeSearchControl.controller.signal,
            );
            if (!isCurrentSimcAttributeSearch(generation)) return;
            if (next.converged) {
                const r = next.recommendation || {};
                const stats = r.ratings || {};
                const reason = r.stop_reason === 'cycle_detected'
                    ? '检测到搜索回环，保留当前实际 DPS 最优点'
                    : r.stop_reason === 'max_rounds_reached'
                        ? '达到最大搜索轮数，保留当前实际 DPS 最优点'
                        : '已收敛';
                setSimcAttributeSearchStatus(`${reason}：暴击 ${stats.crit} / 急速 ${stats.haste} / 精通 ${stats.mastery} / 全能 ${stats.versatility}，DPS ${r.dps}`, 'text-emerald-700');
                showMessage('四属性自动寻优已结束', 'success');
                if (typeof window.simcWorkbenchLoadTaskResource === 'function') window.simcWorkbenchLoadTaskResource('batches');
                simcAttributeSearchControl = null;
                return;
            }
            setSimcAttributeSearchStatus(`已选出下一轮中心，继续以步长 ${next.recommendation.step} 模拟 ${next.accepted} 个方案…`);
            if (typeof window.simcWorkbenchLoadTaskResource === 'function') window.simcWorkbenchLoadTaskResource('batches');
            pollSimcAttributeSearch(next.batch_id, generation);
        } catch (error) {
            if (error.name === 'AbortError' || !isCurrentSimcAttributeSearch(generation)) return;
            setSimcAttributeSearchStatus('自动寻优已停止：' + String(error.message || error), 'text-red-700');
            showMessage('四属性自动寻优失败：' + String(error.message || error), 'error');
            simcAttributeSearchControl = null;
        }
    };
    poll();
}

async function startSimcAttributeSearch() {
    const button = document.getElementById('simc-sim-attribute-optimize-btn');
    stopSimcAttributeSearch();
    const generation = ++simcAttributeSearchGeneration;
    simcAttributeSearchControl = {
        generation,
        controller: new AbortController(),
        button,
        oldLabel: button?.innerHTML,
    };
    let pollingStarted = false;
    try {
        const payload = simcAttributeSearchRequestBody();
        if (button) { button.disabled = true; button.innerHTML = '<i class="fas fa-spinner fa-spin mr-1"></i>创建中…'; }
        const data = await submitSimcAttributeSearch(payload, simcAttributeSearchControl.controller.signal);
        if (!isCurrentSimcAttributeSearch(generation)) return;
        setSimcAttributeSearchStatus(`已创建第 1 轮 ${data.accepted} 个方案，等待常规模拟完成…`);
        showMessage('四属性自动寻优第一轮已创建', 'success');
        switchSimcWorkbenchL1Tab('history');
        if (!isCurrentSimcAttributeSearch(generation)) return;
        pollSimcAttributeSearch(data.batch_id, generation);
        pollingStarted = true;
    } catch (error) {
        if (error.name !== 'AbortError' && isCurrentSimcAttributeSearch(generation)) {
            setSimcAttributeSearchStatus('无法启动：' + String(error.message || error), 'text-red-700');
            showMessage('启动四属性自动寻优失败：' + String(error.message || error), 'error');
        }
    } finally {
        if (isCurrentSimcAttributeSearch(generation) && button) {
            button.disabled = false;
            button.innerHTML = '<i class="fas fa-compass mr-1"></i>自动寻优四属性';
        }
        if (!pollingStarted && isCurrentSimcAttributeSearch(generation)) simcAttributeSearchControl = null;
    }
}

async function createSimcSimulationTask() {
    try {
        const spec = (document.getElementById('simc-sim-spec') || {}).value || '';
        if (!spec) { showMessage('请先选择专精', 'warning'); return; }
        const fightStyle = (document.getElementById('simc-sim-fight-style') || {}).value || 'Patchwerk';
        const time = parseInt((document.getElementById('simc-sim-time') || {}).value || '300', 10) || 300;
        const targetCount = parseInt((document.getElementById('simc-sim-target-count') || {}).value || '1', 10) || 1;

        const checkedMode = document.querySelector('input[name="simc-player-import-mode"]:checked');
        const mode = checkedMode ? checkedMode.value : 'battlenet';

        const requestBody = {
            task_type: 1,
            spec: spec,
            fight_style: fightStyle,
            time: time,
            target_count: targetCount,
            player_import_mode: mode,
            player_config_mode: mode,
        };
        const baseTemplate = document.getElementById('base-template-select');
        if (baseTemplate && baseTemplate.value) requestBody.base_template_id = parseInt(baseTemplate.value, 10);
        const baseTemplateEditor = document.getElementById('base-template-content');
        if (baseTemplateEditor) requestBody.base_template_content = baseTemplateEditor.value;
        const baselineEditor = document.getElementById('player-baseline-config');
        const frozenBaseline = baselineEditor ? baselineEditor.value.trim() : '';

        if (mode === 'manual_equipment') {
            const equipment = ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
            if (!equipment) { showMessage('请粘贴玩家装备/天赋信息块', 'warning'); return; }
            requestBody.player_equipment = equipment;
        } else if (mode === 'attribute_only') {
            const config = syncSimcAttributeOnlyConfigFromInputs();
            if (!config.talent) { showMessage('请填写天赋构筑码', 'warning'); return; }
            const equipment = frozenBaseline || ((document.getElementById('simc-sim-equipment') || {}).value || '').trim();
            if (!equipment) { showMessage('请填写冻结的玩家装备基线', 'warning'); return; }
            requestBody.player_equipment = equipment;
            Object.assign(requestBody, config);
        } else if (mode === 'battlenet') {
            const region = ((document.getElementById('simc-sim-battlenet-region') || {}).value || '').trim();
            const realm = ((document.getElementById('simc-sim-battlenet-realm') || {}).value || '').trim();
            const character = ((document.getElementById('simc-sim-battlenet-character') || {}).value || '').trim();
            if (!region || !realm || !character) { showMessage('请填写 Battle.net 地区、服务器和角色名', 'warning'); return; }
            requestBody.battlenet_region = region;
            requestBody.battlenet_realm = realm;
            requestBody.battlenet_character = character;
        }

        const aplRadio = document.querySelector('input[name="simc-sim-apl"]:checked');
        if (aplRadio && aplRadio.value) {
            requestBody.selected_apl_id = parseInt(aplRadio.value, 10);
        }
        const aplOverride = document.getElementById('apl-override');
        if (aplOverride) requestBody.override_action_list = aplOverride.value;

        const specLabels = {
            arms: '武器战', fury: '狂怒战', protection: '防战', havoc: '浩劫', vengeance: '复仇',
            balance: '平衡', feral: '野性', guardian: '守护', restoration: '恢复德',
            devastation: '湮灭', preservation: '恩护', augmentation: '增辉',
            beast_mastery: '兽王', marksmanship: '射击', survival: '生存',
            arcane: '奥术', fire: '火焰', frost: '冰霜', frost_dk: '冰霜DK',
            brewmaster: '酒仙', mistweaver: '织雾', windwalker: '踏风',
            holy: '神圣', discipline: '戒律', shadow: '暗影',
            retribution: '惩戒',
            assassination: '奇袭', outlaw: '狂徒', subtlety: '敏锐',
            elemental: '元素', enhancement: '增强', restoration_shaman: '恢复萨',
            affliction: '痛苦', demonology: '恶魔', destruction: '毁灭',
            blood: '鲜血', unholy: '邪恶'
        };
        const fightLabels = {
            Patchwerk: '单体', Cleave: '双目标', HecticAddCleave: '杂乱AOE',
            DungeonSlice: '大秘境', DungeonRoute: '大秘境路线'
        };
        const taskName = `${specLabels[spec] || spec} ${fightLabels[fightStyle] || fightStyle} ${time}s ${targetCount}目标`;
        requestBody.name = taskName;

        const btn = document.getElementById('simc-sim-submit-btn');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>提交中…'; }
        try {
            const resp = await fetch('/api/simc-task/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCSRFToken() },
                body: JSON.stringify(requestBody)
            });
            const payload = await resp.json();
            if (!resp.ok || !payload.success) throw new Error(payload.error || payload.message || '创建失败');
            showMessage('模拟任务已创建: ' + taskName, 'success');
            switchSimcWorkbenchL1Tab('history');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-play-circle mr-2"></i>发起模拟'; }
        }
    } catch (error) {
        console.error('Create SimC simulation task failed:', error);
        showMessage('创建模拟任务失败：' + String(error.message || error), 'error');
    }
}

function bindSimcWorkbenchSimulationControls() {
    const importPanel = document.getElementById('simc-workbench-import-panel');
    if (importPanel && importPanel.dataset.importModeBound !== '1') {
        importPanel.dataset.importModeBound = '1';
        importPanel.addEventListener('change', function(event) {
            const modeInput = event.target.closest('input[name="simc-player-import-mode"]');
            if (modeInput) switchSimcPlayerImportMode(modeInput.value);
        });
    }

    const specSel = document.getElementById('simc-sim-spec');
    if (specSel && specSel.dataset.bound !== '1') {
        specSel.dataset.bound = '1';
        specSel.addEventListener('change', function() {
            loadSimcAplCandidates(this.value);
            loadSimcSnapshotDefaults(this.value).catch((error) => showMessage('加载默认模拟输入失败：' + String(error.message || error), 'error'));
            loadSimcSimSavedProfiles();
        });
    }

    const detailRefreshBtn = document.getElementById('simc-sim-player-detail-refresh-btn');
    if (detailRefreshBtn && detailRefreshBtn.dataset.bound !== '1') {
        detailRefreshBtn.dataset.bound = '1';
        detailRefreshBtn.addEventListener('click', refreshSimcPlayerDetail);
    }

    const battlenetPreflightBtn = document.getElementById('simc-sim-battlenet-preflight-btn');
    if (battlenetPreflightBtn && battlenetPreflightBtn.dataset.bound !== '1') {
        battlenetPreflightBtn.dataset.bound = '1';
        battlenetPreflightBtn.addEventListener('click', preflightSimcBattlenet);
    }

    const submitBtn = document.getElementById('simc-sim-submit-btn');
    if (submitBtn && submitBtn.dataset.bound !== '1') {
        submitBtn.dataset.bound = '1';
        submitBtn.addEventListener('click', createSimcSimulationTask);
    }

    const attributeOptimizeBtn = document.getElementById('simc-sim-attribute-optimize-btn');
    if (attributeOptimizeBtn && attributeOptimizeBtn.dataset.bound !== '1') {
        attributeOptimizeBtn.dataset.bound = '1';
        attributeOptimizeBtn.addEventListener('click', startSimcAttributeSearch);
    }

    const refreshProfilesBtn = document.getElementById('simc-sim-refresh-profiles-btn');
    if (refreshProfilesBtn && refreshProfilesBtn.dataset.bound !== '1') {
        refreshProfilesBtn.dataset.bound = '1';
        refreshProfilesBtn.addEventListener('click', function() {
            loadSimcSimSavedProfiles();
        });
    }

    const fightPresetSel = document.getElementById('simc-sim-fight-preset');
    if (fightPresetSel && fightPresetSel.dataset.bound !== '1') {
        fightPresetSel.dataset.bound = '1';
        fightPresetSel.addEventListener('change', function() {
            applySimcFightPreset(this.value);
        });
    }

    const timeInput = document.getElementById('simc-sim-time');
    const targetInput = document.getElementById('simc-sim-target-count');
    [timeInput, targetInput].forEach(el => {
        if (el && el.dataset.boundPresetSync !== '1') {
            el.dataset.boundPresetSync = '1';
            el.addEventListener('change', syncSimcFightPresetFromInputs);
            if (el.tagName === 'INPUT') el.addEventListener('input', syncSimcFightPresetFromInputs);
        }
    });

    const equipmentInput = document.getElementById('simc-sim-equipment');
    if (equipmentInput && equipmentInput.dataset.boundSpecAuto !== '1') {
        equipmentInput.dataset.boundSpecAuto = '1';
        equipmentInput.addEventListener('blur', function() {
            const parsedSpec = parseSpecFromPlayerBlock(this.value);
            if (parsedSpec) autoSelectSpecIfSafe(parsedSpec);
        });
    }

    const attributeTalentInput = document.getElementById('simc-sim-attribute-talent');
    if (attributeTalentInput && attributeTalentInput.dataset.boundSpecAuto !== '1') {
        attributeTalentInput.dataset.boundSpecAuto = '1';
    }

    syncSimcFightPresetFromInputs();
    loadSimcSimSavedProfiles();
}

function applySimcFightPreset(presetValue) {
    if (!presetValue || presetValue === 'custom') return;
    const parts = String(presetValue).split(',');
    if (parts.length !== 2) return;
    const [time, targetCount] = parts;
    const timeInput = document.getElementById('simc-sim-time');
    const targetInput = document.getElementById('simc-sim-target-count');
    if (timeInput && time) timeInput.value = String(parseInt(time, 10) || 300);
    if (targetInput && targetCount) targetInput.value = String(parseInt(targetCount, 10) || 1);
}

function syncSimcFightPresetFromInputs() {
    const presetSel = document.getElementById('simc-sim-fight-preset');
    const timeInput = document.getElementById('simc-sim-time');
    const targetInput = document.getElementById('simc-sim-target-count');
    if (!presetSel || !timeInput || !targetInput) return;
    const time = String(parseInt(timeInput.value, 10) || 300);
    const targetCount = String(parseInt(targetInput.value, 10) || 1);
    const expected = `${time},${targetCount}`;
    const matched = Array.from(presetSel.options || []).some(opt => opt.value === expected);
    presetSel.value = matched ? expected : 'custom';
}

document.addEventListener('DOMContentLoaded', function() {
    startSimcBackendUpdatePolling();
});

/**
 * 处理子菜单展开/收起
 */
function initSubmenuToggle() {
    const hasSubmenuItems = document.querySelectorAll('.has-submenu');

    hasSubmenuItems.forEach(item => {
        const mainLink = item.querySelector('a');
        const submenu = item.querySelector('.submenu');
        const chevron = item.querySelector('.fa-chevron-down');

        if (mainLink && submenu) {
            mainLink.addEventListener('click', function(e) {
                e.preventDefault();

                if (item.classList.contains('open')) {
                    // 收起子菜单
                    item.classList.remove('open');
                    submenu.style.maxHeight = '0';
                    if (chevron) chevron.classList.remove('rotate-180');
                } else {
                    // 展开子菜单
                    item.classList.add('open');
                    submenu.style.maxHeight = submenu.scrollHeight + 'px';
                    if (chevron) chevron.classList.add('rotate-180');
                }
            });
        }
    });
}

/**
 * 初始化数据库表选择和计算总记录数
 */
function initTableSelection() {
    // 计算总记录数
    calculateTotalRecords();

    // 初始化子菜单展开/收起功能
    initSubmenuToggle();
}

/**
 * 计算所有表的总记录数
 */
function calculateTotalRecords() {
    const totalRecordsElement = document.getElementById('total-records');
    if (!totalRecordsElement) return;

    // 获取所有表项
    const tableItems = document.querySelectorAll('.table-overview-item');
    let totalRecords = 0;

    // 计算总记录数
    tableItems.forEach(item => {
        const countText = item.querySelector('p:last-child').textContent;
        const count = parseInt(countText.replace('记录数: ', ''));
        if (!isNaN(count)) {
            totalRecords += count;
        }
    });

    // 更新总记录数显示
    totalRecordsElement.textContent = totalRecords.toLocaleString();
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
let currentTableRowMap = new Map();
let currentEditRowId = null;
let simcProfileSpecFilter = '';
let simcProfileFightStyleFilter = '';
let wowArticleSourceFilter = '';
let wowArticleCategoryFilter = '';
let secondaryStatRuleMap = null;
let secondaryStatRulePromise = null;
let tableFetchRequestSeq = 0;

const MANAGED_DATA_ADD_DISABLED_MESSAGE = '该表数据来自采集/聚合任务，不支持手工新增';
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
    PlayerSpecTopPlayer: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    SpecDungeonRanking: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    SpecRaidRanking: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    PortalMythicstatsDpsRow: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowSpellSnapshot: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowSpellEffectSnapshot: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowSpecSpellMapSnapshot: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowSkillDiffReport: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowHotfixReport: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
    WowDailyReport: { disableAdd: true, disableAddMessage: MANAGED_DATA_ADD_DISABLED_MESSAGE },
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
                if (data.table_description) {
                    currentTableDisplayName = data.table_description;
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

    // 如果没有数据
    if (!data || data.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="100%" class="text-center py-8 text-gray-500">暂无数据</td></tr>';
        return;
    }

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
    // 添加操作列（WechatArticle和RssArticle表不显示操作列）
    if (renderTableName !== 'WechatArticle' && renderTableName !== 'RssArticle') {
        const actionTh = document.createElement('th');
        actionTh.className = 'px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-32 action-col-header';
        actionTh.id = 'action-col-header';
        actionTh.textContent = '操作';
        headerRow.appendChild(actionTh);
    }
    tableHeader.appendChild(headerRow);

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
                // URL字段显示为链接
                const link = document.createElement('a');
                link.href = cellText;
                link.target = '_blank';
                link.textContent = truncateText(cellText, 30);
                link.className = 'text-blue-600 hover:text-blue-800 hover:underline cursor-pointer';
                link.title = cellText;
                td.appendChild(link);
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
                const url = row['url'] || '';
                if (url) {
                    const link = document.createElement('a');
                    link.href = url;
                    link.target = '_blank';
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
                // SimcProfile表的装备属性字段右对齐并添加样式
                td.className += ' text-right font-mono';
                td.textContent = cellText || '0';
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

        // 添加操作列（WechatArticle和RssArticle表不显示操作列）
        if (renderTableName !== 'WechatArticle' && renderTableName !== 'RssArticle') {
            const actionTd = document.createElement('td');
            actionTd.className = 'px-4 py-4 whitespace-nowrap text-sm font-medium w-32 action-col';

            // SimcProfile表使用特殊的操作按钮
            if (renderTableName === 'SimcProfile') {
                actionTd.innerHTML = `
                    <div class="flex space-x-1">
                        <button class="simc-profile-edit-btn text-blue-600 hover:text-blue-900 transition-colors duration-200" data-profile-id="${rowId}">
                            <i class="fas fa-edit mr-1"></i>编辑
                        </button>
                        <button class="simc-profile-copy-btn text-green-600 hover:text-green-900 transition-colors duration-200" data-profile-id="${rowId}">
                            <i class="fas fa-copy mr-1"></i>复制
                        </button>
                        <button class="simc-profile-apl-btn text-orange-600 hover:text-orange-900 transition-colors duration-200" data-profile-id="${rowId}">
                            <i class="fas fa-list mr-1"></i>APL
                        </button>
                        <button class="simc-profile-simulate-btn text-purple-600 hover:text-purple-900 transition-colors duration-200" data-profile-id="${rowId}">
                            <i class="fas fa-play mr-1"></i>模拟
                        </button>
                        <button class="simc-profile-delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-profile-id="${rowId}">
                            <i class="fas fa-trash mr-1"></i>删除
                        </button>
                    </div>
                `;
            } else if (renderTableName === 'WowArticle') {
                actionTd.innerHTML = `
                    <div class="flex space-x-2">
                        <button class="delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-trash mr-1"></i>删除
                        </button>
                    </div>
                `;
            } else if (renderTableName === 'MonitorTask') {
                actionTd.innerHTML = `
                    <div class="flex space-x-2">
                        <button class="edit-btn text-blue-600 hover:text-blue-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-edit mr-1"></i>编辑
                        </button>
                        <button class="rerun-btn text-orange-600 hover:text-orange-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-play mr-1"></i>重跑
                        </button>
                        <button class="delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-trash mr-1"></i>删除
                        </button>
                    </div>
                `;
            } else {
                actionTd.innerHTML = `
                    <div class="flex space-x-2">
                        <button class="edit-btn text-blue-600 hover:text-blue-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-edit mr-1"></i>编辑
                        </button>
                        <button class="delete-btn text-red-600 hover:text-red-900 transition-colors duration-200" data-row-id="${rowId}">
                            <i class="fas fa-trash mr-1"></i>删除
                        </button>
                    </div>
                `;
            }
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
                threads: threads,
                no_pull: noPull,
                check_only: checkOnly
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
async function convertText(text, conversionType) {
    try {
        const response = await fetch('/api/convert-text/', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCSRFToken()
            },
            body: JSON.stringify({
                text: text,
                conversion_type: conversionType
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
    const config = getCurrentFormConfig();
    if (config.disableAdd) {
        showMessage(config.disableAddMessage || '该表不支持手工新增', 'warning');
        return;
    }

    // SimcProfile表使用专门的模态框
    if (currentTableName === 'SimcProfile') {
        openAddSimcProfileModal();
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
        if (column.toLowerCase() === 'id' || isEditFormHiddenField(column)) {
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
        if (column === 'id' || isEditFormHiddenField(column)) {
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
        // 跳过ID字段和当前模型新增窗口隐藏字段
        if (column.toLowerCase() === 'id' || isAddFormHiddenField(column)) {
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

    // 根据表名选择不同的API端点
    let apiUrl, requestData;

    if (currentTableName === 'SimcAplKeywordPair') {
        // 使用关键字管理API
        apiUrl = '/api/keyword-manager/';
        requestData = data;
    } else {
        // 使用通用的dashboard API
        apiUrl = '/dashboard/';
        requestData = {
            action: 'create_table_row',
            table_name: currentTableName,
            create_data: data
        };
    }

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
