<script setup lang="ts">
import type { FileLyrics } from '@/lib/types'
import { MicVocalIcon } from '@lucide/vue'
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { cn } from '@/lib/utils'

/** Lyrics of a shelved track, as Navidrome will show them: synced (.lrc), plain text, or none. */
const props = defineProps<{ lyrics: FileLyrics | null | undefined, class?: string }>()
const { t } = useI18n()

const hint = computed(() => {
  const l = props.lyrics
  if (!l)
    return t('lyrics.noneHint')
  const kind = t(l.kind === 'synced' ? 'lyrics.syncedHint' : 'lyrics.plainHint')
  return `${kind} · ${l.source === 'file' ? t('lyrics.fromFile', { ext: l.ext }) : t('lyrics.embedded')}`
})
</script>

<template>
  <span
    :title="hint"
    :class="cn(
      'inline-flex h-[18px] items-center gap-1 rounded-[3px] px-1.5 font-mono text-[10.5px] leading-none',
      lyrics?.kind === 'synced' ? 'border border-primary/30 text-primary'
      : lyrics ? 'border border-border-strong text-muted-foreground'
        : 'text-faint',
      props.class,
    )"
  >
    <template v-if="lyrics">
      <MicVocalIcon class="size-3" />{{ t(lyrics.kind === 'synced' ? 'lyrics.synced' : 'lyrics.plain') }}
    </template>
    <template v-else>—</template>
  </span>
</template>
