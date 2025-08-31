from model import *
import json

# TODO move this to main to use registries, invoking using subcommand
r = Manifest.model_json_schema()
Path("manifest_schema.json").write_text(json.dumps(r, indent=2))
print("Done")