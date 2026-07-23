# Template IR Architecture

## Purpose

Template IR replaces the legacy PPTAgent layout engine in the DeepPresenter
generation path. A PowerPoint template is compiled once into an immutable,
versioned description. Presentation generation then uses that description as
design context while producing normal DeepPresenter HTML slides.

The primary invariant is:

> Parsing may read a source PPTX. Generation may read only a READY, pinned
> Template IR revision and its task-local context pack.

There is no lazy parsing, legacy-layout fallback, or source-PPTX access during
generation.

## Parse and generation boundary

### Parse phase

`TemplateCompiler` owns the offline or background parse phase. It:

1. validates and copies the source PPTX for provenance;
2. extracts deterministic slide, shape, text, theme, and media facts;
3. renders each page to a reference image and a shape overlay whose short
   `#N` labels match the compact shape listing sent to the annotator;
4. asks a VLM to annotate each page independently, or uses the deterministic
   annotator when explicitly requested. The VLM answers only the semantic
   question: how the labeled shapes group into regions and what the page
   means. A schema-invalid response is retried with the validation error fed
   back; validated per-slide annotations are cached next to the template until
   the revision publishes, so a failed compile resumes instead of re-buying
   every annotation;
5. derives every region's geometry, capacity, and asset bindings from the
   bound source shapes — an annotator can never invent coordinates or assets;
6. builds layout families and bounded per-page contexts;
7. validates and hashes the result; and
8. atomically publishes a READY revision.

The revision ID is derived from the source hash plus compiler, extractor,
annotator, and renderer identities; the VLM annotator identity includes a
fingerprint of its prompt, so editing the prompt always produces a new
revision. Recompiling the same inputs reuses the same revision. A changed
input or output-affecting compiler version produces a new revision; a READY
revision is never rewritten.

### Generation phase

Task creation resolves one revision through `TemplateStore`, pins its ID, and
materializes a self-contained context pack inside the task workspace before the
task enters the execution queue. The Design agent reads only that pack,
retrieves at most a few matching reference pages into model context, and writes
HTML. It never reopens the global store. Deleting or activating another global
template revision after task creation therefore cannot change or break the
queued task. The normal HTML-to-PPTX/PDF exporter remains the only layout and
export path.

`TemplateStore` enforces the boundary at the filesystem layer. It rejects
`source/`, `.pptx`, path traversal, escaping symlinks, malformed indexes,
non-READY revisions, and integrity mismatches. A source-only legacy template
raises `LegacyTemplateError` and must be compiled before use.

## On-disk format

Template IR schema version `2.0` uses the following layout:

```text
<templates-root>/<template-id>/
├── manifest.json
└── revisions/
    └── <revision-id>/
        ├── revision.json
        ├── source/
        │   └── original.pptx          # parse provenance; runtime-forbidden
        ├── theme/
        │   ├── theme.json
        │   └── theme.css
        ├── assets/
        │   ├── index.json
        │   ├── <asset-id>.<ext>
        │   └── <asset-id>.browser.png # when the source format is not web-safe
        ├── families/
        │   └── index.json
        ├── slides/
        │   ├── index.jsonl
        │   └── <slide-id>/
        │       ├── source_graph.json
        │       ├── semantic.json
        │       ├── context.compact.json
        │       ├── reference.webp     # optional rendered reference
        │       └── overlay.webp       # optional shape-bound overlay
        └── validation/
            └── report.json
```

`manifest.json` records template status, display metadata, revision history,
and the active revision. `revision.json` records compiler identities, source
hash, canvas, artifact paths, timestamps, and SHA-256 hashes for generated IR
files.

The main IR layers are:

- **Source graph:** canvas, ordered shapes, exact and normalized bounds, text
  runs, placeholder types, fill/line styles, crop data, scope
  (`slide`/`layout`/`master`), and asset bindings.
- **Semantic page:** stage (`cover`, `agenda`, `section`, `content`, `summary`,
  `closing`, or `appendix`), layout and message patterns, density, modalities,
  page-level reading order, selection hints, and avoid conditions.
