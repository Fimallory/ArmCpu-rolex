# ArmCpu

ARM generic interrupt controller and timer.

Independent `rolex-gicv2` fork of ArmCpu revision
`9066b53bbc82e92a98ec44a11dd50fd13eaf1a1b` for MSM8917 single-core bring-up.

The original GICv3 interface and virtual timer INTID 27 remain the default for
existing trees. Exact `arm,gic-400` or `qcom,msm-qgic2` compatible properties
select a GICv2 MMIO path with a DT-derived virtual timer interrupt.

The GICv2 path requires one CPU, non-secure firmware ownership of SGI 0 and the
timer PPI, and exported `intc` and `timer` ADT nodes. SMP, remote CPU signaling,
the SPI IPI fallback, and suspend/resume are unsupported.

See [GICv2 interface and integration](docs/gicv2.md). This source change has not
been compiled or exercised on hardware; the parent build/CI owns compilation.

Build and interface details: [documentation](https://unixnu.com/reference/components). Component sources and dependencies: [catalog](https://unixnu.com/reference/components).

Fork compilation on GitHub Actions: [ArmCpu ABI build](docs/ci-build.md).
