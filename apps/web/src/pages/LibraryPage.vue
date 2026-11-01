<script setup lang="ts">
import type { LyricsJob, ReleaseGroup } from '@/lib/types'
import { isPartial } from '@/lib/types'
import { LibraryIcon, MicVocalIcon, SearchIcon } from '@lucide/vue'
import { useMutation, useQuery } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
import CoverArt from '@/components/CoverArt.vue'
import FormatTag from '@/components/FormatTag.vue'
import ImportPanel from '@/components/ImportPanel.vue'
import ManualFolder, { type ManualFolderInfo } from '@/components/ManualFolder.vue'
import PageHeader from '@/components/PageHeader.vue'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useStatus, useStorage } from '@/composables/queries'
import { api } from '@/lib/api'
import { ago, bytes, year } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useLive } from '@/stores/live'

type LibraryAlbum = ReleaseGroup & { sources: string[], file_count: number }

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { data: status } = useStatus()
const { data: storage } = useStorage()
const musicDisk = computed(() => storage.value?.disks.find(d => d.folders.some(f => f.key === 'music')))
const { data: albums, isPending } = useQuery({ queryKey: ['albums', 'library'], queryFn: () => api.get<LibraryAlbum[]>('/library/albums') })
const { data: manual } = useQuery({ queryKey: ['transfers', 'manual'], queryFn: () => api.get<ManualFolderInfo[]>('/library/manual') })

// Missing lyrics from LRCLIB: the pass runs in the background, its progress comes live
const live = useLive()
const { data: lyricsInitial } = useQuery({ queryKey: ['lyrics-job'], queryFn: () => api.get<LyricsJob>('/library/lyrics') })
const lyricsJob = computed(() => live.lyricsJob ?? lyricsInitial.value)
const fetchLyrics = useMutation({
  mutationFn: () => api.post<LyricsJob & { started: boolean }>('/library/lyrics'),
  onSuccess: r => r.started && toast.success(t('library.fetchLyricsStarted'), { description: t('library.fetchLyricsHint') }),
  onError: e => toast.error(t('lyrics.unavailable'), { description: e.message }),
})

const TABS = ['albums', 'manual', 'import']
const tab = ref(TABS.includes(route.query.tab as string) ? route.query.tab as string : 'albums')
watch(tab, v => router.replace({ query: v === 'albums' ? {} : { tab: v } }))

const q = ref('')
const shown = computed(() => {
  const n = q.value.trim().toLowerCase()
  return (albums.value ?? []).filter(a => !n || a.title.toLowerCase().includes(n) || a.artist?.name.toLowerCase().includes(n))
})
</script>

