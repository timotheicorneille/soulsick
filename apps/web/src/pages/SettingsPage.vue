<script setup lang="ts">
import type { AppSettings, QualityProfile } from '@/lib/types'
import { PlusIcon } from '@lucide/vue'
import { useMutation, useQueryClient } from '@tanstack/vue-query'
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { toast } from 'vue-sonner'
import PageHeader from '@/components/PageHeader.vue'
import ProfileEditor from '@/components/ProfileEditor.vue'
import ProfilePicture from '@/components/ProfilePicture.vue'
import StoragePanel from '@/components/StoragePanel.vue'
import ThemeGallery from '@/components/ThemeGallery.vue'
import { Button } from '@/components/ui/button'
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldSeparator, FieldSet, FieldLegend } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '@/components/ui/spinner'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useFormats, useProfiles, useSettings, useStatus } from '@/composables/queries'
import { api } from '@/lib/api'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { getLocalePref, LOCALES, type LocalePref, setLocalePref } from '@/i18n'
import { getTheme, setTheme, type ThemePref } from '@/lib/theme'

const { t } = useI18n()
const qc = useQueryClient()
const { data: settings, isPending } = useSettings()
const { data: profiles } = useProfiles()
const { data: formats } = useFormats()
const { data: status } = useStatus()

const route = useRoute()
const router = useRouter()
const tab = ref((route.query.tab as string) || 'soulseek')
watch(tab, t => router.replace({ query: { ...route.query, tab: t } }))
const theme = ref<ThemePref>(getTheme())
function pickTheme(v: ThemePref) {
  theme.value = v
  setTheme(v)
}
const lang = ref<LocalePref>(getLocalePref())
function pickLang(v: LocalePref) {
  lang.value = v
  setLocalePref(v)
}
const form = ref<AppSettings | null>(null)
watch(settings, (s) => {
  if (s)
    form.value = JSON.parse(JSON.stringify(s))
}, { immediate: true })
const dirty = computed(() => !!form.value && JSON.stringify(form.value) !== JSON.stringify(settings.value))

const save = useMutation({
  mutationFn: () => api.put<AppSettings>('/settings', form.value),
  onSuccess: (s) => {
    qc.setQueryData(['settings'], s)
    form.value = JSON.parse(JSON.stringify(s))
    toast.success(t('settings.saved'), { description: t('settings.savedHint') })
  },
  onError: e => toast.error(e.message),
})
const testNavidrome = useMutation({
  mutationFn: () => api.post<{ ok: boolean, error?: string }>('/settings/navidrome/test'),
  onSuccess: r => (r.ok ? toast.success(t('settings.navidrome.ok')) : toast.error('Navidrome', { description: r.error })),
})

// Naming template preview, computed like the server does
const sample = { albumartist: 'Nirvana', artist: 'Nirvana', album: 'Nevermind', year: '1991', title: 'In Bloom', track: 2, disc: 1 }
const namingPreview = computed(() => {
  const tpl = form.value?.library.naming_template ?? ''
  const render = (discCount: number) => `${tpl
    .replace(/\{(\w+)(?::0?(\d+))?\}/g, (_, k: string, pad?: string) => {
      if (k === 'disc_prefix')
        return discCount > 1 ? `${sample.disc}-` : ''
      const v = (sample as Record<string, string | number>)[k]
      if (v === undefined)
        return ''
      return pad ? String(v).padStart(Number(pad), '0') : String(v)
    })
    .split('/')
    .map(s => s.trim())
    .filter(Boolean)
    .join('/')}.flac`
  return [render(1), render(2)]
})

