import type { CapacitorConfig } from '@capacitor/cli'

const config: CapacitorConfig = {
  appId: 'app.terminalmcp.console',
  appName: 'Terminal MCP',
  webDir: 'dist',
  server: {
    androidScheme: 'https',
  },
}

export default config