- **Semantic region:** role and kind chosen by the annotator, plus bounds,
  capacity, asset bindings, reflow/resize behavior, and style reference
  derived from the bound source shapes. Every region binds at least one
  extracted shape. Typical roles include title, body, chart, table, diagram,
  photo, metric, logo, footer, decoration, and background.
- **Compact context:** the bounded subset required by generation: page
  semantics, regions, family IDs, theme tokens, reusable asset IDs, and
  reference paths. Capacity numbers stay in `semantic.json` as facts about
  the sample and are deliberately not served to the model: they are measured
  around placeholder text and read as writing constraints.
- **Layout families:** groups of semantically and spatially similar pages, with
  a representative page, selection hints, and avoid conditions.

### Layout scaffold contract

Every page compiles to a `layout.css` scaffold with three rule tiers:

- `.tpl-*` chrome rules reproduce the template's identity: decoration the
  layout and master put on every page (logos, colour bands, the page
  background), plus slide-drawn artwork promoted by the tier decision below.
  Exact, written at guarded `.slide .tpl-*` specificity, and generation must
  emit them verbatim as empty `<div>`s. A region painted from a template
  asset keeps the same guarded specificity.
- `.dec-*` rules reproduce the rest of the sample slide's own decoration
  (card frames, arrows, sample icons). Included by default as empty
  `<div>`s, but single-class and droppable: they follow the content they
  were drawn around, and pinning them onto a rearranged page paints sample
  artwork across new content.
- `.r-*` region rules record how the *sample* page arranged its content:
  geometry, typography, and colour in one single-class rule per region. They
  are defaults, not constraints — a generated page retunes them by
  redeclaring the class after the import. When the sample's text sat on paint
  the scaffold does not reproduce (a card fill or photo inside a content
  region), the colour is omitted and the rule's comment says so, because a
  colour without its ground is how white-on-white pages happen.

**Tier decision for slide-drawn decoration.** Scope records where an author
drew a shape, not what it is — brand wedges hand-copied onto the cover and
the closing page are slide-scope yet template identity. A slide-drawn shape
is therefore promoted to `.tpl-*` only on converging evidence:

1. *Recurrence* (mechanical): its signature — rounded geometry, kind, fill,
   line, rotation — appears on at least two pages of the revision. Only a
   whole-revision pass can observe this, so the compiler calls
   `recurring_chrome_signatures` once and threads the result into every
   page's scaffold. The rule lives in `scaffold.py` so that changing it
   changes `COMPILER_VERSION`, which is fingerprinted from the modules that
   decide a revision's contents.
2. *No annotator veto* (semantic): during annotation the VLM receives the
   slide's decoration shapes under `d1…dN` labels and lists the ones that
   are fixed page furniture in `fixed_decorations`. The verdict is
   deliberately conservative toward movable: modular, per-item artwork —
   capsules behind process steps, card frames, arrows, icon holders — is
   never fixed, and an unsure annotator omits the label. A judged shape that
   was not listed stays `.dec-*` even when it recurs; the verdict can veto a
   promotion but never force one. Legacy revisions without verdicts fall
   back to recurrence alone.

The hard tier (`.tpl-*` and painted template assets) is exactly the
template's visual identity; everything sized, coloured, or drawn around
sample content is soft. Enforcement is mechanical (specificity, omission,
and the `inspect_slide` lint, which reports missing hard-tier divs and
template artwork buried under opaque content), not exhortative.

### Canvas ratio contract

Template IR currently supports only `16:9` and `4:3` slide canvases. The
compiler rejects other ratios before annotation or publication. The pinned
revision's `canvas.aspect_ratio` is authoritative for HTML, PPTX, PDF, API,
and frontend display. Task creation returns HTTP 422 when a caller explicitly
requests a different ratio; non-HTTP task creation enforces the same check.
Selecting a template in the frontend updates the template ID and ratio
atomically, and the ratio is displayed as template-owned rather than as an
independent generation setting.

## Assets, logos, and backgrounds

