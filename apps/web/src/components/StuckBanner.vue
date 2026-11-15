<script setup lang="ts">
import { ArrowRightIcon, TriangleAlertIcon } from '@lucide/vue'
import { useI18n } from 'vue-i18n'
import { useStatus } from '@/composables/queries'

/**
 * Album downloads that stopped moving: one line, straight to them. With `count` (the page of an
 * album), only those of that album and no link: switching peer is right there.
 */
defineProps<{ count?: number }>()
const { t } = useI18n()
const { data: status } = useStatus()
</script>

<template>
  <RouterLink
    v-if="count === undefined && status?.stuck_transfers"
    :to="{ name: 'transfers', query: { filter: 'stuck' } }"
    class="group flex items-center gap-2.5 rounded-xl bg-warning/10 px-4 py-2.5 text-[12.5px] text-warning transition-colors hover:bg-warning/15"
  >
    <TriangleAlertIcon class="size-4 shrink-0" />
    <span class="font-medium">{{ t('stuck.banner', status.stuck_transfers) }}</span>
    <span class="text-warning/80 max-sm:hidden">{{ t('stuck.bannerHint') }}</span>
    <ArrowRightIcon class="ml-auto size-3.5 transition-transform group-hover:translate-x-0.5" />
  </RouterLink>
  <div v-else-if="count" class="flex items-center gap-2.5 rounded-xl bg-warning/10 px-4 py-2.5 text-[12.5px] text-warning">
    <TriangleAlertIcon class="size-4 shrink-0" />
    <span class="font-medium">{{ t('stuck.banner', count) }}</span>
    <span class="text-warning/80 max-sm:hidden">{{ t('stuck.bannerHint') }}</span>
  </div>
</template>
