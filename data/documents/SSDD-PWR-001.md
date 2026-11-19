# SSDD-PWR-001 — Power Package/Drive Train System Design Description

SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.

| Document | Type | Revision | System |
|---|---|---|---|
| SSDD-PWR-001 | SSDD | A | SYS-PWR |

## 1. Scope

This document describes the design of the Power Package/Drive Train system (SYS-PWR, WBS 1.1.1.7) of the SYNTH-LTV vehicle. It is a system-level document, so it is attached to the system and not to one subsystem.

## 2. System architecture

The system contains one software-bearing unit, the Engine Control Module (SUB-ENG-ECM). It is a node on the J1939 backbone (SUB-J1939-BB) with source address 0, as Figure 1 shows.

> **Picture 1** (`image1.png`). Caption: Figure 1 — Engine control in the vehicle.
> Text in the picture (OCR): Figure 1 Engine control in the vehicle Power Package/Drive Train Vehicle Electronics Engine Control EEC1 J1939 Backbone Module

Figure 1 — Engine control in the vehicle

## 3. Traction torque reduction

During a traction event the brake controller may ask the engine for less torque. The request would travel as Figure 2 shows, top row first:

> **Picture 2** (`drawing1`). Caption: Figure 2 — Torque reduction path.
> Text in the picture: Brake Control ECU; J1939 Backbone; Engine Control Module; Fuel injection.

Figure 2 — Torque reduction path

The Engine Control Module's use of the EBC1 traction bits is not confirmed.

> **Picture 3** (`drawing2`).
> Text in the picture: Open point: no interface document states this use.

## 4. Unit interfaces

| Interface | Carries | Drawing |
|---|---|---|
| Crank speed input | One sensor on the flywheel housing | (Picture 4, below) |
| J1939 | Node on the backbone, source address 0 |  |

> **Picture 4** (`image2.png`).
> Text in the picture (OCR): Crank speed sensor

## 5. Design decisions

- The engine keeps running with no data bus.
- A torque reduction request is honoured only while the sender is heard.
