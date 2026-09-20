#!/usr/bin/env python3
"""Smoke test for the automation socket: drives a running compositor-linux
through a few calls and checks the answers. CI starts the app headless with
--demo first.

    compositor-linux --headless --rpc-socket /tmp/c.sock --demo &
    python3 tools/rpc_smoke.py /tmp/c.sock
"""
import base64
import json
import os
import socket
import sys
import time


class PipeFile:
    """A tiny line reader/writer over a Windows named-pipe handle.

    Qt's QLocalServer presents an automation "socket" as a named pipe on
    Windows, and Python's socket module has no AF_UNIX there, so the pipe is
    opened as a plain file and wrapped in the write/flush/readline surface the
    rest of this script expects.
    """

    def __init__(self, raw):
        self.raw = raw
        self.buffer = b""

    def write(self, text):
        self.raw.write(text.encode("utf-8"))

    def flush(self):
        pass

    def readline(self):
        while b"\n" not in self.buffer:
            chunk = self.raw.read(65536)
            if not chunk:
                break
            self.buffer += chunk
        if b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            return line.decode("utf-8") + "\n"
        line, self.buffer = self.buffer, b""
        return line.decode("utf-8") if line else ""


def open_connection(path):
    """Return a file-like object for the automation endpoint at `path`."""
    if os.name == "nt":
        pipe = "\\\\.\\pipe\\" + path.replace("/", "\\").lstrip("\\")
        return PipeFile(open(pipe, "r+b", buffering=0))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    return sock.makefile("rw", encoding="utf-8")


class Rpc:
    def __init__(self, path):
        self.file = open_connection(path)
        self.next_id = 0
        self.events = []

    def call(self, method, **params):
        self.next_id += 1
        self.file.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n")
        self.file.flush()
        reply = json.loads(self.file.readline())
        while reply.get("method") == "event":
            self.events.append(reply["params"])
            reply = json.loads(self.file.readline())
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']['message']}")
        return reply["result"]


