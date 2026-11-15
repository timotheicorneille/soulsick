<script setup lang="ts">
import type { BrowseDirectory } from '@/lib/types'
import { FolderTreeIcon, LockIcon, MessageSquareIcon, SearchIcon, UserRoundIcon } from '@lucide/vue'
import { useMutation, useQuery } from '@tanstack/vue-query'
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
import FolderResult from '@/components/FolderResult.vue'
import PageHeader from '@/components/PageHeader.vue'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '@/components/ui/empty'
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group'
import { Skeleton } from '@/components/ui/skeleton'
import { useRequestedFiles } from '@/composables/requestedFiles'
import { api } from '@/lib/api'
import { bytes } from '@/lib/format'
import { isAudioName } from '@/lib/files'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const username = computed(() => (route.params.username as string) || '')
const input = ref(username.value)
const filter = ref('')

const { data, isFetching, error } = useQuery({
  queryKey: ['browse', username],
  queryFn: () => api.get<{ directories: BrowseDirectory[], locked: BrowseDirectory[] }>(`/users/${encodeURIComponent(username.value)}/browse`),
  enabled: computed(() => !!username.value),
  staleTime: 5 * 60_000,
  retry: false,
})

const dirs = computed(() => {
  const needle = filter.value.trim().toLowerCase()
  return (data.value?.directories ?? [])
    .filter(d => d.files.length && (!needle || d.path.toLowerCase().includes(needle)))
    .map(d => ({ ...d, files: d.files.map(f => ({ ...f, format: isAudioName(f.name) ? f.format : null })) }))
})
const totals = computed(() => {
  const all = data.value?.directories ?? []
  return { dirs: all.length, files: all.reduce((n, d) => n + d.files.length, 0), size: all.reduce((n, d) => n + d.files.reduce((s, f) => s + (f.size ?? 0), 0), 0) }
})

const requested = useRequestedFiles()
const download = useMutation({
  mutationFn: (files: string[]) => api.post<{ ids: number[] }>('/downloads', { username: username.value, files }),
  onSuccess: d => toast.success(t('search.queued', d.ids.length)),
  onError: (e, files) => {
    requested.release(username.value, files)
    toast.error(e.message)
  },
})

function ask(files: string[]) {
  const fresh = requested.claim(username.value, files)
  if (fresh.length)
    download.mutate(fresh)
}

function go() {
  if (input.value.trim())
    router.push({ name: 'browse', params: { username: input.value.trim() } })
}
</script>

<template>
  <div class="flex flex-col">
    <PageHeader
      :title="username ? t('browse.titleOf', { peer: username }) : t('browse.title')"
      :description="t('browse.description')"
    >
      <template v-if="username" #actions>
        <Button variant="ghost" size="sm" as-child>
          <RouterLink :to="{ name: 'peer', params: { username } }">
            <UserRoundIcon data-icon="inline-start" />
            {{ t('browse.profile') }}
          </RouterLink>
        </Button>
        <Button variant="outline" size="sm" as-child>
          <RouterLink :to="{ name: 'chat', query: { with: username } }">
            <MessageSquareIcon data-icon="inline-start" />
            {{ t('chat.message') }}
          </RouterLink>
        </Button>
      </template>
    </PageHeader>

    <form class="mb-5 flex gap-2" @submit.prevent="go">
      <InputGroup class="sm:max-w-sm">
        <InputGroupAddon>
          <FolderTreeIcon />
        </InputGroupAddon>
        <InputGroupInput v-model="input" :placeholder="t('browse.placeholder')" class="font-mono" />
      </InputGroup>
      <Button type="submit" variant="secondary">
        {{ t('browse.submit') }}
      </Button>
    </form>

    <div v-if="isFetching" class="flex flex-col gap-2">
      <p class="text-[12.5px] text-muted-foreground">
        {{ t('browse.loading', { peer: username }) }}
      </p>
      <Skeleton v-for="i in 6" :key="i" class="h-16 w-full" />
    </div>

    <Alert v-else-if="error" variant="destructive">
      <AlertTitle>{{ t('browse.failed', { peer: username }) }}</AlertTitle>
      <AlertDescription>{{ error.message }}</AlertDescription>
    </Alert>

    <template v-else-if="data">
      <div class="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center">
        <p class="font-mono text-[12px] text-muted-foreground tnum">
          {{ t('browse.stats', { folders: totals.dirs, files: totals.files }) }} · {{ bytes(totals.size) }}
          <span v-if="data.locked.length" class="text-faint"> · {{ t('browse.lockedCount', data.locked.length) }}</span>
        </p>
        <InputGroup class="sm:ml-auto sm:max-w-xs">
          <InputGroupAddon>
            <SearchIcon />
          </InputGroupAddon>
          <InputGroupInput v-model="filter" :placeholder="t('browse.filter')" />
        </InputGroup>
      </div>
      <div class="flex flex-col gap-2">
        <FolderResult
          v-for="d in dirs.slice(0, 200)"
          :key="d.path"
          :username="username"
          :directory="d.path"
          :files="d.files"
          :requested="requested.of(username)"
          @download="ask"
        />
        <p v-if="dirs.length > 200" class="py-4 text-center text-[12px] text-faint">
          {{ t('browse.more', dirs.length - 200) }}
        </p>
      </div>
      <div v-if="data.locked.length" class="mt-6 flex items-center gap-2 text-[12px] text-faint">
        <LockIcon class="size-3.5" />
        {{ t('browse.locked', { peer: username }) }} {{ data.locked.map(d => d.path.split('\\').pop()).slice(0, 8).join(', ') }}…
      </div>
    </template>

    <Empty v-else class="rounded-xl bg-card/50 py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <FolderTreeIcon />
        </EmptyMedia>
        <EmptyTitle class="text-[14px]">
          {{ t('browse.emptyTitle') }}
        </EmptyTitle>
        <EmptyDescription class="text-[12.5px]">
          {{ t('browse.emptyHint') }}
        </EmptyDescription>
      </EmptyHeader>
    </Empty>
  </div>
</template>
