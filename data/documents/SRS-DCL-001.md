# SRS-DCL-001 — Driver Digital Cluster Software Requirements

> SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

## 1. Display requirements

- **SRS-DCL-010** The software shall render primary gauges at no less than 30 frames per second
  under all operating conditions.
- **SRS-DCL-011** The software shall display critical telltales within 2 s of power-on.

## 2. Brake warning requirements

- **SRS-DCL-020** The software shall illuminate the amber ABS telltale within 200 ms of
  receiving an EBC1 frame requesting the amber warning lamp.
  Traces to: SRS-SYS-DCL-040
- **SRS-DCL-021** The software shall treat anti-lock braking as unavailable and illuminate the
  ABS telltale if no EBC1 frame is received for 300 ms.
  Traces to: SRS-SYS-DCL-040
- **SRS-DCL-022** The software shall display brake ECU DM1 trouble codes on the maintenance page.

## 3. Night operations requirements

- **SRS-DCL-030** The software shall provide an NVG-compatible display mode.
  Traces to: SRS-SYS-DCL-041
- **SRS-DCL-031** In blackout mode the software shall suppress all graphics except critical
  warnings.
  Traces to: SRS-SYS-DCL-041

## 4. System requirements

These are what the Driver Digital Cluster as a whole must do, whatever mix of hardware and
software does it. Their ids carry the marker `SYS` (`SRS-SYS-DCL-…`). The software requirements
in sections 1 to 3 name the system requirement they derive from on a `Traces to:` line.

- **SRS-SYS-DCL-040** The Driver Digital Cluster shall warn the driver of a brake-system fault
  within 500 ms of the fault being broadcast on the vehicle data bus.
  Timing: 500 ms max | Verification: T
- **SRS-SYS-DCL-041** The Driver Digital Cluster shall remain readable by a crew using
  night-vision devices.
  Verification: D
