from django.apps import AppConfig


def remove_combat_account_policies(sender, instance, using, **kwargs):
    from botend.models import CombatLogPolicy, CombatPublicRolePolicy
    for policy in (CombatLogPolicy, CombatPublicRolePolicy):
        policy.objects.using(using).filter(scope=str(instance.pk)).delete()


class BotendConfig(AppConfig):
    name = 'botend'
    default_auto_field = 'django.db.models.AutoField'

    def ready(self):
        from django.db.models.signals import post_delete
        from django.contrib.auth import get_user_model
        from botend.models import CombatLogReport
        from botend.services.combat_log_archive import record_purge
        post_delete.connect(record_purge, sender=CombatLogReport, dispatch_uid="combat_log_private_cleanup")
        post_delete.connect(remove_combat_account_policies, sender=get_user_model(), dispatch_uid="combat_account_policies")
        from botend.services.simc_benchmark_result_snapshot import register_snapshot_signals
        register_snapshot_signals()
        from botend.services.simc_reference_ranking_index import register_signals as register_ranking_signals
        register_ranking_signals()
        from botend.services.news_snapshot import register_news_signals
        register_news_signals()
        from botend.services.class_guide_snapshot import register_guide_signals
        register_guide_signals()
        from botend.services.journal_snapshot import register_journal_signals
        register_journal_signals()
        # 部署配置由各主机维护，在应用启动时统一注册，避免遗漏独立报告页。
        from django.conf import settings
        middleware = 'botend.analytics.middleware.SiteAnalyticsMiddleware'
        if middleware not in settings.MIDDLEWARE:
            settings.MIDDLEWARE = [*settings.MIDDLEWARE, middleware]
