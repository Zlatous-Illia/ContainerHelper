"""The suite runs in Russian whatever the machine's language.

The program starts in the system language, and the tests compare shown text
against Russian; on an English Windows they would fail for no reason.
"""

from containerhelper import i18n

i18n.set_language("ru")
