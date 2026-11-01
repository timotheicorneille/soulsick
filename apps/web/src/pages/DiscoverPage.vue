<script setup lang="ts">
import type { DiscoverResponse, Recommendation } from '@/lib/types'
import { CompassIcon, PlusIcon, RefreshCwIcon, XIcon } from '@lucide/vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { toast } from 'vue-sonner'
import AddArtistDialog from '@/components/AddArtistDialog.vue'
import ArtistPhoto from '@/components/ArtistPhoto.vue'
import PageHeader from '@/components/PageHeader.vue'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

const { t } = useI18n()
const qc = useQueryClient()

const { data, isPending } = useQuery({
  queryKey: ['discover'],
  queryFn: () => api.get<DiscoverResponse>('/discover'),
})
const recos = computed(() => data.value?.recommendations ?? [])

const refresh = useMutation({
  mutationFn: () => api.post<{ refreshed: number }>('/discover/refresh'),
  onSuccess: (r) => {
    toast.success(t('discover.refreshed', { n: r.refreshed }))
    qc.invalidateQueries({ queryKey: ['discover'] })
    qc.invalidateQueries({ queryKey: ['similar'] })
  },
  onError: e => toast.error(t('discover.refreshFailed'), { description: e.message }),
})

const dismiss = useMutation({
  mutationFn: (mbid: string) => api.post('/discover/dismiss', { mbid }),
  onSuccess: () => qc.invalidateQueries({ queryKey: ['discover'] }),
})

// Several recommendations followed at once, with the same settings
const selected = ref(new Set<string>())
watch(recos, (list) => {
  const visible = new Set(list.map(r => r.mbid))
  if ([...selected.value].some(m => !visible.has(m)))
    selected.value = new Set([...selected.value].filter(m => visible.has(m)))
})
function select(mbids: string[], on: boolean) {
  const next = new Set(selected.value)
  for (const m of mbids) on ? next.add(m) : next.delete(m)
  selected.value = next
}
const selection = computed(() => recos.value.filter(r => selected.value.has(r.mbid)))
const allState = computed(() => !selection.value.length
  ? false
  : selection.value.length === recos.value.length ? true : 'indeterminate')

const presets = ref<{ mbid: string, name: string }[]>([])
const addOpen = ref(false)
function follow(list: Recommendation[]) {
  presets.value = list.map(r => ({ mbid: r.mbid, name: r.name }))
  addOpen.value = true
}

const dismissMany = useMutation({
  mutationFn: (mbids: string[]) => Promise.all(mbids.map(mbid => api.post('/discover/dismiss', { mbid }))),
  onSuccess: () => {
    selected.value = new Set()
    qc.invalidateQueries({ queryKey: ['discover'] })
  },
})

</script>

