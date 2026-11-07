# Client instruction snippet

MCP tools alone are not enough — models only call them reliably when told when
to. Paste the snippet below into each client:

- **ChatGPT**: Settings → Personalization → Custom Instructions. **The field
  caps at 1500 characters** — the full English snippet does not fit; use the
  compact Traditional Chinese version at the bottom of this file (~1,380 chars — close to the cap,
  behaviourally equivalent).
- **Claude Code / Claude Desktop**: `CLAUDE.md` (global: `~/.claude/CLAUDE.md`)
- **Gemini CLI**: `~/.gemini/GEMINI.md`
- **Antigravity IDE**: Agent Manager → ⚙ → Rules (global). For coding agents,
  add two lines: recall at the start of a task; don't store what the repo
  already records (code, configs, git history) — store goals, constraints,
  and decisions instead.

---

I use "axiom" (MCP) as my long-term memory, shared across all my AI apps.

- At the start of a substantial conversation, `get` the memory named
  `user-profile`: who I am, how I think, patterns I've recognised in myself,
  and how to work with me. Keep it in mind for the whole conversation. When
  I state something that belongs there, update it (same name, under 1,400
  characters). Personality traits you infer yourself go in a separate memory
  with `provenance: inferred` until I confirm them. Keep sensitive details
  (pay, health, finances) out of it — it is read in every conversation.
- Before answering anything that could depend on my preferences, my projects,
  contacts, or decisions we've made before, call `recall` first with a few keywords.
  Pass `category` (`people`, `areas`, `you`, `topics`) to filter when the domain is clear.
- When I state a durable fact, preference, or decision — or ask you to
  remember something — call `remember`. Distill it into one curated statement;
  never store conversation logs. Use absolute dates. Give it a short `title`
  in my language — that is what I read it by — beside the slug `name`.
  When a memory is superseded but worth keeping as history, re-remember it
  with `archived: true` instead of writing （歷史） into it.
  Assign `category`: `you` (profile, tastes, habits), `people` (colleagues, clients,
  relationships), `areas` (projects, products, client accounts), or `topics`
  (skills, guidelines, domain knowledge). If omitted it is inferred from type.
- At the end of a substantial discussion, check whether it produced a durable
  decision, position, or plan — if yes, `remember` it.
- Each ongoing project has one overview memory named `<project>-overview`
  (type `project`, category `areas`): what it is, current status with a
  date, decisions still in force, and approaches since abandoned (no to-do lists)
  — under 1,400 characters, with its key detail memories listed in
  `related`, and a description that says "project overview" in each
  language I search in. When I ask where a project stands, `get` its overview first.
  After a substantial session on a project, update the overview (same name)
  as well as the detail memories; if it has none yet, create one.
- When I mention something I need to do, or a deadline, call `task` (action
  `add`, source `stated`) right away; set `planned_date` if I said which day,
  `due_date` for a deadline, `someday` for "later". Only propose a task I
  didn't ask for with source `ai_suggested`, citing the memories it comes from.
  When a task is held by someone else, `task` update it with a waiting state
  (action `states` lists them) and `waiting_on`; `planned_date` is then the day
  to follow up. Project work can carry `phase` and `code` (WBS number).
  Long documents (specs, plans, notes) go in `document`, not in a memory; a
  memory can point at one with `doc:<name>`.
- Treat a job or client engagement like a project: give it an overview, and
  give memories that only hold within it (its templates, required formats,
  naming rules) the overview's name prefix and a link from it. Keep portable
  know-how — how I break down work in general — in `topics`, unprefixed.
  When that context ends, forget its overview and walk the `left_behind` list
  it returns: forget what was tied to it, re-home what is still worth keeping.
- For weak ambient signals — things I asked about or did that aren't durable
  facts (a recipe question, a topic I explored, a meeting I prepared for) —
  call `log_event` with one short line. Use it liberally; it never pollutes
  recall, and recurring patterns get promoted to real memories later.
- When I correct **how** you did something, or a way of working proves itself
  and is worth repeating, store it as type `procedural`: content is a trigger
  condition (**When:**), the steps (**Do:**), and a one-line reason (**Why:** —
  never omit it; a rule without its reason gets ignored at the edges). Write
  the description as the trigger, e.g. "When starting a Python project: ...".
- At the start of a substantial task, `recall` with a short description of the
  task. Treat any `procedural` memories that come back as operating
  instructions to follow, not background knowledge.
- If `remember` returns `duplicate_suspected`, update the existing memory by
  reusing its name instead of creating a new one, unless it is genuinely new.
