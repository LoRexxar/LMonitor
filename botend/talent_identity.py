"""天赋结构身份采用写时分离；已发布文件引用的旧主键始终保留。"""
from django.db import models, router, transaction


IDENTITY_FIELDS = frozenset({
    'talent_version', 'talent_version_id', 'class_name', 'spec_name', 'tree_type',
    'node_id', 'talent_id', 'spell_id', 'localization_only',
})
IDENTITY_ATTRS = (
    'talent_version_id', 'class_name', 'spec_name', 'tree_type',
    'node_id', 'talent_id', 'spell_id', 'localization_only',
)
DISPLAY_FIELDS = ('name', 'name_zh', 'icon', 'description', 'description_zh')


def save_node(instance, *, force_insert=False, force_update=False, using=None, update_fields=None):
    """内容修正沿用主键；结构身份变化原子退役旧行并创建或复用新身份。"""
    using = using or router.db_for_write(type(instance), instance=instance)
    if any(hasattr(getattr(instance, field), 'resolve_expression') for field in IDENTITY_ATTRS
           if update_fields is None or field in update_fields or field.removesuffix('_id') in update_fields):
        raise ValueError('天赋身份必须使用明确值，不能保存SQL表达式')
    options = dict(force_insert=force_insert, force_update=force_update,
                   using=using, update_fields=update_fields)
    if (instance.pk is None or force_insert or update_fields is not None
            and not IDENTITY_FIELDS.intersection(update_fields)):
        return models.Model.save(instance, **options)
    model = type(instance)
    query = model.all_objects.using(using)
    with transaction.atomic(using=using):
        previous = query.filter(pk=instance.pk).first()
        if previous is None:
            return models.Model.save(instance, **options)
        # 和中央发布保持先锁版本、后锁节点的顺序；相同分支写入者串行化。
        version_model = model._meta.get_field('talent_version').remote_field.model
        versions = {previous.talent_version_id, instance.talent_version_id} - {None}
        list(version_model.objects.using(using).select_for_update().filter(
            pk__in=versions).order_by('pk').values_list('pk', flat=True))
        previous = query.select_for_update().get(pk=instance.pk)
        fields = [field for field in model._meta.concrete_fields if not field.primary_key]
        data = {field.attname: getattr(previous, field.attname) for field in fields}
        for field in fields:
            if update_fields is None or field.name in update_fields or field.attname in update_fields:
                data[field.attname] = getattr(instance, field.attname)
        changed = any(data[key] != getattr(previous, key) for key in IDENTITY_ATTRS)
        if previous.localization_only or not changed:
            return models.Model.save(instance, **options)
        # 本地化引用编号独立于节点身份；禁止在不同结构身份之间隐式搬迁。
        if previous.reference_id is not None:
            raise ValueError('带名称引用编号的结构节点不能隐式改变身份')
        identity = {key: data[key] for key in IDENTITY_ATTRS}
        replacement = query.filter(**identity).exclude(pk=previous.pk).order_by('-is_current', 'pk').first()
        if update_fields is not None and any(data[key] != getattr(previous, key)
                                             for key in ('talent_id', 'spell_id')):
            for field in DISPLAY_FIELDS:
                if field not in update_fields:
                    # 身份修复没有提供新技能文字时，不把旧技能说明转移给新技能。
                    data[field] = getattr(replacement, field) if replacement is not None else ''
        query.filter(pk=previous.pk).update(is_current=False)
        if replacement is None:
            replacement = model(**data)
            models.Model.save(replacement, force_insert=True, using=using)
        else:
            changed_fields = [field for field in fields if getattr(replacement, field.attname) != data[field.attname]]
            for field in changed_fields:
                setattr(replacement, field.attname, data[field.attname])
            if changed_fields:
                models.Model.save(replacement, using=using, update_fields=[field.name for field in changed_fields])
        instance.pk = replacement.pk
        instance._state.adding, instance._state.db = False, using


