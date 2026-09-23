/**
 * 桌面端本地配置（localStorage `my-agent-config`）：
 * 对话模型三件套 + 视觉模型三件套 + 工作目录。
 * 视觉模型经 /api/config 下发到后端运行时（后端重启后由 connectBackend 重新同步）。
 */

export interface AppConfig {
  apiKey: string;
  model: string;
  baseUrl: string;
  /** 用户给 Agent 起的名字（写入系统提示词；空 = 默认「小悟」） */
  agentName: string;
  workDir: string;
  visionModel: string;
  visionBaseUrl: string;
  visionApiKey: string;
  onboarded: boolean;
}

const KEY = 'my-agent-config';

export const DEFAULT_CONFIG: AppConfig = {
  apiKey: '',
  // deepseek-flash = DeepSeek V4.1 Flash（2026-09-23 实测：原生工具调用/流式/
  // reasoning 回传/JSON 全过，1M 上下文、自带视觉、定价 0）
  model: 'deepseek-flash',
  // 端点默认仍是商汤外网（任何网络都可达）。想走内网网关（约 0.7s、不计费、
  // 无限流，.env 现在就是这么配的）在设置里把 baseUrl 改成
  // http://172.16.10.242:3000/v1 即可 —— 你选的端点会被保留（见 loadConfig）。
  baseUrl: 'https://token.sensenova.cn/v1',
  agentName: '小悟',
  workDir: '',
  visionModel: '',
  visionBaseUrl: '',
  visionApiKey: '',
  onboarded: false,
};

/** 从 localStorage 读取配置；缺失字段回默认值。
 *  密钥不落 localStorage：Electron 下由 App 启动时经 safeStorage 解密注入
 *  secureCache，这里用缓存覆盖返回（明文只在内存）。 */
const secureCache: { apiKey: string; visionApiKey: string } = { apiKey: '', visionApiKey: '' };
let secureInUse = false;

/** Electron 环境探测到 safeStorage 可用后调用：启用"localStorage 剥离密钥"模式 */
export function markSecureInUse(): void {
  secureInUse = true;
}

/** 注入解密后的密钥（App 启动时从主进程 secure:get 拉取） */
export function setSecureCache(patch: { apiKey?: string; visionApiKey?: string }): void {
  if (patch.apiKey !== undefined) secureCache.apiKey = patch.apiKey;
  if (patch.visionApiKey !== undefined) secureCache.visionApiKey = patch.visionApiKey;
}

/** 保存密钥到安全层（App/设置保存时调用；内部走 desktopApi.secureSet 加密落盘） */
export function persistSecureKey(name: 'apiKey' | 'visionApiKey', value: string): void {
  secureCache[name] = value;
  void window.desktopApi?.secureSet?.(name, value).catch(() => {});
}

/** 有效显示名：配置名 → 回退默认「小悟」（界面壳与系统提示共用同一口径） */
export function displayAgentName(name?: string): string {
  const v = (name ?? '').trim();
  return v || DEFAULT_CONFIG.agentName || '小悟';
}

export function loadConfig(): AppConfig {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const saved = { ...DEFAULT_CONFIG, ...JSON.parse(raw) };
      // 注：这里曾有一条迁移，把指向内网网关（172.16.10.242:3000）的配置**强制改回**
      // 商汤外网，当时的依据是"该网关不稳定、与 .env 不一致"。2026-09-23 复测推翻了
      // 这两个依据：内网网关 12/12 成功、中位 0.7s、不计费无限流，且 .env 的主模型
      // 现在就走它；同期商汤免费 plan 配额已满（429）。所以该迁移已删除——用户选的
      // 端点应当被保留，而不是每次加载都被悄悄改掉。
      // 迁移：商汤免费 plan 已 rpm/tpm 限流（429），模型切回默认（端点同上）
      if (saved.baseUrl === 'https://token.sensenova.cn/v1' && saved.model === 'sensenova-6.8-flash-lite') {
        return { ...saved, model: DEFAULT_CONFIG.model, baseUrl: DEFAULT_CONFIG.baseUrl, apiKey: '' };
      }
      // 密钥用安全缓存覆盖（localStorage 里已无明文）
      return {
        ...saved,
        apiKey: secureCache.apiKey || saved.apiKey,
        visionApiKey: secureCache.visionApiKey || saved.visionApiKey,
      };
    }
  } catch { /* ignore */ }
  return { ...DEFAULT_CONFIG, apiKey: secureCache.apiKey, visionApiKey: secureCache.visionApiKey };
}

export function saveConfig(cfg: AppConfig): void {
  const persist = { ...cfg };
  if (secureInUse) {
    // 明文不进 localStorage（安全缓存持有，persistSecureKey 落盘密文）
    persist.apiKey = '';
    persist.visionApiKey = '';
    if (cfg.apiKey) secureCache.apiKey = cfg.apiKey;
    if (cfg.visionApiKey) secureCache.visionApiKey = cfg.visionApiKey;
  }
  localStorage.setItem(KEY, JSON.stringify(persist));
}

/** 是否需要首次引导（已完成过一次引导后不再强制弹出，即使留空了 API Key） */
export function needsOnboarding(cfg: AppConfig): boolean {
  if (cfg.onboarded) return false;
  return !cfg.apiKey || !cfg.model || !cfg.workDir;
}
