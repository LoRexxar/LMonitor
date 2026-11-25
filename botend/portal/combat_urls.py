"""登录用户的日志库、上传和战斗分析路由。"""

from django.urls import path, reverse_lazy
from django.contrib.auth.decorators import login_required
from django.views.generic.base import RedirectView

from botend.dashboard import combat_library, combat_logs, combat_wcl, combat_workbench


urlpatterns = [
    path('', combat_logs.library, name='combat_logs'),
    path('upload/', combat_logs.uploader, name='combat_upload'),
    path('battles/', combat_library.battles, name='combat_battles'),
    path('progress/<uuid:token>/', combat_logs.request_progress, name='combat_request_progress'),
    path('wcl/', combat_wcl.home, name='combat_wcl'),
    path('wcl/workspace/', combat_wcl.report, name='combat_wcl_workspace'),
    path('wcl/connect/', combat_wcl.connect, name='combat_wcl_connect'),
    path('wcl/callback/', combat_wcl.callback, name='combat_wcl_callback'),
    path('wcl/disconnect/', combat_wcl.disconnect, name='combat_wcl_disconnect'),
    path('wcl/reports/<str:code>/', combat_wcl.report, name='combat_wcl_report'),
    path('wcl/reports/<str:code>/fights/<int:fight>/', combat_wcl.single, name='combat_wcl_single'),
    path('<uuid:pk>/remove/', combat_logs.remove_upload, name='combat_remove'),
    path('workspace/', combat_workbench.workspace, name='combat_workspace'),
    path('workspace/studies/crown/', login_required(RedirectView.as_view(url=reverse_lazy("combat_wcl_report",args=["dkc3TBQPZKYa7npm"])))),
    path('maps/<str:build>/<int:art>/', combat_workbench.replay_map, name='combat_replay_map'),
    path('players/', combat_workbench.player_comparison, name='combat_player_comparison'),
    path('teams/', combat_workbench.collection_list, name='combat_collections'),
    path('teams/<int:pk>/', combat_workbench.collection, name='combat_collection'),
    path('<uuid:pk>/analysis/', combat_workbench.workbench, name='combat_analysis'),
    path('<uuid:pk>/replay/<int:fight>/', combat_workbench.replay, name='combat_replay'),
    path('<uuid:pk>/actor/<int:fight>/<str:guid>/', combat_workbench.actor_view, name='combat_actor'),
    path('<uuid:pk>/event/<int:line>/', combat_workbench.event, name='combat_evidence'),
    path('<uuid:pk>/review/<int:fight>/<str:topic>/<str:anchor>/', combat_workbench.review, name='combat_review'),
    path('<uuid:pk>/rebuild/', combat_workbench.rebuild, name='combat_rebuild'),
    path('<uuid:pk>/', combat_logs.detail, name='combat_report'),
    path('<uuid:pk>/parts/', combat_logs.manifest, name='combat_manifest'),
    path('<uuid:pk>/original/', combat_logs.download, name='combat_download'),
    path('<uuid:pk>/parts/<int:number>/', combat_logs.upload_part, name='combat_part'),
    path('<uuid:pk>/finish/', combat_logs.finish, name='combat_finish'),
    path('<uuid:pk>/pause/', combat_logs.pause, name='combat_pause'),
    path('<uuid:pk>/highlights/', combat_logs.highlights, name='combat_highlights'),
]
