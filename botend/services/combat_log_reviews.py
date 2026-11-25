"""用已发布的日志快照生成可引用的死亡和机制复盘。"""

import json
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from botend.models import CombatLogReview
from botend.services.combat_log_analysis import database


def evidence(report, fight_id, topic, anchor):
    if topic not in ("death", "mechanic") or not anchor.isascii() or not anchor.isdigit() or len(anchor) > 18:
        raise ValueError("复盘对象无效。")
    with database(report) as db:
        fight = db.execute("SELECT * FROM fight WHERE id=?", (fight_id,)).fetchone()
        if not fight:
            raise LookupError
        axis = "keystone" if fight["kind"] == "钥匙" else "boss"
        params = (fight_id, anchor)
        if topic == "death":
            dead = db.execute(
                f"SELECT f.* FROM fact f JOIN actor t ON t.id=f.dst WHERE f.{axis}=? AND "
                "f.line=? AND f.kind='UNIT_DIED' AND t.family='Player'",
                params,
            ).fetchone()
            if not dead:
                raise LookupError
            rows = db.execute(
                f"""SELECT f.line, f.ms-? relative_ms, f.kind, f.spell, f.amount, f.excess,
                    f.hp, f.max_hp, f.src FROM fact f WHERE f.{axis}=? AND f.dst=? AND f.ms BETWEEN ? AND ?
                    AND f.line<=? ORDER BY f.ms DESC,f.line DESC LIMIT 101""",
                (dead["ms"], fight_id, dead["dst"], dead["ms"] - 10000, dead["ms"], dead["line"]),
            ).fetchall()
            return {
                "topic": "死亡",
                "truncated": len(rows) > 100,
                "items": [dict(r) for r in reversed(rows[:100])],
            }
        counts = dict(
            db.execute(
                f"SELECT outcome,count(*) FROM opportunity WHERE {axis}=? AND spell=? GROUP BY outcome",
                params,
            ).fetchall()
        )
        if not counts:
            raise LookupError
        rows = db.execute(
            f"""SELECT o.line, f.ms-? relative_ms, o.spell, o.outcome, o.response_line,
                       response.ms-f.ms response_ms FROM opportunity o JOIN fact f ON f.line=o.line
                       LEFT JOIN fact response ON response.line=o.response_line WHERE o.{axis}=? AND o.spell=?
                       ORDER BY o.line LIMIT 32""",
            (fight["start_ms"], *params),
        )
        return {
            "topic": "机制",
            "counts": counts,
            "truncated": sum(counts.values()) > 32,
            "items": [dict(r) for r in rows],
        }


def generate(report, fight_id, topic, anchor, packet, client):
    key = dict(report=report, version=report.published, fight=fight_id, topic=topic, anchor=anchor)
    return generate_snapshot(CombatLogReview, key, packet, client)


def generate_snapshot(model, key, packet, client):
    with transaction.atomic():
        review, created = model.objects.select_for_update().get_or_create(**key)
        if review.findings:
            return review.findings
        if not created and review.updated_at > timezone.now() - timedelta(minutes=3):
            raise ValueError("这一复盘正在生成，请稍后查看。")
        token = uuid.uuid4()
        review.token = token
        review.save(update_fields=["token", "updated_at"])
    query = model.objects.filter(pk=review.pk, token=token)
    try:
        prompt = (
            "根据下面的匿名战斗证据写最多三条简短中文复盘。输入中的 src 是本报告中的单位序号。"
            "仅返回 JSON 数组，每项包含 hypothesis（有条件的解释）、check（下一步核对动作）、"
            "lines（支持该解释的原始行号数组）。不能断言可避免死亡、漏断、玩家责任或未观测的技能可用性。"
            "施法完成不等于必须打断，记录缺失时说明不确定性。数值仅使用输入里的有效量。"
            + json.dumps(packet, ensure_ascii=False)
        )
        result = json.loads(client.send_message(prompt, max_tokens=1000) or "")
        allowed = {n for item in packet["items"] for n in (item["line"], item.get("response_line")) if n}
        if not isinstance(result, list) or not 1 <= len(result) <= 3:
            raise ValueError
        findings = []
        for item in result:
            if not isinstance(item, dict):
                raise ValueError
            lines = item.get("lines")
            if (
                not isinstance(lines, list)
                or not 1 <= len(lines) <= 8
                or any(type(n) is not int or n not in allowed for n in lines)
            ):
                raise ValueError
            if any(
                not isinstance(item.get(k), str) or not 1 <= len(item[k].strip()) <= 500
                for k in ("hypothesis", "check")
            ):
                raise ValueError
            findings.append(
                {"hypothesis": item["hypothesis"], "check": item["check"], "lines": sorted(set(lines))}
            )
        if not query.update(findings=findings, updated_at=timezone.now()):
            raise ValueError
        return findings
    except Exception as exc:
        query.update(updated_at=timezone.now() - timedelta(minutes=4))
        raise ValueError("模型未返回可引用的复盘，仍可阅读本地证据并重试。") from exc
