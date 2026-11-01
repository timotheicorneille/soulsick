<script setup lang="ts">
import type { Transfer } from '@/lib/types'
import { PauseIcon, PlayIcon, RotateCcwIcon, XIcon } from '@lucide/vue'
import { useIntervalFn } from '@vueuse/core'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ago, bytes, splitPath, speed } from '@/lib/format'
import { transferState } from '@/lib/status'
import { cn } from '@/lib/utils'
import { useLive } from '@/stores/live'

const props = withDefaults(defineProps<{ transfer: Transfer, actions?: boolean, busy?: boolean, selectable?: boolean, selected?: boolean }>(), { actions: true, busy: false, selectable: false, selected: false })
const emit = defineEmits<{ cancel: [id: number], retry: [id: number], pause: [id: number], resume: [id: number], select: [id: number, on: boolean] }>()
const { t } = useI18n()
const live = useLive()

/** Merge the stored row with the live progress received over the WebSocket. */
const tr = computed(() => ({ ...props.transfer, ...(live.transfers.get(props.transfer.id) ?? {}) }) as Transfer)
const pct = computed(() => (tr.value.size ? Math.min(100, (tr.value.bytes_transferred / tr.value.size) * 100) : 0))
const name = computed(() => splitPath(tr.value.remote_path).name)
const running = computed(() => ['queued', 'initializing', 'transferring'].includes(tr.value.state))
/** Since when each download has been "connecting" (shared across rows and re-renders) */
const connectingSince = (globalThis as { __slskConnecting?: Map<number, number> }).__slskConnecting ??= new Map()
const now = ref(Date.now())
const ticker = useIntervalFn(() => (now.value = Date.now()), 1000, { immediate: false })
watch(() => tr.value.state, (state) => {
  if (state === 'initializing') {
    if (!connectingSince.has(tr.value.id))
      connectingSince.set(tr.value.id, Date.now())
    now.value = Date.now()
    ticker.resume()
  }
  else {
    connectingSince.delete(tr.value.id)
    ticker.pause()
  }
}, { immediate: true })
const connectingFor = computed(() => {
  const since = connectingSince.get(tr.value.id)
  return since && tr.value.state === 'initializing' ? Math.max(0, Math.round((now.value - since) / 1000)) : null
})

/** Album download that stopped moving: shown first, it's what needs you */
const stuck = computed(() => !!tr.value.stuck && running.value && tr.value.direction === 'download')
const stuckHint = computed(() => stuck.value
  ? t(`transfer.stuckWhy.${tr.value.stuck_reason}`, { since: ago(tr.value.stuck_since), n: tr.value.attempts, place: tr.value.place_in_queue ?? '?' })
  : '')

const st = computed(() => {
  if (stuck.value)
    return { label: t('transfer.stuck'), badge: 'warning' as const }
  // "Queued" alone means nothing: did the peer accept, and where are we in their queue?
  if (tr.value.direction === 'download' && tr.value.state === 'queued') {
    return tr.value.remotely_queued
      ? { label: tr.value.place_in_queue ? t('transfer.inTheirQueueAt', { n: tr.value.place_in_queue }) : t('transfer.inTheirQueue'), badge: 'signal' as const }
      : { label: t('transfer.requested'), badge: 'quiet' as const }
  }
  if (tr.value.direction === 'download' && connectingFor.value !== null && connectingFor.value >= 3)
    return { label: t('transfer.connectingFor', { s: connectingFor.value }), badge: connectingFor.value >= 30 ? 'warning' as const : 'signal' as const }
  return transferState(tr.value.state)
})
</script>

