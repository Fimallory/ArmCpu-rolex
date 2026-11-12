# GICv2 single-core interface

This independent fork keeps the `ArmCpu` source filename, runtime class, bundle
identifier, and exported C interfaces. It adds GICv2 to the existing GICv3
implementation; it does not replace the upstream component checkout.

## Device-tree contract

The driver consumes the Apple device tree, not a raw FDT. Integer fields below
are little-endian after the common FDT-to-ADT conversion.

| Input | Required representation |
| --- | --- |
| `/arm-io/ranges` | Three u64 values: child base, physical base, length |
| `/arm-io/intc/compatible` | Exactly one NUL-terminated `arm,gic-400` or `qcom,msm-qgic2` string |
| `/arm-io/intc/reg` | Exactly four u64 values, 32 bytes: distributor address/size, CPU-interface address/size |
| GICv2 register sizes | Both at least `0x1000`, contained in the bus window |
| `/arm-io/timer/compatible` | Exactly `arm,armv8-timer`, NUL-terminated |
| `/arm-io/timer/interrupts` | Exactly twelve u32 cells: secure physical, non-secure physical, virtual, hypervisor triplets |
| Timer triplets | Type 1, distinct PPI numbers 0–15, level trigger 4 or 8; only the low trigger nibble and PPI CPU-mask byte are allowed |
| Timer `interrupt-parent` | If present, a u32 matching the `intc` phandle |
| `/cpus/cpu0/reg` | One u32 holding the boot CPU MPIDR affinity, not a sequential logical ID |
| CPU topology | One XNU-visible logical CPU, numbered 0, matching the executing boot CPU |

The virtual timer INTID is `16 + interrupts[7]`. The MSM8917 inventory gives
triplets `<1 2 0xf08>, <1 3 0xf08>, <1 4 0xf08>, <1 1 0xf08>`, yielding INTIDs
18, 19, **20**, and 17 respectively. Its boot affinity is `0x100`; other physical
cores have affinities `0x101`–`0x103` but are not supported by this phase.

The source contains no MSM8917 physical-address constants. Measured distributor
`0x0b000000` and CPU-interface `0x0b002000` ranges belong in the SoC DTSI. Do not
append GICH/GICV virtualization regions to this driver's two-region contract.
Export the GIC and timer with `unixnu,adt-name = "intc"` and `"timer"`; nodes
without that property are not exported by the common converter. Disable the
other CPUs in the board DTS for a single-core ADT. PSCI `method = "smc"` remains
hardware information; this phase never issues CPU_ON for GICv2.

GICv3 still requires exact `arm,gic-v3` and distributor/redistributor regions of
at least `0x10000`/`0x20000`. An absent timer node retains virtual INTID 27 for
GICv3 only. A present timer node is validated for either controller version.

## Register operations

All GICv2 accesses are MMIO; no GICR or ICC system-register instruction is
executed on that branch. Architectural MPIDR and timer system registers remain
in use.

| Operation | GICv2 offset |
| --- | --- |
| Distributor control / type | `GICD_CTLR 0x000`, `GICD_TYPER 0x004` |
| Distributor architecture ID | `GICD_PIDR2 0x0fe8`, architecture nibble must be 2 |
| Group / enable / disable | `IGROUPR 0x080`, `ISENABLER 0x100`, `ICENABLER 0x180` |
| Pending / clear pending | `ISPENDR 0x200`, `ICPENDR 0x280` |
| Priority / target / trigger | `IPRIORITYR 0x400`, `ITARGETSR 0x800`, `ICFGR 0xc00` |
| SGI send / clear source pending | `SGIR 0xf00`, `CPENDSGIR 0xf10` |
| CPU control / priority mask / binary point | `GICC_CTLR 0x000`, `PMR 0x004`, `BPR 0x008` |
| CPU acknowledge / end interrupt | `GICC_IAR 0x00c`, `EOIR 0x010` |

