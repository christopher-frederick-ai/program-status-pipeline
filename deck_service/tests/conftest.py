import hashlib, json, shutil, sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
A = ROOT / "tests" / "assets"


def asset(name, ext):
    return (A / f"{name}.{ext}").read_bytes()


class FakeFiles:
    def __init__(self):
        self.store = {}
    def add(self, filename, data):
        fid = f"file_{len(self.store)}"
        self.store[fid] = (filename, data)
        return fid
    def retrieve_metadata(self, file_id):
        return NS(filename=self.store[file_id][0])
    def download(self, file_id):
        data = self.store[file_id][1]
        return NS(read=lambda: data)


class FakeMessages:
    def __init__(self, files):
        self.files, self.script, self.calls = files, [], []
    def queue(self, deck=None, stop="end_turn", extra_files=None):
        """deck: 'good' | 'bad' | None (no files saved)."""
        ids = []
        if deck:
            ids.append(self.files.add("deck.pptx", asset(deck, "pptx")))
            ids.append(self.files.add("manifest.json", asset(deck, "json")))
        for name, data in (extra_files or {}).items():
            ids.append(self.files.add(name, data))
        content = [NS(type="text", text="done")]
        if ids:
            content.append(NS(type="bash_code_execution_tool_result",
                              content=NS(type="bash_code_execution_result", content=[NS(file_id=i) for i in ids])))
        self.script.append(NS(content=content, stop_reason=stop, usage=NS(input_tokens=10, output_tokens=5),
                              container=NS(id="cont_1")))
    def create(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.messages = FakeMessages(self.files)


PDF_FOR = {hashlib.sha256(asset(n, "pptx")).hexdigest(): asset(n, "pdf") for n in ("good", "bad")}


def fake_renderer(pptx, outdir):
    (Path(outdir) / "deck.pdf").write_bytes(PDF_FOR[hashlib.sha256(Path(pptx).read_bytes()).hexdigest()])


def fake_imager(pdf, workdir):
    return ["iVBORw0KGgo="]


@pytest.fixture
def payload():
    fx = json.loads((ROOT / "fixtures" / "week_2026-09-24.json").read_text())
    for k in ("_comment", "expect"):
        fx.pop(k, None)
    return fx


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def settings(tmp_path):
    from deck_service.builder import Settings
    return Settings(output_dir=tmp_path / "decks", instructions_path=ROOT / "agents" / "deck_builder.md",
                    service_token="s3cret", max_repairs=2)


@pytest.fixture
def api(client, settings):
    from fastapi.testclient import TestClient
    from deck_service.app import create_app
    app = create_app(client_factory=lambda: client, settings=settings, renderer=fake_renderer, imager=fake_imager)
    return TestClient(app)
