/**
 * 设置中心（分栏导航式）：左侧导航 + 右侧内容区。
 * - 模型供应商：预设卡片（DeepSeek / Kimi / 智谱 / Anthropic / 自定义），选中即写入
 *   appConfig 并经 syncRuntimeConfig 下发到后端运行时；API Key 走 safeStorage 加密。
 * - 技能 Skills：只读能力面板（与右栏「技能」tab 同源）。
 * - 通用：主题切换。
 * 通过 window 事件 open-settings 打开（左栏「设置」按钮派发）。
 */
import React, { useMemo, useState } from 'react';
import {
  Cpu, Zap, Settings as SettingsIcon, Info, X, Check, ExternalLink, ShieldCheck, Plus, Sparkles,
} from 'lucide-react';
import { loadConfig, saveConfig, persistSecureKey, displayAgentName, type AppConfig } from '../lib/appConfig';
import { syncRuntimeConfig, testLlmConnection, refreshShellTitle } from '../lib/backend';
import { useParamsStore, useUIStore } from '../store';
import { SkillPane } from './SkillPane';
import { PROVIDER_MODEL_SUGGESTIONS, lookupMaxOutput } from '../lib/modelCatalog';

/** 用户自定义模型（设置里可增删改，区别于只读预设；key 仍走 safeStorage） */
interface ExtraProvider {
  id: string;           // x-<ts>
  name: string;
  baseUrl: string;
  model: string;
}
const EXTRA_KEY = 'my-agent-extra-providers';
function loadExtras(): ExtraProvider[] {
  try { return JSON.parse(localStorage.getItem(EXTRA_KEY) || '[]'); } catch { return []; }
}
function saveExtras(list: ExtraProvider[]) {
  try { localStorage.setItem(EXTRA_KEY, JSON.stringify(list)); } catch { /* 忽略 */ }
}

/** 预设模型供应商（OpenAI 兼容端点；名称/端点/推荐模型） */
const PROVIDERS = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    desc: '高性价比通用与推理模型',
    baseUrl: 'https://api.deepseek.com/v1',
    models: 'deepseek-chat · deepseek-reasoner',
    site: 'https://platform.deepseek.com',
  },
  {
    id: 'moonshot',
    name: 'Moonshot Kimi',
    desc: '长上下文与智能体任务',
    baseUrl: 'https://api.moonshot.cn/v1',
    models: 'kimi-k2-0905-preview · moonshot-v1-32k',
    site: 'https://platform.moonshot.cn',
  },
  {
    id: 'zhipu',
    name: '智谱 GLM',
    desc: 'GLM 系列通用/视觉模型',
    baseUrl: 'https://open.bigmodel.cn/api/paas/v4',
    models: 'glm-4.5 · glm-4.5-air',
    site: 'https://open.bigmodel.cn',
  },
  {
    id: 'sensenova',
    name: '商汤 SenseNova',
    desc: '免费 Token Plan（当前 .env 默认）',
    baseUrl: 'https://token.sensenova.cn/v1',
    models: 'deepseek-v4-flash · sensenova-u1.5-lite（文生图）',
    site: 'https://token.sensenova.cn',
  },
  {
    id: 'anthropic',
    name: 'Anthropic',
    desc: '旗舰长上下文系列（OpenAI 兼容网关或直连）',
    baseUrl: 'https://api.anthropic.com/v1',
    models: 'claude-sonnet-4-5 · claude-opus-4-1',
    site: 'https://console.anthropic.com',
  },
  {
    id: 'custom',
    name: '自定义 / OpenAI 兼容',
    desc: '任意 OpenAI 兼容端点（含本地 Ollama / vLLM）',
    baseUrl: '',
    models: '',
    site: '',
  },
] as const;

type TabId = 'providers' | 'skills' | 'general' | 'about';

