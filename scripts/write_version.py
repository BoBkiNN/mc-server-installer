import toml
from pathlib import Path


def main():
    root = Path(__file__).parent.parent
    pyproject =  root / "pyproject.toml"
    data = toml.loads(pyproject.read_text())
    version = data["project"]["version"]

    version_file = root / "mcsi" / "__version__.py"
    version_file.write_text(f'__version__ = "{version}"\n')
    print(version)
