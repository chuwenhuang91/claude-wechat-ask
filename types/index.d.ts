export type Pending = {
  question: string
  options: string[]
  held?: boolean
} | null

declare module 'claude-code' {
  interface PluginState {
    'wechat-ask': { pending: Pending; answer: string | null }
  }
}