<template>
  <div class="flex flex-col">
    <PageHeader :title="t('discover.title')" :description="t('discover.description')">
      <template #actions>
        <Button size="sm" variant="outline" :disabled="refresh.isPending.value" @click="refresh.mutate()">
          <Spinner v-if="refresh.isPending.value" data-icon="inline-start" />
          <RefreshCwIcon v-else data-icon="inline-start" />
          {{ t('discover.refresh') }}
        </Button>
      </template>
    </PageHeader>

    <p v-if="data?.pending" class="mb-5 text-[12.5px] text-faint">
      {{ t('discover.pending', { n: data.pending }) }}
    </p>

    <div v-if="isPending" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <Skeleton v-for="i in 9" :key="i" class="h-[104px] w-full rounded-xl" />
    </div>

    <Empty v-else-if="!recos.length" class="rounded-xl bg-card/50 py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <CompassIcon />
        </EmptyMedia>
        <EmptyTitle>{{ data?.followed ? t('discover.emptyTitle') : t('discover.noArtistsTitle') }}</EmptyTitle>
        <EmptyDescription>
          {{ data?.followed ? t('discover.emptyHint') : t('discover.noArtistsHint') }}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>

    <template v-else>
      <!-- Selection bar: stays visible while scrolling -->
      <div class="sticky top-16 z-10 mb-4 flex min-h-11 flex-wrap items-center gap-2 rounded-xl border bg-background/90 px-4 py-1.5 backdrop-blur lg:top-3">
        <label class="flex items-center gap-2.5 text-[12.5px] text-muted-foreground">
          <Checkbox :model-value="allState" @update:model-value="(v) => select(recos.map(r => r.mbid), v === true)" />
          {{ selection.length ? t('discover.selected', selection.length) : t('discover.selectAll') }}
        </label>
        <div v-if="selection.length" class="ml-auto flex flex-wrap items-center gap-1">
          <Button size="sm" @click="follow(selection)">
            <PlusIcon data-icon="inline-start" />
            {{ t('discover.followSelection', selection.length) }}
          </Button>
          <Button variant="ghost" size="sm" :disabled="dismissMany.isPending.value" @click="dismissMany.mutate(selection.map(r => r.mbid))">
            <XIcon data-icon="inline-start" />
            {{ t('discover.dismiss') }}
          </Button>
          <Button variant="ghost" size="sm" class="text-muted-foreground" @click="selected = new Set()">
            {{ t('discover.deselect') }}
          </Button>
        </div>
      </div>

    <ul class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
      <li
        v-for="r in recos"
        :key="r.mbid"
        :class="cn(
          'group relative flex gap-3.5 rounded-xl bg-card/60 p-4 ring-1 ring-white/[0.03] transition-shadow',
          selected.has(r.mbid) && 'ring-primary/50',
        )"
      >
        <div class="relative shrink-0">
          <ArtistPhoto :mbid="r.mbid" :name="r.name" />
          <Checkbox
            :model-value="selected.has(r.mbid)"
            :aria-label="t('discover.selectOne', { name: r.name })"
            class="absolute -top-1 -left-1 bg-background"
            @update:model-value="(v) => select([r.mbid], v === true)"
          />
        </div>
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <div class="flex items-baseline gap-2">
            <span class="truncate text-[14px] font-medium">{{ r.name }}</span>
            <span v-if="r.comment" class="truncate text-[11.5px] text-faint">{{ r.comment }}</span>
          </div>
          <p class="line-clamp-2 text-[12px] text-muted-foreground">
            {{ t('discover.because') }}
            <template v-for="(b, i) in r.because.slice(0, 3)" :key="b.id">
              <RouterLink :to="{ name: 'artist', params: { id: b.id } }" class="text-foreground hover:text-primary">{{ b.name }}</RouterLink><span v-if="i < Math.min(r.because.length, 3) - 1">, </span>
            </template>
            <span v-if="r.because.length > 3"> {{ t('discover.andMore', { n: r.because.length - 3 }) }}</span>
          </p>
          <div class="mt-auto flex items-center gap-2 pt-1.5">
            <div class="h-1 flex-1 rounded-full bg-white/[0.06]" :title="t('discover.match', { n: r.score })">
              <div class="h-full rounded-full bg-primary/70" :style="{ width: `${r.score}%` }" />
            </div>
            <Button size="xs" @click="follow([r])">
              <PlusIcon data-icon="inline-start" />
              {{ t('discover.follow') }}
            </Button>
          </div>
        </div>
        <button
          type="button"
          class="absolute top-2 right-2 rounded p-1 text-faint opacity-0 transition-opacity hover:bg-white/5 hover:text-foreground group-hover:opacity-100 focus-visible:opacity-100"
          :aria-label="t('discover.dismiss')"
          :title="t('discover.dismiss')"
          @click="dismiss.mutate(r.mbid)"
        >
          <XIcon class="size-3.5" />
        </button>
      </li>
    </ul>
    </template>

    <AddArtistDialog v-model:open="addOpen" :presets="presets" @done="selected = new Set()" />
  </div>
</template>
