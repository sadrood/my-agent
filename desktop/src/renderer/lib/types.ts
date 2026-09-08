/**
 * 与后端 dashboard hub 对齐的事件/类型模型。
 */

/** Agent 事件类型（对齐 dashboard/hub.py 的事件流） */
export type AgentEventType =
  | 'run_start'
  | 'run_loop_start'
  | 'turn_start'
  | 'skills_matched'
  | 'plan'
  | 'step_start'
  | 'model_turn'
  | 'thinking'
  | 'tool_call'
  | 'tool_result'
  | 'tool_output'
  | 'approval'
  | 'approval_resolved'
  | 'approval_ack'
  | 'guardian'
  | 'answer'
  | 'run_end'
  | 'error'
  | 'compaction'
  | 'checkpoint'
  | 'llm_error'
  | 'metrics'
  | 'llm_retry';

export interface AgentEvent {
  type: AgentEventType;
  data: Record<string, unknown>;
  timestamp: number;
}

/** 权限模式 */
export type PermissionMode = 'auto' | 'ask' | 'block';

/** 模型参数 */
export interface ModelParams {
  temperature: number;
  topP: number;
  maxTokens: number;
  /** 任务最大操作轮数（0 = 后端默认，如 .env MAX_LOOP_OPS=80） */
  maxOps: number;
}

/** 会话 */
export interface Session {
  id: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  pinned: boolean;
  model?: string;
  /** 运行时引擎：myagent（内置）/ claude / codex / custom */
  runtime?: string;
  messageCount: number;
  /** 最后一条消息的内容片段（会话卡片预览，后端 /api/sessions 提供） */
  preview?: string;
  /** 所属项目 id（项目=一个工作目录；空=早期/未归属会话） */
  projectId?: string;
  /** 所属工作目录（旧字段，兼容早期会话；新数据用 projectId） */
  workspace?: string;
}

/** 项目：一个工作目录 + 其下的会话集合 */
export interface Project {
  id: string;
  name: string;
  path: string;
}

/** 对话消息（渲染用） */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  /** 思考/推理内容（思考模型流式产出，可折叠展示，默认展开直到正文出现） */
  reasoning?: string;
  kind?: 'text' | 'diff' | 'command' | 'steps' | 'approval' | 'stats' | 'todo';
  meta?: Record<string, unknown>;
  timestamp: number;
}
