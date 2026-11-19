# SRS-DCL-001 — Driver Digital Cluster Software Requirements

> SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

## 1. Display requirements

- **SW-SRS-DCL-010** The software shall render primary gauges at no less than 30 frames per second
  under all operating conditions.
- **SW-SRS-DCL-011** The software shall display critical telltales within 2 s of power-on.

## 2. Brake warning requirements

- **SW-SRS-DCL-020** The software shall illuminate the amber ABS telltale within 200 ms of
  receiving an EBC1 frame requesting the amber warning lamp.
  Traces to: SYS-SRS-DCL-040
- **SW-SRS-DCL-021** The software shall treat anti-lock braking as unavailable and illuminate the
  ABS telltale if no EBC1 frame is received for 300 ms.
  Traces to: SYS-SRS-DCL-040
- **SW-SRS-DCL-022** The software shall display brake ECU DM1 trouble codes on the maintenance page.

## 3. Night operations requirements

- **SW-SRS-DCL-030** The software shall provide an NVG-compatible display mode.
  Traces to: SYS-SRS-DCL-041
- **SW-SRS-DCL-031** In blackout mode the software shall suppress all graphics except critical
  warnings.
  Traces to: SYS-SRS-DCL-041

## 4. System requirements

These are what the Driver Digital Cluster as a whole must do, whatever mix of hardware and
software does it. Their ids start with `SYS-` (`SYS-SRS-DCL-…`). The software requirements
in sections 1 to 3 start with `SW-` (`SW-SRS-DCL-…`) and name the system requirement they derive from on a `Traces to:` line.

- **SYS-SRS-DCL-040** The Driver Digital Cluster shall warn the driver of a brake-system fault
  within 500 ms of the fault being broadcast on the vehicle data bus.
  Timing: 500 ms max | Verification: T
- **SYS-SRS-DCL-041** The Driver Digital Cluster shall remain readable by a crew using
  night-vision devices.
  Verification: D
