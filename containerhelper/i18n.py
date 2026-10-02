"""UI text by stable key, one catalog per language.

The code names a string by its key (`tr("records.col.metadata")`); the text
itself lives in `containerhelper/locale/<code>.json`. A key, not the English
text: editing an English phrase would otherwise break it in every catalog.

No Qt here: `records.py`, `charts.py` and `collect.py` build text too. The
rule that keeps a language switch honest is that text is built when it is
shown, not when the object is created. A `tr()` at module level would
translate once, at import, and never again — `tests/test_i18n.py` rejects
it.

English is the reference: a key missing from another catalog falls back to
English, and the test that compares the catalogs fails the gate on it.
"""

from __future__ import annotations

import json
import string
from functools import lru_cache

LOCALE_PACKAGE = "containerhelper.locale"

#: The reference catalog and the fallback for a missing key.
REFERENCE = "en"

#: Shipped languages, in the order of the language list.
LANGUAGES = ("ru", "en")

#: Key of the catalog's own description; never a UI string.
META = "_meta"

#: Plural forms by rule, in the order the rule tries them.
PLURAL_FORMS = {
    # 1 → one, everything else → other: English, German, Spanish.
    "one_other": ("one", "other"),
    # 0 and 1 → one: French («0 point»).
    "one_other_zero": ("one", "other"),
    # «1 точка, 2 точки, 5 точек»; 11…14 go to many.
    "one_few_many": ("one", "few", "many"),
    # No inflection by number: Chinese.
    "other": ("other",),
}

def match_language(name: str | None) -> str:
    """The shipped language for a system locale name, else English.

    "ru_RU" and "ru-RU" give "ru"; a code with a region matches only that
    region: "zh_TW" is not "zh_CN", and a Traditional reader is better
    served by English than by characters they read as foreign.
    """
    if not name:
        return REFERENCE
    wanted = name.replace("-", "_").lower()
    for code in LANGUAGES:
        lowered = code.lower()
        if "_" in lowered:
            if wanted == lowered or wanted.startswith(lowered + "_"):
                return code
        elif wanted.split("_")[0] == lowered:
            return code
    return REFERENCE


def _system_locale() -> str | None:
    """The Windows UI language as a locale name, or the process locale.

    The UI language, not the regional format: a Russian Windows set to
    German number formats still speaks Russian in its menus.
    """
    import locale

    try:
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        name = locale.windows_locale.get(langid)
        if name:
            return name
    except (AttributeError, OSError):
        pass
    try:
        return locale.getlocale()[0]
    except ValueError:
        return None


def system_language() -> str:
    """The language of the first launch: the system's if shipped, else
    English."""
    return match_language(_system_locale())


_current = system_language()


def plural_form(rule: str, count: int) -> str:
    """The name of the form a number takes under a plural rule."""
    n = abs(count)
    if rule == "one_other":
        return "one" if n == 1 else "other"
    if rule == "one_other_zero":
        return "one" if n <= 1 else "other"
    if rule == "one_few_many":
        if 11 <= n % 100 <= 14:
            return "many"
        tail = n % 10
        if tail == 1:
            return "one"
        if 2 <= tail <= 4:
            return "few"
        return "many"
    return "other"


@lru_cache(maxsize=None)
def catalog(code: str) -> dict:
    """A language's catalog as read from the package; empty if it is missing.

    Through importlib.resources, as the factory data: in a one-file build a
    path leads nowhere on disk.
    """
    try:
        from importlib.resources import files

        raw = (files(LOCALE_PACKAGE) / f"{code}.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, ModuleNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def meta(code: str) -> dict:
    """Name, plural rule, Qt catalog and review status of a language."""
    value = catalog(code).get(META)
    return value if isinstance(value, dict) else {}


def set_language(code: str) -> None:
    """Switch the language; an unknown code leaves the current one."""
    global _current
    if code in LANGUAGES:
        _current = code


def current() -> str:
    """The code of the language the UI speaks now."""
    return _current


def _fill(text: str, params: dict) -> str:
    """`{name}` filled in; `{{` reads as a brace with or without parameters.

    A broken template or a missing parameter comes back as it is, as a
    missing key comes back as itself: something odd on screen is a bug to
    fix, a crash in the middle of a measurement is worse. The catalog tests
    keep both out of the gate.
    """
    try:
        return text.format_map(params)
    except (KeyError, ValueError, IndexError):
        return text


def tr(key: str, /, **params) -> str:
    """The text of a key in the current language, with `{name}` filled in."""
    return tr_in(_current, key, **params)


def tr_in(language: str, key: str, /, **params) -> str:
    """The text of a key in the given language, falling back to English.

    For recognising text written to a file in whatever language the UI spoke
    then; what is shown goes through `tr`.
    """
    for code in (language, REFERENCE):
        value = catalog(code).get(key)
        if isinstance(value, str):
            return _fill(value, params)
    return key


def tr_n(key: str, count: int, **params) -> str:
    """The form of a key that agrees with `count`, with `{n}` set to it.

    Pass `n` explicitly to show the number formatted (digit groups and so
    on); the form is still chosen by `count`. A form missing in the current
    language falls back to English, under the English rule.
    """
    params.setdefault("n", count)
    for code in (_current, REFERENCE):
        value = catalog(code).get(key)
        if isinstance(value, dict):
            text = value.get(plural_form(meta(code).get("plural", ""), count))
            if isinstance(text, str):
                return _fill(text, params)
    return key


def placeholders(text: str) -> set[str]:
    """Names of the `{name}` fields in a catalog string."""
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}
