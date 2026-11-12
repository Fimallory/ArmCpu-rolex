# ArmCpu ABI compilation

The `ArmCpu ABI compile` workflow compiles this independent fork against the
reviewed rolex XNU configuration on `macos-26`. It does not build or boot a device
image. A successful result is **Lv1: compiled**, not proof of GIC, timer, PSCI, or
single-core operation on hardware.

## Run on GitHub Actions

Publish this workflow on the fork's default branch and enable Actions. Publish
the reviewed device and SoC commits before dispatching it. Select the fork ref
to compile and supply:

| Input | Default | Purpose |
| --- | --- | --- |
| `device_ref` | `cfac3c87ba6a9e431e5b0f7c9ae390a75a63e27d` | Full reviewed commit of `Fimallory/UniXNU-rolex` |
| `soc_ref` | `561cf9fe9b404c750173fcbeefc81d2cb74aa66e` | Full reviewed commit of `Fimallory/UniXNU-msm8917-common` |
| `xcode` | `26.6` | Installed `/Applications/Xcode_<version>.app` |

UniXNU itself is pinned to `67df14de69ae6f6ffc3480b30b70ce7667a3bd32`.
The workflow fails before compilation if the selected Xcode is absent. Device
and SoC inputs must be 40-character commit hashes. Update their defaults when
new reviewed inputs are published; no branch head is silently substituted.

```sh
gh workflow run abi-compile.yml --repo OWNER/FORK --ref main \
  -f device_ref=cfac3c87ba6a9e431e5b0f7c9ae390a75a63e27d \
  -f soc_ref=561cf9fe9b404c750173fcbeefc81d2cb74aa66e \
  -f xcode=26.6
```

Replace `OWNER/FORK` and `main` with the published fork and selected branch. No dispatch
or remote build is performed merely by adding these files.

## What runs

1. Check out the fork, pinned UniXNU, device, and SoC in sibling directories.
2. Install `cmake`, `ninja`, and `unifdef`; use Xcode's compiler and archive tools.
3. Resolve the board with explicit local `ArmCpu` and SoC overrides. Initialize
   only selected component submodule paths, never all UniXNU submodules.
4. Fetch XNU, libplatform, libdispatch, and AvailabilityVersions. Assemble the
   target tree and apply the selected architecture/SoC/OS/device patch series.
   The existing header preparation restores omitted DriverKit and TrustCache
   headers using the job's GitHub token, without printing the token.
5. Run `build-xnu-4k-baseline.sh` with `XNU_BOOTSTRAP=1` and
   `XNU_BOOT_PROGRESS=0` to generate the actual compile records. Mockfs is
   disabled only in the generated compile-only configuration, avoiding its
   fixture archive prerequisite without changing the board source config.
6. Require valid `startup.o.json`, `IOPlatformExpert.cpo.json`, and a nonempty
   `link.filelist`. A nonzero bootstrap is accepted only for make exit code 2
   with ARM64 undefined-symbol linker diagnostics and the failed kernel Mach-O
   target; source compiler errors or other failures remain fatal. The report
   records the bootstrap exit code separately from the ArmCpu compile result.
7. Run `build-arm-driver.py ArmCpu` with the same XNU environment and stop.

The bootstrap compiles XNU objects and may attempt its incomplete kernel link.
Missing external archives are expected at that link; it does not qualify a
complete kernel. No auxiliary crypto/pthread archives, prelink, IPSW members,
kernelcache, payload, firmware, tests, runtime clients, or device commands run.

## Artifacts

The fixed `armcpu-abi-compile` artifact contains only:

- `libArmCpu.a` and `ArmCpu.o` built from this open-source fork;
- `compile-command.json` and `build-report.json`;
- preparation, bootstrap, and ArmCpu compiler logs.

Artifacts expire after seven days. No XNU objects, kernel images, kernelcaches,
IPSW files, or firmware inputs are uploaded. GitHub token values are redacted
from staged text logs and reports. On a failed build, inspect `prepare.log`,
`kernel-bootstrap.log`, then `armcpu-driver.log` / `compile.log`.

## Invoke the helper on macOS

With the four checkouts prepared as siblings:

```sh
export DEVELOPER_DIR=/Applications/Xcode_26.6.app/Contents/Developer
python3 ArmCpu/scripts/ci-compile-armcpu.py \
  --unixnu UniXNU --device UniXNU-rolex --soc UniXNU-msm8917-common \
  --armcpu ArmCpu -j 4
```

Default outputs are `ArmCpu/out/abi-work/` and `ArmCpu/out/abi-compile/`.
Use fresh `--out` and `--artifacts` paths for another invocation; the helper
refuses existing build trees and nonempty artifact directories instead of
deleting prior work or accepting stale records. This helper does not install
the Linux toolchain and does not support native Windows.
