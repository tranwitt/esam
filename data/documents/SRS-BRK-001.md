# SRS-BRK-001 — Brake Control ECU Software Requirements

> SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

## 1. Control requirements

- **SW-SRS-BRK-010** The software shall execute the wheel-slip control loop every 5 ms with jitter
  not exceeding 0.5 ms.
  Traces to: SYS-SRS-BRK-040
- **SW-SRS-BRK-011** The software shall release brake pressure on a wheel within 15 ms of
  detecting incipient lock-up on that wheel.
  Traces to: SYS-SRS-BRK-040
- **SW-SRS-BRK-012** The software shall support a crew-selectable off-road ABS mode that permits
  limited wheel lock to build a soil wedge on loose terrain.

## 2. Safety requirements

- **SW-SRS-BRK-020** The software is assigned Software Criticality Index 1 under MIL-STD-882E and
  shall be developed to the corresponding level of rigor.
- **SW-SRS-BRK-021** On detection of an internal fault, the software shall revert the hydraulic
  unit to conventional braking within 20 ms.
  Traces to: SYS-SRS-BRK-041
- **SW-SRS-BRK-022** The software shall not allocate memory dynamically after initialisation.

## 3. Interface requirements

- **SW-SRS-BRK-030** The software shall transmit EBC1 (PGN 61441) every 100 ms ± 10 ms as defined
  in ICD-BRK-001.
  Traces to: SYS-SRS-BRK-042
- **SW-SRS-BRK-031** The software shall set the amber warning lamp request in EBC1 whenever
  anti-lock modulation is unavailable.
  Traces to: SYS-SRS-BRK-042
- **SW-SRS-BRK-032** The software shall transmit DM1 within 1 s of any new active fault.
  Traces to: SYS-SRS-BRK-042

## 4. System requirements

These are what the Brake Control ECU as a whole must do, whatever mix of hardware and software
does it. Their ids start with `SYS-` (`SYS-SRS-BRK-…`). The software requirements in
sections 1 to 3 start with `SW-` (`SW-SRS-BRK-…`) and name the system requirement they derive from on a `Traces to:` line.

- **SYS-SRS-BRK-040** The Brake Control ECU shall prevent sustained lock-up of any braked wheel
  during service braking at speeds above 8 km/h.
  Safety: SwCI 1 | Verification: T, A
- **SYS-SRS-BRK-041** The Brake Control ECU shall revert to conventional braking when it can no
  longer modulate brake pressure.
  Safety: SwCI 1 | Verification: T
- **SYS-SRS-BRK-042** The Brake Control ECU shall report its anti-lock state and its faults to
  the vehicle over the J1939 backbone.
  Verification: T
- **SYS-SRS-BRK-043** The Brake Control ECU shall complete its power-up self-test within 500 ms
  of ignition on.
  Timing: 500 ms max | Verification: T
