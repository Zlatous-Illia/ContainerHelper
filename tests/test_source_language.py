"""Comments, docstrings and docs are English.

The UI is still Russian, and it lives in string literals, which this test
does not look at. Russian may appear in English text only where the exact
wording matters — a UI label or a Russian grammar example — and then only
quoted in «guillemets» or `backticks`. Anything else is a comment someone
forgot to translate.
"""

import ast
import io
import re
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CYRILLIC = re.compile("[А-Яа-яЁё]")

#: Where Russian is allowed inside English text.
QUOTED = re.compile(r"«[^»]*»|`[^`]*`")

DOCS = ("SPEC.md", "CLAUDE.md", "README.md", "GLOSSARY.md")


def untranslated(text: str) -> bool:
    return bool(CYRILLIC.search(QUOTED.sub("", text)))


def python_sources() -> list[Path]:
    return sorted((ROOT / "containerhelper").rglob("*.py")) + sorted(
        (ROOT / "tests").rglob("*.py")
    )


def text_blocks(source: str):
    """Comments and bare string statements (docstrings), with line numbers.

    A bare string statement does nothing at run time, so it is prose. String
    literals used as values are left alone: that is where the UI text is.
    """
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            yield node.lineno, node.value.value


class SourceLanguageTests(unittest.TestCase):
    def test_comments_and_docstrings_are_english(self):
        found = []
        for path in python_sources():
            source = path.read_text(encoding="utf-8")
            for line, text in text_blocks(source):
                if untranslated(text):
                    first = text.strip().splitlines()[0][:70]
                    found.append(f"{path.relative_to(ROOT)}:{line}: {first}")
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_docs_are_english(self):
        found = []
        for name in DOCS:
            fenced = False
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                # Code blocks quote the program as it is, UI strings included.
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                if not fenced and untranslated(line):
                    found.append(f"{name}:{number}: {line[:70]}")
        self.assertEqual(found, [], "\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
