<script setup lang="ts">
import { PlusIcon, SearchIcon, UsersRoundIcon } from '@lucide/vue'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import AddArtistDialog from '@/components/AddArtistDialog.vue'
import CoverArt from '@/components/CoverArt.vue'
import PageHeader from '@/components/PageHeader.vue'
import { Button } from '@/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useArtists } from '@/composables/queries'
import { cn } from '@/lib/utils'

const { t } = useI18n()
const { data: artists, isPending } = useArtists()
const q = ref('')
const filter = ref<'all' | 'followed' | 'paused' | 'attention' | 'auto'>('all')
const addOpen = ref(false)

const list = computed(() => {
  const needle = q.value.trim().toLowerCase()
  return (artists.value ?? []).filter((a) => {
    if (needle && !a.name.toLowerCase().includes(needle))
      return false
    if (filter.value === 'followed')
      return a.monitored
    if (filter.value === 'attention')
      return a.stats.attention > 0
    if (filter.value === 'auto')
      return a.mode === 'auto'
    if (filter.value === 'paused')
      return !a.monitored
    return true
  })
})

function completion(a: { stats: { imported: number, monitored: number } }) {
  return a.stats.monitored ? a.stats.imported / a.stats.monitored : 0
}
</script>

<template>
  <div class="flex flex-col">
    <PageHeader :title="t('artists.title')" :description="t('artists.description')">
      <template #actions>
        <Button size="sm" @click="addOpen = true">
          <PlusIcon data-icon="inline-start" />
          {{ t('common.followArtist') }}
        </Button>
      </template>
    </PageHeader>

    <div class="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center">
      <InputGroup class="sm:max-w-xs">
        <InputGroupAddon>
          <SearchIcon />
        </InputGroupAddon>
        <InputGroupInput v-model="q" :placeholder="t('common.filter')" />
      </InputGroup>
      <ToggleGroup v-model="filter" type="single" variant="outline" size="sm" class="justify-start">
        <ToggleGroupItem value="all">
          {{ t('common.all') }}
        </ToggleGroupItem>
        <ToggleGroupItem value="followed">
          {{ t('artists.filters.followed') }}
        </ToggleGroupItem>
        <ToggleGroupItem value="paused">
          {{ t('artists.filters.paused') }}
        </ToggleGroupItem>
        <ToggleGroupItem value="attention">
          {{ t('artists.filters.attention') }}
        </ToggleGroupItem>
        <ToggleGroupItem value="auto">
          {{ t('artists.filters.auto') }}
        </ToggleGroupItem>
      </ToggleGroup>
      <span class="font-mono text-[11.5px] text-faint tnum sm:ml-auto">{{ list.length }} / {{ artists?.length ?? 0 }}</span>
    </div>

    <div v-if="isPending" class="grid grid-cols-2 gap-x-5 gap-y-7 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
      <div v-for="i in 10" :key="i" class="flex flex-col gap-2">
        <Skeleton class="aspect-square w-full" />
        <Skeleton class="h-4 w-2/3" />
      </div>
    </div>

    <Empty v-else-if="!artists?.length" class="rounded-xl bg-card/50 py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <UsersRoundIcon />
        </EmptyMedia>
        <EmptyTitle>{{ t('artists.empty') }}</EmptyTitle>
        <EmptyDescription>
          {{ t('artists.emptyHint') }}
        </EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button size="sm" @click="addOpen = true">
          <PlusIcon data-icon="inline-start" />
          {{ t('common.followArtist') }}
        </Button>
      </EmptyContent>
    </Empty>

    <div v-else class="grid grid-cols-2 gap-x-5 gap-y-7 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5">
      <RouterLink
        v-for="a in list"
        :key="a.id"
        :to="{ name: 'artist', params: { id: a.id } }"
        class="group flex flex-col gap-2.5"
      >
        <div class="relative">
          <CoverArt
            :mbid="a.stats.cover_mbid"
            :size="500"
            :alt="a.name"
            :class="cn('w-full transition-[filter,opacity] duration-300 group-hover:brightness-110', !a.monitored && 'opacity-50 grayscale')"
          />
          <span
            v-if="a.stats.attention"
            class="absolute top-2 right-2 rounded-[4px] bg-background/90 px-1.5 font-mono text-[10.5px] leading-[18px] text-warning ring-1 ring-warning/30 backdrop-blur tnum"
          >{{ t('artists.toHandle', { n: a.stats.attention }) }}</span>
          <!-- Edge: share of followed albums already in the library -->
          <div class="absolute inset-x-0 bottom-0 h-[3px] bg-background/60">
            <div class="h-full bg-success/80" :style="{ width: `${completion(a) * 100}%` }" />
          </div>
        </div>
        <div class="flex min-w-0 flex-col gap-0.5">
          <span class="truncate text-[13.5px] font-medium group-hover:text-primary">{{ a.name }}</span>
          <div class="flex items-center gap-2 text-[11.5px] text-faint">
            <span class="font-mono tnum">{{ a.stats.imported }}/{{ a.stats.monitored }}</span>
            <span>{{ t('artists.inLibrary') }}</span>
            <span class="ml-auto">{{ !a.monitored ? t('artists.mode.paused') : t(`artists.mode.${a.mode}`) }}</span>
          </div>
        </div>
      </RouterLink>
    </div>

    <AddArtistDialog v-model:open="addOpen" />
  </div>
</template>
