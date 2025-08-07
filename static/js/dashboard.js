/**
 * Dashboard页面的JavaScript功能
 */

document.addEventListener('DOMContentLoaded', function() {
    console.log('Dashboard页面已加载');
    
    // 初始化页面数据
    initDashboard();
    
    // 设置定时刷新
    setInterval(refreshData, 60000); // 每分钟刷新一次数据
});

/**
 * 初始化仪表盘数据
 */
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
    console.log('刷新数据...');
    // 实际应用中，这里应该发送AJAX请求获取最新数据
    // 然后更新页面上的内容
}

/**
 * 更新系统状态信息
 */
function updateSystemStatus() {
    // 模拟数据，实际应用中应该从服务器获取
    const uptime = Math.floor(Math.random() * 10) + '天' + Math.floor(Math.random() * 24) + '小时';
    const cpuUsage = Math.floor(Math.random() * 100) + '%';
    const memoryUsage = (Math.random() * 4).toFixed(1) + 'GB/8GB';
    
    // 更新DOM元素
    // 在实际应用中，应该使用document.querySelector选择正确的元素
}

/**
 * 更新最近活动信息
 */
function updateRecentActivities() {
    // 实际应用中应该从服务器获取数据
}

/**
 * 更新统计数据
 */
function updateStatistics() {
    // 实际应用中应该从服务器获取数据
}