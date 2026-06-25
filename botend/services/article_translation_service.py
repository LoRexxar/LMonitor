import json
import time
from typing import Any, Callable, Dict, List, Optional, Type
from urllib.parse import urljoin

import requests
from django.conf import settings

from core.glm import GLMClient
from botend.services.article_content_service import TEXT_BLOCK_TYPES, article_blocks_match_reference, blocks_to_plain_text, dumps_blocks, html_block_text_nodes, html_block_translate_texts, loads_blocks, translate_blocks
from utils.log import logger


class GLMTranslationEngine:
    """Default article translation engine backed by core.glm.GLMClient."""

    name = "glm"

    def __init__(self, client: Optional[GLMClient] = None):
        self.client = client or GLMClient()

    @property
    def last_error(self) -> str:
        return getattr(self.client, "last_error", "") or ""

    def available(self) -> bool:
        return bool(getattr(self.client, "api_key", ""))

    def send_message(self, prompt: str, *, max_tokens: int) -> str:
        return self.client.send_message(prompt, max_tokens=max_tokens, thinking_type="disabled") or ""


class CodexTranslationEngine:
    """OpenAI-compatible fallback engine backed by settings.CODEX_API_CONFIG."""

    name = "codex"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config if config is not None else (getattr(settings, "CODEX_API_CONFIG", {}) or {})
        self.last_error = ""

    def available(self) -> bool:
        return bool((self.config.get("api_key") or "").strip() and (self.config.get("base_url") or "").strip())

    def send_message(self, prompt: str, *, max_tokens: int) -> str:
        self.last_error = ""
        if not self.available():
            self.last_error = "CODEX_API_CONFIG 缺少 api_key/base_url"
            return ""
        base_url = (self.config.get("base_url") or "").strip().rstrip("/") + "/"
        url = urljoin(base_url, "v1/chat/completions")
        model = (self.config.get("model") or "gpt-5.5").strip()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是专业的游戏资讯翻译。只输出用户要求的翻译结果，不要添加解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        try:
            resp = requests.post(
                url,
                headers={
                    "Authorization": "Bearer {}".format((self.config.get("api_key") or "").strip()),
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=int(self.config.get("request_timeout_seconds", 90) or 90),
            )
            if resp.status_code >= 400:
                self.last_error = "Codex HTTP bad status={}: {}".format(resp.status_code, (resp.text or "")[:300])
                return ""
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                self.last_error = "Codex response missing choices"
                return ""
            content = ((choices[0].get("message") or {}).get("content") or "").strip()
            if not content:
                self.last_error = "Codex response empty content"
            return content
        except Exception as e:
            self.last_error = "Codex request failed: {}".format(str(e)[:300])
            return ""


class FallbackTranslationEngine:
    name = "fallback"

    def __init__(self, engines: Optional[List[Any]] = None):
        self.engines = engines if engines is not None else [GLMTranslationEngine(), CodexTranslationEngine()]
        self.last_error = ""

    def available(self) -> bool:
        return any(getattr(engine, "available", lambda: False)() for engine in self.engines)

    def send_message(self, prompt: str, *, max_tokens: int) -> str:
        errors = []
        for engine in self.engines:
            if not getattr(engine, "available", lambda: False)():
                errors.append("{} unavailable".format(getattr(engine, "name", engine.__class__.__name__)))
                continue
            try:
                result = engine.send_message(prompt, max_tokens=max_tokens)
            except Exception as e:
                errors.append("{} exception: {}".format(getattr(engine, "name", engine.__class__.__name__), str(e)[:200]))
                continue
            if result:
                self.last_error = ""
                return result
            errors.append("{} empty: {}".format(getattr(engine, "name", engine.__class__.__name__), getattr(engine, "last_error", "")))
        self.last_error = "; ".join([e for e in errors if e])
        return ""


TRANSLATION_ENGINES: Dict[str, Type[Any]] = {
    "glm": GLMTranslationEngine,
    "codex": CodexTranslationEngine,
    "fallback": FallbackTranslationEngine,
}


class ArticleTranslationService:
    """
    Shared article translation service for monitor plugins.

    Keep translation prompts/batching in one place so new engines can be added by
    implementing the same engine interface and registering it in TRANSLATION_ENGINES.
    """

    def __init__(self, engine: Any = None, engine_name: str = "fallback", sleep_func: Callable[[float], None] = time.sleep):
        self.engine = engine if engine is not None else build_translation_engine(engine_name)
        self.sleep_func = sleep_func

    def available(self) -> bool:
        return bool(self.engine and getattr(self.engine, "available", lambda: False)())

    def has_engine(self) -> bool:
        return self.available()

    @property
    def last_error(self) -> str:
        return getattr(self.engine, "last_error", "") or ""

    def translate_title(self, title: str) -> str:
        title = (title or "").strip()
        if not title or not self.available():
            return ""
        prompt = f"请将以下英文标题翻译成中文，只返回翻译结果，不要添加任何解释：\n\n{title}"
        result = self.engine.send_message(prompt, max_tokens=200)
        return (result or "").strip().strip('"').strip("'")

    def translate_content(self, content: str) -> str:
        content = (content or "").strip()
        if not content or not self.available():
            return ""
        paragraphs = [p.strip() for p in content.split("\n") if p and p.strip()]
        if not paragraphs:
            return ""

        translated_pairs = []
        i = 0
        while i < len(paragraphs):
            batch = []
            total = 0
            while i < len(paragraphs) and len(batch) < 10:
                p = paragraphs[i]
                if batch and (total + len(p) > 4000):
                    break
                batch.append(p)
                total += len(p)
                i += 1

            prompt = (
                "请把下面 JSON 数组中的每个英文字符串翻译成中文，保持数组长度与顺序一致。"
                "仅输出 JSON 数组（不要输出其它文字/解释/Markdown）。\n\n"
                f"输入JSON：\n{json.dumps(batch, ensure_ascii=False)}"
            )
            result = self.engine.send_message(prompt, max_tokens=2400)
            translated_list = None
            if result:
                try:
                    translated_list = json.loads(result)
                except Exception:
                    translated_list = None
            if not isinstance(translated_list, list):
                translated_list = [t.strip() for t in (result or "").splitlines() if t.strip()]

            for j, orig in enumerate(batch):
                trans = ""
                if j < len(translated_list) and isinstance(translated_list[j], str):
                    trans = translated_list[j].strip()
                translated_pairs.append({"original": orig, "translated": trans})

            self.sleep_func(0.6)

        return json.dumps(translated_pairs, ensure_ascii=False)

    def translate_content_blocks(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        source_blocks = [dict(b) for b in (blocks or []) if isinstance(b, dict)]
        if not source_blocks or not self.available():
            return []

        text_items = self._block_text_items(source_blocks)

        if not text_items:
            return source_blocks

        translated_by_index = {}
        i = 0
        while i < len(text_items):
            batch = []
            batch_indexes = []
            total = 0
            while i < len(text_items) and len(batch) < 10:
                item_index, _block_index, text = text_items[i]
                if batch and (total + len(text) > 4000):
                    break
                batch.append(text)
                batch_indexes.append(item_index)
                total += len(text)
                i += 1

            prompt = (
                "请把下面 JSON 数组中的每个英文字符串翻译成中文，保持数组长度与顺序一致。"
                "仅输出 JSON 数组（不要输出其它文字/解释/Markdown）。\n\n"
                f"输入JSON：\n{json.dumps(batch, ensure_ascii=False)}"
            )
            result = self.engine.send_message(prompt, max_tokens=3000)
            translated_list = None
            if result:
                try:
                    translated_list = json.loads(result)
                except Exception:
                    translated_list = None
            if not isinstance(translated_list, list):
                translated_list = [t.strip() for t in (result or "").splitlines() if t.strip()]

            for j, block_index in enumerate(batch_indexes):
                if j < len(translated_list) and isinstance(translated_list[j], str):
                    translated = translated_list[j].strip()
                    if translated:
                        translated_by_index[block_index] = translated

            self.sleep_func(0.6)

        result_blocks = []
        for index, block in enumerate(source_blocks):
            new_block = dict(block)
            if block.get("type") == "html":
                translations = [translated_by_index[item_index] for item_index, _block_index, _text in text_items if _block_index == index and item_index in translated_by_index]
                new_block = html_block_translate_texts(block, {}, translations)
            elif index in translated_by_index:
                new_block["original"] = (block.get("text") or "").strip()
                new_block["text"] = translated_by_index[index]
            result_blocks.append(new_block)
        return result_blocks

    def _block_text_items(self, blocks: List[Dict[str, Any]]) -> List[Any]:
        text_items = []
        for block_index, block in enumerate(blocks):
            if block.get("type") in TEXT_BLOCK_TYPES:
                text = (block.get("text") or "").strip()
                if text:
                    text_items.append((block_index, block_index, text))
            elif block.get("type") == "html":
                for text in html_block_text_nodes(block.get("html") or ""):
                    text_items.append((len(text_items), block_index, text))
        return text_items

    def _blocks_look_polluted(self, source_blocks: List[Dict[str, Any]], translated_blocks: List[Dict[str, Any]]) -> bool:
        source_text = blocks_to_plain_text(source_blocks).lower()
        translated_text = blocks_to_plain_text(translated_blocks).lower()
        if not translated_text:
            return False
        for marker in ["pulverize"]:
            source_count = source_text.count(marker)
            translated_count = translated_text.count(marker)
            if translated_count >= max(20, source_count * 10 + 20):
                return True
        return False

    def _needs_content_blocks_translation(self, source_blocks: List[Dict[str, Any]], translated_blocks: List[Dict[str, Any]]) -> bool:
        if not source_blocks:
            return False
        if not translated_blocks or len(translated_blocks) != len(source_blocks):
            return True
        source_text_len = len(blocks_to_plain_text(source_blocks))
        translated_text_len = len(blocks_to_plain_text(translated_blocks))
        if source_text_len >= 1000 and translated_text_len < max(300, int(source_text_len * 0.4)):
            return True
        return False

    def translate_article_fields(self, article, logger_prefix: str = "ArticleTranslationService") -> bool:
        """Translate missing title/content fields and save each field independently."""
        if not self.available():
            return False

        any_translated = False
        article_id = getattr(article, "id", None)
        url = getattr(article, "url", "")

        if (getattr(article, "title", "") or "").strip() and not (getattr(article, "title_cn", "") or "").strip():
            try:
                title_cn = self.translate_title(article.title)
                if title_cn:
                    article.title_cn = title_cn
                    article.save(update_fields=["title_cn"])
                    any_translated = True
                else:
                    logger.warning(
                        f"[{logger_prefix}] title translate returned empty for article_id={article_id} url={url}; engine_error={self.last_error}"
                    )
            except Exception as e:
                logger.error(
                    f"[{logger_prefix}] title translate exception for article_id={article_id} url={url}: {e}; engine_error={self.last_error}"
                )

        content = (getattr(article, "content", "") or "").strip()
        blocks = loads_blocks(getattr(article, "content_blocks", "") or "")
        translated_blocks = loads_blocks(getattr(article, "content_blocks_cn", "") or "")
        blocks_are_valid = (
            blocks
            and hasattr(article, "content_blocks_cn")
            and article_blocks_match_reference(
                blocks,
                reference_text=content,
                reference_title=getattr(article, "title", "") or "",
            )
        )
        needs_content_cn = content and not (getattr(article, "content_cn", "") or "").strip()
        needs_blocks_cn = blocks_are_valid and self._needs_content_blocks_translation(blocks, translated_blocks)

        if needs_content_cn or needs_blocks_cn:
            try:
                update_fields = []
                content_cn = getattr(article, "content_cn", "") or ""
                if needs_content_cn:
                    content_cn = self.translate_content(article.content)
                    if content_cn:
                        article.content_cn = content_cn
                        update_fields.append("content_cn")
                    else:
                        logger.warning(
                            f"[{logger_prefix}] content translate returned empty for article_id={article_id} url={url}; engine_error={self.last_error}"
                        )

                if needs_blocks_cn:
                    new_blocks = self.translate_content_blocks(blocks)
                    if not new_blocks and content_cn:
                        new_blocks = translate_blocks(blocks, content_cn)
                    if new_blocks and self._blocks_look_polluted(blocks, new_blocks):
                        logger.warning(
                            f"[{logger_prefix}] skip polluted content blocks translation for article_id={article_id} url={url}; engine_error={self.last_error}"
                        )
                        new_blocks = []
                    if new_blocks:
                        article.content_blocks_cn = dumps_blocks(new_blocks)
                        update_fields.append("content_blocks_cn")
                    else:
                        logger.warning(
                            f"[{logger_prefix}] content blocks translate returned empty for article_id={article_id} url={url}; engine_error={self.last_error}"
                        )

                if update_fields:
                    article.save(update_fields=update_fields)
                    any_translated = True
            except Exception as e:
                logger.error(
                    f"[{logger_prefix}] content translate exception for article_id={article_id} url={url}: {e}; engine_error={self.last_error}"
                )

        return any_translated


def build_translation_engine(engine_name: str = "fallback"):
    engine_cls = TRANSLATION_ENGINES.get(engine_name or "fallback")
    if not engine_cls:
        raise ValueError(f"Unknown translation engine: {engine_name}")
    return engine_cls()


def build_translation_service(engine_name: str = "fallback", engine: Any = None, **kwargs) -> ArticleTranslationService:
    return ArticleTranslationService(engine=engine, engine_name=engine_name, **kwargs)
