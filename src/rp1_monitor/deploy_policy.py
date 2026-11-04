"""Local native station credentials shared by motor parameter maintenance.

Strategies are started manually on the robot. No deploy command is exposed here.
"""

from pathlib import Path


def station_headers(context):
    viewer = Path(context["station_token_file"])
    return {"Authorization": "Bearer " + viewer.read_text().strip(),
            "X-RP1-Station-Token": viewer.with_name("station-token.txt").read_text().strip()}
