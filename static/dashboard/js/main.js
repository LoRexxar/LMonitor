/**
 * Dashboard页面的JavaScript功能
 */

document.addEventListener('DOMContentLoaded', function() {
    console.log('Dashboard页面已加载');
    
    // 初始化页面数据
    initDashboard();
    
    // 设置定时刷新
    setInterval(refreshData, 30000); // 每30秒刷新一次数据
    
    // 初始化导航菜单点击事件
    initNavigation();
    
    // 初始化子菜单切换
    initSubmenuToggle();
    
    // 初始化数据库表点击事件
    initTableSelection();
    
    // 初始化转换器
    initSimcAplConverter();
    
    // 初始化新增记录功能
    initAddRecord();
    
    // 初始化侧边栏切换功能
    initSidebarToggle();
    
    // 初始化搜索功能
    initSearch();
    
    // 初始化页面大小选择器
    initPageSizeSelector();
    
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
function initDashboard() {
    console.log('Dashboard initialized');
    // 这里可以添加AJAX请求获取初始数据
    updateSystemStatus();
    updateRecentActivities();
    updateStatistics();
}

/**
 * 刷新仪表盘数据
 */
function refreshData() {
    console.log('Refreshing dashboard data...');
    // 更新系统状态
    updateSystemStatus();
    // 更新最近活动
    updateRecentActivities();
    // 更新统计数据
    updateStatistics();
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
            }
        });
    });
    
    // 处理子菜单项点击
    submenuItems.forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            e.stopPropagation(); // 阻止事件冒泡到父级菜单项
            
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
                    }
                }
            } else if (tableName) {
                // 处理数据库表菜单项
                const tableTitle = this.querySelector('a').textContent;
                
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
                        selectedTableName.textContent = tableName;
                    }
                    
                    // 获取表数据
                    fetchTableData(tableName);
                }
            }
        });
    });
}

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


/**
 * 获取表数据
 */
function fetchTableData(tableName, page = 1) {
    // 显示加载中
    const tableBody = document.getElementById('table-body');
    if (!tableBody) {
        console.log('表格元素不存在，跳过数据加载');
        return;
    }
    tableBody.innerHTML = '<tr><td colspan="10">加载中...</td></tr>';
    
    // 获取CSRF令牌
    const csrfToken = getCSRFToken();
    if (!csrfToken) {
        console.error('无法获取CSRF令牌');
        const tableBody = document.getElementById('table-body');
        if (tableBody) {
            tableBody.innerHTML = '<tr><td colspan="10">错误: 无法获取CSRF令牌，请刷新页面</td></tr>';
        }
        return;
    }
    
    console.log('正在获取表数据:', tableName, 'page:', page);
    console.log('使用的CSRF令牌:', csrfToken);
    
    // 保存当前表名和页码
    currentTableName = tableName;
    currentPage = page;
    
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
    
    console.log('发送请求数据:', JSON.stringify(requestData));
    
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
        console.log('获取到的数据:', data);
        if (data.status === 'success') {
            if (data.data && Array.isArray(data.data) && data.fields) {
                // 更新分页信息
                totalPages = data.total_pages || 1;
                totalCount = data.total_count || 0;
                currentPage = data.page || 1;
                pageSize = data.page_size || 50;
                
                // 保存字段类型信息
                currentFieldTypes = data.field_types || {};
                
                displayTableData(data.data, data.fields);
                updatePagination();
            } else {
                console.error('返回的数据格式不正确:', data);
                const tableBody = document.getElementById('table-body');
                if (tableBody) {
                    tableBody.innerHTML = '<tr><td colspan="10">错误: 返回的数据格式不正确</td></tr>';
                }
            }
        } else {
            console.error('获取数据失败:', data.message || '未知错误');
            const tableBody = document.getElementById('table-body');
            if (tableBody) {
                tableBody.innerHTML = `<tr><td colspan="10">获取数据失败: ${data.message || '未知错误'}</td></tr>`;
            }
        }
    })
    .catch(error => {
        console.error('获取表数据时发生错误:', error);
        const tableBody = document.getElementById('table-body');
        if (tableBody) {
            tableBody.innerHTML = `<tr><td colspan="10">获取数据时发生错误: ${error.message}</td></tr>`;
        }
    });
}

