/**
 * 设置中心：对话模型 / 视觉模型 / 工作目录 / 生成参数，单页分区式。
 * 首次启动以「欢迎使用」口吻出现（不可关闭），之后从左下角「设置」随时打开（可关闭）。
 * 视觉模型三件套经 POST /api/config 下发后端运行时；工作目录变更触发后端重启生效。
 */
import React, { useEffect, useState } from 'react';
import { Bot, Eye, EyeOff, FolderOpen, Rocket, Settings as SettingsIcon, SlidersHorizontal, X } from 'lucide-react';
import { AppConfig, loadConfig, saveConfig } from '../lib/appConfig';
import { refreshShellTitle } from '../lib/backend';
import { useParamsStore, useUIStore } from '../store';

const FALLBACK_BASE = 'http://127.0.0.1:8090';

interface Props {
  firstRun: boolean;
  onDone: (cfg: AppConfig) => void;
  onClose?: () => void;
}

export function OnboardingModal({ firstRun, onDone, onClose }: Props) {
  const [cfg, setCfg] = useState<AppConfig>(() => loadConfig());
  const [workDirPicker, setWorkDirPicker] = useState(() => loadConfig().workDir);
  const [showKey, setShowKey] = useState(false);
  const [showVisionKey, setShowVisionKey] = useState(false);
  const [visionHint, setVisionHint] = useState('');
  const [saved, setSaved] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState('');
  const { params, setParam } = useParamsStore();

  const set = (patch: Partial<AppConfig>) => { setCfg((c) => ({ ...c, ...patch })); setSaved(false); };

  // 测试连接：用表单当前值（未保存也行）让后端真实调用一次视觉模型
  const testVision = () => {
    setTesting(true);
    setTestResult('');
    void (async () => {
      try {
        const base = (await window.desktopApi?.getBackendBase?.()) || FALLBACK_BASE;
        const vision: Record<string, string> = {};
        if (cfg.visionModel) vision.model = cfg.visionModel;
        if (cfg.visionBaseUrl) vision.base_url = cfg.visionBaseUrl;
        if (cfg.visionApiKey) vision.api_key = cfg.visionApiKey;
        const res = await fetch(`${base}/api/config/test-vision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.keys(vision).length ? { vision } : {}),
        });
        const json = await res.json();
        setTestResult(json.ok
          ? `✅ ${json.model}：${String(json.answer || '').trim().slice(0, 60)}`
          : `❌ ${json.error || '测试失败'}`);
      } catch {
        setTestResult('❌ 后端不可达');
      } finally {
        setTesting(false);
      }
    })();
  };

  // 拉取后端当前生效的视觉配置（作为占位提示，key 只回尾部）
  useEffect(() => {
    void (async () => {
      try {
        const base = (await window.desktopApi?.getBackendBase?.()) || FALLBACK_BASE;
        const res = await fetch(`${base}/api/config`);
        const json = await res.json();
        if (json?.ok) {
          const v = json.vision;
          setVisionHint(`后端当前生效：${v.model || '（自动）'} @ ${v.base_url || '（同主模型）'}${v.key_set ? ` · key ${v.key_tail}` : ''}`);
        }
      } catch { /* 后端不可达时忽略 */ }
    })();
  }, []);

  const finish = () => {
    const finalCfg = { ...cfg, workDir: workDirPicker || cfg.workDir, onboarded: true };
    saveConfig(finalCfg);
    // 改名即时同步界面壳（状态栏/标题/窗口标题）
    useUIStore.getState().setAgentName(finalCfg.agentName || '小悟');
    refreshShellTitle(finalCfg.agentName || '小悟');
    onDone(finalCfg);
    // 密钥：安全层可用时加密落盘（saveConfig 已把明文从 localStorage 剥离）
    if (window.desktopApi?.secureSet) {
      void window.desktopApi.secureSet('apiKey', finalCfg.apiKey);
      void window.desktopApi.secureSet('visionApiKey', finalCfg.visionApiKey);
    }
    // 视觉模型三件套 → 后端运行时覆盖（后端重启后 connectBackend 会自动重新同步）
    void (async () => {
      try {
        const base = (await window.desktopApi?.getBackendBase?.()) || FALLBACK_BASE;
        const vision: Record<string, string> = {};
        if (finalCfg.visionModel) vision.model = finalCfg.visionModel;
        if (finalCfg.visionBaseUrl) vision.base_url = finalCfg.visionBaseUrl;
        if (finalCfg.visionApiKey) vision.api_key = finalCfg.visionApiKey;
        await fetch(`${base}/api/config`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ vision }),
        });
      } catch { /* 忽略：连接后会重新同步 */ }
    })();
    // 多工作区：工作目录变化 → 后端以新目录重启生效
    if (finalCfg.workDir && finalCfg.workDir !== cfg.workDir) {
      void window.desktopApi?.backendRestart?.(finalCfg.workDir);
    }
  };

  const sliders = ([
    { key: 'temperature', label: '温度 Temperature', min: 0, max: 2, step: 0.1, fmt: (v: number) => v.toFixed(1) },
    { key: 'topP', label: 'Top P', min: 0, max: 1, step: 0.05, fmt: (v: number) => v.toFixed(2) },
    { key: 'maxTokens', label: '最大输出 Max Tokens', min: 256, max: 32768, step: 256, fmt: (v: number) => String(v) },
    { key: 'maxOps', label: '任务最大轮数 Max Ops（0=系统默认80）', min: 0, max: 600, step: 10, fmt: (v: number) => String(v) },
  ] as const);

  return (
    <div className="modal-overlay">
      <div className="modal" style={{ position: 'relative' }}>
        <h2 className="sr-only">{firstRun ? '欢迎使用' : '设置'}</h2>
        <div className="modal-header">
          <h2>
            {firstRun
              ? <><Rocket size={16} style={{ display: 'inline', marginRight: 6 }} />欢迎使用 {cfg.agentName || '小悟'} Desktop</>
              : <><SettingsIcon size={16} style={{ display: 'inline', marginRight: 6 }} />设置</>}
          </h2>
          <p>{firstRun ? '配好这几块就能开干：名字 · 对话模型 · 视觉模型 · 工作目录' : '名字 · 对话模型 · 视觉模型 · 工作目录 · 生成参数'}</p>
          {!firstRun && onClose && (
            <button className="modal-close" title="关闭" onClick={onClose}><X size={16} /></button>
          )}
        </div>

        <div className="modal-body">
          {/* ---- 给它起个名字（写入系统提示词） ---- */}
          <div className="settings-section">
            <h3><Bot size={12} /> 给它起个名字</h3>
            <div className="form-field">
              <label>Agent 名字</label>
              <input
                placeholder="默认：小悟"
                value={cfg.agentName}
                onChange={(e) => set({ agentName: e.target.value })}
              />
              <div className="form-hint">
                会写入系统提示词——Agent 用这个名字自称（留空 = 默认「小悟」，可随时在 设置 → 通用 里改）。
              </div>
            </div>
          </div>

          {/* ---- 对话模型 ---- */}
          <div className="settings-section">
            <h3><Bot size={12} /> 对话模型</h3>
            <div className="form-field">
              <label>API Key</label>
              <div className="key-row">
                <input
                  type={showKey ? 'text' : 'password'}
                  placeholder="sk-...（可留空走演示模式）"
                  value={cfg.apiKey}
                  onChange={(e) => set({ apiKey: e.target.value })}
                />
                <button className="eye-btn" title={showKey ? '隐藏' : '显示'}
                  onClick={() => setShowKey((v) => !v)}>
                  {showKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
            </div>
            <div className="form-field">
              <label>模型名称</label>
              <input value={cfg.model} onChange={(e) => set({ model: e.target.value })} />
            </div>
            <div className="form-field">
              <label>API 端点</label>
              <input value={cfg.baseUrl} onChange={(e) => set({ baseUrl: e.target.value })} />
              <div className="form-hint">OpenAI 兼容端点；composer 上的模型 chip 修改的也是这里。</div>
            </div>
          </div>

          {/* ---- 视觉模型 ---- */}
          <div className="settings-section">
            <h3><Eye size={12} /> 视觉模型（截图理解 / computer 看屏幕）</h3>
            <div className="form-field">
              <label>视觉模型名称</label>
              <input
                placeholder="如 gpt-4o-mini / qwen-vl-plus（留空用 .env 的 VISION_MODEL）"
                value={cfg.visionModel}
                onChange={(e) => set({ visionModel: e.target.value })}
              />
            </div>
            <div className="form-field">
              <label>视觉端点</label>
              <input
                placeholder="留空用 .env 的 VISION_BASE_URL"
                value={cfg.visionBaseUrl}
                onChange={(e) => set({ visionBaseUrl: e.target.value })}
              />
            </div>
            <div className="form-field">
              <label>视觉 API Key</label>
              <div className="key-row">
                <input
                  type={showVisionKey ? 'text' : 'password'}
                  placeholder="留空用 .env 的 VISION_API_KEY"
                  value={cfg.visionApiKey}
                  onChange={(e) => set({ visionApiKey: e.target.value })}
                />
                <button className="eye-btn" title={showVisionKey ? '隐藏' : '显示'}
                  onClick={() => setShowVisionKey((v) => !v)}>
                  {showVisionKey ? <EyeOff size={14} /> : <Eye size={14} />}
                </button>
              </div>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 4 }}>
              <button className="btn" onClick={testVision} disabled={testing || (!cfg.visionModel && !cfg.visionBaseUrl && !cfg.visionApiKey)}>
                {testing ? '测试中…' : '测试连接'}
              </button>
              {testResult && <span style={{ fontSize: 12, color: testResult.startsWith('✅') ? 'var(--color-success)' : 'var(--color-error)' }}>{testResult}</span>}
            </div>
            {visionHint && <div className="form-hint">{visionHint}</div>}
          </div>

          {/* ---- 工作目录 ---- */}
          <div className="settings-section">
            <h3><FolderOpen size={12} /> 工作目录</h3>
            <div className="form-field">
              <label>Agent 工作目录（绝对路径）</label>
              <input
                placeholder="D:\projects\demo（留空使用项目根目录）"
                value={workDirPicker}
                onChange={(e) => setWorkDirPicker(e.target.value)}
              />
              <div className="form-hint">更改保存后，后端会以该目录自动重启生效（文件树 / 回滚 / 会话均落在该工作区内）。</div>
            </div>
          </div>

          {/* ---- 生成参数 ---- */}
          <div className="settings-section">
            <h3><SlidersHorizontal size={12} /> 生成参数</h3>
            {sliders.map((r) => (
              <div key={r.key} className="form-field">
                <label>{r.label}：{r.fmt(params[r.key])}</label>
                <input
                  type="range"
                  min={r.min} max={r.max} step={r.step}
                  value={params[r.key]}
                  onChange={(e) => setParam(r.key, Number(e.target.value))}
                  style={{ width: '100%', accentColor: 'var(--accent)' }}
                />
              </div>
            ))}
          </div>
        </div>

        <div className="modal-footer">
          {saved && <span className="settings-saved">已保存 ✓</span>}
          {!firstRun && onClose && (
            <button className="btn" onClick={onClose}>取消</button>
          )}
          <button
            className="btn primary"
            onClick={() => { setSaved(true); finish(); }}
          >
            {firstRun ? '完成，开始使用' : '保存'}
          </button>
        </div>
      </div>
    </div>
  );
}