The GICv3 architecture check remains at `0xffe8` with architecture nibble 3.
The implemented interrupt count is `min(32 * ((TYPER & 31) + 1), 1020)`. No
SPI register bit for reserved IDs 1020–1023 is written on the GICv2 path.

The driver uses the **non-secure** view of GICD/GICC control: bit 0 enables
Group 1. It disables that bit during initial local/distributor configuration;
SPI attachment disables the individual line before changing it. Boot
target routing is read from the banked ITARGETSR bytes for IDs 0–31; their union
must be a nonzero one-bit mask. MPIDR affinity is never turned into a GICv2 CPU
target mask.

Only SGI 0 and the virtual timer are accepted initially. PPIs are disabled and
cleared before timer configuration; SGI source-pending bytes are cleared. SGIs
are architecturally always enabled on GICv2, so unused SGIs get priority `0xff`
to mask them at PMR rather than assuming their enable bits can be cleared.
SGI 0 and the timer have priority `0x80`; the timer is level-triggered. CPU PMR
is written `0xff`, BPR is 0, firmware bypass-disable bits `0x1e0` are preserved,
and EOImodeNS bit 9 is cleared. EOIR therefore also deactivates the interrupt;
the second-page DIR register is not needed.

Readback checks timer/SGI priority, local enable state, timer trigger, distributor
enable, CPU-interface enable/EOImode, and a usable priority mask. IGROUPR writes
are attempted but its non-secure view may be RAZ/WI; a zero group readback alone
is not treated as failure. Enable/priority/target failures reject unusable or
secure-owned sources instead of pretending to transfer their ownership. Secure
firmware must already permit non-secure use of the required interrupt lines.

## Interrupt and exported interfaces

`IOCPUInterruptController::enableCPUInterrupt` installs the kernel callback with
`ml_install_interrupt_handler`. Its registered vector dispatches to
`ArmCpu::dispatch`, then `interrupt`. GICv2 dispatch decodes `IAR & 0x3ff` but
writes the **entire original IAR token** to EOIR, retaining SGI source bits
12:10. IDs 1020–1023 stop draining without an EOI. Unhandled implemented
interrupts are counted and disabled (unexpected SGIs are priority-masked).
Drain processing is bounded at 128
interrupts before a panic, independent of the optional diagnostics flag.

| Interface | GICv2 behavior |
| --- | --- |
| `signalCPU` | Only the calling boot CPU itself is accepted; SGIR target-list filter 2 sends SGI 0 |
| `startCPU` / secondary registration | Unsupported; no PSCI secondary start |
| `armcpu_spi_attach` | Implemented IDs 32 and above only; priority `0xa0`, requested edge/level mode, ITARGETSR byte set to the measured boot mask; ownership/readback failures return not-ready |
| `armcpu_spi_attach` router result | Zero-extended ITARGETSR byte, not an affinity or IROUTER value |
| `armcpu_spi_count` | Existing per-handler count API |
| `armcpu_gic_diag` | Read-only snapshot of the calling boot CPU's own bank; remote affinity/caller is rejected |
| `armcpu_sgi_selfprobe` | Unsupported: all ten output values are `UINT64_MAX`; no diagnostic interrupts or ICC reads |
| `armcpu_ipi_selftest` | Send-only self SGI 0 for a matching calling boot affinity; no remote send or SPI fallback |
| `quiesceCPU` | Masks/disables CNTV and clears GICC enable bit 0; no ICC write |

The `armcpu_gic_boot` array retains eight entries. For GICv2 they are IGROUPR0,
ISENABLER0, GICD_CTLR, GICD_TYPER, boot target mask, GICC_PMR, GICC_CTLR, GICC_BPR.
For `armcpu_gic_diag`, entries 3 and 4 are `UINT32_MAX` because GICv2 has neither
IGRPMODR nor GICR_WAKER. The unhandled counter also includes unexpected local
interrupts on GICv2 despite its retained `armcpu_spi_unhandled` symbol name.

## Timer ABI evidence