const NAV: { id: TabId; label: string; icon: React.ReactNode }[] = [
  { id: 'providers', label: '模型供应商', icon: <Cpu size={14} /> },
  { id: 'skills', label: '技能 Skills', icon: <Zap size={14} /> },
  { id: 'general', label: '通用', icon: <SettingsIcon size={14} /> },
  { id: 'about', label: '关于', icon: <Info size={14} /> },
];

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<TabId>('providers');
  return (
    <div className="settings-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="settings-modal">
        <div className="settings-nav">
          <div className="settings-brand">
            <ShieldCheck size={15} /> {displayAgentName(useUIStore((s) => s.agentName))} · 设置
          </div>
          {NAV.map((n) => (
            <button
              key={n.id}
              className={`settings-nav-item ${tab === n.id ? 'active' : ''}`}
              onClick={() => setTab(n.id)}
            >
              {n.icon} {n.label}
            </button>
          ))}
          <button className="collapse-btn settings-close" title="关闭" onClick={onClose}><X size={15} /></button>
        </div>
        <div className="settings-body">
          {tab === 'providers' && <ProvidersTab />}
          {tab === 'skills' && (
            <div className="settings-pane">
              <h3>技能 Skills</h3>
              <p className="settings-hint">
                技能 = <code>SKILL.md</code> 技能包；任务描述命中触发词时自动注入对应说明。
                下面与右栏「技能」tab 同源；安装：<code>my-agent --skills-install &lt;git-url&gt;</code>。
              </p>
              <div className="settings-skillbox"><SkillPane /></div>
            </div>
          )}
          {tab === 'general' && <GeneralTab />}
          {tab === 'about' && <AboutTab />}
        </div>
      </div>
    </div>
  );
}

/* ---------------- 模型供应商 ---------------- */

