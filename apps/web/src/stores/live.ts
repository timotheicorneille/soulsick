import type { QueryClient } from '@tanstack/vue-query'
import type { ChatMessage, EngineStatus, HunterState, JournalEvent, LogLine, LyricsJob, Transfer } from '@/lib/types'
import { defineStore } from 'pinia'
import { reactive, ref, shallowRef } from 'vue'
import { toast } from 'vue-sonner'
import { t } from '@/i18n'
import { router } from '@/router'

type Msg
  = | { type: 'status', data: EngineStatus }
    | { type: 'hunter', data: HunterState }
    | { type: 'event', data: JournalEvent }
    | { type: 'transfer', data: Partial<Transfer> & { id: number } }
    | { type: 'album', data: { id: number, status: string, status_reason?: string | null } }
    | { type: 'logs', data: LogLine[] }
    | { type: 'existing', data: { running: boolean, done: number, total: number } }
    | { type: 'chat', data: ChatMessage }
    | { type: 'chat_read', data: { kind: string, target: string } }
    | { type: 'chat_rooms', data: { rooms: string[] } }
    | { type: 'library_guard', data: unknown }
    | { type: 'lyrics', data: LyricsJob }

/** Real-time stream: network status, current hunt, journal, transfer progress. */
export const useLive = defineStore('live', () => {
  const engine = ref<EngineStatus | null>(null)
  const hunter = ref<HunterState | null>(null)
  const events = shallowRef<JournalEvent[]>([])
  /** Engine logs (Activity → Live logs), last 3000 lines */
  const logs = shallowRef<LogLine[]>([])
  /** Warnings and errors since the Activity page was last opened */
  const unseenProblems = ref(0)
  const transfers = reactive(new Map<number, Partial<Transfer>>())
  const connected = ref(false)
  /** Chat messages received live (the Chat page merges them into the open conversation) */
  const chat = shallowRef<ChatMessage[]>([])
  /** Conversation currently on screen: no notification for it */
  const chatOpen = ref<{ kind: string, target: string } | null>(null)
  /** The pass looking for missing lyrics (Library), as it goes */
  const lyricsJob = ref<LyricsJob | null>(null)
  /** Short speed history for the sidebar meter */
  const speedHistory = ref<{ down: number, up: number }[]>([])

  let socket: WebSocket | null = null
  let retry = 0
  let client: QueryClient | null = null
  let invalidateTimer: ReturnType<typeof setTimeout> | null = null
  const pendingKeys = new Set<string>()

  function invalidate(...keys: string[]) {
    keys.forEach(k => pendingKeys.add(k))
    if (invalidateTimer)
      return
    // Batch invalidations: an import emits dozens of events in a row
    invalidateTimer = setTimeout(() => {
      invalidateTimer = null
      for (const k of pendingKeys)
        client?.invalidateQueries({ queryKey: [k] })
      pendingKeys.clear()
    }, 400)
  }

  function handle(msg: Msg) {
    switch (msg.type) {
      case 'status':
        engine.value = msg.data
        speedHistory.value = [...speedHistory.value.slice(-59), { down: msg.data.download_speed, up: msg.data.upload_speed }]
        break
      case 'hunter':
        hunter.value = msg.data
        break
      case 'event':
        events.value = [msg.data, ...events.value].slice(0, 300)
        if (msg.data.level !== 'info')
          unseenProblems.value++
        invalidate('events', 'status')
        if (msg.data.release_group_id)
          invalidate('album', 'albums', 'artist', 'plan')
        if (msg.data.kind.startsWith('artist') || msg.data.kind.startsWith('musicbrainz'))
          invalidate('artists', 'artist', 'discover', 'similar')
        if (msg.data.kind.startsWith('share'))
          invalidate('shares')
        break
      case 'transfer': {
        const prev = transfers.get(msg.data.id) ?? {}
        transfers.set(msg.data.id, { ...prev, ...msg.data })
        if (msg.data.state && msg.data.state !== prev.state)
          invalidate('transfers', 'album')
        break
      }
      case 'album':
        invalidate('album', 'albums', 'artist', 'artists', 'status')
        break
      case 'chat': {
        const m = msg.data
        chat.value = [...chat.value, m].slice(-500)
        invalidate('chat-overview', 'status')
        const open = chatOpen.value?.kind === m.kind && chatOpen.value.target === m.target
        if (m.kind === 'private' && !m.outgoing && !open) {
          toast(t('chat.newMessage', { user: m.username }), {
            description: m.text.length > 140 ? `${m.text.slice(0, 140)}…` : m.text,
            action: { label: t('chat.reply'), onClick: () => router.push({ name: 'chat', query: { with: m.target } }) },
          })
        }
        break
      }
      case 'chat_read':
      case 'chat_rooms':
        invalidate('chat-overview', 'status')
        break
      case 'existing':
        invalidate('existing')
        break
      case 'lyrics':
        lyricsJob.value = msg.data
        invalidate('album', 'albums')
        break
      case 'library_guard':
        invalidate('status', 'storage')
        break
      case 'logs': {
        const last = logs.value.at(-1)?.id ?? 0
        const fresh = msg.data.filter(l => l.id > last)
        if (fresh.length)
          logs.value = [...logs.value, ...fresh].slice(-3000)
        break
      }
    }
  }

  function connect(qc: QueryClient) {
    client = qc
    if (socket)
      return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    socket = new WebSocket(`${proto}://${location.host}/api/ws`)
    socket.onopen = () => {
      connected.value = true
      retry = 0
    }
    socket.onmessage = e => handle(JSON.parse(e.data))
    socket.onclose = (e) => {
      connected.value = false
      socket = null
      if (e.code === 4401)
        return
      retry = Math.min(retry + 1, 6)
      setTimeout(() => client && connect(client), 500 * 2 ** retry)
    }
  }

  function disconnect() {
    socket?.close(4401)
    socket = null
  }

  function seedLogs(list: LogLine[]) {
    const known = new Set(logs.value.map(l => l.id))
    logs.value = [...list.filter(l => !known.has(l.id)), ...logs.value].sort((a, b) => a.id - b.id).slice(-3000)
  }

  function seedEvents(list: JournalEvent[]) {
    if (!events.value.length)
      events.value = list
  }

  return { engine, hunter, events, logs, unseenProblems, chat, chatOpen, transfers, connected, speedHistory, lyricsJob, connect, disconnect, seedEvents, seedLogs }
})
