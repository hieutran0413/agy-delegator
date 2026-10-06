import * as React from "react"
import {
  ArrowDownIcon,
  ArrowRightIcon,
  BrainIcon,
  CheckIcon,
  ChevronRightIcon,
  CopyIcon,
  DownloadIcon,
  FileCode2Icon,
  FlaskConicalIcon,
  InboxIcon,
  LoaderCircleIcon,
  MenuIcon,
  MoonIcon,
  MoreHorizontalIcon,
  RefreshCwIcon,
  SearchIcon,
  SendIcon,
  SquareIcon,
  SunIcon,
  TriangleAlertIcon,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { Input } from "@/components/ui/input"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent, MessageGroup, MessageHeader } from "@/components/ui/message"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Separator } from "@/components/ui/separator"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupTextarea } from "@/components/ui/input-group"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { AgentAvatar, MarkdownText, StatusPill, ToolMarker, downloadText, useCopy } from "@/components/agy-parts"
import { DemoView } from "@/components/demo-view"
import { cn } from "@/lib/utils"
import {
  MAX_STEER_MESSAGE,
  POLL_MS,
  basename,
  buildTimeline,
  continueJob,
  fetchEventsFrom,
  fetchHistory,
  fetchJob,
  fetchJobs,
  formatElapsed,
  formatTime,
  groupTasks,
  isActiveState,
  modeLabel,
  newRequestId,
  parseBrief,
  stateInfo,
  stopJob,
  summarizeThought,
  type HistoryTurn,
  type JobDetail,
  type JobSummary,
  type RawEvent,
  type TaskHistory,
  type Timeline,
} from "@/lib/events"

// ---------------------------------------------------------------------------
// URL + theme
// ---------------------------------------------------------------------------

function readParams() {
  const p = new URLSearchParams(window.location.search)
  return { job: p.get("job"), demo: p.get("demo") === "1" }
}

function writeParams(next: { job?: string | null; demo?: boolean }) {
  const p = new URLSearchParams(window.location.search)
  if (next.demo) {
    p.set("demo", "1")
    p.delete("job")
  } else {
    p.delete("demo")
    if (next.job) p.set("job", next.job)
    else p.delete("job")
  }
  const query = p.toString()
  window.history.replaceState(null, "", query ? `?${query}` : window.location.pathname)
}

type Theme = "dark" | "light"
const THEME_KEY = "agy-live-theme"

function useTheme() {
  const [theme, setTheme] = React.useState<Theme>(() => {
    try {
      return localStorage.getItem(THEME_KEY) === "light" ? "light" : "dark"
    } catch {
      return "dark"
    }
  })
  React.useLayoutEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark")
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* storage unavailable */
    }
  }, [theme])
  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) }
}

// ---------------------------------------------------------------------------
// Data hooks
// ---------------------------------------------------------------------------

function useJobs(autoRefresh: boolean) {
  const [jobs, setJobs] = React.useState<JobSummary[] | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(false)
  const [nonce, setNonce] = React.useState(0)

  React.useEffect(() => {
    const ac = new AbortController()
    let timer: number | undefined
    const tick = async () => {
      setLoading(true)
      try {
        const list = await fetchJobs(ac.signal)
        setJobs(list)
        setError(null)
      } catch (e) {
        if (ac.signal.aborted) return
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!ac.signal.aborted) setLoading(false)
      }
      if (autoRefresh && !ac.signal.aborted) timer = window.setTimeout(tick, POLL_MS)
    }
    tick()
    return () => {
      ac.abort()
      window.clearTimeout(timer)
    }
  }, [autoRefresh, nonce])

  return { jobs, error, loading, refresh: () => setNonce((n) => n + 1) }
}

/**
 * Polls one job: detail first, then every event page from the saved offset.
 * A generation counter + AbortController discard stale responses on job switch.
 */
function useJob(id: string | null, autoRefresh: boolean) {
  const [detail, setDetail] = React.useState<JobDetail | null>(null)
  const [events, setEvents] = React.useState<RawEvent[]>([])
  const [error, setError] = React.useState<string | null>(null)
  const [loading, setLoading] = React.useState(false)
  const [nonce, setNonce] = React.useState(0)
  const generation = React.useRef(0)
  const offset = React.useRef(0)
  const currentId = React.useRef<string | null>(null)

  React.useEffect(() => {
    const gen = ++generation.current
    if (currentId.current !== id) {
      currentId.current = id
      offset.current = 0
      setDetail(null)
      setEvents([])
      setError(null)
    }
    if (!id) return
    const ac = new AbortController()
    let timer: number | undefined
    setLoading(true)

    const tick = async () => {
      try {
        // Detail before events: once detail is terminal, the following event read includes the final lines.
        const job = await fetchJob(id, ac.signal)
        const page = await fetchEventsFrom(id, offset.current, ac.signal)
        if (gen !== generation.current) return
        offset.current = page.nextOffset
        setDetail(job)
        if (page.events.length) {
          setEvents((prev) => {
            const last = prev.length ? prev[prev.length - 1].index : -1
            const fresh = page.events.filter((e) => e.index > last)
            return fresh.length ? [...prev, ...fresh] : prev
          })
        }
        setError(null)
        setLoading(false)
        if (autoRefresh && isActiveState(job.state)) timer = window.setTimeout(tick, POLL_MS)
      } catch (e) {
        if (ac.signal.aborted || gen !== generation.current) return
        setError(e instanceof Error ? e.message : String(e))
        setLoading(false)
      }
    }
    tick()
    return () => {
      ac.abort()
      window.clearTimeout(timer)
    }
  }, [id, autoRefresh, nonce])

  const timeline = React.useMemo(() => buildTimeline(events), [events])
  return { detail, events, timeline, error, loading, retry: () => setNonce((n) => n + 1) }
}

/**
 * Prior turns of the same logical task (same AGY session). Refetched when the
 * selected turn changes state or is continued; stale responses are dropped.
 */
function useHistory(id: string | null, version: string) {
  const [history, setHistory] = React.useState<TaskHistory | null>(null)
  const [error, setError] = React.useState<string | null>(null)
  React.useEffect(() => {
    setHistory((current) => (current && id && current.turns.some((t) => t.jobId === id) ? current : null))
    if (!id) return
    const ac = new AbortController()
    fetchHistory(id, ac.signal)
      .then((data) => {
        if (ac.signal.aborted) return
        setHistory(data)
        setError(null)
      })
      .catch((e) => {
        if (ac.signal.aborted) return
        setError(e instanceof Error ? e.message : String(e))
      })
    return () => ac.abort()
  }, [id, version])
  return { history, error }
}