function ProvidersTab() {
  // cfg 用受控 state：保存后立即重读，让「当前」徽章/端点即时刷新
  const [cfg, setCfg] = useState<AppConfig>(() => loadConfig());
  const { providerOrder, setProviderOrder } = useUIStore();
  const params = useParamsStore((st) => st.params);
  const setParam = useParamsStore((st) => st.setParam);
  const [extras, setExtras] = useState<ExtraProvider[]>(loadExtras);
  const [editing, setEditing] = useState<string | null>(null);
  const [savedFlash, setSavedFlash] = useState('');
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [paramFlash, setParamFlash] = useState(false);

  const updateExtras = (list: ExtraProvider[]) => { setExtras(list); saveExtras(list); };

  const activeProvider =
    PROVIDERS.find((p) => p.baseUrl && p.baseUrl === cfg.baseUrl)?.id
    || extras.find((x) => x.baseUrl && x.baseUrl === cfg.baseUrl)?.id
    || 'custom';

  // 预设卡片顺序：先按用户拖拽顺序（providerOrder），未收录的 id 追加在尾
  const orderedPresets = useMemo(() => {
    type P = (typeof PROVIDERS)[number];
    if (!providerOrder.length) return [...PROVIDERS] as P[];
    const map = new Map<string, P>(PROVIDERS.map((p) => [p.id, p] as const));
    const out: P[] = [];
    for (const id of providerOrder) {
      const p = map.get(id);
      if (p) { out.push(p); map.delete(id); }
    }
    out.push(...map.values());
    return out;
  }, [providerOrder]);

  const commitOrder = (from: number, to: number) => {
    if (from === to) return;
    const ids = orderedPresets.map((p) => p.id);
    const [moved] = ids.splice(from, 1);
    ids.splice(to, 0, moved);
    setProviderOrder(ids);
  };

  const save = (patch: Partial<AppConfig>, pid: string, extraName?: string) => {
    const next = { ...loadConfig(), ...patch };
    if (patch.apiKey !== undefined) persistSecureKey('apiKey', patch.apiKey);
    saveConfig(next);
    setCfg(next); // 立即刷新「当前」徽章与端点展示
    void syncRuntimeConfig();
    setSavedFlash(pid);
    setTimeout(() => setSavedFlash(''), 1600);
    // 自定义模型同步更新其条目（端点/模型变更后下次仍可一键切回）
    if (pid.startsWith('x-')) {
      updateExtras(extras.map((x) => x.id === pid
        ? { ...x, name: extraName?.trim() || x.name, baseUrl: patch.baseUrl ?? x.baseUrl, model: patch.model ?? x.model }
        : x));
    }
  };

  const addExtra = () => {
    const id = `x-${Date.now()}`;
    updateExtras([...extras, { id, name: '新模型', baseUrl: '', model: '' }]);
    setEditing(id);
  };

  const removeExtra = (id: string) => {
    updateExtras(extras.filter((x) => x.id !== id));
    if (editing === id) setEditing(null);
  };

  const numParam = (k: 'temperature' | 'topP' | 'maxTokens' | 'maxOps', max: number) => (
    <label className="param-field">
      {k === 'temperature' ? 'Temperature' : k === 'topP' ? 'Top P'
        : k === 'maxTokens' ? 'Max Tokens' : '任务最大轮数 Max Ops（0=默认80）'}
      <input
        type="number"
        step={k === 'maxTokens' ? 256 : k === 'maxOps' ? 10 : 0.05}
        min={0}
        max={max}
        value={params[k]}
        onChange={(e) => {
          const v = parseFloat(e.target.value);
          if (!Number.isNaN(v)) setParam(k, v);
        }}
      />
    </label>
  );

  const providerCard = (
    key: string,
    name: string,
    desc: string,
    url: string,
    models: string,
    isActive: boolean,
    idx: number,
    extra?: ExtraProvider,
  ) => {
    const isOpen = editing === key;
    return (
      <div
        key={key}
        className={`provider-card ${isActive ? 'active' : ''} ${dragIdx === idx ? 'dragging' : ''}`}
        draggable={!extra}
        onDragStart={() => setDragIdx(idx)}
        onDragEnd={() => setDragIdx(null)}
        onDragOver={(e) => e.preventDefault()}
        onDrop={() => { if (dragIdx !== null) commitOrder(dragIdx, idx); setDragIdx(null); }}
      >
        <button className="provider-head" onClick={() => setEditing(isOpen ? null : key)} title={isOpen ? '收起' : '点按展开设置'}>
          <span className="provider-name">{name}</span>
          {extra && <span className="provider-badge extra"><Sparkles size={9} /> 自定义</span>}
          {isActive && <span className="provider-badge"><Check size={10} /> 当前</span>}
          <span className="provider-set">{isOpen ? '收起' : '设置'}</span>
        </button>
        <div className="provider-desc">{desc}</div>
        {url && (
          <div className="provider-url"><ExternalLink size={10} /> {url}</div>
        )}
        {models && !extra && <div className="provider-models">{models}</div>}
        {isOpen && (
          <ProviderForm
            pid={key}
            baseUrl={extra ? extra.baseUrl : (url || cfg.baseUrl || '')}
            model={isActive ? cfg.model : (extra ? extra.model : '')}
            extraName={extra?.name}
            savedFlash={savedFlash === key}
            onSave={save}
            onRemove={extra ? () => removeExtra(key) : undefined}
          />
        )}
      </div>
    );
  };

  return (
    <div className="settings-pane">
      <h3>模型供应商</h3>
      <p className="settings-hint">
        选择供应商并填入 API Key（本地 safeStorage 加密保存，不落明文）。
        保存后立即生效并下发到后端运行时；主对话与视觉模型可分别配置。
        预设卡片可<strong>拖拽排序</strong>；点卡片上的「设置」展开编辑，
        或<strong>添加自定义模型</strong>（任意 OpenAI 兼容端点）。
      </p>
      <div className="provider-grid">
        {orderedPresets.map((p, idx) => providerCard(p.id, p.name, p.desc, p.baseUrl || '', p.models || '', activeProvider === p.id, idx))}
        {extras.map((x, idx) => providerCard(x.id, x.name || '未命名模型', x.model || '自定义端点', x.baseUrl, '', activeProvider === x.id, orderedPresets.length + idx, x))}
      </div>
      <button className="btn add-provider" onClick={addExtra}>
        <Plus size={13} /> 添加自定义模型
      </button>

      <h3 style={{ marginTop: 20 }}>模型参数（随下次请求生效）</h3>
      <div className="param-row">
        {numParam('temperature', 2)}
        {numParam('topP', 1)}
        {numParam('maxTokens', 1000000)}
        {numParam('maxOps', 600)}
        <span className="param-hint">temperature 采样温度 · topP 核采样 · maxTokens 单次输出上限</span>
      </div>
      {(() => {
        const cap = lookupMaxOutput(cfg.model);
        if (cap && params.maxTokens > cap) {
          return (
            <div className="param-warn">
              当前模型 {cfg.model} 目录上限为 {cap.toLocaleString()} tokens，
              你设置的 {params.maxTokens.toLocaleString()} 会被服务端截断或报错。
            </div>
          );
        }
        return null;
      })()}

      <h3 style={{ marginTop: 20 }}>视觉模型（独立端点，可选）</h3>
      <VisionForm onSave={save} />
    </div>
  );
}

