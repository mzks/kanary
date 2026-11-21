# kanary-template: http-json
# kind: source
# level: basic
# summary: GET a JSON API and pick values by path, converted to numbers (HTTPJSONSource).
# summary-ja: JSON の API を GET し、path で選んだ値を数値にして出す (HTTPJSONSource)。
import os

import kanary


@kanary.source(source_id="template.http_json", interval=1 * kanary.minute)
class TemplateHttpJson(kanary.HTTPJSONSource):
    description = "TODO: describe the API."

    url = "https://api.example.org/v1/status"  # TODO
    timeout = 5.0

    # Input name -> dotted path into the JSON; list items by index ("sensors.0.value").
    # Values are converted to numbers ("12.5" -> 12.5, true -> 1.0). Missing paths and
    # non-numeric values are listed in the result metadata. Leave fields empty to
    # publish every numeric value under its dotted path.
    fields = {
        "temperature": "data.temperature",
        "pump_running": "data.pump.running",
        "first_sensor": "data.sensors.0.value",
    }

    # A token is a secret: keep it out of this file, for example in an environment
    # variable. Without it the API answers 401 and the poll fails with that.
    headers = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"}  # TODO
    # Or from secrets.json next to this file (chmod 600, not in git), read when this
    # file is loaded: {"api_token": "..."}
    # headers = {"Authorization": f"Bearer {kanary.load_json('api_token', filename='secrets.json')}"}

    # Override select(document) for documents that need more than paths:
    # def select(self, document):
    #     return {item["name"]: item["value"] for item in document["sensors"]}
