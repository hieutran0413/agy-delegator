// Offline, deterministic preview of the AGY Live timeline. It uses the shadcn
// createChat helper as a scripted transport only — it never talks to the
// backend and is never mixed with real jobs.
import { useChat } from "@ai-sdk/react"
import { createChat } from "@shadcn/helpers/ai-sdk"
import { ArrowDownIcon, FlaskConicalIcon, LogOutIcon, RotateCcwIcon, SendHorizontalIcon } from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import { Message, MessageContent, MessageHeader } from "@/components/ui/message"
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller"
import { AgentAvatar, MarkdownText, ToolMarker } from "@/components/agy-parts"
import type { TimelineItem, ToolStatus } from "@/lib/events"

const demoChat = createChat()
  .user("Thêm nút sao chép vào thẻ kết quả trong src/App.tsx và báo lại các tệp đã sửa.")
  .assistant(({ writer }) => {
    writer.text("Mình sẽ đọc tệp hiện tại trước khi sửa.")
    writer.tool("readFile", { input: { path: "src/App.tsx" } }).sleep(700).output({ lines: 142 })
    writer.tool("editFile", { input: { path: "src/App.tsx" } }).sleep(900).output({ ok: true })
    writer.text(
      "Đã xong.\n\n**Kết quả**\n- Thêm nút *Sao chép* vào thẻ kết quả.\n- Tệp đã sửa: `src/App.tsx`\n\nGợi ý kiểm tra: `npm run build`"
    )
  })

const demoTransport = demoChat.transport({ delayMs: 30 })

type AnyPart = { type: string; [key: string]: unknown }

function partToTool(part: AnyPart, fallbackId: string): Extract<TimelineItem, { type: "tool" }> {
  const name = part.type === "dynamic-tool" ? String(part.toolName ?? "") : part.type.slice("tool-".length)
  const input = (part.input ?? {}) as { path?: string }
  const state = String(part.state ?? "")
  const status: ToolStatus =
    state === "output-available" ? "completed" : state === "output-error" ? "failed" : "in_progress"
  return {
    type: "tool",
    id: String(part.toolCallId ?? fallbackId),
    toolCallId: String(part.toolCallId ?? fallbackId),
    kind: /read/i.test(name) ? "read" : /edit|write/i.test(name) ? "edit" : "other",
    title: name,
    status,
    paths: input.path ? [input.path] : [],
  }
}

export function DemoView({ onExit }: { onExit: () => void }) {
  const { messages, sendMessage, setMessages, status, stop } = useChat({
    messages: demoChat.get(0),
    transport: demoTransport,
  })
  const next = demoChat.next(messages)
  const busy = status === "submitted" || status === "streaming"

  const replay = () => {
    stop()
    setMessages(demoChat.get(0))
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-3 border-b px-4 py-3 sm:px-6">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h1 className="truncate text-base font-semibold">Bản demo AGY Live</h1>
            <Badge variant="secondary" className="gap-1">
              <FlaskConicalIcon aria-hidden="true" />
              Ngoại tuyến
            </Badge>
          </div>
          <p className="truncate text-xs text-muted-foreground">
            Mô hình: demo-model (minh hoạ) · Mức nỗ lực: minh hoạ · Không kết nối máy chủ
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => next && sendMessage(next)} disabled={!next || busy}>
            <SendHorizontalIcon aria-hidden="true" />
            Gửi bước tiếp
          </Button>
          <Button size="sm" variant="outline" onClick={replay} disabled={messages.length === 0}>
            <RotateCcwIcon aria-hidden="true" />
            Phát lại
          </Button>
          <Button size="sm" variant="ghost" onClick={onExit}>
            <LogOutIcon aria-hidden="true" />
            Thoát demo
          </Button>
        </div>
      </header>

      <div className="mx-auto w-full max-w-3xl px-4 pt-4">
        <Alert>
          <FlaskConicalIcon />
          <AlertTitle>Đây là dữ liệu minh hoạ</AlertTitle>
          <AlertDescription>
            Kịch bản cố định chạy hoàn toàn trên trình duyệt để xem trước giao diện. Không có tác vụ thật nào được tạo hay hiển thị ở đây.
          </AlertDescription>
        </Alert>
      </div>

      <MessageScrollerProvider autoScroll defaultScrollPosition="end">
        <MessageScroller className="min-h-0 flex-1">
          <MessageScrollerViewport aria-label="Dòng hoạt động demo">
            <MessageScrollerContent className="mx-auto w-full max-w-3xl gap-4 px-4 py-6">
              {messages.length === 0 && (
                <MessageScrollerItem>
                  <p className="py-10 text-center text-sm text-muted-foreground">
                    Nhấn “Gửi bước tiếp” để phát kịch bản demo.
                  </p>
                </MessageScrollerItem>
              )}
              {messages.map((message) =>
                message.role === "user" ? (
                  <MessageScrollerItem key={message.id} messageId={message.id} scrollAnchor>
                    <Message align="end">
                      <MessageContent>
                        <MessageHeader>Yêu cầu (demo)</MessageHeader>
                        <Bubble variant="tinted" align="end">
                          <BubbleContent className="whitespace-pre-wrap">
                            {message.parts.map((p) => (p.type === "text" ? p.text : "")).join("")}
                          </BubbleContent>
                        </Bubble>
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                ) : (
                  <MessageScrollerItem key={message.id} messageId={message.id}>
                    <Message>
                      <AgentAvatar />
                      <MessageContent className="gap-2">
                        <MessageHeader className="px-0">AGY (demo)</MessageHeader>
                        {(message.parts as AnyPart[]).map((part, i) => {
                          if (part.type === "text") {
                            return (
                              <Bubble key={i} variant="ghost">
                                <BubbleContent>
                                  <MarkdownText text={String(part.text ?? "")} />
                                </BubbleContent>
                              </Bubble>
                            )
                          }
                          if (part.type.startsWith("tool-") || part.type === "dynamic-tool") {
                            return <ToolMarker key={i} item={partToTool(part, `${message.id}-${i}`)} />
                          }
                          return null
                        })}
                      </MessageContent>
                    </Message>
                  </MessageScrollerItem>
                )
              )}
              {status === "submitted" && (
                <MessageScrollerItem>
                  <p role="status" className="text-sm text-muted-foreground">
                    Đang chờ phản hồi (demo)…
                  </p>
                </MessageScrollerItem>
              )}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton direction="end" aria-label="Cuộn đến mới nhất">
            <ArrowDownIcon aria-hidden="true" />
          </MessageScrollerButton>
        </MessageScroller>
      </MessageScrollerProvider>
    </div>
  )
}
