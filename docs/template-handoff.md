# Template IR — Handoff

Goal of this work: generated decks should actually look like the chosen
template. They did not. Every cause found so far was the IR or the exporter
dropping a fact, never the model refusing to follow instructions.

## Where things stand

Last commit is still `cf50718`. **Everything since is uncommitted** and spans
`deeppresenter/templates/`, `deeppresenter/html2pptx/`, `pptagent/apis.py`,
`pptagent/editor/service.py`, and one frontend file. 224 tests pass:

```bash
uv run pytest deeppresenter/templates/tests/ deeppresenter/server/tests/ deeppresenter/test/ -q
```

The user's uploaded template in `<project-root>/userdata/templates_api/` is
compiled and current (`rev_0fc5b536473958f08ebb0c8a`).

**The six bundled templates in `pptagent/templates/` are badly stale.** Their
active revisions predate most of this work, so they still carry rectangles
where the template has triangles, opaque panels where it has translucent ones,
and black text where it has white. The user was asked twice whether to
recompile them and has not answered. Ask again before assuming.

## The one thing to internalise

**When the output is wrong, suspect the IR before the model.** Every single
defect this session followed the same shape: a fact existed in the PPTX, the
extractor dropped it or recorded it wrongly, the scaffold emitted something
plausible-but-false, and the model faithfully rendered the false thing. In
every case the model had obeyed.

Concretely, the ones that were found by *measuring* rather than reading code:

- Region z-index came from reading order, so the cover photo (real z=0)
  painted over the title. Found with `elementFromPoint` in a browser.
- The logo looked mis-layered; the stacking was correct. It was a *white*
  logo variant the model had fetched itself, invisible on a white ground.
- `»` chevrons exported as blue rectangles: the group carried the fill and the
  children declared `<a:grpFill/>`, so we painted the container and dropped
  the children.

When you add a test for a bug, revert the fix and confirm the test fails. Two
tests in this session asserted nothing until that check caught them — one
because a synthetic fixture bypassed the code path entirely.

## How it works now

Compile once, generate from the frozen result.

- `extractor.py` + `styles.py` walk the PowerPoint inheritance chain to get
  the style a viewer actually sees. **Read the shape's own XML first**;
  python-pptx silently loses `lumMod`/`lumOff`/`alpha` on fills and returns
  nothing at all for `schemeClr`/`prstClr` text. Both cost a full debugging
  cycle.
- `scaffold.py` compiles each page to a `layout.css`. `.slide .tpl-*` is
  chrome, `.slide .r-*` is a content region. **Every rule paints itself**,
  imagery included, so a page only ever emits empty `<div>`s. Handing the
  model an asset id instead made it fetch its own copy.
- Chrome and content share **one** stacking order, taken from the template's
  own paint order. Do not reintroduce a separate content band.
- Region text colour is bound to the text elements by name
  (`.slide .r-x :is(p,h1,…)`), not left to inherit — inheritance loses to any
  direct declaration whatever its specificity, so a page's own `p` rule
  silently won.
