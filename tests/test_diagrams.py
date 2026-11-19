"""Picture diagrams (esamkit.diagrams): a vision model's description when one
is configured, local text otherwise, cached by picture content. The vision
model is a fake endpoint on 127.0.0.1 (tests/conftest.py)."""

import shutil
from dataclasses import replace

import pytest

import wordfiles as wf
from conftest import chat_answer, responses_answer
from esamkit import diagrams, llm
from esamkit.config import settings
from esamkit.docmodel import Picture
from esamkit.readers import convert

ANSWER = ("**Block diagram.** The `BRAKE_ECU` (SUB-BRK-ECU) sends EBC1 (PGN 61441) every 100 ms to the cluster.\n"
          "- wheel_speed_fl enters from the left\n| a | table |")
CLEAN = ("Block diagram. The BRAKE_ECU (SUB-BRK-ECU) sends EBC1 (PGN 61441) every 100 ms to the cluster. "
         "wheel_speed_fl enters from the left / a / table /")


@pytest.fixture(autouse=True)
def fresh():
    llm._CHAINS.clear()


def vision(tmp_path, url, **kw):
    return replace(settings, **{"diagrams": "auto", "vision_model": "gpt-5.1", "vision_base_url": url, "vision_api_key": "k",
                                "vision_api": "chat", "vision_fallbacks": (), "chat_fallbacks": (),
                                "diagram_cache_dir": tmp_path / "cache", "llm_timeout": 5, **kw})


def test_a_vision_model_describes_the_picture_once(tmp_path, endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer(ANSWER))
    s = vision(tmp_path, endpoint.url)
    pic = Picture("image1.png", wf.png(), "image/png", caption="Figure 2 — Brake bus")
    block, info = diagrams.figure_block(pic, 1, s)
    assert block == ("> **Picture 1** (`image1.png`). Caption: Figure 2 — Brake bus.\n"
                     f"> Diagram description (gpt-5.1): {CLEAN}")
    assert info["read_by"] == ["vision model gpt-5.1"] and info["description"] == CLEAN
    body = endpoint.calls[0][1]
    assert body["model"] == "gpt-5.1" and "verbatim" in body["messages"][1]["content"][0]["text"]
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    again, info2 = diagrams.figure_block(pic, 1, s)               # the same picture: from the cache, no call
    assert again == block and info2["read_by"] == ["vision model gpt-5.1 (cached)"] and len(endpoint.calls) == 1
    other, _ = diagrams.figure_block(Picture("image2.png", wf.png(9, 9), "image/png"), 2, s)
    assert len(endpoint.calls) == 2 and other.startswith("> **Picture 2** (`image2.png`).\n")


def test_the_vision_model_fails_over_to_the_responses_api(tmp_path, endpoint):
    endpoint.handlers["/responses"] = lambda b: (200, responses_answer("A state machine with states IDLE and ACTIVE."))
    s = vision(tmp_path, endpoint.url, vision_api="auto")
    block, info = diagrams.figure_block(Picture("sm.png", wf.png(), "image/png"), 1, s)
    assert "Diagram description (gpt-5.1): A state machine with states IDLE and ACTIVE." in block
    assert endpoint.paths() == ["/chat/completions", "/responses"]
    assert endpoint.calls[1][1]["input"][0]["content"][1]["type"] == "input_image"


def test_an_svg_goes_to_the_model_as_its_source_and_locally_gives_its_labels(tmp_path, endpoint):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><desc>Power feeds</desc><text>PDU</text><text>FEED A</text></svg>'
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("Two feeds, FEED A and FEED B, enter the PDU."))
    block, _ = diagrams.figure_block(Picture("feeds.svg", svg), 1, vision(tmp_path, endpoint.url))
    sent = endpoint.calls[0][1]["messages"][1]["content"]
    assert isinstance(sent, str) and "<text>FEED A</text>" in sent              # text, not an image part
    assert "Diagram description (gpt-5.1): Two feeds" in block
    local, info = diagrams.figure_block(Picture("feeds.svg", svg), 1, replace(settings, diagrams="local", vision_model=""))
    assert local == "> **Picture 1** (`feeds.svg`).\n> Text in the picture: Power feeds; PDU; FEED A."
    assert info["read_by"] == ["SVG text"] and diagrams.sniff(svg) == "image/svg+xml"