<template>
  <div :class="cn('group grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1.5 py-2.5', selected && '-mx-4 bg-primary/[0.06] px-4')">
    <div class="flex min-w-0 items-center gap-2.5">
      <Checkbox
        v-if="selectable"
        :model-value="selected"
        :aria-label="t('folder.select', { name })"
        @update:model-value="(v) => emit('select', tr.id, v === true)"
      />
      <span class="truncate text-[13px] text-foreground" :title="tr.remote_path">{{ name }}</span>
    </div>
    <div class="flex items-center gap-2">
      <span class="font-mono text-[11px] text-muted-foreground tnum">
        <template v-if="tr.state === 'transferring'">{{ speed(tr.speed) }} · </template>
        {{ bytes(tr.bytes_transferred) }}<span class="text-faint"> / {{ bytes(tr.size) }}</span>
      </span>
      <Tooltip>
        <TooltipTrigger as-child>
          <span class="inline-flex"><Badge :variant="st.badge">{{ st.label }}</Badge></span>
        </TooltipTrigger>
        <TooltipContent class="max-w-64">
          <template v-if="stuck">
            {{ stuckHint }} {{ t('transfer.stuckAction') }}
          </template>
          <template v-else-if="tr.state === 'queued' && tr.direction === 'download'">
            {{ tr.remotely_queued
              ? t('transfer.acceptedHint', { peer: tr.username })
              : t('transfer.requestedHint', { peer: tr.username }) }}
          </template>
          <template v-else-if="tr.state === 'initializing' && tr.direction === 'download'">
            {{ t('transfer.connectingHint', { peer: tr.username }) }}
          </template>
          <template v-else>
            {{ st.label }}
          </template>
        </TooltipContent>
      </Tooltip>
      <span v-if="(tr.attempts ?? 1) > 1" class="font-mono text-[10.5px] text-faint tnum" :title="t('transfer.attempts')">{{ t('transfer.attempt', { n: tr.attempts }) }}</span>
      <template v-if="actions">
        <Tooltip v-if="running && tr.direction === 'download'">
          <TooltipTrigger as-child>
            <Button variant="ghost" size="icon-xs" class="text-faint hover:text-foreground" :aria-label="t('common.pause')" :disabled="busy" @click="emit('pause', tr.id)">
              <PauseIcon />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{{ t('transfer.pauseHint') }}</TooltipContent>
        </Tooltip>
        <Button
          v-if="tr.state === 'paused'"
          variant="ghost"
          size="xs"
          class="text-primary"
          :disabled="busy"
          @click="emit('resume', tr.id)"
        >
          <PlayIcon data-icon="inline-start" />
          {{ t('common.resume') }}
        </Button>
        <Tooltip v-if="running || tr.state === 'paused'">
          <TooltipTrigger as-child>
            <Button variant="ghost" size="icon-xs" class="text-faint hover:text-destructive" :aria-label="t('common.cancel')" :disabled="busy" @click="emit('cancel', tr.id)">
              <XIcon />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{{ t('transfer.cancelHint') }}</TooltipContent>
        </Tooltip>
        <Tooltip v-else-if="tr.direction === 'download' && tr.state !== 'complete'">
          <TooltipTrigger as-child>
            <Button variant="ghost" size="xs" class="text-muted-foreground hover:text-primary" :disabled="busy" @click="emit('retry', tr.id)">
              <RotateCcwIcon data-icon="inline-start" />
              {{ t('common.retry') }}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{{ t('transfer.retryHint') }}</TooltipContent>
        </Tooltip>
        <span v-else class="size-6" />
      </template>
    </div>
    <div class="col-span-2 flex min-w-0 items-center gap-3">
      <div class="h-[3px] flex-1 overflow-hidden rounded-full bg-border">
        <div
          :class="cn('h-full rounded-full transition-[width] duration-500',
                     tr.state === 'complete' ? 'bg-success' : tr.state === 'failed' ? 'bg-destructive' : 'bg-primary')"
          :style="{ width: `${tr.state === 'complete' ? 100 : pct}%` }"
        />
      </div>
    </div>
    <div class="col-span-2 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-0.5 text-[11.5px]">
      <span class="text-faint">{{ tr.direction === 'download' ? t('transfer.from') : t('transfer.to') }}</span>
      <RouterLink :to="{ name: 'peer', params: { username: tr.username } }" class="font-mono text-muted-foreground hover:text-primary">
        {{ tr.username }}
      </RouterLink>
      <template v-if="tr.local_path">
        <span class="text-faint">{{ tr.direction === 'download' ? '→' : t('transfer.file') }}</span>
        <span class="min-w-0 truncate font-mono text-faint" :title="tr.local_path">{{ tr.local_path }}</span>
      </template>
      <span v-if="tr.error" class="text-destructive">{{ tr.error }}</span>
    </div>
  </div>
</template>
