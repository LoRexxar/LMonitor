import json
import time
from typing import Any, Callable, Dict, Optional, Type

from core.glm import GLMClient
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


TRANSLATION_ENGINES: Dict[str, Type[GLMTranslationEngine]] = {
    "glm": GLMTranslationEngine,
}


class ArticleTranslationService:
    """
    Shared article translation service for monitor plugins.

    Keep translation prompts/batching in one place so new engines can be added by
    implementing the same engine interface and registering it in TRANSLATION_ENGINES.
    """

    def __init__(self, engine: Any = None, engine_name: str = "glm", sleep_func: Callable[[float], None] = time.sleep):
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
                if len(p) > 2000:
                    p = p[:2000]
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

        if (getattr(article, "content", "") or "").strip() and not (getattr(article, "content_cn", "") or "").strip():
            try:
                content_cn = self.translate_content(article.content)
                if content_cn:
                    article.content_cn = content_cn
                    article.save(update_fields=["content_cn"])
                    any_translated = True
                else:
                    logger.warning(
                        f"[{logger_prefix}] content translate returned empty for article_id={article_id} url={url}; engine_error={self.last_error}"
                    )
            except Exception as e:
                logger.error(
                    f"[{logger_prefix}] content translate exception for article_id={article_id} url={url}: {e}; engine_error={self.last_error}"
                )

        return any_translated


def build_translation_engine(engine_name: str = "glm"):
    engine_cls = TRANSLATION_ENGINES.get(engine_name or "glm")
    if not engine_cls:
        raise ValueError(f"Unknown translation engine: {engine_name}")
    return engine_cls()


def build_translation_service(engine_name: str = "glm", engine: Any = None, **kwargs) -> ArticleTranslationService:
    return ArticleTranslationService(engine=engine, engine_name=engine_name, **kwargs)
