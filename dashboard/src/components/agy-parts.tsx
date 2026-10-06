// Small shared view pieces used by both the live dashboard and the offline demo.
import * as React from "react"
import Markdown from "react-markdown"
import { CheckIcon, CircleAlertIcon, FileTextIcon, LoaderCircleIcon, PencilIcon, SearchIcon, TerminalIcon, WrenchIcon } from "lucide-react"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { cn } from "@/lib/utils"
import { describeTool, stateInfo, type JobState, type TimelineItem } from "@/lib/events"

export function AgentAvatar() {
  return (
    <Avatar size="sm" className="mt-0.5 ring-1 ring-border">
      <AvatarFallback className="bg-accent text-[11px] font-semibold text-accent-foreground">A</AvatarFallback>
    </Avatar>
  )
}

/** Safe Markdown: react-markdown never renders raw HTML by default. */
export function MarkdownText({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("prose-agy text-sm", className)}>
      <Markdown
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
        }}
      >
        {text}
      </Markdown>
    </div>
  )
}

type ToolItem = Extract<TimelineItem, { type: "tool" }>

function toolIcon(item: ToolItem) {
  if (item.status === "failed") return <CircleAlertIcon className="text-destructive" />
  if (item.status !== "completed") return <LoaderCircleIcon className="animate-spin text-primary motion-reduce:animate-none" />
  switch (item.kind) {
    case "read":
      return <FileTextIcon />
    case "edit":
      return <PencilIcon className="text-primary" />
    case "search":
      return <SearchIcon />
    case "execute":
      return <TerminalIcon />
    default:
      return item.kind === "other" ? <WrenchIcon /> : <CheckIcon />
  }
}

/** AI activity Marker. role=status only while active so screen readers announce progress. */
export function ToolMarker({ item }: { item: ToolItem }) {
  const active = item.status === "pending" || item.status === "in_progress"
  return (
    <Marker
      role={active ? "status" : undefined}
      className={cn("py-0.5", item.status === "failed" && "text-destructive")}
      title={item.paths.join("\n") || item.title}
    >
      <MarkerIcon>{toolIcon(item)}</MarkerIcon>
      <MarkerContent>{describeTool(item)}</MarkerContent>
    </Marker>
  )
}

export function StatusPill({ state }: { state?: JobState }) {
  const { label, tone } = stateInfo(state)
  return (
    <Badge
      variant="outline"
      className={cn(
        "gap-1.5 rounded-full px-2 py-0.5 font-medium",
        tone === "active" && "border-primary/30 text-primary",
        tone === "success" && "text-foreground",
        tone === "error" && "border-destructive/40 text-destructive"
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "size-1.5 rounded-full",
          tone === "active" && "agy-pulse bg-primary",
          tone === "success" && "bg-success",
          tone === "error" && "bg-destructive",
          tone === "neutral" && "bg-muted-foreground"
        )}
      />
      {label}
    </Badge>
  )
}

/** Clipboard copy with transient per-key feedback. */
export function useCopy() {
  const [copied, setCopied] = React.useState<string | null>(null)
  const [failed, setFailed] = React.useState<string | null>(null)
  const timer = React.useRef<number | undefined>(undefined)
  React.useEffect(() => () => window.clearTimeout(timer.current), [])
  const copy = React.useCallback(async (key: string, text: string) => {
    window.clearTimeout(timer.current)
    try {
      await navigator.clipboard.writeText(text)
      setFailed(null)
      setCopied(key)
    } catch {
      setCopied(null)
      setFailed(key)
    }
    timer.current = window.setTimeout(() => {
      setCopied(null)
      setFailed(null)
    }, 1800)
  }, [])
  return { copied, failed, copy }
}

export function downloadText(filename: string, text: string, type = "text/markdown;charset=utf-8") {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const link = document.createElement("a")
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}
