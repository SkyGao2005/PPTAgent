import { lazy, Suspense } from "react"
import { Navigate, Route, Routes, useParams } from "react-router-dom"
import { LoaderCircleIcon } from "lucide-react"

import { AppErrorBoundary } from "@/components/app-error-boundary"
import { SceneBackground } from "@/components/scene-background"

const CreatePage = lazy(() =>
  import("@/pages/create-page").then((module) => ({ default: module.CreatePage })),
)
const TemplatesPage = lazy(() =>
  import("@/pages/templates-page").then((module) => ({
    default: module.TemplatesPage,
  })),
)
const OutlineReviewPage = lazy(() =>
  import("@/pages/outline-review-page").then((module) => ({
    default: module.OutlineReviewPage,
  })),
)
const WorkbenchPage = lazy(() =>
  import("@/pages/workbench-page").then((module) => ({
    default: module.WorkbenchPage,
  })),
)

function RouteFallback() {
  return (
    <div className="relative flex min-h-svh items-center justify-center">
      <SceneBackground />
      <span className="glass-card relative z-10 flex items-center gap-2.5 rounded-full px-5 py-3 text-[13px] font-medium text-muted-foreground">
        <LoaderCircleIcon className="size-4 animate-spin" />
        正在加载工作台…
      </span>
    </div>
  )
}

function LegacyTaskRedirect() {
  const { taskId } = useParams()
  return <Navigate to={`/workbench/${taskId}`} replace />
}

function App() {
  return (
    <AppErrorBoundary>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<CreatePage />} />
          <Route path="/templates" element={<TemplatesPage />} />
          <Route path="/outline/:outlineId" element={<OutlineReviewPage />} />
          <Route path="/workbench/:taskId" element={<WorkbenchPage />} />
          <Route path="/create" element={<Navigate to="/" replace />} />
          <Route path="/tasks/:taskId" element={<LegacyTaskRedirect />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Suspense>
    </AppErrorBoundary>
  )
}

export default App
