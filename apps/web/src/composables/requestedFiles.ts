import { shallowRef } from 'vue'

const NONE: ReadonlySet<string> = new Set()

/**
 * Files already asked for, per peer, while the page is open: a second click does not ask for
 * them again. Claimed on click (synchronously, before any render), released if the request fails.
 */
export function useRequestedFiles() {
  const byPeer = shallowRef(new Map<string, ReadonlySet<string>>())

  const of = (username: string) => byPeer.value.get(username) ?? NONE

  function update(username: string, files: string[], on: boolean) {
    const next = new Set(of(username))
    for (const f of files) {
      if (on)
        next.add(f)
      else next.delete(f)
    }
    byPeer.value = new Map(byPeer.value).set(username, next)
  }

  /** Marks the files and returns those that were not asked for yet */
  function claim(username: string, files: string[]) {
    const known = of(username)
    const fresh = files.filter(f => !known.has(f))
    if (fresh.length)
      update(username, fresh, true)
    return fresh
  }

  return {
    of,
    claim,
    release: (username: string, files: string[]) => update(username, files, false),
  }
}
