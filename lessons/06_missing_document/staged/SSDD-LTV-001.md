# SSDD-LTV-001 — SYNTH-LTV Vehicle System/Subsystem Design Description

> SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

| Document | Type | Revision | System |
|---|---|---|---|
| SSDD-LTV-001 | SSDD | A | V-001 |

This is the system-level design of the whole vehicle (MIL-STD-498 SSDD), so it is attached to the
vehicle system `V-001`, not to one subsystem.

## 1. Scope

This document describes the system-wide design of the SYNTH-LTV Ground Vehicle System (V-001, WBS 1.0):
the systems it is made of, how software-bearing subsystems are allocated to them, and the design
decisions that hold across the vehicle. The design of each subsystem's software is in its own SDD.

## 2. System-wide design decisions

- All electronic control units exchange data over one SAE J1939 CAN backbone at 250 kbit/s
  (SUB-J1939-BB). No subsystem talks to another by a private link.
- Safety-critical control loops run on a real-time operating system. Display and crew-station
  software may run on a general-purpose operating system and shall never be in the path of a
  safety-critical command.
- A subsystem that loses the backbone keeps its own function in a defined degraded mode. The brake
  ECU regulates wheel slip with no bus at all.
- Every message a subsystem depends on is defined in an interface control document owned by the
  sender.

## 3. System architecture

| System | WBS | Subsystems | Role |
|---|---|---|---|
| Suspension/Steering (SYS-SUSP) | 1.1.1.5 | Brake Control ECU (SUB-BRK-ECU) | Wheel-slip regulation; broadcasts brake state (EBC1) |
| Vehicle Electronics (SYS-VETR) | 1.1.1.6 | J1939 Vehicle Data Backbone (SUB-J1939-BB), Driver Digital Cluster (SUB-DCL) | Data distribution; driver displays and warnings |
| Power Package/Drive Train (SYS-PWR) | 1.1.1.7 | Engine Control Module (SUB-ENG-ECM) | Engine control |

## 4. Allocation of functions to subsystems

| Vehicle function | Allocated to | Criticality |
|---|---|---|
| Prevent wheel lock-up under braking | SUB-BRK-ECU (FN-BRK-SLIP) | MIL-STD-882E SwCI 1 |
| Tell the driver about a brake-system fault | SUB-BRK-ECU (FN-BRK-STATUS) sends; SUB-DCL (FN-DCL-BRKWARN) shows | Sender SwCI 1; display not safety-critical |
| Carry messages between ECUs | SUB-J1939-BB (FN-BB-TRANSPORT) | Safety-critical |
| Night-vision-compatible display | SUB-DCL (FN-DCL-NVG) | Not safety-critical |

The brake warning reaches the driver through two subsystems and the backbone. The vehicle-level
budget from fault detection to a lit telltale is 500 ms: 100 ms for the EBC1 broadcast period, and
the rest for the cluster.

## 5. Interfaces between systems

| From | To | Carries | Defined in |
|---|---|---|---|
| SUB-BRK-ECU | SUB-J1939-BB | EBC1 (PGN 61441), every 100 ms | ICD-BRK-001 |
| SUB-J1939-BB | SUB-DCL | EBC1 (PGN 61441) | ICD-BRK-001 |
| SUB-J1939-BB | SUB-ENG-ECM | EBC1 (PGN 61441), use by the ECM not confirmed | none yet |

## 6. Notes

The lead variant (VAR-001) carries every system listed here. A derivative variant that changes a
system will state the difference in its own revision of this document.
