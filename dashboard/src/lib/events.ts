// AGY Live data layer: API types, paginated event capture, and a natural-language
// mapper from raw ACP/stream-json events to timeline items. Pure functions only,
// so it can be unit-tested without a DOM.

export type JobState = "starting" | "running" | "finished" | "failed" | (string & {})

export type JobSummary = {
  id: string
  state: JobState
  mode?: string | null
  workspace?: string | null
  startedAt?: string | null
  parentJobId?: string | null
  supersededBy?: string | null
  /** Logical task shared by every turn of one AGY conversation (older records: derived from parentJobId). */
  rootTaskId?: string | null
  turn?: number | null
  sessionId?: string | null
}

export type UserMessage = { requestId?: string | null; message: string; at?: number | null }

export type JobDetail = JobSummary & {
  model?: string | null
  effort?: string | null
  prompt?: string
  originalBrief?: string | null
  turnMessage?: string | null
  resumeSessionId?: string | null
  sessionMismatch?: boolean
  reportPath?: string | null
  userMessages?: UserMessage[]
  steerPending?: boolean
  processAlive?: boolean
  done?: boolean
  report?: string | null
  finalTextTruncated?: boolean
  error?: string | null
  errorDetails?: unknown
  finishStatus?: string | null
  stopReason?: string | null
  eventCount?: number
}

export type SteerReceipt = {
  jobId: string
  parentJobId: string
  requestId: string
  dashboardUrl?: string
  state?: string | null
  duplicate?: boolean
  sessionId?: string | null
  rootTaskId?: string | null
  turn?: number | null
}

/** One turn of a logical task, with its complete raw report. */
export type HistoryTurn = {
  jobId: string
  turn: number
  parentJobId?: string | null
  supersededBy?: string | null
  requestId?: string | null
  sessionId?: string | null
  resumeSessionId?: string | null
  state: JobState
  done: boolean
  acknowledged?: boolean
  startedAt?: string | null
  finishedAt?: number | null
  message?: string | null
  report?: string | null
  reportPath?: string | null
  finalTextTruncated?: boolean
  changedFiles?: string[]
  error?: string | null
}

export type TaskHistory = {
  rootTaskId: string
  latestJobId?: string | null
  sessionId?: string | null
  originalBrief?: string | null
  turnCount: number
  offset: number
  limit: number
  nextOffset: number | null
  turns: HistoryTurn[]
}

export type RawEvent = { index: number; event: unknown }

export const PAGE_LIMIT = 200
export const POLL_MS = 2500
export const MAX_STEER_MESSAGE = 8000

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

async function errorMessage(response: Response): Promise<string> {
  let message = `HTTP ${response.status}`
  try {
    const body = (await response.json()) as { error?: string }
    if (body?.error) message = body.error
  } catch {
    /* non-JSON error body */
  }
  return message
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { cache: "no-store", signal })
  if (!response.ok) throw new Error(await errorMessage(response))
  return (await response.json()) as T
}

/**
 * Same-origin JSON mutation. The browser adds `Origin`; the server rejects any
 * request whose Host/Origin is not this dashboard. Never aborted on unmount:
 * callers ignore late results instead, so a sent mutation is never half-observed.
 */
async function postJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(await errorMessage(response))
  return (await response.json()) as T
}

export function stopJob(id: string): Promise<JobDetail> {
  return postJson<JobDetail>(`/api/jobs/${encodeURIComponent(id)}/stop`, {})
}

/**
 * Sends a new message to the same AGY session as a new turn. A running turn is
 * stopped first. If the session cannot be resumed the server refuses (409) and
 * never starts a fresh session.
 */
export function continueJob(id: string, message: string, requestId: string): Promise<SteerReceipt> {
  return postJson<SteerReceipt>(`/api/jobs/${encodeURIComponent(id)}/continue`, { message, requestId })
}

/** @deprecated Same as `continueJob`; kept for older callers. */
export const steerJob = continueJob

/** Idempotency key matching the server's `[A-Za-z0-9_-]{1,80}`. */
export function newRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID()
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")
}

export async function fetchJobs(signal?: AbortSignal): Promise<JobSummary[]> {
  const data = await getJson<{ jobs?: JobSummary[] }>("/api/jobs", signal)
  return Array.isArray(data.jobs) ? data.jobs : []
}

