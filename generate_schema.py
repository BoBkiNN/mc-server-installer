from model import *
import json

r = Manifest.model_json_schema()
Path("manifest_schema.json").write_text(json.dumps(r, indent=2))
print("Done")