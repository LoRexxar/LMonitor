"""Read-only analysis of immutable SimulationCraft HTML report artifacts.

The source report is never modified. This module only reads the exact report
bound to a SimulationRun and produces a small structured analysis document for
the workbench UI.
"""
import re

from botend.services import simc_artifacts


def parse_simc_html_report(html_content):
    """Return a bounded structured analysis document from a SimC HTML report."""
    document = {
        "dps": None,
        "character": {},
        "simulation": {},
        "talents": {},
        "abilities": [],
        "top_abilities": [],
        "sample_sequence": [],
        "buffs": {"dynamic": [], "constant": []},
    }
    if not isinstance(html_content, str) or not html_content:
        return document

    dps_match = re.search(r":\s*([\d,]+)\s*dps", html_content, re.IGNORECASE)
    if dps_match:
        try:
            document["dps"] = int(dps_match.group(1).replace(",", ""))
        except ValueError:
            pass

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html_content, "html.parser")
        player = soup.find(class_="player")
        if player:
            heading = player.find("h2")
            if heading:
                heading_text = heading.get_text(" ", strip=True)
                name_match = re.match(r"\s*([^:]+):", heading_text)
                if name_match:
                    document["character"]["name"] = name_match.group(1).strip()[:200]
                if document["dps"] is None:
                    heading_dps = re.search(r":\s*([\d,]+)\s*dps", heading_text, re.IGNORECASE)
                    if heading_dps:
                        document["dps"] = int(heading_dps.group(1).replace(",", ""))

            # Current SimC reports keep heavy player details as escaped HTML in
            # .toggle-content, then expand it with JavaScript in the browser.
            # Parse that fragment separately without touching the source file.
            player_detail = player
            toggle_content = player.find("div", class_="toggle-content", recursive=False)
            if toggle_content:
                deferred = toggle_content.find("script", attrs={"type": "text/x-deferred-html"})
                if deferred:
                    fragment_text = deferred.string or deferred.decode_contents()
                else:
                    fragment_text = toggle_content.get_text("", strip=False)
                if "<" in fragment_text and ">" in fragment_text:
                    player_detail = BeautifulSoup(fragment_text, "html.parser")

            character_fields = {
                "Race:": "race", "Class:": "class", "Spec:": "spec", "Level:": "level",
            }
            for item in player_detail.select(".params li"):
                text = item.get_text(" ", strip=True)
                for prefix, key in character_fields.items():
                    if prefix in text:
                        document["character"][key] = text.split(":", 1)[1].strip()[:200]
                        break

            for row in player_detail.select("table.spec tr"):
                cells = row.find_all(["th", "td"], recursive=False)
                if len(cells) < 2:
                    continue
                label = cells[0].get_text(" ", strip=True)
                value = cells[1].get_text(" ", strip=True)
                if label == "Talent" and value:
                    document["talents"]["string"] = value[:2000]
                elif label == "Set Bonus" and value:
                    document["talents"]["set_bonuses"] = [
                        part.strip()[:500] for part in value.splitlines() if part.strip()
                    ][:20]
            set_bonuses = [
                item.get_text(" ", strip=True)[:500]
                for item in player_detail.select("tr.left.nowrap td li")
                if item.get_text(strip=True)
            ]
            if set_bonuses:
                document["talents"]["set_bonuses"] = set_bonuses[:20]

            abilities_table = next((
                table for table in player_detail.select("table.sc.sort")
                if table.find("th") and table.find("th").get_text(" ", strip=True) == "Damage Stats"
            ), None)
            if abilities_table is None:
                abilities_table = next((
                    table for table in soup.select("table.sc.sort")
                    if table.find("th") and table.find("th").get_text(" ", strip=True) == "Damage Stats"
                ), None)
            abilities = []
            if abilities_table:
                header_row = next((
                    row for row in abilities_table.select("thead tr")
                    if any(cell.get_text(" ", strip=True) == "DPS" for cell in row.find_all("th", recursive=False))
                ), None) or abilities_table.find("tr")
                headers = [cell.get_text(" ", strip=True) for cell in header_row.find_all("th", recursive=False)] if header_row else []
                header_index = {name: index for index, name in enumerate(headers)}

                def cell_text(cells, label):
                    index = header_index.get(label)
                    return cells[index].get_text(" ", strip=True)[:300] if index is not None and index < len(cells) else ""

                detail_keys = {
                    "Type": "type", "Executes": "executes", "Direct Results": "direct_results",
                    "Ticks": "ticks", "Tick Results": "tick_results", "Refreshes": "refreshes",
                    "Execute Time per Execution": "execute_time_per_execution",
                    "Tick Time per Tick": "tick_time_per_tick", "Actual Amount": "actual_amount",
                    "Raw Amount": "raw_amount", "Mitigated": "mitigated",
                    "Amount per Total Time": "amount_per_total_time",
                    "Amount per Total Execute Time": "amount_per_total_execute_time",
                }
                for row in abilities_table.select("tr.toprow:not(.childrow)"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) < 3:
                        continue
                    name = cells[0].get_text(" ", strip=True)[:300]
                    link = cells[0].find("a", href=True)
                    spell_match = re.search(r"(?:spell=|/spell/)(\d+)", link.get("href", "")) if link else None
                    dps_text = cell_text(cells, "DPS")
                    percent_text = cell_text(cells, "DPS%")
                    dps_value = (re.search(r"\(([\d,]+)\)", dps_text) or re.search(r"([\d,]+)", dps_text))
                    percent_value = (re.search(r"\(([\d.]+%)\)", percent_text) or re.search(r"([\d.]+%)", percent_text))
                    percent_number = None
                    if percent_value:
                        try:
                            percent_number = float(percent_value.group(1).rstrip("%"))
                        except ValueError:
                            pass

                    details = {}
                    detail_row = row.find_next_sibling("tr", class_="details")
                    detail_table = detail_row.find("table", class_="details") if detail_row else None
                    if detail_table:
                        detail_headers = [cell.get_text(" ", strip=True) for cell in detail_table.find("tr").find_all(["th", "td"], recursive=False)]
                        detail_value_row = detail_table.find("tr").find_next_sibling("tr")
                        detail_values = detail_value_row.find_all(["th", "td"], recursive=False) if detail_value_row else []
                        for index, label in enumerate(detail_headers):
                            key = detail_keys.get(label)
                            if key and index < len(detail_values):
                                details[key] = detail_values[index].get_text(" ", strip=True)[:300]
                    if name:
                        abilities.append({
                            "name": name,
                            "spell_id": spell_match.group(1) if spell_match else "",
                            "dps": dps_value.group(1).replace(",", "") if dps_value else "",
                            "dps_percent": percent_value.group(1) if percent_value else "",
                            "execute": cell_text(cells, "Execute"),
                            "interval": cell_text(cells, "Interval"),
                            "total_time": cell_text(cells, "Total Time"),
                            "dpe": cell_text(cells, "DPE"),
                            "dpet": cell_text(cells, "DPET"),
                            "type": cell_text(cells, "Type"),
                            "count": cell_text(cells, "Count"),
                            "hit": cell_text(cells, "Hit"),
                            "crit": cell_text(cells, "Crit"),
                            "average": cell_text(cells, "Avg"),
                            "crit_percent": cell_text(cells, "Crit%"),
                            "avoid_percent": cell_text(cells, "Avoid%"),
                            "uptime_percent": cell_text(cells, "Up%"),
                            "details": details,
                            "_percent": percent_number,
                        })
            abilities.sort(key=lambda row: row["_percent"] if row["_percent"] is not None else -1, reverse=True)
            document["abilities"] = [
                {key: value for key, value in row.items() if key != "_percent"}
                for row in abilities
            ]
            document["top_abilities"] = [
                {key: row[key] for key in ("name", "dps", "dps_percent")}
                for row in abilities[:12]
            ]

            def parse_label_details(container):
                values = {}
                if not container:
                    return values
                key_map = {
                    "max_stacks": "max_stacks", "base duration": "base_duration",
                    "base cooldown": "base_cooldown", "default_chance": "default_chance",
                    "refresh behavior": "refresh_behavior", "stack behavior": "stack_behavior",
                    "stat": "stat", "amount": "amount", "trigger_pct": "trigger_pct",
                    "interval_min/max": "interval_min_max", "trigger_min/max": "trigger_min_max",
                    "duration_min/max": "duration_min_max", "uptime_min/max": "uptime_min_max",
                }
                for item in container.select("ul.label li"):
                    label = item.find("span")
                    if not label:
                        continue
                    raw_label = label.get_text(" ", strip=True).rstrip(":")
                    key = key_map.get(raw_label)
                    if key:
                        full_text = item.get_text(" ", strip=True)
                        values[key] = full_text[len(label.get_text(" ", strip=True)):].strip()[:500]
                return values

            dynamic_table = next((
                table for table in player_detail.select("table.sc")
                if any(th.get_text(" ", strip=True) == "Dynamic Buffs" for th in table.find_all("th"))
            ), None)
            if dynamic_table:
                dynamic_headers = next((
                    [cell.get_text(" ", strip=True) for cell in row.find_all("th", recursive=False)]
                    for row in dynamic_table.select("thead tr")
                    if any(cell.get_text(" ", strip=True) == "Dynamic Buffs" for cell in row.find_all("th", recursive=False))
                ), [])
                if dynamic_headers != ["Dynamic Buffs", "Start", "Refresh", "Total", "Start", "Trigger", "Duration", "Uptime", "Benefit", "Overflow", "Expiry"]:
                    dynamic_headers = []
                dynamic_buffs = []
                for body in dynamic_table.find_all("tbody", recursive=False):
                    if not dynamic_headers:
                        break
                    row = body.find("tr", class_="right", recursive=False)
                    if not row:
                        continue
                    cells = row.find_all("td", recursive=False)
                    if len(cells) < 11:
                        continue
                    link = cells[0].find("a", href=True)
                    spell_match = re.search(r"(?:spell=|/spell/)(\d+)", link.get("href", "")) if link else None
                    detail_row = row.find_next_sibling("tr", class_="details")
                    stack_uptimes = []
                    if detail_row:
                        heading = next((h for h in detail_row.find_all("h4") if h.get_text(" ", strip=True) == "Stack Uptimes"), None)
                        stack_list = heading.find_next_sibling("ul") if heading else None
                        if stack_list:
                            for item in stack_list.find_all("li", recursive=False):
                                label = item.find("span")
                                if label:
                                    stack_uptimes.append({
                                        "stack": label.get_text(" ", strip=True).rstrip(":"),
                                        "uptime": item.get_text(" ", strip=True)[len(label.get_text(" ", strip=True)):].strip()[:100],
                                    })
                    dynamic_buffs.append({
                        "name": cells[0].get_text(" ", strip=True)[:300],
                        "spell_id": spell_match.group(1) if spell_match else "",
                        "trigger_count_start": cells[1].get_text(" ", strip=True)[:100],
                        "trigger_count_refresh": cells[2].get_text(" ", strip=True)[:100],
                        "trigger_count_total": cells[3].get_text(" ", strip=True)[:100],
                        "interval_start": cells[4].get_text(" ", strip=True)[:100],
                        "interval_trigger": cells[5].get_text(" ", strip=True)[:100],
                        "duration": cells[6].get_text(" ", strip=True)[:100],
                        "uptime": cells[7].get_text(" ", strip=True)[:100],
                        "benefit": cells[8].get_text(" ", strip=True)[:100],
                        "overflow": cells[9].get_text(" ", strip=True)[:100],
                        "expiry": cells[10].get_text(" ", strip=True)[:100],
                        "details": parse_label_details(detail_row),
                        "stack_uptimes": stack_uptimes[:100],
                    })
                document["buffs"]["dynamic"] = dynamic_buffs

            constant_table = next((
                table for table in player_detail.select("table.sc")
                if table.find("th") and table.find("th").get_text(" ", strip=True) == "Constant Buffs"
            ), None)
            if constant_table:
                constant_buffs = []
                for body in constant_table.find_all("tbody", recursive=False):
                    row = body.find("tr", recursive=False)
                    if not row or "details" in (row.get("class") or []):
                        continue
                    cell = row.find("td", recursive=False)
                    if not cell:
                        continue
                    link = cell.find("a", href=True)
                    spell_match = re.search(r"(?:spell=|/spell/)(\d+)", link.get("href", "")) if link else None
                    detail_row = row.find_next_sibling("tr", class_="details")
                    constant_buffs.append({
                        "name": cell.get_text(" ", strip=True)[:300],
                        "spell_id": spell_match.group(1) if spell_match else "",
                        "details": parse_label_details(detail_row),
                    })
                document["buffs"]["constant"] = constant_buffs

            sequence_heading = next((
                heading for heading in player_detail.find_all(["h3", "h4"])
                if heading.get_text(" ", strip=True) == "Sample Sequence Table"
            ), None)
            sequence_table = sequence_heading.find_next("table", class_="sc") if sequence_heading else None
            if sequence_table:
                sequence = []
                for row in sequence_table.select("tr"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) < 6:
                        continue
                    action_cell = cells[2]
                    action_node = action_cell.find("b")
                    action = action_node.get_text(" ", strip=True) if action_node else ""
                    action_text = action_cell.get_text(" ", strip=True)
                    list_match = re.search(r"\[([^\]]+)\]\s*$", action_text)
                    if not action:
                        action = re.sub(r"\s*\[[^\]]+\]\s*$", "", action_text).strip()
                    sequence.append({
                        "time": cells[0].get_text(" ", strip=True)[:50],
                        "marker": cells[1].get_text(" ", strip=True)[:20],
                        "action": action[:300],
                        "action_list": list_match.group(1)[:100] if list_match else "",
                        "target": cells[3].get_text(" ", strip=True)[:300],
                        "resources": cells[4].get_text(" ", strip=True)[:300],
                        "buffs": cells[5].get_text(" ", strip=True)[:2000],
                    })
                document["sample_sequence"] = sequence[:2000]

        masthead = soup.find(id="masthead")
        if masthead:
            simulation_fields = {
                "Timestamp:": "timestamp", "Iterations:": "iterations",
                "Fight Length:": "fight_length", "Fight Style:": "fight_style",
            }
            for item in masthead.select(".params li"):
                text = item.get_text(" ", strip=True)
                for prefix, key in simulation_fields.items():
                    if prefix in text:
                        document["simulation"][key] = text.split(":", 1)[1].strip()[:200]
                        break
    except Exception:
        # A partial analysis document is preferable to hiding the immutable report.
        pass
    return document


def analyze_run_artifact(task, artifact):
    """Analyze one exact Run-bound HTML artifact without modifying its bytes."""
    if not artifact or artifact.task_id != task.id or artifact.artifact_type != "html_report" or not artifact.run_id:
        return None
    filename = str(artifact.file_path or "").rsplit("/", 1)[-1]
    validated = simc_artifacts._validated_result(task, filename, run=artifact.run)
    if not validated:
        return None
    report_path, _ = validated
    try:
        html_content = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return parse_simc_html_report(html_content)
