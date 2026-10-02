"""Language catalogs: the same keys everywhere, and every key written whole."""

import ast
import re
import unittest
from pathlib import Path
from unittest import mock

from containerhelper import i18n
from containerhelper.i18n import (
    LANGUAGES,
    META,
    PLURAL_FORMS,
    REFERENCE,
    catalog,
    meta,
    placeholders,
    plural_form,
    tr,
    tr_n,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted((ROOT / "containerhelper").rglob("*.py"))

TRANSLATORS = {"tr", "tr_n"}

#: A string constant shaped like a key: dotted lower-case segments.
KEY_SHAPE = re.compile(r"[a-z_]+(\.[a-z0-9_]+)+")

#: Dotted constants that are file names, not keys.
FILE_SUFFIXES = (".json", ".py", ".hc", ".exe", ".ini", ".bak", ".qm")


def strings(code: str) -> dict:
    return {key: value for key, value in catalog(code).items() if key != META}


def texts(value) -> list[str]:
    return list(value.values()) if isinstance(value, dict) else [value]


def calls():
    """Every `tr`/`tr_n` call in the program, with whether it runs at import.

    A call outside any function body runs once, when the module is imported,
    and its text would never change language again. Default arguments and
    decorators run at import too, so only the body counts as deferred.
    Qt's own `self.tr` is not ours: only a bare name or `i18n.<name>`.
    """

    def translator(func):
        if isinstance(func, ast.Name):
            return func.id
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "i18n"
        ):
            return func.attr
        return None

    def walk(node, path, deferred):
        body = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            body = node.body if isinstance(node.body, list) else [node.body]
        for child in ast.iter_child_nodes(node):
            inner = deferred or any(child is statement for statement in body)
            name = translator(child.func) if isinstance(child, ast.Call) else None
            if name in TRANSLATORS:
                yield path, child, name, inner
            yield from walk(child, path, inner)

    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        yield from walk(tree, path.relative_to(ROOT), False)


class PluralRuleTests(unittest.TestCase):
    def test_russian(self):
        forms = {n: plural_form("one_few_many", n) for n in (0, 1, 2, 4, 5, 11, 14, 21, 22, 25, 111, 1001)}
        self.assertEqual(
            forms,
            {0: "many", 1: "one", 2: "few", 4: "few", 5: "many", 11: "many", 14: "many",
             21: "one", 22: "few", 25: "many", 111: "many", 1001: "one"},
        )

    def test_english_and_french_differ_only_at_zero(self):
        self.assertEqual(plural_form("one_other", 0), "other")
        self.assertEqual(plural_form("one_other_zero", 0), "one")
        for n in (1, 2, 21):
            self.assertEqual(plural_form("one_other", n), plural_form("one_other_zero", n))

    def test_chinese_has_one_form(self):
        self.assertEqual({plural_form("other", n) for n in range(30)}, {"other"})


class SystemLanguageTests(unittest.TestCase):
    def test_language_part_decides(self):
        self.assertEqual(i18n.match_language("ru_RU"), "ru")
        self.assertEqual(i18n.match_language("ru-UA"), "ru")
        self.assertEqual(i18n.match_language("en_GB"), "en")

    def test_unsupported_or_unknown_is_english(self):
        for name in ("ja_JP", "", None, "C"):
            with self.subTest(name=name):
                self.assertEqual(i18n.match_language(name), "en")

    def test_a_region_code_matches_only_its_region(self):
        self.assertEqual(i18n.match_language("zh-CN"), "zh_CN")
        self.assertEqual(i18n.match_language("zh_TW"), "en")
        self.assertEqual(i18n.match_language("de_AT"), "de")

    def test_first_launch_follows_the_system(self):
        with mock.patch.object(i18n, "_system_locale", lambda: "ja_JP"):
            self.assertEqual(i18n.system_language(), "en")
        with mock.patch.object(i18n, "_system_locale", lambda: "de_DE"):
            self.assertEqual(i18n.system_language(), "de")
        with mock.patch.object(i18n, "_system_locale", lambda: "ru_RU"):
            self.assertEqual(i18n.system_language(), "ru")


FAKE = {
    "en": {META: {"plural": "one_other"}, "a": "A {x}", "n": {"one": "{n} point", "other": "{n} points"}},
    "ru": {META: {"plural": "one_few_many"}, "n": {"one": "{n} точка", "few": "{n} точки", "many": "{n} точек"}},
}


class LookupTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(i18n, "catalog", lambda code: FAKE.get(code, {}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(i18n.set_language, i18n.current())
        i18n.set_language("ru")

    def test_plural_follows_the_current_language(self):
        self.assertEqual(tr_n("n", 2), "2 точки")
        self.assertEqual(tr_n("n", 5, n="5 000"), "5 000 точек")
        i18n.set_language("en")
        self.assertEqual(tr_n("n", 1), "1 point")

    def test_missing_key_falls_back_to_english(self):
        self.assertEqual(tr("a", x=1), "A 1")

    def test_missing_form_falls_back_to_english(self):
        with mock.patch.dict(FAKE["ru"], {"n": {"one": "{n} точка"}}):
            self.assertEqual(tr_n("n", 5), "5 points")

    def test_braces_and_broken_templates(self):
        i18n.set_language("en")
        with mock.patch.dict(FAKE["en"], {"b": "{{x}} {y}"}):
            self.assertEqual(tr("b", y=1), "{x} 1")
            self.assertEqual(tr("b"), "{{x}} {y}")

    def test_a_given_language_needs_no_switch(self):
        """`tr_in` reads written text back; the current language stays."""
        self.assertEqual(i18n.tr_in("ru", "n.missing"), "n.missing")
        self.assertEqual(i18n.tr_in("xx", "a", x=2), "A 2")
        self.assertEqual(i18n.tr_in("en", "a", key=1, x=3), "A 3")
        self.assertEqual(i18n.current(), "ru")

    def test_unknown_key_shows_itself(self):
        self.assertEqual(tr("nowhere"), "nowhere")
        self.assertEqual(tr_n("nowhere", 3), "nowhere")

    def test_unknown_language_is_ignored(self):
        i18n.set_language("xx")
        self.assertEqual(i18n.current(), "ru")


class CatalogTests(unittest.TestCase):
    def test_every_language_has_a_catalog(self):
        self.assertIn(REFERENCE, LANGUAGES)
        for code in LANGUAGES:
            with self.subTest(code=code):
                info = meta(code)
                self.assertTrue(info.get("name"))
                self.assertIn(info.get("plural"), PLURAL_FORMS)
                self.assertIn(info.get("status"), ("reviewed", "draft"))
                self.assertTrue(strings(code))

    def test_same_keys_everywhere(self):
        reference = set(strings(REFERENCE))
        for code in LANGUAGES:
            keys = set(strings(code))
            with self.subTest(code=code):
                self.assertEqual(sorted(reference - keys), [], "missing")
                self.assertEqual(sorted(keys - reference), [], "extra")

    def test_same_kind_forms_and_placeholders(self):
        reference = strings(REFERENCE)
        for code in LANGUAGES:
            forms = set(PLURAL_FORMS[meta(code)["plural"]])
            for key, value in strings(code).items():
                with self.subTest(code=code, key=key):
                    expected = reference[key]
                    self.assertEqual(isinstance(value, dict), isinstance(expected, dict))
                    if isinstance(value, dict):
                        self.assertEqual(set(value), forms)
                    wanted = set().union(*(placeholders(t) for t in texts(expected)))
                    for text in texts(value):
                        self.assertIsInstance(text, str)
                        self.assertEqual(placeholders(text), wanted, text)


class SourceKeyTests(unittest.TestCase):
    def test_keys_are_literals_in_the_reference(self):
        """A key is written whole. One chosen by condition comes out of a
        dict of literals, and `test_key_shaped_constants_exist` checks those;
        a key glued together is seen by no test."""
        reference = strings(REFERENCE)
        found = []
        for path, call, name, _ in calls():
            first = call.args[0] if call.args else None
            if isinstance(first, (ast.JoinedStr, ast.BinOp)) or first is None:
                found.append(f"{path}:{call.lineno}: key is glued together")
            elif not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            elif first.value not in reference:
                found.append(f"{path}:{call.lineno}: {first.value} not in {REFERENCE}.json")
            elif isinstance(reference[first.value], dict) != (name == "tr_n"):
                found.append(f"{path}:{call.lineno}: {name} on {first.value}")
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_no_translation_at_import(self):
        found = [
            f"{path}:{call.lineno}"
            for path, call, _, deferred in calls()
            if not deferred
        ]
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_key_shaped_constants_exist(self):
        """A key in a dict of literals never meets `tr()` in person: every
        constant shaped like a key of a known namespace must exist."""
        reference = strings(REFERENCE)
        namespaces = {key.split(".")[0] for key in reference}
        found = []
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and KEY_SHAPE.fullmatch(node.value)
                    and node.value.split(".")[0] in namespaces
                    and not node.value.endswith(FILE_SUFFIXES)
                    and node.value not in reference
                ):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value}")
        self.assertEqual(found, [], "\n" + "\n".join(found))

    def test_every_key_is_used(self):
        """A key chosen by condition sits in a dict of literals, so any string
        constant in the code counts as a use."""
        used = set()
        for path in SOURCES:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    used.add(node.value)
        unused = sorted(set(strings(REFERENCE)) - used)
        self.assertEqual(unused, [])

    def test_no_russian_in_the_code(self):
        """UI text lives in the catalogs; a Russian literal in the code is a
        string that does not switch. Docstrings are prose and are checked by
        `test_source_language`."""
        cyrillic = re.compile("[А-Яа-яЁё]")
        found = []
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and cyrillic.search(node.value)
                ):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [], "\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
