import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'

import './index.css'
import '@/lib/liquid-glass'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <TooltipProvider>
        <App />
        <Toaster
          position="top-center"
          offset={{ top: 88 }}
          mobileOffset={{ top: 88, right: 12, left: 12 }}
          duration={4500}
          gap={8}
          visibleToasts={2}
          theme="light"
        />
      </TooltipProvider>
    </BrowserRouter>
  </StrictMode>,
)
