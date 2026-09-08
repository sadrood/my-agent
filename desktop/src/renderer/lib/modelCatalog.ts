/**
 * 模型目录（常用模型子集的 contextWindow 快照）。
 * 数据来源：本机模型目录快照（2026-06 采集）。
 * 每项为「模型 id → 精确 contextWindow」，供概览窗口水位与设置提示使用，
 * 不再按模型家族粗估。
 */
export const MODEL_WINDOWS: Record<string, number> = {
  // DeepSeek（1M 上下文）
  'deepseek-v4-flash': 1_000_000,
  'deepseek-v4-pro': 1_000_000,
  // Moonshot / Kimi
  'kimi-k3': 1_048_576,
  k3: 1_048_576,
  'k3-256k': 262_144,
  'kimi-k2.6': 262_144,
  'kimi-k2.5': 262_144,
  'moonshot-v1-8k': 8192,
  'moonshot-v1-32k': 32_768,
  'moonshot-v1-128k': 131_072,
  // MiniMax
  'MiniMax-M3': 1_000_000,
  'MiniMax-M2.7': 204_800,
  'MiniMax-M2.5': 204_800,
  'MiniMax-M2': 204_800,
  // Qwen
  'qwen3.5-plus': 1_000_000,
  'qwen3.5-flash': 1_000_000,
  'qwen3-max': 262_144,
  'qwen-plus': 1_000_000,
  'qwen-flash': 1_000_000,
  'qwen3-vl-plus': 262_144,
  // 智谱 GLM
  'glm-5.3': 1_000_000,
  'glm-5.1': 200_000,
  'glm-5': 200_000,
  'glm-4.7': 200_000,
  'glm-4.6': 200_000,
  'glm-4.5': 131_072,
  'glm-4.5-air': 131_072,
};

/** 供应商 preset id → catalog 模型 id 列表（设置页模型下拉建议） */
export const PROVIDER_MODEL_SUGGESTIONS: Record<string, string[]> = {
  deepseek: ['deepseek-v4-flash', 'deepseek-v4-pro', 'deepseek-chat', 'deepseek-reasoner'],
  moonshot: ['kimi-k3', 'kimi-k2.6', 'kimi-k2-0905-preview', 'moonshot-v1-32k', 'moonshot-v1-128k'],
  zhipu: ['glm-5.3', 'glm-4.7', 'glm-4.5', 'glm-4.5-air'],
  sensenova: ['deepseek-v4-flash', 'sensenova-u1.5-lite'],
  anthropic: ['claude-sonnet-4-5', 'claude-opus-4-1'],
};

/** 模型目录 maxOutputTokens（设置参数联动提示用） */
export const MODEL_MAX_OUTPUT: Record<string, number> = {
  'deepseek-v4-flash': 384_000,
  'deepseek-v4-pro': 384_000,
  'kimi-k3': 131_072,
  'kimi-k2.6': 98_304,
  'kimi-k2.5': 98_304,
  'glm-5.3': 128_000,
  'glm-5.1': 64_000,
  'glm-4.7': 131_072,
  'glm-4.5': 98_304,
  'glm-4.5-air': 98_304,
  'qwen3.5-plus': 65_536,
  'qwen3.5-flash': 65_536,
};

export function lookupMaxOutput(model: string): number | undefined {
  const m = (model || '').trim().toLowerCase();
  for (const [id, v] of Object.entries(MODEL_MAX_OUTPUT)) {
    if (id.toLowerCase() === m) return v;
  }
  return undefined;
}

/** 精确查模型窗口；未收录返回 undefined（由调用方家族回退/默认处理） */
export function lookupContextWindow(model: string): number | undefined {
  const m = (model || '').trim();
  if (!m) return undefined;
  const direct = MODEL_WINDOWS[m];
  if (direct) return direct;
  // 大小写/前后缀容错：catalog 中 model id 全小写匹配
  const lower = m.toLowerCase();
  for (const [id, w] of Object.entries(MODEL_WINDOWS)) {
    if (id.toLowerCase() === lower) return w;
  }
  return undefined;
}