function useNow(enabled: boolean) {
  const [now, setNow] = React.useState(() => Date.now())
  React.useEffect(() => {
    if (!enabled) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [enabled])
  return now
}

// ---------------------------------------------------------------------------
// Small UI helpers
// ---------------------------------------------------------------------------

function IconButton({
  label,
  children,
  ...props
}: React.ComponentProps<typeof Button> & { label: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label={label} {...props}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function jobTitle(job: { workspace?: string | null; id: string }) {
  return job.workspace ? basename(job.workspace) : job.id
}

type StatusFilter = "all" | "active" | "finished" | "failed"

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function Sidebar(props: {
  jobs: JobSummary[] | null
  jobsError: string | null
  jobsLoading: boolean
  selected: string | null
  onSelect: (id: string) => void
  onRefresh: () => void
  onDemo: () => void
  autoRefresh: boolean
  onAutoRefresh: (v: boolean) => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const [query, setQuery] = React.useState("")
  const [filter, setFilter] = React.useState<StatusFilter>("all")
  const searchId = React.useId()
  const autoId = React.useId()

  const groups = React.useMemo(() => groupTasks(props.jobs ?? []), [props.jobs])
  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase()
    return groups.filter(({ latest, jobs }) => {
      // Status filters apply to the latest turn: that is the task's current state.
      if (filter === "active" && !isActiveState(latest.state)) return false
      if (filter === "finished" && latest.state !== "finished") return false
      if (filter === "failed" && latest.state !== "failed") return false
      if (!q) return true
      return jobs.some((job) => [job.id, job.workspace, job.mode, job.sessionId].some((v) => v?.toLowerCase().includes(q)))
    })
  }, [groups, query, filter])

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 px-4 pt-4 pb-3">
        <span aria-hidden="true" className="grid size-6 place-items-center rounded-md bg-primary/15 text-xs font-bold text-primary">
          A
        </span>
        <span className="flex-1 text-sm font-semibold tracking-tight">AGY Live</span>
        <IconButton label={props.theme === "dark" ? "Chuyển sang giao diện sáng" : "Chuyển sang giao diện tối"} onClick={props.onToggleTheme}>
          {props.theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </IconButton>
        <IconButton label="Làm mới danh sách" onClick={props.onRefresh}>
          <RefreshCwIcon className={cn(props.jobsLoading && "animate-spin motion-reduce:animate-none")} />
        </IconButton>
      </div>

      <div className="flex flex-col gap-2 px-4 pb-3">
        <label htmlFor={searchId} className="sr-only">
          Tìm tác vụ
        </label>
        <div className="relative">
          <SearchIcon aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            id={searchId}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Tìm theo thư mục, mã…"
            className="h-8 pl-8 text-sm"
          />
        </div>
        <Select value={filter} onValueChange={(v) => setFilter(v as StatusFilter)}>
          <SelectTrigger size="sm" className="w-full" aria-label="Lọc theo trạng thái">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">Tất cả trạng thái</SelectItem>
            <SelectItem value="active">Đang chạy</SelectItem>
            <SelectItem value="finished">Hoàn tất</SelectItem>
            <SelectItem value="failed">Thất bại</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <Separator />

      <ScrollArea className="min-h-0 flex-1">
        <nav aria-label="Danh sách tác vụ" className="flex flex-col gap-0.5 p-2">
          {props.jobs === null && !props.jobsError &&
            Array.from({ length: 4 }, (_, i) => (
              <div key={i} className="flex flex-col gap-1.5 rounded-md px-2.5 py-2">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
              </div>
            ))}
          {props.jobsError && (
            <Alert variant="destructive" className="text-xs">
              <TriangleAlertIcon />
              <AlertTitle>Không tải được danh sách</AlertTitle>
              <AlertDescription>
                <p>{props.jobsError}</p>
                <Button size="xs" variant="outline" onClick={props.onRefresh}>
                  Thử lại
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {props.jobs && filtered.length === 0 && (
            <p className="px-2.5 py-6 text-center text-xs text-muted-foreground">
              {props.jobs.length ? "Không có tác vụ phù hợp bộ lọc." : "Chưa có tác vụ nào."}
            </p>
          )}
          {filtered.map(({ rootId, latest: job, jobs: turns }) => {
            const info = stateInfo(job.state)
            const selected = turns.some((turn) => turn.id === props.selected)
            const turnCount = Math.max(turns.length, job.turn ?? 1)
            return (
              <button
                key={rootId}
                type="button"
                onClick={() => props.onSelect(job.id)}
                aria-current={selected ? "page" : undefined}
                aria-label={`${jobTitle(job)}, ${info.label}, ${turnCount} lượt trong cùng phiên`}
                className={cn(
                  "group flex w-full flex-col gap-0.5 rounded-md px-2.5 py-2 text-left outline-none transition-colors hover:bg-accent/60 focus-visible:ring-[3px] focus-visible:ring-ring/50",
                  selected && "bg-accent text-accent-foreground"
                )}
              >
                <span className="flex items-center gap-2">
                  <span
                    aria-hidden="true"
                    className={cn(
                      "size-1.5 shrink-0 rounded-full",
                      info.tone === "active" && "agy-pulse bg-primary",
                      info.tone === "success" && "bg-success",
                      info.tone === "error" && "bg-destructive",
                      info.tone === "neutral" && "bg-muted-foreground"
                    )}
                  />
                  <span className="truncate text-sm font-medium">{jobTitle(job)}</span>
                  {turnCount > 1 && (
                    <Badge variant="secondary" className="ml-auto shrink-0 rounded-full px-1.5 py-0 text-[10px]">
                      {turnCount} lượt
                    </Badge>
                  )}
                </span>
                <span className="truncate pl-3.5 text-xs text-muted-foreground">
                  {info.label} · {modeLabel(job.mode)} · {formatTime(job.startedAt)}
                </span>
              </button>
            )
          })}
        </nav>
      </ScrollArea>

      <Separator />
      <div className="flex flex-col gap-3 p-4">
        <div className="flex items-center justify-between gap-2">
          <label htmlFor={autoId} className="text-xs text-muted-foreground">
            Tự động cập nhật (2,5 giây)
          </label>
          <Switch id={autoId} size="sm" checked={props.autoRefresh} onCheckedChange={props.onAutoRefresh} />
        </div>
        <Button variant="outline" size="sm" onClick={props.onDemo}>
          <FlaskConicalIcon aria-hidden="true" />
          Xem demo ngoại tuyến
        </Button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Activity timeline
// ---------------------------------------------------------------------------

function ThoughtBlock({ text, active }: { text: string; active: boolean }) {
  return (
    <Collapsible>
      <CollapsibleTrigger asChild>
        <Marker asChild className="w-fit rounded-sm outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <button type="button" className="group/thought">
            <MarkerIcon>
              <BrainIcon />
            </MarkerIcon>
            <MarkerContent>{summarizeThought(text, active)}</MarkerContent>
            <ChevronRightIcon aria-hidden="true" className="size-3.5 transition-transform group-data-[state=open]/thought:rotate-90" />
          </button>
        </Marker>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <p className="mt-1.5 border-l-2 pl-3 text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground">{text}</p>
      </CollapsibleContent>
    </Collapsible>
  )
}

function turnEndLabel(turn: HistoryTurn) {
  if (turn.state === "finished") return `Lượt ${turn.turn} hoàn tất · phiên AGY được giữ lại`
  if (turn.state === "cancelled") return `Lượt ${turn.turn} đã dừng · phiên AGY được giữ lại`
  return `Lượt ${turn.turn}: ${stateInfo(turn.state).label}${turn.error ? ` · ${turn.error}` : ""}`
}

/** One earlier turn of the same session: the message sent, then AGY's full report. */
function PriorTurn({ turn }: { turn: HistoryTurn }) {
  const failed = !["finished", "cancelled"].includes(String(turn.state))
  return (
    <>
      {turn.message && (
        <MessageScrollerItem messageId={`turn-${turn.jobId}-message`}>
          <Message align="end">
            <MessageContent>
              <MessageHeader>{turn.turn === 1 ? "Yêu cầu từ Codex" : `Tin nhắn tiếp tục phiên · lượt ${turn.turn}`}</MessageHeader>
              <Bubble variant="tinted" align="end" className="max-w-[88%]">
                <BubbleContent className="max-h-64 overflow-y-auto whitespace-pre-wrap">{turn.message}</BubbleContent>
              </Bubble>
            </MessageContent>
          </Message>
        </MessageScrollerItem>
      )}
      <MessageScrollerItem messageId={`turn-${turn.jobId}-report`}>
        <Message>
          <AgentAvatar />
          <MessageContent className="gap-2">
            <MessageHeader className="px-0">AGY · báo cáo lượt {turn.turn}</MessageHeader>
            <Bubble variant="ghost">
              <BubbleContent>
                {turn.report ? (
                  <MarkdownText text={turn.report} />
                ) : (
                  <p className="text-sm text-muted-foreground">Lượt này không trả về báo cáo.</p>
                )}
              </BubbleContent>
            </Bubble>
          </MessageContent>
        </Message>
      </MessageScrollerItem>
      <MessageScrollerItem messageId={`turn-${turn.jobId}-end`}>
        <Marker variant="separator" className={cn("text-xs", failed && "text-destructive")}>
          <MarkerContent>{turnEndLabel(turn)}</MarkerContent>
        </Marker>
      </MessageScrollerItem>
    </>
  )
}

function ActivityView({
  detail,
  timeline,
  loading,
  history,
}: {
  detail: JobDetail | null
  timeline: Timeline
  loading: boolean
  history: TaskHistory | null
}) {
  const brief = parseBrief(detail?.prompt)
  // A continued turn's prompt is only its new message; show the original brief and messages separately.
  const objective = detail?.originalBrief?.trim() || brief.objective
  const userMessages = detail?.userMessages ?? []
  const currentIndex = history && detail ? history.turns.findIndex((t) => t.jobId === detail.id) : -1
  // With history, earlier turns render as one conversation; otherwise fall back to the record's own messages.
  const priorTurns = history && currentIndex > 0 ? history.turns.slice(0, currentIndex) : []
  const current = history && currentIndex >= 0 ? history.turns[currentIndex] : null
  const currentTurn = current?.turn ?? detail?.turn ?? 1
  const active = isActiveState(detail?.state)
  const items = timeline.items
  const last = items[items.length - 1]
  const lastIsActiveTool = last?.type === "tool" && (last.status === "pending" || last.status === "in_progress")

  // Group consecutive agent items (messages, thoughts, tool markers) under one AGY header.
  const groups = React.useMemo(() => {
    const out: { id: string; items: typeof items }[] = []
    for (const item of items) {
      if (item.type === "run-end") {
        out.push({ id: item.id, items: [item] })
        continue
      }
      const g = out[out.length - 1]
      if (g && g.items[0].type !== "run-end") g.items.push(item)
      else out.push({ id: item.id, items: [item] })
    }
    return out
  }, [items])

  return (
    <MessageScrollerProvider autoScroll defaultScrollPosition="end">
      <MessageScroller className="h-full">
        <MessageScrollerViewport aria-label="Dòng hoạt động">
          <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-6 px-4 py-6 sm:px-6">
            {current ? (
              <>
                {priorTurns.map((turn) => (
                  <PriorTurn key={turn.jobId} turn={turn} />
                ))}
                {current.message && (
                  <MessageScrollerItem messageId={`turn-${current.jobId}-message`}>
                    <Message align="end">
                      <MessageContent>
                        <MessageHeader>
                          {currentTurn === 1 ? "Yêu cầu từ Codex" : `Tin nhắn tiếp tục phiên · lượt ${currentTurn}`}
                        </MessageHeader>
                        <Bubble variant="tinted" align="end" className="max-w-[88%]">
                          <BubbleContent className="max-h-64 overflow-y-auto whitespace-pre-wrap">{current.message}</BubbleContent>
                        </Bubble>
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                )}
              </>
            ) : (
              <>
                {objective && (
                  <MessageScrollerItem messageId="brief">
                    <Message align="end">
                      <MessageContent>
                        <MessageHeader>Yêu cầu từ Codex</MessageHeader>
                        <Bubble variant="tinted" align="end" className="max-w-[88%]">
                          <BubbleContent className="max-h-64 overflow-y-auto whitespace-pre-wrap">{objective}</BubbleContent>
                        </Bubble>
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                )}

                {userMessages.map((item, i) => (
                  <MessageScrollerItem key={item.requestId ?? `user-${i}`} messageId={`user-${item.requestId ?? i}`}>
                    <Message align="end">
                      <MessageContent>
                        <MessageHeader>
                          {i === userMessages.length - 1 ? "Tin nhắn tiếp tục phiên" : "Tin nhắn tiếp tục phiên trước đó"}
                        </MessageHeader>
                        <Bubble variant="tinted" align="end" className="max-w-[88%]">
                          <BubbleContent className="max-h-64 overflow-y-auto whitespace-pre-wrap">{item.message}</BubbleContent>
                        </Bubble>
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                ))}
              </>
            )}

            {loading && items.length === 0 && (
              <MessageScrollerItem>
                <div className="flex gap-2" aria-busy="true" aria-label="Đang tải hoạt động">
                  <Skeleton className="size-6 rounded-full" />
                  <div className="flex flex-1 flex-col gap-2">
                    <Skeleton className="h-3 w-24" />
                    <Skeleton className="h-4 w-4/5" />
                    <Skeleton className="h-4 w-3/5" />
                  </div>
                </div>
              </MessageScrollerItem>
            )}

            {!loading && detail && items.length === 0 && !active && (
              <MessageScrollerItem>
                <p className="py-8 text-center text-sm text-muted-foreground">Tác vụ này không ghi nhận hoạt động nào.</p>
              </MessageScrollerItem>
            )}

            {groups.map((group, gi) => {
              const first = group.items[0]
              if (first.type === "run-end") {
                return (
                  <MessageScrollerItem key={group.id} messageId={group.id}>
                    <Marker variant="separator" className={cn("text-xs", !first.ok && "text-destructive")}>
                      <MarkerContent>{first.text}</MarkerContent>
                    </Marker>
                  </MessageScrollerItem>
                )
              }
              const isLastGroup = gi === groups.length - 1
              return (
                <MessageScrollerItem key={group.id} messageId={group.id}>
                  <Message>
                    <AgentAvatar />
                    <MessageContent className="gap-2">
                      <MessageHeader className="px-0">AGY</MessageHeader>
                      <MessageGroup className="gap-2.5">
                        {group.items.map((item, ii) => {
                          const isLast = isLastGroup && ii === group.items.length - 1
                          if (item.type === "message")
                            return (
                              <Bubble key={item.id} variant="ghost">
                                <BubbleContent>
                                  <MarkdownText text={item.text} />
                                </BubbleContent>
                              </Bubble>
                            )
                          if (item.type === "thought") return <ThoughtBlock key={item.id} text={item.text} active={active && isLast} />
                          if (item.type === "tool") return <ToolMarker key={item.id} item={item} />
                          return null
                        })}
                      </MessageGroup>
                    </MessageContent>
                  </Message>
                </MessageScrollerItem>
              )
            })}

            {active && !lastIsActiveTool && !loading && (
              <MessageScrollerItem>
                <Marker role="status" className="pl-8 text-xs">
                  <MarkerIcon>
                    <span className="agy-pulse block size-2 translate-y-1 rounded-full bg-primary" />
                  </MarkerIcon>
                  <MarkerContent>
                    {detail?.state === "starting" ? "AGY đang khởi động…" : "Đang chờ hoạt động tiếp theo từ AGY…"}
                  </MarkerContent>
                </Marker>
              </MessageScrollerItem>
            )}
          </MessageScrollerContent>
        </MessageScrollerViewport>
        <MessageScrollerButton direction="end" size="sm" className="gap-1.5 rounded-full px-3 shadow-sm" aria-label="Cuộn đến hoạt động mới nhất">
          <ArrowDownIcon aria-hidden="true" />
          <span className="text-xs">Mới nhất</span>
        </MessageScrollerButton>
      </MessageScroller>
    </MessageScrollerProvider>
  )
}

// ---------------------------------------------------------------------------
// Result / files / details tabs
// ---------------------------------------------------------------------------

function ResultView({ detail }: { detail: JobDetail | null }) {
  const { copied, failed, copy } = useCopy()
  if (!detail) return <TabSkeleton />
  const active = isActiveState(detail.state)
  const report = detail.report ?? ""

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6 sm:px-6">
      {detail.error && (
        <Alert variant="destructive">
          <TriangleAlertIcon />
          <AlertTitle>Tác vụ thất bại</AlertTitle>
          <AlertDescription>
            <p className="whitespace-pre-wrap">{detail.error}</p>
            {detail.errorDetails != null && (
              <Collapsible>
                <CollapsibleTrigger className="text-xs underline underline-offset-3">Chi tiết lỗi (kỹ thuật)</CollapsibleTrigger>
                <CollapsibleContent>
                  <pre className="mt-2 max-h-60 overflow-auto rounded-md bg-muted p-2 font-mono text-xs text-foreground">
                    {JSON.stringify(detail.errorDetails, null, 2)}
                  </pre>
                </CollapsibleContent>
              </Collapsible>
            )}
          </AlertDescription>
        </Alert>
      )}

      {report ? (
        <Card className="gap-4 py-5">
          <CardHeader className="px-5">
            <CardTitle className="text-sm">Báo cáo cuối cùng</CardTitle>
            <CardDescription className="text-xs">Nội dung gốc do AGY viết</CardDescription>
            <CardAction className="flex gap-1">
              <Button size="sm" variant="ghost" onClick={() => copy("report", report)}>
                {copied === "report" ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
                {copied === "report" ? "Đã sao chép" : failed === "report" ? "Không sao chép được" : "Sao chép"}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => downloadText(`${detail.id}-report.md`, report)}>
                <DownloadIcon aria-hidden="true" />
                Tải về
              </Button>
            </CardAction>
          </CardHeader>
          <Separator />
          <CardContent className="px-5">
            {detail.finalTextTruncated && (
              <p className="mb-3 text-xs text-warning">Báo cáo đã bị cắt bớt bởi AGY; nội dung có thể chưa đầy đủ.</p>
            )}
            <MarkdownText text={report} />
          </CardContent>
        </Card>
      ) : (
        !detail.error && (
          <Empty className="border">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <InboxIcon />
              </EmptyMedia>
              <EmptyTitle className="text-base">{active ? "Chưa có kết quả" : "Không có báo cáo"}</EmptyTitle>
              <EmptyDescription>
                {active ? "Kết quả sẽ xuất hiện khi AGY hoàn tất tác vụ." : "Tác vụ đã kết thúc nhưng không trả về nội dung báo cáo."}
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        )
      )}
    </div>
  )
}

function FilesView({ detail, files }: { detail: JobDetail | null; files: string[] }) {
  const { copied, copy } = useCopy()
  if (!detail) return <TabSkeleton />
  const active = isActiveState(detail.state)
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-6 sm:px-6">
      {files.length === 0 ? (
        <Empty className="border">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <FileCode2Icon />
            </EmptyMedia>
            <EmptyTitle className="text-base">Chưa có tệp nào được sửa</EmptyTitle>
            <EmptyDescription>
              {detail.mode === "review"
                ? "Chế độ rà soát chỉ đọc tệp, không sửa."
                : active
                  ? "Danh sách cập nhật khi AGY hoàn tất một thao tác sửa."
                  : "Không ghi nhận thao tác sửa nào đã hoàn tất."}
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <Card className="gap-0 py-0">
          <CardHeader className="px-5 py-4">
            <CardTitle className="text-sm">{files.length} tệp đã sửa</CardTitle>
            <CardDescription className="text-xs">Chỉ tính các thao tác sửa đã hoàn tất</CardDescription>
            <CardAction>
              <Button size="sm" variant="ghost" onClick={() => copy("all", files.join("\n"))}>
                {copied === "all" ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
                {copied === "all" ? "Đã sao chép" : "Sao chép tất cả"}
              </Button>
            </CardAction>
          </CardHeader>
          <Separator />
          <ul className="divide-y">
            {files.map((path) => (
              <li key={path} className="flex items-center gap-3 px-5 py-2.5">
                <FileCode2Icon aria-hidden="true" className="size-4 shrink-0 text-primary" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{basename(path)}</p>
                  <p className="truncate font-mono text-xs text-muted-foreground" title={path}>
                    {path}
                  </p>
                </div>
                <IconButton label={copied === path ? "Đã sao chép" : `Sao chép đường dẫn ${basename(path)}`} onClick={() => copy(path, path)}>
                  {copied === path ? <CheckIcon /> : <CopyIcon />}
                </IconButton>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}

function DetailRow({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-4">
      <dt className="w-36 shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("min-w-0 text-sm break-all", mono && "font-mono text-xs")}>{value ?? "—"}</dd>
    </div>
  )
}

function DetailsView({ detail, events, timeline }: { detail: JobDetail | null; events: RawEvent[]; timeline: Timeline }) {
  if (!detail) return <TabSkeleton />
  const brief = parseBrief(detail.prompt)
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6 sm:px-6">
      <Card className="gap-4 py-5">
        <CardHeader className="px-5">
          <CardTitle className="text-sm">Thông tin tác vụ</CardTitle>
        </CardHeader>
        <CardContent className="px-5">
          <dl className="flex flex-col gap-2.5">
            <DetailRow label="Mã tác vụ" value={detail.id} mono />
            <DetailRow label="Tác vụ gốc" value={detail.rootTaskId} mono />
            <DetailRow label="Lượt" value={detail.turn ?? 1} />
            <DetailRow
              label="Mã phiên AGY"
              value={detail.sessionMismatch ? "Không khớp với phiên đã tiếp tục" : detail.sessionId || "Chưa ghi nhận"}
              mono
            />
            <DetailRow label="Báo cáo đã lưu" value={detail.reportPath} mono />
            <DetailRow label="Trạng thái" value={<StatusPill state={detail.state} />} />
            <DetailRow label="Chế độ" value={modeLabel(detail.mode)} />
            <DetailRow label="Mô hình" value={detail.model} mono />
            <DetailRow label="Mức nỗ lực" value={detail.effort} />
            <DetailRow label="Bắt đầu" value={formatTime(detail.startedAt)} />
            <DetailRow label="Thư mục" value={detail.workspace} mono />
            <DetailRow label="Sự kiện" value={`${events.length} đã nhận / ${detail.eventCount ?? "?"} trên máy chủ`} />
            {(detail.finishStatus || detail.stopReason) && (
              <DetailRow label="Kết thúc" value={[detail.finishStatus, detail.stopReason].filter(Boolean).join(" · ")} mono />
            )}
          </dl>
        </CardContent>
      </Card>

      <Card className="gap-3 py-5">
        <CardHeader className="px-5">
          <CardTitle className="text-sm">Yêu cầu</CardTitle>
          {brief.hasPolicy && <CardDescription className="text-xs">Đã ẩn phần chính sách thực thi được chèn tự động.</CardDescription>}
        </CardHeader>
        <CardContent className="flex flex-col gap-3 px-5">
          <p className="text-sm whitespace-pre-wrap">{brief.objective || "—"}</p>
          {brief.full && (
            <Collapsible>
              <CollapsibleTrigger asChild>
                <Button variant="outline" size="xs" className="group/brief">
                  <ChevronRightIcon aria-hidden="true" className="transition-transform group-data-[state=open]/brief:rotate-90" />
                  Toàn bộ yêu cầu gốc
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <pre className="mt-2 max-h-80 overflow-auto rounded-md border bg-muted/50 p-3 font-mono text-xs whitespace-pre-wrap">{brief.full}</pre>
              </CollapsibleContent>
            </Collapsible>
          )}
        </CardContent>
      </Card>

      <Card className="gap-3 py-5">
        <CardHeader className="px-5">
          <CardTitle className="text-sm">Gỡ lỗi</CardTitle>
          <CardDescription className="text-xs">
            {timeline.debug.length} sự kiện siêu dữ liệu hoặc chưa nhận diện, không hiển thị trong dòng hoạt động.
          </CardDescription>
          <CardAction>
            <Button
              size="sm"
              variant="ghost"
              disabled={events.length === 0}
              onClick={() =>
                downloadText(`${detail.id}-events.jsonl`, events.map((e) => JSON.stringify(e.event)).join("\n") + "\n", "application/x-ndjson")
              }
            >
              <DownloadIcon aria-hidden="true" />
              Tải JSONL
            </Button>
          </CardAction>
        </CardHeader>
        {timeline.debug.length > 0 && (
          <CardContent className="px-5">
            <Collapsible>
              <CollapsibleTrigger asChild>
                <Button variant="outline" size="xs" className="group/debug">
                  <ChevronRightIcon aria-hidden="true" className="transition-transform group-data-[state=open]/debug:rotate-90" />
                  Xem sự kiện thô
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <ul className="mt-2 flex max-h-[28rem] flex-col gap-1 overflow-auto">
                  {timeline.debug.map((e) => (
                    <li key={e.index}>
                      <details className="rounded-md border bg-muted/30 px-2.5 py-1.5">
                        <summary className="cursor-pointer font-mono text-xs text-muted-foreground">
                          #{e.index + 1} · {eventLabel(e.event)}
                        </summary>
                        <pre className="mt-1.5 overflow-auto font-mono text-[11px] whitespace-pre-wrap">{JSON.stringify(e.event, null, 2)}</pre>
                      </details>
                    </li>
                  ))}
                </ul>
              </CollapsibleContent>
            </Collapsible>
          </CardContent>
        )}
      </Card>
    </div>
  )
}

function eventLabel(event: unknown): string {
  if (typeof event !== "object" || event === null) return typeof event
  const e = event as Record<string, unknown>
  const data = e.data as Record<string, unknown> | undefined
  const update = (e.update ?? (data && data.update)) as Record<string, unknown> | undefined
  return String(update?.sessionUpdate ?? e.type ?? e.method ?? "event")
}

function TabSkeleton() {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-3 px-4 py-6 sm:px-6" aria-busy="true" aria-label="Đang tải">
      <Skeleton className="h-4 w-40" />
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-4 w-2/3" />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Steering composer
// ---------------------------------------------------------------------------

/**
 * Continues the same AGY session with a new message. A running turn is
 * stopped first; the server resumes the exact session id (never a new one)
 * and we navigate to the new turn. No optimistic echo: the message appears
 * once the new turn's record shows it.
 */
function SteerComposer({
  jobId,
  active,
  superseded,
  sessionKnown,
  onSteered,
}: {
  jobId: string
  active: boolean
  superseded: boolean
  sessionKnown: boolean
  onSteered: (parentId: string, childId: string) => void
}) {
  const [text, setText] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState<string | null>(null)
  // The requestId is bound to the exact text it was minted for: retrying the same
  // text dedupes server-side; editing the text after a failure gets a fresh id.
  const pending = React.useRef<{ message: string; requestId: string } | null>(null)
  const composing = React.useRef(false)
  const sending = React.useRef(false)
  const alive = React.useRef(true)
  React.useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])
  const inputId = React.useId()
  const helpId = React.useId()
  const errorId = React.useId()
  const trimmed = text.trim()
  const tooLong = trimmed.length > MAX_STEER_MESSAGE
  const canSend = !busy && trimmed.length > 0 && !tooLong

  const submit = async () => {
    if (!canSend || sending.current) return
    sending.current = true
    const message = trimmed
    if (!pending.current || pending.current.message !== message) pending.current = { message, requestId: newRequestId() }
    const { requestId } = pending.current
    setBusy(true)
    setError(null)
    try {
      const receipt = await continueJob(jobId, message, requestId)
      if (!alive.current) return // user switched jobs meanwhile: no state updates, no navigation
      pending.current = null
      setText("")
      setBusy(false)
      onSteered(jobId, receipt.jobId)
    } catch (e) {
      if (!alive.current) return
      setError(e instanceof Error ? e.message : String(e)) // keep the text for retry
      setBusy(false)
    } finally {
      sending.current = false
    }
  }

  const describedBy = [helpId, error ? errorId : null].filter(Boolean).join(" ")

  // Stay mounted while our own request is in flight: polling may observe
  // `supersededBy` before the response arrives, and we still must navigate.
  if (superseded && !busy) return null

  return (
    <form
      aria-label="Tiếp tục cùng phiên AGY"
      className="border-t bg-background px-3 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:px-5"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        {error && (
          <Alert variant="destructive" className="py-2">
            <TriangleAlertIcon />
            <AlertTitle>Chưa tiếp tục được phiên</AlertTitle>
            <AlertDescription id={errorId}>
              <p className="whitespace-pre-wrap">{error}</p>
              <p>Tin nhắn vẫn được giữ nguyên, bạn có thể bấm Gửi để thử lại. Không có phiên mới nào được mở.</p>
            </AlertDescription>
          </Alert>
        )}
        <label htmlFor={inputId} className="sr-only">
          Tin nhắn tiếp tục cùng phiên AGY
        </label>
        <InputGroup
          className={cn(
            "bg-card/60 shadow-xs",
            (tooLong || error) && "border-destructive/60"
          )}
        >
          <InputGroupTextarea
            id={inputId}
            value={text}
            rows={1}
            maxLength={MAX_STEER_MESSAGE}
            readOnly={busy}
            aria-busy={busy || undefined}
            aria-invalid={tooLong || undefined}
            aria-describedby={describedBy}
            placeholder="Nhắn tiếp cho AGY trong cùng phiên…"
            enterKeyHint="send"
            className="max-h-40 min-h-10 flex-1 resize-none overflow-y-auto border-0 bg-transparent px-2 py-2 shadow-none focus-visible:ring-0 dark:bg-transparent"
            onChange={(e) => setText(e.target.value)}
            onCompositionStart={() => {
              composing.current = true
            }}
            onCompositionEnd={() => {
              composing.current = false
            }}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || e.shiftKey) return
              // IME: Enter confirms a composition; never send it (Safari reports keyCode 229).
              if (composing.current || e.nativeEvent.isComposing || e.keyCode === 229) return
              e.preventDefault()
              void submit()
            }}
          />
          <InputGroupAddon align="block-end" className="justify-end pt-0">
          <InputGroupButton type="submit" size="icon-sm" variant="default" disabled={!canSend} aria-label={busy ? "Đang gửi tin nhắn tiếp tục phiên" : "Gửi tin nhắn tiếp tục phiên"}>
            {busy ? <LoaderCircleIcon aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <SendIcon aria-hidden="true" />}
          </InputGroupButton>
          </InputGroupAddon>
        </InputGroup>
        <div className="flex items-start justify-between gap-3">
          <p id={helpId} className="text-xs text-muted-foreground">
            {active
              ? "Gửi tin nhắn sẽ dừng lượt hiện tại rồi tiếp tục cùng phiên AGY với tin nhắn của bạn."
              : "Gửi tin nhắn sẽ tiếp tục cùng phiên AGY; AGY giữ nguyên ngữ cảnh của các lượt trước."}{" "}
            {!sessionKnown &&
              "Chưa ghi nhận mã phiên cho lượt này; nếu không tìm được phiên, hệ thống sẽ báo lỗi thay vì mở phiên mới. "}
            Các thay đổi tệp đã có được giữ nguyên. Enter để gửi, Shift+Enter để xuống dòng.
          </p>
          {trimmed.length > MAX_STEER_MESSAGE - 500 && (
            <span className={cn("shrink-0 text-xs tabular-nums", tooLong ? "text-destructive" : "text-muted-foreground")}>
              {trimmed.length.toLocaleString("vi-VN")}/{MAX_STEER_MESSAGE.toLocaleString("vi-VN")}
            </span>
          )}
        </div>
        <p role="status" aria-live="polite" className={cn("text-xs text-muted-foreground", !busy && "sr-only")}>
          {busy ? (active ? "Đang dừng lượt hiện tại và tiếp tục cùng phiên AGY…" : "Đang tiếp tục cùng phiên AGY…") : ""}
        </p>
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// Main job panel
// ---------------------------------------------------------------------------

function JobPanel({
  jobId,
  autoRefresh,
  onOpenSidebar,
  onSelect,
  onSteered,
}: {
  jobId: string
  autoRefresh: boolean
  onOpenSidebar: () => void
  onSelect: (id: string) => void
  onSteered: (parentId: string, childId: string) => void
}) {
  const { detail, events, timeline, error, loading, retry } = useJob(jobId, autoRefresh)
  const { history } = useHistory(jobId, `${detail?.state ?? ""}|${detail?.supersededBy ?? ""}|${detail?.done ?? ""}`)
  const [tab, setTab] = React.useState("activity")
  const [briefOpen, setBriefOpen] = React.useState(false)
  const [stopping, setStopping] = React.useState(false)
  const [stopError, setStopError] = React.useState<string | null>(null)
  const alive = React.useRef(true)
  React.useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])
  const { copy } = useCopy()
  const active = isActiveState(detail?.state)
  const now = useNow(active)
  const brief = parseBrief(detail?.prompt)
  const supersededBy = detail?.supersededBy ?? null

  React.useEffect(() => setTab("activity"), [jobId])

  const stop = async () => {
    if (stopping) return
    setStopping(true)
    setStopError(null)
    try {
      await stopJob(jobId) // resolves once the server confirmed exit (or reports why not)
    } catch (e) {
      if (alive.current) setStopError(e instanceof Error ? e.message : String(e))
    } finally {
      if (alive.current) {
        setStopping(false)
        retry() // re-poll even if auto-refresh is off or polling already ended
      }
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-center gap-2 border-b px-3 py-2.5 sm:px-5">
        <div className="md:hidden">
          <IconButton label="Mở danh sách tác vụ" onClick={onOpenSidebar}>
            <MenuIcon />
          </IconButton>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            {detail ? <h1 className="truncate text-sm font-semibold sm:text-base">{jobTitle(detail)}</h1> : <Skeleton className="h-4 w-32" />}
            {detail && <StatusPill state={detail.state} />}
          </div>
          <p className="truncate text-xs text-muted-foreground">
            {detail ? (
              <>
                <span title={detail.workspace ?? undefined}>{detail.workspace ?? "—"}</span>
                {" · "}
                {modeLabel(detail.mode)}
                {` · Lượt ${detail.turn ?? 1}`}
                {" · "}
                <span className="font-mono">{detail.model ?? "—"}</span>
                {detail.effort ? ` / ${detail.effort}` : ""}
                {active && ` · ${formatElapsed(detail.startedAt, now)}`}
              </>
            ) : (
              "Đang tải…"
            )}
          </p>
        </div>
        {active && !supersededBy && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void stop()}
            disabled={stopping}
            aria-busy={stopping || undefined}
            aria-label={stopping ? "Đang dừng tác vụ" : "Dừng tác vụ"}
            className="text-destructive hover:text-destructive"
          >
            {stopping ? <LoaderCircleIcon aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <SquareIcon aria-hidden="true" />}
            <span className="hidden sm:inline">{stopping ? "Đang dừng…" : "Dừng"}</span>
          </Button>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label="Thao tác khác">
              <MoreHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => copy("id", jobId)}>
              <CopyIcon aria-hidden="true" />
              Sao chép mã tác vụ
            </DropdownMenuItem>
            <DropdownMenuItem disabled={!detail?.report} onSelect={() => detail?.report && copy("report", detail.report)}>
              <CopyIcon aria-hidden="true" />
              Sao chép kết quả
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!detail?.report}
              onSelect={() => detail?.report && downloadText(`${jobId}-report.md`, detail.report)}
            >
              <DownloadIcon aria-hidden="true" />
              Tải kết quả (.md)
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={!brief.objective} onSelect={() => setBriefOpen(true)}>
              <FileCode2Icon aria-hidden="true" />
              Xem yêu cầu
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </header>

      {error && (
        <div className="border-b px-3 py-2 sm:px-5">
          <Alert variant="destructive" className="py-2">
            <TriangleAlertIcon />
            <AlertTitle>Mất kết nối với AGY Live</AlertTitle>
            <AlertDescription className="flex flex-row flex-wrap items-center gap-2">
              <span>{error}. Dữ liệu hiển thị có thể chưa mới nhất.</span>
              <Button size="xs" variant="outline" onClick={retry}>
                <RefreshCwIcon aria-hidden="true" />
                Thử lại
              </Button>
            </AlertDescription>
          </Alert>
        </div>
      )}

      {stopError && (
        <div className="border-b px-3 py-2 sm:px-5">
          <Alert variant="destructive" className="py-2">
            <TriangleAlertIcon />
            <AlertTitle>Chưa dừng được tác vụ</AlertTitle>
            <AlertDescription className="flex flex-row flex-wrap items-center gap-2">
              <span className="whitespace-pre-wrap">{stopError}</span>
              {active && (
                <Button size="xs" variant="outline" onClick={() => void stop()} disabled={stopping}>
                  <RefreshCwIcon aria-hidden="true" />
                  Thử dừng lại
                </Button>
              )}
            </AlertDescription>
          </Alert>
        </div>
      )}

      {supersededBy && (
        <div className="border-b px-3 py-2 sm:px-5">
          <Alert className="py-2">
            <ArrowRightIcon />
            <AlertTitle>Phiên AGY đã được tiếp tục ở lượt sau</AlertTitle>
            <AlertDescription className="flex flex-row flex-wrap items-center gap-2">
              <span>Lượt này đã kết thúc; cuộc trò chuyện tiếp tục trong cùng phiên ở lượt mới hơn.</span>
              <Button size="xs" variant="outline" onClick={() => onSelect(history?.latestJobId || supersededBy)}>
                Mở lượt mới nhất
              </Button>
            </AlertDescription>
          </Alert>
        </div>
      )}

      <Tabs value={tab} onValueChange={setTab} className="min-h-0 flex-1 gap-0">
        <div className="border-b px-3 sm:px-5">
          <TabsList variant="line" className="h-10 gap-3">
            <TabsTrigger value="activity" className="flex-none px-1">Hoạt động</TabsTrigger>
            <TabsTrigger value="result" className="flex-none px-1">Kết quả</TabsTrigger>
            <TabsTrigger value="files" className="flex-none px-1">
              Tệp đã sửa
              {timeline.changedFiles.length > 0 && (
                <span className="rounded-full bg-accent px-1.5 text-[10px] text-accent-foreground">{timeline.changedFiles.length}</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="details" className="flex-none px-1">Chi tiết</TabsTrigger>
          </TabsList>
        </div>
        <TabsContent value="activity" className="min-h-0">
          <ActivityView detail={detail} timeline={timeline} loading={loading && !detail} history={history} />
        </TabsContent>
        <TabsContent value="result" className="min-h-0 overflow-y-auto">
          <ResultView detail={detail} />
        </TabsContent>
        <TabsContent value="files" className="min-h-0 overflow-y-auto">
          <FilesView detail={detail} files={timeline.changedFiles} />
        </TabsContent>
        <TabsContent value="details" className="min-h-0 overflow-y-auto">
          <DetailsView detail={detail} events={events} timeline={timeline} />
        </TabsContent>
      </Tabs>

      {detail && (
        <SteerComposer
          jobId={jobId}
          active={active}
          superseded={!!supersededBy}
          sessionKnown={!!(detail.sessionId || detail.resumeSessionId)}
          onSteered={onSteered}
        />
      )}

      <Dialog open={briefOpen} onOpenChange={setBriefOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Yêu cầu gửi cho AGY</DialogTitle>
            <DialogDescription>
              {brief.hasPolicy ? "Phần chính sách thực thi tự động đã được ẩn; xem bản gốc ở tab Chi tiết." : "Nội dung yêu cầu gốc."}
            </DialogDescription>
          </DialogHeader>
          <ScrollArea className="max-h-[60vh]">
            <p className="pr-3 text-sm whitespace-pre-wrap">{brief.objective}</p>
          </ScrollArea>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const { theme, toggle } = useTheme()
  const initial = React.useMemo(readParams, [])
  const [demo, setDemo] = React.useState(initial.demo)
  const [selected, setSelected] = React.useState<string | null>(initial.job)
  const [autoRefresh, setAutoRefresh] = React.useState(true)
  const [sheetOpen, setSheetOpen] = React.useState(false)
  const { jobs, error: jobsError, loading: jobsLoading, refresh } = useJobs(autoRefresh && !demo)
  // A just-launched successor may not be in the (possibly stale) list yet; keep it selected until it is.
  const [pinned, setPinned] = React.useState<string | null>(null)
  const selectedRef = React.useRef(selected)
  React.useEffect(() => {
    selectedRef.current = selected
  }, [selected])

  // Auto-select the newest job when nothing (or a vanished job) is selected.
  React.useEffect(() => {
    if (demo || !jobs) return
    if (pinned && jobs.some((j) => j.id === pinned)) setPinned(null)
    if (jobs.length === 0) return
    if (!selected || (selected !== pinned && !jobs.some((j) => j.id === selected))) setSelected(jobs[0].id)
  }, [jobs, selected, demo, pinned])

  React.useEffect(() => writeParams({ job: selected, demo }), [selected, demo])

  const select = (id: string) => {
    setPinned(null)
    setSelected(id)
    setSheetOpen(false)
  }
  // Follow a steered run to its successor only if the user is still on the parent.
  const steered = (parentId: string, childId: string) => {
    if (selectedRef.current === parentId) {
      selectedRef.current = childId
      setPinned(childId)
      setSelected(childId)
    }
    refresh()
  }
  const openDemo = () => {
    setSheetOpen(false)
    setDemo(true)
  }

  const sidebar = (
    <Sidebar
      jobs={jobs}
      jobsError={jobsError}
      jobsLoading={jobsLoading}
      selected={selected}
      onSelect={select}
      onRefresh={refresh}
      onDemo={openDemo}
      autoRefresh={autoRefresh}
      onAutoRefresh={setAutoRefresh}
      theme={theme}
      onToggleTheme={toggle}
    />
  )

  let main: React.ReactNode
  if (demo) {
    main = <DemoView onExit={() => setDemo(false)} />
  } else if (selected && (!jobs || selected === pinned || jobs.some((j) => j.id === selected))) {
    main = (
      <JobPanel
        key={selected}
        jobId={selected}
        autoRefresh={autoRefresh}
        onOpenSidebar={() => setSheetOpen(true)}
        onSelect={select}
        onSteered={steered}
      />
    )
  } else {
    main = (
      <div className="flex h-full flex-col">
        <div className="flex items-center border-b px-3 py-2.5 md:hidden">
          <IconButton label="Mở danh sách tác vụ" onClick={() => setSheetOpen(true)}>
            <MenuIcon />
          </IconButton>
          <span className="ml-2 text-sm font-semibold">AGY Live</span>
        </div>
        {jobs === null && !jobsError ? (
          <TabSkeleton />
        ) : (
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <InboxIcon />
              </EmptyMedia>
              <EmptyTitle>{jobsError ? "Không kết nối được AGY Live" : "Chưa có tác vụ AGY nào"}</EmptyTitle>
              <EmptyDescription>
                {jobsError
                  ? `Máy chủ cục bộ không phản hồi (${jobsError}).`
                  : "Tác vụ do Codex khởi chạy sẽ xuất hiện tại đây; bạn có thể dừng lượt đang chạy hoặc tiếp tục cùng phiên AGY từ bảng điều khiển."}
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent className="flex-row justify-center">
              <Button size="sm" onClick={refresh}>
                <RefreshCwIcon aria-hidden="true" />
                Làm mới
              </Button>
              <Button size="sm" variant="outline" onClick={openDemo}>
                <FlaskConicalIcon aria-hidden="true" />
                Xem demo
              </Button>
            </EmptyContent>
          </Empty>
        )}
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-dvh overflow-hidden bg-background text-foreground">
        {!demo && <aside className="hidden w-64 shrink-0 border-r bg-card/40 md:block lg:w-72">{sidebar}</aside>}
        <main className="min-w-0 flex-1">{main}</main>
      </div>
      <Sheet open={sheetOpen} onOpenChange={setSheetOpen}>
        <SheetContent side="left" className="w-72 gap-0 p-0" showCloseButton={false}>
          <SheetHeader className="sr-only">
            <SheetTitle>Danh sách tác vụ</SheetTitle>
            <SheetDescription>Chọn một tác vụ AGY để theo dõi</SheetDescription>
          </SheetHeader>
          {sidebar}
        </SheetContent>
      </Sheet>
    </TooltipProvider>
  )
}
