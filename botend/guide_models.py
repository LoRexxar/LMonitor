"""职业攻略的文章、修订和同步记录。"""

from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from functools import reduce
from operator import or_
from botend.constants.wow import SPEC_IDENTITY_MAP, CLASS_CN, SPEC_CN, resolve_spec_identity


class ClassGuideTag(models.Model):
    name = models.CharField('标签', max_length=60, unique=True)
    disclaimer = models.TextField('统一免责声明', blank=True, default='', max_length=3000)

    class Meta:
        verbose_name = '攻略标签'
        verbose_name_plural = '攻略标签'
        ordering = ['name']


class ClassGuide(models.Model):
    title = models.CharField('标题', max_length=255)
    slug = models.SlugField('标识', max_length=200)
    class_name = models.CharField('职业', max_length=32)
    spec_name = models.CharField('专精', max_length=32)
    spec_id = models.PositiveIntegerField('专精编号', db_index=True)
    game_version = models.CharField('游戏版本', max_length=64)
    guide_type = models.CharField('攻略类型', max_length=40, default='raid')
    tags = models.ManyToManyField(ClassGuideTag, blank=True, related_name='guides', verbose_name='标签')
    source_url = models.URLField('来源', max_length=1000, blank=True)
    author = models.CharField('原作者', max_length=200, blank=True)
    source_author_profile = models.JSONField('来源作者资料', default=dict, blank=True)
    author_profile = models.JSONField('自定义作者资料', null=True, blank=True, default=None)
    archived = models.BooleanField('已归档', default=False)
    revision_number = models.PositiveIntegerField('修订序号', default=0)
    published_revision = models.ForeignKey('ClassGuideRevision', null=True, blank=True,
        on_delete=models.PROTECT, related_name='+', verbose_name='已审核版本')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = '职业攻略'
        verbose_name_plural = '职业攻略'
        constraints = [models.UniqueConstraint(fields=['slug', 'game_version'], name='guide_slug_version_unique'),
            models.CheckConstraint(condition=reduce(or_, (models.Q(spec_id=key, class_name=pair[0], spec_name=pair[1])
                for key, pair in SPEC_IDENTITY_MAP.items())), name='guide_spec_identity_valid')]
        indexes = [models.Index(fields=['class_name', 'spec_name', 'game_version', 'guide_type'], name='guide_catalog_idx')]

    def normalize_specialization(self):
        try:
            self.spec_id, self.class_name, self.spec_name = resolve_spec_identity(self.spec_id, self.class_name, self.spec_name)
        except ValueError as exc:
            raise ValidationError({'spec_id': str(exc)}) from exc

    def full_clean(self, *args, **kwargs):
        self.normalize_specialization()
        return super().full_clean(*args, **kwargs)

    def save(self, *args, **kwargs):
        self.normalize_specialization()
        if kwargs.get('update_fields') and {'spec_id', 'class_name', 'spec_name'} & set(kwargs['update_fields']):
            kwargs['update_fields'] = set(kwargs['update_fields']) | {'spec_id', 'class_name', 'spec_name'}
        return super().save(*args, **kwargs)

    @property
    def specialization_label(self):
        return f'{CLASS_CN[self.class_name]} · {SPEC_CN[self.spec_name]}'


class ClassGuideRevision(models.Model):
    guide = models.ForeignKey(ClassGuide, on_delete=models.PROTECT, related_name='revisions')
    number = models.PositiveIntegerField('序号')
    origin = models.CharField('来源类型', max_length=20, default='manual')
    title = models.CharField('修订标题', max_length=255)
    content_markdown = models.TextField('Markdown 正文', blank=True)
    source_markdown = models.TextField('原文 Markdown', blank=True)
    blocks = models.JSONField('中文内容块', default=list)
    source_blocks = models.JSONField('原文内容块', default=list)
    source_payload = models.JSONField('原始来源快照', default=dict)
    source_hash = models.CharField('来源指纹', max_length=64, blank=True, db_index=True)
    source_modified = models.CharField('来源更新时间', max_length=80, blank=True)
    audit = models.JSONField('完整性审核', default=dict)
    note = models.CharField('修订说明', max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '攻略修订'
        verbose_name_plural = '攻略修订'
        ordering = ['-number']
        constraints = [models.UniqueConstraint(fields=['guide', 'number'], name='guide_revision_number_unique')]


class ClassGuideFeed(models.Model):
    key = models.CharField(max_length=64, unique=True, default='maxroll')
    enabled = models.BooleanField('启用监控', default=False)
    interval_minutes = models.PositiveIntegerField('检查间隔（分钟）', default=360)
    authorization_note = models.TextField('授权说明', blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = '攻略同步设置'
        verbose_name_plural = '攻略同步设置'


class ClassGuideSyncRun(models.Model):
    status = models.CharField('状态', max_length=24, default='running')
    discovered = models.JSONField('目录清单', default=list)
    results = models.JSONField('逐篇结果', default=list)
    coverage = models.JSONField('覆盖率', default=dict)
    error = models.TextField('错误', blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = '攻略同步批次'
        verbose_name_plural = '攻略同步批次'
        ordering = ['-id']


class ClassGuideTranslation(models.Model):
    key = models.CharField(max_length=64, unique=True)
    source = models.TextField('原文')
    translated = models.TextField('译文')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = '攻略翻译缓存'
        verbose_name_plural = '攻略翻译缓存'


class ClassGuideTerm(models.Model):
    game_version = models.CharField('版本', max_length=64)
    kind = models.CharField('引用类型', max_length=16)
    object_id = models.PositiveBigIntegerField('对象编号')
    name_en = models.CharField('英文', max_length=255, blank=True)
    name_zh = models.CharField('官方中文', max_length=255)
    icon = models.CharField('图标', max_length=255, blank=True)
    evidence = models.CharField('核对依据', max_length=1000)

    @staticmethod
    def phrase_identifier(name):
        """为普通专有名词生成可安全传给浏览器的稳定编号。"""
        import hashlib
        return int(hashlib.sha256(str(name).strip().casefold().encode()).hexdigest()[:13], 16)

    class Meta:
        verbose_name = '攻略术语校订'
        verbose_name_plural = '攻略术语校订'
        constraints = [models.UniqueConstraint(fields=['game_version', 'kind', 'object_id'], name='guide_term_identity_unique')]
