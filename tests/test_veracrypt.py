"""Поиск VeraCrypt, командные строки и проверка результата по факту.

Настоящий VeraCrypt здесь не запускается: он требует установки, прав
администратора и минут на каждый контейнер. Проверяется то, что от него не
зависит, — а зависит от одного неверного ключа несколько часов работы.
"""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from containerhelper.veracrypt import (
    CALIBRATION_PASSWORD,
    LETTER_TIMEOUT,
    UNMOUNT_ATTEMPTS,
    CONTAINER_PREFIX,
    FALLBACK_DIRS,
    Install,
    VeraCrypt,
    VeraCryptError,
    Volumes,
    container_name,
    create_command,
    find_install,
    install_at,
    missing_report,
    mount_command,
    orphans,
    parse_version,
    remove_container,
    standard_dirs,
    unmount_command,
    version_notice,
)

MIB = 1024 * 1024


def make_install(folder: Path, format_name: str, mount_name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in (format_name, mount_name):
        (folder / name).write_text("", encoding="utf-8")
    return folder


class Fake:
    """Запуск процессов и тома, которых нет. Помнит всё, что ей сказали."""

    def __init__(self, size_of=lambda mib: mib * MIB):
        self.commands: list[list[str]] = []
        self.drives = ["C:"]
        self.pauses = 0
        self.slept = 0.0
        self.size_of = size_of
        #: Что не делать: 'create', 'mount', 'unmount'.
        self.broken: set[str] = set()
        #: Сколько «съест» файловая система на смонтированном томе. Меняется
        #: в тестах, чтобы развести два способа создания контейнера.
        self.ntfs = 17_879_040
        self.total = 0
        self.cluster_bytes = 4096
        #: Чем оборачивается появление файлов на поддельном томе. Числа те же,
        #: что в модели по умолчанию, — важно не значение, а то, что замер
        #: возвращает ровно заложенное.
        self.slack_base = 192 * 1024
        self.slack_per_file = 1280
        #: Папка, изображающая корень тома. Заводится при создании контейнера
        #: рядом с ним и переживает размонтирование: замер остатка снимается
        #: на свежесмонтированном томе, и данные обязаны там остаться.
        self.volume_dir: Path | None = None
        #: Сколько первых попыток размонтирования том «не отдаст». Так он
        #: ведёт себя сразу после записи данных: VeraCrypt отказывает кодом 1
        #: и немедленно, а через полминуты отпускает. `/force` берёт его
        #: всегда — на то он и force.
        self.stubborn = 0

    # --- подставляется вместо run_command ---------------------------------

    def run(self, command, timeout):
        command = list(command)
        self.commands.append(command)
        if "/create" in command:
            self._create(command)
        elif "/unmount" in command or "/dismount" in command:
            # Обе формы: 1.26 понимает и старую, просто ругает её устаревшей.
            return self._unmount(command)
        elif "/volume" in command:
            self._mount(command)
        return 0

    def _create(self, command):
        if "create" in self.broken:
            return
        path = Path(command[command.index("/create") + 1])
        size = int(command[command.index("/size") + 1])
        with open(path, "wb") as handle:
            handle.truncate(size)
        # Свежий контейнер пуст — как и настоящий после форматирования.
        self.volume_dir = path.with_name(path.stem + "-volume")
        if self.volume_dir.exists():
            shutil.rmtree(self.volume_dir)
        self.volume_dir.mkdir(parents=True)

    def _mount(self, command):
        if "mount" in self.broken:
            return
        letter = command[command.index("/letter") + 1]
        path = Path(command[command.index("/volume") + 1])
        self.total = path.stat().st_size - 266_240
        self.drives.append(f"{letter}:")

    def _occupied(self) -> int:
        """Сколько заняли файлы, положенные на поддельный том.

        По кластерам плюс заложенный запас — ровно та величина, которую замер
        и должен вернуть обратно.
        """
        if self.volume_dir is None or not self.volume_dir.is_dir():
            return 0
        sizes = [
            item.stat().st_size
            for item in self.volume_dir.rglob("*")
            if item.is_file()
        ]
        if not sizes:
            return 0
        alloc = sum(
            -(-size // self.cluster_bytes) * self.cluster_bytes for size in sizes
        )
        return alloc + self.slack_base + self.slack_per_file * len(sizes)

    def _unmount(self, command):
        if "unmount" in self.broken:
            return 1
        if self.stubborn > 0 and "/force" not in command:
            # Том занят: VeraCrypt отказывает сразу и ненулевым кодом.
            self.stubborn -= 1
            return 1
        switch = "/unmount" if "/unmount" in command else "/dismount"
        letter = command[command.index(switch) + 1]
        if f"{letter}:" in self.drives:
            self.drives.remove(f"{letter}:")
        return 0

    # --- подставляется вместо ожидания и чтения тома ----------------------

    def pause(self, seconds):
        self.pauses += 1
        #: Сколько «прошло». По-настоящему тесты не спят, но проверить, что
        #: отказ не выжидается минутой, иначе нечем.
        self.slept += seconds

    def volumes(self) -> Volumes:
        return Volumes(
            drives=lambda: list(self.drives),
            usage=lambda drive: (self.total, self.free),
            cluster=lambda drive: self.cluster_bytes,
            filesystem=lambda drive: "NTFS",
            root=lambda drive: self.volume_dir,
        )

    @property
    def free(self) -> int:
        """Свободное место считается, а не запоминается.

        Запомненное не менялось бы от записи файлов, и замер остатка вернул
        бы то же, что замер пустого тома.
        """
        return self.total - self.ntfs - self._occupied()


class DiscoveryTests(unittest.TestCase):
    """Стандартные места, портативные имена и ручное указание папки."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name) / "VeraCrypt"

    def tearDown(self):
        self._dir.cleanup()

    def test_standard_dirs_name_both_program_files(self):
        """Именно туда VeraCrypt и ставится."""
        found = {str(path) for path in standard_dirs()}
        for expected in FALLBACK_DIRS:
            self.assertIn(expected, found)

    def test_standard_dirs_have_no_repeats(self):
        """На 64-битной Windows ProgramFiles и ProgramW6432 совпадают."""
        paths = [str(path).lower() for path in standard_dirs()]
        self.assertEqual(len(paths), len(set(paths)))

    def test_installed_build_is_found(self):
        make_install(self.folder, "VeraCrypt Format.exe", "VeraCrypt.exe")
        install = install_at(self.folder)
        self.assertIsNotNone(install)
        self.assertEqual(install.format_exe.name, "VeraCrypt Format.exe")
        self.assertEqual(install.mount_exe.name, "VeraCrypt.exe")

    def test_portable_build_with_architecture_suffix_is_found(self):
        """У портативной сборки имена другие, и поиск обязан знать оба вида."""
        make_install(self.folder, "VeraCrypt Format-x64.exe", "VeraCrypt-x64.exe")
        install = install_at(self.folder)
        self.assertIsNotNone(install)
        self.assertEqual(install.mount_exe.name, "VeraCrypt-x64.exe")

    def test_arm64_names_are_known_too(self):
        make_install(self.folder, "VeraCrypt Format-arm64.exe", "VeraCrypt-arm64.exe")
        self.assertIsNotNone(install_at(self.folder))

    def test_half_an_installation_is_not_an_installation(self):
        """Создание и монтирование — разные бинарники, нужны оба."""
        self.folder.mkdir(parents=True)
        (self.folder / "VeraCrypt.exe").write_text("", encoding="utf-8")
        self.assertIsNone(install_at(self.folder))
        self.assertIn("создания контейнеров", missing_report(self.folder))

    def test_pointing_at_the_exe_works_too(self):
        make_install(self.folder, "VeraCrypt Format.exe", "VeraCrypt.exe")
        install = install_at(self.folder / "VeraCrypt.exe")
        self.assertIsNotNone(install)
        self.assertEqual(install.directory, self.folder)

    def test_a_named_folder_wins_over_the_standard_places(self):
        make_install(self.folder, "VeraCrypt Format.exe", "VeraCrypt.exe")
        install = find_install([self.folder])
        self.assertEqual(install.directory, self.folder)

    def test_nothing_anywhere_means_none(self):
        self.assertIsNone(find_install([Path(self._dir.name) / "нет такой"]))

    def test_missing_report_names_an_absent_folder(self):
        self.assertIn("нет", missing_report(Path(self._dir.name) / "пусто"))

    def test_a_complete_folder_has_nothing_to_report(self):
        make_install(self.folder, "VeraCrypt Format.exe", "VeraCrypt.exe")
        self.assertEqual(missing_report(self.folder), "")


class DefaultWiringTests(unittest.TestCase):
    """Без подмены объекты берут настоящие функции, а не свои описатели."""

    def test_volumes_default_to_the_real_readers(self):
        from containerhelper.sizes import (
            cluster_size,
            mounted_drives,
            volume_filesystem,
            volume_usage,
        )

        volumes = Volumes()
        self.assertIs(volumes.drives, mounted_drives)
        self.assertIs(volumes.usage, volume_usage)
        self.assertIs(volumes.cluster, cluster_size)
        self.assertIs(volumes.filesystem, volume_filesystem)

    def test_veracrypt_defaults_to_the_real_launcher(self):
        from containerhelper.veracrypt import run_command

        install = Install(Path("."), Path("format.exe"), Path("mount.exe"))
        self.assertIs(VeraCrypt(install=install).run, run_command)


class CommandTests(unittest.TestCase):
    """Ключи сверены с документацией VeraCrypt 1.26.24."""

    def setUp(self):
        self.install = Install(
            directory=Path(r"C:\Program Files\VeraCrypt"),
            format_exe=Path(r"C:\Program Files\VeraCrypt\VeraCrypt Format.exe"),
            mount_exe=Path(r"C:\Program Files\VeraCrypt\VeraCrypt.exe"),
        )
        self.path = Path(r"C:\Temp\test.hc")

    def test_create_passes_the_size_in_exact_bytes(self):
        """Суффикс G округлил бы, и замер встал бы мимо своей строки."""
        command = create_command(self.install, self.path, 1024 * MIB)
        self.assertEqual(command[command.index("/size") + 1], "1073741824")

    def test_create_carries_the_mandatory_switches(self):
        command = create_command(self.install, self.path, MIB)
        for switch in ("/create", "/nosizecheck", "/force", "/silent"):
            self.assertIn(switch, command)
        self.assertEqual(command[command.index("/filesystem") + 1], "NTFS")
        self.assertEqual(command[command.index("/hash") + 1], "sha512")

    def test_nosizecheck_is_there_on_purpose(self):
        """Без него терабайтный динамический контейнер просто не создастся."""
        self.assertIn("/nosizecheck", create_command(self.install, self.path, MIB))

    def test_the_fast_variant_is_dynamic_and_quick(self):
        command = create_command(self.install, self.path, MIB)
        self.assertIn("/dynamic", command)
        self.assertIn("/quick", command)

    def test_the_slow_variant_drops_both(self):
        command = create_command(self.install, self.path, MIB, dynamic=False, quick=False)
        self.assertNotIn("/dynamic", command)
        self.assertNotIn("/quick", command)

    def test_format_never_gets_a_pim(self):
        """У VeraCrypt Format.exe ключа /pim нет вовсе."""
        self.assertNotIn("/pim", create_command(self.install, self.path, MIB))

    def test_creation_and_mounting_use_different_binaries(self):
        create = create_command(self.install, self.path, MIB)
        mount = mount_command(self.install, self.path, "Z")
        self.assertEqual(create[0], str(self.install.format_exe))
        self.assertEqual(mount[0], str(self.install.mount_exe))

    def test_mounting_names_the_hash(self):
        """Без /hash VeraCrypt перебирает все PRF подряд — это долго."""
        command = mount_command(self.install, self.path, "Z")
        self.assertEqual(command[command.index("/hash") + 1], "sha512")
        self.assertEqual(command[command.index("/letter") + 1], "Z")

    def test_a_current_version_gets_the_current_switch(self):
        command = unmount_command(replace(self.install, version="1.26.24"), "Z")
        self.assertIn("/unmount", command)
        self.assertNotIn("/dismount", command)

    def test_the_password_goes_in_as_given(self):
        command = mount_command(self.install, self.path, "Z", "секрет")
        self.assertEqual(command[command.index("/password") + 1], "секрет")

    def test_the_default_password_is_long_enough_to_pass_without_a_warning(self):
        """На коротком VeraCrypt показывает предупреждение, а в тихом режиме
        предупреждение — это молчаливый отказ."""
        self.assertGreaterEqual(len(CALIBRATION_PASSWORD), 20)


class OldVersionTests(unittest.TestCase):
    """Ключи VeraCrypt менялись; версия читается именно ради этого."""

    def install(self, version):
        return Install(
            directory=Path("."),
            format_exe=Path("VeraCrypt Format.exe"),
            mount_exe=Path("VeraCrypt.exe"),
            version=version,
        )

    def switch(self, version):
        command = unmount_command(self.install(version), "Z")
        return command[-2]

    def test_versions_parse_into_comparable_numbers(self):
        self.assertEqual(parse_version("1.26.24"), (1, 26, 24))
        self.assertEqual(parse_version("1.25.9.0"), (1, 25, 9, 0))
        self.assertEqual(parse_version("1.24-Update7"), (1, 24))
        self.assertEqual(parse_version(""), ())

    def test_dismount_is_used_before_the_rename(self):
        """/unmount появился в 1.26.20; до неё его просто не понимают."""
        self.assertEqual(self.switch("1.25.9"), "/dismount")
        self.assertEqual(self.switch("1.26.7"), "/dismount")

    def test_unmount_is_used_from_the_rename_on(self):
        self.assertEqual(self.switch("1.26.20"), "/unmount")
        self.assertEqual(self.switch("1.26.24"), "/unmount")

    def test_an_unreadable_version_falls_back_to_the_old_switch(self):
        """/dismount понимают все версии, включая свежую, /unmount — нет."""
        self.assertEqual(self.switch(""), "/dismount")

    def test_a_version_too_old_for_the_run_is_refused(self):
        supported, notice = version_notice(self.install("1.23"))
        self.assertFalse(supported)
        self.assertIn("1.24", notice)

    def test_the_oldest_workable_version_is_allowed_with_a_warning(self):
        """До 1.25.4 /silent не убирал окно ожидания — но сбор идёт."""
        supported, notice = version_notice(self.install("1.24"))
        self.assertTrue(supported)
        self.assertIn("окно", notice)

    def test_an_old_but_workable_version_says_which_switch_it_gets(self):
        supported, notice = version_notice(self.install("1.25.9"))
        self.assertTrue(supported)
        self.assertIn("/dismount", notice)

    def test_a_current_version_has_nothing_to_warn_about(self):
        self.assertEqual(version_notice(self.install("1.26.24")), (True, ""))

    def test_an_unreadable_version_is_allowed_but_mentioned(self):
        supported, notice = version_notice(self.install(""))
        self.assertTrue(supported)
        self.assertIn("/dismount", notice)


class OperationTests(unittest.TestCase):
    """Результат проверяется по факту: /silent молчит и об ошибке тоже."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = Path(self._dir.name)
        folder = make_install(
            self.workdir / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.fake = Fake()
        self.vc = VeraCrypt(
            install=install_at(folder),
            run=self.fake.run,
            pause=self.fake.pause,
            volumes=self.fake.volumes(),
        )
        self.path = self.workdir / container_name(1)

    def tearDown(self):
        self._dir.cleanup()

    def test_a_created_container_is_accepted_by_its_size(self):
        self.vc.create(self.path, MIB)
        self.assertEqual(self.path.stat().st_size, MIB)

    def test_a_missing_container_is_an_error_whatever_the_return_code(self):
        self.fake.broken.add("create")
        with self.assertRaises(VeraCryptError) as caught:
            self.vc.create(self.path, MIB)
        self.assertIn("не создан", str(caught.exception))

    def test_a_container_of_the_wrong_size_is_refused(self):
        """Иначе замер встал бы не в свою строку таблицы покрытия."""
        self.path.write_bytes(b"0" * 10)
        self.fake.broken.add("create")
        with self.assertRaises(VeraCryptError) as caught:
            self.vc.create(self.path, MIB)
        self.assertIn("вместо", str(caught.exception))

    def test_mounting_waits_for_the_letter(self):
        self.vc.create(self.path, MIB)
        self.vc.mount(self.path, "Z")
        self.assertIn("Z:", self.fake.drives)

    def test_a_letter_that_never_appears_is_an_error(self):
        self.fake.broken.add("mount")
        self.vc.create(self.path, MIB)
        with self.assertRaises(VeraCryptError) as caught:
            self.vc.mount(self.path, "Z")
        self.assertIn("не поднялся", str(caught.exception))
        self.assertGreater(self.fake.pauses, 0)

    def test_unmounting_waits_for_the_letter_to_go(self):
        self.vc.create(self.path, MIB)
        self.vc.mount(self.path, "Z")
        self.vc.unmount("Z")
        self.assertNotIn("Z:", self.fake.drives)

    def test_a_volume_that_stays_up_is_an_error(self):
        """Пока том поднят, файл контейнера удалить нельзя."""
        self.vc.create(self.path, MIB)
        self.vc.mount(self.path, "Z")
        self.fake.broken.add("unmount")
        with self.assertRaises(VeraCryptError):
            self.vc.unmount("Z")

    def test_quiet_unmount_says_nothing_about_a_volume_that_is_not_there(self):
        self.assertTrue(self.vc.unmount_quietly("Z"))
        self.assertEqual(self.fake.commands, [])

    def test_quiet_unmount_swallows_the_failure(self):
        """В finally ошибка уборки не должна заслонять настоящую."""
        self.vc.create(self.path, MIB)
        self.vc.mount(self.path, "Z")
        self.fake.broken.add("unmount")
        self.assertFalse(self.vc.unmount_quietly("Z"))

    def test_a_free_letter_is_picked_from_the_far_end(self):
        self.assertEqual(self.vc.free_letter(), "Z")

    def test_taken_letters_are_skipped(self):
        self.fake.drives.extend(["Z:", "Y:"])
        self.assertEqual(self.vc.free_letter(), "X")

    def test_no_letters_left_is_an_error(self):
        self.fake.drives.extend(f"{chr(code)}:" for code in range(ord("D"), ord("Z") + 1))
        with self.assertRaises(VeraCryptError):
            self.vc.free_letter()


class OrphanTests(unittest.TestCase):
    """Терабайтный файл, переживший падение, иначе копился бы молча."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def test_leftovers_are_recognised_by_their_name(self):
        (self.workdir / container_name(1024)).write_bytes(b"")
        (self.workdir / "чужой.hc").write_bytes(b"")
        found = [path.name for path in orphans(self.workdir)]
        self.assertEqual(found, [container_name(1024)])

    def test_the_name_carries_the_size(self):
        self.assertTrue(container_name(4096).startswith(CONTAINER_PREFIX))
        self.assertIn("4096", container_name(4096))

    def test_removing_a_missing_file_is_not_a_failure(self):
        self.assertTrue(remove_container(self.workdir / "нет.hc"))

    def test_an_absent_folder_has_no_orphans(self):
        self.assertEqual(orphans(self.workdir / "нет"), [])


class UnmountRetryTests(unittest.TestCase):
    """Отказ размонтировать временный, и снимает его повтор, а не ожидание.

    На первом настоящем прогоне сбора запаса так сорвались четыре замера из
    семи: VeraCrypt отказывала кодом 1 сразу после записи данных, а через
    полминуты отдавала том без возражений.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        folder = make_install(
            Path(self._dir.name) / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.fake = Fake()
        self.vc = VeraCrypt(
            install=install_at(folder),
            run=self.fake.run,
            pause=self.fake.pause,
            volumes=self.fake.volumes(),
        )
        self.fake.drives.append("Z:")

    def tearDown(self):
        self._dir.cleanup()

    def attempts(self):
        return [
            item
            for item in self.fake.commands
            if "/unmount" in item or "/dismount" in item
        ]

    def test_a_busy_volume_is_retried_until_it_lets_go(self):
        self.fake.stubborn = 2
        self.vc.unmount("Z")
        self.assertEqual(len(self.attempts()), 3)
        self.assertNotIn("Z:", self.fake.drives)

    def test_it_gives_up_after_the_last_attempt(self):
        self.fake.stubborn = 99
        with self.assertRaises(VeraCryptError) as caught:
            self.vc.unmount("Z")
        self.assertEqual(len(self.attempts()), UNMOUNT_ATTEMPTS)
        self.assertIn("попыт", str(caught.exception))

    def test_a_refusal_is_not_waited_out_for_a_whole_minute(self):
        """Том не размонтируется медленно — его не отдали вовсе."""
        self.fake.stubborn = 1
        self.vc.unmount("Z")
        self.assertLess(self.fake.slept, LETTER_TIMEOUT)

    def test_a_volume_that_goes_at_once_costs_no_retries(self):
        self.vc.unmount("Z")
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.fake.slept, 0.0)

    def test_the_measuring_unmount_never_forces(self):
        """Force отбросил бы кэш, и остаток вышел бы завышенным."""
        self.vc.unmount("Z")
        self.assertNotIn("/force", self.attempts()[0])

    def test_the_cleanup_asks_politely_before_forcing(self):
        """Незнакомый ключ VeraCrypt проглотит молча — начинать с силы нельзя."""
        self.fake.stubborn = 99
        self.vc.unmount_quietly("Z")
        forced = ["/force" in item for item in self.attempts()]
        self.assertEqual(forced, [False] * (UNMOUNT_ATTEMPTS - 1) + [True])

    def test_a_cleanup_that_goes_at_once_never_forces(self):
        self.vc.unmount_quietly("Z")
        self.assertNotIn("/force", self.attempts()[0])

    def test_force_takes_a_volume_that_refuses_everything_else(self):
        self.fake.stubborn = 99
        self.assertTrue(self.vc.unmount_quietly("Z"))
        self.assertNotIn("Z:", self.fake.drives)

    def test_an_already_gone_volume_is_not_touched(self):
        self.fake.drives.remove("Z:")
        self.assertTrue(self.vc.unmount_quietly("Z"))
        self.assertEqual(self.attempts(), [])


if __name__ == "__main__":
    unittest.main()
