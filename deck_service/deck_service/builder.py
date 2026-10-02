"""Build a weekly deck by calling Claude with code execution and the pptx skill.

Flow: call the model with agents/deck_builder.md as the system prompt -> download deck.pptx and
manifest.json from the model's sandbox -> render the PDF here -> run the automated checks ->
if checks fail, send the failures and page images back (same sandbox) for up to N repair rounds.
The best attempt is always returned. A person reviews every deck, so failed checks mark the
deck "needs_review" and never block it.
"""
import json
import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import os

from .checks import normalize_input, run_checks
from .render import RenderError, page_images_b64, render_pdf

log = logging.getLogger("deck_service")

WANTED = {"deck.pptx", "manifest.json"}
TOOLS = [{"type": "code_execution_20250825", "name": "code_execution"}]
SKILLS = [{"type": "anthropic", "skill_id": "pptx", "version": "latest"}]


class BuildError(RuntimeError):
    """The model never produced a usable deck."""


@dataclass
class Settings:
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 20000      # the SDK refuses non-streaming requests above ~21,000
    max_repairs: int = 2
    max_pauses: int = 0          # 0 = never continue a paused turn: the deck is saved in the first few steps
    max_calls: int = 6           # hard cap on model API calls for one request (all rounds)
    output_dir: Path = Path("/files/decks")
    instructions_path: Path = Path("agents/deck_builder.md")
    service_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(model=e("DECK_MODEL") or e("ANTHROPIC_MODEL") or cls.model, max_tokens=int(e("DECK_MAX_TOKENS", cls.max_tokens)),
                   max_repairs=int(e("DECK_MAX_REPAIRS", cls.max_repairs)),
                   max_pauses=int(e("DECK_MAX_PAUSES", cls.max_pauses)),
                   max_calls=int(e("DECK_MAX_CALLS", cls.max_calls)),
                   output_dir=Path(e("DECK_OUTPUT_DIR", str(cls.output_dir))),
                   instructions_path=Path(e("DECK_INSTRUCTIONS_PATH", str(cls.instructions_path))),
                   service_token=e("SERVICE_SHARED_SECRET", ""))


@dataclass
class BuildResult:
    request_id: str
    status: str
    files: dict
    slide_count: int
    results: list
    repairs: int
    usage: dict = field(default_factory=dict)


def _files_api(client):
    return getattr(client, "files", None) or client.beta.files


def _run_turn(client, settings, system, messages, container_id, calls=None, request_id=""):
    """One model turn, following pause_turn continuations. Returns
    (messages_with_final_assistant, all_blocks, container_id, usage). `calls` is a one-item list holding the
    number of API calls made so far for this request; it is capped by settings.max_calls."""
    calls = calls if calls is not None else [0]
    blocks, usage = [], {"input_tokens": 0, "output_tokens": 0}
    for _ in range(settings.max_pauses + 1):
        if calls[0] >= settings.max_calls:
            raise BuildError(f"stopped: more than {settings.max_calls} model calls for one request")
        container = {"skills": SKILLS}
        if container_id:
            container["id"] = container_id
        t0 = time.time()
        resp = client.messages.create(model=settings.model, max_tokens=settings.max_tokens, system=system,
                                      messages=messages, tools=TOOLS, container=container)
        calls[0] += 1
        blocks.extend(resp.content)
        u = resp.usage
        i_tok = getattr(u, "input_tokens", 0) or 0
        o_tok = getattr(u, "output_tokens", 0) or 0
        usage["input_tokens"] += i_tok
        usage["output_tokens"] += o_tok
        tool_steps = sum(1 for b in resp.content if getattr(b, "type", "") in
                         ("server_tool_use", "bash_code_execution_tool_result", "text_editor_code_execution_tool_result"))
        # sizes and counts only, never content
        log.info("model call request=%s n=%d stop=%s input=%d output=%d cache_read=%s cache_write=%s tool_blocks=%d secs=%.0f",
                 request_id, calls[0], getattr(resp, "stop_reason", "?"), i_tok, o_tok,
                 getattr(u, "cache_read_input_tokens", None), getattr(u, "cache_creation_input_tokens", None),
                 tool_steps, time.time() - t0)
        c = getattr(resp, "container", None)
        container_id = getattr(c, "id", None) or container_id
        if getattr(resp, "stop_reason", None) == "max_tokens":
            # the reply was cut off mid-step (usually inside the build script); continuing would send an
            # invalid conversation, so fail with a clear message instead of a 400 from the API
            raise BuildError(f"model output was cut off at max_tokens={settings.max_tokens} before the deck was saved")
        messages = messages + [{"role": "assistant", "content": resp.content}]
        if resp.stop_reason != "pause_turn":
            break
    return messages, blocks, container_id, usage


