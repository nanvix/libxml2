# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for libxml2.

Usage:
    ./z setup     # Download Nanvix sysroot
    ./z build     # Cross-compile libxml2.a
    ./z test      # Run test suite (smoke + integration + functional)
    ./z release   # Package release tarball
    ./z clean     # Remove build artifacts
"""

import shutil
import sys
import tempfile
from pathlib import Path

from nanvix_zutil import (
    CFG_DOCKER_IMAGE,
    CFG_SYSROOT,
    TOOLCHAIN_CONTAINER_PATH,
    EXIT_MISSING_DEP,
    ZScript,
    log,
)

IS_WINDOWS = sys.platform == "win32"

_MAKE_VAR_CONFIG = "CONFIG_NANVIX"
_MAKE_VAR_HOME = "NANVIX_HOME"
_MAKE_VAR_BUILDROOT = "NANVIX_BUILDROOT"
_MAKE_VAR_TOOLCHAIN = "NANVIX_TOOLCHAIN"
_MAKE_VAR_DOCKER_IMAGE = "NANVIX_DOCKER_IMAGE"
_MAKE_VAR_PLATFORM = "PLATFORM"
_MAKE_VAR_PROCESS_MODE = "PROCESS_MODE"
_MAKE_VAR_MEMORY_SIZE = "MEMORY_SIZE"


class Libxml2Build(ZScript):
    """Build script for nanvix/libxml2."""

    def _make_args(self, *targets: str) -> list[str]:
        """Build the common make argument list."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first to download the sysroot.",
            )
        toolchain = str(TOOLCHAIN_CONTAINER_PATH)
        sysroot_p = self.translate_path(Path(sysroot))
        toolchain_p = toolchain

        # Buildroot contains dependency libraries (zlib).
        buildroot_dir = self.nanvix_dir / "buildroot"
        if buildroot_dir.is_dir():
            buildroot_p = self.translate_path(buildroot_dir)
        else:
            buildroot_p = sysroot_p

        args = [
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            f"{_MAKE_VAR_CONFIG}=y",
            f"{_MAKE_VAR_HOME}={sysroot_p}",
            f"{_MAKE_VAR_BUILDROOT}={buildroot_p}",
            f"{_MAKE_VAR_TOOLCHAIN}={toolchain_p}",
        ]

        # Forward the Docker image from nanvix-zutil config so the
        # Makefile's Docker autodetection uses the same image that was
        # pulled during setup (important for CI where the reusable
        # workflow controls which image is available).
        docker_image = self.config.get(CFG_DOCKER_IMAGE, "")
        if docker_image:
            args.append(f"{_MAKE_VAR_DOCKER_IMAGE}={docker_image}")

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
        self.run(*self._make_args("all"), cwd=self.repo_root)

    def test(self) -> None:
        """Run the libxml2 test suite.

        Smoke and integration tests are always delegated to the Makefile.
        The functional test in standalone mode is handled in Python via
        make_initrd so that initrd creation is shared across platforms.
        """
        if IS_WINDOWS:
            self._run_tests_windows()
            return

        if self.config.deployment_mode == "standalone":
            targets = self.targets if self.targets else []
            # Targets that require the Python functional path.
            _functional_targets = {"test", "test-functional"}
            needs_functional = not targets or bool(set(targets) & _functional_targets)
            # Delegate non-functional targets to the Makefile.
            make_targets = [t for t in targets if t not in _functional_targets]
            if not targets:
                make_targets = ["test-smoke", "test-integration"]
            elif needs_functional and not make_targets:
                # Ensure Makefile prerequisites run when only functional
                # targets are requested (build + smoke/integration).
                if "test" in targets:
                    make_targets = ["test-smoke", "test-integration"]
                else:
                    make_targets = ["test-integration"]
            if make_targets:
                self.run(*self._make_args(*make_targets), cwd=self.repo_root)
            if needs_functional:
                self._run_functional_standalone()
        else:
            targets = self.targets if self.targets else ["test"]
            self.run(*self._make_args(*targets), cwd=self.repo_root)

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
        initrd = self.make_initrd("test_libxml2.elf")

        try:
            with tempfile.TemporaryDirectory(prefix="nanvix_libxml2_") as tmpdir:
                tmpdir_path = Path(tmpdir)
                ramfs_dir = tmpdir_path / "ramfs"
                ramfs_dir.mkdir()
                (ramfs_dir / "tmp").mkdir(exist_ok=True)
                ramfs_img = tmpdir_path / "rootfs.img"

                self.run(
                    str(mkramfs),
                    "-o",
                    str(ramfs_img),
                    str(ramfs_dir),
                    docker=False,
                )

                # self.run() raises SystemExit on non-zero exit code,
                # and nanvixd propagates the guest's exit status, so
                # reaching the PASS line guarantees exit code 0.
                self.run(
                    str(sysroot_path / "bin" / "nanvixd.elf"),
                    "-bin-dir",
                    str(sysroot_path / "bin"),
                    "-ramfs",
                    str(ramfs_img),
                    "--",
                    str(initrd),
                    docker=False,
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
                initrd = self.make_initrd(binary.name)
                with tempfile.TemporaryDirectory(prefix=f"nanvix_{name}_") as tmpdir:
                    tmpdir_path = Path(tmpdir)
                    ramfs_dir = tmpdir_path / "ramfs"
                    ramfs_dir.mkdir()
                    (ramfs_dir / "tmp").mkdir(exist_ok=True)
                    ramfs_img = tmpdir_path / f"rootfs_{name}.img"

                    try:
                        self.run(
                            str(mkramfs),
                            "-o",
                            str(ramfs_img),
                            str(ramfs_dir),
                            docker=False,
                        )
                    except SystemExit:
                        print(f"FAIL {name} (mkramfs failed)")
                        failed.append(name)
                        continue

                    try:
                        self.run(
                            str(nanvixd),
                            "-bin-dir",
                            str(sysroot_path / "bin"),
                            "-ramfs",
                            str(ramfs_img),
                            "--",
                            str(initrd),
                            docker=False,
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
        self.run(*self._make_args("package"), cwd=self.repo_root)
        self.run(*self._make_args("verify-package"), cwd=self.repo_root)

    def clean(self) -> None:
        """Remove build artifacts."""
        self.run(
            "make",
            "-f",
            ".nanvix/Makefile.nanvix",
            "clean",
            cwd=self.repo_root,
        )


if __name__ == "__main__":
    Libxml2Build.main()
