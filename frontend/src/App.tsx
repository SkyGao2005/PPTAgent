import { lazy, Suspense } from "react"
import { Navigate, Route, Routes, useParams } from "react-router-dom"

import { AppErrorBoundary } from "@/components/app-error-boundary"

const CreatePage = lazy(() =>
  import("@/pages/create-page").then((module) => ({ default: module.CreatePage })),
)
const TemplatesPage = lazy(() =>
  import("@/pages/templates-page").then((module) => ({
    default: module.TemplatesPage,
  })),
)
const WorkbenchPage = lazy(() =>
  import("@/pages/workbench-page").then((module) => ({
    default: module.WorkbenchPage,
  })),
)

function RouteFallback() {
  return (
    <div className="flex min-h-svh items-center justify-center bg-background text-sm text-muted-foreground">
      正在加载工作台……
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