function ProviderForm({ pid, baseUrl, model, extraName, savedFlash, onSave, onRemove }: {
  pid: string;
  baseUrl: string;
  model: string;
  extraName?: string;
  savedFlash: boolean;
  onSave: (patch: Partial<AppConfig>, pid: string, extraName?: string) => void;
  onRemove?: () => void;
}) {
  const [url, setUrl] = useState(baseUrl);
  const [m, setM] = useState(model);
  const [nm, setNm] = useState(extraName || '');
  const [key, setKey] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; latency_ms?: number; reply?: string; error?: string; endpoint?: string; model?: string } | null>(null);
  const hasKey = Boolean(loadConfig().apiKey);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    const r = await testLlmConnection({ model: m.trim(), baseUrl: url.trim(), apiKey: key || undefined });
    setTestResult(r);
    setTesting(false);
  };
  return (
    <div className="provider-form">
      {onRemove && (
        <label>
          名称
          <input value={nm} onChange={(e) => setNm(e.target.value)} placeholder="自定义模型名" />
        </label>
      )}
      <label>
        端点 Base URL
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/v1" />
      </label>
      <label>
        模型名称
        <input
          value={m}
          onChange={(e) => setM(e.target.value)}
          placeholder="deepseek-chat"
          list={PROVIDER_MODEL_SUGGESTIONS[pid] ? `ml-${pid}` : undefined}
        />
        {PROVIDER_MODEL_SUGGESTIONS[pid] && (
          <datalist id={`ml-${pid}`}>
            {PROVIDER_MODEL_SUGGESTIONS[pid].map((mm) => <option key={mm} value={mm} />)}
          </datalist>
        )}
      </label>
      <label>
        API Key {hasKey && <span className="key-ok"><Check size={10} /> 已配置（留空则保持不变）</span>}
        <span className="key-hint">Key 为当前全局 Key——切换供应商/模型时请同步更新</span>
        <input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="sk-…" autoComplete="off" />
      </label>
      <div className="provider-form-actions">
        <button
          className="btn primary"
          disabled={!url.trim() || !m.trim()}
          title={(!url.trim() || !m.trim()) ? '请先填写端点 Base URL 与模型名称（防止误清主配置）' : undefined}
          onClick={() => onSave({ baseUrl: url.trim(), model: m.trim(), apiKey: key || undefined }, pid, onRemove ? nm : undefined)}
        >
          {savedFlash ? <><Check size={13} /> 已保存</> : '保存并设为当前'}
        </button>
        <button
          className="btn"
          disabled={!url.trim() || !m.trim() || testing}
          onClick={runTest}
        >
          {testing ? '测试中…' : testResult === null ? '⚡ 测试连接' : '再测一次'}
        </button>
        {onRemove && (
          <button className="btn provider-remove" onClick={onRemove}>删除此模型</button>
        )}
      </div>
      {(!url.trim() || !m.trim()) && (
        <div className="provider-form-warn">端点与模型名称不能为空——留空保存会把主配置清成默认（误变演示版本）</div>
      )}
      {testResult !== null && (
        <div className={`test-result ${testResult.ok ? 'ok' : 'fail'}`}>
          {testResult.ok
            ? <><Check size={12} /> 连接成功 · {testResult.latency_ms}ms · {testResult.reply ? `回复: ${testResult.reply}` : ''}</>
            : <><X size={12} /> 连接失败：{testResult.error || '未知错误'}</>}
          {testResult.endpoint && <span className="test-endpoint">{testResult.endpoint} · {testResult.model}</span>}
        </div>
      )}
    </div>
  );
}

function VisionForm({ onSave }: { onSave: (patch: Partial<AppConfig>, pid: string) => void }) {
  const cfg = loadConfig();
  const [vm, setVm] = useState(cfg.visionModel);
  const [vurl, setVurl] = useState(cfg.visionBaseUrl);
  const [vkey, setVkey] = useState('');
  const hasKey = Boolean(cfg.visionApiKey);
  return (
    <div className="provider-form" style={{ maxWidth: 420 }}>
      <label>
        视觉模型（留空自动选择）
        <input value={vm} onChange={(e) => setVm(e.target.value)} placeholder="同主模型" />
      </label>
      <label>
        视觉端点（留空跟随主模型）
        <input value={vurl} onChange={(e) => setVurl(e.target.value)} placeholder="同主端点" />
      </label>
      <label>
        视觉 API Key {hasKey && <span className="key-ok"><Check size={10} /> 已配置</span>}
        <input type="password" value={vkey} onChange={(e) => setVkey(e.target.value)} placeholder="留空保持不变" autoComplete="off" />
      </label>
      <button
        className="btn primary"
        onClick={() =>
          onSave(
            { visionModel: vm.trim(), visionBaseUrl: vurl.trim(), visionApiKey: vkey || undefined },
            'vision',
          )
        }
      >
        保存视觉配置
      </button>
    </div>
  );
}

