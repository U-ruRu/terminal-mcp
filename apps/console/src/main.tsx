import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'

import { FleetRuntime } from './fleet/FleetRuntime'
import { I18nProvider } from './i18n/I18nProvider'
import { applyDocumentLocale, initialLocale } from './i18n/runtime'
import { ThemeProvider } from './theme/ThemeProvider'
import './styles/tokens.css'
import './styles/app.css'

applyDocumentLocale(initialLocale())

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <I18nProvider>
      <ThemeProvider>
        <BrowserRouter>
          <FleetRuntime />
        </BrowserRouter>
      </ThemeProvider>
    </I18nProvider>
  </StrictMode>,
)
