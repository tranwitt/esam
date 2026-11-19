# SSDD-SUSP-001 — Suspension/Steering System Design Description

SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

| Document | Type | Revision | System |
|---|---|---|---|
| SSDD-SUSP-001 | SSDD | A | SYS-SUSP |

## 1. Scope

This document describes the design of the Suspension/Steering system (SYS-SUSP, WBS 1.1.1.5) of the SYNTH-LTV vehicle: the units it is made of and how its brake function reaches the rest of the vehicle. It is a system-level document, so it is attached to the system and not to one subsystem.

## 2. System architecture

The system contains one software-bearing unit, the Brake Control ECU (SUB-BRK-ECU). It reports brake state to the Vehicle Electronics system (SYS-VETR) over the J1939 backbone, as Figure 1 shows.

> **Picture 1** (`image1.png`). Caption: Figure 1 — Brake system in the vehicle.
> Text in the picture (OCR): Figure 1 Brake system in the vehicle Suspension/Steering Vehicle Electronics EBC1 Brake Control ECU (ABS)

Figure 1 — Brake system in the vehicle

### 2.1 Hydraulic control

The Brake Control ECU drives the hydraulic control unit through four valve pairs. On an internal fault the valves are released and the vehicle keeps conventional braking.

> **Picture 2** (`image2.jpeg`). Caption: Figure 2 — Valve command path.
> Text in the picture (OCR): Brake ECU valve cmd Hydraulic Control Unit

Figure 2 — Valve command path

## 3. Unit interfaces

| Interface | Carries | Drawing |
|---|---|---|
| Wheel speed inputs | Four variable-reluctance sensors, one per wheel | (Picture 3, below) |
| J1939 | EBC1 (PGN 61441) every 100 ms | see ICD-BRK-001 |

> **Picture 3** (`image3.png`).
> Not described: no vision model is configured and no text could be read locally.

## 4. Fault reporting

A brake fault reaches the driver through three units:

> **Picture 4** (`drawing1`). Caption: Figure 3 — Fault reporting chain.
> Text in the picture: Brake Control ECU; J1939 Backbone; Driver Digital Cluster.

Figure 3 — Fault reporting chain

The budget from detection to a lit telltale is 500 ms.

> **Picture 5** (`drawing2`).
> Text in the picture: Design note: 100 ms of the budget is the EBC1 period.

- EBC1 broadcast period: 100 ms
- Cluster reaction: 250 ms
- Margin: 150 ms

## 5. Design decisions

1. The brake function keeps working with no data bus.
2. Display software is never in the path of a brake command.

> **Picture 6** (`image1.png`). Caption: Figure 4 — Figure 1 again, reduced.
> Text in the picture (OCR): Figure 1 Brake system in the vehicle Suspension/Steering Vehicle Electronics EBC1 Brake Control ECU (ABS)

Figure 4 — Figure 1 again, reduced