PowerPoint media is extracted as content-addressed files. `assets/index.json`
stores each asset's ID, hash, media type, dimensions, role, reuse policy,
occurrences, normalized bounds, and optional tags or alt text. Duplicate media
is stored once while retaining every slide/layout/master occurrence.

Every asset also records an optional browser representation (`browser_path`,
media type, hash, and dimensions). Web-safe PNG/JPEG/WebP/GIF/SVG/AVIF assets
point to their original content-addressed file. Reusable WMF/EMF or unsupported
raster assets are converted to a PNG during compilation; compilation fails if
a logo, background, or decoration cannot be made browser-readable. Generation
therefore never performs media conversion or asks the model to approximate an
unrenderable brand asset.

Roles and default policies are:

| Role | Default policy | Generation behavior |
| --- | --- | --- |
| `logo` | `always` | Stage for direct HTML reuse. |
| `background` | `template_only` | Stage as a reusable template asset. |
| `decoration` | `template_only` | Stage when relevant to the selected style. |
| `content_image` | `reference_only` | Use to understand the sample page; do not copy as new content. |
| `unknown` | `never` | Do not reuse. |

Role inference uses explicit background data, source names, placement, size,
scope, and recurrence across pages. The theme points directly to known logo,
background, and decoration asset IDs. This preserves brand marks, textures,
and non-HTML artwork instead of asking the model to imitate them.

Extracted reusable assets may be placed in generated HTML. A rendered full-page
reference image must never be used as the generated slide background: it is a
visual example, not a reusable layout surface.

## Revision pinning and task context packs

If a request omits a revision, `TemplateStore` resolves the manifest's active
READY revision exactly once. The resolved revision ID is persisted with the
task and used for every later read. Changes to the template's active revision
therefore cannot alter a running task.

Before task creation returns, `TemplateContextProvider.materialize(...)`
creates an immutable task-local pack:

```text
<task-workspace>/template_context/
├── snapshot.json
├── overview.md
├── overview.json
├── selection.json
├── theme.json
├── theme.css
├── assets/
│   ├── index.json
│   └── <reusable assets>
└── refs/
    └── <slide-id>/
        ├── context.compact.json
        └── <one reference image>
```

`snapshot.json` pins the template ID, revision ID/hash, IR schema version,
selection request hash, asset/reference inventory, and file hashes. The pack
contains the bounded semantic context and one style image for every template
page so later per-slide retrieval remains task-local; only the 1–2 selected
matches enter the model conversation. Publishing is atomic and idempotent. The
pack is integrity-checked again when generation starts. The sandboxed agent
uses only these task-local paths; global template roots and source PPTX files
are not exposed.

## Retrieval tools

The agents receive bounded tools backed only by the verified,
revision-pinned task context pack, split by stage:

Available from the outline stage onward (Planner, Research, Design):

- `get_template_overview()` returns theme, canvas, family, slide-count, and
  asset-role summaries.
- `get_family_detail(family_id)` expands one family's structure, selection
  hints, and member pages.

The outline and manuscript decide each page's structure — how many parallel
items, whether there is an image slot — and those decisions only fit the
template when made against its layout families. The Planner records a
`template_family` per outline page (or `null` when nothing fits), and
Research cuts each page's content to that family's structure.

Available only to the Design agent:

- `search_template_references(...)` ranks pages by stage, layout pattern,
  message pattern, modalities, density, region roles, families, and keywords.
  It returns at most two compact matches.
- `get_template_reference(slide_id)` returns one compact semantic page context
  and, when available, one ephemeral reference image.

The model should first plan slide needs, retrieve only the closest references,
and record selected reference IDs and asset IDs in `slides/design_plan.json`.
It must adapt the template's visual grammar to the new content rather than copy
sample text or coordinates mechanically.

## Context budget contract

Every model call performs a preflight estimate before sending the request. The
effective input budget is:

```text
context limit
- max(configured output reserve, requested completion tokens)
- safety margin
```

