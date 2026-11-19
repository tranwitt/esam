"""Build the sample Word documents of the corpus (data/documents/originals/).

They are deliberately awkward, the way delivered Word files are: a logo in the
page header, a floating picture the text wraps around, a picture inside a
table cell, a diagram drawn with Word shapes (one row, and a two-by-two
group), a floating text box, the same picture used twice, and a BMP.

    python tests/fixtures/word/make_samples.py OUT_DIR      # writes the .docx files
    soffice --headless --convert-to 'doc:MS Word 97' --outdir OUT_DIR OUT_DIR/*.docx

Needs python-docx and Pillow (requirements-dev.txt). Pictures are drawn with
Pillow's built-in font, so the files differ slightly between Pillow versions.
See tests/fixtures/word/README.md for what was committed and how.
"""
import io
import sys
from pathlib import Path

from docx import Document
from docx.shared import Inches, Pt
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from PIL import Image, ImageDraw, ImageFont

def font(size):
    try: return ImageFont.load_default(size=size)
    except TypeError: return ImageFont.load_default()

def diagram(boxes, links, size=(1200, 520), title=None, fmt="PNG"):
    img = Image.new("RGB", size, "white"); d = ImageDraw.Draw(img)
    if title: d.text((20, 10), title, fill="black", font=font(30))
    centers = {}
    for name, (x, y, w, h), label in boxes:
        d.rectangle([x, y, x + w, y + h], outline="black", width=4)
        lines = label.split("\n")
        for i, line in enumerate(lines):
            d.text((x + 14, y + 12 + i * 38), line, fill="black", font=font(30))
        centers[name] = (x, y, w, h)
    for a, b, text in links:
        ax, ay, aw, ah = centers[a]; bx, by, bw, bh = centers[b]
        p1 = (ax + aw, ay + ah // 2); p2 = (bx, by + bh // 2)
        d.line([p1, p2], fill="black", width=4)
        d.polygon([p2, (p2[0] - 18, p2[1] - 10), (p2[0] - 18, p2[1] + 10)], fill="black")
        if text: d.text(((p1[0] + p2[0]) // 2 - 60, (p1[1] + p2[1]) // 2 - 40), text, fill="black", font=font(26))
    buf = io.BytesIO(); img.save(buf, fmt, **({"quality": 90} if fmt == "JPEG" else {})); return buf.getvalue()

def anchor_xml(rid, n, cx=2400000, cy=920000):
    return parse_xml(f'''<w:r {nsdecls("w","wp","a","pic","r")}><w:drawing>
<wp:anchor distT="0" distB="0" distL="114300" distR="114300" simplePos="0" relativeHeight="251658240" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>3200000</wp:posOffset></wp:positionH>
<wp:positionV relativeFrom="paragraph"><wp:posOffset>100000</wp:posOffset></wp:positionV>
<wp:extent cx="{cx}" cy="{cy}"/><wp:wrapSquare wrapText="bothSides"/><wp:docPr id="{100+n}" name="Floating {n}" descr="Valve command path"/>
<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>
<pic:nvPicPr><pic:cNvPr id="{100+n}" name="valve.jpg"/><pic:cNvPicPr/></pic:nvPicPr>
<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
</pic:pic></a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>''')

WPS = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:v="urn:schemas-microsoft-com:vml"'
def wsp(i, x, y, w, h, text):
    return f'''<wps:wsp><wps:cNvPr id="{300+i}" name="Box {i}"/><wps:cNvSpPr/><wps:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/></a:xfrm>
<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln w="12700"><a:solidFill><a:srgbClr val="000000"/></a:solidFill></a:ln></wps:spPr>
<wps:txbx><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></wps:txbx><wps:bodyPr/></wps:wsp>'''
def group_xml(labels, descr="Fault reporting chain"):
    """One row of boxes, or rows of boxes when `labels` is a list of rows. The
    boxes are written bottom row first, right to left: a reader has to go by
    where they sit, not by their order in the file."""
    rows = labels if isinstance(labels[0], list) else [labels]
    cells = [(r, c, t) for r, row in enumerate(rows) for c, t in enumerate(row)]
    boxes = "".join(wsp(i, 100000 + c * 1500000, 100000 + r * 650000, 1300000, 500000, t)
                    for i, (r, c, t) in enumerate(reversed(cells)))
    w = 1500000 * max(len(r) for r in rows) + 200000
    h = 650000 * len(rows) + 50000
    return parse_xml(f'''<w:r {nsdecls("w","wp","a")} {WPS}><w:drawing>
<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{w}" cy="{h}"/><wp:docPr id="200" name="Group 200" descr="{descr}"/>
<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"><wpg:wgp><wpg:cNvGrpSpPr/>
<wpg:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{w}" cy="{h}"/><a:chOff x="0" y="0"/><a:chExt cx="{w}" cy="{h}"/></a:xfrm></wpg:grpSpPr>
{boxes}</wpg:wgp></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>''')
def textbox_xml(text):
    return parse_xml(f'''<w:r {nsdecls("w","wp","a")} {WPS}><w:drawing>
<wp:anchor distT="0" distB="0" distL="114300" distR="114300" simplePos="0" relativeHeight="251659264" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">
<wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="column"><wp:posOffset>3600000</wp:posOffset></wp:positionH>
<wp:positionV relativeFrom="paragraph"><wp:posOffset>0</wp:posOffset></wp:positionV><wp:extent cx="2000000" cy="600000"/><wp:wrapSquare wrapText="bothSides"/>
<wp:docPr id="400" name="Text Box 400"/><a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">
{wsp(99, 0, 0, 2000000, 600000, text)}</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>''')


ARCH = diagram([("sus", (40, 80, 520, 380), "Suspension/Steering"), ("ecu", (80, 190, 440, 110), "Brake Control ECU\n(ABS)"),
                ("vet", (680, 80, 480, 380), "Vehicle Electronics"), ("bb", (720, 150, 400, 100), "J1939 Backbone"),
                ("dcl", (720, 310, 400, 100), "Driver Digital Cluster")],
               [("ecu", "bb", "EBC1")], title="Figure 1  Brake system in the vehicle")
VALVE = diagram([("ecu", (40, 120, 300, 120), "Brake ECU"), ("hcu", (520, 120, 360, 120), "Hydraulic\nControl Unit"),
                 ], [("ecu", "hcu", "valve cmd")], size=(940, 360), fmt="JPEG")
SENSOR = diagram([("ws", (20, 40, 300, 100), "Wheel speed\nsensor x4")], [], size=(360, 200))
LOGO = diagram([("l", (4, 4, 150, 60), "SYNTH")], [], size=(160, 70))
POWER = diagram([("pwr", (40, 80, 520, 380), "Power Package/Drive Train"), ("ecm", (80, 190, 440, 110), "Engine Control\nModule"),
                 ("vet", (680, 80, 480, 380), "Vehicle Electronics"), ("bb", (720, 190, 400, 110), "J1939 Backbone")],
                [("ecm", "bb", "EEC1")], title="Figure 1  Engine control in the vehicle")
SPEED = diagram([("cs", (20, 40, 300, 100), "Crank speed\nsensor")], [], size=(360, 200), fmt="BMP")


def header(d, text):
    sec = d.sections[0]
    p = sec.header.paragraphs[0]
    p.add_run().add_picture(io.BytesIO(LOGO), width=Inches(0.9))
    p.add_run("  " + text)
    return sec


def front(d, doc_id, name, system):
    d.add_paragraph(f"{doc_id} — {name}", style="Title")
    d.add_paragraph("SYNTHETIC LEARNING DOCUMENT. Programme SYNTH-LTV is fictional.")
    t = d.add_table(rows=2, cols=4); t.style = "Table Grid"
    for i, x in enumerate(["Document", "Type", "Revision", "System"]): t.rows[0].cells[i].text = x
    for i, x in enumerate([doc_id, "SSDD", "A", system]): t.rows[1].cells[i].text = x


def build_susp(path):
    d = Document()
    header(d, "SYNTH-LTV programme")
    front(d, "SSDD-SUSP-001", "Suspension/Steering System Design Description", "SYS-SUSP")
    d.add_heading("1. Scope", 1)
    d.add_paragraph("This document describes the design of the Suspension/Steering system (SYS-SUSP, WBS 1.1.1.5) of the SYNTH-LTV "
                    "vehicle: the units it is made of and how its brake function reaches the rest of the vehicle. It is a "
                    "system-level document, so it is attached to the system and not to one subsystem.")
    d.add_heading("2. System architecture", 1)
    d.add_paragraph("The system contains one software-bearing unit, the Brake Control ECU (SUB-BRK-ECU). It reports brake state "
                    "to the Vehicle Electronics system (SYS-VETR) over the J1939 backbone, as Figure 1 shows.")
    p = d.add_paragraph(); p.add_run().add_picture(io.BytesIO(ARCH), width=Inches(6.0))
    d.add_paragraph("Figure 1 — Brake system in the vehicle", style="Caption")
    d.add_heading("2.1 Hydraulic control", 2)
    p = d.add_paragraph("The Brake Control ECU drives the hydraulic control unit through four valve pairs. On an internal fault "
                        "the valves are released and the vehicle keeps conventional braking.")
    rid, _ = p.part.get_or_add_image(io.BytesIO(VALVE))
    p._p.append(anchor_xml(rid, 1))                        # a floating picture: the text wraps around it
    d.add_paragraph("Figure 2 — Valve command path", style="Caption")
    d.add_heading("3. Unit interfaces", 1)
    t = d.add_table(rows=3, cols=3); t.style = "Table Grid"
    for i, x in enumerate(["Interface", "Carries", "Drawing"]): t.rows[0].cells[i].text = x
    t.rows[1].cells[0].text = "Wheel speed inputs"; t.rows[1].cells[1].text = "Four variable-reluctance sensors, one per wheel"
    t.rows[1].cells[2].paragraphs[0].add_run().add_picture(io.BytesIO(SENSOR), width=Inches(1.4))   # a picture in a cell
    t.rows[2].cells[0].text = "J1939"; t.rows[2].cells[1].text = "EBC1 (PGN 61441) every 100 ms"; t.rows[2].cells[2].text = "see ICD-BRK-001"
    d.add_heading("4. Fault reporting", 1)
    d.add_paragraph("A brake fault reaches the driver through three units:")
    p = d.add_paragraph(); p._p.append(group_xml(["Brake Control ECU", "J1939 Backbone", "Driver Digital Cluster"]))
    d.add_paragraph("Figure 3 — Fault reporting chain", style="Caption")
    p = d.add_paragraph("The budget from detection to a lit telltale is 500 ms.")
    p._p.append(textbox_xml("Design note: 100 ms of the budget is the EBC1 period."))
    for x in ("EBC1 broadcast period: 100 ms", "Cluster reaction: 250 ms", "Margin: 150 ms"):
        d.add_paragraph(x, style="List Bullet")
    d.add_heading("5. Design decisions", 1)
    for x in ("The brake function keeps working with no data bus.", "Display software is never in the path of a brake command."):
        d.add_paragraph(x, style="List Number")
    p = d.add_paragraph(); p.add_run().add_picture(io.BytesIO(ARCH), width=Inches(3.0))      # the same picture used twice
    d.add_paragraph("Figure 4 — Figure 1 again, reduced", style="Caption")
    d.save(path)


def build_pwr(path):
    d = Document()
    sec = header(d, "SYNTH-LTV programme")
    sec.footer.paragraphs[0].add_run().add_picture(io.BytesIO(LOGO), width=Inches(0.6))
    front(d, "SSDD-PWR-001", "Power Package/Drive Train System Design Description", "SYS-PWR")
    d.add_heading("1. Scope", 1)
    d.add_paragraph("This document describes the design of the Power Package/Drive Train system (SYS-PWR, WBS 1.1.1.7) of the "
                    "SYNTH-LTV vehicle. It is a system-level document, so it is attached to the system and not to one subsystem.")
    d.add_heading("2. System architecture", 1)
    d.add_paragraph("The system contains one software-bearing unit, the Engine Control Module (SUB-ENG-ECM). It is a node on the "
                    "J1939 backbone (SUB-J1939-BB) with source address 0, as Figure 1 shows.")
    p = d.add_paragraph(); p.add_run().add_picture(io.BytesIO(POWER), width=Inches(6.0))
    d.add_paragraph("Figure 1 — Engine control in the vehicle", style="Caption")
    d.add_heading("3. Traction torque reduction", 1)
    d.add_paragraph("During a traction event the brake controller may ask the engine for less torque. The request would travel "
                    "as Figure 2 shows, top row first:")
    p = d.add_paragraph(); p._p.append(group_xml([["Brake Control ECU", "J1939 Backbone"], ["Engine Control Module", "Fuel injection"]],
                                                 descr="Torque reduction path"))
    d.add_paragraph("Figure 2 — Torque reduction path", style="Caption")
    p = d.add_paragraph("The Engine Control Module's use of the EBC1 traction bits is not confirmed.")
    p._p.append(textbox_xml("Open point: no interface document states this use."))
    d.add_heading("4. Unit interfaces", 1)
    t = d.add_table(rows=3, cols=3); t.style = "Table Grid"
    for i, x in enumerate(["Interface", "Carries", "Drawing"]): t.rows[0].cells[i].text = x
    t.rows[1].cells[0].text = "Crank speed input"; t.rows[1].cells[1].text = "One sensor on the flywheel housing"
    t.rows[1].cells[2].paragraphs[0].add_run().add_picture(io.BytesIO(SPEED), width=Inches(1.4))    # a BMP, in a cell
    t.rows[2].cells[0].text = "J1939"; t.rows[2].cells[1].text = "Node on the backbone, source address 0"; t.rows[2].cells[2].text = ""
    d.add_heading("5. Design decisions", 1)
    for x in ("The engine keeps running with no data bus.", "A torque reduction request is honoured only while the sender is heard."):
        d.add_paragraph(x, style="List Bullet")
    d.save(path)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    build_susp(out / "SSDD-SUSP-001.docx")
    build_pwr(out / "SSDD-PWR-001.docx")