Pinned XNU `d8b80295118ef25ac3a784134bcf95cd8e88109f` uses the virtual timer:

- `osfmk/arm64/machine_routines.c`, `ml_set_decrementer`: the default writes
  `CNTV_TVAL_EL0`; `ml_get_decrementer` reads the same register.
- `_enable_virtual_timer` writes `CNTV_CTL_EL0` and masks `CNTP_CTL_EL0`.
- `ml_get_timer_pending` reads `CNTV_CTL_EL0`.
- `osfmk/arm/rtclock.c`, `setPop`: calls `ml_set_decrementer`; `rtclock_intr` has
  signature `void rtclock_intr(unsigned int)` and executes with IRQs disabled.
- `osfmk/arm64/sleh.c`, `sleh_irq`: the non-AppleSMP path invokes the per-CPU
  installed interrupt handler.

The port therefore retains `CNTV_CTL_EL0` and calls the real `rtclock_intr(0)`
for the virtual PPI. It does not switch to CNTP or invent a timer callback.
Kernel/firmware register access and the installed decrementer function must
match that ABI; those conditions need parent build and hardware evidence.

## Build integration and runtime policy

Keep canonical component selection `ArmCpu`, the `ArmCpu.cpp` filename, and
`org.blu-sharky.ArmCpu`. Point the component's **local source override** at this
independent checkout; the resolved tree then exposes it under `drivers/ArmCpu`.
The existing `build-arm-driver.py ArmCpu` recipe emits
`build/armcpu/libArmCpu.a`. That script currently has no source, recipe or archive
CLI override and the kernel script hardcodes the ArmCpu archive path. A custom
archive location needs parent build-tool changes; no such files are changed here.

Required integration flags are `CONFIG_XNU_ARMCPU=y`,
`CONFIG_XNU_ARM_DT_PLATFORM=y`, `CONFIG_XNU_NATIVE_ARCH_TIMER=y`, and archive
building via `CONFIG_XNU_BUILD_ARCHIVES=y`. The architectural timer entry patch
must suppress BCM2837's unrelated early timer MMIO routing. The generic ARM64
profile also inherits `CONFIG_XNU_NATIVE_PAN=y`; Cortex-A53 does not implement
ARMv8.1 PAN, so the parent must choose its non-PAN path for MSM8917.

No new build-time or boot-time GIC version knob is added. The following policies
apply when the DT selects GICv2:

- Reject `sheng-smp`, `sheng-smp-gic`, and `armcpu-spi-ipi=1`, and any XNU-visible
  multi-core topology.
- `armcpu-self-ipi=1` remains an explicit self-SGI request, not an automatic test.
- `armcpu-skip-gic` retains the explicit no-GIC diagnostic path; it is not a
  working timer/interrupt configuration.
- `armcpu-diagnostics=1` retains the upstream unsupported-power panic policy.
- Suspend/resume and secondary startup are not implemented. Supply the platform
  sleep-disabled policy where appropriate.

No compilation, syntax-check program, runtime command, diagnostic probe, or test
was executed while implementing this source change. Compilation belongs to the
parent build/CI; hardware usability is not established by source review.

## Sources

- [Arm GICv2 architecture](https://developer.arm.com/documentation/ihi0048/latest/).
- [Linux v6.12 ARM GIC register definitions](https://github.com/torvalds/linux/blob/v6.12/include/linux/irqchip/arm-gic.h)
  and [GIC driver](https://github.com/torvalds/linux/blob/v6.12/drivers/irqchip/irq-gic.c):
  register facts and non-secure interface semantics only; no GPL implementation
  code is copied into this BSD-licensed component.
- [MSM8917 inventory](https://github.com/Fimallory/UniXNU-rolex/blob/main/docs/hardware-inventory.md),
  sections 2–3 and its pinned downstream/mainline DTS references.
- [Pinned XNU](https://github.com/apple-oss-distributions/xnu/tree/d8b80295118ef25ac3a784134bcf95cd8e88109f).
