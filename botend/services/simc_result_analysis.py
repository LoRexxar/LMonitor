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
        "top_abilities": [],
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

            abilities_table = player_detail.select_one("table.sc.sort") or soup.select_one("table.sc.sort")
            abilities = []
            if abilities_table:
                for row in abilities_table.select("tr.toprow:not(.childrow)"):
                    cells = row.find_all("td", recursive=False)
                    if len(cells) < 3:
                        cells = row.find_all("td")
                    if len(cells) < 3:
                        continue
                    name = cells[0].get_text(" ", strip=True)[:300]
                    dps_text = cells[1].get_text(" ", strip=True)
                    percent_text = cells[2].get_text(" ", strip=True)
                    dps_value = (re.search(r"\(([\d,]+)\)", dps_text) or re.search(r"([\d,]+)", dps_text))
                    percent_value = (re.search(r"\(([\d.]+%)\)", percent_text) or re.search(r"([\d.]+%)", percent_text))
                    percent_number = None
                    if percent_value:
                        try:
                            percent_number = float(percent_value.group(1).rstrip("%"))
                        except ValueError:
                            pass
                    if name:
                        abilities.append({
                            "name": name,
                            "dps": dps_value.group(1).replace(",", "") if dps_value else "",
                            "dps_percent": percent_value.group(1) if percent_value else "",
                            "_percent": percent_number,
                        })
            abilities.sort(key=lambda row: row["_percent"] if row["_percent"] is not None else -1, reverse=True)
            document["top_abilities"] = [
                {key: row[key] for key in ("name", "dps", "dps_percent")}
                for row in abilities[:12]
            ]

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
