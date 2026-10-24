# Copies whoop_data.json into dashboard.html so the single file works when opened directly.
# Run after whoop.py:  python3 whoop.py && python3 embed_data.py
import json, re

data = json.load(open("whoop_data.json"))
data.get("profile", {}).pop("email", None)  # the dashboard never shows it; keep it out of the file
blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

html = open("dashboard.html").read()
tag = re.compile(r'(<script type="application/json" id="whoop-data">)(.*?)(</script>)', re.S)
if not tag.search(html):
    raise SystemExit("whoop-data block not found in dashboard.html")
open("dashboard.html", "w").write(tag.sub(lambda m: m.group(1) + blob + m.group(3), html, count=1))
print({k: len(v) for k, v in data.items() if isinstance(v, list)})