- Asset roles come from facts, not name/size heuristics: `scope != slide` is
  chrome; a non-trivial alpha mask means artwork cut for that slot. Only what
  remains genuinely ambiguous goes to the annotator's `replaceable_images`
  verdict, which is deliberately conservative (unsure ⇒ keep the template's).

### Geometry

Non-rectangular shapes are `clip-path: polygon()` in percentages so they scale.
`clip-path: path()` is not usable — it resolves in absolute pixels.

- Bezier and arc segments are flattened in `extractor.py` (`_flatten_path`).
- One `<a:path>` may hold several subpaths; the extra ones are holes. They are
  threaded into one contour with `evenodd`, entering and leaving each hole
  **along the same line**. Bridges a hair apart leave a visible white slit.
- Preset adjust handles (`parallelogram`, `homePlate`, `roundRect`, …) are read
  from `avLst`, never assumed. The defaults are wrong on real templates —
  the cover band's `adj` is 76100 against a 25000 default.
- A stroked path with no fill (line-art icons) cannot be done with
  `border` + `clip-path`: the border follows the box and the clip shreds it.
  Those emit an inline SVG `<path>` data URI with `vector-effect:
  non-scaling-stroke`.
- Groups: `chOff`/`chExt` map children onto the slide, and `flipH`/`flipV`
  propagate down. Without the transform, three identical card groups stack in
  one place.

## Traps that already bit

**Revision identity must cover anything that changes output.** It hashes
source + compiler/extractor/annotator/renderer versions, and
`_output_fingerprint()` hashes `scaffold.py`, `overview.py`, `rendering.py`,
`annotation.py`. Editing a module outside that set produces a recompile that
silently reuses a stale revision and reports success in 0s. If you add another
module that writes into a revision, add it to the fingerprint. `PROMPT_VERSION`
in `annotation.py` hashes the prompt text for the same reason.

**A truncated traceback keeps its head, which is boilerplate.** The
conversational editor reported `res[1][:500]`, which threw away the exception
and printed a `<string>` frame that linecache resolves to the process's spawn
command — so a real error read as `from multiprocessing.spawn import
spawn_main`. `execute_actions` now leads with the exception and the failing
call. Do not undo that; it hid a crash for an entire session.

**The browser preview being right does not mean the export is.**
`html2pptx.js` had no notion of `clip-path` at all, so every shaped element
became its bounding rectangle in the PPTX while the preview looked perfect.
It also ignored `transform` on rasterized elements and sized them from
`getBoundingClientRect()`, which reports the *rotated* box. After any scaffold
change that adds a CSS property, export a deck and render it:

```bash
uv run python -c "
import asyncio; from pathlib import Path
from deeppresenter.utils.webview import convert_html_to_pptx
asyncio.run(convert_html_to_pptx(Path('<task>/slides'), Path('/tmp/out.pptx'), aspect_ratio='16:9'))"
```

then rasterize `/tmp/out.pptx` with `LibreOfficeRenderer` and look at it.

## Recompiling

The user's uploaded template (uses the real VLM annotator):

```python
# needs vision_model in deeppresenter/config.yaml; ~6 min
from deeppresenter.server.services.template_service import (
    TemplateInductionService, TemplateSettings)
from deeppresenter.templates import (
    DeepPresenterStructuredVLMClient, LibreOfficeRenderer,
    TemplateCompiler, VLMAnnotator)
from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.constants import PROJECT_ROOT
import asyncio

config = DeepPresenterConfig.load_from_file("deeppresenter/config.yaml")
model = config.vision_model or config.design_agent
service = TemplateInductionService(
    workspace=PROJECT_ROOT / "userdata/templates_api",
    compiler=TemplateCompiler(
        renderer=LibreOfficeRenderer(required=True),
        annotator=VLMAnnotator(
            DeepPresenterStructuredVLMClient(model), model_id=model.model_name),
    ),
    settings=TemplateSettings(),
)
print(asyncio.run(service.run_induction("tpl_template_fb2fe79e8ab436e4")).active_revision_id)
```

The bundled six have a CLI, `deeppresenter/templates/compile_bundled.py`, but
it uses `DeterministicAnnotator` — no page images, so no `replaceable_images`
verdicts and the asset roles fall back to the extractor's own reasoning. If the
bundled templates should get VLM verdicts too, that entrypoint needs the same
wiring as above.

Always verify the recompile actually landed rather than reusing a revision:

```bash
R=<project-root>/userdata/templates_api/templates/tpl_template_fb2fe79e8ab436e4/revisions/<rev>
grep -c 'clip-path' $R/slides/*/layout.css
```

The user's cache now holds **11 revisions, 150 MB**. Only the manifest's
`active_revision_id` matters. They have not agreed to pruning — ask.

## Verifying a template renders correctly

Render the scaffold **with no page content** and compare against the
template's own reference image. Anything missing from the left that is not
text is a bug in the IR:

```python
# materialize a task pack, emit only empty divs for every .tpl-*/.r-* class,
# screenshot at 1280x720, paste beside revisions/<rev>/slides/<sid>/reference.webp
```

This is how every geometry defect in this session was found and confirmed.

## Open questions for the user

- **`font-family` is still adjustable.** Colour is locked to the template;
  size, weight, and alignment are deliberately free (their call). Font family
  currently sits with the free group. They were asked whether to lock it and
  answered only "ok", which was ambiguous, so it was left alone.
- **Bundled template recompile** — see above.
- **Cache pruning** — see above.

## Known gaps

- **Bleed regions get content.** A region at `left:-15%` is a bleed
  decoration, but nothing stops the model putting a content image there.
  Either score bleed regions down in `search_template_references` or say so in
  the Design role.
- **The single-slide editor has no retry.** `pptgen` feeds the executor's
  error back to the model and retries; `SlideEditService._run_api_actions`
  runs once and gives up. A correctable mistake (a stale element id) surfaces
  to the user as a hard failure. The plumbing for it is all there.
- **`background-size:100% 100%`** matches PowerPoint's stretch, so artwork
  whose frame aspect differs from the image will distort rather than letterbox.
  Deliberate, but it is a trade.
- **Presets without an implemented adjust formula** (`pentagon`, `hexagon`,
  arrows) fall back to plain rectangles by design — a polygon built on a
  guessed handle is a wrong shape, which is worse. Add them to
  `_ADJUSTED_PRESETS` with the real ECMA-376 formula if a template needs them.

## Working notes

- Reply to this user in Chinese; think in English.
- They push their own commits — check `git log` before assuming state.
- `deeppresenter/config.yaml` is gitignored. `heavy_reflect: true` was added
  there this session, which is what makes `inspect_slide` actually render the
  slide and hand the image back. With it `false`, the tool returns the literal
  string "This slide is valid." and the agent never sees its own output.
- Compiled templates live in the repo (~60 MB). They asked to commit code and
  artifacts separately.
- `pptagent/test/` cannot be collected without `OPENAI_API_KEY` (its conftest
  builds a `ModelManager`), which is why the editor's tests live in
  `deeppresenter/test/test_slide_edit_failure.py`.
