"""Finding VeraCrypt, the command lines, and checking the result by the facts.

The real VeraCrypt is not run here: it needs an installation, administrator
rights and minutes per container. What is checked is what does not depend on
it, and several hours of work hang on a single wrong switch.
"""

import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from containerhelper.model import VC_HEADERS_BYTES
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
    """Process runs and volumes that do not exist. Remembers all it is told."""

    def __init__(self, size_of=lambda mib: mib * MIB):
        self.commands: list[list[str]] = []
        self.drives = ["C:"]
        self.pauses = 0
        self.slept = 0.0
        self.size_of = size_of
        #: What not to do: 'create', 'mount', 'unmount'.
        self.broken: set[str] = set()
        #: How much the filesystem "eats" on the mounted volume. Changed in
        #: tests to tell the two ways of creating a container apart.
        self.ntfs = 17_879_040
        self.total = 0
        self.volume = 0
        self.cluster_bytes = 4096
        #: What the appearance of files costs on the fake volume. The numbers
        #: are the same as in the default model: what matters is not the value
        #: but that the measurement returns exactly what was put in.
        self.slack_base = 192 * 1024
        self.slack_per_file = 1280
        #: A folder standing in for the volume root. It is made next to the
        #: container when the container is created and survives unmounting:
        #: left space is measured on a freshly mounted volume, and the data
        #: must still be there.
        self.volume_dir: Path | None = None
        #: How many first unmount attempts the volume "refuses". That is how it
        #: behaves right after data is written: VeraCrypt refuses with code 1,
        #: at once, and half a minute later lets go. `/force` always takes
        #: it; that is what force is for.
        self.stubborn = 0

    # --- stands in for run_command ----------------------------------------

    def run(self, command, timeout):
        command = list(command)
        self.commands.append(command)
        if "/create" in command:
            self._create(command)
        elif "/unmount" in command or "/dismount" in command:
            # Both forms: 1.26 understands the old one too, just calls it
            # deprecated.
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
        # A fresh container is empty, like a real one after formatting.
        self.volume_dir = path.with_name(path.stem + "-volume")
        if self.volume_dir.exists():
            shutil.rmtree(self.volume_dir)
        self.volume_dir.mkdir(parents=True)

    def _mount(self, command):
        if "mount" in self.broken:
            return
        letter = command[command.index("/letter") + 1]
        path = Path(command[command.index("/volume") + 1])
        # The capacity is one cluster short of the volume, as on real NTFS.
        self.volume = path.stat().st_size - VC_HEADERS_BYTES
        self.total = self.volume - 4096
        self.drives.append(f"{letter}:")

    def _occupied(self) -> int:
        """How much the files put on the fake volume took.

        By clusters plus the built-in slack: exactly the value the
        measurement must give back.
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
            # The volume is busy: VeraCrypt refuses at once, with a non-zero
            # code.
            self.stubborn -= 1
            return 1
        switch = "/unmount" if "/unmount" in command else "/dismount"
        letter = command[command.index(switch) + 1]
        if f"{letter}:" in self.drives:
            self.drives.remove(f"{letter}:")
        return 0

    # --- stands in for waiting and for reading the volume -----------------

    def pause(self, seconds):
        self.pauses += 1
        #: How much time has "passed". The tests do not really sleep, but
        #: there is no other way to check that a refusal is not waited out
        #: for a minute.
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
        """Free space is computed, not stored.

        A stored value would not change when files are written, and the
        left-space measurement would return the same as the empty-volume one.
        """
        return self.volume - self.ntfs - self._occupied()


class DiscoveryTests(unittest.TestCase):
    """Standard locations, portable names and a folder given by hand."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name) / "VeraCrypt"

    def tearDown(self):
        self._dir.cleanup()

    def test_standard_dirs_name_both_program_files(self):
        """That is exactly where VeraCrypt installs."""
        found = {str(path) for path in standard_dirs()}
        for expected in FALLBACK_DIRS:
            self.assertIn(expected, found)

    def test_standard_dirs_have_no_repeats(self):
        """On 64-bit Windows ProgramFiles and ProgramW6432 are the same."""
        paths = [str(path).lower() for path in standard_dirs()]
        self.assertEqual(len(paths), len(set(paths)))

    def test_installed_build_is_found(self):
        make_install(self.folder, "VeraCrypt Format.exe", "VeraCrypt.exe")
        install = install_at(self.folder)
        self.assertIsNotNone(install)
        self.assertEqual(install.format_exe.name, "VeraCrypt Format.exe")
        self.assertEqual(install.mount_exe.name, "VeraCrypt.exe")

    def test_portable_build_with_architecture_suffix_is_found(self):
        """Portable builds have other names; the search must know both."""
        make_install(self.folder, "VeraCrypt Format-x64.exe", "VeraCrypt-x64.exe")
        install = install_at(self.folder)
        self.assertIsNotNone(install)
        self.assertEqual(install.mount_exe.name, "VeraCrypt-x64.exe")

    def test_arm64_names_are_known_too(self):
        make_install(self.folder, "VeraCrypt Format-arm64.exe", "VeraCrypt-arm64.exe")
        self.assertIsNotNone(install_at(self.folder))

    def test_half_an_installation_is_not_an_installation(self):
        """Creating and mounting are different binaries; both are needed."""
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
    """By default the objects take the real functions, not own descriptors."""

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
    """The switches are checked against the VeraCrypt 1.26.24 documentation."""

    def setUp(self):
        self.install = Install(
            directory=Path(r"C:\Program Files\VeraCrypt"),
            format_exe=Path(r"C:\Program Files\VeraCrypt\VeraCrypt Format.exe"),
            mount_exe=Path(r"C:\Program Files\VeraCrypt\VeraCrypt.exe"),
        )
        self.path = Path(r"C:\Temp\test.hc")

    def test_create_passes_the_size_in_exact_bytes(self):
        """The G suffix would round, and the measurement would miss its row."""
        command = create_command(self.install, self.path, 1024 * MIB)
        self.assertEqual(command[command.index("/size") + 1], "1073741824")

    def test_create_carries_the_mandatory_switches(self):
        command = create_command(self.install, self.path, MIB)
        for switch in ("/create", "/nosizecheck", "/force", "/silent"):
            self.assertIn(switch, command)
        self.assertEqual(command[command.index("/filesystem") + 1], "NTFS")
        self.assertEqual(command[command.index("/hash") + 1], "sha512")

    def test_nosizecheck_is_there_on_purpose(self):
        """Without it a terabyte dynamic container is simply not created."""
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
        """VeraCrypt Format.exe has no /pim switch at all."""
        self.assertNotIn("/pim", create_command(self.install, self.path, MIB))

    def test_creation_and_mounting_use_different_binaries(self):
        create = create_command(self.install, self.path, MIB)
        mount = mount_command(self.install, self.path, "Z")
        self.assertEqual(create[0], str(self.install.format_exe))
        self.assertEqual(mount[0], str(self.install.mount_exe))

    def test_mounting_names_the_hash(self):
        """Without /hash VeraCrypt tries every PRF in turn; that is slow."""
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
        """With a short password VeraCrypt shows a warning, and in silent mode
        a warning is a silent refusal."""
        self.assertGreaterEqual(len(CALIBRATION_PASSWORD), 20)


