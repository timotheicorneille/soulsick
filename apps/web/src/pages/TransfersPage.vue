<script setup lang="ts">
import type { Transfer } from '@/lib/types'
import type { TransferAction } from '@/composables/transferActions'
import { ArrowDownIcon, ArrowUpIcon, BrushCleaningIcon, PauseIcon, PlayIcon, RotateCcwIcon, XIcon } from '@lucide/vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { toast } from 'vue-sonner'
import PageHeader from '@/components/PageHeader.vue'
import SwitchPeerMenu from '@/components/SwitchPeerMenu.vue'
import TransferRow from '@/components/TransferRow.vue'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useTransfers } from '@/composables/queries'
import { useTransferActions } from '@/composables/transferActions'
import { api } from '@/lib/api'
import { speed } from '@/lib/format'
import { useLive } from '@/stores/live'

const { t } = useI18n()
const live = useLive()
const qc = useQueryClient()
const route = useRoute()
const direction = ref<'download' | 'upload'>('download')
type Filter = 'active' | 'stuck' | 'done' | 'all'
const filter = ref<Filter>(['active', 'stuck', 'done', 'all'].includes(route.query.filter as string) ? route.query.filter as Filter : 'active')
const { data, isPending } = useTransfers(direction)

const RUNNING = ['queued', 'initializing', 'transferring']
const ACTIVE = [...RUNNING, 'paused']
const stateOf = (tr: { id: number, state: string }) => live.transfers.get(tr.id)?.state ?? tr.state
const isStuck = (tr: Transfer) => !!(live.transfers.get(tr.id)?.stuck ?? tr.stuck) && RUNNING.includes(stateOf(tr))
const stuckCount = computed(() => (data.value ?? []).filter(isStuck).length)

const rows = computed(() => (data.value ?? []).filter((tr) => {
  const state = stateOf(tr)
  if (filter.value === 'active')
    return ACTIVE.includes(state)
  if (filter.value === 'stuck')
    return isStuck(tr)
  if (filter.value === 'done')
    return !ACTIVE.includes(state)
  return true
}))

/** Transfers from the same remote folder are grouped: you read an album, not 12 loose lines. */
const groups = computed(() => {
  const map = new Map<string, Transfer[]>()
  for (const tr of rows.value) {
    const dir = tr.remote_path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
    const key = `${tr.username}|${dir}`
    if (!map.has(key))
      map.set(key, [])
    map.get(key)!.push(tr)
  }
  return [...map.entries()]
    .map(([key, items]) => ({
      key,
      username: key.split('|')[0],
      dir: key.split('|')[1],
      items,
      // An album source still downloading can be switched to another peer
      candidateId: items.some(x => ACTIVE.includes(stateOf(x))) ? items.find(x => x.candidate_id)?.candidate_id ?? null : null,
      stuck: items.some(isStuck),
    }))
    // What needs you comes first
    .sort((a, b) => Number(b.stuck) - Number(a.stuck))
})

// --- Selection ----------------------------------------------------------------------
const selected = ref(new Set<number>())
watch([direction, filter], () => (selected.value = new Set()))
// Rows that disappear (cleared, or filtered out once finished) leave the selection
watch(rows, (r) => {
  const visible = new Set(r.map(x => x.id))
  if ([...selected.value].some(id => !visible.has(id)))
    selected.value = new Set([...selected.value].filter(id => visible.has(id)))
})
function select(ids: number[], on: boolean) {
  const next = new Set(selected.value)
  ids.forEach(id => (on ? next.add(id) : next.delete(id)))
  selected.value = next
}
function groupState(items: Transfer[]): boolean | 'indeterminate' {
  const n = items.filter(x => selected.value.has(x.id)).length
  return n === 0 ? false : n === items.length ? true : 'indeterminate'
}
const allState = computed(() => groupState(rows.value))
const selection = computed(() => rows.value.filter(x => selected.value.has(x.id)))

/** What can be done to a set of transfers, given their states */
function possible(items: Transfer[]) {
  const download = direction.value === 'download'
  return {
    pause: download ? items.filter(x => RUNNING.includes(stateOf(x))) : [],
    resume: items.filter(x => stateOf(x) === 'paused'),
    retry: download ? items.filter(x => ['failed', 'aborted'].includes(stateOf(x))) : [],
    cancel: items.filter(x => ACTIVE.includes(stateOf(x))),
  }
}
const sel = computed(() => possible(selection.value))
const all = computed(() => possible(rows.value))

// --- Actions ------------------------------------------------------------------------
const { busy, run, batch } = useTransferActions()
const working = ref(false)
async function act(items: Transfer[], kind: TransferAction) {
  working.value = true
  try {
    await batch(items.map(x => x.id), kind)
    select(items.map(x => x.id), false)
  }
  finally {
    working.value = false
  }
}