def test_no_model_configured_means_no_picture_is_sent(tmp_path, endpoint):
    s = replace(vision(tmp_path, endpoint.url), vision_model="")
    block, info = diagrams.figure_block(Picture("p.png", wf.png(), "image/png"), 1, s)
    assert endpoint.calls == [] and "Diagram description" not in block
    if not info["read_by"]:                                       # (with tesseract installed OCR may find nothing in a red square)
        assert "> Not described: no vision model is configured" in block
    assert endpoint.calls == []
    local = replace(vision(tmp_path, endpoint.url), diagrams="local")           # a model IS configured, but mode says local
    diagrams.figure_block(Picture("p.png", wf.png(), "image/png"), 1, local)
    assert endpoint.calls == []


def test_off_keeps_only_name_caption_and_the_documents_own_text(tmp_path, endpoint):
    s = vision(tmp_path, endpoint.url, diagrams="off")
    block, info = diagrams.figure_block(Picture("p.png", wf.png(), "image/png", alt="wiring", caption="Figure 1"), 3, s)
    assert block == "> **Picture 3** (`p.png`). Caption: Figure 1. Alt text: wiring." and endpoint.calls == []
    shapes, _ = diagrams.figure_block(Picture("drawing1", labels=["ECU", "Valve"]), 4, s)
    assert shapes == "> **Picture 4** (`drawing1`).\n> Text in the picture: ECU; Valve."       # typed in Word: document text


def test_a_failing_vision_model_never_stops_an_ingest(tmp_path, endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (500, {"error": "down"})
    s = vision(tmp_path, endpoint.url)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><text>ONLY LABEL</text></svg>'
    block, info = diagrams.figure_block(Picture("x.svg", svg), 1, s)
    assert "Text in the picture: ONLY LABEL." in block and "did not answer" in info["note"]
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").iterdir())   # failures are not cached


def test_sniff_goes_by_content():
    assert diagrams.sniff(wf.png()) == "image/png" and diagrams.sniff(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert diagrams.sniff(b"GIF89a") == "image/gif" and diagrams.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert diagrams.sniff(b"\x00\x01", "image/x-emf") == "image/x-emf" and diagrams.sniff(b"??") == ""
    assert diagrams.svg_text(b'<!DOCTYPE svg [<!ENTITY x "y">]><svg><text>&x;</text></svg>') == []


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract is not installed")
def test_ocr_reads_text_off_a_picture(tmp_path):
    PIL = pytest.importorskip("PIL")
    from PIL import Image, ImageDraw, ImageFont
    del PIL
    img = Image.new("RGB", (900, 220), "white")
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:
        pytest.skip("this Pillow cannot scale its default font")
    ImageDraw.Draw(img).text((40, 60), "BRAKE ECU 100 ms", fill="black", font=font)
    img.save(tmp_path / "label.png")
    res = convert(tmp_path / "label.png", replace(settings, diagrams="local", vision_model=""))
    assert res.figures[0]["read_by"] == ["OCR (tesseract)"] and "BRAKE ECU" in res.text


def test_docx_with_a_diagram_end_to_end(tmp_path, endpoint):
    endpoint.handlers["/chat/completions"] = lambda b: (200, chat_answer("A block diagram: PDU feeds LOAD 1 at 24 V."))
    body = (wf.p("ICD-X-007 — Power", "Title") + wf.p("Architecture", "Heading1")
            + wf.p("", extra=wf.picture("rId1")) + wf.p("Figure 1 — Feeds", "Caption"))
    (tmp_path / "ICD-X-007.docx").write_bytes(wf.make_docx(body, {"image1.png": wf.png()}))
    res = convert(tmp_path / "ICD-X-007.docx", vision(tmp_path, endpoint.url))
    assert ("## Architecture\n\n> **Picture 1** (`image1.png`). Caption: Figure 1 — Feeds.\n"
            "> Diagram description (gpt-5.1): A block diagram: PDU feeds LOAD 1 at 24 V.\n\nFigure 1 — Feeds\n") in res.text
    assert res.report()["pictures"][0]["read_by"] == ["vision model gpt-5.1"] and "bytes" not in res.report()["pictures"][0]
    # and it is retrievable text: the chunker keeps the description with its section
    from esamkit.chunking import chunk_markdown
    assert any("PDU feeds LOAD 1" in c and c.startswith("[ICD-X-007 — Power › Architecture]") for c in chunk_markdown(res.text))
    structure_only = convert(tmp_path / "ICD-X-007.docx", vision(tmp_path, endpoint.url), diagrams=False)
    assert "Diagram description" not in structure_only.text and len(endpoint.calls) == 1