<template>
  <div class="flex flex-col">
    <PageHeader :title="t('library.title')">
      <template #meta>
        <span class="font-mono text-[12px] text-faint tnum">
          {{ status?.paths.music ?? '/music' }} · {{ t('library.fileCount', status?.library.files ?? 0) }} · {{ bytes(status?.library.size) }}
          <RouterLink
            v-if="musicDisk"
            :to="{ name: 'settings', query: { tab: 'library' } }"
            :class="cn('hover:underline', musicDisk.level === 'ok' ? 'hover:text-foreground' : musicDisk.level === 'low' ? 'text-warning' : 'text-destructive')"
          >· {{ t('storage.freeShort', { free: bytes(musicDisk.free) }) }}</RouterLink>
        </span>
      </template>
      <Button
        size="sm"
        variant="outline"
        :title="t('library.fetchLyricsHint')"
        :disabled="lyricsJob?.running || fetchLyrics.isPending.value"
        @click="fetchLyrics.mutate()"
      >
        <Spinner v-if="lyricsJob?.running || fetchLyrics.isPending.value" data-icon="inline-start" />
        <MicVocalIcon v-else data-icon="inline-start" />
        {{ lyricsJob?.running && lyricsJob.total ? t('library.fetchLyricsRunning', { done: lyricsJob.done, total: lyricsJob.total }) : t('library.fetchLyrics') }}
      </Button>
    </PageHeader>

    <Tabs v-model="tab" class="gap-5">
      <div class="flex flex-col gap-3 sm:flex-row sm:items-center">
        <TabsList>
          <TabsTrigger value="albums">
            {{ t('library.tabs.albums') }} <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ albums?.length ?? 0 }}</span>
          </TabsTrigger>
          <TabsTrigger value="manual">
            {{ t('library.tabs.manual') }} <span class="ml-1 font-mono text-[11px] text-faint tnum">{{ manual?.length ?? 0 }}</span>
          </TabsTrigger>
          <TabsTrigger value="import">
            {{ t('library.tabs.import') }}
          </TabsTrigger>
        </TabsList>
        <InputGroup v-if="tab === 'albums'" class="sm:ml-auto sm:max-w-xs">
          <InputGroupAddon>
            <SearchIcon />
          </InputGroupAddon>
          <InputGroupInput v-model="q" :placeholder="t('library.filterPlaceholder')" />
        </InputGroup>
      </div>

      <TabsContent value="albums">
        <div v-if="isPending" class="grid grid-cols-2 gap-5 sm:grid-cols-3 lg:grid-cols-5">
          <Skeleton v-for="i in 10" :key="i" class="aspect-square w-full" />
        </div>
        <div v-else-if="shown.length" class="grid grid-cols-2 gap-x-5 gap-y-7 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
          <RouterLink v-for="a in shown" :key="a.id" :to="{ name: 'album', params: { id: a.id } }" class="group flex flex-col gap-2.5">
            <CoverArt :mbid="a.mbid" :size="500" :alt="a.title" class="w-full transition-[filter] duration-300 group-hover:brightness-110" />
            <div class="flex min-w-0 flex-col gap-0.5">
              <span class="truncate text-[13.5px] font-medium group-hover:text-primary">{{ a.title }}</span>
              <span class="truncate text-[12px] text-muted-foreground">{{ a.artist?.name }} · {{ year(a.first_release_date) }}</span>
              <div class="mt-1 flex items-center gap-2 text-[11px] text-faint">
                <FormatTag :format="a.imported_format" />
                <span v-if="isPartial(a)" class="font-mono text-warning tnum" :title="t('library.partialHint')">{{ t('library.partial', { n: a.present_tracks, total: a.total_tracks }) }}</span>
                <span v-else class="font-mono tnum">{{ t('library.fileCount', a.file_count) }}</span>
                <span class="ml-auto">{{ ago(a.updated_at) }}</span>
              </div>
              <span v-if="a.sources.length" class="truncate text-[11px] text-faint">{{ t('library.from', { peers: a.sources.join(', ') }) }}</span>
              <span class="truncate font-mono text-[10.5px] text-faint" :title="a.imported_path ?? ''">{{ a.imported_path }}</span>
            </div>
          </RouterLink>
        </div>
        <Empty v-else class="rounded-xl bg-card/50 py-14">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <LibraryIcon />
            </EmptyMedia>
            <EmptyTitle class="text-[14px]">
              {{ albums?.length ? t('library.noMatch') : t('library.empty') }}
            </EmptyTitle>
            <EmptyDescription class="text-[12.5px]">
              {{ t('library.emptyHint') }}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </TabsContent>

      <TabsContent value="manual" class="flex flex-col gap-3">
        <p class="text-[12.5px] text-muted-foreground">
          {{ t('library.manualHint', { path: `${status?.paths.downloads ?? '/downloads'}/manual` }) }}
        </p>
        <div v-if="manual?.length" class="flex flex-col divide-y divide-border/60 rounded-xl bg-card px-4">
          <ManualFolder v-for="f in manual" :key="f.dir" :folder="f" />
        </div>
        <p v-else class="rounded-xl bg-card/50 px-4 py-6 text-[12.5px] text-faint">
          {{ t('library.manualEmpty') }}
        </p>
      </TabsContent>

      <TabsContent value="import">
        <ImportPanel />
      </TabsContent>
    </Tabs>
  </div>
</template>