class TalentIdentityQuerySet(models.QuerySet):
    """覆盖修复命令的批量入口，避免绕开模型保存或物理删除历史节点。"""

    def update(self, **kwargs):
        self._for_write = True
        if {'pk', 'id'}.intersection(kwargs):
            raise ValueError('天赋资料主键不可修改')
        if not IDENTITY_FIELDS.intersection(kwargs):
            return super().update(**kwargs)
        if any(hasattr(value, 'resolve_expression') for value in kwargs.values()):
            raise ValueError('天赋身份更新只接受明确值，不能直接执行SQL表达式')
        with transaction.atomic(using=self.db):
            rows = list(self)
            for row in rows:
                for field, value in kwargs.items():
                    setattr(row, field, value)
                row.save(using=self.db, update_fields=list(kwargs))
            return len(rows)

    def bulk_update(self, objs, fields, batch_size=None):
        self._for_write = True
        objs, fields = tuple(objs), tuple(fields)
        if not IDENTITY_FIELDS.intersection(fields):
            return super().bulk_update(objs, fields, batch_size=batch_size)
        if any(obj.pk is None for obj in objs):
            raise ValueError('批量修改天赋身份必须提供主键')
        # 同身份的批量补全文本仍使用批量SQL，只有真实身份变化才逐项分离。
        attrs = [self.model._meta.get_field(field).attname for field in fields if field in IDENTITY_FIELDS]
        with transaction.atomic(using=self.db):
            previous = {row.pk: row for row in self.filter(pk__in=[obj.pk for obj in objs]).only('pk', *attrs)}
            stable, changed = [], []
            for obj in objs:
                old = previous.get(obj.pk)
                if old is not None:
                    (changed if any(getattr(old, attr) != getattr(obj, attr) for attr in attrs)
                     else stable).append(obj)
            count = 0
            for obj in changed:
                obj.save(using=self.db, update_fields=fields)
                count += 1
            content_fields = [field for field in fields if field not in IDENTITY_FIELDS]
            if stable:
                count += (super().bulk_update(stable, content_fields, batch_size=batch_size)
                          if content_fields else len(stable))
            return count

    def bulk_create(self, objs, batch_size=None, ignore_conflicts=False, update_conflicts=False,
                    update_fields=None, unique_fields=None):
        self._for_write = True
        if update_conflicts and IDENTITY_FIELDS.intersection(update_fields or ()):
            raise ValueError('天赋身份不能通过冲突更新覆盖，请使用身份分离写入')
        objs = list(objs)
        structural = [obj for obj in objs if not obj.localization_only and obj.pk is None]
        options = dict(batch_size=batch_size, ignore_conflicts=ignore_conflicts,
                       update_conflicts=update_conflicts, update_fields=update_fields, unique_fields=unique_fields)
        if not structural or update_conflicts:
            return super().bulk_create(objs, **options)
        with transaction.atomic(using=self.db):
            versions = {obj.talent_version_id for obj in structural}
            version_model = self.model._meta.get_field('talent_version').remote_field.model
            list(version_model.objects.using(self.db).select_for_update().filter(
                pk__in=versions - {None}).order_by('pk').values_list('pk', flat=True))
            scope = models.Q(talent_version_id__in=versions - {None})
            if None in versions:
                scope |= models.Q(talent_version_id__isnull=True)
            node_ids = {obj.node_id for obj in structural}
            nodes = models.Q(node_id__in=node_ids - {None})
            if None in node_ids:
                nodes |= models.Q(node_id__isnull=True)
            existing = {tuple(getattr(row, key) for key in IDENTITY_ATTRS): row
                        for row in self.model.all_objects.using(self.db).filter(scope, nodes, localization_only=False)
                        .only('pk', 'is_current', *IDENTITY_ATTRS)
                        .order_by('is_current', 'pk')}
            fresh = []
            for obj in objs:
                row = existing.get(tuple(getattr(obj, key) for key in IDENTITY_ATTRS))
                if obj.localization_only or obj.pk is not None or row is None or row.is_current and not ignore_conflicts:
                    fresh.append(obj)
                    continue
                # 批量补回已退役的同一身份时复用原行，不能被ignore_conflicts悄悄丢弃。
                if not row.is_current and obj.is_current:
                    row.is_current = True
                    models.Model.save(row, using=self.db, update_fields=['is_current'])
                obj.pk, obj._state.adding, obj._state.db = row.pk, False, self.db
            super().bulk_create(fresh, **options)
            return objs

    def delete(self):
        self._for_write = True
        with transaction.atomic(using=self.db):
            retired = self.filter(localization_only=False).update(is_current=False)
            deleted, details = super(TalentIdentityQuerySet, self.filter(localization_only=True)).delete()
            if retired:
                details[self.model._meta.label] = details.get(self.model._meta.label, 0) + retired
            return retired + deleted, details

    def _raw_delete(self, using):
        if self.using(using).filter(localization_only=False).exists():
            raise ValueError('结构节点只能退役，不能绕过历史引用保护进行物理删除')
        return super()._raw_delete(using)


class TalentIdentityManager(models.Manager.from_queryset(TalentIdentityQuerySet)):
    pass