class OldVersionTests(unittest.TestCase):
    """VeraCrypt switches have changed; that is why the version is read."""

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
        """/unmount came in 1.26.20; older versions simply do not know it."""
        self.assertEqual(self.switch("1.25.9"), "/dismount")
        self.assertEqual(self.switch("1.26.7"), "/dismount")

    def test_unmount_is_used_from_the_rename_on(self):
        self.assertEqual(self.switch("1.26.20"), "/unmount")
        self.assertEqual(self.switch("1.26.24"), "/unmount")

    def test_an_unreadable_version_falls_back_to_the_old_switch(self):
        """All versions, the latest too, understand /dismount; not /unmount."""
        self.assertEqual(self.switch(""), "/dismount")

    def test_a_version_too_old_for_the_run_is_refused(self):
        supported, notice = version_notice(self.install("1.23"))
        self.assertFalse(supported)
        self.assertIn("1.24", notice)

    def test_the_oldest_workable_version_is_allowed_with_a_warning(self):
        """Before 1.25.4 /silent kept the wait window, but collection runs."""
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
    """The result is checked by the facts: /silent is silent on errors too."""

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
        """Otherwise the measurement goes into the wrong coverage table row."""
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
        """While the volume is up, the container file cannot be deleted."""
        self.vc.create(self.path, MIB)
        self.vc.mount(self.path, "Z")
        self.fake.broken.add("unmount")
        with self.assertRaises(VeraCryptError):
            self.vc.unmount("Z")

    def test_quiet_unmount_says_nothing_about_a_volume_that_is_not_there(self):
        self.assertTrue(self.vc.unmount_quietly("Z"))
        self.assertEqual(self.fake.commands, [])

    def test_quiet_unmount_swallows_the_failure(self):
        """In finally, a cleanup error must not mask the real one."""
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
    """Otherwise terabyte files surviving a crash would pile up silently."""

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
    """A refusal to unmount is temporary; a retry clears it, not waiting.

    On the first real copy-slack collection run four measurements of seven
    failed this way: VeraCrypt refused with code 1 right after data was
    written, and half a minute later released the volume without objection.
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
        """The volume is not unmounting slowly; it was not released at all."""
        self.fake.stubborn = 1
        self.vc.unmount("Z")
        self.assertLess(self.fake.slept, LETTER_TIMEOUT)

    def test_a_volume_that_goes_at_once_costs_no_retries(self):
        self.vc.unmount("Z")
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(self.fake.slept, 0.0)

    def test_the_measuring_unmount_never_forces(self):
        """Force would drop the cache, and left space would read too high."""
        self.vc.unmount("Z")
        self.assertNotIn("/force", self.attempts()[0])

    def test_the_cleanup_asks_politely_before_forcing(self):
        """VeraCrypt swallows an unknown switch silently: never force first."""
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
