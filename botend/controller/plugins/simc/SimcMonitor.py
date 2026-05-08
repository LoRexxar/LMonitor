#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: SimcMonitor.py
@time: 2025/1/15 10:00
@desc: SimC模拟监控

'''

import os
import subprocess
import hashlib
import time
import json
import re
import zipfile
import tarfile
import shutil
import platform as py_platform
from urllib.parse import urljoin
import requests
from django.conf import settings
from django.utils import timezone
from utils.log import logger
from botend.models import SimcTask, SimcProfile, SimcBackendBinary
from botend.alerting import upsert_system_alert
from botend.controller.BaseScan import BaseScan


class SimcMonitor(BaseScan):
    """
    SimC模拟监控
    """

    def __init__(self, req, task):
        super().__init__(req, task)
        
        self.task = task
        self.hint = ""
        
        # 从settings获取SimC配置
        self.simc_config = getattr(settings, 'SIMC_CONFIG', {})
        self.simc_path = self.simc_config.get('simc_path', '')
        self.result_path = os.path.join(os.getcwd(), self.simc_config.get('result_path', 'static/simc_results/'))
        self.simc_template_path = self.simc_config.get('simc_template', 'LMonitor/simc_template.txt')
        
        # 确保结果目录存在
        if not os.path.exists(self.result_path):
            os.makedirs(self.result_path, exist_ok=True)

    def _get_http_proxies(self):
        try:
            proxy = getattr(settings, 'PROXY_CONFIG', None)
            if not (isinstance(proxy, dict) and proxy):
                return None
            values = [str(v or '').strip().lower() for v in proxy.values()]
            uses_socks = any(v.startswith('socks') for v in values if v)
            if uses_socks:
                try:
                    import socks  # noqa: F401
                except Exception:
                    return None
            return proxy
        except Exception:
            return None

    def _get_runtime_platform(self):
        sys_name = str(py_platform.system() or '').lower()
        if 'windows' in sys_name:
            return 'win64'
        if 'linux' in sys_name:
            machine = str(py_platform.machine() or '').lower()
            if machine in ('aarch64', 'arm64'):
                return 'linuxarm64'
            return 'linux64'
        return 'win64'

    def _parse_version_from_path(self, simc_path, platform):
        text = str(simc_path or '')
        m = re.search(rf"simc-(.+?)-{re.escape(platform)}", text)
        return m.group(1) if m else ""

    def _format_eta(self, seconds):
        try:
            s = int(seconds)
        except Exception:
            return ""
        if s <= 0:
            return "00:00"
        if s >= 24 * 3600:
            h = s // 3600
            m = (s % 3600) // 60
            return f"{h:02d}:{m:02d}"
        m = s // 60
        ss = s % 60
        return f"{m:02d}:{ss:02d}"

    def _format_speed_mbps(self, bytes_per_sec):
        try:
            bps = float(bytes_per_sec)
        except Exception:
            return ""
        if bps <= 0:
            return ""
        return f"{bps / (1024 * 1024):.2f}MB/s"

    def _set_update_status(self, row, status=None, progress=None, is_updating=None, latest_version=None, last_error=None):
        try:
            fields = []
            if status is not None:
                row.update_status = str(status)[:255]
                fields.append('update_status')
            if progress is not None:
                row.update_progress = max(0, min(100, int(progress)))
                fields.append('update_progress')
            if is_updating is not None:
                row.is_updating = bool(is_updating)
                fields.append('is_updating')
            if latest_version is not None:
                row.latest_version = str(latest_version)[:128]
                fields.append('latest_version')
            if last_error is not None:
                row.last_error = str(last_error)[:500]
                fields.append('last_error')
            if fields:
                row.save(update_fields=fields)
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to update SimC progress state: {e}")

    def _fetch_latest_nightly(self, platform):
        index_url = "http://downloads.simulationcraft.org/nightly/?C=M;O=D"
        html = ""
        proxies = self._get_http_proxies()
        try:
            try:
                resp = requests.get(index_url, timeout=15, proxies=proxies)
            except requests.exceptions.ProxyError:
                if proxies:
                    resp = requests.get(index_url, timeout=15, proxies=None)
                else:
                    raise
            resp.raise_for_status()
            html = resp.text or ''
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to fetch nightly index via requests: {e}")
            html = self._fetch_text_via_system_tools(index_url)
        m = re.search(
            r'href="([^"]*simc-[^"]*' + re.escape(platform) + r'[^"]*\.(?:7z|zip|tar\\.gz|tar\\.xz|tgz))"',
            html,
            flags=re.IGNORECASE
        )
        if not m:
            return None
        href = m.group(1)
        file_name = href.split('/')[-1]
        download_url = urljoin("http://downloads.simulationcraft.org/nightly/", file_name)
        vm = re.search(rf"simc-(.+?)-{re.escape(platform)}", file_name, flags=re.IGNORECASE)
        version = vm.group(1) if vm else file_name
        return {"version": version, "url": download_url, "file_name": file_name}

    def _fetch_text_via_powershell(self, url, timeout_seconds=30):
        ps = shutil.which('powershell') or shutil.which('pwsh')
        if not ps:
            raise Exception("未找到 PowerShell")
        safe_url = str(url).replace("'", "''")
        cmd = [
            ps,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$ProgressPreference='SilentlyContinue'; "
            f"(Invoke-WebRequest -Uri '{safe_url}' -UseBasicParsing -TimeoutSec {int(timeout_seconds)}).Content",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds + 10)
        if proc.returncode != 0:
            raise Exception((proc.stderr or '').strip() or "PowerShell 请求失败")
        return proc.stdout or ""

    def _fetch_text_via_curl(self, url, timeout_seconds=30):
        curl = shutil.which('curl')
        if not curl:
            raise Exception("未找到 curl")
        cmd = [
            curl,
            "-L",
            "--fail",
            "--connect-timeout",
            "15",
            "--max-time",
            str(int(timeout_seconds)),
            url,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds + 10)
        if proc.returncode != 0:
            raise Exception((proc.stderr or '').strip() or "curl 请求失败")
        return proc.stdout or ""

    def _fetch_text_via_wget(self, url, timeout_seconds=30):
        wget = shutil.which('wget')
        if not wget:
            raise Exception("未找到 wget")
        cmd = [wget, "-q", "-O", "-", url]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds + 10)
        if proc.returncode != 0:
            raise Exception((proc.stderr or '').strip() or "wget 请求失败")
        return proc.stdout or ""

    def _fetch_text_via_system_tools(self, url):
        sys_name = str(py_platform.system() or '').lower()
        errors = []
        if 'windows' in sys_name:
            try:
                return self._fetch_text_via_powershell(url)
            except Exception as e:
                errors.append(f"powershell: {e}")
        for fn in (self._fetch_text_via_curl, self._fetch_text_via_wget):
            try:
                return fn(url)
            except Exception as e:
                errors.append(str(e))
        raise Exception("无法获取nightly列表页: " + " | ".join(errors))

    def _download_file(self, url, dest_path, progress_cb=None):
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        final_path = dest_path
        part_path = dest_path + ".part"

        if os.path.exists(final_path) and os.path.getsize(final_path) > 0:
            return final_path

        existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        headers = {}
        if existing > 0:
            headers['Range'] = f"bytes={existing}-"

        proxies = self._get_http_proxies()
        try:
            resp = requests.get(
                url,
                stream=True,
                timeout=(10, 60),
                proxies=proxies,
                headers=headers
            )
        except requests.exceptions.ProxyError:
            if proxies:
                resp = requests.get(
                    url,
                    stream=True,
                    timeout=(10, 60),
                    proxies=None,
                    headers=headers
                )
            else:
                raise
        if resp.status_code not in (200, 206):
            resp.raise_for_status()

        content_len = int(resp.headers.get('Content-Length') or 0)
        total = (existing + content_len) if resp.status_code == 206 else content_len
        downloaded = existing if resp.status_code == 206 else 0
        last_emit_percent = -1

        mode = 'ab' if resp.status_code == 206 and existing > 0 else 'wb'
        if mode == 'wb' and existing > 0:
            try:
                os.remove(part_path)
            except Exception:
                pass
            downloaded = 0

        with open(part_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    percent = int(downloaded * 100 / total)
                    if percent != last_emit_percent:
                        last_emit_percent = percent
                        if progress_cb:
                            progress_cb(downloaded, total, percent)
                else:
                    if progress_cb:
                        progress_cb(downloaded, 0, 0)

        if total > 0 and downloaded >= total:
            try:
                if os.path.exists(final_path):
                    os.remove(final_path)
            except Exception:
                pass
            os.replace(part_path, final_path)
        if progress_cb:
            progress_cb(downloaded, total, 100 if total > 0 else 0)
        return final_path if os.path.exists(final_path) else part_path

    def _safe_remove_file(self, path):
        if not path:
            return
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def _is_retryable_download_error(self, exc):
        try:
            if isinstance(
                exc,
                (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                ),
            ):
                return True
        except Exception:
            pass
        return False

    def _download_via_powershell_bits(self, url, dest_path, timeout_seconds=900):
        ps = shutil.which('powershell') or shutil.which('pwsh')
        if not ps:
            raise Exception("未找到 PowerShell")
        safe_url = str(url).replace("'", "''")
        safe_dest = str(dest_path).replace("'", "''")
        cmd = [
            ps,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"$ProgressPreference='SilentlyContinue'; "
            f"Start-BitsTransfer -Source '{safe_url}' -Destination '{safe_dest}' -TransferType Download -ErrorAction Stop",
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds, check=True)

    def _download_via_curl(self, url, dest_path, timeout_seconds=900):
        curl = shutil.which('curl')
        if not curl:
            raise Exception("未找到 curl")
        cmd = [
            curl,
            "-L",
            "--fail",
            "--retry",
            "3",
            "--retry-delay",
            "1",
            "--connect-timeout",
            "15",
            "--max-time",
            str(int(timeout_seconds)),
            "-o",
            dest_path,
            url,
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds + 30, check=True)

    def _download_via_wget(self, url, dest_path, timeout_seconds=900):
        wget = shutil.which('wget')
        if not wget:
            raise Exception("未找到 wget")
        cmd = [wget, "-O", dest_path, "--tries=3", "--timeout=30", url]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_seconds + 30, check=True)

    def _download_archive_with_resilience(self, row, latest_ver, url, archive_path, platform, progress_cb=None):
        retries = int(self.simc_config.get('download_retry', 3) or 3)
        backoff = float(self.simc_config.get('download_backoff_seconds', 1) or 1)
        fallback_enabled = bool(self.simc_config.get('download_fallback', True))

        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                if attempt > 1:
                    self._set_update_status(
                        row,
                        status=f'下载重试 {attempt}/{retries} {latest_ver}',
                        progress=1,
                        is_updating=True,
                    )
                self._download_file(url, archive_path, progress_cb=progress_cb)
                if not os.path.exists(archive_path):
                    raise Exception("下载未生成目标文件")
                if not self._validate_archive(archive_path, platform):
                    raise Exception("下载文件校验失败（压缩包损坏或未完整下载）")
                return
            except Exception as e:
                last_exc = e
                retryable = self._is_retryable_download_error(e) or ('校验失败' in str(e))
                if attempt < retries and retryable:
                    if '校验失败' in str(e):
                        self._safe_remove_file(archive_path)
                        self._safe_remove_file(archive_path + '.part')
                    time.sleep(backoff * (2 ** (attempt - 1)))
                    continue
                break

        if not fallback_enabled:
            raise last_exc or Exception("下载失败")

        part_path = archive_path + ".part"
        self._safe_remove_file(part_path)
        self._set_update_status(row, status=f'切换兜底下载 {latest_ver}', progress=5, is_updating=True)
        errors = []
        try:
            if str(platform).startswith('win'):
                try:
                    self._set_update_status(row, status=f'兜底下载中（BITS） {latest_ver}', progress=10, is_updating=True)
                    self._download_via_powershell_bits(url, part_path)
                except Exception as e:
                    errors.append(f"BITS: {e}")
                    self._download_via_curl(url, part_path)
            else:
                try:
                    self._set_update_status(row, status=f'兜底下载中（curl） {latest_ver}', progress=10, is_updating=True)
                    self._download_via_curl(url, part_path)
                except Exception as e:
                    errors.append(f"curl: {e}")
                    self._download_via_wget(url, part_path)
        except Exception as e:
            errors.append(str(e))
            raise Exception("兜底下载失败: " + " | ".join(errors))

        if not os.path.exists(part_path) or os.path.getsize(part_path) <= 0:
            raise Exception("兜底下载失败：未生成文件")
        self._safe_remove_file(archive_path)
        os.replace(part_path, archive_path)
        if not self._validate_archive(archive_path, platform):
            self._safe_remove_file(archive_path)
            raise Exception("兜底下载完成但校验失败（压缩包损坏或未完整下载）")

    def _build_manual_download_hint(self, platform, base_dir, file_name=None):
        platform_text = str(platform or '').strip() or 'win64'
        dir_text = str(base_dir or '').strip()
        if file_name:
            return f"可手动从 http://downloads.simulationcraft.org/nightly/ 下载匹配 {platform_text} 的压缩包并保存到 {dir_text}（文件名保持 {file_name}），下次扫描会自动解压安装"
        return f"可手动从 http://downloads.simulationcraft.org/nightly/ 下载匹配 {platform_text} 的压缩包并保存到 {dir_text}（文件名保持原样），下次扫描会自动解压安装"

    def _pick_local_archive(self, base_dir, platform):
        try:
            if not base_dir or not os.path.isdir(base_dir):
                return ""
            platform_text = str(platform or '').strip()
            exts = ('.7z', '.zip', '.tar.gz', '.tar.xz', '.tgz')
            candidates = []
            for name in os.listdir(base_dir):
                lower = str(name).lower()
                if not lower.startswith('simc-'):
                    continue
                if platform_text and (platform_text.lower() not in lower):
                    continue
                if not any(lower.endswith(ext) for ext in exts):
                    continue
                full = os.path.join(base_dir, name)
                if os.path.isfile(full):
                    try:
                        candidates.append((os.path.getmtime(full), full))
                    except Exception:
                        candidates.append((0, full))
            if not candidates:
                return ""
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]
        except Exception:
            return ""

    def _try_install_from_local_archive(self, row, platform, base_dir):
        archive_path = self._pick_local_archive(base_dir, platform)
        if not archive_path:
            return False
        file_name = os.path.basename(archive_path)
        if not self._validate_archive(archive_path, platform):
            self._safe_remove_file(archive_path)
            self._safe_remove_file(archive_path + '.part')
            raise Exception("发现本地安装包但校验失败（已删除），请重新下载")
        vm = re.search(rf"simc-(.+?)-{re.escape(platform)}", file_name, flags=re.IGNORECASE)
        local_ver = vm.group(1) if vm else file_name
        extract_dir = os.path.join(base_dir, f"simc-{local_ver}-{platform}")
        self._set_update_status(row, status=f'发现本地安装包，解压中 {local_ver}', progress=85, is_updating=True)
        if not os.path.exists(extract_dir):
            self._extract_archive(archive_path, extract_dir, platform)
        self._set_update_status(row, status='定位可执行文件', progress=95, is_updating=True)
        exe_path = self._find_simc_executable(extract_dir, platform)
        if not exe_path:
            raise Exception(f"未在解压目录中找到SimC可执行文件: {extract_dir}")
        self._ensure_executable_permission(exe_path, platform)
        now = timezone.now()
        row.simc_path = exe_path
        row.current_version = local_ver
        row.latest_version = local_ver
        row.last_updated_at = now
        row.last_checked_at = now
        row.last_error = ''
        row.is_updating = False
        row.update_progress = 100
        row.update_status = f'安装完成 {local_ver}'
        row.save(
            update_fields=[
                'simc_path',
                'current_version',
                'latest_version',
                'last_updated_at',
                'last_checked_at',
                'last_error',
                'is_updating',
                'update_progress',
                'update_status',
            ]
        )
        settings.SIMC_CONFIG['simc_path'] = exe_path
        self.simc_path = exe_path
        return True

    def _validate_archive(self, archive_path, platform):
        p = str(archive_path)
        lower = p.lower()
        if lower.endswith(('.tar.gz', '.tar.xz', '.tgz')):
            try:
                return tarfile.is_tarfile(p)
            except Exception:
                return False
        if lower.endswith('.zip'):
            try:
                return zipfile.is_zipfile(p)
            except Exception:
                return False
        if lower.endswith('.7z'):
            try:
                with open(p, 'rb') as f:
                    sig = f.read(6)
                if sig != b"7z\xbc\xaf'\x1c":
                    return False
            except Exception:
                return False
            try:
                import py7zr
                with py7zr.SevenZipFile(p, mode='r') as z:
                    _ = z.getnames()
                return True
            except Exception:
                return False
        return False

    def _extract_archive(self, archive_path, extract_dir, platform):
        os.makedirs(extract_dir, exist_ok=True)
        lower = str(archive_path).lower()
        if lower.endswith(('.tar.gz', '.tar.xz', '.tgz')):
            with tarfile.open(archive_path, 'r:*') as tf:
                base = os.path.realpath(extract_dir)
                base_prefix = base + os.sep
                for m in tf.getmembers():
                    target = os.path.realpath(os.path.join(extract_dir, m.name))
                    if not (target == base or target.startswith(base_prefix)):
                        raise Exception("压缩包内容包含非法路径")
                tf.extractall(extract_dir)
            return extract_dir
        if lower.endswith('.zip'):
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(extract_dir)
            return extract_dir

        if lower.endswith('.7z'):
            try:
                import py7zr
            except Exception:
                candidates = []
                if platform.startswith("win"):
                    candidates.extend([r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"])
                    seven_zip = next((p for p in candidates if os.path.exists(p)), "")
                else:
                    seven_zip = shutil.which('7z') or shutil.which('7zz') or ''
                if not seven_zip:
                    raise Exception("无法解压 .7z：未安装 7-Zip（7z.exe）且未安装 py7zr")
                subprocess.run([seven_zip, "x", "-y", f"-o{extract_dir}", archive_path], check=True)
                return extract_dir
            try:
                with py7zr.SevenZipFile(archive_path, mode='r') as z:
                    z.extractall(path=extract_dir)
                return extract_dir
            except Exception as e:
                raise Exception(f"py7zr解压失败: {str(e)}")

        raise Exception(f"不支持的压缩格式: {archive_path}")

    def _has_7z_extractor(self, platform):
        try:
            import py7zr  # noqa: F401
            return True
        except Exception:
            pass
        if str(platform).startswith("win"):
            for p in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
                if os.path.exists(p):
                    return True
        else:
            if shutil.which('7z') or shutil.which('7zz'):
                return True
        return False

    def _find_simc_executable(self, root_dir, platform):
        exe_name = "simc.exe" if platform.startswith("win") else "simc"
        for base, _, files in os.walk(root_dir):
            for fn in files:
                if fn.lower() == exe_name.lower():
                    return os.path.join(base, fn)
        return ""

    def _ensure_executable_permission(self, exe_path, platform):
        if not exe_path or str(platform).startswith('win'):
            return
        try:
            st = os.stat(exe_path)
            os.chmod(exe_path, st.st_mode | 0o111)
        except Exception:
            pass

    def ensure_simc_backend_up_to_date(self):
        platform = self._get_runtime_platform()
        phase = ''
        base_dir = os.path.join(os.getcwd(), 'bin', 'simc')
        known_file_name = None
        try:
            os.makedirs(base_dir, exist_ok=True)
            now = timezone.now()
            row = SimcBackendBinary.objects.filter(platform=platform).first()
            if not row:
                row = SimcBackendBinary(platform=platform)
                row.simc_path = str(getattr(settings, 'SIMC_CONFIG', {}).get('simc_path', '') or '')
                row.current_version = self._parse_version_from_path(row.simc_path, platform)
                row.auto_update = True
                row.last_checked_at = None
                row.last_updated_at = None
                row.latest_version = row.current_version
                row.update_progress = 0
                row.update_status = '未检查'
                row.last_error = ''
                row.is_updating = False
                row.save()

            self._set_update_status(row, status='检查更新中', progress=0, is_updating=True, last_error='')
            interval = int(self.simc_config.get('update_check_interval_seconds', 1800) or 1800)
            if row.last_checked_at and (now - row.last_checked_at).total_seconds() < interval:
                if row.simc_path:
                    settings.SIMC_CONFIG['simc_path'] = row.simc_path
                    self.simc_path = row.simc_path
                mins = max(1, int(interval / 60))
                self._set_update_status(row, status=f'跳过检查（{mins}分钟内）', progress=100, is_updating=False)
                return

            simc_path = str(row.simc_path or '').strip()
            simc_missing = (not simc_path) or (not os.path.isfile(simc_path))
            if simc_missing:
                try:
                    if self._try_install_from_local_archive(row, platform, base_dir):
                        return
                except Exception as e:
                    logger.error(f"[SimC Monitor] Local archive install failed: {str(e)}")

            phase = 'fetch_latest'
            try:
                latest = self._fetch_latest_nightly(platform)
            except Exception:
                try:
                    if self._try_install_from_local_archive(row, platform, base_dir):
                        return
                except Exception as e:
                    logger.error(f"[SimC Monitor] Local archive install failed: {str(e)}")
                raise
            row.last_checked_at = now
            row.save(update_fields=['last_checked_at'])
            if not latest:
                self._set_update_status(row, status='未获取到最新版本信息', progress=0, is_updating=False, last_error='下载站返回为空')
                return

            latest_ver = str(latest.get('version') or '').strip()
            known_file_name = latest.get('file_name')
            self._set_update_status(row, latest_version=latest_ver)
            current_ver = str(row.current_version or '').strip()
            need_update = bool(latest_ver) and latest_ver != current_ver
            if bool(latest_ver) and simc_missing:
                need_update = True

            if not need_update:
                if row.simc_path:
                    settings.SIMC_CONFIG['simc_path'] = row.simc_path
                    self.simc_path = row.simc_path
                self._set_update_status(row, status=f'已是最新版本 {latest_ver}', progress=100, is_updating=False)
                return

            if not row.auto_update:
                self._set_update_status(row, status=f'检测到新版本 {latest_ver}，但自动更新已关闭', progress=0, is_updating=False)
                return

            archive_path = os.path.join(base_dir, latest['file_name'])
            extract_dir = os.path.join(base_dir, f"simc-{latest_ver}-{platform}")
            is_7z = str(archive_path).lower().endswith('.7z')
            if is_7z and not self._has_7z_extractor(platform):
                raise Exception("检测到 nightly 包为 .7z，但当前环境没有可用解压器（建议安装 7-Zip 或 py7zr）")

            if simc_missing and (not current_ver or current_ver == latest_ver):
                self._set_update_status(row, status=f'SimC不可用，准备重新安装 {latest_ver}', progress=1, is_updating=True)
            else:
                self._set_update_status(row, status=f'准备下载 {latest_ver}', progress=1, is_updating=True)
            if os.path.exists(archive_path) and not self._validate_archive(archive_path, platform):
                self._safe_remove_file(archive_path)
                self._safe_remove_file(archive_path + '.part')

            if not os.path.exists(archive_path):
                last_logged = {'percent': -5}
                meter = {
                    'start_ts': time.time(),
                    'last_ts': None,
                    'last_bytes': 0,
                    'ema_bps': None,
                }
                def _on_progress(downloaded, total, percent):
                    # 下载阶段映射到 1-80%
                    mapped = 1 + int(percent * 79 / 100)
                    mb_done = downloaded / (1024 * 1024) if downloaded else 0
                    mb_total = total / (1024 * 1024) if total else 0
                    now_ts = time.time()
                    speed = ''
                    eta = ''
                    if total and downloaded:
                        if meter['last_ts'] is not None:
                            dt = max(0.001, now_ts - meter['last_ts'])
                            db = max(0, downloaded - meter['last_bytes'])
                            inst_bps = db / dt
                            if meter['ema_bps'] is None:
                                meter['ema_bps'] = inst_bps
                            else:
                                meter['ema_bps'] = meter['ema_bps'] * 0.8 + inst_bps * 0.2
                        meter['last_ts'] = now_ts
                        meter['last_bytes'] = downloaded
                        if meter['ema_bps'] and meter['ema_bps'] > 0:
                            speed = self._format_speed_mbps(meter['ema_bps'])
                            remaining = max(0, total - downloaded)
                            eta = self._format_eta(remaining / meter['ema_bps'])
                    core = f"下载中 {percent}% ({mb_done:.1f}MB/{mb_total:.1f}MB)" if total else f"下载中 {mb_done:.1f}MB"
                    extra = ''
                    if speed:
                        extra += f" {speed}"
                    if eta:
                        extra += f" ETA {eta}"
                    status = (core + extra).strip()
                    if percent == 100 or percent >= last_logged['percent'] + 5:
                        last_logged['percent'] = percent
                        logger.info(f"[SimC Monitor] Downloading SimC {latest_ver}: {status}")
                    self._set_update_status(row, status=status, progress=mapped, is_updating=True)
                phase = 'download'
                self._download_archive_with_resilience(row, latest_ver, latest['url'], archive_path, platform, progress_cb=_on_progress)
            else:
                self._set_update_status(row, status='已存在安装包，跳过下载', progress=80, is_updating=True)

            self._set_update_status(row, status='解压中', progress=85, is_updating=True)
            if not os.path.exists(extract_dir):
                self._extract_archive(archive_path, extract_dir, platform)

            self._set_update_status(row, status='定位可执行文件', progress=95, is_updating=True)
            exe_path = self._find_simc_executable(extract_dir, platform)
            if not exe_path:
                raise Exception(f"未在解压目录中找到SimC可执行文件: {extract_dir}")
            self._ensure_executable_permission(exe_path, platform)

            row.simc_path = exe_path
            row.current_version = latest_ver
            row.last_updated_at = now
            row.save(update_fields=['simc_path', 'current_version', 'last_updated_at'])

            settings.SIMC_CONFIG['simc_path'] = exe_path
            self.simc_path = exe_path
            self._set_update_status(row, status=f'更新完成 {latest_ver}', progress=100, is_updating=False, last_error='')
        except Exception as e:
            err_text = str(e)
            if phase in ('fetch_latest', 'download'):
                hint = self._build_manual_download_hint(platform, base_dir, known_file_name)
                err_text = f"{err_text}；{hint}"
                logger.error(f"[SimC Monitor] Manual download hint: {hint}")
            logger.error(f"[SimC Monitor] Failed to update SimC backend binary: {err_text}")
            upsert_system_alert(
                category='SIMC_UPDATE_FAILED',
                subject=platform,
                level=3,
                title='SimC 更新失败',
                content=err_text
            )
            try:
                row = SimcBackendBinary.objects.filter(platform=platform).first()
                if row:
                    self._set_update_status(row, status='更新失败', is_updating=False, last_error=err_text)
            except Exception:
                pass

    def mark_task_failed(self, simc_task, reason, exc=None, overwrite_when_has_error=False):
        """
        将任务标记为失败，并写入可见错误信息。
        """
        try:
            detail = str(reason or "未知错误").strip()
            if exc is not None:
                detail = f"{detail}\n异常信息: {str(exc)}"

            current_result = str(simc_task.result_file or "").strip()
            has_existing_error = bool(current_result) and not current_result.endswith('.html')
            should_overwrite = overwrite_when_has_error or (not has_existing_error)
            if should_overwrite:
                simc_task.result_file = detail

            if exc is not None:
                self.save_simc_error_details(
                    simc_task,
                    summary=str(reason or "任务失败").strip(),
                    stderr_text=str(exc)
                )

            simc_task.current_status = 3
            simc_task.save()
        except Exception as save_err:
            logger.error(f"[SimC Monitor] Failed to persist task error for task {getattr(simc_task, 'id', '-')}: {save_err}")

    def clear_simc_error_details(self, simc_task):
        """
        清理任务中的历史SimC错误详情，避免重跑后显示旧错误。
        """
        try:
            payload = self.parse_task_ext(simc_task.ext)
            if not isinstance(payload, dict) or not payload:
                return
            changed = False
            for key in ('simc_error_summary', 'simc_error_native', 'simc_error_code'):
                if key in payload:
                    payload.pop(key, None)
                    changed = True
            if changed:
                simc_task.ext = json.dumps(payload, ensure_ascii=False) if payload else ''
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to clear simc error details for task {getattr(simc_task, 'id', '-')}: {e}")

    def save_simc_error_details(self, simc_task, summary, return_code=None, stderr_text=None, stdout_text=None):
        """
        保存缩略错误 + 原生错误到任务ext，便于前端日志查看。
        """
        try:
            payload = self.parse_task_ext(simc_task.ext)
            if not isinstance(payload, dict):
                payload = {}

            payload['simc_error_summary'] = str(summary or '').strip()[:800]
            if return_code is not None:
                try:
                    payload['simc_error_code'] = int(return_code)
                except Exception:
                    payload['simc_error_code'] = str(return_code)

            native_parts = []
            if return_code is not None:
                native_parts.append(f"returncode: {return_code}")
            if stderr_text:
                native_parts.append("stderr:")
                native_parts.append(str(stderr_text))
            if stdout_text:
                native_parts.append("stdout:")
                native_parts.append(str(stdout_text))
            native_text = '\n'.join(native_parts).strip()
            if native_text:
                payload['simc_error_native'] = native_text

            serialized = json.dumps(payload, ensure_ascii=False)
            # ext 字段上限5000，循环裁剪原生日志以保证可落库
            if len(serialized) > 4800:
                native_value = str(payload.get('simc_error_native') or '')
                while len(serialized) > 4800 and native_value:
                    native_value = native_value[:max(0, len(native_value) - 400)]
                    payload['simc_error_native'] = native_value + '\n...(原生错误已截断)' if native_value else '(原生错误过长，已截断)'
                    serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload.pop('simc_error_native', None)
                serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload['simc_error_summary'] = str(payload.get('simc_error_summary') or '')[:200]
                serialized = json.dumps(payload, ensure_ascii=False)
            if len(serialized) > 5000:
                payload = {'simc_error_summary': str(summary or '')[:200]}
                if return_code is not None:
                    payload['simc_error_code'] = return_code
                serialized = json.dumps(payload, ensure_ascii=False)
            simc_task.ext = serialized
        except Exception as e:
            logger.warning(f"[SimC Monitor] Failed to save native error details for task {getattr(simc_task, 'id', '-')}: {e}")

    def fail_pending_tasks(self, reason):
        """
        当运行前置条件不满足时，为待执行任务写入失败原因，避免前端无日志可看。
        """
        try:
            pending_tasks = SimcTask.objects.filter(is_active=True, current_status=0)
            for task in pending_tasks:
                self.mark_task_failed(task, reason)
        except Exception as e:
            logger.error(f"[SimC Monitor] Failed to mark pending tasks as failed: {e}")

    def scan(self, url=None):
        """
        执行SimC模拟扫描
        :param url: 可选参数，这里不使用
        :return:
        """
        logger.info("[SimC Monitor] Start SimC simulation check.")
        
        try:
            self.ensure_simc_backend_up_to_date()
            # 检查SimC路径是否正确
            if not self.simc_path:
                logger.error(f"[SimC Monitor] SimC path not configured")
                self.fail_pending_tasks("SimC路径未配置，请检查系统配置")
                return False
            if not os.path.exists(self.simc_path):
                logger.error(f"[SimC Monitor] SimC executable not found at path: {self.simc_path}")
                self.fail_pending_tasks(f"SimC可执行文件不存在: {self.simc_path}")
                return False
            if not os.path.isfile(self.simc_path):
                logger.error(f"[SimC Monitor] SimC path is not a file: {self.simc_path}")
                self.fail_pending_tasks(f"SimC路径不是文件: {self.simc_path}")
                return False

            # 获取所有活跃的SimC任务
            simc_tasks = SimcTask.objects.filter(is_active=True, current_status=0)
            
            for task in simc_tasks:
                logger.info(f"[SimC Monitor] Processing task: {task.name} (ID: {task.id})")
                self.process_simc_task(task)
                
        except Exception as e:
            logger.error(f"[SimC Monitor] Error during SimC simulation: {str(e)}")
            self.fail_pending_tasks("SimC调度进程异常，请稍后重试")
            return False
            
        return True

    def process_simc_task(self, simc_task):
        """
        处理单个SimC任务
        :param simc_task: SimcTask对象
        :return:
        """
        try:
            # 更新任务状态为进行中
            simc_task.current_status = 1
            self.clear_simc_error_details(simc_task)
            simc_task.save()
            
            # 获取SimC配置
            simc_profile = SimcProfile.objects.filter(
                id=simc_task.simc_profile_id,
                user_id=simc_task.user_id,
                is_active=True
            ).first()
            
            if not simc_profile:
                logger.error(f"[SimC Monitor] SimC profile not found for task {simc_task.id}")
                self.mark_task_failed(simc_task, "未找到对应的SimC配置，可能已被删除或禁用")
                return False
            
            # 根据任务类型选择处理方式
            if simc_task.task_type == 2:  # 属性模拟
                return self.process_attribute_simulation(simc_task, simc_profile)
            else:  # 常规模拟
                return self.process_regular_simulation(simc_task, simc_profile)
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error processing task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "任务处理失败", e)
            return False
        
        return True
    
    def process_regular_simulation(self, simc_task, simc_profile):
        """
        处理常规模拟任务
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象
        :return: 执行是否成功
        """
        try:
            ext_payload = self.parse_task_ext(simc_task.ext)
            override_time = ext_payload.get('regular_time')
            override_target_count = ext_payload.get('regular_target_count')
            override_action_list = ext_payload.get('override_action_list')
            logger.info(
                f"[SimC Monitor] Regular overrides for task {simc_task.id}: "
                f"time={override_time}, targets={override_target_count}"
            )

            # 生成SimC代码
            simc_code = self.generate_simc_code(
                simc_profile,
                simc_task.result_file,
                override_time=override_time,
                override_target_count=override_target_count,
                override_action_list=override_action_list
            )
            if not isinstance(simc_code, str) or not simc_code.strip():
                raise Exception("生成SimC配置失败：模板渲染结果为空")
            
            # 创建临时SimC文件
            simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}.simc")
            
            with open(simc_file_path, 'w', encoding='utf-8') as f:
                f.write(simc_code)
            
            # 执行SimC命令
            success = self.execute_simc_command(simc_file_path, simc_task)
            
            # 清理临时文件
            if os.path.exists(simc_file_path):
                os.remove(simc_file_path)
            
            # 更新任务状态
            if success:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Regular simulation task {simc_task.id} completed successfully")
            else:
                simc_task.current_status = 3  # 失败
                logger.error(f"[SimC Monitor] Regular simulation task {simc_task.id} failed")
            
            simc_task.save()
            return success
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in regular simulation for task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "常规模拟执行异常", e)
            return False
    
    def process_attribute_simulation(self, simc_task, simc_profile):
        """
        处理属性模拟任务
        :param simc_task: SimcTask对象
        :param simc_profile: SimcProfile对象
        :return: 执行是否成功
        """
        try:
            ext_payload = self.parse_task_ext(simc_task.ext)
            selected_combination = ext_payload.get('selected_attributes') or simc_task.ext
            step_size = ext_payload.get('attribute_step') or 50
            try:
                step_size = max(1, int(step_size))
            except Exception:
                step_size = 50

            # 解析属性组合
            selected_attributes = self.parse_selected_attributes(selected_combination)
            if len(selected_attributes) != 2:
                logger.error(f"[SimC Monitor] Attribute simulation requires exactly 2 attributes, got {len(selected_attributes)} for task {simc_task.id}")
                self.mark_task_failed(simc_task, f"属性模拟参数错误：需要2个属性，当前为{len(selected_attributes)}个")
                return False
            
            # 获取基础属性值
            base_attributes = self.get_base_attributes(simc_profile)
            
            # 获取两个属性及其总和
            attr1, attr2 = selected_attributes[0], selected_attributes[1]
            attr1_base = base_attributes[attr1]
            attr2_base = base_attributes[attr2]
            total_value = attr1_base + attr2_base
            
            logger.info(f"[SimC Monitor] Starting attribute simulation for {attr1} and {attr2}, total: {total_value}, task {simc_task.id}")
            
            # 执行分阶段模拟
            result_files = []
            stage = 0
            
            # 以可配置步长进行分配模拟，从attr1=0到attr1=total_value
            # 生成所有需要测试的步长点，确保包含0和total_value
            test_points = list(range(0, total_value, step_size))
            if total_value not in test_points:
                test_points.append(total_value)
            
            for attr1_value in test_points:
                attr2_value = total_value - attr1_value
                
                stage_result_file = f"{simc_task.id}_{attr1}_{attr1_value}_{attr2}_{attr2_value}.html"
                
                # 生成当前阶段的SimC代码
                modified_attributes = base_attributes.copy()
                modified_attributes[attr1] = attr1_value
                modified_attributes[attr2] = attr2_value
                
                simc_code = self.generate_attribute_simc_code(simc_profile, modified_attributes, stage_result_file)
                if not isinstance(simc_code, str) or not simc_code.strip():
                    raise Exception(f"生成属性模拟配置失败：stage={stage}")
                
                # 创建临时SimC文件
                simc_file_path = os.path.join(self.result_path, f"temp_{simc_task.id}_{stage}.simc")
                
                with open(simc_file_path, 'w', encoding='utf-8') as f:
                    f.write(simc_code)
                
                # 执行SimC命令
                success = self.execute_simc_command(simc_file_path, simc_task, stage_result_file)
                
                # 清理临时文件
                if os.path.exists(simc_file_path):
                    os.remove(simc_file_path)
                
                if success:
                    result_files.append(stage_result_file)
                    logger.info(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) completed for task {simc_task.id}")
                else:
                    logger.error(f"[SimC Monitor] Stage {stage} ({attr1}:{attr1_value}, {attr2}:{attr2_value}) failed for task {simc_task.id}")
                
                stage += 1
            
            # 保存所有结果文件名（以逗号分割）
            simc_task.result_file = ','.join(result_files)
            
            # 更新任务状态
            if result_files:
                simc_task.current_status = 2  # 完成
                logger.info(f"[SimC Monitor] Attribute simulation task {simc_task.id} completed with {len(result_files)} result files")
            else:
                self.mark_task_failed(simc_task, "属性模拟未生成任何结果文件")
                logger.error(f"[SimC Monitor] Attribute simulation task {simc_task.id} failed - no results generated")
            
            if simc_task.current_status == 2:
                simc_task.save()
            return len(result_files) > 0
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error in attribute simulation for task {simc_task.id}: {str(e)}")
            self.mark_task_failed(simc_task, "属性模拟执行异常", e)
            return False

    def generate_simc_code(self, profile, result_file, override_time=None, override_target_count=None, override_action_list=None):
        """
        生成SimC代码
        :param profile: SimcProfile对象
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 从数据库获取模板
            from botend.models import SimcTemplate
            template_obj = self.select_template_by_spec(profile.spec)
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = template_obj.template_content
            return self.apply_template(
                template=template,
                profile=profile,
                result_file=result_file,
                attributes=None,
                override_time=override_time,
                override_target_count=override_target_count,
                override_action_list=override_action_list
            )
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating SimC code: {str(e)}")
            raise e

    def execute_simc_command(self, simc_file_path, simc_task, result_file_name=None):
        """
        执行SimC命令
        :param simc_file_path: SimC文件路径
        :param simc_task: SimcTask对象
        :param result_file_name: 自定义结果文件名（可选）
        :return: 执行是否成功
        """
        try:
            
            # 构建命令
            cmd = [self.simc_path, simc_file_path]
            
            logger.info(f"[SimC Monitor] Executing command: {' '.join(cmd)}")
            
            # 执行命令
            result = subprocess.run(
                cmd,
                cwd=self.result_path,
                capture_output=True,
                text=True,
                timeout=300  # 5分钟超时
            )
            
            if result.returncode == 0:
                logger.info(f"[SimC Monitor] SimC execution successful for task {simc_task.id}")
                if result.stdout:
                    logger.debug(f"[SimC Monitor] SimC output: {result.stdout[:500]}...")  # 只记录前500字符
                
                # 上传结果文件到OSS
                # 使用自定义结果文件名或默认的任务结果文件名
                target_result_file = result_file_name if result_file_name else simc_task.result_file
                result_file_path = os.path.join(self.result_path, target_result_file)
                if os.path.exists(result_file_path):
                    from botend.interface.ossupload import ossUpload
                    try:
                        upload_success = ossUpload(result_file_path)
                        if upload_success:
                            logger.info(f"[SimC Monitor] Result file {target_result_file} uploaded to OSS successfully for task {simc_task.id}")
                        else:
                            logger.error(f"[SimC Monitor] Failed to upload result file {target_result_file} to OSS for task {simc_task.id}")
                    except Exception as e:
                        logger.error(f"[SimC Monitor] Error uploading result file to OSS: {str(e)}")
                else:
                    logger.warning(f"[SimC Monitor] Result file not found: {result_file_path}")
                
                return True
            else:
                logger.error(f"[SimC Monitor] SimC execution failed for task {simc_task.id}")
                logger.error(f"[SimC Monitor] Return code: {result.returncode}")
                
                # 构建错误信息并直接存储到result_file字段
                error_info = f"SimC执行失败\n返回码: {result.returncode}\n"
                if result.stderr:
                    logger.error(f"[SimC Monitor] Error output: {result.stderr}")
                    error_info += f"错误输出: {result.stderr}\n"
                if result.stdout:
                    error_info += f"标准输出: {result.stdout}\n"
                
                # 直接将错误信息存储到result_file字段
                simc_task.result_file = error_info
                self.save_simc_error_details(
                    simc_task,
                    summary=f"SimC执行失败（返回码: {result.returncode}）",
                    return_code=result.returncode,
                    stderr_text=result.stderr,
                    stdout_text=result.stdout
                )
                simc_task.save()
                return False
                
        except subprocess.TimeoutExpired:
            error_info = f"SimC执行超时\n任务ID: {simc_task.id}\n超时时间: 300秒"
            logger.error(f"[SimC Monitor] SimC execution timeout for task {simc_task.id}")
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
            self.save_simc_error_details(
                simc_task,
                summary="SimC执行超时（300秒）"
            )
            simc_task.save()
            return False
        except Exception as e:
            error_info = f"SimC执行异常\n任务ID: {simc_task.id}\n异常信息: {str(e)}"
            logger.error(f"[SimC Monitor] Error executing SimC command: {str(e)}")
            # 直接将错误信息存储到result_file字段
            simc_task.result_file = error_info
            self.save_simc_error_details(
                simc_task,
                summary="SimC执行异常",
                stderr_text=str(e)
            )
            simc_task.save()
            return False


    def check_status(self, result):
        """
        检查请求状态
        :param result: 请求结果
        :return: 状态检查结果
        """
        return True

    def resolve_data(self, result):
        """
        处理返回的内容
        :param result: 返回内容
        :return: 处理结果
        """
        return True

    def trigger_webhook(self):
        """
        触发webhook
        :return: 触发结果
        """
        return True
    
    def parse_selected_attributes(self, ext_data):
        """
        解析选中的属性组合
        :param ext_data: 扩展信息字符串，单个属性组合名称（如'crit_versatility'）
        :return: 选中的属性列表
        """
        try:
            if not ext_data:
                return []
            
            # ext_data是单个属性组合字符串，如"crit_versatility"
            combination = ext_data.strip()
            
            # 属性组合映射
            combination_map = {
                'crit_mastery': ['gear_crit', 'gear_mastery'],
                'crit_haste': ['gear_crit', 'gear_haste'],
                'crit_versatility': ['gear_crit', 'gear_versatility'],
                'mastery_haste': ['gear_mastery', 'gear_haste'],
                'mastery_versatility': ['gear_mastery', 'gear_versatility'],
                'haste_versatility': ['gear_haste', 'gear_versatility'],
                'haste_mastery': ['gear_haste', 'gear_mastery']
            }
            
            # 获取选中的属性
            if combination in combination_map:
                return combination_map[combination]
            else:
                logger.warning(f"[SimC Monitor] Unknown attribute combination: {combination}")
                return []
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error parsing selected attributes: {str(e)}")
            return []

    def parse_task_ext(self, ext_data):
        if not ext_data:
            return {}
        if isinstance(ext_data, dict):
            return ext_data
        text = str(ext_data).strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
            return {}
        except Exception:
            return {'selected_attributes': text}
    
    def get_base_attributes(self, simc_profile):
        """
        获取基础属性值
        :param simc_profile: SimcProfile对象
        :return: 属性字典
        """
        return {
            'gear_strength': simc_profile.gear_strength or 93330,
            'gear_crit': simc_profile.gear_crit or 10730,
            'gear_haste': simc_profile.gear_haste or 18641,
            'gear_mastery': simc_profile.gear_mastery or 21785,
            'gear_versatility': simc_profile.gear_versatility or 6757
        }
    
    def generate_attribute_simc_code(self, profile, attributes, result_file):
        """
        生成属性模拟的SimC代码
        :param profile: SimcProfile对象
        :param attributes: 修改后的属性字典
        :param result_file: 结果文件名
        :return: 生成的SimC代码字符串
        """
        try:
            # 从数据库获取模板
            from botend.models import SimcTemplate
            template_obj = self.select_template_by_spec(profile.spec)
            if not template_obj:
                raise Exception("未找到启用的SimC模板")
            template = template_obj.template_content
            return self.apply_template(
                template=template,
                profile=profile,
                result_file=result_file,
                attributes=attributes
            )
            
        except Exception as e:
            logger.error(f"[SimC Monitor] Error generating attribute SimC code: {str(e)}")
            raise e

    def select_template_by_spec(self, spec):
        from botend.models import SimcTemplate
        active = SimcTemplate.objects.filter(is_active=True).order_by('id')
        if not active.exists():
            return None

        spec_value = str(spec or '').strip().lower()
        if spec_value:
            for tpl in active:
                spec_field = str(getattr(tpl, 'spec', '') or '').strip().lower()
                if not spec_field:
                    continue
                if spec_field == spec_value:
                    return tpl
                candidates = [s.strip() for s in spec_field.split(',') if s.strip()]
                if spec_value in candidates:
                    return tpl

        for tpl in active:
            spec_field = str(getattr(tpl, 'spec', '') or '').strip().lower()
            if not spec_field:
                continue
            candidates = [s.strip() for s in spec_field.split(',') if s.strip()]
            if 'default' in candidates or 'all' in candidates or '*' in candidates:
                return tpl
        return active.first()

    def apply_template(self, template, profile, result_file, attributes=None, override_time=None, override_target_count=None, override_action_list=None):
        attrs = attributes or self.get_base_attributes(profile)
        normalized_template = str(template or '')
        if '{time}' not in normalized_template:
            if re.search(r'^\s*max_time\s*=.*$', normalized_template, flags=re.MULTILINE):
                normalized_template = re.sub(r'^\s*max_time\s*=.*$', 'max_time={time}', normalized_template, flags=re.MULTILINE)
            else:
                normalized_template += '\nmax_time={time}'
            logger.warning('[SimC Monitor] 模板缺少 {time} 占位符，已自动规范为 max_time={time}')
        if '{target_count}' not in normalized_template:
            if re.search(r'^\s*desired_targets\s*=.*$', normalized_template, flags=re.MULTILINE):
                normalized_template = re.sub(r'^\s*desired_targets\s*=.*$', 'desired_targets={target_count}', normalized_template, flags=re.MULTILINE)
            else:
                normalized_template += '\ndesired_targets={target_count}'
            logger.warning('[SimC Monitor] 模板缺少 {target_count} 占位符，已自动规范为 desired_targets={target_count}')

        simc_code = normalized_template
        fight_style = profile.fight_style or 'Patchwerk'
        max_time = override_time if override_time not in (None, '') else profile.time
        target_count = override_target_count if override_target_count not in (None, '') else profile.target_count
        spec_value = str(getattr(profile, 'spec', '') or '').strip() or 'fury'

        simc_code = simc_code.replace('{fight_style}', fight_style)
        simc_code = simc_code.replace('{time}', str(max_time or 300))
        simc_code = simc_code.replace('{target_count}', str(target_count or 1))
        simc_code = simc_code.replace('{talent}', profile.talent or '')
        final_action_list = override_action_list if override_action_list not in (None, '') else (profile.action_list or '')
        simc_code = simc_code.replace('{action_list}', final_action_list)
        simc_code = simc_code.replace('{spec}', spec_value)
        simc_code = simc_code.replace('{gear_strength}', str(attrs['gear_strength']))
        simc_code = simc_code.replace('{gear_crit}', str(attrs['gear_crit']))
        simc_code = simc_code.replace('{gear_haste}', str(attrs['gear_haste']))
        simc_code = simc_code.replace('{gear_mastery}', str(attrs['gear_mastery']))
        simc_code = simc_code.replace('{gear_versatility}', str(attrs['gear_versatility']))
        simc_code = simc_code.replace('{result_file}', self.result_path + result_file)

        # 兼容旧模板：未提供 {spec} 占位符时，覆盖或追加 spec 行
        if '{spec}' not in normalized_template:
            if 'spec=' in simc_code:
                simc_code = re.sub(r'^\s*spec\s*=.*$', f"spec={spec_value}", simc_code, flags=re.MULTILINE)
            else:
                simc_code = f"spec={spec_value}\n" + simc_code

        return simc_code