/**
 * Cancelling always asks first. The dialog closes BEFORE the confirm button's click runs,
 * so what to cancel is kept apart from the open state (closing must not erase it).
 */
const confirmOpen = ref(false)
const toCancel = ref<Transfer[]>([])
function askCancel(items: Transfer[]) {
  if (!items.length)
    return
  toCancel.value = items
  confirmOpen.value = true
}
async function doCancel() {
  const items = toCancel.value
  confirmOpen.value = false
  toCancel.value = []
  if (items.length)
    await act(items, 'cancel')
}

const clear = useMutation({
  mutationFn: () => api.post<{ removed: number }>(`/transfers/clear?direction=${direction.value}`),
  onSuccess: (d) => {
    toast.success(t('transfers.cleared', d.removed), { description: t('transfers.clearedHint') })
    qc.invalidateQueries({ queryKey: ['transfers'] })
  },
})
</script>

<template>
  <div class="flex flex-col">
    <PageHeader :title="t('transfers.title')" :description="t('transfers.description')">
      <template #actions>
        <Button v-if="all.resume.length" variant="outline" size="sm" :disabled="working" @click="act(all.resume, 'resume')">
          <PlayIcon data-icon="inline-start" />
          {{ t('transfers.resumeAll') }}
        </Button>
        <Button v-if="all.pause.length" variant="outline" size="sm" :disabled="working" @click="act(all.pause, 'pause')">
          <PauseIcon data-icon="inline-start" />
          {{ t('transfers.pauseAll') }}
        </Button>
        <Button
          v-if="all.cancel.length"
          variant="ghost"
          size="sm"
          class="text-muted-foreground hover:text-destructive"
          :disabled="working"
          @click="askCancel(all.cancel)"
        >
          <XIcon data-icon="inline-start" />
          {{ t('transfers.cancelAll') }}
        </Button>
        <Button variant="ghost" size="sm" class="text-muted-foreground" @click="clear.mutate()">
          <BrushCleaningIcon data-icon="inline-start" />
          {{ t('transfers.clear') }}
        </Button>
      </template>
    </PageHeader>

    <div class="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <Tabs v-model="direction">
        <TabsList>
          <TabsTrigger value="download">
            <ArrowDownIcon />
            {{ t('transfers.downloads') }}
            <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ speed(live.engine?.download_speed) }}</span>
          </TabsTrigger>
          <TabsTrigger value="upload">
            <ArrowUpIcon />
            {{ t('transfers.uploads') }}
            <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ speed(live.engine?.upload_speed) }}</span>
          </TabsTrigger>
        </TabsList>
      </Tabs>
      <ToggleGroup v-model="filter" type="single" variant="outline" size="sm">
        <ToggleGroupItem value="active">
          {{ t('transfers.filters.active') }}
        </ToggleGroupItem>
        <ToggleGroupItem v-if="direction === 'download' && (stuckCount || filter === 'stuck')" value="stuck" class="data-[state=on]:text-warning">
          {{ t('transfers.filters.stuck') }}
          <span class="ml-1 font-mono text-[11px] tnum">{{ stuckCount }}</span>
        </ToggleGroupItem>
        <ToggleGroupItem value="done">
          {{ t('transfers.filters.done') }}
        </ToggleGroupItem>
        <ToggleGroupItem value="all">
          {{ t('common.all') }}
        </ToggleGroupItem>
      </ToggleGroup>
    </div>

    <!-- Selection bar: stays visible while scrolling long lists -->
    <div
      v-if="rows.length"
      class="sticky top-16 z-10 mb-4 flex min-h-11 flex-wrap items-center gap-2 rounded-xl border bg-background/90 px-4 py-1.5 backdrop-blur lg:top-3"
    >
      <label class="flex items-center gap-2.5 text-[12.5px] text-muted-foreground">
        <Checkbox :model-value="allState" @update:model-value="(v) => select(rows.map(x => x.id), v === true)" />
        {{ selection.length ? t('transfers.selected', selection.length) : t('transfers.selectAll') }}
      </label>
      <div v-if="selection.length" class="ml-auto flex flex-wrap items-center gap-1">
        <Button v-if="sel.resume.length" variant="ghost" size="sm" :disabled="working" @click="act(sel.resume, 'resume')">
          <PlayIcon data-icon="inline-start" />
          {{ t('common.resume') }} <span class="font-mono text-[11px] text-faint tnum">{{ sel.resume.length }}</span>
        </Button>
        <Button v-if="sel.pause.length" variant="ghost" size="sm" :disabled="working" @click="act(sel.pause, 'pause')">
          <PauseIcon data-icon="inline-start" />
          {{ t('common.pause') }} <span class="font-mono text-[11px] text-faint tnum">{{ sel.pause.length }}</span>
        </Button>
        <Button v-if="sel.retry.length" variant="ghost" size="sm" :disabled="working" @click="act(sel.retry, 'retry')">
          <RotateCcwIcon data-icon="inline-start" />
          {{ t('common.retry') }} <span class="font-mono text-[11px] text-faint tnum">{{ sel.retry.length }}</span>
        </Button>
        <Button
          v-if="sel.cancel.length"
          variant="ghost"
          size="sm"
          class="text-destructive hover:text-destructive"
          :disabled="working"
          @click="askCancel(sel.cancel)"
        >
          <XIcon data-icon="inline-start" />
          {{ t('common.cancel') }} <span class="font-mono text-[11px] tnum">{{ sel.cancel.length }}</span>
        </Button>
        <Button variant="ghost" size="sm" class="text-muted-foreground" @click="selected = new Set()">
          {{ t('transfers.deselect') }}
        </Button>
      </div>
    </div>

    <div v-if="isPending" class="flex flex-col gap-2">
      <Skeleton v-for="i in 4" :key="i" class="h-20 w-full" />
    </div>
    <div v-else-if="groups.length" class="flex flex-col gap-4">
      <section v-for="g in groups" :key="g.key" class="rounded-xl bg-card">
        <header class="flex items-center gap-2.5 border-b px-4 py-2.5">
          <Checkbox
            :model-value="groupState(g.items)"
            :aria-label="t('transfers.selectFolder')"
            @update:model-value="(v) => select(g.items.map(x => x.id), v === true)"
          />
          <RouterLink :to="{ name: 'browse', params: { username: g.username } }" class="font-mono text-[12px] font-medium hover:text-primary">
            {{ g.username }}
          </RouterLink>
          <span class="min-w-0 truncate font-mono text-[11.5px] text-faint" :title="g.dir">{{ g.dir }}</span>
          <span class="ml-auto font-mono text-[11px] text-faint tnum">{{ g.items.length }}</span>
          <SwitchPeerMenu v-if="g.candidateId" :candidate-id="g.candidateId" :stuck="g.stuck" />
          <Button
            v-if="possible(g.items).pause.length"
            variant="ghost"
            size="xs"
            class="text-muted-foreground"
            :disabled="working"
            @click="act(possible(g.items).pause, 'pause')"
          >
            <PauseIcon data-icon="inline-start" />
            {{ t('transfers.pauseAll') }}
          </Button>
          <Button
            v-if="possible(g.items).resume.length"
            variant="ghost"
            size="xs"
            class="text-primary"
            :disabled="working"
            @click="act(possible(g.items).resume, 'resume')"
          >
            <PlayIcon data-icon="inline-start" />
            {{ t('transfers.resumeAll') }}
          </Button>
          <Button
            v-if="possible(g.items).cancel.length"
            variant="ghost"
            size="xs"
            class="text-muted-foreground hover:text-destructive"
            :disabled="working"
            @click="askCancel(possible(g.items).cancel)"
          >
            <XIcon data-icon="inline-start" />
            {{ t('transfers.cancelAll') }}
          </Button>
        </header>
        <div class="flex flex-col divide-y px-4">
          <TransferRow
            v-for="tr in g.items"
            :key="tr.id"
            :transfer="tr"
            :busy="busy.has(tr.id)"
            selectable
            :selected="selected.has(tr.id)"
            @select="(id, on) => select([id], on)"
            @cancel="(id) => askCancel(g.items.filter(x => x.id === id))"
            @retry="(id) => run([id], 'retry')"
            @pause="(id) => run([id], 'pause')"
            @resume="(id) => run([id], 'resume')"
          />
        </div>
      </section>
    </div>
    <Empty v-else class="rounded-xl bg-card/50 py-14">
      <EmptyHeader>
        <EmptyTitle class="text-[14px]">
          {{ t(`transfers.empty.${direction}`) }}
        </EmptyTitle>
        <EmptyDescription class="text-[12.5px]">
          {{ t(`transfers.emptyHint.${direction}`) }}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>

    <AlertDialog v-model:open="confirmOpen">
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{{ t('transfers.confirmTitle', toCancel.length) }}</AlertDialogTitle>
          <AlertDialogDescription>
            {{ direction === 'download' ? t('transfers.confirmDownload') : t('transfers.confirmUpload') }}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{{ t('transfers.keep') }}</AlertDialogCancel>
          <AlertDialogAction class="bg-destructive text-white hover:bg-destructive/90" @click="doCancel">
            {{ t('transfers.confirmCancel') }}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  </div>
</template>
