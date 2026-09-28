export type InstanceFixture = {
  name: string
  origin: string
  version: string
  health: 'healthy' | 'degraded' | 'offline'
  activeAgents: number
  readyTasks: number
  updatedAt: string
}

export const fixtureInstance: InstanceFixture = {
  name: 'Server C',
  origin: 'https://terminal.example',
  version: '0.10.1',
  health: 'healthy',
  activeAgents: 3,
  readyTasks: 7,
  updatedAt: 'just now',
}
