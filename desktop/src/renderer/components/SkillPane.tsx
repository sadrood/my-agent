/**
 * 技能面板：「Agent 能力」列表。
 * 数据源：后端 /api/skills（agent.skills.SkillManager.discover 只读枚举）。
 * 定位：告知层——展示有什么技能、怎么触发；绝不在此执行任何脚本
 *（执行仍走终端工具 → 审批门原样生效，见 agent/skills.py 安全说明）。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Search, Zap, FileCode2, ChevronDown, ChevronRight, Sparkles } from 'lucide-react';
import { fetchSkills, type SkillInfo } from '../lib/backend';

export function SkillPane() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [openScripts, setOpenScripts] = useState<Set<string>>(new Set());

  const reload = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const list = await fetchSkills();
      setSkills(list);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void reload(); }, [reload]);

  // 技能目录变化（安装/卸载）时自动刷新
  useEffect(() => {
    const onTick = () => { void reload(); };
    window.addEventListener('skills-changed', onTick);
    return () => window.removeEventListener('skills-changed', onTick);
  }, [reload]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter(
      (s) =>
        s.name.toLowerCase().includes(q) ||
        (s.description || '').toLowerCase().includes(q) ||
        s.triggers.some((t) => t.toLowerCase().includes(q)),
    );
  }, [skills, query]);

  const toggleScripts = (name: string) => {
    setOpenScripts((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="skill-pane">
      <div className="skill-head">
        <span className="skill-count">
          <Zap size={12} /> {skills.length} 个技能
        </span>
        <button className="collapse-btn" title="刷新" onClick={() => void reload()}>
          <RefreshCw size={12} className={loading ? 'spin' : ''} />
        </button>
      </div>
      <div className="skill-search">
        <Search size={12} />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="按名称 / 描述 / 触发词过滤"
        />
      </div>

      {error && <div className="skill-empty">加载失败：{error}</div>}

      {!error && !loading && filtered.length === 0 && (
        <div className="skill-empty">
          <Sparkles size={22} style={{ opacity: 0.6 }} />
          <div style={{ marginTop: 8 }}>还没有技能包</div>
          <div style={{ fontSize: 11.5, marginTop: 6, lineHeight: 1.7 }}>
            在项目 <code>skills/&lt;技能名&gt;/SKILL.md</code> 创建技能文件，
            <br />或运行 <code>my-agent --skills-install &lt;git-url&gt;</code> 安装。
            <br />触发词命中的任务会自动注入对应技能说明。
          </div>
        </div>
      )}

      <div className="skill-list">
        {filtered.map((s) => (
          <div key={s.name} className="skill-card">
            <div className="skill-name">
              <Zap size={12} />
              <span>{s.name}</span>
            </div>
            {s.description && <div className="skill-desc">{s.description}</div>}
            {s.triggers.length > 0 && (
              <div className="skill-triggers">
                {s.triggers.slice(0, 6).map((t) => (
                  <span key={t} className="skill-trigger">{t}</span>
                ))}
                {s.triggers.length > 6 && <span className="skill-trigger more">+{s.triggers.length - 6}</span>}
              </div>
            )}
            {s.scripts.length > 0 && (
              <button className="skill-scripts-toggle" onClick={() => toggleScripts(s.name)}>
                {openScripts.has(s.name) ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                <FileCode2 size={11} />
                {s.scripts.length} 个脚本（执行仍需审批）
              </button>
            )}
            {openScripts.has(s.name) && (
              <div className="skill-scripts">
                {s.scripts.map((p) => (
                  <div key={p} className="skill-script" title={p}>{p}</div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
