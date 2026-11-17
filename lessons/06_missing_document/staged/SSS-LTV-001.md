# SSS-LTV-001 — SYNTH-LTV Vehicle System/Subsystem Specification

> SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

| Document | Type | Revision | System |
|---|---|---|---|
| SSS-LTV-001 | SSS | A | V-001 |

This specification states SYSTEM requirements: what the vehicle as a whole must do, whatever mix of
hardware and software does it. The SOFTWARE requirements that follow from them are in each
subsystem's SRS, and point back here with a `Traces to:` line.

## 1. Scope

This specification applies to the SYNTH-LTV Ground Vehicle System (V-001, WBS 1.0).

## 3. Requirements

### 3.1 Braking

- **SSS-LTV-010** The vehicle shall prevent sustained lock-up of any braked wheel during service
  braking at speeds above 8 km/h on surfaces with a friction coefficient of 0.2 or more.
  Safety: SwCI 1 | Verification: T, A

- **SSS-LTV-011** The vehicle shall inform the driver of a brake-system fault that reduces
  anti-lock performance within 500 ms of the fault being detected.
  Safety: SwCI 2 | Timing: 500 ms max | Verification: T

- **SSS-LTV-012** The vehicle shall keep service braking available when the vehicle data bus
  has failed.
  Safety: SwCI 1 | Verification: T, A

### 3.2 Vehicle data distribution

- **SSS-LTV-020** The vehicle shall exchange data between its electronic control units over a
  single SAE J1939 network.
  Verification: I

- **SSS-LTV-021** The vehicle shall broadcast brake-system state to every control unit on the
  network at least every 100 ms.
  Timing: 100 ms max | Verification: T

### 3.3 Crew station

- **SSS-LTV-030** The vehicle shall present driver warnings in a form readable with night-vision
  devices.
  Verification: D
