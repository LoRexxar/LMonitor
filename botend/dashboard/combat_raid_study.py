"""按开怪和结束时间绘制首领推进曲线。"""


def clock(ms):
    minutes, seconds = divmod(ms // 1000, 60)
    return f"{minutes}:{seconds:02d}"


def progress_chart(pulls, field, selected, link):
    origin = pulls[0]["start"]
    length = pulls[-1]["end"] - origin
    best = None
    points, steps = [], []
    selected_record = {}
    for p in pulls:
        pct = p[field]
        x = round((p["end"] - origin) / length * 100, 4)
        if best is None or pct < best["value"]:
            best = {"value": pct, "id": p["id"], "time": clock(p["end"] - origin)}
        if p["id"] == selected:
            elapsed = p["end"] - origin
            # 用自首拉起的真实经过时间做分母，包含场间休整。
            selected_record = dict(value=best["value"], time=clock(elapsed),
                                   pace=round((100-best["value"]) * 3600000 / elapsed, 2))
        # 每次尝试结束时才有新观测；两次之间只延续上一条最佳记录。
        pos = f"{x},{100-best['value']}"
        steps.append(f"M{pos}" if not steps else f"H{x} V{100-best['value']}")
        stage = p["stage_label"]
        points.append(dict(x=x, y=100-pct, value=pct, id=p["id"], stage=stage,
                           time=clock(p["end"]-origin), best=best["value"], selected=p["id"]==selected,
                           url=link(pull=p["id"], page=None)))
    return dict(points=points, best=best, selected=selected_record, steps=" ".join(steps),
                line=" ".join(f"{p['x']},{p['y']}" for p in points),
                ticks=[dict(x=round(t/length*100, 4), label=f"{t//60000} 分") for t in range(0,length+1,1800000*max(1,length//10800000))],
                duration=clock(length))