/* ---------------- 通用 ---------------- */

function GeneralTab() {
  const { theme, setTheme } = useUIStore();
  return (
    <div className="settings-pane">
      <h3>外观</h3>
      <div className="theme-row">
        <button
          className={`theme-opt ${theme === 'light' ? 'active' : ''}`}
          onClick={() => setTheme('light')}
        >
          <span className="theme-dot" style={{ background: 'linear-gradient(135deg,#ffffff 60%,#0d0d0d 60%)' }} />
          浅色（白 · 推荐）
        </button>
        <button
          className={`theme-opt ${theme === 'dark' ? 'active' : ''}`}
          onClick={() => setTheme('dark')}
        >
          <span className="theme-dot" style={{ background: 'linear-gradient(135deg,#0d0d0d 60%,#f5f5f5 60%)' }} />
          深色（黑）
        </button>
      </div>
      <h3 style={{ marginTop: 20 }}>Agent 名字</h3>
      <div className="form-field">
        <input
          key={loadConfig().agentName}
          defaultValue={loadConfig().agentName}
          placeholder="小悟"
          onBlur={(e) => {
            const name = e.target.value.trim() || '小悟';
            const c = loadConfig();
            saveConfig({ ...c, agentName: name });
            useUIStore.getState().setAgentName(name);
            refreshShellTitle(name);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
          }}
        />
        <div className="form-hint">写入系统提示词，Agent 自称用这个名字；留空恢复「小悟」。</div>
      </div>
      <h3 style={{ marginTop: 20 }}>桌面宠物</h3>
      <PetToggleRow />
      <h3 style={{ marginTop: 20 }}>安全</h3>
      <p className="settings-hint">
        API Key 经 Electron safeStorage 加密落盘，永不明文写入 localStorage；
        审批 / 沙箱等级沿用后端 <code>APPROVAL_POLICY / SANDBOX_MODE</code>，工具调用的
        危险操作在对话流中弹卡确认。
      </p>
    </div>
  );
}

/* ---------------- 桌面宠物开关（挂在通用页） ---------------- */

function PetToggleRow() {
  const { petVisible, setPetVisible } = useUIStore();
  return (
    <button className={`theme-opt ${petVisible ? 'active' : ''}`} style={{ marginTop: 10 }}
      onClick={() => {
        const next = !petVisible;
        setPetVisible(next);
        void window.desktopApi?.petToggle?.(next);
      }}
    >
      <span className="theme-dot" style={{ background: 'radial-gradient(circle at 34% 30%, #fffdf8, #ecdcc8)' }} />
      桌面宠物浮窗 {petVisible ? '（开启 · 可拖拽，任务时冒泡）' : '（关闭）'}
    </button>
  );
}

/* ---------------- 关于 ---------------- */

function AboutTab() {
  const [version, setVersion] = useState('0.6.0');
  const shellName = displayAgentName(useUIStore((s) => s.agentName));
  React.useEffect(() => {
    void window.desktopApi?.getVersion?.().then((v) => { if (v) setVersion(v); }).catch(() => {});
  }, []);
  return (
    <div className="settings-pane">
      <h3>{shellName} Desktop</h3>
      <p className="settings-hint">
        my_agent 桌面客户端 v{version} —— Python 通用 AI Agent 的图形工作台。
        单循环执行 · 多标签会话 · 工作区文件树 · 任务中心 · 技能包 · 审批沙箱 ·
        多引擎运行时（内置 / CLI 子进程）· 内嵌可操控浏览器。
      </p>
      <p className="settings-hint">
        命令行同源：<code>my-agent --doctor</code> 环境自检 · <code>my-agent --dashboard</code> Web 面板。
      </p>
    </div>
  );
}
