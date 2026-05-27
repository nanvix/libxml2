# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for libxml2.

Usage:
    ./z setup     # Download Nanvix sysroot
    ./z build     # Cross-compile libxml2.a
    ./z test      # Run functional tests
    ./z release   # Package release tarball
    ./z clean     # Remove build artifacts
"""

import shutil
import sys
import tempfile
from pathlib import Path

from nanvix_zutil import (
    CFG_SYSROOT,
    DockerConfig,
    EXIT_MISSING_DEP,
    TOOLCHAIN_CONTAINER_PATH,
    ZScript,
    log,
    make_initrd,
    run,
)

IS_WINDOWS = sys.platform == "win32"

_MAKE_VAR_HOME = "NANVIX_HOME"
_MAKE_VAR_BUILDROOT = "NANVIX_BUILDROOT"
_MAKE_VAR_TOOLCHAIN = "NANVIX_TOOLCHAIN"
_MAKE_VAR_PLATFORM = "PLATFORM"
_MAKE_VAR_PROCESS_MODE = "PROCESS_MODE"
_MAKE_VAR_MEMORY_SIZE = "MEMORY_SIZE"


class Libxml2Build(ZScript):
    """Build script for nanvix/libxml2."""

    # Files produced by the cross-compile step that must be copied back
    # from the container's build directory into the host workspace. The
    # zutils Docker wrapper builds in a container-local scratch dir for
    # performance (especially on Windows), so anything not listed here
    # is discarded when the container exits.
    _BUILD_OUTPUTS: tuple[str, ...] = (
        ".libs/libxml2.a",
        "include/libxml/xmlversion.h",
        "test_libxml2.elf",
    )

    def docker_config(self, image: str) -> DockerConfig:
        cfg = super().docker_config(image)
        cfg.output_files = list(self._BUILD_OUTPUTS)
        return cfg

    def _make_args(self, *targets: str) -> list[str]:
        """Build the common make argument list."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first to download the sysroot.",
            )
        toolchain_p = str(TOOLCHAIN_CONTAINER_PATH)
        sysroot_p = (
            self.docker.translate_path(Path(sysroot)) if self.docker else Path(sysroot)
        )

        # Buildroot contains dependency libraries (zlib).
        buildroot_dir = self.nanvix_dir / "buildroot"
        if buildroot_dir.is_dir():
            buildroot_p = (
                self.docker.translate_path(buildroot_dir)
                if self.docker
                else buildroot_dir
            )
        else:
            buildroot_p = sysroot_p

        args = [
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            f"{_MAKE_VAR_HOME}={sysroot_p}",
            f"{_MAKE_VAR_BUILDROOT}={buildroot_p}",
            f"{_MAKE_VAR_TOOLCHAIN}={toolchain_p}",
        ]

        args.extend(
            [
                f"{_MAKE_VAR_PLATFORM}={self.config.machine}",
                f"{_MAKE_VAR_PROCESS_MODE}={self.config.deployment_mode}",
                f"{_MAKE_VAR_MEMORY_SIZE}={self.config.memory_size}",
            ]
        )

        args.extend(targets)
        return args

    def build(self) -> None:
        """Cross-compile libxml2.a for Nanvix."""
        run(*self._make_args("all"), cwd=self.repo_root, docker=self.docker)

    # Test targets accepted by `./z test` on paths that bypass the
    # Makefile (Windows and standalone Linux). Both aliases run the same
    # functional test, but we accept either to mirror Makefile naming.
    _SUPPORTED_TEST_TARGETS: frozenset[str] = frozenset({"test", "test-functional"})

    def _validate_test_targets(self) -> None:
        """Fail fast on unsupported `./z test <target>` arguments.

        Only the functional test is supported on Windows and in
        standalone mode; honoring stale Makefile-only targets would
        silently no-op.
        """
        if not self.targets:
            return
        unsupported = [t for t in self.targets if t not in self._SUPPORTED_TEST_TARGETS]
        if unsupported:
            allowed = ", ".join(sorted(self._SUPPORTED_TEST_TARGETS))
            log.fatal(
                f"Unsupported test target(s): {', '.join(unsupported)}.",
                code=EXIT_MISSING_DEP,
                hint=f"Supported targets: {allowed}.",
            )

    def test(self) -> None:
        """Run the libxml2 functional test suite.

        Functional tests run the test ELF under nanvixd. They exercise the
        full library and are the only tests supported across all platforms.
        """
        if IS_WINDOWS:
            self._validate_test_targets()
            self._run_tests_windows()
            return

        if self.config.deployment_mode == "standalone":
            self._validate_test_targets()
            self._run_functional_standalone()
        else:
            targets = self.targets if self.targets else ["test"]
            run(
                *self._make_args(*targets),
                cwd=self.repo_root,
            )

    def _get_sysroot(self) -> str:
        """Return the sysroot path or fatal if unset."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first to download the sysroot.",
            )
        return sysroot

    def _run_functional_standalone(self) -> None:
        """Run the standalone functional test using make_initrd.

        Creates an initrd bundling test_libxml2.elf with system daemons via
        make_initrd, and a ramfs providing /tmp for test file output.
        """
        binary = self.repo_root / "test_libxml2.elf"
        if not binary.is_file():
            log.fatal(
                "test_libxml2.elf not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z build` first.",
            )

        print("=== libxml2 functional tests ===")
        print("  Running test_libxml2.elf via nanvixd standalone...")

        sysroot_path = Path(self._get_sysroot())
        mkramfs = sysroot_path / "bin" / "mkramfs.elf"

        # Bundle test_libxml2.elf + daemons into an initrd.
        initrd = make_initrd(self, "test_libxml2.elf")

        try:
            with tempfile.TemporaryDirectory(prefix="nanvix_libxml2_") as tmpdir:
                tmpdir_path = Path(tmpdir)
                ramfs_dir = tmpdir_path / "ramfs"
                ramfs_dir.mkdir()
                (ramfs_dir / "tmp").mkdir(exist_ok=True)
                ramfs_img = tmpdir_path / "rootfs.img"

                run(
                    str(mkramfs),
                    "-o",
                    str(ramfs_img),
                    str(ramfs_dir),
                )

                # run() raises SystemExit on non-zero exit code,
                # and nanvixd propagates the guest's exit status, so
                # reaching the PASS line guarantees exit code 0.
                run(
                    str(sysroot_path / "bin" / "nanvixd.elf"),
                    "-bin-dir",
                    str(sysroot_path / "bin"),
                    "-ramfs",
                    str(ramfs_img),
                    "--",
                    str(initrd),
                    timeout=120,
                )
        finally:
            if initrd.exists():
                initrd.unlink()

        print("  PASS: test_libxml2 standalone (exit code 0)")
        print("  PASS: libxml2 functional tests")
        print("=== All libxml2 tests PASSED ===")

    def _run_tests_windows(self) -> None:
        """Run tests natively on Windows.

        Only standalone mode is tested on Windows; multi-process and
        single-process require linuxd, which is Linux-only. Uses
        make_initrd to bundle each test binary with system daemons,
        and a ramfs providing /tmp for any test I/O.
        """
        if self.config.deployment_mode != "standalone":
            print(
                f"Skipping tests on Windows for mode '{self.config.deployment_mode}' (requires linuxd)."
            )
            return

        sysroot_path = Path(self._get_sysroot())
        nanvixd = sysroot_path / "bin" / "nanvixd.exe"
        mkramfs = sysroot_path / "bin" / "mkramfs.exe"
        if not nanvixd.is_file():
            log.fatal(
                "nanvixd.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )
        if not mkramfs.is_file():
            log.fatal(
                "mkramfs.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )

        test_allowlist = {"test_libxml2.elf"}
        test_binaries: list[Path] = []
        for candidate in [self.repo_root, self.repo_root / "build"]:
            if candidate.is_dir():
                for elf in sorted(candidate.glob("*.elf")):
                    if elf.name in test_allowlist and elf.name not in {
                        x.name for x in test_binaries
                    }:
                        test_binaries.append(elf)

        if not test_binaries:
            expected = ", ".join(sorted(test_allowlist))
            log.fatal(
                f"No allowlisted test binaries found. Expected: {expected}.",
                code=EXIT_MISSING_DEP,
                hint="Build the test binaries first (run `./z build`) and then rerun `./z test`.",
            )

        failed: list[str] = []
        for binary in test_binaries:
            name = binary.stem
            print(f"RUN  {name}...")
            # make_initrd resolves binaries relative to repo_root;
            # copy the ELF there temporarily unless it already lives there.
            # This is a constraint of the make_initrd API; cleanup is
            # handled in the finally block below.
            repo_elf = self.repo_root / binary.name
            copied_elf = False
            initrd: Path | None = None
            try:
                if binary.resolve() != repo_elf.resolve():
                    if repo_elf.exists():
                        raise FileExistsError(
                            f"refusing to clobber existing {repo_elf}"
                        )
                    shutil.copy2(binary, repo_elf)
                    copied_elf = True
                initrd = make_initrd(self, binary.name)
                with tempfile.TemporaryDirectory(prefix=f"nanvix_{name}_") as tmpdir:
                    tmpdir_path = Path(tmpdir)
                    ramfs_dir = tmpdir_path / "ramfs"
                    ramfs_dir.mkdir()
                    (ramfs_dir / "tmp").mkdir(exist_ok=True)
                    ramfs_img = tmpdir_path / f"rootfs_{name}.img"

                    try:
                        run(
                            str(mkramfs),
                            "-o",
                            str(ramfs_img),
                            str(ramfs_dir),
                        )
                    except SystemExit:
                        print(f"FAIL {name} (mkramfs failed)")
                        failed.append(name)
                        continue

                    try:
                        run(
                            str(nanvixd),
                            "-bin-dir",
                            str(sysroot_path / "bin"),
                            "-ramfs",
                            str(ramfs_img),
                            "--",
                            str(initrd),
                            timeout=120,
                        )
                    except SystemExit:
                        print(f"FAIL {name} (nanvixd non-zero exit)")
                        failed.append(name)
                        continue
                print(f"OK   {name}")
            except FileExistsError as e:
                print(f"FAIL {name} ({e})")
                failed.append(name)
            finally:
                if initrd is not None and initrd.exists():
                    initrd.unlink()
                if copied_elf and repo_elf.exists():
                    repo_elf.unlink()

        if failed:
            msg = " ".join(failed)
            raise RuntimeError(f"{len(failed)} test(s) failed: {msg}")
        print(f"\t\t*** All {len(test_binaries)} tests PASSED ***")

    def release(self) -> None:
        """Package the libxml2 release tarball and verify it."""
        run(*self._make_args("package"), cwd=self.repo_root)
        run(*self._make_args("verify-package"), cwd=self.repo_root)

    def clean(self) -> None:
        """Remove build artifacts."""
        run(
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            "clean",
            cwd=self.repo_root,
        )


if __name__ == "__main__":
    Libxml2Build.main()