/**
 * 显示表数据
 */
function displayTableData(data, fields) {
    const tableHeader = document.getElementById('table-header');
    const tableBody = document.getElementById('table-body');
    
    // 如果表格元素不存在，直接返回
    if (!tableHeader || !tableBody) {
        console.log('表格元素不存在，跳过数据显示');
        return;
    }
    
    // 获取当前表名并设置全局变量
    const selectedTableName = document.getElementById('selected-table-name');
    currentTableName = selectedTableName ? selectedTableName.textContent : '';
    
    // 设置当前表的列信息
    currentTableColumns = fields || [];
    
    // 清空表格
    tableHeader.innerHTML = '';
    tableBody.innerHTML = '';
    
    // 如果没有数据
    if (!data || data.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="100%" class="text-center py-8 text-gray-500">暂无数据</td></tr>';
        return;
    }
    
    // 所有表格都显示序号，不显示数据库ID
    let displayFields = fields;
    let showCustomIndex = true;
    
    // 过滤掉ID字段，所有表格都不显示数据库ID
    displayFields = fields.filter(field => field !== 'id');
    
    // 针对WechatArticle表的特殊处理：显示序号、title、author和时间字段
    if (currentTableName === 'WechatArticle') {
        displayFields = fields.filter(field => 
            field === 'title' || 
            field === 'author' || 
            field === 'created_at' || 
            field === 'updated_at' ||
            field === 'publish_time'
        );
        // 确保关键字段存在并按顺序排列
        const orderedFields = ['title', 'author', 'publish_time', 'created_at', 'updated_at'];
        displayFields = orderedFields.filter(field => fields.includes(field));
    }
    
    // 针对WowArticle表的特殊处理：只显示序号、title和author
    else if (currentTableName === 'WowArticle') {
        displayFields = fields.filter(field => 
            field === 'title' || 
            field === 'author'
        );
        // 确保关键字段存在并按顺序排列
        const orderedFields = ['title', 'author'];
        displayFields = orderedFields.filter(field => fields.includes(field));
    }
    
    // 针对RssArticle表的特殊处理：不显示rss_id、url、content_html，限制title长度并可点击跳转
    else if (currentTableName === 'RssArticle') {
        displayFields = fields.filter(field => 
            !['rss_id', 'url', 'content_html'].includes(field)
        );
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
        th.textContent = field;
        headerRow.appendChild(th);
    });
    // 添加操作列（WechatArticle、WowArticle和RssArticle表不显示操作列）
    if (currentTableName !== 'WechatArticle' && currentTableName !== 'WowArticle' && currentTableName !== 'RssArticle') {
        const actionTh = document.createElement('th');
        actionTh.className = 'px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-32';
        actionTh.textContent = '操作';
        headerRow.appendChild(actionTh);
    }
    tableHeader.appendChild(headerRow);
    
    // 创建表格内容
    data.forEach((row, index) => {
        const tr = document.createElement('tr');
        tr.className = index % 2 === 0 ? 'bg-white hover:bg-gray-50' : 'bg-gray-50 hover:bg-gray-100';
        
        // 使用行的第一个字段值作为row-id，如果没有则使用index
        const rowId = row[fields[0]] || index;
        tr.setAttribute('data-row-id', rowId);
        
        // 所有表格都显示序号列，根据分页计算正确的序号
        const indexTd = document.createElement('td');
        indexTd.className = 'px-4 py-4 text-sm text-gray-900 w-16';
        const globalIndex = (currentPage - 1) * pageSize + index + 1;
        indexTd.textContent = globalIndex;
        tr.appendChild(indexTd);
        
        displayFields.forEach((field, index) => {
            const td = document.createElement('td');
            const widthClass = getColumnWidth(field, index, displayFields.length);
            td.className = `px-4 py-4 text-sm text-gray-900 ${widthClass}`;
            td.setAttribute('data-field', field);
            
            // 处理字段值
            const cellValue = row[field] !== null ? row[field] : '';
            let cellText = String(cellValue);
            
            // 处理undefined值
            if (cellValue === undefined || cellText === 'undefined') {
                cellText = '';
            }
            
            // 根据字段类型和名称进行特殊处理
            if (isUrlField(field) && cellText) {
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
            else if (isNumericField(field)) {
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
            else if ((currentTableName === 'WechatArticle' || currentTableName === 'WowArticle' || currentTableName === 'RssArticle') && field === 'title') {
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
        
        // 添加操作列（WechatArticle、WowArticle和RssArticle表不显示操作列）
        if (currentTableName !== 'WechatArticle' && currentTableName !== 'WowArticle' && currentTableName !== 'RssArticle') {
            const actionTd = document.createElement('td');
            actionTd.className = 'px-4 py-4 whitespace-nowrap text-sm font-medium w-32';
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
            tr.appendChild(actionTd);
        }
        
        tableBody.appendChild(tr);
    });
    
    // 绑定编辑和删除事件
    bindTableActions();
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
            const row = this.closest('tr');
            toggleRowEdit(row, rowId);
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
}

/**
 * 切换行编辑模式
 */
function toggleRowEdit(row, rowId) {
    const isEditing = row.classList.contains('editing');
    
    if (isEditing) {
        // 保存编辑
        saveRowEdit(row, rowId);
    } else {
        // 进入编辑模式
        enterEditMode(row, rowId);
    }
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
        
        // 跳过时间字段，保持只读状态
        if (isTimeField(field)) {
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
    
    // 获取当前选中的表名
    const selectedTableName = document.getElementById('selected-table-name').textContent.trim();
    
    // 构建请求数据
    const requestData = {
        action: 'update_table_row',
        table_name: selectedTableName,
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
    
    // 获取当前选中的表名
    const selectedTableName = document.getElementById('selected-table-name').textContent.trim();
    
    // 构建请求数据
    const requestData = {
        action: 'delete_table_row',
        table_name: selectedTableName,
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
            alert('删除失败: ' + (data.message || '未知错误'));
        }
    })
    .catch(error => {
        console.error('删除数据时发生错误:', error);
        alert('删除数据时发生错误: ' + error.message);
    });
}

/**
 * 显示消息提示
 */
function showMessage(message, type = 'info') {
    // 创建消息元素
    const messageDiv = document.createElement('div');
    messageDiv.className = `fixed top-4 right-4 px-4 py-2 rounded-lg shadow-lg z-50 transition-all duration-300`;
    
    if (type === 'success') {
        messageDiv.className += ' bg-green-500 text-white';
    } else if (type === 'error') {
        messageDiv.className += ' bg-red-500 text-white';
    } else {
        messageDiv.className += ' bg-blue-500 text-white';
    }
    
    messageDiv.textContent = message;
    
    // 添加到页面
    document.body.appendChild(messageDiv);
    
    // 3秒后自动移除
    setTimeout(() => {
        messageDiv.style.opacity = '0';
        setTimeout(() => {
            if (messageDiv.parentNode) {
                messageDiv.parentNode.removeChild(messageDiv);
            }
        }, 300);
    }, 3000);
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
        { time: new Date().toLocaleString(), action: '收到新的webhook请求' },
        { time: new Date(Date.now() - 1000 * 60 * 30).toLocaleString(), action: '系统自动更新完成' },
        { time: new Date(Date.now() - 1000 * 60 * 60).toLocaleString(), action: '用户登录' }
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
    return booleanFields.includes(field.toLowerCase()) || 
           typeof value === 'boolean' || 
           value === 'true' || value === 'false';
}

/**
 * 判断是否为时间字段
 */
function isTimeField(field) {
    const timeFields = ['time', 'date', 'created_at', 'updated_at', 'publish_time', 'last_scan_time', 'last_spider_time', 'last_publish_time', 'create_time'];
    // 排除wait_time，它应该显示为数值而不是时间
    if (field.toLowerCase() === 'wait_time') {
        return false;
    }
    return timeFields.some(timeField => field.toLowerCase().includes(timeField));
}

/**
 * 判断是否为数值字段
 */
function isNumericField(field) {
    const numericFields = ['score', 'severity', 'wait_time', 'type', 'state', 'flag', 'room_member_count', 'msg_type', 'active_type'];
    return numericFields.includes(field.toLowerCase());
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
 * 格式化日期时间
 */
function formatDateTime(dateString) {
    if (!dateString || dateString === 'null' || dateString === 'undefined' || dateString === undefined) {
        return '';
    }
    
    try {
        const date = new Date(dateString);
        if (isNaN(date.getTime())) {
            return dateString; // 如果无法解析，返回原始字符串
        }
        
        // 直接显示完整的日期时间格式
        return date.toLocaleDateString('zh-CN') + ' ' + date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
    } catch (e) {
        return dateString;
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
        console.log('分页容器不存在');
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
        console.log('分页按钮容器不存在');
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
    const convertToAplBtn = document.getElementById('convert-to-apl');
    const convertToSimcBtn = document.getElementById('convert-to-simc');
    const clearAllBtn = document.getElementById('clear-all');
    const copyResultBtn = document.getElementById('copy-result');
    const simcInput = document.getElementById('simc-input');
    const aplInput = document.getElementById('apl-input');
    
    if (!convertToAplBtn || !convertToSimcBtn || !clearAllBtn || !copyResultBtn || !simcInput || !aplInput) {
        return; // 如果元素不存在，直接返回
    }
    
    // 翻译按钮（从左侧APL转换到右侧SimC）
    convertToSimcBtn.addEventListener('click', function() {
        const aplText = aplInput.value.trim();
        if (!aplText) {
            showMessage('请先输入左侧APL代码内容', 'warning');
            return;
        }
        
        convertText(aplText, 'apl_to_cn')
            .then(result => {
                simcInput.value = result;
                showMessage('转换成功', 'success');
            })
            .catch(error => {
                showMessage('转换失败: ' + error.message, 'error');
            });
    });
    
    // 反向按钮（从右侧SimC转换到左侧APL）
    convertToAplBtn.addEventListener('click', function() {
        const simcText = simcInput.value.trim();
        if (!simcText) {
            showMessage('请先输入右侧SimC代码内容', 'warning');
            return;
        }
        
        convertText(simcText, 'cn_to_apl')
            .then(result => {
                aplInput.value = result;
                showMessage('转换成功', 'success');
            })
            .catch(error => {
                showMessage('转换失败: ' + error.message, 'error');
            });
    });
    
    // 清空所有内容
    clearAllBtn.addEventListener('click', function() {
        simcInput.value = '';
        aplInput.value = '';
        showMessage('已清空所有内容', 'info');
    });
    
    // 复制结果
    copyResultBtn.addEventListener('click', function() {
        const simcText = simcInput.value.trim();
        const aplText = aplInput.value.trim();
        
        if (!simcText && !aplText) {
            showMessage('没有可复制的内容', 'warning');
            return;
        }
        
        // 复制最后修改的内容
        const textToCopy = aplText || simcText;
        navigator.clipboard.writeText(textToCopy)
            .then(() => {
                showMessage('内容已复制到剪贴板', 'success');
            })
            .catch(() => {
                showMessage('复制失败，请手动复制', 'error');
            });
    });
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
    
    // 关闭弹窗事件
    closeModalBtn.addEventListener('click', closeAddRecordModal);
    cancelBtn.addEventListener('click', closeAddRecordModal);
    
    // 点击弹窗外部关闭
    modal.addEventListener('click', function(e) {
        if (e.target === modal) {
            closeAddRecordModal();
        }
    });
    
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
    const modal = document.getElementById('add-record-modal');
    const modalTitle = document.getElementById('modal-title');
    const formFields = document.getElementById('form-fields');
    
    // 设置弹窗标题
    modalTitle.textContent = `新增${currentTableName}记录`;
    
    // 生成表单字段
    generateFormFields(formFields);
    
    // 显示弹窗
    modal.classList.remove('hidden');
}

/**
 * 关闭新增记录弹窗
 */
function closeAddRecordModal() {
    const modal = document.getElementById('add-record-modal');
    const addRecordForm = document.getElementById('add-record-form');
    
    // 隐藏弹窗
    modal.classList.add('hidden');
    
    // 重置表单
    addRecordForm.reset();
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
        // 跳过ID字段和时间字段（通常由系统自动生成）
        if (column.toLowerCase() === 'id' || column.toLowerCase().includes('time')) {
            return;
        }
        
        const fieldDiv = document.createElement('div');
        fieldDiv.className = 'space-y-2';
        
        const label = document.createElement('label');
        label.className = 'block text-sm font-semibold text-gray-700';
        label.textContent = getFieldDisplayName(column);
        label.setAttribute('for', `field-${column}`);
        
        const inputType = getFieldInputType(column);
        let inputElement;
        
        if (inputType === 'textarea') {
            // 创建textarea元素
            inputElement = document.createElement('textarea');
            inputElement.rows = 4;
            inputElement.placeholder = `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200 resize-none';
        } else if (inputType === 'checkbox') {
            // 创建checkbox元素
            inputElement = document.createElement('input');
            inputElement.type = 'checkbox';
            inputElement.className = 'w-5 h-5 text-blue-600 border-gray-300 rounded focus:ring-2 focus:ring-blue-500 transition-all duration-200';
            // 为checkbox添加默认值处理
            if (currentFieldTypes && currentFieldTypes[column] && currentFieldTypes[column].default !== null) {
                inputElement.checked = currentFieldTypes[column].default;
            }
        } else {
            // 创建普通input元素
            inputElement = document.createElement('input');
            inputElement.type = inputType;
            inputElement.placeholder = `请输入${getFieldDisplayName(column)}`;
            inputElement.className = 'w-full px-4 py-3 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-all duration-200';
            
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
            checkboxWrapper.className = 'bg-gray-50 p-4 rounded-lg';
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
    const fieldNames = {
        'apl_keyword': 'APL关键字',
        'cn_keyword': '中文关键字',
        'description': '描述',
        'is_active': '是否激活',
        'name': '名称',
        'url': 'URL',
        'status': '状态',
        'content': '内容',
        'title': '标题'
    };
    
    return fieldNames[column] || column;
}

/**
 * 获取字段输入类型
 */
function getFieldInputType(column) {
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
    const requiredFields = ['apl_keyword', 'cn_keyword', 'name', 'title', 'url'];
    return requiredFields.includes(column.toLowerCase());
}

/**
 * 提交新增记录
 */
function submitAddRecord() {
    const form = document.getElementById('add-record-form');
    const data = {};
    
    // 遍历所有表单字段，正确处理不同类型的输入
    currentTableColumns.forEach(column => {
        // 跳过自动生成的字段
        if (column === 'id' || column.includes('created_at') || column.includes('updated_at')) {
            return;
        }
        
        const element = document.getElementById(`field-${column}`);
        if (element) {
            const inputType = getFieldInputType(column);
            
            if (inputType === 'checkbox') {
                // 对于checkbox，获取checked状态
                data[column] = element.checked;
            } else if (inputType === 'number') {
                // 对于数字类型，转换为数字或保持空值
                const value = element.value.trim();
                if (value !== '') {
                    data[column] = parseFloat(value);
                } else {
                    data[column] = null;
                }
            } else {
                // 对于其他类型，直接获取值
                data[column] = element.value;
            }
        }
    });
    
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

/**
 * 初始化页面大小选择器
 */
function initPageSizeSelector() {
    const pageSizeSelect = document.getElementById('page-size-select');
    if (!pageSizeSelect) {
        console.log('页面大小选择器不存在');
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

// 在DOMContentLoaded事件中初始化SimC APL转换工具
// 关键字管理功能的初始化已移至主要的DOMContentLoaded事件中