export function fetchJob(id: string, signal?: AbortSignal): Promise<JobDetail> {
  return getJson<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`, signal)
}

export const HISTORY_PAGE_LIMIT = 50

/** Every turn of the logical task containing `id`, following server pagination. */
export async function fetchHistory(id: string, signal?: AbortSignal): Promise<TaskHistory> {
  let offset = 0
  let first: TaskHistory | null = null
  const turns: HistoryTurn[] = []
  for (let page = 0; page < 1000; page++) {
    const data = await getJson<TaskHistory>(
      `/api/jobs/${encodeURIComponent(id)}/history?offset=${offset}&limit=${HISTORY_PAGE_LIMIT}`,
      signal
    )
    first ??= data
    turns.push(...(Array.isArray(data.turns) ? data.turns : []))
    if (typeof data.nextOffset !== "number" || data.nextOffset <= offset) break
    offset = data.nextOffset
  }
  if (!first) throw new Error("Không đọc được lịch sử tác vụ")
  return { ...first, turns, offset: 0, nextOffset: null }
}

// ---------------------------------------------------------------------------
// Logical tasks: every turn of one AGY session is one sidebar entry.
// ---------------------------------------------------------------------------

export type TaskGroup = { rootId: string; latest: JobSummary; jobs: JobSummary[] }

/** Root of a job's lineage; older records without `rootTaskId` follow `parentJobId`. */
export function taskRootId(job: JobSummary, byId: Map<string, JobSummary>): string {
  if (job.rootTaskId) return job.rootTaskId
  let current = job
  const seen = new Set<string>()
  while (current.parentJobId && !seen.has(current.parentJobId)) {
    seen.add(current.parentJobId)
    const parent = byId.get(current.parentJobId)
    if (!parent) return current.parentJobId
    if (parent.rootTaskId) return parent.rootTaskId
    current = parent
  }
  return current.id
}

const timeOf = (job: JobSummary) => {
  const value = job.startedAt ? new Date(job.startedAt).getTime() : NaN
  return Number.isNaN(value) ? 0 : value
}

/** Groups turns by logical task; `latest` is the newest turn that was not continued further. */
export function groupTasks(jobs: JobSummary[]): TaskGroup[] {
  const byId = new Map(jobs.map((job) => [job.id, job]))
  const groups = new Map<string, JobSummary[]>()
  for (const job of jobs) {
    const root = taskRootId(job, byId)
    const list = groups.get(root)
    if (list) list.push(job)
    else groups.set(root, [job])
  }
  const output: TaskGroup[] = []
  for (const [rootId, list] of groups) {
    const ordered = [...list].sort((a, b) => (a.turn ?? 1) - (b.turn ?? 1) || timeOf(a) - timeOf(b))
    const open = ordered.filter((job) => !job.supersededBy)
    const candidates = open.length ? open : ordered
    output.push({ rootId, latest: candidates[candidates.length - 1], jobs: ordered })
  }
  return output.sort((a, b) => timeOf(b.latest) - timeOf(a.latest))
}

/**
 * Reads every event from `offset` until caught up. The server caps a page at
 * 200 and `after` is an inclusive line offset; we always continue from the
 * server-provided `nextOffset`, so no line is skipped or read twice.
 */
export async function fetchEventsFrom(
  id: string,
  offset: number,
  signal?: AbortSignal
): Promise<{ events: RawEvent[]; nextOffset: number }> {
  const collected: RawEvent[] = []
  let cursor = offset
  // Hard stop guards against a misbehaving server returning a stuck cursor.
  for (let page = 0; page < 10_000; page++) {
    const data = await getJson<{ events?: RawEvent[]; nextOffset?: number }>(
      `/api/jobs/${encodeURIComponent(id)}/events?after=${cursor}`,
      signal
    )
    const events = Array.isArray(data.events) ? data.events : []
    collected.push(...events)
    const next = typeof data.nextOffset === "number" ? data.nextOffset : cursor
    if (next <= cursor) break
    cursor = next
    if (events.length < PAGE_LIMIT) break
  }
  return { events: collected, nextOffset: cursor }
}

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

export type StateTone = "active" | "success" | "error" | "neutral"

export function stateInfo(state: JobState | undefined): { label: string; tone: StateTone } {
  switch (state) {
    case "starting":
      return { label: "Đang khởi động", tone: "active" }
    case "running":
      return { label: "Đang chạy", tone: "active" }
    case "finished":
      return { label: "Lượt hoàn tất", tone: "success" }
    case "cancelled":
      return {label:"Đã dừng", tone:"neutral"}
    case "timed_out":
      return {label:"Quá thời gian", tone:"error"}
    case "stalled":
      return {label:"Không có tiến triển", tone:"error"}
    case "completing":
      return {label:"Đang hoàn tất", tone:"active"}
    case "cancelling":
      return {label:"Đang dừng", tone:"active"}
    case "failed":
      return { label: "Thất bại", tone: "error" }
    default:
      return { label: state ? String(state) : "Không rõ", tone: "neutral" }
  }
}

export const isActiveState = (state: JobState | undefined) =>
  ["starting", "running", "completing", "cancelling"].includes(state ?? "")

export function modeLabel(mode?: string | null) {
  if (mode === "implement") return "Triển khai"
  if (mode === "review") return "Rà soát"
  return mode || "—"
}

export function basename(path: string) {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

export function formatTime(value?: string | null) {
  if (!value) return "—"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString("vi-VN", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit" })
}

export function formatElapsed(from?: string | null, now = Date.now()) {
  if (!from) return "—"
  const start = new Date(from).getTime()
  if (Number.isNaN(start)) return "—"
  const s = Math.max(0, Math.floor((now - start) / 1000))
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h) return `${h} giờ ${m % 60} phút`
  if (m) return `${m} phút ${s % 60} giây`
  return `${s} giây`
}

// ---------------------------------------------------------------------------
// Brief parsing: hide the injected CODE_ONLY_POLICY prefix + "Mode:" line.
// ---------------------------------------------------------------------------

const POLICY_HEADER = "Execution boundary (mandatory):"

export function parseBrief(prompt?: string | null): { objective: string; full: string; hasPolicy: boolean } {
  const full = prompt ?? ""
  let body = full
  let hasPolicy = false
  if (body.startsWith(POLICY_HEADER)) {
    const end = body.indexOf("\n\n")
    if (end !== -1) {
      body = body.slice(end + 2)
      hasPolicy = true
    }
  }
  body = body.replace(/^Mode:\s*\w+\s*\n/, "")
  return { objective: body.trim(), full, hasPolicy }
}

// ---------------------------------------------------------------------------
// Event mapper
// ---------------------------------------------------------------------------

type Obj = Record<string, unknown>
const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v)
const str = (v: unknown) => (typeof v === "string" ? v : undefined)

export type ToolStatus = "pending" | "in_progress" | "completed" | "failed"

export type TimelineItem =
  | { type: "message"; id: string; text: string }
  | { type: "thought"; id: string; text: string }
  | {
      type: "tool"
      id: string
      toolCallId: string
      kind: string
      title: string
      status: ToolStatus
      paths: string[]
    }
  | { type: "run-end"; id: string; ok: boolean; text: string }

export type Timeline = {
  items: TimelineItem[]
  changedFiles: string[]
  debug: RawEvent[] // unknown / metadata events, shown only in the debug tab
}

/** ACP update may live at event.update or event.data.update. */
function getUpdate(event: unknown): Obj | null {
  if (!isObj(event)) return null
  if (isObj(event.update)) return event.update
  if (isObj(event.data) && isObj(event.data.update)) return event.data.update
  return null
}

function contentText(content: unknown): string | undefined {
  if (isObj(content) && content.type === "text") return str(content.text)
  return undefined
}

function toolPaths(update: Obj): string[] {
  const out: string[] = []
  if (Array.isArray(update.locations)) {
    for (const loc of update.locations) if (isObj(loc) && str(loc.path)) out.push(loc.path as string)
  }
  const input = update.rawInput
  if (isObj(input)) {
    for (const key of ["path", "file_path", "filePath", "target_file"]) {
      const p = str(input[key])
      if (p) out.push(p)
    }
    // fs_write / fs_read operations array style
    if (Array.isArray(input.ops)) {
      for (const op of input.ops) if (isObj(op) && str(op.path)) out.push(op.path as string)
    }
  }
  if (Array.isArray(update.content)) {
    for (const c of update.content) if (isObj(c) && c.type === "diff" && str(c.path)) out.push(c.path as string)
  }
  return [...new Set(out)]
}

function normalizeStatus(value: unknown): ToolStatus | undefined {
  if (value === "pending" || value === "in_progress" || value === "completed" || value === "failed") return value
  return undefined
}

function inferKind(kind: string | undefined, title: string): string {
  if (kind && kind !== "other") return kind
  const t = title.toLowerCase()
  if (/write|edit|replace|insert|create/.test(t)) return "edit"
  if (/read|list|view|open/.test(t)) return "read"
  if (/search|grep|find|glob/.test(t)) return "search"
  return kind || "other"
}

/** Natural Vietnamese description of a tool call; never claims completion it hasn't seen. */
export function describeTool(item: Extract<TimelineItem, { type: "tool" }>): string {
  const target = item.paths.length
    ? item.paths.length === 1
      ? basename(item.paths[0])
      : `${basename(item.paths[0])} và ${item.paths.length - 1} tệp khác`
    : ""
  const done = item.status === "completed"
  const failed = item.status === "failed"
  const verbs: Record<string, [string, string, string]> = {
    read: ["Đang đọc", "Đã đọc", "Không đọc được"],
    edit: ["Đang sửa", "Đã sửa", "Không sửa được"],
    delete: ["Đang xoá", "Đã xoá", "Không xoá được"],
    move: ["Đang di chuyển", "Đã di chuyển", "Không di chuyển được"],
    search: ["Đang tìm kiếm", "Đã tìm kiếm", "Tìm kiếm thất bại"],
    execute: ["Đang chạy lệnh", "Đã chạy lệnh", "Lệnh thất bại"],
    fetch: ["Đang tải", "Đã tải", "Tải thất bại"],
    think: ["Đang suy luận", "Đã suy luận", "Suy luận thất bại"],
  }
  const verb = verbs[item.kind]
  if (verb) {
    const v = failed ? verb[2] : done ? verb[1] : verb[0]
    return target ? `${v} ${target}` : v
  }
  const title = item.title || "công cụ"
  if (failed) return `Thất bại: ${title}`
  return done ? `Đã xong: ${title}` : `Đang thực hiện: ${title}`
}

// Metadata updates that never belong in the main timeline.
const METADATA_UPDATES = new Set([
  "available_commands_update",
  "current_mode_update",
  "user_message_chunk",
  "plan",
  "session_info_update",
  "usage_update",
  "config_option_update",
])

/**
 * Builds the timeline from all captured events, in index order. Consecutive
 * message chunks concatenate; consecutive thought chunks concatenate; tool
 * calls upsert by toolCallId.
 */
export function buildTimeline(events: RawEvent[]): Timeline {
  const items: TimelineItem[] = []
  const debug: RawEvent[] = []
  const tools = new Map<string, Extract<TimelineItem, { type: "tool" }>>()

  for (const raw of events) {
    const event = raw.event
    const update = getUpdate(event)
    const kind = update ? str(update.sessionUpdate) : undefined

    if (update && kind === "agent_message_chunk") {
      const text = contentText(update.content)
      if (text === undefined) {
        debug.push(raw)
        continue
      }
      const last = items[items.length - 1]
      if (last?.type === "message") last.text += text
      else items.push({ type: "message", id: `m-${raw.index}`, text })
      continue
    }

    if (update && kind === "agent_thought_chunk") {
      const text = contentText(update.content) ?? ""
      const last = items[items.length - 1]
      if (last?.type === "thought") last.text += text
      else items.push({ type: "thought", id: `t-${raw.index}`, text })
      continue
    }

    if (update && (kind === "tool_call" || kind === "tool_call_update")) {
      const toolCallId = str(update.toolCallId)
      if (!toolCallId) {
        debug.push(raw)
        continue
      }
      const existing = tools.get(toolCallId)
      const title = str(update.title) ?? existing?.title ?? ""
      const paths = toolPaths(update)
      if (existing) {
        existing.title = title
        existing.kind = inferKind(str(update.kind) ?? existing.kind, title)
        existing.status = normalizeStatus(update.status) ?? existing.status
        if (paths.length) existing.paths = [...new Set([...existing.paths, ...paths])]
      } else {
        const item = {
          type: "tool" as const,
          id: `tool-${toolCallId}`,
          toolCallId,
          title,
          kind: inferKind(str(update.kind), title),
          status: normalizeStatus(update.status) ?? "pending",
          paths,
        }
        tools.set(toolCallId, item)
        items.push(item)
      }
      continue
    }

    if (isObj(event) && (event.type === "runFinished" || event.type === "runError")) {
      const data = isObj(event.data) ? event.data : {}
      const status = str(data.status)
      const ok = event.type === "runFinished" && !["error", "failed", "cancelled"].includes(status ?? "")
      const message = str(data.message) ?? str(event.message)
      items.push({
        type: "run-end",
        id: `end-${raw.index}`,
        ok,
        text: ok
          ? "Lượt hoàn tất · phiên AGY được giữ lại để tiếp tục"
          : `Lượt kết thúc với lỗi${message ? `: ${message}` : ""}`,
      })
      continue
    }

    // Metadata (METADATA_UPDATES) and unknown events: debug tab only.
    debug.push(raw)
  }

  // Changed files: only from completed edit tool calls.
  const changed = new Set<string>()
  for (const t of tools.values()) {
    if (t.kind === "edit" && t.status === "completed") t.paths.forEach((p) => changed.add(p))
  }

  return { items, changedFiles: [...changed], debug }
}

/** One-line summary of a thought block without dumping the chain. */
export function summarizeThought(text: string, active: boolean) {
  const words = text.trim().split(/\s+/).filter(Boolean).length
  const label = active ? "Đang suy luận" : "Đã suy luận"
  return words ? `${label} · ${words} từ` : label
}

export function isMetadataUpdate(event: unknown) {
  const kind = str(getUpdate(event)?.sessionUpdate)
  return !!kind && METADATA_UPDATES.has(kind)
}
