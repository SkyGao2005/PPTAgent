# Frontend Commit Plan

## Scope and constraints

This is a retrospective, file-level split of the existing frontend work into ten commits.

- Existing implementation files are not modified for this split.
- Each file is assigned to exactly one commit; no partial-file staging is used.
- Cross-cutting files such as `workbench-page.tsx`, `workbench-store.ts`, and
  `mock-server.ts` contain work from several days. They are assigned to the day
  that best represents their primary responsibility.
- Because the current implementation was not originally committed day by day,
  intermediate commits are not guaranteed to build independently. The complete
  build and regression check belongs to Day 9.
- Day 1 and Day 2 are committed and pushed first. Day 3 through Day 10 remain
  uncommitted until their corresponding commit is intentionally prepared.

## Ten-commit overview

| Day | B1 | B2 | Result | Commit message |
|---|---|---|---|---|
| 1 | Three-column sketches and visual tokens | User flow and state inventory | Freeze page structure and primary interactions | `docs(frontend): freeze workbench UX and visual system` |
| 2 | Initialize project and base layout | SSE mock and state stores | Establish the mock task progress foundation | `feat(frontend): scaffold app and mock task state` |
| 3 | Shared components and page skeletons | Create task page and template list | Create a mock task and enter the workbench route | `feat(frontend): add task creation and template library pages` |
| 4 | Thumbnail list and central preview | Template parsing progress | Make the main workbench surface available | `feat(frontend): build the presentation workbench` |
| 5 | Consume real page events | Connect real task events over SSE | Freeze the real frontend API and event contract | `feat(frontend): connect task and template event APIs` |
| 6 | Revision UI | Current-slide chat and quick commands | Support slide revisions and editing commands | `feat(frontend): add slide revision and chat controls` |
| 7 | Undo and preview cache handling | Reconnect and refresh recovery | Restore task state and versioned previews | `feat(frontend): restore sessions and versioned previews` |
| 8 | Error, cancel, and export states | Run details and failed-slide retry | Freeze P0 interaction states | `feat(frontend): add task recovery and run detail surfaces` |
| 9 | Visual and responsive polish | Event and cross-slide regression | Stabilize the complete demonstration flow | `style(frontend): polish desktop workbench visuals` |
| 10 | Documentation and demo fixes | Documentation and demo fixes | Complete delivery documentation | `docs(frontend): document setup and delivery workflow` |

## Day 1

Commit message:

```text
docs(frontend): freeze workbench UX and visual system
```

Files:

- `docs/frontend-commit-plan.md`
- `PPTAgent二次开发计划_两周_8人.md`
- `PPTAgent frontend design/.thumbnail`
- `PPTAgent frontend design/Create.dc.html`
- `PPTAgent frontend design/Templates.dc.html`
- `PPTAgent frontend design/Workspace.dc.html`
- `PPTAgent frontend design/pptagent-data.js`
- `PPTAgent frontend design/support.js`
- `frontend/FRONTEND_SPEC.md`
- `frontend/src/index.css`

## Day 2

Commit message:

```text
feat(frontend): scaffold app and mock task state
```

Files:

- `.gitignore`
- `frontend/.env.example`
- `frontend/.gitignore`
- `frontend/.oxlintrc.json`
- `frontend/components.json`
- `frontend/index.html`
- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/tsconfig.json`
- `frontend/tsconfig.app.json`
- `frontend/tsconfig.node.json`
- `frontend/vite.config.ts`
- `frontend/src/main.tsx`
- `frontend/src/components/app-header.tsx`
- `frontend/src/components/app-shell.tsx`
- `frontend/src/components/brand-mark.tsx`
- `frontend/src/lib/api.ts`
- `frontend/src/lib/sse.ts`
- `frontend/src/mocks/deck.ts`
- `frontend/src/mocks/mock-server.ts`
- `frontend/src/mocks/slide-preview.ts`
- `frontend/src/mocks/slides.ts`
- `frontend/src/mocks/templates.ts`
- `frontend/src/stores/create-task-store.ts`
- `frontend/src/stores/templates-store.ts`
- `frontend/src/stores/workbench-store.ts`
- `frontend/src/types/api.ts`

## Day 3

Commit message:

```text
feat(frontend): add task creation and template library pages
```

Files:

- `frontend/src/App.tsx`
- `frontend/src/pages/create-page.tsx`
- `frontend/src/pages/templates-page.tsx`
- `frontend/src/components/ui/badge.tsx`
- `frontend/src/components/ui/button.tsx`
- `frontend/src/components/ui/card.tsx`
- `frontend/src/components/ui/input.tsx`
- `frontend/src/components/ui/label.tsx`
- `frontend/src/components/ui/select.tsx`
- `frontend/src/components/ui/separator.tsx`
- `frontend/src/components/ui/slider.tsx`

## Day 4

Commit message:

```text
feat(frontend): build the presentation workbench
```

Files:

- `frontend/src/pages/workbench-page.tsx`
- `frontend/src/components/template-cover.tsx`
- `frontend/src/components/ui/progress.tsx`
- `frontend/src/components/ui/scroll-area.tsx`

## Day 5

Commit message:

```text
feat(frontend): connect task and template event APIs
```

Files:

- `docs/api-contract.md`

Notes:

- The API types, REST adapter, and SSE adapter are already assigned to Day 2
  because each file contains both mock and real transport paths and cannot be
  split without modifying or partially staging the file.

## Day 6

Commit message:

```text
feat(frontend): add slide revision and chat controls
```

Files:

- `frontend/src/components/ui/dropdown-menu.tsx`
- `frontend/src/components/ui/textarea.tsx`

## Day 7

Commit message:

```text
feat(frontend): restore sessions and versioned previews
```

Files:

- `frontend/src/lib/utils.ts`

Notes:

- The Zustand hydration and reconnect state are in `workbench-store.ts` and
  `sse.ts`, which are assigned to Day 2 as foundational state infrastructure.

## Day 8

Commit message:

```text
feat(frontend): add task recovery and run detail surfaces
```

Files:

- `frontend/src/components/ui/alert-dialog.tsx`
- `frontend/src/components/ui/dialog.tsx`
- `frontend/src/components/ui/sheet.tsx`
- `frontend/src/components/ui/sonner.tsx`

## Day 9

Commit message:

```text
style(frontend): polish desktop workbench visuals
```

Files:

- `frontend/public/favicon.svg`
- `frontend/public/icons.svg`
- `frontend/src/components/ui/avatar.tsx`
- `frontend/src/components/ui/collapsible.tsx`
- `frontend/src/components/ui/skeleton.tsx`
- `frontend/src/components/ui/tooltip.tsx`

Validation to run after this commit:

```bash
cd frontend
npm run build
npm run lint
```

## Day 10

Commit message:

```text
docs(frontend): document setup and delivery workflow
```

Files:

- `frontend/README.md`

## Intentionally excluded from all commits

These files are local, generated, or currently unused and should not be staged:

- `frontend/.env` — local mock/backend selection.
- `frontend/.DS_Store`
- `frontend/src/.DS_Store`
- `frontend/.claude/launch.json`
- `frontend/dist/`
- `frontend/node_modules/`
- `frontend/src/assets/hero.png` — currently unused.
- `frontend/src/assets/react.svg` — unused Vite starter asset.
- `frontend/src/assets/vite.svg` — unused Vite starter asset.

## Known review findings

The commits above preserve the current implementation as-is. They do not fix
the previously identified dropdown-menu crash, empty-response parsing,
nested-button markup, missing template dropzone, or contract inconsistencies.
