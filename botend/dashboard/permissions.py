from collections import OrderedDict
from functools import wraps

from django.core.exceptions import ValidationError
from django.http import JsonResponse


DASHBOARD_PAGE_PERMISSIONS = OrderedDict((item['code'], item) for item in (
    {'code': 'dashboard.home', 'label': '首页', 'section': 'dashboard-home', 'parent': '概览'},
    {'code': 'dashboard.user-management', 'label': '用户管理', 'section': 'user-management', 'parent': '系统'},
    {'code': 'dashboard.user-groups', 'label': '用户组管理', 'section': 'user-groups', 'parent': '系统'},
    {'code': 'news.index', 'label': '新闻资讯', 'section': 'news', 'parent': '内容'},
    {'code': 'reports.wow-daily', 'label': 'WoW 日报', 'section': 'wow-daily-reports', 'parent': '内容'},
    {'code': 'reports.hotfix', 'label': 'Hotfix 报告', 'section': 'wago-hotfix-reports', 'parent': '内容'},
    {'code': 'system.alerts', 'label': '系统报警', 'section': 'error-logs', 'parent': '系统'},
    {'code': 'system.logs', 'label': '日志文件', 'section': 'log-files', 'parent': '系统'},
    {'code': 'mythic.config', 'label': '规划器设置', 'section': 'mythic-planner-config', 'parent': '大秘境规划器'},
    {'code': 'mythic.positions', 'label': '位置标记', 'section': 'mythic-planner-positions', 'parent': '大秘境规划器'},
    {'code': 'mythic.routes', 'label': '路线管理', 'section': 'mythic-planner-routes', 'parent': '大秘境规划器'},
    {'code': 'simc.workflow', 'label': '工作流', 'section': 'simc-workflow', 'parent': 'SimC'},
    {'code': 'simc.history', 'label': '历史任务', 'section': 'simc-history', 'parent': 'SimC'},
    {'code': 'simc.advanced', 'label': '高级设置', 'section': 'simc-advanced', 'parent': 'SimC'},
    {'code': 'simc.skill-damage', 'label': '技能伤害快照', 'section': 'simc-skill-damage', 'parent': 'SimC'},
    {'code': 'simc.benchmarks', 'label': '基准测试', 'section': 'simc-benchmarks', 'parent': 'SimC'},
    {'code': 'tools.wcl-analysis', 'label': 'WCL 分析', 'section': 'wcl-analysis-entry', 'parent': '工具箱'},
    {'code': 'tools.wago-rerun', 'label': 'Wago 指定版本重跑', 'section': 'wago-skill-diff-rerun', 'parent': '工具箱'},
    {'code': 'database.tables', 'label': '数据库', 'section': 'database-tables', 'parent': '系统'},
))

SECTION_PERMISSION_CODES = {
    item['section']: code for code, item in DASHBOARD_PAGE_PERMISSIONS.items()
}


def validate_permission_codes(codes):
    if not isinstance(codes, list) or any(not isinstance(code, str) for code in codes):
        raise ValidationError({'permission_codes': ['必须是权限 code 字符串数组']})
    if len(codes) != len(set(codes)):
        raise ValidationError({'permission_codes': ['不能包含重复 code']})
    unknown = sorted(set(codes) - set(DASHBOARD_PAGE_PERMISSIONS))
    if unknown:
        raise ValidationError({'permission_codes': [f'未知权限：{", ".join(unknown)}']})
    return list(codes)


def effective_dashboard_permissions(user):
    if not user or not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set(DASHBOARD_PAGE_PERMISSIONS)
    codes = set()
    for group in user.dashboard_user_groups.filter(is_active=True).only('permission_codes'):
        codes.update(code for code in group.permission_codes if code in DASHBOARD_PAGE_PERMISSIONS)
    return codes


def has_dashboard_permission(user, code):
    if code not in DASHBOARD_PAGE_PERMISSIONS:
        raise ValueError(f'Unknown dashboard permission: {code}')
    return code in effective_dashboard_permissions(user)


def dashboard_permission_required(code):
    if code not in DASHBOARD_PAGE_PERMISSIONS:
        raise ValueError(f'Unknown dashboard permission: {code}')

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not has_dashboard_permission(request.user, code):
                return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator


class DashboardPermissionRequiredMixin:
    dashboard_permission = None

    def dispatch(self, request, *args, **kwargs):
        if not self.dashboard_permission:
            raise ValueError('dashboard_permission is required')
        if not has_dashboard_permission(request.user, self.dashboard_permission):
            return JsonResponse({'status': 'error', 'message': '无权访问该 Dashboard 页面'}, status=403)
        return super().dispatch(request, *args, **kwargs)


def permission_catalog():
    return [dict(item) for item in DASHBOARD_PAGE_PERMISSIONS.values()]
