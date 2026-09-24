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
  // 默认模型：原生工具调用 / 流式 / reasoning 回传 / JSON 均可用，
  // 1M 上下文、自带视觉、零定价
  model: 'deepseek-flash',
  // 端点默认与 .env 一致（任何网络可达；限流缺点见下）
  // 备选：自建网关 —— 中位 0.7s、不计费、不限流，但需网络可达；
  // 在设置里改 baseUrl 即可，用户选的端点会被保留（见 loadConfig）。
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
      // 注：这里曾有一条迁移，把用户指向自建网关的配置强制改回默认端点。
      // 已删除：自建网关是正当备选（成功率高、不计费不限流），
      // 「把用户选的端点悄悄改掉」会让想用它的人每次加载都被弹回去。
      // 保留的迁移：默认端点免费档有 rpm/tpm 限流（429），命中时模型切回默认。
      // 端点本身始终由用户决定。
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
