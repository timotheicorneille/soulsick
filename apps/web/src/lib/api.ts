import { serverText } from './serverText'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${url}`, {
    method,
    credentials: 'same-origin',
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!res.ok) {
    let message = res.statusText
    try {
      const data = await res.json()
      if (typeof data.detail === 'string')
        message = data.detail
      // Translatable error: { message (English), key, params }
      else if (data.detail && typeof data.detail.message === 'string')
        message = serverText(data.detail.message, data.detail)
      else if (Array.isArray(data.detail))
        message = data.detail.map((d: { msg: string }) => d.msg).join(', ')
    }
    catch {}
    if (res.status === 401 && !url.startsWith('/auth'))
      onUnauthorized?.()
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(url: string) => request<T>('GET', url),
  post: <T>(url: string, body?: unknown) => request<T>('POST', url, body ?? {}),
  put: <T>(url: string, body: unknown) => request<T>('PUT', url, body),
  patch: <T>(url: string, body: unknown) => request<T>('PATCH', url, body),
  del: <T>(url: string) => request<T>('DELETE', url),
}

export function coverUrl(mbid: string | null | undefined, size: 250 | 500 | 1200 = 250, kind = 'release-group') {
  return mbid ? `/api/covers/${kind}/${mbid}?size=${size}` : undefined
}

export function artistPhotoUrl(mbid: string) {
  return `/api/artist-photos/${mbid}`
}
