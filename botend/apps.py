from django.apps import AppConfig


class BotendConfig(AppConfig):
    name = 'botend'
    default_auto_field = 'django.db.models.AutoField'

    def ready(self):
        from botend.services.simc_benchmark_result_snapshot import register_snapshot_signals
        register_snapshot_signals()
        from botend.services.news_snapshot import register_news_signals
        register_news_signals()
        # 部署配置由各主机维护，在应用启动时统一注册，避免遗漏独立报告页。
        from django.conf import settings
        middleware = 'botend.analytics.middleware.SiteAnalyticsMiddleware'
        if middleware not in settings.MIDDLEWARE:
            settings.MIDDLEWARE = [*settings.MIDDLEWARE, middleware]
