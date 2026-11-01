<script setup lang="ts">
import { ref, watch } from 'vue'
import { artistPhotoUrl } from '@/lib/api'
import { cn } from '@/lib/utils'

/** Round artist photo (Deezer, through the API); their initials while it loads or when there is none. */
const props = defineProps<{
  mbid: string
  name: string
  class?: string
}>()

const failed = ref(false)
const loaded = ref(false)
watch(() => props.mbid, () => {
  failed.value = false
  loaded.value = false
})

function initials(name: string) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]!.toUpperCase()).join('')
}
</script>

<template>
  <div :class="cn('relative flex size-11 items-center justify-center overflow-hidden rounded-full bg-primary/10 font-mono text-[13px] text-primary', props.class)">
    <span v-if="!loaded">{{ initials(name) }}</span>
    <img
      v-if="!failed"
      :src="artistPhotoUrl(mbid)"
      :alt="name"
      loading="lazy"
      :class="cn('absolute inset-0 size-full object-cover transition-opacity duration-300', loaded ? 'opacity-100' : 'opacity-0')"
      @load="loaded = true"
      @error="failed = true"
    >
  </div>
</template>
