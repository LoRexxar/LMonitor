"""按同一机制逐轮查验命中与驱散，保留未到达及观测不足样本。"""

import hashlib
import json
from collections import Counter
from itertools import zip_longest

from botend.services.combat_log_analysis import database, evidence_url
from botend.services.combat_log_facts import DAMAGE


def evaluate(rule, samples):
    rounds, totals, absent = [], Counter(), []
    for sample in samples:
        axis = "keystone" if sample["kind"] == "钥匙" else "boss"
        report = sample["report"]
        impact = rule.kind == "impact"
        if impact:
            trigger = "f.kind='SPELL_CAST_SUCCESS' AND s.family IN ('Creature','Vehicle') AND f.spell=?"
            kinds = ",".join("?" for _ in DAMAGE)
            answer = f"f.kind IN ({kinds}) AND t.family='Player' AND f.spell=?"
            params = [rule.trigger, *sorted(DAMAGE), rule.response]
        else:
            trigger = "f.kind='SPELL_AURA_APPLIED' AND t.family='Player' AND f.spell=?"
            answer = "f.kind='SPELL_DISPEL' AND f.extra_spell=?"
            params = [rule.trigger, rule.trigger]
        sql = (
            f"SELECT f.line,f.ms,f.src,f.dst,f.kind,t.name target FROM fact f "
            f"JOIN actor s ON s.id=f.src JOIN actor t ON t.id=f.dst WHERE f.{axis}=? "
            f"AND (({trigger}) OR ({answer})) ORDER BY f.line"
        )
        attempts = []
        pending = {}
        with database(report) as db:
            for row in db.execute(sql, [sample["id"], *params]):
                ident = row["src" if impact else "dst"]
                opportunity = pending.get(ident)
                if row["kind"] == ("SPELL_CAST_SUCCESS" if impact else "SPELL_AURA_APPLIED"):
                    if opportunity and row["ms"] < opportunity["until"]:
                        opportunity["interrupted"] = True
                    op = dict(
                        number=len(attempts) + 1,
                        start=row["ms"],
                        until=row["ms"] + rule.window * 1000,
                        line=row["line"],
                        url=evidence_url(report, row["line"]),
                        responses=[],
                        interrupted=False,
                    )
                    attempts.append(op)
                    pending[ident] = op
                elif opportunity and opportunity["start"] <= row["ms"] < opportunity["until"]:
                    opportunity["responses"].append(
                        dict(line=row["line"], target=row["target"], url=evidence_url(report, row["line"]))
                    )
                    if not impact:
                        pending.pop(ident)
        if not attempts:
            absent.append(sample)
        for op in attempts:
            if op["start"] < sample["window_start"] or (
                sample["window_end"] is not None and op["start"] > sample["window_end"]
            ):
                continue
            if op["responses"]:
                outcome = "记录受击" if impact else "记录驱散"
            elif op["interrupted"] or sample["end_ms"] is None or sample["end_ms"] < op["until"]:
                outcome = "窗口不完整"
            else:
                outcome = "未见受击" if impact else "未见驱散"
            op.update(
                outcome=outcome, sample=sample, seconds=round((op["start"] - sample["start_ms"]) / 1000, 2)
            )
            totals[outcome] += 1
            rounds.append(op)
    return {"rule": rule, "rounds": rounds, "totals": dict(totals), "absent": absent}


def review_packet(matrix, samples):
    # 对照窗口按观测结果轮流选取，避免只送入最早发生的一类机制。
    outcomes = {}
    for op in matrix["rounds"]:
        outcomes.setdefault(op["outcome"], []).append(op)
    items, refs = [], {}
    for group in zip_longest(*outcomes.values()):
        for op in group:
            if op is None or len(items) >= 32:
                continue
            line = len(refs) + 1
            refs[line] = op["url"]
            item = dict(
                line=line,
                round=op["number"],
                seconds=op["seconds"],
                outcome=op["outcome"],
                responses=len(op["responses"]),
            )
            if op["responses"]:
                refs[line + 1] = op["responses"][0]["url"]
                item["response_line"] = line + 1
            items.append(item)
        if len(items) >= 32:
            break
    rule = matrix["rule"]
    packet = dict(
        topic="跨场机制对照",
        trigger=rule.trigger,
        response=rule.response,
        rule_type=rule.kind,
        seconds=rule.window,
        counts=matrix["totals"],
        absent=len(matrix["absent"]),
        items=items,
    )
    versions = [
        (str(s["report"].pk), str(s["report"].published), s["id"], s["window_start"], s["window_end"])
        for s in samples
    ]
    digest = hashlib.sha256(json.dumps([packet, refs, versions], sort_keys=True).encode()).hexdigest()
    return packet, refs, digest
