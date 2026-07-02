import os
import re
import tempfile
from hashlib import sha1
from urllib.parse import urlparse

from django.conf import settings

from utils.log import logger


def upload_article_images_in_blocks(blocks, *, req=None, article_url="", source="portal", upload_cache=None):
    result = []
    upload_cache = upload_cache if isinstance(upload_cache, dict) else {}
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        new_block = dict(block)
        if new_block.get("type") == "html":
            new_block["html"] = upload_article_html_images(
                new_block.get("html") or "",
                req=req,
                article_url=article_url,
                source=source,
                upload_cache=upload_cache,
            )
            if new_block.get("original_html"):
                new_block["original_html"] = upload_article_html_images(
                    new_block.get("original_html") or "",
                    req=req,
                    article_url=article_url,
                    source=source,
                    upload_cache=upload_cache,
                )
        elif new_block.get("type") == "image":
            image_url = (new_block.get("url") or "").strip()
            if _is_external_article_image_url(image_url):
                uploaded_url = upload_cache.get(image_url)
                if uploaded_url is None:
                    uploaded_url = download_and_upload_article_image(
                        image_url,
                        req=req,
                        article_url=article_url,
                        source=source,
                    )
                    upload_cache[image_url] = uploaded_url or ""
                if uploaded_url:
                    new_block["source_url"] = image_url
                    new_block["url"] = uploaded_url
        result.append(new_block)
    return result


def upload_article_html_images(html_text, *, req=None, article_url="", source="portal", upload_cache=None):
    if not html_text:
        return html_text
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        upload_cache = upload_cache if isinstance(upload_cache, dict) else {}
        changed = False

        def upload_url(image_url):
            image_url = (image_url or "").strip()
            if not _is_external_article_image_url(image_url):
                return ""
            uploaded_url = upload_cache.get(image_url)
            if uploaded_url is None:
                uploaded_url = download_and_upload_article_image(
                    image_url,
                    req=req,
                    article_url=article_url,
                    source=source,
                )
                upload_cache[image_url] = uploaded_url or ""
            return uploaded_url or ""

        for img in soup.find_all("img"):
            image_url = (img.get("src") or "").strip()
            source_url = (img.get("data-source-src") or "").strip()
            if source_url and _is_external_article_image_url(source_url):
                img.attrs.pop("data-source-src", None)
                changed = True
            uploaded_url = upload_url(image_url)
            if uploaded_url:
                img["src"] = uploaded_url
                parent_link = img.find_parent("a")
                if parent_link and _is_external_article_image_url((parent_link.get("href") or "").strip()):
                    parent_link["href"] = uploaded_url
                changed = True
        for link in soup.find_all("a"):
            href = (link.get("href") or "").strip()
            if not _is_external_article_image_url(href):
                continue
            path = urlparse(href).path.lower()
            if not re.search(r"\.(?:png|jpe?g|webp|gif)$", path):
                continue
            uploaded_url = upload_url(href)
            if uploaded_url:
                link["href"] = uploaded_url
                changed = True
        return str(soup) if changed else html_text
    except Exception as exc:
        logger.warning("[article_image_service] Upload article html images failed {}: {}".format(article_url, str(exc)))
        return html_text


def download_and_upload_article_image(image_url, *, req=None, article_url="", source="portal"):
    image_url = (image_url or "").strip()
    if not image_url or not image_url.startswith(("http://", "https://")):
        return ""
    try:
        from botend.interface.ossupload import ossUploadObject
        resp = _fetch_image_response(image_url, req=req)
        if not resp:
            return ""
        status_code = getattr(resp, "status_code", 200)
        if int(status_code or 0) >= 400:
            return ""
        content = getattr(resp, "content", None)
        if content is None:
            text = getattr(resp, "text", "") or ""
            content = text.encode("utf-8")
        if not content:
            return ""
        suffix = _image_suffix(image_url, getattr(resp, "headers", {}) or {})
        digest = sha1(image_url.encode("utf-8")).hexdigest()[:16]
        article_slug = _article_slug(article_url)
        safe_source = re.sub(r"[^a-zA-Z0-9_-]+", "-", source or "portal").strip("-") or "portal"
        object_key = "portal/articles/{}/{}/{}{}".format(safe_source[:48], article_slug, digest, suffix)
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            return ossUploadObject(tmp_path, object_key=object_key) or ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except Exception as exc:
        logger.warning("[article_image_service] Upload article image failed {}: {}".format(image_url, str(exc)))
        return ""


def _fetch_image_response(image_url, *, req=None):
    try:
        import requests
        headers = {"User-Agent": "Mozilla/5.0"}
        session = getattr(req, "s", None) if req else None
        if session is not None:
            try:
                get_header = getattr(req, "get_header", None)
                if callable(get_header):
                    headers = get_header(image_url, "", {"Accept": "image/avif,image/webp,image/png,image/jpeg,image/*,*/*;q=0.8"})
            except Exception:
                headers = {"User-Agent": "Mozilla/5.0"}
            proxies = _get_request_proxies(req)
            return session.get(image_url, timeout=(8, 20), headers=headers, proxies=proxies or None)

        proxies = _get_configured_proxies()
        return requests.get(image_url, timeout=(8, 20), headers=headers, proxies=proxies or None)
    except Exception:
        logger.warning("[article_image_service] Direct image request failed: {}".format(image_url))
        return None


def _get_configured_proxies():
    try:
        from django.conf import settings as django_settings
        proxies = getattr(django_settings, "PROXY_CONFIG", None)
        if isinstance(proxies, dict) and proxies:
            return proxies
        req_cfg = getattr(django_settings, "REQUEST_CONFIG", {}) or {}
        proxies = req_cfg.get("proxies") if isinstance(req_cfg, dict) else None
        return proxies if isinstance(proxies, dict) and proxies else None
    except Exception:
        return None


def _get_request_proxies(req):
    session = getattr(req, "s", None) if req else None
    session_proxies = getattr(session, "proxies", None) or None
    if session_proxies:
        return session_proxies

    task = getattr(req, "current_task", None) if req else None
    if bool(getattr(task, "proxy_enabled", False)):
        return _get_configured_proxies()
    return None


def _is_external_article_image_url(image_url):
    image_url = (image_url or "").strip()
    if not image_url.startswith(("http://", "https://")):
        return False
    base_url = ((getattr(settings, "OSS_CONFIG", {}) or {}).get("base_url") or "").strip()
    if base_url and image_url.startswith(base_url.rstrip("/") + "/"):
        return False
    return True


def _image_suffix(image_url, headers):
    content_type = ""
    try:
        content_type = (headers.get("Content-Type") or headers.get("content-type") or "").split(";")[0].strip().lower()
    except Exception:
        content_type = ""
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type in mapping:
        return mapping[content_type]
    path = urlparse(image_url).path.lower()
    _, ext = os.path.splitext(path)
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _article_slug(article_url):
    path = urlparse(article_url or "").path.strip("/")
    slug = path.split("/")[-1] if path else "article"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-")
    return slug[:96] or "article"