def wait_for(path, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            return Rpc(path)
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"no socket at {path} after {seconds}s")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        raise SystemExit("usage: rpc_smoke.py <socket path>")
    rpc = wait_for(path)
    info = rpc.call("app.info")
    assert info["name"] == "compositor-linux", info
    print("version", info["version"], "platform", info["platform"])

    doc = rpc.call("document.info")
    assert doc["width"] > 0 and doc["layers"] > 0, doc
    layers = rpc.call("layers.list")
    print(f"document {doc['width']}x{doc['height']}, {len(layers)} layers")
    pixel_layers = [l for l in layers if l["kind"] in ("pixels", "shape") and not l.get("pixelSize", {}).get("blank", True)]
    assert pixel_layers, layers

    # Observe: a render comes back as PNG.
    shot = rpc.call("render", maxSize=256)
    png = base64.b64decode(shot["png"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n", png[:8]
    assert max(shot["width"], shot["height"]) == 256, shot
    print("render", shot["width"], "x", shot["height"], len(png), "bytes")

    # Edit and undo: a property change is one history step.
    target = pixel_layers[0]
    before = target["opacity"]
    changed = rpc.call("layers.set", id=target["id"], opacity=0.5)
    assert abs(changed["opacity"] - 0.5) < 1e-6, changed
    hist = rpc.call("history.info")
    assert hist["canUndo"], hist
    rpc.call("history.undo")
    restored = rpc.call("layers.get", id=target["id"])
    assert abs(restored["opacity"] - before) < 1e-6, restored

    # A selection, a fill, and a cropped render of that area.
    rpc.call("layers.select", id=target["id"])
    sel = rpc.call("selection.rect", x=10, y=10, width=40, height=30)
    assert sel["bounds"]["width"] == 40, sel
    rpc.call("pixels.fill", color="#ff0000")
    region = rpc.call("render", region={"x": 10, "y": 10, "width": 40, "height": 30}, maxSize=0)
    assert region["width"] == 40 and region["height"] == 30, region
    rpc.call("selection.none")

    # An adjustment layer with settings, and a destructive filter.
    adj = rpc.call("layers.add", kind="adjustment", adjustmentKind="Exposure", settings={"exposure": 0.5}, name="Brighter")
    assert adj["kind"] == "adjustment" and adj["name"] == "Brighter", adj
    got = rpc.call("adjustments.get", id=adj["id"])
    assert abs(got["settings"]["exposure"] - 0.5) < 1e-6, got
    # Hue/Saturation: the top-level sliders adjustments.get reports are honoured on set,
    # even though the manifest's hsvSettings block is what the core parser prefers.
    hsv = rpc.call("layers.add", kind="adjustment", adjustmentKind="Hue/Saturation", name="HS")
    applied = rpc.call("adjustments.set", id=hsv["id"], settings={"saturation": -100, "lightness": 5})
    assert abs(applied["settings"]["saturation"] + 100) < 1e-6, applied
    assert abs(applied["settings"]["lightness"] - 5) < 1e-6, applied
    rpc.call("layers.delete", id=hsv["id"])
    rpc.call("layers.select", id=target["id"])
    rpc.call("layers.select", id=target["id"])
    rpc.call("pixels.filter", kind="Gaussian Blur", radius=2)
    rpc.call("pixels.filter", kind="Lens Correction", distortion=20, bicubic=True)
    if info.get("scribble"):
        scribble = rpc.call("selection.scribble", foreground=[[[300, 200], [340, 210]]], background=[[[20, 20], [60, 20]]], size=16, clear=True)
        assert scribble["strokes"] == 2, scribble
        rpc.call("selection.none")
    rpc.call("selection.rect", x=40, y=40, width=30, height=30)
    rpc.call("pixels.contentAwareFill")
    rpc.call("selection.none")
    text = rpc.call("layers.add", kind="text", text="Hello", x=20, y=20, size=36, color="#ff8800")
    assert text["kind"] == "text" and text["text"]["text"] == "Hello", text
    assert text["pixelSize"]["width"] > 20 and text["pixelSize"]["height"] > 20, text
    edited = rpc.call("text.set", text="Hello there", bold=True, align="center")
    assert edited["text"]["bold"] and edited["text"]["align"] == "center" and edited["pixelSize"]["width"] > text["pixelSize"]["width"], edited
    rpc.call("layers.delete", id=text["id"])
    rpc.call("layers.select", id=target["id"])

    # Painting by coordinates: a stroke, a gradient and a shape layer.
    rpc.call("brush.stroke", points=[[20, 20], [120, 60], [220, 20]], size=12, color="#00ff00", opacity=1)
    rpc.call("gradient.draw", x0=0, y0=0, x1=200, y1=0, foreground="#0000ff", opacity=0.5)
    n = len(rpc.call("layers.list"))
    shape = rpc.call("shape.draw", kind="ellipse", x=300, y=100, width=120, height=80, color="#ff00ff")
    assert shape["kind"] == "shape", shape
    assert len(rpc.call("layers.list")) == n + 1
    assert rpc.call("app.info")["currentTab"] == 0

    # The eye swipe: press toggles, dragging across another eye sets it too, release ends one undo step,
    # and a second click still works (the rows are rebuilt underneath the gesture).
    layers = rpc.call("layers.list")
    a, b = [l for l in layers if l["depth"] == 0][:2]
    was = a["visible"]
    rpc.call("debug.eye", id=a["id"], action="press")
    assert rpc.call("layers.get", id=a["id"])["visible"] == (not was)
    rpc.call("debug.eye", id=a["id"], action="move", to=b["id"])
    assert rpc.call("layers.get", id=b["id"])["visible"] == (not was)
    rpc.call("debug.eye", id=a["id"], action="release")
    hist = rpc.call("history.info")
    assert hist["canUndo"] and "Layer" in hist["undo"], hist
    rpc.call("debug.eye", id=a["id"], action="press")
    rpc.call("debug.eye", id=a["id"], action="release")
    assert rpc.call("layers.get", id=a["id"])["visible"] == was
    rpc.call("history.undo", steps=2)
    assert rpc.call("layers.get", id=b["id"])["visible"] == b["visible"]

    # A new layer below the active one, for a fresh background.
    rpc.call("layers.select", id=target["id"])
    under = rpc.call("layers.add", kind="pixels", name="Backdrop", below=True)
    order = [l["id"] for l in rpc.call("layers.list")]
    assert order.index(under["id"]) == order.index(target["id"]) + 1, order

    # layers.get reports the real nesting depth, like layers.list, not a flat 0.
    folder = rpc.call("layers.add", kind="group", name="Depth Folder")
    child = rpc.call("layers.add", kind="pixels", name="Depth Child")
    rpc.call("layers.move", id=child["id"], parent=folder["id"])
    listed = [l for l in rpc.call("layers.list") if l["id"] == child["id"]][0]
    fetched = rpc.call("layers.get", id=child["id"])
    assert listed["depth"] > 0 and fetched["depth"] == listed["depth"], (listed, fetched)
    rpc.call("layers.delete", id=folder["id"])
    rpc.call("layers.select", id=target["id"])

    # History listing and a selection mask.
    names = rpc.call("history.list")
    assert names["undo"] and isinstance(names["undo"][-1], str), names
    rpc.call("selection.rect", x=0, y=0, width=50, height=50)
    mask = rpc.call("selection.render", maxSize=64)
    assert base64.b64decode(mask["png"])[:4] == b"\x89PNG", mask
    rpc.call("selection.none")

    # Loading a mask's black areas as a selection (as on the Mac): the hidden region is
    # what gets selected, not the revealed one. A reveal-all mask with a black patch in it
    # yields a selection exactly over the patch.
    rpc.call("layers.select", id=target["id"])
    layer = rpc.call("layers.get", id=target["id"])
    if layer.get("mask"):
        rpc.call("layers.mask", action="delete")
    rpc.call("layers.mask", action="add", revealing=True)   # reveal-all; the mask is now active
    rpc.call("selection.rect", x=12, y=14, width=20, height=22)
    rpc.call("pixels.fill", color="#000000")                # black = hidden
    rpc.call("selection.none")
    loaded = rpc.call("selection.fromLayer", mask=True)
    b = loaded["bounds"]
    assert (b["x"], b["y"], b["width"], b["height"]) == (12, 14, 20, 22), loaded
    rpc.call("selection.none")
    rpc.call("layers.mask", action="delete")
    rpc.call("history.undo")   # drop the mask edit itself
    print("mask selection polarity: hidden areas selected")

    # Events: after subscribing, an edit produces notifications before the next reply.
    rpc.call("events.subscribe", kinds=["layers", "history"])
    rpc.call("layers.set", id=target["id"], opacity=0.7)
    rpc.call("app.info")
    kinds = {e["kind"] for e in rpc.events}
    assert "layers" in kinds and "history" in kinds, rpc.events
    rpc.call("events.unsubscribe")
    rpc.call("history.undo")

    # The command-line client and batch mode.
    import os, subprocess
    binary = os.environ.get("COMPOSITOR_BIN", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "src", "app", "compositor-linux"))
    if os.path.exists(binary):
        env = dict(os.environ); env.pop("QT_QPA_PLATFORM", None)
        out = subprocess.run([binary, "--rpc-socket", path, "--call", "app.info"], capture_output=True, text=True, env=env, timeout=60)
        assert out.returncode == 0 and '"name": "compositor-linux"' in out.stdout, (out.returncode, out.stdout, out.stderr)
        bad = subprocess.run([binary, "--rpc-socket", path, "--call", "layers.get", "--params", '{"id": "nope"}'], capture_output=True, text=True, env=env, timeout=60)
        assert bad.returncode == 1 and "no layer" in bad.stderr, (bad.returncode, bad.stderr)
        script = '{"method":"document.new","params":{"width":64,"height":48}}\n{"method":"shape.draw","params":{"x":4,"y":4,"width":20,"height":20,"color":"#ff0000"}}\n{"method":"layers.list"}\n'
        batch = subprocess.run([binary, "--headless", "--batch", "-"], input=script, capture_output=True, text=True, env=env, timeout=120)
        lines = [json.loads(l) for l in batch.stdout.splitlines() if l.strip()]
        assert batch.returncode == 0 and len(lines) == 3 and len(lines[2]["result"]) == 2, (batch.returncode, batch.stdout, batch.stderr)
        print("--call and --batch ok")

    # G'MIC, when the executable is installed: a core command on the active layer, and the catalogue listing.
    cat = rpc.call("gmic.filters", search="sharpen")
    if cat["installed"]:
        rpc.call("layers.select", id=target["id"])
        applied = rpc.call("pixels.gmic", command="unsharp 2,1.5")
        assert applied["applied"].startswith("unsharp"), applied
        rpc.call("history.undo")
        if cat["catalogue"]:
            assert cat["filters"], cat
            print("gmic", cat["version"], "catalogue entries matching 'sharpen':", len(cat["filters"]))
        else:
            print("gmic", cat["version"], "(no catalogue file)")

    # Errors come back as errors, not crashes.
    try:
        rpc.call("layers.get", id="nope")
    except RuntimeError as e:
        print("expected error:", e)
    else:
        raise SystemExit("missing-layer lookup should have failed")
    methods = rpc.call("rpc.methods")
    print(len(methods), "methods; smoke test passed")


if __name__ == "__main__":
    main()