Preflight includes messages, tool and response schemas, and conservative image
costs. It folds summarizable history before the configured trigger ratio and
fails explicitly if the request still cannot fit. Template overview and image
limits are checked independently, so template data is never silently cut in a
way that changes its meaning.

Messages use four retention layers:

- `PINNED`: system instructions and the task's template identity/overview;
  re-injected after every fold.
- `EPHEMERAL`: reference image payloads; available for one model call, then
  replaced by a small textual reference.
- `SUMMARIZABLE`: normal dialogue and tool results; eligible for folding.
- `EXTERNAL`: material kept in files and retrieved only when needed.

Large IR files, all-page renders, and base64 image payloads must not accumulate
in chat history. Provider responses have hard size bounds, and only the compact
overview plus selected references enter model context.

## CLI lifecycle

Compilation and generation are separate commands:

```bash
# Parse and publish a revision. Uses VLM annotation by default.
pptagent template-compile template.pptx \
  --template-id corporate \
  --templates-root ~/.cache/deeppresenter/templates

# Use deterministic annotations when no multimodal model is configured.
pptagent template-compile template.pptx --template-id corporate --deterministic

# Generate from an already READY revision.
pptagent generate "Quarterly business review" \
  --output qbr.pptx \
  --template corporate \
  --template-revision <revision-id>
```

`generate` never invokes `template-compile`. Omitting `--template-revision`
pins the active revision at startup; providing it makes the run reproducible.
Packaged templates are compiled separately by maintainers with
`python -m deeppresenter.templates.compile_bundled`.

## API lifecycle

1. `POST /api/templates` accepts a PPTX, stores the upload, and starts an
   independent compilation job.
2. Clients monitor `template.parse_*`, `template.ready`, and `template.failed`
   events, or query `GET /api/templates/{template_id}`.
3. Failed jobs may be retried with
   `POST /api/templates/{template_id}/retry`; user templates may be deleted
   with `DELETE /api/templates/{template_id}`.
4. `POST /api/tasks` accepts `template_id` and optional
   `template_revision_id`. Task creation resolves a READY revision, materializes
   and verifies the task-local context pack, then persists both identities;
   missing, compiling, failed, legacy-only, unsupported-ratio, or
   ratio-conflicting templates are rejected.
5. Task execution loads that fixed local snapshot, runs the HTML Design agent,
   and exports through the standard DeepPresenter converter. It does not need
   the global template to remain present.

Template parsing progress and presentation-generation progress are distinct
lifecycles. A generation worker is never responsible for making a template
READY.

## Migration

- Compile every bundled `pptagent/templates/<id>/source.pptx` as a release or
  build step. Do not compile it on first generation.
- Compile existing user templates with `template-compile` or re-upload them;
  pass the old stable ID through `--template-id` when identity must be kept.
- Treat `slide_induction.json`, legacy layout schemas, and PPTAgent layout
  selection as migration input only, not runtime dependencies.
- Update task producers to send `template_id` and, for reproducibility, the
  resolved `template_revision_id`.
- Remove source-only template directories after their READY IR revisions and
  backups have been verified; source-only directories intentionally fail at
  generation time.

The standalone legacy `pptagent-mcp` entrypoint is outside this DeepPresenter
generation contract. It must not be called from the primary Design pipeline.

## Prohibited behavior

- Opening a source PPTX, running LibreOffice, or calling `TemplateCompiler`
  from a generation task.
- Invoking the PPTAgent layout engine or silently falling back to legacy
  induction data.
- Choosing the newest revision directory by timestamp or re-resolving an active
  revision after the task has been pinned.
- Reading the global template store from Design tools instead of the task-local
  snapshot.
- Mutating files in a READY revision.
- Sending the full IR, all slide images, or unbounded file contents to a model.
- Keeping inline reference-image bytes in persistent chat history.
- Using a whole rendered reference page as a generated-slide background.
- Reusing content or unknown images against their reuse policy.
- Accepting model semantics that reference unknown shapes or assets.
- Ignoring integrity, path-sandbox, validation, or context-budget failures.