// Profiles
const newProfile = ref<Omit<QualityProfile, 'id'> | null>(null)
const saveProfile = useMutation({
  mutationFn: (p: Omit<QualityProfile, 'id'> & { id?: number }) =>
    p.id ? api.put(`/profiles/${p.id}`, p) : api.post('/profiles', p),
  onSuccess: () => {
    newProfile.value = null
    toast.success(t('settings.profiles.saved'))
    qc.invalidateQueries({ queryKey: ['profiles'] })
  },
  onError: e => toast.error(e.message),
})
const removeProfile = useMutation({
  mutationFn: (id: number) => api.del(`/profiles/${id}`),
  onSuccess: () => qc.invalidateQueries({ queryKey: ['profiles'] }),
  onError: e => toast.error(e.message),
})
</script>

<template>
  <div class="flex flex-col">
    <PageHeader :title="t('settings.title')" :description="t('settings.description')">
      <template #actions>
        <Button v-if="tab !== 'profiles' && tab !== 'appearance'" size="sm" :disabled="!dirty || save.isPending.value" @click="save.mutate()">
          <Spinner v-if="save.isPending.value" data-icon="inline-start" />
          {{ t('common.save') }}
        </Button>
      </template>
    </PageHeader>

    <div v-if="isPending || !form" class="flex flex-col gap-3">
      <Skeleton v-for="i in 4" :key="i" class="h-12 w-full max-w-2xl" />
    </div>

    <Tabs v-else v-model="tab" class="gap-6">
      <TabsList class="flex-wrap">
        <TabsTrigger value="soulseek">
          Soulseek
        </TabsTrigger>
        <TabsTrigger value="hunter">
          {{ t('settings.tabs.hunter') }}
        </TabsTrigger>
        <TabsTrigger value="library">
          {{ t('settings.tabs.library') }}
        </TabsTrigger>
        <TabsTrigger value="profiles">
          {{ t('settings.tabs.profiles') }}
        </TabsTrigger>
        <TabsTrigger value="navidrome">
          Navidrome
        </TabsTrigger>
        <TabsTrigger value="appearance">
          {{ t('settings.tabs.appearance') }}
        </TabsTrigger>
      </TabsList>

      <TabsContent value="soulseek" class="max-w-2xl">
        <FieldGroup>
          <FieldSet>
            <FieldLegend>{{ t('settings.soulseek.account') }}</FieldLegend>
            <FieldDescription>{{ t('settings.soulseek.accountHint') }}</FieldDescription>
            <div class="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel for="su">
                  {{ t('common.username') }}
                </FieldLabel>
                <Input id="su" v-model="form.soulseek.username" class="font-mono" autocomplete="off" />
              </Field>
              <Field>
                <FieldLabel for="sp">
                  {{ t('common.password') }}
                </FieldLabel>
                <Input id="sp" v-model="form.soulseek.password" type="password" autocomplete="new-password" />
              </Field>
            </div>
            <Field>
              <FieldLabel for="sd">
                {{ t('settings.soulseek.description') }}
              </FieldLabel>
              <Input id="sd" v-model="form.soulseek.description" />
            </Field>
            <Field>
              <FieldLabel>{{ t('profilePicture.label') }}</FieldLabel>
              <ProfilePicture />
            </Field>
          </FieldSet>
          <FieldSeparator />
          <FieldSet>
            <FieldLegend>{{ t('settings.soulseek.network') }}</FieldLegend>
            <div class="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel for="lp">
                  {{ t('settings.soulseek.port') }}
                </FieldLabel>
                <Input id="lp" v-model.number="form.soulseek.listen_port" type="number" class="font-mono" :disabled="!!status?.engine.forwarded_port" />
                <FieldDescription v-if="status?.engine.forwarded_port">
                  {{ t('settings.soulseek.portVpn', { port: status.engine.forwarded_port }) }}
                </FieldDescription>
                <FieldDescription v-else>
                  {{ t('settings.soulseek.portHint', { next: form.soulseek.listen_port + 1 }) }}
                </FieldDescription>
              </Field>
              <Field orientation="horizontal" class="self-start pt-7">
                <Switch id="upnp" v-model="form.soulseek.upnp" />
                <FieldLabel for="upnp">
                  {{ t('settings.soulseek.upnp') }}
                </FieldLabel>
              </Field>
            </div>
          </FieldSet>
          <FieldSeparator />
          <FieldSet>
            <FieldLegend>{{ t('settings.soulseek.sharing') }}</FieldLegend>
            <div class="grid gap-4 sm:grid-cols-3">
              <Field>
                <FieldLabel for="slots">
                  {{ t('network.uploadSlots') }}
                </FieldLabel>
                <Input id="slots" v-model.number="form.soulseek.upload_slots" type="number" min="0" class="font-mono" />
              </Field>
              <Field>
                <FieldLabel for="ul">
                  {{ t('settings.soulseek.upLimit') }}
                </FieldLabel>
                <Input id="ul" v-model.number="form.soulseek.upload_speed_kbps" type="number" min="0" class="font-mono" />
                <FieldDescription>{{ t('settings.soulseek.unlimited') }}</FieldDescription>
              </Field>
              <Field>
                <FieldLabel for="dl">
                  {{ t('settings.soulseek.downLimit') }}
                </FieldLabel>
                <Input id="dl" v-model.number="form.soulseek.download_speed_kbps" type="number" min="0" class="font-mono" />
                <FieldDescription>{{ t('settings.soulseek.unlimited') }}</FieldDescription>
              </Field>
            </div>
            <FieldDescription>
              {{ t('settings.soulseek.sharesHint') }}
              <RouterLink :to="{ name: 'shares' }" class="text-primary underline-offset-2 hover:underline">{{ t('nav.shares') }} →</RouterLink>
            </FieldDescription>
          </FieldSet>
          <p class="text-[12px] text-faint">
            {{ t('settings.soulseek.reconnectHint') }}
          </p>
        </FieldGroup>
      </TabsContent>

      <TabsContent value="hunter" class="max-w-2xl">
        <FieldGroup>
          <Field orientation="horizontal">
            <Switch id="he" v-model="form.hunter.enabled" />
            <FieldLabel for="he">
              {{ t('settings.hunter.enabled') }}
            </FieldLabel>
          </Field>
          <div class="grid gap-4 sm:grid-cols-3">
            <Field>
              <FieldLabel for="hi">
                {{ t('settings.hunter.interval') }}
              </FieldLabel>
              <Input id="hi" v-model.number="form.hunter.interval_minutes" type="number" min="5" class="font-mono" />
            </Field>
            <Field>
              <FieldLabel for="hm">
                {{ t('settings.hunter.perRun') }}
              </FieldLabel>
              <Input id="hm" v-model.number="form.hunter.max_searches_per_run" type="number" min="1" class="font-mono" />
              <FieldDescription>{{ t('settings.hunter.perRunHint') }}</FieldDescription>
            </Field>
            <Field>
              <FieldLabel for="hw">
                {{ t('settings.hunter.wait') }}
              </FieldLabel>
              <Input id="hw" v-model.number="form.hunter.search_wait_seconds" type="number" min="5" class="font-mono" />
            </Field>
          </div>
          <div class="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel for="hp">
                {{ t('settings.hunter.proposals') }}
              </FieldLabel>
              <Input id="hp" v-model.number="form.hunter.proposals" type="number" min="1" class="font-mono" />
            </Field>
            <Field>
              <FieldLabel for="hmm">
                {{ t('settings.hunter.minMatch') }}
              </FieldLabel>
              <Input id="hmm" v-model.number="form.hunter.min_match" type="number" min="0" max="1" step="0.05" class="font-mono" />
              <FieldDescription>{{ t('settings.hunter.minMatchHint') }}</FieldDescription>
            </Field>
          </div>
          <Field orientation="horizontal">
            <Switch id="hc" v-model="form.hunter.download_covers" />
            <FieldLabel for="hc">
              {{ t('settings.hunter.extras') }}
            </FieldLabel>
          </Field>
          <FieldSeparator />
          <FieldDescription>{{ t('settings.hunter.stuckIntro') }}</FieldDescription>
          <div class="grid gap-4 sm:grid-cols-3">
            <Field>
              <FieldLabel for="hsp">
                {{ t('settings.hunter.stuckNoProgress') }}
              </FieldLabel>
              <Input id="hsp" v-model.number="form.hunter.stuck_no_progress_minutes" type="number" min="1" class="font-mono" />
            </Field>
            <Field>
              <FieldLabel for="hsq">
                {{ t('settings.hunter.stuckQueue') }}
              </FieldLabel>
              <Input id="hsq" v-model.number="form.hunter.stuck_queue_minutes" type="number" min="1" class="font-mono" />
            </Field>
            <Field>
              <FieldLabel for="hsa">
                {{ t('settings.hunter.stuckAttempts') }}
              </FieldLabel>
              <Input id="hsa" v-model.number="form.hunter.stuck_after_attempts" type="number" min="0" class="font-mono" />
              <FieldDescription>{{ t('settings.hunter.stuckAttemptsHint') }}</FieldDescription>
            </Field>
          </div>
        </FieldGroup>
      </TabsContent>

      <TabsContent value="library" class="max-w-2xl">
        <FieldGroup>
          <Field>
            <FieldLabel for="nt">
              {{ t('settings.library.template') }}
            </FieldLabel>
            <Input id="nt" v-model="form.library.naming_template" class="font-mono" />
            <FieldDescription>
              {{ t('settings.library.variables') }} <code class="font-mono">{albumartist} {artist} {album} {year} {title} {track:02} {disc} {disc_prefix} {release_type}</code>
            </FieldDescription>
            <div class="mt-1 flex flex-col gap-1 rounded-md bg-well px-3 py-2.5 font-mono text-[12px] ring-1 ring-border ring-inset">
              <span><span class="text-faint">{{ status?.paths.music }}/</span>{{ namingPreview[0] }}</span>
              <span class="text-muted-foreground"><span class="text-faint">{{ t('settings.library.multiDisc') }} …/</span>{{ namingPreview[1].split('/').pop() }}</span>
            </div>
          </Field>
          <div class="grid gap-4 sm:grid-cols-2">
            <Field orientation="horizontal">
              <Switch id="wt" v-model="form.library.write_tags" />
              <FieldLabel for="wt">
                {{ t('settings.library.writeTags') }}
              </FieldLabel>
            </Field>
            <Field orientation="horizontal">
              <Switch id="ec" v-model="form.library.embed_cover" />
              <FieldLabel for="ec">
                {{ t('settings.library.embedCover') }}
              </FieldLabel>
            </Field>
            <Field orientation="horizontal">
              <Switch id="fl" v-model="form.library.fetch_lyrics" />
              <FieldLabel for="fl">
                {{ t('settings.library.fetchLyrics') }}
              </FieldLabel>
            </Field>
          </div>
          <Field class="sm:max-w-xs">
            <FieldLabel for="si">
              {{ t('settings.library.syncInterval') }}
            </FieldLabel>
            <Input id="si" v-model.number="form.library.sync_interval_hours" type="number" min="1" class="font-mono" />
          </Field>
          <FieldSeparator />
          <StoragePanel />
          <p class="text-[12px] text-faint">
            {{ t('settings.library.pathsHint') }}
          </p>
        </FieldGroup>
      </TabsContent>

      <TabsContent value="profiles" class="flex flex-col gap-4">
        <p class="max-w-2xl text-[12.5px] text-muted-foreground">
          {{ t('settings.profiles.intro') }}
        </p>
        <ProfileEditor
          v-for="p in profiles ?? []"
          :key="p.id"
          :profile="p"
          :formats="formats ?? []"
          @save="(v) => saveProfile.mutate(v)"
          @remove="removeProfile.mutate(p.id)"
        />
        <ProfileEditor
          v-if="newProfile"
          :profile="newProfile"
          :formats="formats ?? []"
          @save="(v) => saveProfile.mutate(v)"
        />
        <Button v-else variant="outline" size="sm" class="w-fit" @click="newProfile = { name: t('settings.profiles.newName'), formats: ['flac', 'mp3-320'], upgrade_until: null, max_missing_tracks: 0 }">
          <PlusIcon data-icon="inline-start" />
          {{ t('settings.profiles.new') }}
        </Button>
      </TabsContent>

      <TabsContent value="navidrome" class="max-w-2xl">
        <FieldGroup>
          <FieldDescription>
            {{ t('settings.navidrome.intro') }}
          </FieldDescription>
          <Field orientation="horizontal">
            <Switch id="ne" v-model="form.navidrome.enabled" />
            <FieldLabel for="ne">
              {{ t('settings.navidrome.enabled') }}
            </FieldLabel>
          </Field>
          <Field>
            <FieldLabel for="nu">
              URL
            </FieldLabel>
            <Input id="nu" v-model="form!.navidrome.url" placeholder="http://navidrome:4533" class="font-mono" />
          </Field>
          <div class="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel for="nn">
                {{ t('common.username') }}
              </FieldLabel>
              <Input id="nn" v-model="form!.navidrome.username" autocomplete="off" />
            </Field>
            <Field>
              <FieldLabel for="np">
                {{ t('common.password') }}
              </FieldLabel>
              <Input id="np" v-model="form!.navidrome.password" type="password" autocomplete="new-password" />
            </Field>
          </div>
          <Button variant="outline" size="sm" class="w-fit" :disabled="dirty || testNavidrome.isPending.value" @click="testNavidrome.mutate()">
            {{ t('settings.navidrome.test') }}
          </Button>
          <FieldDescription v-if="dirty">
            {{ t('settings.navidrome.saveFirst') }}
          </FieldDescription>
        </FieldGroup>
      </TabsContent>
      <TabsContent value="appearance" class="flex flex-col gap-8">
        <FieldGroup class="max-w-2xl">
          <FieldSet>
            <FieldLegend>{{ t('settings.appearance.language') }}</FieldLegend>
            <FieldDescription>{{ t('settings.appearance.languageHint') }}</FieldDescription>
            <ToggleGroup
              :model-value="lang"
              type="single"
              variant="outline"
              size="sm"
              class="justify-start"
              @update:model-value="(v) => v && pickLang(v as LocalePref)"
            >
              <ToggleGroupItem value="auto">
                {{ t('settings.appearance.auto') }}
              </ToggleGroupItem>
              <ToggleGroupItem v-for="l in LOCALES" :key="l.value" :value="l.value">
                {{ l.label }}
              </ToggleGroupItem>
            </ToggleGroup>
          </FieldSet>
          <FieldSeparator />
          <FieldSet>
            <FieldLegend>{{ t('settings.appearance.mode') }}</FieldLegend>
            <FieldDescription>{{ t('settings.appearance.modeHint') }}</FieldDescription>
            <ToggleGroup
              :model-value="theme"
              type="single"
              variant="outline"
              size="sm"
              class="justify-start"
              @update:model-value="(v) => v && pickTheme(v as ThemePref)"
            >
              <ToggleGroupItem value="system">
                {{ t('settings.appearance.system') }}
              </ToggleGroupItem>
              <ToggleGroupItem value="light">
                {{ t('settings.appearance.light') }}
              </ToggleGroupItem>
              <ToggleGroupItem value="dark">
                {{ t('settings.appearance.dark') }}
              </ToggleGroupItem>
            </ToggleGroup>
          </FieldSet>
        </FieldGroup>
        <section class="flex flex-col gap-3">
          <div class="flex flex-col gap-1">
            <h2 class="text-[15px] font-semibold">
              {{ t('settings.appearance.themes') }}
            </h2>
            <p class="max-w-2xl text-[12.5px] text-muted-foreground">
              {{ t('settings.appearance.themesHint') }}
            </p>
          </div>
          <ThemeGallery />
        </section>
      </TabsContent>
    </Tabs>
  </div>
</template>