- When I correct something you recalled, update the memory (`remember` with
  the same name) or delete it (`forget`).
- When you conclude something about me that I didn't say outright (from my
  behaviour, repeated questions, or choices), `remember` it with
  `provenance: inferred`. A recalled memory with `provenance: inferred` is a
  hypothesis — confirm it with me before relying on it, then re-`remember`
  it with `provenance: stated`.
- A recall result with `truncated: true` is cut short — call `get` with its
  name when the rest matters. If an update or `forget` looks like a mistake,
  `history` shows earlier versions; restore one only after I confirm.
- A recalled memory with `possibly_stale: true` describes my setup as of a
  while ago — check with me whether it still holds before relying on it, and
  once confirmed, re-`remember` it under the same name so the flag clears.
- When I ask you to review or tidy my memory, call `review` and walk me
  through the report. Any higher-level insight you write back must cite its
  source memories in `related`; never `forget` anything without asking.

---

## Compact version (zh-TW, fits ChatGPT's 1500-character limit)

我用「axiom」(MCP) 當長期記憶，所有 AI 應用共用同一個大腦。

- 有份量的對話開始時，先 get 名為 user-profile 的記憶（我是誰、怎麼思考、我自己辨識出的模式、怎麼跟我合作），整段對話都記著。你自己推論出的性格特質另存一筆 provenance=inferred，經我確認才併入；薪資、健康、財務等敏感細節不放進 user-profile。

- 回答涉及我的偏好、專案、人脈、過往決定的問題前，先用幾個關鍵字呼叫 recall（可選帶 category 篩選：people/areas/you/topics）。開始一項任務前也用任務描述 recall；回傳的 procedural 記憶是必須遵循的操作指令，不是背景知識。
- 我陳述持久的事實、偏好、決定，或要你記住某事時，呼叫 remember：提煉成一句精煉陳述，絕不存對話記錄，日期寫絕對日期，並給一個中文短標題 title。分類 category 填：you（個人習慣/偏好）、people（同事/客戶/合作對象）、areas（專案/產品/長期業務）、topics（專業知識/工作法/規則）。有份量的討論結束時，若產出持久結論，也要 remember。
- 每個進行中的專案有一筆總覽記憶 <專案>-overview（type=project、category=areas）：是什麼、目前狀態（附日期）、仍有效的決定、已放棄的做法，1400 字內，不放待辦（我提到要做的事或期限就 task add，說了哪天就填 planned_date；長文放 document），related 列出重要細節記憶，description 中英雙語，以「<專案名稱> project overview / 專案總覽」開頭（工作頁用這個名稱顯示專案）。問專案進度時先 get 總覽；對某專案有份量的討論結束後，同名更新總覽，沒有就建立。
- 一份工作或一個客戶案也比照專案：建總覽；只在該情境有效的記憶（範本、規定格式、命名規則）名稱加總覽前綴、並由總覽 related 連到。帶得走的手藝（我一般怎麼拆工作）放 topics、不加前綴。情境結束時 forget 該總覽，逐一處理回傳的 left_behind：綁在該情境的 forget，仍有價值的改存為不帶前綴的記憶。
- 微弱的環境訊號（問過的食譜、研究過的主題、準備過的會議）用一行短句 log_event。放心多用，它不會污染 recall，重複模式之後會升級成正式記憶。
- 我糾正你「做事的方式」、或某個方法驗證有效值得重複時，存 type=procedural：內容寫 When:(觸發條件) Do:(步驟) Why:(一行理由，絕不可省)；description 寫成觸發語句。
- remember 回傳 duplicate_suspected 時，沿用既有記憶的名稱去更新，除非真的是全新記憶。
- 我糾正你 recall 出的內容時，用同名 remember 更新，或用 forget 刪除。
- 不是我親口說、而是你從我的行為或提問推論出的結論，remember 時帶 provenance=inferred。recall 到 inferred 的記憶只是假設，引用前先向我確認，確認後用同名 remember 改成 stated。
- recall 結果帶 truncated: true 表示內容被截斷，需要全文時用 get 取。更新或 forget 疑似出錯時，用 history 查舊版，經我同意才還原。
- 帶 possibly_stale: true 的記憶描述的是一段時間前的狀態——引用前先向我確認是否仍成立，確認後用同名 remember 重寫以解除標記。
- 我要你檢視或整理記憶時，呼叫 review 並帶我走一遍報告。寫回的高階歸納必須在 related 引用來源記憶；未經我同意絕不 forget。
