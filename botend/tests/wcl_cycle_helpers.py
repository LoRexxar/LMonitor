"""仅推进发布状态的单元测试辅助；真实文件发布由集成测试验证。"""
from botend.services.wcl_collection_cycle import begin_aggregation, finish_publication


def publish_attempt(cycle):
    publication = begin_aggregation(cycle.season)
    if publication is None:
        raise AssertionError('本轮采集结果应立即允许发布')
    finish_publication(publication)
    cycle.refresh_from_db()
