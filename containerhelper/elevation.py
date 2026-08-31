"""Права администратора: определить и перезапросить.

Нужны из-за одного ключа: документация `/filesystem NTFS` говорит, что «a UAC
prompt will be displayed unless the process is run with full administrative
privileges». На двадцати двух контейнерах это двадцать два запроса UAC подряд,
и сбор перестаёт быть автоматическим.

Сбросить права обратно нельзя — процесс с ними живёт до конца, поэтому
перезапуск предлагается, а не делается сам, и окно после него должно об этом
говорить вслух.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .paths import DATA_ARGUMENT, program_dir
from .sizes import IS_WINDOWS

#: Глагол ShellExecuteW, поднимающий запрос UAC.
RUNAS = "runas"

#: Всё, что ShellExecuteW возвращает не больше этого, — ошибка. Так описано в
#: документации самой функции: успех отдаёт «псевдо-HINSTANCE» больше 32.
SHELL_EXECUTE_OK = 32


def is_admin() -> bool:
    """Запущены ли мы с полными правами администратора."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def relaunch_arguments(data_dir: Path | None) -> tuple[str, list[str]]:
    """Что запускать при перезапуске: сам exe в сборке, python -m в исходниках.

    Папка данных передаётся явным `--data`: у поднятого процесса другое
    окружение, и полагаться на то, что он найдёт ту же папку сам, нельзя —
    портативность на этом бы и кончилась.
    """
    arguments: list[str] = []
    if not getattr(sys, "frozen", False):
        arguments += ["-m", "containerhelper"]
    if data_dir is not None:
        arguments += [DATA_ARGUMENT, str(data_dir)]
    return sys.executable, arguments


def quote(arguments: list[str]) -> str:
    """Склеить аргументы в одну строку: ShellExecuteW принимает только её."""
    return " ".join(
        f'"{item}"' if " " in item or not item else item for item in arguments
    )


def relaunch_as_admin(data_dir: Path | None = None) -> bool:
    """Запросить перезапуск с правами администратора.

    True — запрос принят и новая копия пошла подниматься; вызывающий обязан
    закрыть текущую, иначе две копии станут писать в одну папку данных.
    """
    if not IS_WINDOWS:
        return False
    import ctypes

    executable, arguments = relaunch_arguments(data_dir)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            RUNAS,
            executable,
            quote(arguments),
            str(program_dir()),
            1,  # SW_SHOWNORMAL
        )
    except (OSError, AttributeError):
        return False
    return int(result) > SHELL_EXECUTE_OK
