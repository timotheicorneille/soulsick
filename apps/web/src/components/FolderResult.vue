<script setup lang="ts">
import type { RemoteFile } from '@/lib/types'
import { CheckIcon, ChevronDownIcon, DownloadIcon, FolderIcon } from '@lucide/vue'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { bytes, duration, speed, splitPath } from '@/lib/format'
import { cn } from '@/lib/utils'
import FormatTag from './FormatTag.vue'

/** A folder on a peer (search result or browse), downloadable in full or in part. */
const props = defineProps<{
  username: string
  directory: string
  files: RemoteFile[]
  formats?: string[]
  hasFreeSlots?: boolean
  avgSpeed?: number
  queueSize?: number
  defaultOpen?: boolean
  showPeer?: boolean
  /** Files of this folder already asked for: shown as such, never asked for again */
  requested?: ReadonlySet<string>
}>()
const emit = defineEmits<{ download: [files: string[]] }>()

const { t } = useI18n()
const open = ref(props.defaultOpen ?? false)
const selected = ref<Set<string>>(new Set())
const audio = computed(() => props.files.filter(f => !f.extra && f.format))
const size = computed(() => props.files.reduce((s, f) => s + (f.size ?? 0), 0))
const fmts = computed(() => props.formats ?? [...new Set(audio.value.map(f => f.format).filter(Boolean) as string[])])
const isRequested = (name: string) => props.requested?.has(name) ?? false
/** What the folder button still asks for */
const remaining = computed(() => props.files.map(f => f.filename).filter(name => !isRequested(name)))

function ask(files: string[]) {
  selected.value = new Set()
  const todo = files.filter(name => !isRequested(name))
  if (todo.length)
    emit('download', todo)
}

function toggle(name: string, on: boolean) {
  const s = new Set(selected.value)
  if (on)
    s.add(name)
  else s.delete(name)
  selected.value = s
}
</script>

<template>
  <Collapsible v-model:open="open" class="rounded-xl bg-card">
    <div class="flex flex-col gap-3 p-3.5 sm:flex-row sm:items-center">
      <CollapsibleTrigger class="flex min-w-0 flex-1 items-start gap-3 text-left">
        <FolderIcon class="mt-0.5 size-4 shrink-0 text-faint" :stroke-width="1.75" />
        <div class="flex min-w-0 flex-col gap-1">
          <span class="truncate font-mono text-[12.5px] text-foreground" :title="directory">{{ splitPath(directory).name || directory }}</span>
          <span class="truncate font-mono text-[11px] text-faint" :title="directory">{{ splitPath(directory).dir }}</span>
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11.5px] text-muted-foreground">
            <RouterLink v-if="showPeer" :to="{ name: 'peer', params: { username } }" class="font-mono text-foreground/80 hover:text-primary" @click.stop>
              {{ username }}
            </RouterLink>
            <span class="flex gap-1">
              <FormatTag v-for="f in fmts" :key="f" :format="f" />
            </span>
            <span class="tnum">{{ t('album.trackCount', audio.length) }} · {{ bytes(size) }}</span>
            <template v-if="hasFreeSlots !== undefined">
              <span :class="hasFreeSlots ? 'text-success' : 'text-faint'">{{ hasFreeSlots ? t('peer.freeSlot') : t('peer.noFreeSlot') }}</span>
              <span class="tnum">{{ t('peer.queue', { n: queueSize ?? 0 }) }}</span>
              <span class="tnum">{{ speed(avgSpeed) }}</span>
            </template>
          </div>
        </div>
        <ChevronDownIcon :class="cn('mt-0.5 ml-auto size-4 shrink-0 text-faint transition-transform', open && 'rotate-180')" />
      </CollapsibleTrigger>
      <div class="flex shrink-0 items-center gap-2">
        <Button
          v-if="selected.size"
          variant="outline"
          size="sm"
          @click="ask([...selected])"
        >
          {{ t('folder.selected', selected.size) }}
        </Button>
        <Button size="sm" variant="secondary" :disabled="!remaining.length" @click="ask(remaining)">
          <CheckIcon v-if="!remaining.length" data-icon="inline-start" />
          <DownloadIcon v-else data-icon="inline-start" />
          <template v-if="!remaining.length">
            {{ t('folder.requested') }}
          </template>
          <template v-else-if="remaining.length < files.length">
            {{ t('folder.rest', { n: remaining.length }) }}
          </template>
          <template v-else>
            {{ t('folder.download') }}
          </template>
        </Button>
      </div>
    </div>
    <CollapsibleContent>
      <ul class="border-t px-3.5 py-1.5">
        <li v-for="f in files" :key="f.filename" class="flex items-center gap-3 py-1 text-[12px]">
          <Checkbox
            :model-value="selected.has(f.filename) || isRequested(f.filename)"
            :disabled="isRequested(f.filename)"
            :aria-label="t('folder.select', { name: splitPath(f.filename).name })"
            @update:model-value="(v) => toggle(f.filename, v === true)"
          />
          <span :class="cn('min-w-0 flex-1 truncate font-mono', f.extra || !f.format ? 'text-faint' : 'text-foreground/90')" :title="f.filename">
            {{ f.name ?? splitPath(f.filename).name }}
          </span>
          <span class="hidden font-mono text-faint tnum sm:inline">{{ f.bitrate ? `${f.bitrate} kbps` : '' }}{{ f.bit_depth ? ` ${f.bit_depth} bit` : '' }}</span>
          <span class="w-12 text-right font-mono text-faint tnum">{{ duration(f.duration) }}</span>
          <span class="w-16 text-right font-mono text-muted-foreground tnum">{{ bytes(f.size) }}</span>
        </li>
      </ul>
    </CollapsibleContent>
  </Collapsible>
</template>
