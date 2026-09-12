"""通过现有监控后端低频检查并按需同步冒险手册。"""
from botend.controller.BaseScan import BaseScan
from botend.journal_models import JournalState
from botend.services.journal_service import sync_journal
from botend.services.journal_source import latest_retail_build


def current_release_build():
    state = JournalState.objects.select_related('active_release').filter(key='wow-zhCN').first()
    return state.active_release.build if state and state.active_release_id else ''


def retail_build(value):
    return str(value or '').split('+ptr-', 1)[0]


class AdventureJournalMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://wago.tools/journal'

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            build = latest_retail_build()
            current = current_release_build()
            if current and retail_build(current) == build:
                self.task.flag = f'正式服 {build} 未变化，无需完整同步'
                return True
            release = sync_journal(build=build, refresh=True)
            self.task.flag = f'冒险手册 {release.build} · {release.report["encounters"]} 个首领'
            return True
        except Exception as exc:
            self.last_error_detail = str(exc)[:1000]
            return False
