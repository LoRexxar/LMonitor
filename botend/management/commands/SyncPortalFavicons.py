import os
import re
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.core.management.base import BaseCommand

from utils.log import logger
from botend.models import PortalToolLink


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, **options):
        force = bool(options.get('force'))
        limit = int(options.get('limit') or 0)

        out_dir = os.path.join(settings.BASE_DIR, 'static', 'portal', 'favicons')
        os.makedirs(out_dir, exist_ok=True)

        qs = PortalToolLink.objects.filter(is_active=True).order_by('id')
        if limit > 0:
            qs = qs[:limit]

        updated = 0
        skipped = 0
        failed = 0

        for t in qs:
            if (t.icon_path or '').strip() and not force:
                skipped += 1
                continue
            if not (t.url or '').strip() or not (t.url_hash or '').strip():
                skipped += 1
                continue
            try:
                icon_url = self._discover_icon_url(t.url)
                if not icon_url:
                    failed += 1
                    continue
                ext, content = self._download_icon(icon_url)
                if not content:
                    failed += 1
                    continue
                filename = f"{t.url_hash}.{ext}"
                full_path = os.path.join(out_dir, filename)
                with open(full_path, 'wb') as f:
                    f.write(content)
                t.icon_path = f"/static/portal/favicons/{filename}"
                t.save(update_fields=['icon_path'])
                updated += 1
            except Exception as e:
                logger.warning(f"[PortalFavicon Sync] failed: {t.url} {str(e)}")
                failed += 1

        logger.info(f"[PortalFavicon Sync] done. updated={updated} skipped={skipped} failed={failed}")

    def _discover_icon_url(self, site_url: str):
        site_url = (site_url or '').strip()
        if not site_url:
            return None
        try:
            import requests
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(site_url, timeout=12, headers=headers, allow_redirects=True)
            base = resp.url or site_url
            ct = (resp.headers.get('Content-Type') or '').lower()
            html = ''
            if 'text/html' in ct or 'application/xhtml' in ct or ct == '':
                resp.encoding = resp.encoding or 'utf-8'
                html = (resp.text or '')[:200000]
            icon = self._extract_icon_href(html)
            if icon:
                return urljoin(base, icon)
            parsed = urlparse(base)
            return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
        except Exception:
            try:
                parsed = urlparse(site_url)
                if parsed.scheme and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
            except Exception:
                return None
        return None

    def _extract_icon_href(self, html: str):
        if not html:
            return None
        m = re.findall(r"<link[^>]+>", html, flags=re.IGNORECASE)
        if not m:
            return None
        candidates = []
        for tag in m:
            rel_m = re.search(r'rel\s*=\s*["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
            href_m = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
            if not rel_m or not href_m:
                continue
            rel = rel_m.group(1).lower()
            href = href_m.group(1).strip()
            if not href:
                continue
            if 'icon' not in rel:
                continue
            score = 10
            if 'apple-touch-icon' in rel:
                score = 5
            if 'shortcut' in rel:
                score = 1
            candidates.append((score, href))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _download_icon(self, icon_url: str):
        icon_url = (icon_url or '').strip()
        if not icon_url:
            return None, None
        import requests
        headers = {"User-Agent": "Mozilla/5.0", "Referer": icon_url}
        resp = requests.get(icon_url, timeout=15, headers=headers, allow_redirects=True)
        if resp.status_code != 200:
            return None, None
        ct = (resp.headers.get('Content-Type') or '').lower()
        ext = self._guess_ext(icon_url, ct)
        return ext, resp.content

    def _guess_ext(self, url: str, content_type: str):
        path = urlparse(url).path or ''
        lower = path.lower()
        for e in ['.svg', '.png', '.jpg', '.jpeg', '.ico', '.webp']:
            if lower.endswith(e):
                return e.lstrip('.')
        if 'image/svg' in content_type:
            return 'svg'
        if 'image/png' in content_type:
            return 'png'
        if 'image/jpeg' in content_type:
            return 'jpg'
        if 'image/webp' in content_type:
            return 'webp'
        if 'image/x-icon' in content_type or 'image/vnd.microsoft.icon' in content_type:
            return 'ico'
        return 'ico'

