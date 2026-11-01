<script setup lang="ts">
import { RefreshCcwIcon } from '@lucide/vue'
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { toast } from 'vue-sonner'
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
import { Spinner } from '@/components/ui/spinner'
import { api } from '@/lib/api'

/**
 * Get this album again: what is downloading is dropped, the best version found on Soulseek starts
 * right away, and the shelved files are replaced once it's imported (nothing goes if nobody
 * has the album).
 */
const props = defineProps<{ albumId: number, format?: string | null, downloading?: boolean }>()
const emit = defineEmits<{ done: [] }>()
const { t } = useI18n()
const open = ref(false)
const busy = ref(false)

async function confirm() {
  open.value = false
  busy.value = true
  try {
    await api.post(`/albums/${props.albumId}/redownload`)
    toast.success(t('redownload.started'), { description: t('redownload.startedHint') })
    emit('done')
  }
  catch (e) {
    toast.error((e as Error).message)
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <Button variant="ghost" size="sm" class="text-muted-foreground" :disabled="busy" @click="open = true">
    <Spinner v-if="busy" data-icon="inline-start" />
    <RefreshCcwIcon v-else data-icon="inline-start" />
    {{ t('redownload.button') }}
  </Button>
  <AlertDialog v-model:open="open">
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>{{ t('redownload.title') }}</AlertDialogTitle>
        <AlertDialogDescription class="flex flex-col gap-2">
          <span v-if="downloading">{{ t('redownload.dropping') }}</span>
          <span>{{ t('redownload.search') }}</span>
          <span>{{ format ? t('redownload.replaceFormat', { format }) : t('redownload.replace') }}</span>
        </AlertDialogDescription>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancel>{{ t('common.cancel') }}</AlertDialogCancel>
        <AlertDialogAction @click="confirm">
          {{ t('redownload.confirm') }}
        </AlertDialogAction>
      </AlertDialogFooter>
    </AlertDialogContent>
  </AlertDialog>
</template>