def _extract_files(client, blocks, dest: Path) -> list[str]:
    """Download deck.pptx / manifest.json produced in the sandbox. The latest copy of each name wins."""
    ids = {}
    for b in blocks:
        if getattr(b, "type", "") != "bash_code_execution_tool_result":
            continue
        c = b.content
        if getattr(c, "type", "") != "bash_code_execution_result":
            continue
        for f in (c.content or []):
            name = Path(_files_api(client).retrieve_metadata(f.file_id).filename).name
            if name in WANTED:
                ids[name] = f.file_id
    dest.mkdir(parents=True, exist_ok=True)
    for name, fid in ids.items():
        (dest / name).write_bytes(_files_api(client).download(fid).read())
    return sorted(ids)


def _check(fx, rdir: Path, renderer, imager):
    """Render the PDF, run the checks. Returns (results, page_images)."""
    try:
        renderer(rdir / "deck.pptx", rdir)
    except RenderError as e:
        return [("renders_to_pdf", False, str(e)[:300])], []
    results = run_checks(fx, rdir)
    try:
        images = imager(rdir / "deck.pdf", rdir)
    except RenderError:
        images = []
    return results, images


def _repair_message(failed, images):
    lines = "\n".join(f"- {n}: {d[:300] or 'failed'}" for n, _, d in failed)
    text = ("Automated checks found problems with your deck:\n" + lines +
            "\n\nFix exactly these in your build script, regenerate deck.pptx and manifest.json, and save both to "
            "$OUTPUT_DIR. Do not change anything that already passed. Rendered pages of your current deck are attached.")
    content = [{"type": "text", "text": text}]
    for b64 in images:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}})
    return {"role": "user", "content": content}


def build_deck(inp: dict, client, settings: Settings,
               renderer: Callable = render_pdf, imager: Callable = page_images_b64) -> BuildResult:
    """inp is the adapter's output (adapter.build_deck_input)."""
    request_id = uuid.uuid4().hex[:12]
    system = settings.instructions_path.read_text()
    payload = {k: v for k, v in inp.items() if k != "options"}   # what the model sees
    fx = normalize_input(inp)                                    # what the checks use
    messages = [{"role": "user", "content": "Build this week's leadership deck from this input.\n\n```json\n"
                 + json.dumps(payload, indent=2) + "\n```"}]
    work = Path(tempfile.mkdtemp(prefix="deck_"))
    usage = {"input_tokens": 0, "output_tokens": 0}
    container_id, repairs, rounds = None, 0, []
    try:
        calls = [0]
        while True:
            messages, blocks, container_id, u = _run_turn(client, settings, system, messages, container_id, calls, request_id)
            usage = {k: usage[k] + u[k] for k in usage}
            rdir = work / f"round{repairs}"
            written = _extract_files(client, blocks, rdir)
            missing = sorted(WANTED - set(written))
            if missing:
                results, images = [("outputs_exist", False, f"model did not save {missing} to $OUTPUT_DIR")], []
            else:
                results, images = _check(fx, rdir, renderer, imager)
                rounds.append((rdir, results))
            failed = [r for r in results if not r[1]]
            log.info("deck build request=%s round=%d files=%d failed_checks=%d calls=%d input_tokens=%d output_tokens=%d failed_names=%s",
                     request_id, repairs, len(written), len(failed), calls[0], usage["input_tokens"], usage["output_tokens"],
                     ",".join(r[0] for r in failed) or "none")
            if not failed or repairs >= settings.max_repairs:
                break
            messages = messages + [_repair_message(failed, images)]
            repairs += 1

        if not rounds:
            raise BuildError("the model never saved both deck.pptx and manifest.json")
        # best round = fewest failed checks, latest on ties
        best_dir, best_results = min(reversed(rounds), key=lambda r: sum(1 for x in r[1] if not x[1]))
        final = settings.output_dir / f"{inp['report_date']}_{request_id}"
        final.mkdir(parents=True, exist_ok=True)
        files = {}
        for key, name in (("pptx", "deck.pptx"), ("manifest", "manifest.json"), ("pdf", "deck.pdf")):
            if (best_dir / name).exists():
                shutil.copy2(best_dir / name, final / name)
                files[key] = str(final / name)
        manifest = json.loads((best_dir / "manifest.json").read_text())
        n_failed = sum(1 for x in best_results if not x[1])
        return BuildResult(request_id=request_id, status="ok" if n_failed == 0 else "needs_review", files=files,
                           slide_count=int(manifest.get("slide_count", 0)), results=best_results,
                           repairs=repairs, usage=usage)
    finally:
        shutil.rmtree(work, ignore_errors=True)
