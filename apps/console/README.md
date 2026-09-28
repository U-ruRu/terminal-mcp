# Terminal MCP Console

Static React/Vite client for direct connections to Terminal MCP instances.

## Local workflow

```sh
npm install
npm run dev
npm run lint
npm test
npm run build
```

The M1 scaffold intentionally runs from fixture data. Transport/authentication, typed M0 read models, realtime convergence, and production static deployment are separate M1 tasks. Domain/state code lives under `src/` so the same frontend can later be wrapped by Capacitor without replacing the application model.
