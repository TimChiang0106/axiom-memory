"""Copy and limits shared by the /app pages."""

WEEKDAYS = "一二三四五六日"
TRIGGER_LABELS = {
    "unrecorded_commitment": "對話中提到但沒記下的承諾",
    "due_soon": "截止日將近",
    "overview_stale": "專案總覽落後",
}
CATEGORY_LABELS = {
    "you": "關於你",
    "people": "人",
    "areas": "專案與領域",
    "topics": "主題",
}
TYPE_LABELS = {
    "preference": "偏好",
    "fact": "事實",
    "state": "現況",
    "procedural": "做法",
    "reference": "參考",
    "project": "專案",
}
PROFILE = "user-profile"
# source_app values as the client reported them → what the user calls them.
APP_LABELS = {
    "openai-mcp": "ChatGPT",
    "claude-user": "Claude",
    "claude-code": "Claude Code",
    "go-http-client": "程式（API）",
    "curl": "程式（curl）",
    "unknown": "不明",
}
# Query params that carry a one-shot notice and must not follow the user on.
NOTICE_PARAMS = ("error", "undo", "forgot")
STATUS_LABELS = {
    "open": "未完成",
    "done": "已完成",
    "dropped": "已刪除",
    "suggested": "AI 建議",
    "expired": "已失效",
}
OPEN_WARN = 5
# Path segment standing for "no project" in /app/projects/<name>.
NO_PROJECT = "-"
