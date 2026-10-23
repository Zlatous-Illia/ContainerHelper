"""Switching the UI language while the windows are open.

The core (`i18n`) holds our own text; Qt's stock captions — the buttons of a
message box, the labels of the file dialog, the menu of an input field — come
from its own catalog, `qtbase_<code>.qm`, through a `QTranslator`. Both
switch together, or a window speaks two languages at once.

Every window answers `QEvent.LanguageChange` with its `retranslate()`. Qt
sends that event itself on a translator change, and more than once: posted
to every top-level widget, posted again through every shown native window,
and a window with a parent (a chart window, a record window) hears it from
its parent as well. Taking the posted events back does not catch them all.
So a switch tells each parentless window at once, and every top-level window
lets through only the first event for a language (`repeated_change`): the
rest would rebuild the charts and read the volume again for nothing.

A language may name a font of its own (`_meta.font`), added after the
system one: Qt looks for the characters the system font lacks along the
Windows fallback chain, and for Chinese that starts with a Japanese font.
Characters it lacks came from a third one — two typefaces in one line.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QLibraryInfo, QTranslator
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from .. import i18n

#: Qt's own catalog for the current language; None while none is installed.
_translator: QTranslator | None = None

#: The application font before any language added its own to it.
_system_font: QFont | None = None

#: The window property holding the language the window last answered.
APPLIED = "applied_language"


def _qt_translator(code: str) -> QTranslator | None:
    """Qt's catalog for a language, or None if Qt ships none for it."""
    name = i18n.meta(code).get("qt")
    if not name:
        return None
    # No parent: one that fails to load is collected with the reference.
    translator = QTranslator()
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    return translator if translator.load(name, folder) else None


def _language_font() -> None:
    """Put the current language's own font after the system one.

    After, not instead: Latin letters and digits stay in the system font,
    and the language's font only fills in what that one lacks. A font the
    machine does not have changes nothing — Qt falls back as before.
    """
    global _system_font
    app = QCoreApplication.instance()
    if not isinstance(app, QApplication):
        return
    if _system_font is None:
        _system_font = QApplication.font()
    font = QFont(_system_font)
    extra = i18n.meta(i18n.current()).get("font")
    if extra:
        system = _system_font.families() or [_system_font.family()]
        font.setFamilies([*system, extra])
    if font != QApplication.font():
        QApplication.setFont(font)


def load_language(code: str) -> None:
    """Switch our text and Qt's to a language; we send no event.

    For the start, before the first window has text to change. Qt still
    posts its own events to the windows there are; `repeated_change` drops
    them once a window has the language. An unknown code leaves the current
    language, as `i18n.set_language` does, but Qt's catalog is brought in
    line with it all the same.
    """
    global _translator
    i18n.set_language(code)
    app = QCoreApplication.instance()
    if _translator is not None:
        app.removeTranslator(_translator)
    _translator = _qt_translator(i18n.current())
    if _translator is not None:
        app.installTranslator(_translator)
    _language_font()


def apply_language(code: str) -> None:
    """Switch the language and tell every open window, each one once.

    At once, not when Qt's posted events arrive: whoever switched sees every
    window in the new language before the next line of code. A parentless
    window passes the event down to its children, child windows included.
    """
    load_language(code)
    for window in QApplication.topLevelWidgets():
        if window.parentWidget() is None:
            QCoreApplication.sendEvent(window, QEvent(QEvent.Type.LanguageChange))


def repeated_change(window: QWidget, event: QEvent) -> bool:
    """Whether the event is a LanguageChange the window has answered already.

    For a top-level window's `event()`: returning before Qt's own handling
    stops the event from reaching the children too.
    """
    if event.type() != QEvent.Type.LanguageChange:
        return False
    if window.property(APPLIED) == i18n.current():
        return True
    window.setProperty(APPLIED, i18n.current())
    return False
