import hashlib
import re
import time

import requests
from django.core.management.base import BaseCommand

from botend.models import PortalToolLink
from utils.LReq import LReq


def _hash_url(url):
    return hashlib.sha256(str(url).encode('utf-8')).hexdigest()


class Command(BaseCommand):
    help = 'Sync PortalToolLink from KuangBaoZhan flowus page'

    def handle(self, *args, **options):
        url = 'https://kuangbaozhan.flowus.cn/f0e00ce9-362a-44a8-9530-deec9e387bb2'
        links = []
        try:
            req = LReq(is_chrome=True)
            driver = req.get(url, 'RespByChrome', 0, '', is_origin=1)
            if driver:
                time.sleep(2)
                anchors = driver.eles('tag:a') or []
                for a in anchors:
                    try:
                        href = getattr(a, 'link', None) or a.attr('href') or ''
                    except Exception:
                        href = ''
                    href = (href or '').strip()
                    if not href:
                        continue
                    links.append(href.split('#', 1)[0])
            try:
                req.close_driver()
            except Exception:
                pass
        except Exception:
            links = []

        if not links:
            resp = requests.get(url, timeout=25, headers={'User-Agent': 'Mozilla/5.0'})
            if resp.status_code != 200:
                self.stdout.write('Fetch failed.')
                return
            html = resp.text or ''
            links = re.findall(r'href="(https?://[^"]+)"', html, flags=re.I)
            links = [x.strip() for x in links if x and not x.startswith('javascript:')]
            links = [x.split('#', 1)[0] for x in links]

        if not links:
            preset = [
                ('灵魂复苏前瞻资讯', 'https://www.ngasb.com/', '魔兽蓝贴&资讯&新闻'),
                ('NGA 魔兽世界', 'https://bbs.nga.cn/thread.php?fid=7', '社区论坛'),
                ('Wowhead', 'https://www.wowhead.com/', '数据库与新闻'),
                ('MythicStats', 'https://mythicstats.com/dps', '职业排名统计'),
                ('法反列表', 'https://tinyurl.com/TWWreflects', '每赛季更新的法反列表'),
                ('WarcraftLogs', 'https://cn.warcraftlogs.com/', '战斗日志汇总'),
                ('Raider.IO', 'https://raider.io/', '大秘境与团本进度'),
                ('Bloodmallet', 'https://bloodmallet.com/index', '饰品/附魔/宝石模拟'),
                ('Not Even Close', 'https://not-even-close.vercel.app/', '计算是否吃得住技能'),
                ('QE Live', 'https://questionablyepic.com/live/', '奶妈饰品模拟'),
                ('U.GG', 'https://u.gg/wow/', '天赋与构筑'),
                ('Archon', 'https://www.archon.gg/wow', 'WCL 数据统计'),
                ('Maxroll', 'https://maxroll.gg/wow', '职业攻略与资源'),
                ('Murlok', 'https://murlok.io/', 'PvP/PvE 构筑数据'),
                ('Raidbots', 'https://www.raidbots.com/', 'SimC 模拟与对比'),
                ('Wago', 'https://wago.io/', 'WA/字符串分享'),
                ('CurseForge', 'https://www.curseforge.com/wow', '插件下载'),
                ('拍卖行价格趋势', 'https://wow.jiguanqiang.net/', '拍卖行数据趋势分析'),
            ]
            topbar_defaults = [
                'cn.warcraftlogs.com',
                'raider.io',
                'wowhead.com',
                'maxroll.gg',
                'archon.gg',
                'raidbots.com',
                'bloodmallet.com',
                'wago.io',
                'curseforge.com',
            ]

            sort_order = 10
            for name, u, desc in preset:
                h = _hash_url(u)
                is_topbar = any(d in u for d in topbar_defaults)
                topbar_order = 0
                for idx, d in enumerate(topbar_defaults, start=1):
                    if d in u:
                        topbar_order = idx
                        break
                PortalToolLink.objects.update_or_create(
                    url_hash=h,
                    defaults={
                        'name': name,
                        'url': u,
                        'desc': desc,
                        'source': 'kuangbaozhan',
                        'sort_order': sort_order,
                        'is_topbar': bool(is_topbar),
                        'topbar_order': topbar_order,
                        'is_active': True,
                    },
                )
                sort_order += 10

            self.stdout.write(f'Synced {len(preset)} tools.')
            return

        skip_hosts = (
            'kuangbaozhan.flowus.cn',
            'flowus.cn',
        )

        name_overrides = {
            'cn.warcraftlogs.com': 'WarcraftLogs',
            'raider.io': 'Raider.IO',
            'www.wowhead.com': 'Wowhead',
            'maxroll.gg': 'Maxroll',
            'www.archon.gg': 'Archon',
            'www.raidbots.com': 'Raidbots',
            'bloodmallet.com': 'Bloodmallet',
            'wago.io': 'Wago',
            'www.curseforge.com': 'CurseForge',
            'mythicstats.com': 'MythicStats',
            'murlok.io': 'Murlok',
        }

        uniq = {}
        for link in links:
            if any(h in link for h in skip_hosts):
                continue
            link = link.rstrip(').,，。；;')
            h = _hash_url(link)
            if h in uniq:
                continue
            host = re.sub(r'^https?://', '', link, flags=re.I).split('/', 1)[0]
            host = host.lower()
            name = name_overrides.get(host) or host
            uniq[h] = {'name': name, 'desc': '', 'url': link, 'url_hash': h}

        if not uniq:
            preset = [
                ('灵魂复苏前瞻资讯', 'https://www.ngasb.com/', '魔兽蓝贴&资讯&新闻'),
                ('NGA 魔兽世界', 'https://bbs.nga.cn/thread.php?fid=7', '社区论坛'),
                ('Wowhead', 'https://www.wowhead.com/', '数据库与新闻'),
                ('MythicStats', 'https://mythicstats.com/dps', '职业排名统计'),
                ('法反列表', 'https://tinyurl.com/TWWreflects', '每赛季更新的法反列表'),
                ('WarcraftLogs', 'https://cn.warcraftlogs.com/', '战斗日志汇总'),
                ('Raider.IO', 'https://raider.io/', '大秘境与团本进度'),
                ('Bloodmallet', 'https://bloodmallet.com/index', '饰品/附魔/宝石模拟'),
                ('Not Even Close', 'https://not-even-close.vercel.app/', '计算是否吃得住技能'),
                ('QE Live', 'https://questionablyepic.com/live/', '奶妈饰品模拟'),
                ('U.GG', 'https://u.gg/wow/', '天赋与构筑'),
                ('Archon', 'https://www.archon.gg/wow', 'WCL 数据统计'),
                ('Maxroll', 'https://maxroll.gg/wow', '职业攻略与资源'),
                ('Murlok', 'https://murlok.io/', 'PvP/PvE 构筑数据'),
                ('Raidbots', 'https://www.raidbots.com/', 'SimC 模拟与对比'),
                ('Wago', 'https://wago.io/', 'WA/字符串分享'),
                ('CurseForge', 'https://www.curseforge.com/wow', '插件下载'),
                ('拍卖行价格趋势', 'https://wow.jiguanqiang.net/', '拍卖行数据趋势分析'),
            ]
            topbar_defaults = [
                'cn.warcraftlogs.com',
                'raider.io',
                'wowhead.com',
                'maxroll.gg',
                'archon.gg',
                'raidbots.com',
                'bloodmallet.com',
                'wago.io',
                'curseforge.com',
            ]
            sort_order = 10
            for name, u, desc in preset:
                h = _hash_url(u)
                is_topbar = any(d in u for d in topbar_defaults)
                topbar_order = 0
                for idx, d in enumerate(topbar_defaults, start=1):
                    if d in u:
                        topbar_order = idx
                        break
                PortalToolLink.objects.update_or_create(
                    url_hash=h,
                    defaults={
                        'name': name,
                        'url': u,
                        'desc': desc,
                        'source': 'kuangbaozhan',
                        'sort_order': sort_order,
                        'is_topbar': bool(is_topbar),
                        'topbar_order': topbar_order,
                        'is_active': True,
                    },
                )
                sort_order += 10
            self.stdout.write(f'Synced {len(preset)} tools.')
            return

        topbar_defaults = [
            'cn.warcraftlogs.com',
            'raider.io',
            'wowhead.com',
            'maxroll.gg',
            'archon.gg',
            'raidbots.com',
            'bloodmallet.com',
            'wago.io',
            'curseforge.com',
        ]

        items = list(uniq.values())
        items.sort(key=lambda x: x['name'])

        sort_order = 10
        for it in items:
            u = it['url']
            is_topbar = any(d in u for d in topbar_defaults)
            topbar_order = 0
            for idx, d in enumerate(topbar_defaults, start=1):
                if d in u:
                    topbar_order = idx
                    break

            PortalToolLink.objects.update_or_create(
                url_hash=it['url_hash'],
                defaults={
                    'name': it['name'],
                    'url': it['url'],
                    'desc': it['desc'] or '',
                    'source': 'kuangbaozhan',
                    'sort_order': sort_order,
                    'is_topbar': bool(is_topbar),
                    'topbar_order': topbar_order,
                    'is_active': True,
                },
            )
            sort_order += 10

        self.stdout.write(f'Synced {len(items)} tools.')
