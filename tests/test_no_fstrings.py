"""House rule: no f-strings anywhere in the repo. Logging uses lazy %-style arguments, other text uses % or format."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_fstrings_anywhere():
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in (".venv", "venv", "build", "node_modules") for part in path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.JoinedStr):
                offenders.append("%s:%d" % (path.relative_to(ROOT), node.lineno))
    assert not offenders, "f-strings found: " + ", ".join(offenders)
