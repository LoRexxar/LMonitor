import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "LMonitor.settings")

import django

django.setup()

from django.utils import timezone

from botend.models import MonitorTask, RssMonitorTask, RssArticle, WowArticle, VulnData


def main():
    if MonitorTask.objects.count() == 0:
        MonitorTask.objects.create(
            name="示例监控任务-1",
            target="https://example.com",
            type=0,
            wait_time=600,
            is_active=True,
        )
        MonitorTask.objects.create(
            name="示例监控任务-2",
            target="https://example.com/login",
            type=1,
            wait_time=1200,
            is_active=False,
        )

    rss = RssMonitorTask.objects.first()
    if not rss:
        rss = RssMonitorTask.objects.create(
            name="示例 RSS 源",
            link="https://example.com/feed.xml",
            tag="demo",
            is_active=True,
        )

    if RssArticle.objects.count() == 0:
        for i in range(1, 6):
            RssArticle.objects.create(
                rss_id=rss.id,
                title=f"示例 RSS 文章 {i}",
                url=f"https://example.com/rss/{i}",
                author="demo",
                publish_time=timezone.now(),
                content_html="<p>demo</p>",
                is_active=True,
            )

    if WowArticle.objects.count() == 0:
        for i in range(1, 6):
            WowArticle.objects.create(
                title=f"示例 Wow 文章 {i}",
                url=f"https://example.com/wow/{i}",
                author="demo",
                description="用于本地预览的示例数据",
                publish_time=timezone.now(),
                is_active=True,
            )

    if VulnData.objects.count() == 0:
        for i in range(1, 6):
            VulnData.objects.create(
                title=f"示例漏洞 {i}",
                publish_time=timezone.now(),
                cveid=f"CVE-2026-{1000 + i}",
                score="7.5",
                link=f"https://example.com/cve/{i}",
                source="demo",
                is_active=True,
            )

    print(
        "seed ok:",
        {
            "MonitorTask": MonitorTask.objects.count(),
            "RssMonitorTask": RssMonitorTask.objects.count(),
            "RssArticle": RssArticle.objects.count(),
            "WowArticle": WowArticle.objects.count(),
            "VulnData": VulnData.objects.count(),
        },
    )


if __name__ == "__main__":
    main()
